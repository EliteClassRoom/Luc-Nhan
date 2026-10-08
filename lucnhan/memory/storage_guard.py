"""Storage guard: local FS, containment, permissions, symlink, size, and regular-file checks.

Centralizes path-safety validation for all central memory file operations.
Every file write passes through ``validate_regular_contained_path()`` before
touching disk.
"""

from __future__ import annotations

import os
from pathlib import Path


class StorageError(RuntimeError):
    """Raised when a storage path is unsafe or unsupported."""


def validate_regular_contained_path(
    candidate: Path,
    *,
    root: Path,
    must_exist: bool = False,
    max_size: int | None = None,
) -> Path:
    """Validate that *candidate* is a regular file inside *root*.

    Checks:
    * Resolved path stays within *root* (no traversal escape).
    * Path is not a symlink or reparse point.
    * If it exists, it is a regular file.
    * If *must_exist*, raises if the file is absent.
    * If *max_size*, raises if the file exceeds the limit.

    Returns the resolved path on success.
    """
    root_real = root.resolve()
    resolved = candidate.resolve()

    # Containment check
    try:
        common = os.path.commonpath((str(root_real), str(resolved)))
    except ValueError as exc:
        raise StorageError(f"path {candidate} is not within {root}") from exc

    if common != str(root_real):
        raise StorageError(f"path {candidate} escapes root {root}")

    # Symlink / reparse point check
    if candidate.is_symlink():
        raise StorageError(f"refusing symlink: {candidate}")

    # Existence + regular file check
    if resolved.exists():
        if not resolved.is_file():
            raise StorageError(f"not a regular file: {candidate}")
        stat = resolved.stat()
        if max_size is not None and stat.st_size > max_size:
            raise StorageError(f"file {candidate} exceeds max size {max_size}: {stat.st_size}")
    elif must_exist:
        raise StorageError(f"required file does not exist: {candidate}")

    return resolved


def ensure_private_directory(path: Path) -> Path:
    """Create *path* with owner-only permissions where supported (POSIX 0o700)."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass  # Best-effort on read-only filesystems
    return path


def validate_memory_root(root: Path) -> Path:
    """Validate the central memory root directory for safe operations."""
    if not root.exists():
        return ensure_private_directory(root)
    if not root.is_dir():
        raise StorageError(f"memory root is not a directory: {root}")
    if root.is_symlink():
        raise StorageError(f"refusing symlink memory root: {root}")
    return root
