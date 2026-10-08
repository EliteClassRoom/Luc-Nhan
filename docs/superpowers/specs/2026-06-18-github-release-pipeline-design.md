# GitHub Release Pipeline — Design Spec

**Date**: 2026-06-18 (revised 2026-06-18 for HCLI packaging compliance)
**Status**: Approved (pending user review of written spec)
**Scope**: Replace `.github/workflows/release.yml` (currently 36 lines, no build artifact) with a full release pipeline: tag/version validation, inline CI re-run, **HCLI-compliant plugin archive** build (flat ZIP per Hex-Rays spec), SHA256SUMS, `hcli plugin lint` packaging validation, GitHub Release publish with auto-notes and pre-release detection.

> **HCLI compliance (revised):** The release artifact must be installable by Hex-Rays' official plugin manager `hcli`. Per [Plugin Packaging and Format](https://hcli.docs.hex-rays.com/reference/plugin-packaging-and-format/), the `ida-plugin.json` metadata file **must sit at the root of the ZIP** (no wrapping subfolder), and HCLI only accepts ZIP archives (no tar.gz). This spec was revised after the original approval to align with that contract.

---

## Problem

The current `release.yml` workflow only does two things:
1. Validates that the pushed tag matches `ida-plugin.json` version.
2. Creates a GitHub Release with **auto-generated notes only — no build artifact attached**.

Concrete gaps in the current pipeline:

1. **No artifact**: Users get a Release page with notes but no downloadable file. To install, they must run `curl | bash` against `install.sh`, which clones the full repo (large, includes tests, docs, dev tooling). For users on air-gapped or bandwidth-limited machines, this is a blocker.
2. **Tag pattern is `v*` only**: Tag `1.0` (Mar 5, 2026) does not start with `v` and therefore did **not** trigger a release when it was pushed. Tag `v1.1` and `v1.2` did trigger. This is an accidental inconsistency in tag history.
3. **CI drift in `ci.yml`**: That workflow triggers on `branches: [main, dev]` — inherited from upstream. Pushes to `master` of this fork do not run CI. Even if a tag push did run CI, tag pushes don't match a `branches` filter, so no test verification happens before the release is published.
4. **No SHA256 sums**: No way for a user to verify download integrity. Required for trust in a security-adjacent tool (IDA plugin for reverse engineering).
5. **No re-run path**: If the release job fails (network blip, API rate limit), the only fix is to bump the version and push a new tag. There is no way to re-run the release for the existing tag.
6. **No pre-release distinction**: A `v2.0.0-rc1` tag would publish as a fully-marked stable release. Users cannot tell at a glance which releases are production-ready.

---

## Goals (in priority order)

1. Ship a downloadable, integrity-verifiable artifact per release (HCLI-compliant flat zip + SHA256SUMS).
2. Catch regressions before the release is published — without depending on the drifted `ci.yml`.
3. Support both `v*` (current convention) and bare `1.0`-style tags (legacy).
4. Allow safe re-runs of the release for an existing tag.
5. Auto-mark pre-release tags as pre-release on GitHub.

## Non-Goals (out of scope)

- Auto-attach `RELEASE_NOTES.md` (use GitHub's auto-generated notes — same as today).
- Auto-mark older releases as pre-release when a stable release comes out.
- Auto-update `webpage/` or `README.md` with new release links.
- Fix the `ci.yml` branch drift (`[main, dev] → [master, main, dev]`) — separate concern, separate PR.
- Build platform-specific installers (`.exe`, `.dmg`, `.AppImage`). The plugin is pure Python; the same archive works on every OS. Installer scripts (`install.sh`, `install.ps1`, `install_ida.*`) are included in the archive.
- Add `CHANGELOG.md` auto-generation.

---

## Design

### Trigger model

```yaml
on:
  push:
    tags:
      - 'v*'              # v1.2, v1.2.3, v2.0.0-rc1
      - '[0-9]*.[0-9]*'   # 1.0, 1.2.3 — legacy bare-version tags
  workflow_dispatch:
    inputs:
      tag:
        description: 'Tag name to re-release (vd: v1.2 hoặc 1.2.3)'
        required: true
        type: string
```

GitHub tag filters use git refspec glob (not regex). Two entries cover both conventions without a `git`-side workaround.

### Workflow structure — three-job chain

```
verify  →  build  →  publish
  │            │          │
  │            │          └─ softprops/action-gh-release@v2
  │            └─ scripts/build_release.py + upload-artifact
  └─ tag/version validation + inline re-run CI checks
```

**Fail-fast**: each job depends on the previous via `needs:`. Any failure skips downstream jobs. There is no partial release.

### Job 1 — `verify` (gate)

**Inputs**: `github.ref_name` (tag push) or `inputs.tag` (workflow_dispatch).
**Outputs**: `tag`, `version`, `is_prerelease`, `archive_basename` (consumed by `build` and `publish`).

**Steps**:
1. `actions/checkout@v4` with `fetch-depth: 0` (desloppify needs git history).
2. `actions/setup-python@v5` with `python-version: "3.11"` (matches CI; consistent objective score).
3. `pip install ruff mypy pytest tomli desloppify`.
4. **Parse tag, version, pre-release flag**:
   - Workflow_dispatch → use `inputs.tag`. Tag push → use `GITHUB_REF_NAME`.
   - Strip leading `v` to get `VERSION`.
   - Compare `VERSION` to `ida-plugin.json.plugin.version`. Fail with `::error::` if mismatch.
   - Detect pre-release: `[[ "$TAG" =~ -(rc|alpha|beta|pre|dev)[0-9]*$ ]]` → `is_prerelease=true`.
   - Compute `archive_basename=lucnhan-v${VERSION}`.
5. **Re-run CI checks inline** (in this single step, `set -e`):
   - `python -m ruff format --check lucnhan/`
   - `python -m ruff check lucnhan/`
   - `python -m mypy lucnhan/core lucnhan/providers`
   - `python -m pytest tests/ --tb=short -q`
   - `desloppify scan --profile objective --no-badge` + score gate (`>= 89.0 - 0.5`)
6. **HCLI packaging lint** (revised): `hcli plugin lint` against the built archive is run in the `build` job after the ZIP is produced (see Job 2), because lint needs the artifact to exist. Code-level checks stay in `verify`; packaging-correctness checks belong next to where the artifact is built.

**Why inline re-run?** The `ci.yml` workflow triggers on `branches: [main, dev]` (upstream drift — does not match `master` of this fork). Tag pushes do not match a `branches` filter. Inline re-run is independent of `ci.yml` and runs against the same SHA the release will publish, so the verify-then-publish chain is closed-loop.

### Job 2 — `build` (artifact)

**Inputs**: `needs.verify.outputs.version`.

**Steps**:
1. `actions/checkout@v4`.
2. `actions/setup-python@v5` with `python-version: "3.11"`.
3. Run `python scripts/build_release.py --version "$VERSION" --out-dir dist`.
4. **HCLI packaging validation** (revised): install the Hex-Rays CLI and lint the archive:
   - `pip install hex-rays-cli` (or the documented install method for `hcli`).
   - `hcli plugin lint dist/lucnhan-v{version}.zip` — fails the job if HCLI reports problems. This is the authoritative check that the archive is installable by HCLI.
   - **Fallback if `hcli` is unavailable on the runner**: run a structural validation shim in Python — assert `ida-plugin.json` is at the ZIP root, `entryPoint` exists at root, required fields present, no wrapping subfolder. The shim is a secondary check; HCLI lint is preferred when available.
5. `actions/upload-artifact@v4` with `name: release-artifacts`, `path: dist/`, `if-no-files-found: error`.

**Build script** — `scripts/build_release.py`:

| Concept | Detail |
|---------|--------|
| **Include list** | `lucnhan_plugin.py`, `lucnhan/`, `install.sh`, `install_ida.sh`, `install.ps1`, `install_ida.bat`, `requirements.txt`, `ida-plugin.json`, `LICENSE`, `README.md` |
| **Exclude (any part of path)** | `__pycache__`, `.git`, `.venv`, `.mypy_cache`, `.ruff_cache`, `.pytest_cache`, `.desloppify`, `.codegraph`, `.reasonix`, `.claude`, `node_modules` |
| **Exclude (suffix)** | `.pyc`, `.pyo`, `.pyd` |
| **Output name** | `lucnhan-v{version}.zip` (single ZIP — HCLI accepts only ZIP) |
| **Archive layout** | **Flat** — files at ZIP root, no wrapping subfolder: `ida-plugin.json`, `lucnhan_plugin.py`, `lucnhan/...`, `install.sh`, ... This matches the HCLI contract that `ida-plugin.json` sits "in the root directory of the plugin within the archive". |
| **SHA256SUMS** | `<hex>  lucnhan-v{version}.zip\n` (two-space separator, GNU coreutils convention) |

The script is invokable locally (same CLI as in CI) so the same build can be reproduced off-CI for debugging.

### Job 3 — `publish` (GitHub Release)

**Inputs**: `needs.verify.outputs.tag`, `needs.verify.outputs.version`, `needs.verify.outputs.is_prerelease`. Build artifacts from `actions/download-artifact@v4`.

**Single step**: `softprops/action-gh-release@v2` with:
- `tag_name`: `${{ needs.verify.outputs.tag }}` (preserves the original `v` prefix on the tag)
- `name`: `"Luc Nhan ${{ needs.verify.outputs.version }}"`
- `prerelease`: `${{ needs.verify.outputs.is_prerelease == 'true' }}`
- `generate_release_notes: true` (same behavior as today)
- `fail_on_unmatched_files: true` (so a broken glob fails the job instead of producing an empty release)
- `files`:
  - `dist/lucnhan-v{version}.zip`
  - `dist/SHA256SUMS`

**Idempotency**: `softprops/action-gh-release@v2` overwrites an existing release with the same tag. The release artifacts, notes, and pre-release flag are all re-applied. This is what makes `workflow_dispatch` re-runs safe.

**Permissions**: `contents: write` at the workflow level.

### Pre-release detection rules

A tag is marked pre-release if it matches `-(rc|alpha|beta|pre|dev)[0-9]*$`:

| Tag | `is_prerelease` | GitHub Release flag |
|-----|-----------------|---------------------|
| `v1.2.0` | `false` | stable |
| `v1.2.3` | `false` | stable |
| `v2.0.0-rc1` | `true` | pre-release |
| `v2.0.0-beta2` | `true` | pre-release |
| `v1.0.0` (legacy, written `1.0` at the time) | `false` | stable |
| `v1.2.3-dev4` | `true` | pre-release |

The regex is intentionally simple. It does not catch every pre-release convention SemVer recognizes (e.g., `.` separators in `1.2.0.0`), but it covers the conventions used in this project (`-rc`, `-alpha`, `-beta`, `-pre`, `-dev`).

---

## Files Touched

| Action | File | Purpose |
|--------|------|---------|
| Rewrite | `.github/workflows/release.yml` | Replace the 36-line skeleton with the 3-job pipeline (~150 lines) |
| Add | `scripts/build_release.py` | Build script: collect files, build flat zip, write SHA256SUMS |
| Add | `scripts/test_build_release.py` | Unit tests for the build script (AAA pattern, pytest) |
| Edit | `AGENTS.md` | Update the "Release Flow" subsection to describe the new pipeline + workflow_dispatch re-run path |
| Edit | `DEVELOPMENT.md` | Same: update the "Release Process" subsection, mention `scripts/build_release.py` for local dry-runs |
| Edit | `.gitignore` | Add `dist/` to keep local build output out of git |

**Not touched**: `ci.yml` (drift fix is a separate concern, separate PR), `install.sh` / `install.ps1` (the `curl | bash` path still works — release artifacts are an additional install method, not a replacement), `ida-plugin.json` schema, Python source under `lucnhan/`.

---

## Verification

### Unit tests for `build_release.py`

In `scripts/test_build_release.py`, using pytest + AAA pattern. Each test seeds a temp directory mimicking the repo layout, runs the script (or its helpers), and asserts on the result.

| Test | What it asserts |
|------|-----------------|
| `test_collect_includes_runtime_files` | All `INCLUDE_PATHS` files are in `collect()` output |
| `test_collect_excludes_tests_and_docs` | `tests/`, `docs/`, `AGENTS.md`, `ARCHITECTURE.md`, `DEVELOPMENT.md`, `llms.txt` are not in output |
| `test_collect_excludes_pycache_and_dotfiles` | `__pycache__/`, `.git/`, `.mypy_cache/`, `*.pyc` are all skipped |
| `test_collect_excludes_dev_assets` | `assets/`, `chat_examples/`, `webpage/`, `pyproject.toml`, `uv.lock`, `ci-local.sh`, `__pycache__` skipped |
| `test_build_zip_creates_valid_archive` | Output opens as a valid `ZipFile`; `namelist()` count matches `collect()` count |
| `test_zip_ida_plugin_json_at_root` | `ida-plugin.json` is at the ZIP root (HCLI contract) |
| `test_zip_entry_point_at_root` | `entryPoint` from `ida-plugin.json` resolves at the ZIP root |
| `test_zip_no_wrapping_subfolder` | No single subfolder wraps every entry (HCLI contract) |
| `test_sha256_matches_stdlib` | `sha256_file(p) == hashlib.sha256(p.read_bytes()).hexdigest()` |
| `test_archive_basename_format` | `--version 1.2.3` produces `lucnhan-v1.2.3.zip` (no tar.gz) |
| `test_empty_source_root_fails` | `collect()` returning `[]` → script exits with code 1 and prints error to stderr |
| `test_argparse_requires_version` | Omitting `--version` → `SystemExit(2)` from argparse |
| `test_sha256sums_format` | File content matches `^[a-f0-9]{64}  lucnhan-v.*\.zip$` (regex) |

### Local dry-run (before push)

```bash
# Build locally
python scripts/build_release.py --version 1.2.3 --out-dir /tmp/lucnhan-test

# Inspect contents — ida-plugin.json MUST be at the ZIP root, no subfolder
unzip -l /tmp/lucnhan-test/lucnhan-v1.2.3.zip

# Verify SHA256SUMS
( cd /tmp/lucnhan-test && sha256sum -c SHA256SUMS )

# If hcli is installed locally, validate the archive
hcli plugin lint /tmp/lucnhan-test/lucnhan-v1.2.3.zip

# Run unit tests
python -m pytest tests/scripts/test_build_release.py -v
```

### Pre-merge checklist

- [ ] `scripts/build_release.py` runs locally, produces flat zip + SHA256SUMS in the right format.
- [ ] `unzip -l` shows `ida-plugin.json` and `lucnhan_plugin.py` at the ZIP root (no wrapping subfolder).
- [ ] `python -m pytest tests/scripts/test_build_release.py -v` — all pass.
- [ ] `./ci-local.sh` still passes (script lives in `scripts/`, not `lucnhan/`, so ruff/mypy don't lint it; pytest picks up `tests/scripts/`).
- [ ] `actions/download-artifact@v4`, `actions/upload-artifact@v4`, `softprops/action-gh-release@v2` are pinned to a major version.

### Post-merge smoke test

1. Push a throwaway tag: `git tag v0.0.0-test && git push origin v0.0.0-test`.
2. Watch the workflow run on GitHub Actions:
   - `verify` job: ruff/mypy/pytest/desloppify all green; tag/version match; `is_prerelease=false`.
   - `build` job: `dist/` contains `lucnhan-v0.0.0-test.zip` + `SHA256SUMS`; `hcli plugin lint` passes (or the structural shim passes as fallback).
   - `publish` job: a **draft** release appears at `https://github.com/EliteClassRoom/Luc-Nhan/releases/tag/v0.0.0-test`.
3. Verify the draft release has both artifacts attached, and `SHA256SUMS` content is correct.
4. Delete the throwaway tag and the draft release:
   ```bash
   git push origin :refs/tags/v0.0.0-test
   gh release delete v0.0.0-test --repo EliteClassRoom/Luc-Nhan --yes
   ```

### Long-term sanity check

After the next real release (`v1.3.0` or whatever the next version is):
- The GitHub Release page shows both artifacts (`lucnhan-v{version}.zip`, `SHA256SUMS`).
- The auto-generated release notes section appears (same as before).
- Pre-release flag is correct for the tag pattern used.
- A user can `curl -L https://github.com/EliteClassRoom/Luc-Nhan/releases/download/v1.3.0/lucnhan-v1.3.0.zip -o lucnhan.zip` and install via `hcli plugin install lucnhan.zip` (flat layout — HCLI finds `ida-plugin.json` at the ZIP root).

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| `actions/setup-python@v5` cache invalidation could slow CI | Pin Python 3.11 explicitly; no `cache: pip` here (verify job does many distinct `pip install`s and cache benefit is minor) |
| `desloppify` score is sensitive to Python version (per `AGENTS.md`); GH Actions uses 3.11 (~89.4) | Pin to Python 3.11 in `verify` job; document the 0.5 tolerance explicitly in the script |
| `softprops/action-gh-release@v2` does not validate artifact paths | Use `fail_on_unmatched_files: true`; check the build job's `if-no-files-found: error` on upload |
| Tag pattern `[0-9]*.[0-9]*` could match unintended tags (e.g., a branch named `1.2`) | Branch refs and tag refs are separate in GitHub; the `tags:` filter only matches tags |
| A re-run via `workflow_dispatch` re-uploads artifacts but does not re-validate CI | The verify job always re-runs the full CI suite for any trigger, so re-runs are safe; a malicious commit pushed between tag and re-run would be caught by the inline re-run |
| HCLI (`hex-rays-cli`) may not be pip-installable or may change install method | Build job runs `hcli plugin lint`; if HCLI is unavailable on the runner, a structural-validation shim (Python) asserts the ZIP is flat with `ida-plugin.json` at root and the `entryPoint` file present — this catches the most common packaging mistake without depending on HCLI |
| Flat ZIP layout differs from the legacy `curl | bash` install path | The two paths are independent. Legacy `install.sh` clones the repo (unchanged); the release ZIP is the HCLI-installable path. Document both in `DEVELOPMENT.md`. |

---

## References

- [`.github/workflows/release.yml`](../../.github/workflows/release.yml) — current minimal release workflow (to be rewritten)
- [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) — drift context (separate concern)
- [`ida-plugin.json`](../../ida-plugin.json) — version source of truth + HCLI plugin metadata
- **[Hex-Rays: Plugin Packaging and Format](https://hcli.docs.hex-rays.com/reference/plugin-packaging-and-format/)** — authoritative spec for the flat-ZIP layout, required `ida-plugin.json` fields, and `hcli plugin lint`
- [`ci-local.sh`](../../ci-local.sh) — local CI mirror (the inline re-run in `verify` follows the same step order)
- [`AGENTS.md`](../../AGENTS.md) §"CI/CD & Branch Model" / §"Release Flow" — sections to be updated
- [`DEVELOPMENT.md`](../../DEVELOPMENT.md) §"Release Process" — section to be updated
- Existing spec: [`2026-06-16-document-standardization-design.md`](2026-06-16-document-standardization-design.md) — precedent for spec format and approval flow
