"""Tests for the v1 -> v2 workspace migration and semantic_hash invariants."""

from __future__ import annotations

import sqlite3

import pytest

from rikugan.memory import workspace_store
from rikugan.memory.fact_identity import semantic_fact_hash
from rikugan.memory.workspace import MemoryLocator, new_memory_id, new_record_id
from rikugan.memory.workspace_store import WorkspaceStore


def _create_v1_database(path, owner: str) -> str:
    # Build a faithful v1 database via the REAL _migrate_v1 instead of
    # hand-declaring DDL, so the fixture cannot drift from the production
    # schema (notably the projection_state columns and relations.object_id
    # nullability). _migrate_v1 creates every v1 table and seeds
    # projection_state(id=1); user_version stays 0 until set below.
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    workspace_store._migrate_v1(conn)
    conn.execute("PRAGMA user_version = 1")
    fact_id = new_record_id("fact")
    conn.execute("INSERT INTO workspace_meta VALUES('owner_memory_id', ?)", (owner,))
    conn.execute("INSERT INTO workspace_meta VALUES('workspace_kind', 'binary')")
    conn.execute("INSERT INTO facts VALUES(?, 'Function  Purpose', 'Parser', 1, 10.0)", (fact_id,))
    conn.execute(
        "INSERT INTO fact_revisions VALUES(?, 1, 'Uses RC4\\r\\n', 'legacy-hash', 0.8, 10.0)",
        (fact_id,),
    )
    conn.commit()
    conn.close()
    return fact_id


def test_v1_workspace_migrates_without_changing_existing_records(tmp_path) -> None:
    owner = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(owner)
    fact_id = _create_v1_database(paths.database, owner)

    store = WorkspaceStore.open(paths, owner_memory_id=owner)
    record = store.get_fact(fact_id)
    assert record is not None
    assert record.semantic_hash == semantic_fact_hash("Function  Purpose", "Uses RC4\\r\\n")
    assert record.revision == 1
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert store._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    store.close()


def test_v2_sql_guards_reject_missing_or_uppercase_hash(tmp_path) -> None:
    owner = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(owner)
    store = WorkspaceStore.create(paths, owner_memory_id=owner)
    with pytest.raises(sqlite3.IntegrityError, match="semantic_hash"):
        store._conn.execute(
            "INSERT INTO facts(fact_id, fact_type, title, semantic_hash, current_revision, created_at) VALUES(?, 'x', 'x', NULL, 1, 1)",
            (new_record_id("fact"),),
        )
    with pytest.raises(sqlite3.IntegrityError, match="semantic_hash"):
        store._conn.execute(
            "INSERT INTO facts(fact_id, fact_type, title, semantic_hash, current_revision, created_at) VALUES(?, 'x', 'x', ?, 1, 1)",
            (new_record_id("fact"), "A" * 64),
        )
    store.close()


def test_v2_migration_failure_rolls_back_schema_and_user_version(tmp_path, monkeypatch) -> None:
    owner = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(owner)
    _create_v1_database(paths.database, owner)

    def fail_after_schema_change(conn) -> None:
        conn.execute("ALTER TABLE facts ADD COLUMN semantic_hash TEXT")
        conn.execute("CREATE INDEX idx_facts_semantic ON facts(semantic_hash)")
        conn.execute("CREATE TRIGGER injected_guard BEFORE INSERT ON facts BEGIN SELECT 1; END")
        raise RuntimeError("injected migration failure")

    monkeypatch.setitem(workspace_store._MIGRATIONS, 2, fail_after_schema_change)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        WorkspaceStore.open(paths, owner_memory_id=owner)
    with sqlite3.connect(paths.database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        columns = {row[1] for row in conn.execute("PRAGMA table_info(facts)")}
        assert "semantic_hash" not in columns
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('idx_facts_semantic', 'injected_guard')"
            ).fetchall()
            == []
        )
