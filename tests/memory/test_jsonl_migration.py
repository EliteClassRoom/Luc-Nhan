"""Tests for the JSONL -> bundle envelope adapter and temp bundle writer."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from rikugan.memory.jsonl_migration import (
    jsonl_to_bundle_envelopes,
    write_envelopes_to_temp_bundle,
)
from rikugan.memory.paths import KnowledgePaths, derive_binary_id
from rikugan.memory.raw_store import KnowledgeRawStore
from rikugan.memory.schema import KnowledgeEntity, KnowledgeMemory, KnowledgeRelation


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
