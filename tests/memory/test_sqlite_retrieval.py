"""Tests for the SQLite retrieval adapter and the refactored ranker."""

from __future__ import annotations

from pathlib import Path

from rikugan.memory.repository import SQLiteKnowledgeRepository
from rikugan.memory.retrieve import (
    NoteExcerpt,
    RetrievalPack,
    RetrievalQuery,
    retrieve,
    retrieve_from_records,
)
from rikugan.memory.schema import (
    KnowledgeEntity,
    KnowledgeMemory,
    KnowledgeRelation,
)
from rikugan.memory.sqlite_retrieval import repository_to_retrieval_pack
from rikugan.memory.workspace import MemoryLocator, new_memory_id
from rikugan.memory.workspace_store import WorkspaceStore


def _create_repo(tmp_path: Path):
    owner = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(owner)
    store = WorkspaceStore.create(paths, owner_memory_id=owner)
    repo = SQLiteKnowledgeRepository(store, owner_memory_id=owner)
    return repo, owner, store


def _build_jsonl_store(
    tmp_path: Path,
    memories: list[KnowledgeMemory],
    entities: list[KnowledgeEntity],
    *,
    note_files: list[tuple[str, str]] | None = None,
):
    """Create a JSONL store + write memory/entity records + optional notes."""
    from rikugan.memory.paths import KnowledgePaths, derive_binary_id
    from rikugan.memory.raw_store import KnowledgeRawStore

    jsonl_paths = KnowledgePaths(
        idb_path=str(tmp_path / "test.i64"),
        notes_dir=str(tmp_path / "notes"),
        kb_dir=str(tmp_path / "kb"),
        reports_dir=str(tmp_path / "notes" / "reports"),
        binary_id=derive_binary_id(str(tmp_path / "test.i64")),
    )
    jsonl_paths.ensure()
    store = KnowledgeRawStore(jsonl_paths)
    for m in memories:
        store.upsert_memory(m)
    for e in entities:
        store.upsert_entity(e)
    if note_files:
        for name, body in note_files:
            (Path(jsonl_paths.notes_dir) / name).write_text(body, encoding="utf-8")
    return store, jsonl_paths


def test_retrieve_from_records_matches_retrieve_for_same_input(tmp_path: Path) -> None:
    """Same input records → same RetrievalPack from both entry points."""
    memories = [KnowledgeMemory(id="mem:1", binary_id="b", type="general", title="t", content="main parses config")]
    entities = [KnowledgeEntity(id="func:0x401000", binary_id="b", type="function", name="main", address="0x401000")]
    relations: list[KnowledgeRelation] = []
    notes: list[NoteExcerpt] = []
    query = RetrievalQuery(text="main", function_name="main", address="0x401000")

    pack_from_records = retrieve_from_records(
        memories,
        entities,
        relations,
        notes,
        query,
        max_memories=12,
        max_entities=8,
        max_relations=15,
        max_notes=3,
    )

    store, jsonl_paths = _build_jsonl_store(tmp_path, memories, entities)
    pack_from_store = retrieve(store, jsonl_paths, query)
    assert {m.id for m in pack_from_records.memories} == {m.id for m in pack_from_store.memories}


def test_retrieve_from_records_matches_retrieve_for_notes(tmp_path: Path) -> None:
    """Both entry points produce identical ``pack.notes`` for non-empty notes."""
    memories = [
        KnowledgeMemory(
            id="mem:1",
            binary_id="b",
            type="general",
            title="function_purpose",
            content="main parses config",
            entity_refs=["func:0x401000"],
        ),
    ]
    entities = [KnowledgeEntity(id="func:0x401000", binary_id="b", type="function", name="main", address="0x401000")]
    relations: list[KnowledgeRelation] = []
    notes = [
        NoteExcerpt(title="function_purpose", body="main parses the configuration blob\n"),
        NoteExcerpt(title="unrelated", body="background context with no main hits\n"),
    ]
    query = RetrievalQuery(text="main", function_name="main", address="0x401000")

    pack_from_records = retrieve_from_records(
        memories,
        entities,
        relations,
        notes,
        query,
        max_memories=12,
        max_entities=8,
        max_relations=15,
        max_notes=3,
    )

    note_files = [
        (
            "function_purpose.md",
            "---\ntitle: function_purpose\n---\nmain parses the configuration blob\n",
        ),
        (
            "unrelated.md",
            "---\ntitle: unrelated\n---\nbackground context with no main hits\n",
        ),
    ]
    store, jsonl_paths = _build_jsonl_store(
        tmp_path,
        memories,
        entities,
        note_files=note_files,
    )
    pack_from_store = retrieve(store, jsonl_paths, query)

    assert pack_from_records.notes == pack_from_store.notes
    # The "function_purpose" note must be title-matched and present in both.
    assert any("main parses the configuration blob" in body for body in pack_from_records.notes)


def test_repository_to_retrieval_pack_reads_sqlite(tmp_path: Path) -> None:
    repo, _owner, store = _create_repo(tmp_path)
    repo.save_exploration_finding(
        "function_purpose",
        "main parses config",
        "exploration",
        entity_refs=["func:0x401000"],
        tags=["parser"],
    )
    pack = repository_to_retrieval_pack(
        repo,
        current_address="0x401000",
        current_function="main @ 0x401000",
        active_mode="normal",
        active_goal="",
        budget=None,  # adapter uses sensible defaults
    )
    assert isinstance(pack, RetrievalPack)
    assert any("main" in m.content for m in pack.memories)
    store.close()
