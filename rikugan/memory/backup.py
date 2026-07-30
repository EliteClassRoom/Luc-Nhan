"""SQLite backup: collision-resistant snapshot, verification, restore.

Creates an offline copy of a workspace database using SQLite's native
backup mechanism, which produces a consistent point-in-time snapshot
even while the database is in use. Every backup uses an exclusive
``O_CREAT | O_EXCL`` destination path with a nanosecond + counter suffix
to guarantee uniqueness, and exposes ``verify_backup`` as a standalone
post-write integrity gate.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .workspace import WorkspacePaths
from .workspace_store import WorkspaceStore


class BackupVerificationError(RuntimeError):
    """Raised when a backup fails any post-write integrity check."""


@dataclass(frozen=True)
class BackupResult:
    """Result of a backup operation."""

    backup_path: Path
    manifest_hash: str
    db_size: int


def _exclusive_backup_path(backup_dir: Path, owner_memory_id: str) -> Path:
    """Allocate a uniquely-named backup destination.

    Uses ``os.open(..., O_CREAT | O_EXCL | O_WRONLY, 0o600)`` so the file
    is created atomically; ``FileExistsError`` triggers a retry with a
    higher counter suffix. Up to 100 attempts before surfacing the
    exception.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    for attempt in range(100):
        suffix = f"{time.time_ns()}_{attempt}"
        candidate = backup_dir / f"memory_{owner_memory_id[:12]}_{suffix}.db"
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise FileExistsError("could not allocate a unique backup path")


def create_backup(
    paths: WorkspacePaths,
    owner_memory_id: str,
    backup_dir: Path,
) -> BackupResult:
    """Create a SQLite backup of a workspace database.

    Uses ``sqlite3.Connection.backup()`` for a coherent snapshot that
    does not require exclusive access to the source database. The
    destination is allocated via :func:`_exclusive_backup_path` so two
    backups taken within the same nanosecond tick will not collide.

    Parameters
    ----------
    paths:
        Workspace filesystem paths.
    owner_memory_id:
        Owner workspace ID (encoded into the backup filename and later
        verified by :func:`verify_backup`).
    backup_dir:
        Directory to write the backup into. Created if missing.

    Returns the backup file path, SHA-256 manifest hash, and byte size.
    """
    if not paths.database.exists():
        raise FileNotFoundError(f"workspace database not found: {paths.database}")

    backup_path = _exclusive_backup_path(backup_dir, owner_memory_id)

    quoted_source = quote(paths.database.resolve().as_posix(), safe="/:")
    source = sqlite3.connect(f"file:{quoted_source}?mode=ro", uri=True)
    dest = sqlite3.connect(str(backup_path))
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()

    # Compute SHA-256 incrementally while reading; final hex digest is
    # the manifest hash carried alongside the backup file.
    digest = hashlib.sha256()
    size = 0
    with backup_path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    manifest_hash = digest.hexdigest()

    return BackupResult(
        backup_path=backup_path,
        manifest_hash=manifest_hash,
        db_size=size,
    )


def list_backups(backup_dir: Path) -> list[Path]:
    """List all backup files in a directory, sorted newest first."""
    if not backup_dir.exists():
        return []
    backups = sorted(
        backup_dir.glob("memory_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return backups


def verify_backup(result: BackupResult, owner_memory_id: str) -> None:
    """Validate a backup against its manifest hash and owner identity.

    Checks, in order:
      1. file exists and is non-empty
      2. SQLite magic header (first 16 bytes)
      3. full-file SHA-256 matches ``result.manifest_hash``
      4. ``workspace_meta.owner_memory_id`` matches *owner_memory_id*
      5. ``PRAGMA integrity_check`` returns ``"ok"``

    Raises :class:`BackupVerificationError` on any failure.
    """
    path = result.backup_path
    if not path.is_file() or path.stat().st_size <= 0:
        raise BackupVerificationError("backup is missing or empty")
    with path.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            raise BackupVerificationError("backup has an invalid SQLite header")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != result.manifest_hash:
        raise BackupVerificationError("backup hash mismatch")
    quoted = quote(path.as_posix(), safe="/:")
    conn = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True)
    try:
        owner = conn.execute("SELECT value FROM workspace_meta WHERE key = 'owner_memory_id'").fetchone()
        if owner is None or owner[0] != owner_memory_id:
            raise BackupVerificationError("backup owner mismatch")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupVerificationError("backup integrity check failed")
    finally:
        conn.close()


def restore_from_backup(
    backup_path: Path,
    target_paths: WorkspacePaths,
    owner_memory_id: str,
    *,
    migration_backup_dir: Path,
) -> WorkspaceStore:
    """Restore a workspace database from a backup file.

    Copies the backup into *target_paths* and returns the opened store.
    If the target database is currently at schema v1 the final reopen
    goes through :func:`open_workspace_for_write`, which will take a
    fresh v1 backup before letting ``WorkspaceStore.open`` migrate to v2.

    Returns the opened ``WorkspaceStore``.
    """
    if not backup_path.exists():
        raise FileNotFoundError(f"backup not found: {backup_path}")

    # Validate backup is a readable SQLite database with matching owner
    test_conn = sqlite3.connect(str(backup_path))
    try:
        test_conn.execute("SELECT COUNT(*) FROM workspace_meta")
    except sqlite3.OperationalError:
        test_conn.close()
        raise ValueError(f"backup is not a valid workspace database: {backup_path}") from None
    finally:
        test_conn.close()

    # Create target workspace and copy data
    target_store = WorkspaceStore.create(target_paths, owner_memory_id=owner_memory_id)
    target_store.close()

    # Overwrite with backup contents via SQLite backup API
    source = sqlite3.connect(str(backup_path))
    dest = sqlite3.connect(str(target_paths.database))
    try:
        source.backup(dest)
        # Update owner to match the new target
        dest.execute(
            "UPDATE workspace_meta SET value = ? WHERE key = 'owner_memory_id'",
            (owner_memory_id,),
        )
        dest.commit()
    finally:
        dest.close()
        source.close()

    # Reopen via the backup-aware entry point so that any future v1 ->
    # v2 migration step is preceded by a freshly verified backup.
    from .workspace_open import open_workspace_for_write

    return open_workspace_for_write(target_paths, owner_memory_id, migration_backup_dir)
