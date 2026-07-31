"""Backup-aware writable open and offline rollback.

The migration gate inside ``WorkspaceStore.open`` runs schema v1 -> v2
in-place. If it ever fails on a production database the original v1
content is lost. This module intermediates every production writable
open so that a v1 database is first snapshotted into a verified
backup. ``restore_v1_backup_offline`` is the matching tool for
returning a v2-migrated database back to its v1 state without ever
calling ``WorkspaceStore.open`` (which would re-trigger migration).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from pathlib import Path
from urllib.parse import quote

from .backup import (
    BackupResult,
    create_backup,
    verify_backup,
)
from .workspace import WorkspacePaths
from .workspace_store import WorkspaceStore


def inspect_workspace_version(paths: WorkspacePaths) -> int:
    """Return ``PRAGMA user_version`` for the database at *paths*.

    Opens the database in read-only mode without going through
    ``WorkspaceStore.open`` (which would trigger migrations). Raises
    ``FileNotFoundError`` if the database does not exist.
    """
    if not paths.database.is_file():
        raise FileNotFoundError(f"workspace database not found: {paths.database}")
    uri = quote(paths.database.resolve().as_posix(), safe="/:")
    conn = sqlite3.connect(f"file:{uri}?mode=ro", uri=True)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def open_workspace_for_write(
    paths: WorkspacePaths,
    owner_memory_id: str,
    backup_dir: Path,
) -> WorkspaceStore:
    """Open *paths* for writing, backing up a v1 database first.

    v1 databases are snapshotted into *backup_dir* and the snapshot is
    verified before :meth:`WorkspaceStore.open` runs the migration to
    v2. v2 (or higher) databases pass through directly — they have
    already been migrated. Any verification failure aborts the open
    so the caller never touches a partially-migrated database without
    a working rollback point.
    """
    version = inspect_workspace_version(paths)
    if version == 1:
        result = create_backup(paths, owner_memory_id, backup_dir)
        verify_backup(result, owner_memory_id)
    return WorkspaceStore.open(paths, owner_memory_id=owner_memory_id)


def _copy_to_sibling_then_replace(source: Path, target: Path) -> None:
    """Copy *source* over *target* via a sibling temp file + fsync + replace.

    The temp file lives next to *target* so ``os.replace`` stays on the
    same filesystem and therefore atomic. Any leftover temp file is
    removed in ``finally``. ``fsync`` is performed on a freshly opened
    writable handle — required on Windows where Python file objects
    close their underlying fd on context exit.
    """
    temp_path = target.with_name(target.name + ".rollback.tmp")
    fd: int | None = None
    try:
        fd = os.open(temp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as dst:
            fd = None
            with source.open("rb") as src:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
        sync_fd = os.open(temp_path, os.O_WRONLY)
        try:
            os.fsync(sync_fd)
        finally:
            os.close(sync_fd)
        os.replace(temp_path, target)
    finally:
        if fd is not None:
            os.close(fd)
        if temp_path.exists():
            try:
                os.remove(temp_path)
            except OSError:
                pass


def restore_v1_backup_offline(
    backup_path: Path,
    target_paths: WorkspacePaths,
    owner_memory_id: str,
) -> None:
    """Replace *target_paths* database with *backup_path*.

    This is the offline counterpart to a successful v1 -> v2 migration
    and is the *only* supported way to roll a workspace back to its
    v1 schema without re-entering ``WorkspaceStore.open``. The
    function verifies the backup (using :func:`verify_backup` with a
    freshly computed hash), copies it onto the target, then re-opens
    the target via a raw ``sqlite3.connect(mode=ro)`` to assert the
    schema user_version is 1 and the owner metadata matches.

    Raises :class:`BackupVerificationError` if the backup fails any
    verification step.
    """
    if not backup_path.is_file():
        raise FileNotFoundError(f"backup not found: {backup_path}")
    digest = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    size = backup_path.stat().st_size
    verify_backup(
        BackupResult(backup_path=backup_path, manifest_hash=digest, db_size=size),
        owner_memory_id,
    )
    _copy_to_sibling_then_replace(backup_path, target_paths.database)
    # Open the restored target in raw read-only mode to confirm v1
    # landed. WorkspaceStore.open() would trigger migration, which is
    # exactly what we are avoiding here.
    if not target_paths.database.is_file():
        raise FileNotFoundError(target_paths.database)
    uri = quote(target_paths.database.resolve().as_posix(), safe="/:")
    conn = sqlite3.connect(f"file:{uri}?mode=ro", uri=True)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != 1:
            raise RuntimeError(f"offline rollback produced unexpected user_version {version}, expected 1")
        row = conn.execute("SELECT value FROM workspace_meta WHERE key = 'owner_memory_id'").fetchone()
        if row is None or row[0] != owner_memory_id:
            raise RuntimeError("offline rollback produced wrong owner_memory_id")
    finally:
        conn.close()
