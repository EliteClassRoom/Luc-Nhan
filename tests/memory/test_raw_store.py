"""Tests for the JSONL raw store's read path."""

from __future__ import annotations

import json
from pathlib import Path

from lucnhan.memory.paths import KnowledgePaths, derive_binary_id
from lucnhan.memory.raw_store import KnowledgeRawStore
from lucnhan.memory.schema import KnowledgeMemory


def _paths(tmp_path: Path) -> KnowledgePaths:
    return KnowledgePaths(
        idb_path=str(tmp_path / "t.i64"),
        notes_dir=str(tmp_path / "notes"),
        kb_dir=str(tmp_path / "kb"),
        reports_dir=str(tmp_path / "reports"),
        binary_id=derive_binary_id(str(tmp_path / "t.i64")),
    )


def test_a_schema_drifted_row_does_not_kill_the_read(tmp_path: Path) -> None:
    """A record from an older build lacks a field; reads must survive it."""
    paths = _paths(tmp_path)
    paths.ensure()
    store = KnowledgeRawStore(paths)
    store.upsert_memory(KnowledgeMemory(id="mem:ok", binary_id="b", type="general", title="t", content="readable"))

    rows = [json.loads(line) for line in open(paths.memories_path, encoding="utf-8")]
    rows.insert(0, {"id": "mem:old", "binary_id": "b"})  # no title/content/type
    with open(paths.memories_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("".join(json.dumps(r) + "\n" for r in rows))

    assert [m.id for m in store.list_memories()] == ["mem:ok"]
