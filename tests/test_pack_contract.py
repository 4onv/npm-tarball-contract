import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import pack_contract as contract


ROOT = Path(__file__).resolve().parents[1]


def archive_members(entries):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for member, data in entries:
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


class ArchiveTests(unittest.TestCase):
    def test_inspection_has_exact_digest_sizes_and_no_payload(self):
        blob = contract.synthetic_archive("1.0.0", {"dist/index.js": b"PRIVATE_FILE_CONTENT"})
        report = contract.inspect_archive(blob)
        self.assertEqual(report["package"], {"name": "@demo/hello", "version": "1.0.0"})
        self.assertEqual(report["gzipBytes"], len(blob))
        self.assertEqual(report["sha256"], hashlib.sha256(blob).hexdigest())
        self.assertEqual(report["files"]["dist/index.js"], {
            "bytes": 20, "sha256": hashlib.sha256(b"PRIVATE_FILE_CONTENT").hexdigest(), "executable": False,
        })
        self.assertNotIn("PRIVATE_FILE_CONTENT", json.dumps(report))

    def test_diff_reports_equal_size_edits_and_distinct_members(self):
        before = contract.inspect_archive(contract.synthetic_archive("1.0.0", {"a.js": b"A", "b.js": b"B"}))
        after = contract.inspect_archive(contract.synthetic_archive("1.0.1", {"a.js": b"C", "c.js": b"C"}))
        result = contract.diff_archives(before, after)
        self.assertEqual(result["contentBytesChange"], 0)
        self.assertFalse(result["archiveBytesMatch"])
        self.assertEqual([row["path"] for row in result["added"]], ["c.js"])
        self.assertEqual([row["path"] for row in result["removed"]], ["b.js"])
        self.assertEqual([row["path"] for row in result["changed"]], ["a.js", "package.json"])
        self.assertEqual(result["changed"][0]["before"]["bytes"], result["changed"][0]["after"]["bytes"])

    def test_diff_records_executable_mode_only_edit(self):
        manifest = (tarfile.TarInfo("package/package.json"), b'{"name":"x","version":"1.0.0"}')
        cli = tarfile.TarInfo("package/bin/cli.js")
        before = contract.inspect_archive(archive_members([manifest, (cli, b"abc")]))
        cli.mode = 0o755
        after = contract.inspect_archive(archive_members([manifest, (cli, b"abc")]))
        [change] = contract.diff_archives(before, after)["changed"]
        self.assertEqual(change["path"], "bin/cli.js")
        self.assertEqual(change["before"]["sha256"], change["after"]["sha256"])
        self.assertTrue(change["after"]["executable"])

    def test_truncated_and_non_tar_inputs_are_controlled_errors(self):
        example = contract.synthetic_archive("1.0.0", {})
        for data in (example[:-5], b"not gzip", gzip.compress(b"not tar"),
                     gzip.compress(gzip.decompress(example)[:-1])):
            with self.subTest(size=len(data)), self.assertRaises(contract.InputError):
                contract.inspect_archive(data)
        with patch.object(contract, "MAX_TAR", 512), self.assertRaisesRegex(contract.InputError, "Expanded"):
            contract.inspect_archive(example)

    def test_case_collisions_and_nonportable_file_layout_fail(self):
        for names, error in [
            (["dist/index.js", "Dist/types.d.ts"], "case-insensitive"),
            (["dist", "dist/types.d.ts"], "parent directory"),
            (["dist/./types.d.ts"], "unsupported path"),
        ]:
            with self.subTest(names=names), self.assertRaisesRegex(contract.InputError, error):
                contract.inspect_archive(contract.synthetic_archive("1.0.0", dict.fromkeys(names, b"example")))

    def test_archive_rejects_duplicate_members_and_nonregular_members(self):
        manifest = (tarfile.TarInfo("package/package.json"), b'{"name":"x","version":"1.0.0"}')
        path = tarfile.TarInfo("package/bin/cli.js")
        with self.assertRaisesRegex(contract.InputError, "duplicate"):
            contract.inspect_archive(archive_members([manifest, (path, b"A"), (path, b"A")]))
        path.type = tarfile.SYMTYPE
        path.linkname = "package.json"
        with self.assertRaisesRegex(contract.InputError, "unsupported"):
            contract.inspect_archive(archive_members([manifest, (path, b"")]))

    def test_manifest_must_be_present_and_valid_json(self):
        with self.assertRaisesRegex(contract.InputError, "missing package/package.json"):
            contract.inspect_archive(archive_members([(tarfile.TarInfo("package/README.md"), b"sample")]))
        duplicate = b'{"name":"one","name":"two","version":"1.0.0"}'
        with self.assertRaisesRegex(contract.InputError, "unique object keys"):
            contract.inspect_archive(archive_members([(tarfile.TarInfo("package/package.json"), duplicate)]))


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.blob = contract.synthetic_archive("1.0.0", {
            "dist/index.js": b"require('./helper');", "dist/helper.js": b"true", "dist-extra/test.js": b"test",
        })
        self.report = contract.inspect_archive(self.blob)

    def test_path_prefix_is_literal_and_tracks_file_budgets(self):
        result = contract.check_contract(self.report, {
            "version": 1, "package": "@demo/hello", "allow": ["package.json", "dist/"],
            "require": ["dist/index.d.ts"], "fileMaxBytes": {"dist/index.js": 4},
        })
        self.assertEqual(result["status"], "fail")
        self.assertEqual([(row["code"], row["path"]) for row in result["violations"]], [
            ("missing-file", "dist/index.d.ts"), ("unexpected-file", "dist-extra/test.js"),
            ("file-budget", "dist/index.js"),
        ])

    def test_draft_round_trip_and_inclusive_total_budgets(self):
        policy = contract.draft_policy(self.report)
        policy["maxGzipBytes"] = self.report["gzipBytes"]
        policy["maxContentBytes"] = self.report["contentBytes"]
        self.assertEqual(contract.check_contract(self.report, policy)["status"], "pass")
        policy["maxGzipBytes"] -= 1
        policy["maxContentBytes"] -= 1
        self.assertEqual([row["code"] for row in contract.check_contract(self.report, policy)["violations"]],
                         ["gzip-budget", "content-budget"])

    def test_invalid_policy_has_no_silent_fallback(self):
        for value in ({"allow": [""]}, {"allow": ["path//"]}, {"other": True}, {"maxContentBytes": True},
                      {"require": ["dir/"]}, {"fileMaxBytes": {"dist/": 1}}, {"version": False}):
            with self.subTest(value=value), self.assertRaises(contract.InputError):
                contract.check_contract(self.report, {"version": 1, "allow": [], **value})

    def test_cli_normal_path_returns_pass_failure_and_invalid_input(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "build with spaces.tgz"
            policy = Path(directory) / "package-contract.json"
            archive.write_bytes(self.blob)

            def run(*args):
                return subprocess.run([sys.executable, str(ROOT / "pack_contract.py"), *map(str, args)],
                                      text=True, capture_output=True, timeout=10)

            draft = run("policy", archive)
            self.assertEqual(draft.returncode, 0, draft.stderr)
            policy.write_text(draft.stdout, encoding="utf-8")
            self.assertEqual(run("check", archive, "--policy", policy).returncode, 0)
            policy.write_text('{"version":1,"allow":["package.json"]}', encoding="utf-8")
            failure = run("check", archive, "--policy", policy, "--json")
            self.assertEqual(failure.returncode, 1, failure.stderr)
            self.assertEqual(json.loads(failure.stdout)["status"], "fail")
            policy.write_text('{"version":1,"allow":[],"allow":[]}', encoding="utf-8")
            broken = run("check", archive, "--policy", policy)
            self.assertEqual(broken.returncode, 2, broken.stderr)
            self.assertEqual(run("inspect", directory).returncode, 2)
            self.assertEqual(run("inspect", archive, "--json").returncode, 0)

    def test_demo_is_visibly_synthetic_and_reproducible_in_process(self):
        result = contract.demo()
        self.assertEqual(result, contract.demo())
        self.assertTrue(result["synthetic"])
        self.assertEqual([row["code"] for row in result["check"]["violations"]],
                         ["missing-file", "unexpected-file"])


if __name__ == "__main__":
    unittest.main()
