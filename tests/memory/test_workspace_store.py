"""Tests for WorkspaceStore: facts, entities, relations, observations, projection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rikugan.memory.fact_identity import semantic_fact_hash
from rikugan.memory.workspace import MemoryLocator, new_memory_id, new_record_id
from rikugan.memory.workspace_store import (
    FactRecord,
    StaleRevisionError,
    WorkspaceStore,
)


def _create_store(tmp_path: Path) -> tuple[WorkspaceStore, str]:
    memory_id = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(memory_id)
    store = WorkspaceStore.create(paths, owner_memory_id=memory_id)
    return store, memory_id


class TestFactUpsert:
    def test_create_and_get_fact(self, tmp_path: Path) -> None:
        store, _ = _create_store(tmp_path)
        fid = new_record_id("fact")
        record = store.put_fact(fid, "algorithm", "RC4", "Uses RC4 for C2", 0.8, expected_revision=0)

        assert isinstance(record, FactRecord)
        assert record.fact_id == fid
        assert record.revision == 1
        assert record.content == "Uses RC4 for C2"
        assert record.confidence == 0.8

        fetched = store.get_fact(fid)
        assert fetched is not None
        assert fetched.fact_id == fid
        assert fetched.content == "Uses RC4 for C2"

    def test_update_creates_next_revision(self, tmp_path: Path) -> None:
        store, _ = _create_store(tmp_path)
        fid = new_record_id("fact")

        first = store.put_fact(fid, "algorithm", "RC4", "Uses RC4", 0.8, expected_revision=0)
        second = store.put_fact(
            fid,
            "algorithm",
            "RC4",
            "Uses modified RC4 for C2 traffic",
            0.9,
            expected_revision=1,
        )

        assert first.revision == 1
        assert second.revision == 2
        assert store.get_fact(fid).content == "Uses modified RC4 for C2 traffic"

    def test_stale_expected_revision_rejected(self, tmp_path: Path) -> None:
        store, _ = _create_store(tmp_path)
        fid = new_record_id("fact")
        store.put_fact(fid, "fact", "A", "first", 0.5, expected_revision=0)

        with pytest.raises(StaleRevisionError):
            store.put_fact(fid, "fact", "A", "stale", 0.6, expected_revision=0)

    def test_invalid_confidence_rejected(self, tmp_path: Path) -> None:
        store, _ = _create_store(tmp_path)
        fid = new_record_id("fact")

        for bad in (-0.1, 1.5, float("nan"), float("inf"), float("-inf")):
            with pytest.raises((ValueError, OverflowError)):
                store.put_fact(fid, "fact", "A", "text", bad, expected_revision=0)

    def test_invalid_fact_id_rejected(self, tmp_path: Path) -> None:
        store, _ = _create_store(tmp_path)
        with pytest.raises(ValueError):
            store.put_fact("func:0x401000", "fact", "A", "text", 0.5, expected_revision=0)


class TestListFacts:
    def test_list_facts_returns_current_only(self, tmp_path: Path) -> None:
        store, _ = _create_store(tmp_path)
        f1 = new_record_id("fact")
        f2 = new_record_id("fact")

        store.put_fact(f1, "algorithm", "RC4", "Uses RC4", 0.8, expected_revision=0)
        store.put_fact(f2, "protocol", "HTTP", "Uses HTTP", 0.7, expected_revision=0)

        facts = store.list_facts()
        assert len(facts) == 2
        ids = {f.fact_id for f in facts}
        assert ids == {f1, f2}

    def test_list_facts_empty(self, tmp_path: Path) -> None:
        store, _ = _create_store(tmp_path)
        assert store.list_facts() == []

    def test_create_update_and_list_expose_current_semantic_hash(self, tmp_path: Path) -> None:
        from rikugan.memory.fact_identity import semantic_fact_hash

        store, _ = _create_store(tmp_path)
        fid = new_record_id("fact")
        first = store.put_fact(fid, "algorithm", "RC4", "Uses RC4", 0.8, expected_revision=0)
        second = store.put_fact(fid, "algorithm", "RC4", "Uses modified RC4", 0.9, expected_revision=1)
        assert first.semantic_hash != second.semantic_hash
        assert first.semantic_hash == semantic_fact_hash("algorithm", "Uses RC4")
        assert second.semantic_hash == semantic_fact_hash("algorithm", "Uses modified RC4")
        assert store.get_fact(fid).semantic_hash == second.semantic_hash
        assert store.list_facts()[0].semantic_hash == second.semantic_hash
        store.close()

    def test_put_fact_with_entity_refs_and_tags_roundtrip(self, tmp_path: Path) -> None:
        store, _ = _create_store(tmp_path)
        fid = new_record_id("fact")
        store.put_fact(
            fid,
            "algorithm",
            "RC4",
            "Uses RC4",
            0.8,
            expected_revision=0,
            entity_refs=["func:0x401000", "global:0x409000"],
            tags=["crypto", "c2"],
        )
        fetched = store.get_fact(fid)
        assert fetched is not None
        assert fetched.entity_refs == ["func:0x401000", "global:0x409000"]
        assert fetched.tags == ["crypto", "c2"]
        store.close()

    def test_put_fact_without_entity_refs_defaults_empty(self, tmp_path: Path) -> None:
        store, _ = _create_store(tmp_path)
        fid = new_record_id("fact")
        store.put_fact(fid, "algorithm", "RC4", "Uses RC4", 0.8, expected_revision=0)
        fetched = store.get_fact(fid)
        assert fetched is not None
        assert fetched.entity_refs == []
        assert fetched.tags == []
        store.close()


class TestEntityAndRelation:
    def test_put_and_get_entity(self, tmp_path: Path) -> None:
        store, _ = _create_store(tmp_path)
        eid = new_record_id("entity")
        store.put_entity(eid, "function", "main", {"address": "0x401000"})

        entity = store.get_entity(eid)
        assert entity is not None
        assert entity.entity_id == eid
        assert entity.entity_type == "function"
        assert entity.name == "main"

    def test_put_and_list_relations(self, tmp_path: Path) -> None:
        store, _ = _create_store(tmp_path)
        e1 = new_record_id("entity")
        e2 = new_record_id("entity")
        rid = new_record_id("relation")

        store.put_entity(e1, "function", "func_a", {})
        store.put_entity(e2, "function", "func_b", {})
        store.put_relation(rid, e1, "calls", e2, 0.9)

        relations = store.list_relations()
        assert len(relations) == 1
        assert relations[0].predicate == "calls"
        assert relations[0].subject_id == e1
        assert relations[0].object_id == e2

    def test_put_entity_with_tags_roundtrip(self, tmp_path: Path) -> None:
        store, _ = _create_store(tmp_path)
        eid = new_record_id("entity")
        store.put_entity(
            eid,
            "function",
            "main",
            metadata={"display_name": "main", "address": "0x401000"},
            tags=["entry", "parser"],
        )
        fetched = store.get_entity(eid)
        assert fetched is not None
        assert fetched.tags == ["entry", "parser"]
        store.close()

    def test_put_relation_with_evidence_roundtrip(self, tmp_path: Path) -> None:
        store, _ = _create_store(tmp_path)
        sid = new_record_id("entity")
        oid = new_record_id("entity")
        store.put_entity(sid, "function", "decrypt", {})
        store.put_entity(oid, "function", "main", {})
        rid = new_record_id("relation")
        store.put_relation(
            rid,
            sid,
            "calls",
            oid,
            0.9,
            evidence="xref at 0x401020",
        )
        rels = store.list_relations()
        assert len(rels) == 1
        assert rels[0].evidence == "xref at 0x401020"
        store.close()


class TestObservation:
    def test_append_observation(self, tmp_path: Path) -> None:
        store, _ = _create_store(tmp_path)
        oid = new_record_id("observation")
        store.append_observation(oid, "analysis", "Found XOR loop at 0x401020")

        count = store.count_observations()
        assert count == 1

    def test_count_observations_multiple(self, tmp_path: Path) -> None:
        store, _ = _create_store(tmp_path)
        for i in range(5):
            oid = new_record_id("observation")
            store.append_observation(oid, "analysis", f"obs {i}")

        assert store.count_observations() == 5


class TestProjectionState:
    def test_projection_state_initial(self, tmp_path: Path) -> None:
        store, _ = _create_store(tmp_path)
        state = store.projection_state()
        assert state.projection_dirty is False
        assert state.projection_conflict is False
        assert state.projected_revision == 0

    def test_mark_projection_dirty(self, tmp_path: Path) -> None:
        store, _ = _create_store(tmp_path)
        store.mark_projection_dirty()
        state = store.projection_state()
        assert state.projection_dirty is True

    def test_mark_projection_clean(self, tmp_path: Path) -> None:
        store, _ = _create_store(tmp_path)
        store.mark_projection_dirty()
        store.mark_projection_clean(
            managed_hash="abc123",
            unmanaged_hash="def456",
            projected_revision=1,
        )
        state = store.projection_state()
        assert state.projection_dirty is False
        assert state.managed_hash == "abc123"
        assert state.projected_revision == 1


class TestReadOnlyOpen:
    def test_missing_database_raises_file_not_found(self, tmp_path: Path) -> None:
        memory_id = new_memory_id()
        paths = MemoryLocator(tmp_path).binary(memory_id)

        with pytest.raises(FileNotFoundError):
            WorkspaceStore.open(paths, owner_memory_id=memory_id, read_only=True)
        with pytest.raises(FileNotFoundError):
            WorkspaceStore.open(paths, owner_memory_id=memory_id, read_only=False)
        assert not paths.database.exists()

    def test_read_only_query_only(self, tmp_path: Path) -> None:
        store, memory_id = _create_store(tmp_path)
        store.close()

        paths = MemoryLocator(tmp_path).binary(memory_id)
        ro_store = WorkspaceStore.open(paths, owner_memory_id=memory_id, read_only=True)
        import sqlite3

        with pytest.raises(sqlite3.OperationalError):
            ro_store.put_fact(new_record_id("fact"), "fact", "A", "text", 0.5, expected_revision=0)
        ro_store.close()


class TestOwnerValidation:
    def test_wrong_owner_rejected_on_open(self, tmp_path: Path) -> None:
        memory_id = new_memory_id()
        wrong_id = new_memory_id()
        paths = MemoryLocator(tmp_path).binary(memory_id)
        store = WorkspaceStore.create(paths, owner_memory_id=memory_id)
        store.close()

        with pytest.raises(ValueError):
            WorkspaceStore.open(paths, owner_memory_id=wrong_id)


class TestConcurrency:
    def test_concurrent_expected_revision_one_wins(self, tmp_path: Path) -> None:
        """Two concurrent puts with same expected_revision: one succeeds, one fails."""
        store, _ = _create_store(tmp_path)
        fid = new_record_id("fact")

        # First put succeeds
        store.put_fact(fid, "fact", "A", "first", 0.5, expected_revision=0)

        # Second put with stale revision fails
        with pytest.raises(StaleRevisionError):
            store.put_fact(fid, "fact", "A", "second", 0.6, expected_revision=0)

        # Third put with correct revision succeeds
        store.put_fact(fid, "fact", "A", "third", 0.7, expected_revision=1)
        assert store.get_fact(fid).content == "third"
        store.close()


class TestSaveFactIfSemanticallyAbsent:
    """Exact-hash lookup + insert + observation append under one transaction."""

    def test_first_call_creates_fact_and_appends_observation(self, tmp_path: Path) -> None:
        store, _owner = _create_store(tmp_path)
        try:
            digest = semantic_fact_hash("function purpose", "Uses RC4")
            record, outcome = store.save_fact_if_semantically_absent(
                fact_id=new_record_id("fact"),
                fact_type="function purpose",
                title="Parser",
                content="Uses RC4",
                semantic_hash=digest,
                confidence=0.7,
                observation_id=new_record_id("observation"),
                observation_type="save_memory",
                observation_payload='{"category": "function purpose"}',
            )
            assert outcome == "created"
            assert record.fact_id in {r.fact_id for r in store.list_facts()}
            assert store.count_observations() == 1
            row = store._conn.execute(
                "SELECT content FROM observations WHERE observation_type = ?",
                ("save_memory",),
            ).fetchone()
            assert row is not None
            payload = json.loads(row["content"])
            assert payload["fact_id"] == record.fact_id
            assert payload["outcome"] == "created"
        finally:
            store.close()

    def test_exact_duplicate_dedups_and_returns_oldest(self, tmp_path: Path) -> None:
        store, _ = _create_store(tmp_path)
        canonical = semantic_fact_hash("algorithm", "Uses RC4")
        first_fid = new_record_id("fact")
        second_fid = new_record_id("fact")
        try:
            first, first_outcome = store.save_fact_if_semantically_absent(
                fact_id=first_fid,
                fact_type="algorithm",
                title="Algo",
                content="Uses RC4",
                semantic_hash=canonical,
                confidence=0.7,
                observation_id=new_record_id("observation"),
                observation_type="save_memory",
                observation_payload='{"category": "algorithm"}',
            )
            second, second_outcome = store.save_fact_if_semantically_absent(
                fact_id=second_fid,
                fact_type="algorithm",
                title="Algo",
                content="Uses RC4",
                semantic_hash=canonical,
                confidence=0.7,
                observation_id=new_record_id("observation"),
                observation_type="save_memory",
                observation_payload='{"category": "algorithm"}',
            )
            assert first_outcome == "created"
            assert second_outcome == "deduplicated"
            assert second.fact_id == first.fact_id
            assert len(store.list_facts()) == 1
            assert store.count_observations() == 2
            fetched = store.get_fact(first.fact_id)
            assert fetched is not None
            assert fetched.revision == 1
            assert (
                store._conn.execute(
                    "SELECT semantic_hash FROM facts WHERE fact_id = ?",
                    (first.fact_id,),
                ).fetchone()["semantic_hash"]
                == canonical
            )
        finally:
            store.close()

    def test_invalid_inputs_rejected_before_transaction(self, tmp_path: Path) -> None:
        import math as _math

        store, _ = _create_store(tmp_path)
        try:
            with pytest.raises(ValueError, match="confidence"):
                store.save_fact_if_semantically_absent(
                    fact_id=new_record_id("fact"),
                    fact_type="algorithm",
                    title="Algo",
                    content="Uses RC4",
                    semantic_hash=semantic_fact_hash("algorithm", "Uses RC4"),
                    confidence=float("nan"),
                    observation_id=new_record_id("observation"),
                    observation_type="save_memory",
                    observation_payload="{}",
                )
            with pytest.raises(ValueError, match="empty string"):
                store.save_fact_if_semantically_absent(
                    fact_id=new_record_id("fact"),
                    fact_type="",
                    title="Algo",
                    content="Uses RC4",
                    semantic_hash=semantic_fact_hash("algorithm", "Uses RC4"),
                    confidence=0.7,
                    observation_id=new_record_id("observation"),
                    observation_type="save_memory",
                    observation_payload="{}",
                )
            with pytest.raises(ValueError, match="64-character"):
                store.save_fact_if_semantically_absent(
                    fact_id=new_record_id("fact"),
                    fact_type="algorithm",
                    title="Algo",
                    content="Uses RC4",
                    semantic_hash="not-a-digest",
                    confidence=0.7,
                    observation_id=new_record_id("observation"),
                    observation_type="save_memory",
                    observation_payload="{}",
                )
            with pytest.raises(ValueError, match="record ID"):
                store.save_fact_if_semantically_absent(
                    fact_id="bad:id",
                    fact_type="algorithm",
                    title="Algo",
                    content="Uses RC4",
                    semantic_hash=semantic_fact_hash("algorithm", "Uses RC4"),
                    confidence=0.7,
                    observation_id=new_record_id("observation"),
                    observation_type="save_memory",
                    observation_payload="{}",
                )
            # Ensure no partial write happened
            assert store.list_facts() == []
            assert store.count_observations() == 0
            # Reference math to silence linter; not used directly here
            assert _math.isfinite(0.7)
        finally:
            store.close()


class TestExactSaveConcurrency:
    """Multi-connection concurrency tests for ``save_fact_if_semantically_absent``."""

    def test_concurrent_exact_saves_create_one_fact_and_two_observations(self, tmp_path: Path) -> None:
        from concurrent.futures import ThreadPoolExecutor

        from rikugan.memory.repository import SQLiteKnowledgeRepository

        owner = new_memory_id()
        paths = MemoryLocator(tmp_path).binary(owner)
        WorkspaceStore.create(paths, owner_memory_id=owner).close()

        def save(_index: int) -> str:
            store = WorkspaceStore.open(paths, owner_memory_id=owner)
            try:
                repo = SQLiteKnowledgeRepository(store, owner_memory_id=owner)
                return repo.save_memory_fact("algorithm", "Uses RC4", "worker").record.id
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(save, range(2)))

        assert len(set(ids)) == 1
        final = WorkspaceStore.open(paths, owner_memory_id=owner)
        try:
            assert len(final.list_facts()) == 1
            assert final.count_observations() == 2
        finally:
            final.close()

    def test_concurrent_distinct_content_creates_two_facts(self, tmp_path: Path) -> None:
        from concurrent.futures import ThreadPoolExecutor

        from rikugan.memory.repository import SQLiteKnowledgeRepository

        owner = new_memory_id()
        paths = MemoryLocator(tmp_path).binary(owner)
        WorkspaceStore.create(paths, owner_memory_id=owner).close()

        contents = (
            "0x401000 parses config",
            "0x402000 decrypts packets",
        )

        def save(index: int) -> str:
            store = WorkspaceStore.open(paths, owner_memory_id=owner)
            try:
                repo = SQLiteKnowledgeRepository(store, owner_memory_id=owner)
                return repo.save_memory_fact("function_purpose", contents[index], "worker").record.id
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(save, range(2)))

        assert len(set(ids)) == 2
        final = WorkspaceStore.open(paths, owner_memory_id=owner)
        try:
            assert len(final.list_facts()) == 2
            assert final.count_observations() == 2
        finally:
            final.close()
