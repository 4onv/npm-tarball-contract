#!/usr/bin/env python3
"""Inspect npm release archives and check a package's file contract."""

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
import unicodedata
import zlib


VERSION = "0.1.0"
MIB = 1024 * 1024
MAX_GZIP = 32 * MIB
MAX_TAR = 96 * MIB
MAX_POLICY = 16 * MIB
MAX_MEMBERS = 5_000
PACKAGE_NAME = re.compile(r"(?:@[a-z0-9._-]+/)?[a-z0-9._-]+")
PACKAGE_VERSION = re.compile(r"[a-zA-Z0-9.+-]+")
POLICY_KEYS = {"version", "package", "allow", "require", "maxGzipBytes", "maxContentBytes", "fileMaxBytes"}


class InputError(ValueError):
    """An input could not be interpreted as a supported archive or policy."""


def read_regular(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise InputError(f"Input must be a regular file of at most {limit // MIB} MiB.")
        data = handle.read(info.st_size + 1)
        if len(data) != info.st_size:
            raise InputError("Input changed size during reading. Retry once it is stable.")
        return data


def strict_json(data: bytes, label: str):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate object key")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("nonfinite number")

    try:
        return json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique_pairs, parse_constant=reject_constant)
    except (ValueError, RecursionError) as exc:
        raise InputError(f"{label} must be UTF-8 JSON with unique object keys.") from exc


def valid_path(path):
    if not isinstance(path, str) or not path or "\\" in path or ":" in path:
        return False
    if any(part in {"", ".", ".."} for part in path.split("/")):
        return False
    if any(unicodedata.category(char).startswith("C") for char in path):
        return False
    return len(path.encode("utf-8")) <= 512


def package_name(value):
    return isinstance(value, str) and len(value) <= 214 and PACKAGE_NAME.fullmatch(value) is not None


def resolve_identity(data):
    info = strict_json(data, "Archive package.json")
    if not isinstance(info, dict) or not package_name(info.get("name")):
        raise InputError("Archive package.json needs a supported lowercase npm name.")
    version = info.get("version")
    if not isinstance(version, str) or len(version) > 100 or not PACKAGE_VERSION.fullmatch(version):
        raise InputError("Archive package.json needs a version string.")
    return {"name": info["name"], "version": version}


def parents(path):
    while "/" in path:
        path = path.rsplit("/", 1)[0]
        yield path


def inspect_archive(compressed):
    """Return file evidence from a local npm archive without extracting it."""
    if len(compressed) > MAX_GZIP:
        raise InputError("Compressed archive exceeds 32 MiB.")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as input_file:
            raw = input_file.read(MAX_TAR + 1)
    except (OSError, EOFError, zlib.error) as exc:
        raise InputError("Expected a complete gzip archive.") from exc
    if len(raw) > MAX_TAR:
        raise InputError("Expanded archive exceeds 96 MiB.")
    files = {}
    seen = set()
    all_paths = {}
    identity = None
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            for index, member in enumerate(archive, 1):
                if index > MAX_MEMBERS:
                    raise InputError("Archive has more than 5,000 members.")
                if member.isdir() and member.name.rstrip("/") == "package" and member.size == 0:
                    continue
                name = member.name.rstrip("/") if member.isdir() else member.name
                if not name.startswith("package/") or not valid_path(name[8:]):
                    raise InputError("Archive contains an unsupported path outside the package layout.")
                path = name[8:]
                if path in seen:
                    raise InputError("Archive contains a duplicate member path.")
                seen.add(path)
                for prefix in [path, *parents(path)]:
                    key = prefix.casefold()
                    if all_paths.setdefault(key, prefix) != prefix:
                        raise InputError("Archive contains case-insensitive path collisions.")
                if member.isdir() and member.size == 0:
                    continue
                if member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE} or member.sparse is not None:
                    raise InputError("Archive contains a link or unsupported member type.")
                with archive.extractfile(member) as content:
                    if path == "package.json":
                        if member.size > MIB:
                            raise InputError("Archive package.json exceeds 1 MiB.")
                        manifest = content.read()
                        identity = resolve_identity(manifest)
                        digest = hashlib.sha256(manifest).hexdigest()
                    else:
                        digest = hashlib.file_digest(content, "sha256").hexdigest()
                files[path] = {
                    "bytes": member.size, "sha256": digest, "executable": bool(member.mode & 0o111),
                }
            end = archive.offset
            if len(raw) % 512 or len(raw) - end < 1024 or any(raw[end:]):
                raise InputError("Archive is truncated or has data after its end marker.")
    except (tarfile.TarError, EOFError, OverflowError, RecursionError) as exc:
        raise InputError("Expected a complete npm tar archive.") from exc
    if any(parent in files for path in seen for parent in parents(path)):
        raise InputError("Archive uses a file as a parent directory.")
    if identity is None:
        raise InputError("Archive is missing package/package.json.")
    return {
        "schemaVersion": 1, "package": identity, "gzipBytes": len(compressed),
        "contentBytes": sum(row["bytes"] for row in files.values()),
        "sha256": hashlib.sha256(compressed).hexdigest(), "files": dict(sorted(files.items())),
    }


def load_archive(path):
    return inspect_archive(read_regular(path, MAX_GZIP))


def diff_archives(before, after):
    if before["package"]["name"] != after["package"]["name"]:
        raise InputError("Compare two archives for the same package name.")
    old, new = before["files"], after["files"]
    added = [{"path": path, **new[path]} for path in sorted(new.keys() - old.keys())]
    removed = [{"path": path, **old[path]} for path in sorted(old.keys() - new.keys())]
    changed = [
        {"path": path, "before": old[path], "after": new[path]}
        for path in sorted(old.keys() & new.keys()) if old[path] != new[path]
    ]
    return {
        "schemaVersion": 1, "before": before["package"], "after": after["package"],
        "gzipBytesChange": after["gzipBytes"] - before["gzipBytes"],
        "contentBytesChange": after["contentBytes"] - before["contentBytes"],
        "archiveBytesMatch": before["sha256"] == after["sha256"],
        "added": added, "removed": removed, "changed": changed,
    }


def draft_policy(report):
    paths = list(report["files"])
    return {"version": 1, "package": report["package"]["name"], "allow": paths, "require": paths}


def path_list(policy, field, allow_prefix=False):
    value = policy.get(field, [])
    if not isinstance(value, list) or len(value) > MAX_MEMBERS:
        raise InputError(f"Policy {field} must be a list of up to 5,000 paths.")
    for rule in value:
        path = rule[:-1] if isinstance(rule, str) and allow_prefix and rule.endswith("/") else rule
        if not valid_path(path):
            raise InputError(f"Policy {field} has an invalid package-relative path.")
    if len(set(value)) != len(value):
        raise InputError(f"Policy {field} contains duplicate paths.")
    return value


def budget_value(value, upper, field):
    if type(value) is not int or not 0 <= value <= upper:
        raise InputError(f"Policy {field} must be an integer from 0 to {upper}.")


def validate_policy(policy):
    if not isinstance(policy, dict) or policy.keys() - POLICY_KEYS:
        raise InputError("Expected a policy object with supported contract fields.")
    if type(policy.get("version")) is not int or policy["version"] != 1 or "allow" not in policy:
        raise InputError("Policy needs version 1 and an allow list.")
    if "package" in policy and not package_name(policy["package"]):
        raise InputError("Policy package must be a supported npm name.")
    path_list(policy, "allow", allow_prefix=True)
    path_list(policy, "require")
    for field, upper in (("maxGzipBytes", MAX_GZIP), ("maxContentBytes", MAX_TAR)):
        if field in policy:
            budget_value(policy[field], upper, field)
    per_file = policy.get("fileMaxBytes", {})
    if not isinstance(per_file, dict) or len(per_file) > MAX_MEMBERS or any(not valid_path(path) for path in per_file):
        raise InputError("Policy fileMaxBytes must map literal paths to byte limits.")
    for limit in per_file.values():
        budget_value(limit, MAX_TAR, "fileMaxBytes")
    return policy


def check_contract(report, policy):
    policy = validate_policy(policy)
    files = report["files"]
    exact = {rule for rule in policy["allow"] if not rule.endswith("/")}
    prefixes = {rule for rule in policy["allow"] if rule.endswith("/")}
    violations = []
    if "package" in policy and policy["package"] != report["package"]["name"]:
        violations.append({"code": "package-name", "expected": policy["package"], "actual": report["package"]["name"]})
    required = set(policy.get("require", [])) | policy.get("fileMaxBytes", {}).keys()
    violations.extend({"code": "missing-file", "path": path} for path in sorted(required - files.keys()))
    for path in files:
        if path not in exact and not any(parent + "/" in prefixes for parent in parents(path)):
            violations.append({"code": "unexpected-file", "path": path})
    for field, metric, code in (
        ("maxGzipBytes", "gzipBytes", "gzip-budget"),
        ("maxContentBytes", "contentBytes", "content-budget"),
    ):
        if field in policy and report[metric] > policy[field]:
            violations.append({"code": code, "bytes": report[metric], "maxBytes": policy[field]})
    for path, limit in sorted(policy.get("fileMaxBytes", {}).items()):
        if path in files and files[path]["bytes"] > limit:
            violations.append({"code": "file-budget", "path": path, "bytes": files[path]["bytes"], "maxBytes": limit})
    return {"schemaVersion": 1, "status": "fail" if violations else "pass",
            "package": report["package"], "sha256": report["sha256"], "violations": violations}


def synthetic_archive(version, files):
    """Build the explicitly synthetic, deterministic demo archive in memory."""
    contents = {"package.json": json.dumps({"name": "@demo/hello", "version": version}).encode(), **files}
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for path, data in sorted(contents.items()):
            member = tarfile.TarInfo("package/" + path)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


def demo():
    before = inspect_archive(synthetic_archive("1.0.0", {
        "dist/index.js": b"export const hello = 'hello';\n", "dist/index.d.ts": b"export declare const hello: string;\n",
    }))
    after = inspect_archive(synthetic_archive("1.1.0", {
        "dist/index.js": b"export const hello = 'hello';\n", "test/data.json": b"{}\n",
    }))
    return {"synthetic": True, "diff": diff_archives(before, after), "check": check_contract(after, draft_policy(before))}


def label(identity):
    return identity["name"] + "@" + identity["version"]


def print_diff(report):
    print(f"{label(report['before'])} -> {label(report['after'])}")
    print(f"Compressed size change {report['gzipBytesChange']:+d} B")
    print(f"File content change {report['contentBytesChange']:+d} B")
    for category in ("added", "removed", "changed"):
        for row in report[category]:
            print(f"{category.upper():7} {json.dumps(row['path'])}")


def print_check(report):
    print(f"{report['status'].upper()} {label(report['package'])}")
    for row in report["violations"]:
        print(f"  {row['code']} {json.dumps({key: value for key, value in row.items() if key != 'code'})}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("inspect", "list member paths, sizes and hashes"),
        ("diff", "compare two archives of the same package"),
        ("policy", "draft an exact file-list policy as JSON"),
        ("check", "check an archive against a policy"),
        ("demo", "run an explicitly synthetic example"),
    ):
        options = commands.add_parser(name, help=help_text)
        if name == "diff":
            options.add_argument("before", type=Path)
            options.add_argument("after", type=Path)
        elif name != "demo":
            options.add_argument("archive", type=Path)
        if name == "check":
            options.add_argument("--policy", type=Path, required=True)
        if name != "policy":
            options.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            result = demo()
        elif args.command == "diff":
            result = diff_archives(load_archive(args.before), load_archive(args.after))
        else:
            archive = load_archive(args.archive)
            if args.command == "check":
                result = check_contract(archive, strict_json(read_regular(args.policy, MAX_POLICY), "Policy"))
            else:
                result = draft_policy(archive) if args.command == "policy" else archive
        if args.command == "policy" or args.json:
            print(json.dumps(result, indent=2, ensure_ascii=True))
        elif args.command == "demo":
            print("Synthetic demo - generated archives and invented package data")
            print_diff(result["diff"])
            print_check(result["check"])
        elif args.command == "diff":
            print_diff(result)
        elif args.command == "check":
            print_check(result)
        else:
            print(f"{label(result['package'])}  {len(result['files'])} files  {result['gzipBytes']} B gzip")
            print(f"Archive SHA-256 {result['sha256']}")
            for path, entry in result["files"].items():
                print(f"{entry['bytes']:>10} B  {json.dumps(path)}")
        return int(args.command == "check" and result["status"] == "fail")
    except InputError as exc:
        print(f"npm-tarball-contract: {exc}", file=sys.stderr)
    except OSError as exc:
        print(f"npm-tarball-contract: Could not read the selected file ({exc.__class__.__name__}).", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
