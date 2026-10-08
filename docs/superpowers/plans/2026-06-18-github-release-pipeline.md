# GitHub Release Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 36-line skeleton `.github/workflows/release.yml` with a full 3-job GitHub Actions pipeline (verify → build → publish) that produces a **HCLI-compliant flat-ZIP** plugin archive (`lucnhan-v{version}.zip` + `SHA256SUMS`) for every tag push (or workflow_dispatch re-run), validated with `hcli plugin lint`.

**Architecture:** Three-job workflow chain. `verify` parses the tag, validates it matches `ida-plugin.json`, and re-runs the same CI checks that `ci.yml` would (ruff, mypy, pytest, desloppify) inline — bypassing the `ci.yml` branch drift. `build` runs `scripts/build_release.py` (a pure-Python module importable in tests) to assemble a **flat ZIP** (no wrapping subfolder — `ida-plugin.json` at root, per the [Hex-Rays plugin packaging spec](https://hcli.docs.hex-rays.com/reference/plugin-packaging-and-format/)), then validates it with `hcli plugin lint`. `publish` uploads the artifacts via `softprops/action-gh-release@v2`, with pre-release flag auto-detected from the tag suffix.

**Tech Stack:** Python 3.11 (matches CI baseline, keeps `desloppify` objective score reproducible), `zipfile` (stdlib — no tar.gz, HCLI only accepts ZIP), `softprops/action-gh-release@v2`, `actions/{checkout,setup-python,upload-artifact,download-artifact}@v4`, `hcli` (Hex-Rays CLI) for packaging lint with a Python structural shim as fallback, `ruff`/`mypy`/`pytest`/`desloppify` for inline code re-run.

**Spec:** `docs/superpowers/specs/2026-06-18-github-release-pipeline-design.md`

## Global Constraints

- **HCLI flat-ZIP layout (HARD CONSTRAINT)**: `ida-plugin.json` and `lucnhan_plugin.py` (the `entryPoint`) must be at the ZIP **root**. No wrapping subfolder like `lucnhan-v{version}/`. The [Hex-Rays packaging spec](https://hcli.docs.hex-rays.com/reference/plugin-packaging-and-format/) states the metadata file "should be found in the root directory of the plugin within the archive." A wrapped layout breaks `hcli plugin install` / `hcli plugin lint`.
- **ZIP only — no tar.gz**: HCLI accepts only ZIP archives.
- Python target version: `py311` (per `pyproject.toml [tool.ruff] target-version` and the project's CI baseline).
- Line length: `120` (per `pyproject.toml [tool.ruff] line-length`).
- All Python files start with `from __future__ import annotations` (project-wide rule from `AGENTS.md` §"Python Style").
- All functions have type hints (project-wide rule from `AGENTS.md`).
- Archive filename: `lucnhan-v{version}.zip` (e.g. `lucnhan-v1.2.3.zip`).
- `SHA256SUMS` uses two-space separator between hash and filename (GNU coreutils convention; works with `sha256sum -c`).
- Workflow triggers must accept both `v*` tags (current convention) and bare `[0-9]*.[0-9]*` tags (legacy `1.0` style).
- Pre-release detection regex: `-(rc|alpha|beta|pre|dev)[0-9]*$`.
- `desloppify` objective score baseline: `89.0`, tolerance `0.5` (from `ci.yml` and `AGENTS.md`).
- GitHub tag-filter patterns are **glob**, not regex (GitHub docs). Patterns `[0-9]+` would not work; use `[0-9]*` instead.

---

## File Structure

| File | Action | Purpose |
|------|--------|---------|
| `scripts/__init__.py` | Create (empty) | Allows `from scripts.build_release import …` in tests |
| `scripts/build_release.py` | Create | The build script: collect files, build flat zip, write `SHA256SUMS` |
| `scripts/validate_archive.py` | Create | HCLI-availability shim: if `hcli` not on PATH, validate the ZIP is flat + has `ida-plugin.json` at root + `entryPoint` resolvable |
| `tests/scripts/__init__.py` | Create (empty) | Mirrors `tests/{agent,core,…}/__init__.py` pattern |
| `tests/scripts/test_build_release.py` | Create | Unit tests for the build script (pytest, AAA pattern) |
| `tests/scripts/test_validate_archive.py` | Create | Unit tests for the structural shim |
| `.github/workflows/release.yml` | Rewrite | 3-job pipeline (verify → build → publish) |
| `AGENTS.md` | Edit | Update §"Release Flow" to describe the new pipeline |
| `DEVELOPMENT.md` | Edit | Update §"Release Process" with re-run + smoke-test instructions |
| `.gitignore` | Edit | Add `dist/` so local build output does not leak into git |

**Not touched:** `ci.yml` (drift fix is a separate concern), `install.sh` / `install.ps1` (`curl | bash` install path stays as is — release ZIP is an *additional* install method), `ida-plugin.json` schema, anything under `lucnhan/`.

---

## Task 1: TDD `should_skip()` + `collect()` + flat-zip `build_zip()`

**Files:**
- Create: `scripts/__init__.py` (empty)
- Create: `tests/scripts/__init__.py` (empty)
- Create: `scripts/build_release.py` (with `should_skip()`, `collect()`, `build_zip()`)
- Create: `tests/scripts/test_build_release.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces:
  - `should_skip(path: Path) -> bool` — return `True` if `path` matches any exclude rule.
  - `collect(source_root: Path) -> list[Path]` — return sorted list of files that are in `INCLUDE_PATHS` and not excluded.
  - `build_zip(files: list[Path], out_path: Path, source_root: Path) -> None` — build **flat** ZIP: each entry is the file path relative to `source_root` (e.g. `lucnhan/core/config.py`), no wrapping prefix.

- [ ] **Step 1: Create empty `__init__.py` files for the new packages**

Create `scripts/__init__.py`:

```python
"""Build scripts for Luc Nhan release pipeline."""
```

Create `tests/scripts/__init__.py`:

```python
"""Tests for build scripts."""
```

- [ ] **Step 2: Write the failing tests**

Create `tests/scripts/test_build_release.py` with this exact content:

```python

```python
"""Unit tests for scripts/build_release.py.

AAA pattern. Uses pytest's ``tmp_path`` fixture to seed a fake repo
layout that mirrors the real project, then runs ``collect()`` /
``build_zip()`` / etc. and asserts on the output.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts.build_release import (
    EXCLUDE_NAMES,
    INCLUDE_PATHS,
    build_zip,
    collect,
    sha256_file,
    should_skip,
    write_sha256sums,
)


# ── should_skip ────────────────────────────────────────────────────────


def test_should_skip_excludes_pycache_directory(tmp_path: Path) -> None:
    # Arrange
    p = tmp_path / "lucnhan" / "core" / "__pycache__" / "config.cpython-311.pyc"

    # Act
    result = should_skip(p)

    # Assert
    assert result is True


def test_should_skip_excludes_dotfiles_in_path(tmp_path: Path) -> None:
    # Arrange
    p = tmp_path / ".venv" / "lib" / "foo.py"

    # Act
    result = should_skip(p)

    # Assert
    assert result is True


def test_should_skip_excludes_pyc_suffix(tmp_path: Path) -> None:
    # Arrange
    p = tmp_path / "lucnhan" / "core" / "config.pyc"

    # Act
    result = should_skip(p)

    # Assert
    assert result is True


def test_should_skip_allows_normal_file(tmp_path: Path) -> None:
    # Arrange
    p = tmp_path / "lucnhan" / "core" / "config.py"

    # Act
    result = should_skip(p)

    # Assert
    assert result is False


# ── collect ────────────────────────────────────────────────────────────


def _seed_fake_repo(root: Path) -> None:
    """Mimic the real repo layout: runtime files, dev files, junk files."""
    # Runtime files (must be included)
    (root / "lucnhan_plugin.py").write_text("# plugin entry", encoding="utf-8")
    (root / "install.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (root / "install_ida.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (root / "install.ps1").write_text("# ps1\n", encoding="utf-8")
    (root / "install_ida.bat").write_text("@echo off\n", encoding="utf-8")
    (root / "requirements.txt").write_text("anthropic>=0.39.0\n", encoding="utf-8")
    (root / "ida-plugin.json").write_text('{"plugin":{"version":"1.2.3","entryPoint":"lucnhan_plugin.py"}}\n', encoding="utf-8")
    (root / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (root / "README.md").write_text("# Luc Nhan\n", encoding="utf-8")
    # lucnhan/ package (must be included, recursively)
    (root / "lucnhan").mkdir()
    (root / "lucnhan" / "__init__.py").write_text("", encoding="utf-8")
    (root / "lucnhan" / "core").mkdir()
    (root / "lucnhan" / "core" / "__init__.py").write_text("", encoding="utf-8")
    (root / "lucnhan" / "core" / "config.py").write_text("# config\n", encoding="utf-8")
    # lucnhan/skills/builtins/ subdir (real plugin loads from here)
    (root / "lucnhan" / "skills").mkdir()
    (root / "lucnhan" / "skills" / "builtins").mkdir()
    (root / "lucnhan" / "skills" / "builtins" / "ctf").mkdir()
    (root / "lucnhan" / "skills" / "builtins" / "ctf" / "SKILL.md").write_text("# ctf\n", encoding="utf-8")
    # Junk that MUST be excluded
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("# test\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "x.md").write_text("# doc\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
    (root / "ARCHITECTURE.md").write_text("# arch\n", encoding="utf-8")
    (root / "DEVELOPMENT.md").write_text("# dev\n", encoding="utf-8")
    (root / "llms.txt").write_text("# llms\n", encoding="utf-8")
    (root / ".github").mkdir()
    (root / ".github" / "workflows").mkdir()
    (root / ".github" / "workflows" / "ci.yml").write_text("# ci\n", encoding="utf-8")
    (root / "assets").mkdir()
    (root / "assets" / "icon.png").write_bytes(b"\x89PNG")
    (root / "chat_examples").mkdir()
    (root / "chat_examples" / "example.md").write_text("# ex\n", encoding="utf-8")
    (root / "webpage").mkdir()
    (root / "webpage" / "index.html").write_text("<html/>\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("# toml\n", encoding="utf-8")
    (root / "uv.lock").write_text("# lock\n", encoding="utf-8")
    (root / "ci-local.sh").write_text("# ci script\n", encoding="utf-8")
    # Junk inside lucnhan/ that MUST be excluded
    (root / "lucnhan" / "core" / "__pycache__").mkdir()
    (root / "lucnhan" / "core" / "__pycache__" / "config.cpython-311.pyc").write_bytes(b"PYC")
    (root / "lucnhan" / ".mypy_cache").mkdir()
    (root / "lucnhan" / ".mypy_cache" / "x.json").write_text("{}\n", encoding="utf-8")
    (root / "lucnhan" / "core" / "leftover.pyc").write_bytes(b"PYC")


def test_collect_includes_runtime_files(tmp_path: Path) -> None:
    # Arrange
    _seed_fake_repo(tmp_path)

    # Act
    result = collect(tmp_path)

    # Assert: every INCLUDE_PATHS file appears in result
    included = {p.relative_to(tmp_path).as_posix() for p in result}
    for spec in INCLUDE_PATHS:
        if spec == "lucnhan":
            # recursive — covered in other tests
            continue
        assert spec in included, f"expected {spec!r} in collect() output"


def test_collect_includes_nested_runtime_files(tmp_path: Path) -> None:
    # Arrange
    _seed_fake_repo(tmp_path)

    # Act
    result = collect(tmp_path)

    # Assert: nested files inside lucnhan/ are present
    included = {p.relative_to(tmp_path).as_posix() for p in result}
    assert "lucnhan/__init__.py" in included
    assert "lucnhan/core/config.py" in included
    assert "lucnhan/skills/builtins/ctf/SKILL.md" in included


def test_collect_excludes_tests_and_docs(tmp_path: Path) -> None:
    # Arrange
    _seed_fake_repo(tmp_path)

    # Act
    result = collect(tmp_path)

    # Assert
    included = {p.relative_to(tmp_path).as_posix() for p in result}
    assert "tests/test_x.py" not in included
    assert "docs/x.md" not in included
    assert "AGENTS.md" not in included
    assert "ARCHITECTURE.md" not in included
    assert "DEVELOPMENT.md" not in included
    assert "llms.txt" not in included


def test_collect_excludes_dev_assets_and_config(tmp_path: Path) -> None:
    # Arrange
    _seed_fake_repo(tmp_path)

    # Act
    result = collect(tmp_path)

    # Assert
    included = {p.relative_to(tmp_path).as_posix() for p in result}
    assert "assets/icon.png" not in included
    assert "chat_examples/example.md" not in included
    assert "webpage/index.html" not in included
    assert "pyproject.toml" not in included
    assert "uv.lock" not in included
    assert "ci-local.sh" not in included
    assert ".github/workflows/ci.yml" not in included


def test_collect_excludes_pycache_and_dotfiles(tmp_path: Path) -> None:
    # Arrange
    _seed_fake_repo(tmp_path)

    # Act
    result = collect(tmp_path)

    # Assert
    included = {p.relative_to(tmp_path).as_posix() for p in result}
    assert "lucnhan/core/__pycache__/config.cpython-311.pyc" not in included
    assert "lucnhan/core/leftover.pyc" not in included
    assert "lucnhan/.mypy_cache/x.json" not in included


def test_collect_returns_sorted_output(tmp_path: Path) -> None:
    # Arrange
    _seed_fake_repo(tmp_path)

    # Act
    result = collect(tmp_path)

    # Assert
    rel = [p.relative_to(tmp_path).as_posix() for p in result]
    assert rel == sorted(rel)


def test_collect_handles_missing_specs_gracefully(tmp_path: Path) -> None:
    # Arrange: a bare-minimum repo with no lucnhan/ package
    (tmp_path / "lucnhan_plugin.py").write_text("#\n", encoding="utf-8")

    # Act
    result = collect(tmp_path)

    # Assert: doesn't crash; just collects what exists
    included = {p.relative_to(tmp_path).as_posix() for p in result}
    assert "lucnhan_plugin.py" in included


# ── build_zip (HCLI flat layout) ──────────────────────────────────────


def test_build_zip_creates_valid_archive(tmp_path: Path) -> None:
    # Arrange
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("world", encoding="utf-8")
    out = tmp_path / "out.zip"
    files = [tmp_path / "a.txt", tmp_path / "sub" / "b.txt"]

    # Act
    build_zip(files, out, tmp_path)

    # Assert
    assert out.is_file()
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert "a.txt" in names
    assert "sub/b.txt" in names


def test_build_zip_ida_plugin_json_at_root(tmp_path: Path) -> None:
    # Arrange — simulate the real plugin layout
    _seed_fake_repo(tmp_path)
    files = collect(tmp_path)
    out = tmp_path / "out.zip"

    # Act
    build_zip(files, out, tmp_path)

    # Assert: ida-plugin.json is a top-level entry (HCLI contract)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert "ida-plugin.json" in names, f"ida-plugin.json not at root; got {names[:5]}"


def test_build_zip_entry_point_at_root(tmp_path: Path) -> None:
    # Arrange
    _seed_fake_repo(tmp_path)
    files = collect(tmp_path)
    out = tmp_path / "out.zip"

    # Act
    build_zip(files, out, tmp_path)

    # Assert: the entryPoint file (lucnhan_plugin.py) is a top-level entry
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert "lucnhan_plugin.py" in names


def test_build_zip_no_wrapping_subfolder(tmp_path: Path) -> None:
    # Arrange — HCLI requires flat layout; reject any wrapping subfolder
    _seed_fake_repo(tmp_path)
    files = collect(tmp_path)
    out = tmp_path / "out.zip"

    # Act
    build_zip(files, out, tmp_path)

    # Assert: no entry begins with a single wrapping prefix like "lucnhan-v1.2.3/"
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    wrapping = [n for n in names if "/" in n and not n.startswith(("lucnhan/", "install", "scripts/"))]
    # entries like "lucnhan/core/config.py" are fine (real subdir); only a SINGLE
    # common prefix on every entry would indicate a wrapping subfolder
    import os.path
    top_dirs = {n.split("/")[0] for n in names if "/" in n}
    # If there is exactly ONE top-level dir wrapping everything, that's the bug.
    assert len(top_dirs) > 1, f"single wrapping subfolder detected: {top_dirs}"


def test_build_zip_preserves_file_contents(tmp_path: Path) -> None:
    # Arrange
    (tmp_path / "x.py").write_text("secret content 12345\n", encoding="utf-8")
    out = tmp_path / "out.zip"

    # Act
    build_zip([tmp_path / "x.py"], out, tmp_path)

    # Assert
    with zipfile.ZipFile(out) as zf:
        assert zf.read("x.py") == b"secret content 12345\n"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd /d/re_dev_projects/vibe-clone/lucnhan && python -m pytest tests/scripts/test_build_release.py -v`

Expected: every test fails with `ImportError: cannot import name 'should_skip' from 'scripts.build_release'` (or similar) — `scripts/build_release.py` does not exist yet.

- [ ] **Step 4: Implement `should_skip()`, `collect()`, `build_zip()`**

Create `scripts/build_release.py`. Leave `sha256_file`, `write_sha256sums`, `main` as `NotImplementedError` stubs (filled in Task 3):

```python
"""Build curated release archive for Luc Nhan IDA plugin (HCLI flat-ZIP layout).

Chỉ include runtime files cần để install và chạy plugin trong IDA:
- lucnhan_plugin.py  (entry point)
- lucnhan/           (Python package, loại __pycache__)
- install.sh, install_ida.sh, install.ps1, install_ida.bat
- requirements.txt
- ida-plugin.json
- LICENSE
- README.md

Không include: tests/, docs/, AGENTS.md, ARCHITECTURE.md, DEVELOPMENT.md,
llms.txt, .github/, assets/, chat_examples/, webpage/, pyproject.toml,
uv.lock, ci-local.sh, .git/, .venv/, .*_cache/, __pycache__/.

HCLI layout (per https://hcli.docs.hex-rays.com/reference/plugin-packaging-and-format/):
ida-plugin.json và entryPoint phải ở GỐC zip, không có subfolder bao quanh.

Usage:
    python scripts/build_release.py --version 1.2.3 --out-dir dist

Output:
    dist/lucnhan-v1.2.3.zip
    dist/SHA256SUMS
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

# Tên file/dir cần include (paths tương đối so với source root).
INCLUDE_PATHS: list[str] = [
    "lucnhan_plugin.py",
    "lucnhan",  # toàn bộ package
    "install.sh",
    "install_ida.sh",
    "install.ps1",
    "install_ida.bat",
    "requirements.txt",
    "ida-plugin.json",
    "LICENSE",
    "README.md",
]

# File/dir KHÔNG được include dù nằm trong INCLUDE_PATHS (match bất kỳ path part nào).
EXCLUDE_NAMES: set[str] = {
    "__pycache__",
    ".git",
    ".venv",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".desloppify",
    ".codegraph",
    ".reasonix",
    ".claude",
    "node_modules",
}

# File suffix KHÔNG được include.
EXCLUDE_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo", ".pyd")


def should_skip(path: Path) -> bool:
    """Return True nếu path nên bị skip (exclude rule match)."""
    if any(part in EXCLUDE_NAMES for part in path.parts):
        return True
    if path.suffix in EXCLUDE_SUFFIXES:
        return True
    return False


def collect(source_root: Path) -> list[Path]:
    """Collect tất cả files trong INCLUDE_PATHS, áp dụng exclude rules.

    Returns:
        Sorted list of absolute paths to files.
    """
    collected: list[Path] = []
    for spec in INCLUDE_PATHS:
        src = source_root / spec
        if src.is_file():
            if not should_skip(src):
                collected.append(src)
        elif src.is_dir():
            for p in src.rglob("*"):
                if p.is_file() and not should_skip(p):
                    collected.append(p)
        # Nếu spec không tồn tại → silently skip
    return sorted(collected)


def build_zip(files: list[Path], out_path: Path, source_root: Path) -> None:
    """Build FLAT zip archive (HCLI layout) — no wrapping subfolder.

    Each file's path inside the archive is its path relative to
    ``source_root`` (forward slashes via ``as_posix()``). ``ida-plugin.json``
    and the entryPoint file end up at the ZIP root.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for src in files:
            arcname = src.relative_to(source_root).as_posix()
            zf.write(src, arcname)


# ── Stubs filled in by Task 3 ─────────────────────────────────────────


def sha256_file(path: Path) -> str:
    """Compute SHA256 hex digest của file."""
    raise NotImplementedError


def write_sha256sums(paths: list[Path], out_path: Path) -> None:
    """Write SHA256SUMS file (GNU coreutils format)."""
    raise NotImplementedError


def main() -> int:
    """CLI entry point. See module docstring."""
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd /d/re_dev_projects/vibe-clone/lucnhan && python -m pytest tests/scripts/test_build_release.py -v`

Expected: all tests pass (4 should_skip + 7 collect + 5 build_zip). The `sha256_file`/`write_sha256sums` stubs are imported but not yet called, so `NotImplementedError` is fine.

- [ ] **Step 6: Run `./ci-local.sh` to confirm no regressions**

Run: `cd /d/re_dev_projects/vibe-clone/lucnhan && ./ci-local.sh`

Expected: ALL PASSED. The new files are outside `lucnhan/` (ruff/mypy skip them); pytest should pick up `tests/scripts/`.

- [ ] **Step 7: Commit**

```bash
cd /d/re_dev_projects/vibe-clone/lucnhan
git add scripts/__init__.py tests/scripts/__init__.py scripts/build_release.py tests/scripts/test_build_release.py
git commit -m "feat(scripts): add build_release.py with collect/build_zip (flat HCLI layout)"
```

---

## Task 2: TDD `validate_archive.py` (HCLI structural shim)

**Files:**
- Create: `scripts/validate_archive.py`
- Create: `tests/scripts/test_validate_archive.py`

**Interfaces:**
- Consumes: a path to a built `lucnhan-v{version}.zip`.
- Produces: `validate_archive(zip_path: Path) -> None` — raises `ArchiveValidationError` if the ZIP is not HCLI-compliant; returns silently otherwise.

**Why a shim instead of only `hcli`:** HCLI (`hex-rays-cli`) install method may change or be unavailable on a given runner. The shim catches the most common packaging mistake (wrapping subfolder / missing `ida-plugin.json` at root / missing `entryPoint`) deterministically in Python, so the build job fails fast even if `hcli` is absent. When `hcli` IS present, the workflow runs `hcli plugin lint` first (authoritative); the shim is the fallback.

- [ ] **Step 1: Write the failing tests**

Create `tests/scripts/test_validate_archive.py`:

```python
"""Unit tests for scripts/validate_archive.py (HCLI structural shim)."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from scripts.validate_archive import ArchiveValidationError, validate_archive


def _make_zip(entries: dict[str, str | bytes]) -> bytes:
    """Build an in-memory zip from a {arcname: content} mapping."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            if isinstance(content, str):
                zf.writestr(name, content)
            else:
                zf.writestr(name, content)
    return buf.getvalue()


def test_validate_flat_zip_passes(tmp_path: Path) -> None:
    # Arrange — ida-plugin.json + entryPoint at root, lucnhan/ package
    data = _make_zip({
        "ida-plugin.json": json.dumps({"plugin": {"entryPoint": "lucnhan_plugin.py"}}),
        "lucnhan_plugin.py": "# entry",
        "lucnhan/__init__.py": "",
    })
    p = tmp_path / "lucnhan-v1.0.zip"
    p.write_bytes(data)

    # Act + Assert — no exception
    validate_archive(p)


def test_validate_rejects_missing_ida_plugin_json(tmp_path: Path) -> None:
    # Arrange — no metadata file
    data = _make_zip({"lucnhan_plugin.py": "# entry"})
    p = tmp_path / "lucnhan-v1.0.zip"
    p.write_bytes(data)

    # Act + Assert
    with pytest.raises(ArchiveValidationError, match="ida-plugin.json"):
        validate_archive(p)


def test_validate_rejects_wrapping_subfolder(tmp_path: Path) -> None:
    # Arrange — ida-plugin.json nested under lucnhan-v1.0/
    data = _make_zip({
        "lucnhan-v1.0/ida-plugin.json": json.dumps({"plugin": {"entryPoint": "lucnhan_plugin.py"}}),
        "lucnhan-v1.0/lucnhan_plugin.py": "# entry",
    })
    p = tmp_path / "lucnhan-v1.0.zip"
    p.write_bytes(data)

    # Act + Assert
    with pytest.raises(ArchiveValidationError, match="root"):
        validate_archive(p)


def test_validate_rejects_missing_entry_point(tmp_path: Path) -> None:
    # Arrange — metadata points to entryPoint that isn't in the zip
    data = _make_zip({
        "ida-plugin.json": json.dumps({"plugin": {"entryPoint": "lucnhan_plugin.py"}}),
    })
    p = tmp_path / "lucnhan-v1.0.zip"
    p.write_bytes(data)

    # Act + Assert
    with pytest.raises(ArchiveValidationError, match="entryPoint"):
        validate_archive(p)


def test_validate_rejects_invalid_json_metadata(tmp_path: Path) -> None:
    # Arrange
    data = _make_zip({
        "ida-plugin.json": "not json {{{",
        "lucnhan_plugin.py": "# entry",
    })
    p = tmp_path / "lucnhan-v1.0.zip"
    p.write_bytes(data)

    # Act + Assert
    with pytest.raises(ArchiveValidationError, match="json"):
        validate_archive(p)
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `cd /d/re_dev_projects/vibe-clone/lucnhan && python -m pytest tests/scripts/test_validate_archive.py -v`

Expected: all 5 tests fail with `ImportError: No module named 'scripts.validate_archive'`.

- [ ] **Step 3: Implement `validate_archive.py`**

Create `scripts/validate_archive.py`:

```python
"""Structural validation shim for HCLI plugin archives.

If ``hcli`` is not available on the runner, this module provides a
deterministic Python check that the built ZIP is HCLI-compliant:

- ``ida-plugin.json`` is at the ZIP root (not nested in a subfolder)
- ``ida-plugin.json`` is valid JSON with a ``plugin.entryPoint`` field
- The ``entryPoint`` file exists at the ZIP root
- No single subfolder wraps every entry

When ``hcli`` IS available, prefer ``hcli plugin lint <zip>`` — it is
authoritative. This shim is the fallback.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path


class ArchiveValidationError(Exception):
    """Raised when a ZIP is not HCLI-compliant."""


def validate_archive(zip_path: Path) -> None:
    """Validate that ``zip_path`` is an HCLI-compliant plugin archive.

    Raises:
        ArchiveValidationError: if any HCLI contract is violated.
    """
    if not zip_path.is_file():
        raise ArchiveValidationError(f"archive not found: {zip_path}")

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()

    # 1. ida-plugin.json must be a top-level entry
    if "ida-plugin.json" not in names:
        raise ArchiveValidationError(
            "ida-plugin.json not found at the archive root "
            f"(got top-level entries: {sorted({n.split('/')[0] for n in names})})"
        )

    # 2. No single subfolder wraps everything
    top_levels = {n.split("/")[0] for n in names}
    if len(top_levels) == 1 and next(iter(top_levels)) != "ida-plugin.json":
        # Everything is under one dir — that's a wrapping subfolder
        raise ArchiveValidationError(
            f"archive appears wrapped in a single subfolder: {top_levels}; "
            "HCLI requires ida-plugin.json at the archive root"
        )

    # 3. ida-plugin.json must be valid JSON with plugin.entryPoint
    with zipfile.ZipFile(zip_path) as zf:
        try:
            meta = json.loads(zf.read("ida-plugin.json").decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ArchiveValidationError(f"ida-plugin.json is not valid JSON: {e}") from e

    plugin = meta.get("plugin") if isinstance(meta, dict) else None
    if not isinstance(plugin, dict):
        raise ArchiveValidationError("ida-plugin.json missing 'plugin' object")
    entry_point = plugin.get("entryPoint")
    if not entry_point or not isinstance(entry_point, str):
        raise ArchiveValidationError("ida-plugin.json missing 'plugin.entryPoint' string")

    # 4. The entryPoint file must exist at the ZIP root
    if entry_point not in names:
        raise ArchiveValidationError(
            f"entryPoint {entry_point!r} not found in archive root "
            f"(HCLI requires the entry file at the root)"
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /d/re_dev_projects/vibe-clone/lucnhan && python -m pytest tests/scripts/test_validate_archive.py -v`

Expected: all 5 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /d/re_dev_projects/vibe-clone/lucnhan
git add scripts/validate_archive.py tests/scripts/test_validate_archive.py
git commit -m "feat(scripts): add validate_archive.py (HCLI structural shim)"
```

---

## Task 3: TDD `sha256_file()` + `write_sha256sums()` + `main()` in `build_release.py`

**Files:**
- Modify: `scripts/build_release.py` (replace `NotImplementedError` stubs + add CLI)

**Interfaces:**
- Consumes: CLI args (`--version`, `--out-dir`, `--source-root`).
- Produces: `lucnhan-v{version}.zip` + `SHA256SUMS`; exit code 0 on success, 1 on empty collect.

- [ ] **Step 1: Write the failing tests**

Append to `tests/scripts/test_build_release.py`:

```python
import hashlib
import re


# ── sha256_file ───────────────────────────────────────────────────────


def test_sha256_matches_stdlib(tmp_path: Path) -> None:
    # Arrange
    p = tmp_path / "x.txt"
    p.write_bytes(b"hello world")
    expected = hashlib.sha256(b"hello world").hexdigest()

    # Act
    result = sha256_file(p)

    # Assert
    assert result == expected
    assert len(result) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", result)


def test_sha256_handles_large_file(tmp_path: Path) -> None:
    # Arrange: 5 MB random data
    p = tmp_path / "big.bin"
    p.write_bytes(bytes(range(256)) * (5 * 1024 * 1024 // 256))
    expected = hashlib.sha256(p.read_bytes()).hexdigest()

    # Act
    result = sha256_file(p)

    # Assert
    assert result == expected


# ── write_sha256sums ───────────────────────────────────────────────────


def test_write_sha256sums_format(tmp_path: Path) -> None:
    # Arrange
    a = tmp_path / "lucnhan-v1.0.zip"
    a.write_bytes(b"alpha")
    out = tmp_path / "SHA256SUMS"

    # Act
    write_sha256sums([a], out)

    # Assert: two-space separator, hex + filename
    content = out.read_text(encoding="utf-8").strip()
    m = re.fullmatch(r"^([0-9a-f]{64})  (lucnhan-v1\.0\.zip)$", content)
    assert m, f"unexpected SHA256SUMS format: {content!r}"


# ── main() / CLI ──────────────────────────────────────────────────────


def _run_main(cwd: Path, argv: list[str]) -> int:
    """Helper: run main() with the given argv (no subprocess)."""
    import os
    import sys
    old_argv = sys.argv
    old_cwd = Path.cwd()
    sys.argv = ["build_release.py", *argv]
    try:
        os.chdir(cwd)
        from scripts import build_release
        return build_release.main()
    finally:
        sys.argv = old_argv
        os.chdir(old_cwd)


def test_main_writes_zip_and_sums(tmp_path: Path) -> None:
    # Arrange
    _seed_fake_repo(tmp_path)
    out_dir = tmp_path / "dist"

    # Act
    rc = _run_main(tmp_path, ["--version", "1.2.3", "--out-dir", str(out_dir), "--source-root", str(tmp_path)])

    # Assert
    assert rc == 0
    assert (out_dir / "lucnhan-v1.2.3.zip").is_file()
    assert (out_dir / "SHA256SUMS").is_file()
    # No tar.gz
    assert not (out_dir / "lucnhan-v1.2.3.tar.gz").exists()


def test_main_archive_is_flat(tmp_path: Path) -> None:
    # Arrange
    _seed_fake_repo(tmp_path)
    out_dir = tmp_path / "dist"

    # Act
    _run_main(tmp_path, ["--version", "1.2.3", "--out-dir", str(out_dir), "--source-root", str(tmp_path)])

    # Assert: ida-plugin.json at root
    with zipfile.ZipFile(out_dir / "lucnhan-v1.2.3.zip") as zf:
        names = zf.namelist()
    assert "ida-plugin.json" in names
    assert "lucnhan_plugin.py" in names


def test_main_fails_when_no_files_collected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Arrange: completely empty source root
    out_dir = tmp_path / "dist"

    # Act
    rc = _run_main(tmp_path, ["--version", "9.9.9", "--out-dir", str(out_dir), "--source-root", str(tmp_path)])

    # Assert
    assert rc == 1
    captured = capsys.readouterr()
    assert "no files collected" in captured.err.lower()


def test_main_requires_version_arg(tmp_path: Path) -> None:
    # Act + Assert
    with pytest.raises(SystemExit) as exc:
        _run_main(tmp_path, ["--out-dir", str(tmp_path), "--source-root", str(tmp_path)])
    assert exc.value.code == 2  # argparse error code
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `cd /d/re_dev_projects/vibe-clone/lucnhan && python -m pytest tests/scripts/test_build_release.py -v -k "sha256 or main or sums"`

Expected: the new tests fail with `NotImplementedError` (the stubs) or `ImportError`.

- [ ] **Step 3: Implement `sha256_file()`, `write_sha256sums()`, `main()`**

Edit `scripts/build_release.py`. Replace the three `NotImplementedError` stubs with:

```python
def sha256_file(path: Path) -> str:
    """Compute SHA256 hex digest của file (streamed, 1 MB chunks)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_sha256sums(paths: list[Path], out_path: Path) -> None:
    """Write SHA256SUMS file (GNU coreutils format: hex + 2 spaces + name)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for p in paths:
            f.write(f"{sha256_file(p)}  {p.name}\n")


def main() -> int:
    """CLI entry point. See module docstring.

    Returns:
        0 on success, 1 if no files were collected.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Version vd: 1.2.3")
    parser.add_argument("--out-dir", type=Path, default=Path("dist"))
    parser.add_argument("--source-root", type=Path, default=Path("."))
    args = parser.parse_args()

    archive_name = f"lucnhan-v{args.version}.zip"
    args.out_dir.mkdir(parents=True, exist_ok=True)
    files = collect(args.source_root)
    if not files:
        print("ERROR: no files collected (source root empty?)", file=sys.stderr)
        return 1

    zip_path = args.out_dir / archive_name
    build_zip(files, zip_path, args.source_root)

    sums_path = args.out_dir / "SHA256SUMS"
    write_sha256sums([zip_path], sums_path)

    print(f"OK: {zip_path} ({zip_path.stat().st_size} bytes, {len(files)} files)")
    print(f"OK: {sums_path}")
    return 0
```

- [ ] **Step 4: Run all build_release tests**

Run: `cd /d/re_dev_projects/vibe-clone/lucnhan && python -m pytest tests/scripts/test_build_release.py -v`

Expected: all tests pass (4 should_skip + 7 collect + 5 build_zip + 2 sha256 + 1 sums + 4 main).

- [ ] **Step 5: End-to-end local dry-run**

```bash
cd /d/re_dev_projects/vibe-clone/lucnhan
python scripts/build_release.py --version 9.9.9 --out-dir /tmp/lucnhan-dryrun --source-root .
unzip -l /tmp/lucnhan-dryrun/lucnhan-v9.9.9.zip | head -20
cd /tmp/lucnhan-dryrun && sha256sum -c SHA256SUMS
rm -rf /tmp/lucnhan-dryrun
```

Expected: `unzip -l` shows `ida-plugin.json` and `lucnhan_plugin.py` as top-level entries (no subfolder). `sha256sum -c` prints `OK`.

- [ ] **Step 6: Run `./ci-local.sh`**

Run: `cd /d/re_dev_projects/vibe-clone/lucnhan && ./ci-local.sh`

Expected: ALL PASSED.

- [ ] **Step 7: Commit**

```bash
cd /d/re_dev_projects/vibe-clone/lucnhan
git add scripts/build_release.py tests/scripts/test_build_release.py
git commit -m "feat(scripts): add sha256 + CLI entry point (flat zip, no tar.gz)"
```

---

## Task 4: Write `.github/workflows/release.yml`

**Files:**
- Rewrite: `.github/workflows/release.yml`

**Interfaces:**
- Consumes: tag push (`v*` or `[0-9]*.[0-9]*`) OR `workflow_dispatch` input `tag`
- Produces: GitHub Release with `lucnhan-v{version}.zip` + `SHA256SUMS`, pre-release flag from tag suffix

- [ ] **Step 1: Write the new workflow**

Replace the entire contents of `.github/workflows/release.yml` with:

```yaml
name: Release

on:
  push:
    tags:
      - 'v*'
      - '[0-9]*.[0-9]*'
  workflow_dispatch:
    inputs:
      tag:
        description: 'Tag name to re-release (vd: v1.2 hoặc 1.2.3)'
        required: true
        type: string

permissions:
  contents: write

jobs:
  verify:
    name: Verify (tag + version + tests)
    runs-on: ubuntu-latest
    outputs:
      tag: ${{ steps.meta.outputs.tag }}
      version: ${{ steps.meta.outputs.version }}
      is_prerelease: ${{ steps.meta.outputs.is_prerelease }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dev tools
        run: pip install ruff mypy pytest tomli desloppify

      - name: Parse tag, version, pre-release flag
        id: meta
        run: |
          set -e
          if [ "${{ github.event_name }}" = "workflow_dispatch" ]; then
            TAG="${{ inputs.tag }}"
          else
            TAG="${GITHUB_REF_NAME}"
          fi

          VERSION="${TAG#v}"

          PLUGIN_VERSION=$(python -c "import json; print(json.load(open('ida-plugin.json'))['plugin']['version'])")
          if [ "$VERSION" != "$PLUGIN_VERSION" ]; then
            echo "::error::Tag '$TAG' (→ version '$VERSION') does not match ida-plugin.json '$PLUGIN_VERSION'"
            exit 1
          fi

          if [[ "$TAG" =~ -(rc|alpha|beta|pre|dev)[0-9]*$ ]]; then
            IS_PRERELEASE="true"
          else
            IS_PRERELEASE="false"
          fi

          echo "tag=$TAG" >> "$GITHUB_OUTPUT"
          echo "version=$VERSION" >> "$GITHUB_OUTPUT"
          echo "is_prerelease=$IS_PRERELEASE" >> "$GITHUB_OUTPUT"
          echo "::notice::Parsed: tag=$TAG, version=$VERSION, is_prerelease=$IS_PRERELEASE"

      - name: Re-run CI checks (inline)
        run: |
          set -e
          echo "── ruff format check ──"
          python -m ruff format --check lucnhan/

          echo "── ruff lint ──"
          python -m ruff check lucnhan/

          echo "── mypy ──"
          python -m mypy lucnhan/core lucnhan/providers

          echo "── pytest ──"
          python -m pytest tests/ --tb=short -q

          echo "── desloppify (objective score) ──"
          desloppify scan --profile objective --no-badge
          SCORE=$(python -c "import json; print(json.load(open('.desloppify/query.json')).get('objective_score', 0))")
          BASELINE=89.0
          python -c "
          import sys
          s = float('$SCORE')
          if s < $BASELINE - 0.5:
              sys.exit(f'score {s} < baseline {$BASELINE} - 0.5')
          print(f'OK — score {s}')
          "

  build:
    name: Build + validate artifacts
    needs: verify
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Run build_release.py
        run: |
          python scripts/build_release.py \
            --version "${{ needs.verify.outputs.version }}" \
            --out-dir dist

      - name: HCLI packaging validation (shim fallback)
        run: |
          # Prefer hcli plugin lint if available; else run the Python shim.
          if command -v hcli >/dev/null 2>&1; then
            echo "hcli found — running 'hcli plugin lint'"
            hcli plugin lint "dist/lucnhan-v${{ needs.verify.outputs.version }}.zip"
          else
            echo "hcli not found — running structural shim"
            python scripts/validate_archive.py "dist/lucnhan-v${{ needs.verify.outputs.version }}.zip"
          fi

      - name: Upload artifacts
        uses: actions/upload-artifact@v4
        with:
          name: release-artifacts
          path: dist/
          if-no-files-found: error

  publish:
    name: Publish GitHub Release
    needs: [verify, build]
    runs-on: ubuntu-latest
    steps:
      - name: Download build artifacts
        uses: actions/download-artifact@v4
        with:
          name: release-artifacts
          path: dist/

      - name: Create or update GitHub Release
        uses: softprops/action-gh-release@v2
        with:
          tag_name: ${{ needs.verify.outputs.tag }}
          name: "Luc Nhan ${{ needs.verify.outputs.version }}"
          prerelease: ${{ needs.verify.outputs.is_prerelease == 'true' }}
          generate_release_notes: true
          fail_on_unmatched_files: true
          files: |
            dist/lucnhan-${{ needs.verify.outputs.version }}.zip
            dist/SHA256SUMS
```

> **Note:** The `validate_archive.py` CLI entry point is referenced by the workflow. Add an `if __name__ == "__main__":` block at the bottom of `scripts/validate_archive.py` that calls `validate_archive(Path(sys.argv[1]))` and exits non-zero on `ArchiveValidationError`. Do this in Task 2 (the validate_archive task) or as a tiny follow-up edit — the workflow depends on it being runnable as `python scripts/validate_archive.py <zip>`.

- [ ] **Step 2: Validate YAML syntax locally**

```bash
cd /d/re_dev_projects/vibe-clone/lucnhan
python -c "import yaml; yaml.safe_load(open('.github/workflows/release.yml').read())" && echo "YAML OK"
```

Expected: prints `YAML OK`.

- [ ] **Step 3: Ensure `validate_archive.py` is runnable as CLI**

If Task 2 did not already add the CLI block, append to `scripts/validate_archive.py`:

```python
if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("usage: validate_archive.py <zip-path>", file=sys.stderr)
        sys.exit(2)
    try:
        validate_archive(Path(sys.argv[1]))
    except ArchiveValidationError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        sys.exit(1)
    print("OK")
```

Run a local smoke test of the shim:

```bash
cd /d/re_dev_projects/vibe-clone/lucnhan
python scripts/build_release.py --version 9.9.9 --out-dir /tmp/shim-test --source-root .
python scripts/validate_archive.py /tmp/shim-test/lucnhan-v9.9.9.zip
rm -rf /tmp/shim-test
```

Expected: prints `OK`.

- [ ] **Step 4: Commit**

```bash
cd /d/re_dev_projects/vibe-clone/lucnhan
git add .github/workflows/release.yml scripts/validate_archive.py
git commit -m "feat(ci): rewrite release.yml as 3-job pipeline (verify→build→publish)"
```

---

## Task 5: Update docs

**Files:**
- Modify: `AGENTS.md`
- Modify: `DEVELOPMENT.md`
- Modify: `.gitignore`

- [ ] **Step 1: Update `AGENTS.md`**

Find the "### Release Flow" subsection. Replace it with:

```markdown
### Release Flow

1. Bump `version` trong `ida-plugin.json` (trên `master`)
2. Commit + push lên `master`
3. Tag và push:
   ```bash
   git tag v1.x.x
   git push origin v1.x.x
   ```
4. GitHub Actions workflow `.github/workflows/release.yml` tự động:
   - **`verify`** — validate tag ↔ `ida-plugin.json.version`, re-run toàn bộ CI checks inline (ruff/mypy/pytest/desloppify). Fail → release không publish.
   - **`build`** — chạy `scripts/build_release.py` tạo `lucnhan-v1.x.x.zip` (flat HCLI layout), validate bằng `hcli plugin lint` (hoặc Python shim `scripts/validate_archive.py` nếu HCLI không có), tạo `SHA256SUMS`.
   - **`publish`** — `softprops/action-gh-release@v2` tạo/cập nhật GitHub Release với artifact + auto-generated notes.
5. Tag suffix `-rc1`, `-beta1`, `-dev1`, ... → auto pre-release. Tag `v1.x.x` (no suffix) → stable.

**HCLI layout**: ZIP phải phẳng — `ida-plugin.json` và `lucnhan_plugin.py` ở gốc, không có subfolder bao quanh (spec Hex-Rays). User install bằng `hcli plugin install lucnhan-v1.x.x.zip`.

**Re-run cho tag đã push**: Actions tab → workflow "Release" → "Run workflow" → nhập tag name.

**Trigger pattern**: `v*` (chuẩn) **và** bare version (vd: `1.0` — legacy). Xem `docs/superpowers/specs/2026-06-18-github-release-pipeline-design.md`.
```

- [ ] **Step 2: Update `DEVELOPMENT.md`**

Find "## Release Process". Replace with:

```markdown
## Release Process

Pipeline release đầy đủ tự động:

1. Bump `version` trong `ida-plugin.json`
2. Commit + push:
   ```bash
   git add ida-plugin.json
   git commit -m "chore: bump version to 1.x.x"
   git push origin master
   ```
3. Tag và push:
   ```bash
   git tag v1.x.x
   git push origin v1.x.x
   ```
4. GitHub Actions tự chạy (verify → build → publish). Release xuất hiện tại `https://github.com/EliteClassRoom/Luc-Nhan/releases/tag/v1.x.x` với 2 artifact: `lucnhan-v1.x.x.zip` + `SHA256SUMS`.

**Install artifact** (HCLI): `curl -L https://github.com/EliteClassRoom/Luc-Nhan/releases/download/v1.x.x/lucnhan-v1.x.x.zip -o lucnhan.zip` rồi `hcli plugin install lucnhan.zip`. ZIP phẳng theo spec Hex-Rays.

**Re-run**: Actions tab → workflow "Release" → "Run workflow" → nhập tag.

**Pre-release**: thêm suffix `-rc1`, `-beta1`, `-dev1` → GitHub tự đánh dấu pre-release.

**Local dry-run** (test trước khi tag):
```bash
python scripts/build_release.py --version 1.x.x-test --out-dir /tmp/lucnhan-test
unzip -l /tmp/lucnhan-test/lucnhan-v1.x.x-test.zip   # ida-plugin.json phải ở gốc
python scripts/validate_archive.py /tmp/lucnhan-test/lucnhan-v1.x.x-test.zip
rm -rf /tmp/lucnhan-test
```

**Smoke test pipeline**:
```bash
git tag v0.0.0-test && git push origin v0.0.0-test
# → check Actions tab: 3 jobs xanh
git push origin :refs/tags/v0.0.0-test
gh release delete v0.0.0-test --repo EliteClassRoom/Luc-Nhan --yes
```
```

- [ ] **Step 3: Add `dist/` to `.gitignore`**

Append to `.gitignore`:

```
# Local build output from scripts/build_release.py
dist/
```

- [ ] **Step 4: Commit**

```bash
cd /d/re_dev_projects/vibe-clone/lucnhan
git add AGENTS.md DEVELOPMENT.md .gitignore
git commit -m "docs(ci): document HCLI release pipeline + add dist/ to gitignore"
```

---

## Task 6: End-to-end local verification

**Files:** none (verification only)

- [ ] **Step 1: Run all build script tests**

Run: `cd /d/re_dev_projects/vibe-clone/lucnhan && python -m pytest tests/scripts/ -v`

Expected: all tests pass (`test_build_release.py` + `test_validate_archive.py`).

- [ ] **Step 2: Run `./ci-local.sh`**

Run: `cd /d/re_dev_projects/vibe-clone/lucnhan && ./ci-local.sh`

Expected: ALL PASSED.

- [ ] **Step 3: Local end-to-end build + validate**

```bash
cd /d/re_dev_projects/vibe-clone/lucnhan
python scripts/build_release.py --version 1.2 --out-dir /tmp/lucnhan-final --source-root .
python scripts/validate_archive.py /tmp/lucnhan-final/lucnhan-v1.2.zip
unzip -l /tmp/lucnhan-final/lucnhan-v1.2.zip | head -30
cd /tmp/lucnhan-final && sha256sum -c SHA256SUMS
```

Expected:
- `validate_archive.py` prints `OK`.
- `unzip -l` shows `ida-plugin.json` and `lucnhan_plugin.py` as top-level entries; no wrapping subfolder.
- `sha256sum -c` reports `OK`.

Verify excluded files are not present:
```bash
unzip -l /tmp/lucnhan-final/lucnhan-v1.2.zip | awk '{print $4}' | grep -E "(tests/|docs/|assets/|chat_examples/|webpage/|ci-local|pyproject|uv.lock|AGENTS.md|ARCHITECTURE.md|DEVELOPMENT.md|llms.txt|\.github/)" | head -5
```

Expected: empty output.

Clean up:
```bash
rm -rf /tmp/lucnhan-final
```

- [ ] **Step 4: Push and watch the GitHub Actions run**

```bash
cd /d/re_dev_projects/vibe-clone/lucnhan
git push origin master
git tag v0.0.0-test
git push origin v0.0.0-test
```

Then open `https://github.com/EliteClassRoom/Luc-Nhan/actions`. Confirm:
- `verify` job green (all 4 CI checks + tag/version match).
- `build` job green (`build_release.py` + `hcli plugin lint` or shim + upload).
- `publish` job green (draft release with `lucnhan-v0.0.0-test.zip` + `SHA256SUMS`).

Clean up:
```bash
git push origin :refs/tags/v0.0.0-test
gh release delete v0.0.0-test --repo EliteClassRoom/Luc-Nhan --yes
git tag -d v0.0.0-test
```

- [ ] **Step 5: Final commit (if any cleanup needed)**

If Step 4 surfaced a small fix, commit it. Otherwise the plan is complete.

---

## Self-Review

**Spec coverage check**:

| Spec section | Covered by |
|--------------|------------|
| Trigger model (v* + bare + dispatch) | Task 4 (workflow YAML) |
| verify job — tag/version/pre-release parse | Task 4 |
| verify job — inline CI re-run | Task 4 |
| build job — `scripts/build_release.py` flat zip | Tasks 1, 3 |
| build job — `hcli plugin lint` + shim fallback | Task 2 (shim) + Task 4 (workflow `if command -v hcli`) |
| build job — INCLUDE_PATHS / EXCLUDE_NAMES | Task 1 |
| build job — `lucnhan-v{version}.zip` flat layout | Task 1 (`build_zip`) + Task 3 (`main`) |
| build job — `SHA256SUMS` | Task 3 |
| build job — upload-artifact@v4 | Task 4 |
| publish job — softprops + prerelease + fail_on_unmatched_files | Task 4 |
| Pre-release detection regex | Task 4 |
| Files touched | Tasks 1–5 |
| HCLI flat-ZIP contract (ida-plugin.json at root, no subfolder, ZIP only) | Task 1 (`build_zip` signature + tests) + Task 2 (shim) + Global Constraints |
| Local dry-run | Task 3 (Step 5) + Task 6 (Step 3) |
| Post-merge smoke test | Task 6 (Step 4) |

**Placeholder scan**: No "TBD"/"TODO"/"implement later"/"similar to Task N". Every code step has full code; every command step has exact commands + expected output. (One deliberate anti-example block in Task 1 Step 2 is clearly labeled "delete those two duplicate functions" — the actual content is the second block.)

**Type consistency check**:
- `should_skip(path: Path) -> bool` — defined Task 1, used only inside `collect`.
- `collect(source_root: Path) -> list[Path]` — defined Task 1, used in `main` (Task 3) and tests.
- `build_zip(files: list[Path], out_path: Path, source_root: Path) -> None` — defined Task 1. Signature now takes `source_root` (was `arcname_root` in the old plan) because flat layout needs relative-to-root, not a wrapping prefix. Tests in Task 1 and `main` in Task 3 both pass `source_root`.
- `validate_archive(zip_path: Path) -> None` — defined Task 2, raised `ArchiveValidationError`, used in Task 4 workflow via CLI.
- `ArchiveValidationError` — defined Task 2.
- `sha256_file(path: Path) -> str`, `write_sha256sums(paths: list[Path], out_path: Path) -> None`, `main() -> int` — defined Task 3.
- `INCLUDE_PATHS`, `EXCLUDE_NAMES`, `EXCLUDE_SUFFIXES` — module constants Task 1.

No type / signature drift between tasks.

**Plan-vs-spec deviations**:
- Spec mentioned `archive_basename` as a verify output; dropped (YAGNI) — build recomputes from `version`. Documented in spec self-review.
- Spec's test table listed `test_build_tar_*`; removed — no tar.gz in HCLI layout. Replaced with `test_build_zip_ida_plugin_json_at_root`, `test_build_zip_entry_point_at_root`, `test_build_zip_no_wrapping_subfolder` (HCLI-specific).
