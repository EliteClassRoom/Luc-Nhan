"""Tests for storage guard: containment, symlink, size checks."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lucnhan.memory.storage_guard import (
    StorageError,
    ensure_private_directory,
    validate_memory_root,
    validate_regular_contained_path,
)


class TestValidateRegularContainedPath:
    def test_valid_contained_file(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        f = root / "file.txt"
        f.write_text("hello")
        result = validate_regular_contained_path(f, root=root)
        assert result == f.resolve()

    def test_traversal_escape_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        candidate = root / ".." / "escape.txt"
        with pytest.raises(StorageError, match="escape"):
            validate_regular_contained_path(candidate, root=root)

    def test_directory_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        subdir = root / "subdir"
        subdir.mkdir()
        with pytest.raises(StorageError, match="regular file"):
            validate_regular_contained_path(subdir, root=root)

    @pytest.mark.skipif(os.name == "nt", reason="symlink behavior differs on Windows")
    def test_symlink_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        target = root / "real.txt"
        target.write_text("data")
        link = root / "link.txt"
        os.symlink(target, link)
        with pytest.raises(StorageError, match="symlink"):
            validate_regular_contained_path(link, root=root)

    def test_must_exist_enforced(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        with pytest.raises(StorageError, match="does not exist"):
            validate_regular_contained_path(root / "missing.txt", root=root, must_exist=True)

    def test_max_size_enforced(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        big = root / "big.txt"
        big.write_text("x" * 1000)
        with pytest.raises(StorageError, match="max size"):
            validate_regular_contained_path(big, root=root, max_size=100)


class TestEnsurePrivateDirectory:
    def test_creates_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "new_dir"
        result = ensure_private_directory(path)
        assert result.exists()
        assert result.is_dir()

    def test_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "existing"
        path.mkdir()
        ensure_private_directory(path)
        assert path.exists()


class TestValidateMemoryRoot:
    def test_creates_missing_root(self, tmp_path: Path) -> None:
        root = tmp_path / "memory_root"
        result = validate_memory_root(root)
        assert result.exists()

    def test_existing_root_accepted(self, tmp_path: Path) -> None:
        root = tmp_path / "existing_root"
        root.mkdir()
        result = validate_memory_root(root)
        assert result == root.resolve()
