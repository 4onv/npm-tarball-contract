# npm Tarball Contract

**Catch a missing declaration file or an unexpected fixture before shipping your npm archive.**

Generate a file policy from a known-good `.tgz`, review it, and make CI check the next build. The checker reads the finished archive directly. It needs Python 3.11 or newer and has zero third-party dependencies.

## Try the demo

Download `pack_contract.py` from the release, or use it from this checkout. [Download the standalone script](https://github.com/4onv/npm-tarball-contract/releases/latest)

```sh
python pack_contract.py demo
```

The synthetic candidate is smaller than the baseline, but it lost `dist/index.d.ts` and gained `test/data.json`. The demo prints both findings and exits 0 so you can try it without preparing an archive. `demo --json` labels the result as synthetic.

## Check a release

```sh
npm pack --offline --ignore-scripts
python pack_contract.py policy my-package-1.0.0.tgz > package-contract.json
# After building and packing your next version:
python pack_contract.py check my-package-1.1.0.tgz --policy package-contract.json --json
```

Draft the policy once from an archive you have reviewed, then check it into Git. Review the draft before treating it as a contract. By default it lists every observed file as both allowed and required. Replace a collection of generated names with a directory rule where the exact contents may vary, and keep important entry files in `require`.

Here's a synthetic policy you can adapt.

```json
{
  "version": 1,
  "package": "@demo/hello",
  "allow": ["package.json", "README.md", "LICENSE", "dist/"],
  "require": ["package.json", "dist/index.js", "dist/index.d.ts"],
  "maxGzipBytes": 30000,
  "maxContentBytes": 75000,
  "fileMaxBytes": {"dist/index.js": 25000}
}
```

| Field | Meaning |
| --- | --- |
| `version` | Required. Use `1`. |
| `package` | Optional exact name from the archived `package.json`. |
| `allow` | Required. Each item is a literal path or a directory prefix ending in `/`. Matching is case-sensitive. |
| `require` | Optional list of exact file paths that must be present and allowed. |
| `maxGzipBytes` | Optional inclusive limit on the entire compressed archive. |
| `maxContentBytes` | Optional inclusive limit on the total regular file bytes before compression. |
| `fileMaxBytes` | Optional exact file size limits. Every listed file is also required. |

Paths are relative to the package root. For example, specify `dist/index.d.ts` instead of `package/dist/index.d.ts`. Characters such as brackets are literal. The checker exits **0** for a passing contract, **1** for a valid archive that violates it, and **2** for malformed, unreadable, or unsupported input.

### GitHub Actions

After your checkout, Python, Node.js, and package build steps, add a step like this. Keep the checked script and policy with the project. Pass the resulting archive to your release job.

```yaml
- name: Check the npm release archive
  shell: bash
  run: |
    npm pack --offline --ignore-scripts --pack-destination "$RUNNER_TEMP"
    python tools/pack_contract.py check "$RUNNER_TEMP/my-package-1.1.0.tgz" --policy package-contract.json
```

## Compare two builds

```sh
python pack_contract.py inspect my-package-1.1.0.tgz --json
python pack_contract.py diff my-package-1.0.0.tgz my-package-1.1.0.tgz --json
```

`inspect` reports member sizes, SHA-256 digests, and executable mode. `diff` reports added and removed files, content and mode changes, and both the gzip and content byte deltas. A same-size edit still shows up through its digest. An archive can differ byte-for-byte even when its observed members match, for example because its gzip metadata changed. A valid diff exits 0 even when changes exist. Use `check` for a CI gate.

## Scope

The checker reads local npm-style gzip tarballs with a `package/` root and a valid `package.json`. Limits are 32 MiB compressed, 96 MiB expanded, 5,000 visible members, and 512 UTF-8 bytes per file path. Links, sparse files, and case-folded path collisions produce a controlled error. The script makes no network requests, extracts no files to disk, and runs no package code.

Reports include filenames, package name and version, sizes, and digests. Review filenames before sharing reports. The file contract does not validate module resolution, types, runtime behavior, or semantic versioning. Use installed-package smoke tests and package analysis tools alongside it. [npm pack docs](https://docs.npmjs.com/cli/pack/) · [publint](https://github.com/publint/publint)

```sh
python -m unittest discover -s tests -v
python tests/check_npm.py
```

The integration test makes two visibly synthetic packages with npm itself. The project's selection and source evidence are recorded in the [November 18 research](docs/research-2026-11-18.md).
