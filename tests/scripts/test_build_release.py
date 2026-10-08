"""Unit tests for scripts/build_release.py.

AAA pattern. Uses pytest's ``tmp_path`` fixture to seed a fake repo
layout that mirrors the real project, then runs ``collect()`` /
``build_zip()`` / etc. and asserts on the output.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path

import pytest

from scripts.build_release import (
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
    (root / "ida-plugin.json").write_text(
        '{"plugin":{"version":"1.2.3","entryPoint":"lucnhan_plugin.py"}}\n', encoding="utf-8"
    )
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
    # Nested tests/ and docs/ inside lucnhan/ (regression seed: these MUST be excluded
    # even though they live inside an INCLUDE_PATHS directory).
    (root / "lucnhan" / "tests").mkdir()
    (root / "lucnhan" / "tests" / "test_nested.py").write_text("# nested test\n", encoding="utf-8")
    (root / "lucnhan" / "docs").mkdir()
    (root / "lucnhan" / "docs" / "NESTED.md").write_text("# nested doc\n", encoding="utf-8")
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
            # recursive dir — covered in other tests
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


def test_collect_excludes_nested_tests_and_docs(tmp_path: Path) -> None:
    """Regression: tests/ and docs/ nested inside lucnhan/ must be excluded.

    The Task 1 test suite only seeded tests/ and docs/ at the REPO ROOT, where
    they are silently ignored because they sit outside INCLUDE_PATHS. The real
    lucnhan/ package ships nested lucnhan/tests/ and lucnhan/docs/ subdirs
    (see v1.2 leak: lucnhan/docs/HEADLESS_PROVIDER.md, lucnhan/tests/conftest.py,
    lucnhan/tests/test_*.py). These need an explicit EXCLUDE_NAMES entry.
    """
    # Arrange
    _seed_fake_repo(tmp_path)

    # Act
    result = collect(tmp_path)

    # Assert
    included = {p.relative_to(tmp_path).as_posix() for p in result}
    assert "lucnhan/tests/test_nested.py" not in included, "nested lucnhan/tests/ leaked into archive"
    assert "lucnhan/docs/NESTED.md" not in included, "nested lucnhan/docs/ leaked into archive"


def test_collect_excludes_dev_assets_and_config(tmp_path: Path) -> None:
    # Arrange
    _seed_fake_repo(tmp_path)

    # Act
    result = collect(tmp_path)

    # Assert
    included = {p.relative_to(tmp_path).as_posix() for p in result}
    assert "assets/icon.png" not in included
    assert "chat_examples/example.md" not in included
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
    # Arrange — HCLI requires flat layout; reject any version-named wrapping subfolder
    _seed_fake_repo(tmp_path)
    files = collect(tmp_path)
    out = tmp_path / "out.zip"

    # Act
    build_zip(files, out, tmp_path)

    # Assert: NO entry is wrapped under a `lucnhan-v<version>/` subfolder.
    # That is the actual HCLI packaging bug (metadata nested one level deep).
    import re

    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    wrapped = [n for n in names if re.match(r"^lucnhan-v\d+\.\d+/[^/]", n)]
    assert not wrapped, f"entries wrapped under a version subfolder: {wrapped}"


def test_build_zip_preserves_file_contents(tmp_path: Path) -> None:
    # Arrange
    (tmp_path / "x.py").write_text("secret content 12345\n", encoding="utf-8", newline="")
    out = tmp_path / "out.zip"

    # Act
    build_zip([tmp_path / "x.py"], out, tmp_path)

    # Assert
    with zipfile.ZipFile(out) as zf:
        assert zf.read("x.py") == b"secret content 12345\n"


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
