"""Tests for the JSONL -> bundle envelope adapter and temp bundle writer."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from rikugan.memory.jsonl_migration import (
    jsonl_to_bundle_envelopes,
    maybe_import_legacy_jsonl,
    write_envelopes_to_temp_bundle,
)
from rikugan.memory.paths import KnowledgePaths, derive_binary_id
from rikugan.memory.raw_store import KnowledgeRawStore
from rikugan.memory.repository import SQLiteKnowledgeRepository
from rikugan.memory.schema import KnowledgeEntity, KnowledgeMemory, KnowledgeRelation
from rikugan.memory.workspace import MemoryLocator, new_memory_id
from rikugan.memory.workspace_store import WorkspaceStore


def _make_paths(tmp_path: Path) -> KnowledgePaths:
    return KnowledgePaths(
        idb_path=str(tmp_path / "test.i64"),
        notes_dir=str(tmp_path / "notes"),
        kb_dir=str(tmp_path / "kb"),
        reports_dir=str(tmp_path / "notes" / "reports"),
        binary_id=derive_binary_id(str(tmp_path / "test.i64")),
    )


def test_jsonl_to_bundle_envelopes_preserves_fact_fields(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    paths.ensure()
    store = KnowledgeRawStore(paths)
    mem = KnowledgeMemory(
        id="mem:function_purpose:0x401000:abc",
        binary_id=paths.binary_id,
        type="function_purpose",
        title="main parses config",
        content="main parses config at startup",
        entity_refs=["func:0x401000"],
        tags=["parser", "config"],
        confidence=0.8,
    )
    store.upsert_memory(mem)

    envelopes = jsonl_to_bundle_envelopes(store, paths)
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env["record_type"] == "fact"
    assert env["record_id"] == mem.id
    payload = env["payload"]
    assert payload["type"] == "function_purpose"
    assert payload["title"] == "main parses config"
    assert payload["content"] == "main parses config at startup"
    assert payload["confidence"] == 0.8
    assert payload["entity_refs"] == ["func:0x401000"]
    assert payload["tags"] == ["parser", "config"]


def test_jsonl_to_bundle_envelopes_preserves_entity_and_relation(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    paths.ensure()
    store = KnowledgeRawStore(paths)
    ent = KnowledgeEntity(
        id="func:0x401000",
        binary_id=paths.binary_id,
        type="function",
        name="main",
        display_name="main",
        address="0x401000",
        tags=["entry"],
    )
    rel = KnowledgeRelation(
        id="rel:1",
        binary_id=paths.binary_id,
        src="func:0x401000",
        predicate="calls",
        dst="func:0x402000",
        evidence="xref at 0x401020",
    )
    store.upsert_entity(ent)
    store.upsert_relation(rel)

    envelopes = jsonl_to_bundle_envelopes(store, paths)
    types = {e["record_type"] for e in envelopes}
    assert types == {"entity", "relation"}


def test_jsonl_to_bundle_envelopes_empty_store_returns_empty(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    paths.ensure()
    store = KnowledgeRawStore(paths)
    assert jsonl_to_bundle_envelopes(store, paths) == []


def test_write_envelopes_to_temp_bundle_creates_valid_zip(tmp_path: Path) -> None:
    envelopes = [
        {
            "record_type": "fact",
            "record_id": "mem:1",
            "payload": {
                "type": "general",
                "title": "t",
                "content": "c",
                "confidence": 0.5,
            },
        },
    ]
    bundle_path = write_envelopes_to_temp_bundle(envelopes, "mem-abc")
    assert bundle_path.is_file()
    with zipfile.ZipFile(bundle_path, "r") as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["schema_version"] == 1
        assert manifest["origin_memory_id"] == "mem-abc"
        assert any(n.startswith("records/") for n in names)
    bundle_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Auto-import trigger (Task 4)
# ---------------------------------------------------------------------------


def test_maybe_import_skips_when_marker_present(tmp_path: Path) -> None:
    owner = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(owner)
    store = WorkspaceStore.create(paths, owner_memory_id=owner)
    store._conn.execute(
        "INSERT INTO workspace_meta(key, value) VALUES('legacy_jsonl_imported', '2026-01-01T00:00:00Z')"
    )
    store._conn.commit()
    jsonl_paths = _make_paths(tmp_path / "jsonl")
    jsonl_paths.ensure()
    # Add a record — it should NOT be imported because the marker exists.
    raw = KnowledgeRawStore(jsonl_paths)
    raw.upsert_memory(
        KnowledgeMemory(
            id="mem:x",
            binary_id=jsonl_paths.binary_id,
            type="general",
            title="t",
            content="c",
        )
    )
    maybe_import_legacy_jsonl(store, owner, jsonl_paths)
    repo = SQLiteKnowledgeRepository(store, owner_memory_id=owner)
    assert repo.count_observations() == 0
    assert len(repo.list_memories()) == 0
    store.close()


def test_maybe_import_imports_records_once(tmp_path: Path) -> None:
    owner = new_memory_id()
    workspace_paths = MemoryLocator(tmp_path / "ws").binary(owner)
    store = WorkspaceStore.create(workspace_paths, owner_memory_id=owner)

    jsonl_paths = _make_paths(tmp_path / "jsonl")
    jsonl_paths.ensure()
    raw = KnowledgeRawStore(jsonl_paths)
    raw.upsert_memory(
        KnowledgeMemory(
            id="mem:function_purpose:0x401000:abc",
            binary_id=jsonl_paths.binary_id,
            type="function_purpose",
            title="main",
            content="main parses config",
            entity_refs=["func:0x401000"],
            tags=["parser"],
        )
    )

    maybe_import_legacy_jsonl(store, owner, jsonl_paths)
    repo = SQLiteKnowledgeRepository(store, owner_memory_id=owner)
    memories = repo.list_memories()
    assert len(memories) == 1
    assert memories[0].content == "main parses config"

    marker = store._conn.execute("SELECT value FROM workspace_meta WHERE key = 'legacy_jsonl_imported'").fetchone()
    assert marker is not None
    store.close()


def test_maybe_import_does_not_delete_jsonl_files(tmp_path: Path) -> None:
    owner = new_memory_id()
    workspace_paths = MemoryLocator(tmp_path / "ws").binary(owner)
    store = WorkspaceStore.create(workspace_paths, owner_memory_id=owner)

    jsonl_paths = _make_paths(tmp_path / "jsonl")
    jsonl_paths.ensure()
    raw = KnowledgeRawStore(jsonl_paths)
    raw.upsert_memory(
        KnowledgeMemory(
            id="mem:1",
            binary_id=jsonl_paths.binary_id,
            type="general",
            title="t",
            content="c",
        )
    )

    maybe_import_legacy_jsonl(store, owner, jsonl_paths)
    assert Path(jsonl_paths.memories_path).exists()
    store.close()


def test_maybe_import_idempotent_on_second_call(tmp_path: Path) -> None:
    owner = new_memory_id()
    workspace_paths = MemoryLocator(tmp_path / "ws").binary(owner)
    store = WorkspaceStore.create(workspace_paths, owner_memory_id=owner)

    jsonl_paths = _make_paths(tmp_path / "jsonl")
    jsonl_paths.ensure()
    raw = KnowledgeRawStore(jsonl_paths)
    raw.upsert_memory(
        KnowledgeMemory(
            id="mem:1",
            binary_id=jsonl_paths.binary_id,
            type="general",
            title="t",
            content="c",
        )
    )

    maybe_import_legacy_jsonl(store, owner, jsonl_paths)
    maybe_import_legacy_jsonl(store, owner, jsonl_paths)
    repo = SQLiteKnowledgeRepository(store, owner_memory_id=owner)
    assert len(repo.list_memories()) == 1
    store.close()


def test_maybe_import_failed_leaves_marker_unset(tmp_path: Path, monkeypatch) -> None:
    owner = new_memory_id()
    workspace_paths = MemoryLocator(tmp_path / "ws").binary(owner)
    store = WorkspaceStore.create(workspace_paths, owner_memory_id=owner)

    jsonl_paths = _make_paths(tmp_path / "jsonl")
    jsonl_paths.ensure()
    raw = KnowledgeRawStore(jsonl_paths)
    raw.upsert_memory(
        KnowledgeMemory(
            id="mem:1",
            binary_id=jsonl_paths.binary_id,
            type="general",
            title="t",
            content="c",
        )
    )

    def boom(*a, **k):
        raise RuntimeError("import crash")

    monkeypatch.setattr("rikugan.memory.jsonl_migration.import_workspace_bundle", boom)

    with pytest.raises(RuntimeError, match="import crash"):
        maybe_import_legacy_jsonl(store, owner, jsonl_paths)
    marker = store._conn.execute("SELECT value FROM workspace_meta WHERE key = 'legacy_jsonl_imported'").fetchone()
    assert marker is None
    store.close()
