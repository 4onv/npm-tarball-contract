"""Generate two synthetic archives with real npm pack and test the CLI."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile


def run(args, cwd, code=0):
    process = subprocess.run(args, cwd=cwd, text=True, encoding="utf-8", capture_output=True, timeout=45)
    if process.returncode != code:
        raise AssertionError(f"Expected exit {code}, got {process.returncode}\n{process.stdout}\n{process.stderr}")
    return process.stdout


def main():
    cli = [sys.executable, str(Path(__file__).resolve().parents[1] / "pack_contract.py")]
    npm = ["node", sys.argv[1]] if len(sys.argv) == 2 else ["npm"]
    with tempfile.TemporaryDirectory(prefix="tarball-contract-npm-") as directory:
        project = Path(directory)
        dist = project / "dist"
        dist.mkdir()
        (dist / "[slug].js").write_text("export default 'synthetic';\n", encoding="utf-8")
        types = dist / "[slug].d.ts"
        types.write_text("declare const slug: string;\nexport default slug;\n", encoding="utf-8")
        long_name = "synthetic-long-archive-member-" + "x" * 100 + ".js"
        (dist / long_name).write_text("export default 1;\n", encoding="utf-8")
        manifest = {"name": "synthetic-tarball-contract-test", "version": "1.0.0", "files": ["dist"]}
        package = project / "package.json"
        package.write_text(json.dumps(manifest), encoding="utf-8")
        print(run([*npm, "--version"], cwd=directory).strip())
        pack = [*npm, "pack", "--offline", "--ignore-scripts"]
        run(pack, cwd=directory)
        before = "synthetic-tarball-contract-test-1.0.0.tgz"
        after = "synthetic-tarball-contract-test-1.1.0.tgz"
        types.unlink()
        (dist / "fixture.json").write_text('{"synthetic":true}', encoding="utf-8")
        package.write_text(json.dumps({**manifest, "version": "1.1.0"}), encoding="utf-8")
        run(pack, cwd=directory)
        policy = project / "contract.json"
        policy.write_text(run([*cli, "policy", before], cwd=directory), encoding="utf-8")
        report = json.loads(run([*cli, "inspect", before, "--json"], cwd=directory))
        assert "dist/[slug].d.ts" in report["files"] and "dist/" + long_name in report["files"]
        assert json.loads(run([*cli, "check", before, "--policy", policy, "--json"], cwd=directory))["status"] == "pass"
        failure = json.loads(run([*cli, "check", after, "--policy", policy, "--json"], cwd=directory, code=1))
        assert [(row["code"], row["path"]) for row in failure["violations"]] == [
            ("missing-file", "dist/[slug].d.ts"), ("unexpected-file", "dist/fixture.json"),
        ]
        diff = json.loads(run([*cli, "diff", before, after, "--json"], cwd=directory))
        assert [row["path"] for row in diff["removed"]] == ["dist/[slug].d.ts"]
        print("Real npm pack artifacts passed draft, inspect, diff, and the expected CI gate failure.")


if __name__ == "__main__":
    main()
