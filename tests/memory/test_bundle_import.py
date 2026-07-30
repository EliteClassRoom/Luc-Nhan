"""Tests for bundle importer: round-trip export→import, idempotency, remap."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rikugan.memory.bundle_export import export_workspace
from rikugan.memory.bundle_import import (
    BundleImportConflictError,
    import_workspace_bundle,
)
from rikugan.memory.repository import SQLiteKnowledgeRepository
from rikugan.memory.schema import (
    KnowledgeEntity,
    KnowledgeMemory,
    KnowledgeRelation,
)
from rikugan.memory.workspace import MemoryLocator, new_memory_id, new_record_id
from rikugan.memory.workspace_store import WorkspaceStore


def _seed_and_export(tmp_path: Path) -> Path:
    """Create a workspace with facts, entities, and a relation, export it, return bundle path."""
    memory_id = new_memory_id()
    locator = MemoryLocator(tmp_path / "memory")
    paths = locator.binary(memory_id)
    store = WorkspaceStore.create(paths, owner_memory_id=memory_id)
    repo = SQLiteKnowledgeRepository(store, owner_memory_id=memory_id)

    fact_id = new_record_id("fact")
    repo.upsert_memory(
        KnowledgeMemory(
            id=fact_id,
            binary_id=memory_id,
            type="algorithm",
            title="RC4",
            content="Uses RC4",
            confidence=0.8,
        )
    )
    entity_id = new_record_id("entity")
    repo.upsert_entity(
        KnowledgeEntity(
            id=entity_id,
            binary_id=memory_id,
            type="function",
            name="main",
            display_name="",
            address="0x401000",
        )
    )
    # Add a second entity and a relation so the re-import exercises relation
    # endpoint remap (which the old test omitted).
    target_entity_id = new_record_id("entity")
    repo.upsert_entity(
        KnowledgeEntity(
            id=target_entity_id,
            binary_id=memory_id,
            type="function",
            name="rc4_init",
            display_name="",
            address="0x401040",
        )
    )
    repo.upsert_relation(
        KnowledgeRelation(
            id=new_record_id("relation"),
            binary_id=memory_id,
            src=entity_id,
            predicate="calls",
            dst=target_entity_id,
            confidence=0.9,
        )
    )
    bundle_path = tmp_path / "bundle.zip"
    export_workspace(paths, repo, bundle_path)
    store.close()
    return bundle_path


def _open_target(tmp_path: Path, suffix: str = "target") -> tuple[WorkspaceStore, SQLiteKnowledgeRepository, str]:
    target_mid = new_memory_id()
    locator = MemoryLocator(tmp_path / suffix)
    paths = locator.binary(target_mid)
    store = WorkspaceStore.create(paths, owner_memory_id=target_mid)
    repo = SQLiteKnowledgeRepository(store, owner_memory_id=target_mid)
    return store, repo, target_mid


class TestBundleImport:
    def test_import_round_trip(self, tmp_path: Path) -> None:
        bundle = _seed_and_export(tmp_path)

        # Create a fresh target workspace
        target_store, target_repo, _ = _open_target(tmp_path, "memory2")

        result = import_workspace_bundle(bundle, target_repo)
        assert result.imported_count > 0

        # Verify facts were imported
        facts = target_repo.list_memories()
        assert len(facts) >= 1
        assert any("RC4" in f.content for f in facts)

        # Verify entities
        entities = target_repo.list_entities()
        assert len(entities) >= 2

        # Verify relations
        relations = target_repo.list_relations()
        assert len(relations) == 1
        target_store.close()

    def test_import_is_record_count_idempotent_and_target_scoped(self, tmp_path: Path) -> None:
        """Re-importing the same bundle produces zero new records and identical IDs."""
        bundle = _seed_and_export(tmp_path)
        target_store, target_repo, _target_mid = _open_target(tmp_path, "memory3")

        first = import_workspace_bundle(bundle, target_repo)
        first_ids = {
            "facts": {m.id for m in target_repo.list_memories()},
            "entities": {e.id for e in target_repo.list_entities()},
            "relations": {r.id for r in target_repo.list_relations()},
        }
        first_counts = {key: len(value) for key, value in first_ids.items()}
        first_observation_count = target_repo.count_observations()
        first_fact_payloads = {
            fact_id: next(m.content for m in target_repo.list_memories() if m.id == fact_id)
            for fact_id in first_ids["facts"]
        }

        second = import_workspace_bundle(bundle, target_repo)
        assert second.import_id == first.import_id
        assert second.imported_count == 0

        # IDs are stable across replay.
        assert {m.id for m in target_repo.list_memories()} == first_ids["facts"]
        assert {e.id for e in target_repo.list_entities()} == first_ids["entities"]
        assert {r.id for r in target_repo.list_relations()} == first_ids["relations"]

        # Counts are stable across replay (no extra revisions / observations).
        assert {
            "facts": len(target_repo.list_memories()),
            "entities": len(target_repo.list_entities()),
            "relations": len(target_repo.list_relations()),
        } == first_counts
        assert target_repo.count_observations() == first_observation_count

        # Payload content is preserved (we did not write a new revision on replay).
        for fact_id, content in first_fact_payloads.items():
            still_present = next((m for m in target_repo.list_memories() if m.id == fact_id), None)
            assert still_present is not None
            assert still_present.content == content
        target_store.close()

    def test_import_target_scoped_ids_differ_between_targets(self, tmp_path: Path) -> None:
        """Same bundle imported into two distinct targets yields different destination IDs."""
        bundle = _seed_and_export(tmp_path)

        # First target.
        target_store_a, target_repo_a, _ = _open_target(tmp_path, "memory_a")
        import_workspace_bundle(bundle, target_repo_a)
        ids_a = {
            "facts": {m.id for m in target_repo_a.list_memories()},
            "entities": {e.id for e in target_repo_a.list_entities()},
            "relations": {r.id for r in target_repo_a.list_relations()},
            "relation_objs_a": list(target_repo_a.list_relations()),
        }

        # Second target, fresh import.
        target_store_b, target_repo_b, _ = _open_target(tmp_path, "memory_b")
        import_workspace_bundle(bundle, target_repo_b)
        ids_b = {
            "facts": {m.id for m in target_repo_b.list_memories()},
            "entities": {e.id for e in target_repo_b.list_entities()},
            "relations": {r.id for r in target_repo_b.list_relations()},
            "relation_objs_b": list(target_repo_b.list_relations()),
        }

        # Same cardinality; disjoint IDs.
        assert ids_a["facts"] and ids_a["entities"] and ids_a["relations"]
        assert len(ids_a["facts"]) == len(ids_b["facts"])
        assert len(ids_a["entities"]) == len(ids_b["entities"])
        assert len(ids_a["relations"]) == len(ids_b["relations"])
        assert ids_a["facts"].isdisjoint(ids_b["facts"])
        assert ids_a["entities"].isdisjoint(ids_b["entities"])
        assert ids_a["relations"].isdisjoint(ids_b["relations"])

        # Each imported relation's src/dst must exist in that target's entity set.
        for relation in ids_b["relation_objs_b"]:
            assert relation.src in ids_b["entities"]
            assert relation.dst in ids_b["entities"]

        target_store_a.close()
        target_store_b.close()

    def test_import_payload_conflict_raises_without_partial_writes(self, tmp_path: Path) -> None:
        """Mutating a deterministic destination fact triggers a conflict on replay."""
        bundle = _seed_and_export(tmp_path)
        target_store, target_repo, _ = _open_target(tmp_path, "memory_conflict")

        first = import_workspace_bundle(bundle, target_repo)
        first_imported_count = first.imported_count
        first_observation_count = target_repo.count_observations()

        # Snapshot original (pre-mutation) state for size comparisons.
        pre_mutate_facts = {m.id: m for m in target_repo.list_memories()}
        pre_mutate_entities = {e.id: e for e in target_repo.list_entities()}
        pre_mutate_relations = {r.id: r for r in target_repo.list_relations()}

        # Mutate a deterministic destination fact through explicit ID-based put.
        target_fact_id = next(iter(pre_mutate_facts.keys()))
        original_record = pre_mutate_facts[target_fact_id]
        mutated_record = target_repo._store.get_fact(target_fact_id)
        assert mutated_record is not None
        target_repo._store.put_fact(
            target_fact_id,
            original_record.type,
            original_record.title,
            "MUTATED CONTENT",
            original_record.confidence,
            expected_revision=mutated_record.revision,
        )
        # The mutation must not append an observation.
        assert target_repo.count_observations() == first_observation_count

        with pytest.raises(BundleImportConflictError):
            import_workspace_bundle(bundle, target_repo)

        # Counts are unchanged after the failed replay.
        after_facts = {m.id: m for m in target_repo.list_memories()}
        after_entities = {e.id: e for e in target_repo.list_entities()}
        after_relations = {r.id: r for r in target_repo.list_relations()}
        assert len(after_facts) == len(pre_mutate_facts)
        assert len(after_entities) == len(pre_mutate_entities)
        assert len(after_relations) == len(pre_mutate_relations)
        assert target_repo.count_observations() == first_observation_count

        # The mutated fact still carries the mutation; entities & relations
        # are untouched because the replay refused to write anything.
        assert after_facts[target_fact_id].content == "MUTATED CONTENT"
        assert after_entities == pre_mutate_entities
        assert after_relations == pre_mutate_relations
        # Sanity: the first import DID write at least one record, so the
        # conflict path was reached through the import-already-applied branch.
        assert first_imported_count > 0
        target_store.close()

    def test_import_preserves_provenance(self, tmp_path: Path) -> None:
        """Imported records have new target memory_id, not origin."""
        bundle = _seed_and_export(tmp_path)

        target_store, target_repo, target_mid = _open_target(tmp_path, "memory4")

        import_workspace_bundle(bundle, target_repo)
        facts = target_repo.list_memories()
        for f in facts:
            assert f.binary_id == target_mid
        entities = target_repo.list_entities()
        for e in entities:
            assert e.binary_id == target_mid
        relations = target_repo.list_relations()
        for r in relations:
            assert r.binary_id == target_mid
        target_store.close()

    def test_imported_facts_have_valid_semantic_hashes(self, tmp_path: Path) -> None:
        """Re-imported facts expose well-formed 64-char lowercase SHA-256 hashes."""
        bundle = _seed_and_export(tmp_path)
        target_store, target_repo, _ = _open_target(tmp_path, "memory5")

        import_workspace_bundle(bundle, target_repo)
        for fact in target_store.list_facts():
            assert re.fullmatch(r"[0-9a-f]{64}", fact.semantic_hash)
        target_store.close()
