"""Tests for the v2 -> v3 workspace migration and graph metadata invariants."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rikugan.memory import workspace_store
from rikugan.memory.workspace import MemoryLocator, new_memory_id, new_record_id
from rikugan.memory.workspace_store import WorkspaceStore


def _create_v2_database(path, owner: str) -> str:
    """Build a faithful v2 database via the real _migrate_v1 + _migrate_v2."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    workspace_store._migrate_v1(conn)
    workspace_store._migrate_v2(conn)
    conn.execute("PRAGMA user_version = 2")
    fact_id = new_record_id("fact")
    conn.execute("INSERT INTO workspace_meta VALUES('owner_memory_id', ?)", (owner,))
    conn.execute("INSERT INTO workspace_meta VALUES('workspace_kind', 'binary')")
    conn.execute(
        "INSERT INTO facts(fact_id, fact_type, title, semantic_hash, current_revision, created_at)"
        " VALUES(?, ?, ?, ?, 1, 10.0)",
        (fact_id, "algorithm", "RC4", "a" * 64),
    )
    conn.execute(
        "INSERT INTO fact_revisions(fact_id, revision, content, content_hash, confidence, created_at)"
        " VALUES(?, 1, ?, 'legacy-hash', 0.8, 10.0)",
        (fact_id, "Uses RC4"),
    )
    conn.commit()
    conn.close()
    return fact_id


def test_v2_workspace_migrates_to_v3_with_default_empty_columns(tmp_path) -> None:
    owner = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(owner)
    fact_id = _create_v2_database(paths.database, owner)

    store = WorkspaceStore.open(paths, owner_memory_id=owner)
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 3

    columns = {row[1] for row in store._conn.execute("PRAGMA table_info(facts)")}
    assert {"entity_refs", "tags"}.issubset(columns)

    record = store.get_fact(fact_id)
    assert record is not None
    assert record.entity_refs == []
    assert record.tags == []
    store.close()


def test_v3_migration_failure_rolls_back(tmp_path, monkeypatch) -> None:
    owner = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(owner)
    _create_v2_database(paths.database, owner)

    def fail_after_alter(conn) -> None:
        conn.execute("ALTER TABLE facts ADD COLUMN entity_refs TEXT")
        raise RuntimeError("injected migration failure")

    monkeypatch.setitem(workspace_store._MIGRATIONS, 3, fail_after_alter)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        WorkspaceStore.open(paths, owner_memory_id=owner)
    with sqlite3.connect(paths.database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = {row[1] for row in conn.execute("PRAGMA table_info(facts)")}
        assert "entity_refs" not in columns


def test_v3_not_null_constraints_reject_missing_values(tmp_path: Path) -> None:
    owner = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(owner)
    store = WorkspaceStore.create(paths, owner_memory_id=owner)
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO facts(fact_id, fact_type, title, semantic_hash, entity_refs, tags,"
            " current_revision, created_at)"
            " VALUES(?, 'x', 'x', ?, NULL, NULL, 1, 1)",
            (new_record_id("fact"), "a" * 64),
        )
    store.close()
