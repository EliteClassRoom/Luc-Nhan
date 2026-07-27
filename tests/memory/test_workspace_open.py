"""Tests for backup-aware writable open and offline rollback.

Tasks 5: ensures every production v1 -> v2 migration is preceded by a
verified backup, and offline rollback can revert to v1 without ever
triggering a v2 migration.
"""

from __future__ import annotations

import sqlite3

import pytest

from rikugan.memory.backup import BackupVerificationError
from rikugan.memory.workspace import MemoryLocator, new_memory_id
from rikugan.memory.workspace_open import open_workspace_for_write, restore_v1_backup_offline

from .test_workspace_migration_v2 import _create_v1_database


def test_writable_v1_open_creates_verified_backup_before_migration(tmp_path) -> None:
    owner = new_memory_id()
    locator = MemoryLocator(tmp_path / "memory")
    paths = locator.binary(owner)
    _create_v1_database(paths.database, owner)

    store = open_workspace_for_write(paths, owner, locator.backups(owner))
    backups = list(locator.backups(owner).glob("memory_*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 2
    store.close()


def test_backup_failure_aborts_before_migration(tmp_path, monkeypatch) -> None:
    owner = new_memory_id()
    locator = MemoryLocator(tmp_path / "memory")
    paths = locator.binary(owner)
    _create_v1_database(paths.database, owner)
    monkeypatch.setattr(
        "rikugan.memory.workspace_open.create_backup",
        lambda *a, **k: (_ for _ in ()).throw(BackupVerificationError("boom")),
    )
    with pytest.raises(BackupVerificationError, match="boom"):
        open_workspace_for_write(paths, owner, locator.backups(owner))
    with sqlite3.connect(paths.database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_offline_rollback_preserves_v1_without_reopening_v2(tmp_path) -> None:
    owner = new_memory_id()
    locator = MemoryLocator(tmp_path / "memory")
    paths = locator.binary(owner)
    _create_v1_database(paths.database, owner)
    store = open_workspace_for_write(paths, owner, locator.backups(owner))
    store.close()
    backup = next(locator.backups(owner).glob("memory_*.db"))
    restore_v1_backup_offline(backup, paths, owner)
    with sqlite3.connect(paths.database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
