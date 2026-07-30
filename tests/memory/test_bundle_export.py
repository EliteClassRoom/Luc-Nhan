"""Tests for bundle exporter."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from rikugan.memory.bundle_export import export_workspace
from rikugan.memory.bundle_import import import_workspace_bundle
from rikugan.memory.repository import SQLiteKnowledgeRepository
from rikugan.memory.schema import KnowledgeEntity, KnowledgeMemory
from rikugan.memory.workspace import MemoryLocator, new_memory_id, new_record_id
from rikugan.memory.workspace_store import WorkspaceStore


def _seed_workspace(tmp_path: Path) -> tuple[WorkspaceStore, SQLiteKnowledgeRepository, MemoryLocator, str]:
    memory_id = new_memory_id()
    locator = MemoryLocator(tmp_path / "memory")
    paths = locator.binary(memory_id)
    store = WorkspaceStore.create(paths, owner_memory_id=memory_id)
    repo = SQLiteKnowledgeRepository(store, owner_memory_id=memory_id)
    return store, repo, locator, memory_id


class TestExport:
    def test_export_creates_valid_zip(self, tmp_path: Path) -> None:
        _store, repo, locator, mid = _seed_workspace(tmp_path)
        repo.upsert_memory(
            KnowledgeMemory(
                id=new_record_id("fact"),
                binary_id=mid,
                type="algorithm",
                title="RC4",
                content="Uses RC4",
                confidence=0.8,
            )
        )
        repo.upsert_entity(
            KnowledgeEntity(
                id=new_record_id("entity"),
                binary_id=mid,
                type="function",
                name="main",
                address="0x401000",
            )
        )

        output = tmp_path / "bundle.zip"
        result = export_workspace(locator.binary(mid), repo, output)

        assert output.exists()
        assert result.total_records == 2  # 1 fact + 1 entity
        assert result.bundle_path == output

        with zipfile.ZipFile(output) as zf:
            names = zf.namelist()
            assert "manifest.json" in names
            assert "records/facts.jsonl" in names
            assert "records/entities.jsonl" in names

    def test_export_manifest_has_correct_counts(self, tmp_path: Path) -> None:
        _store, repo, locator, mid = _seed_workspace(tmp_path)
        for i in range(3):
            repo.upsert_memory(
                KnowledgeMemory(
                    id=new_record_id("fact"),
                    binary_id=mid,
                    type="t",
                    title=f"Fact {i}",
                    content="c",
                    confidence=0.5,
                )
            )

        output = tmp_path / "bundle.zip"
        result = export_workspace(locator.binary(mid), repo, output)

        facts_file = [f for f in result.manifest.files if f.name == "records/facts.jsonl"]
        assert len(facts_file) == 1
        assert facts_file[0].record_count == 3

    def test_export_deterministic_record_order(self, tmp_path: Path) -> None:
        """Two exports of the same workspace produce the same record hashes."""
        _store, repo, locator, mid = _seed_workspace(tmp_path)
        repo.upsert_memory(
            KnowledgeMemory(
                id=new_record_id("fact"),
                binary_id=mid,
                type="t",
                title="A",
                content="x",
                confidence=0.5,
            )
        )

        out1 = tmp_path / "bundle1.zip"
        out2 = tmp_path / "bundle2.zip"
        export_workspace(locator.binary(mid), repo, out1)
        export_workspace(locator.binary(mid), repo, out2)

        with zipfile.ZipFile(out1) as zf1, zipfile.ZipFile(out2) as zf2:
            facts1 = zf1.read("records/facts.jsonl")
            facts2 = zf2.read("records/facts.jsonl")
            assert facts1 == facts2

    def test_export_import_round_trip_same_category_facts(self, tmp_path: Path) -> None:
        """Two independent facts of the same category survive export then import."""
        _store, repo, locator, mid = _seed_workspace(tmp_path)
        repo.upsert_memory(
            KnowledgeMemory(
                id=new_record_id("fact"),
                binary_id=mid,
                type="function_purpose",
                title="RC4 init",
                content="Initializes the RC4 key schedule",
                confidence=0.7,
            )
        )
        repo.upsert_memory(
            KnowledgeMemory(
                id=new_record_id("fact"),
                binary_id=mid,
                type="function_purpose",
                title="RC4 crypt",
                content="XORs the plaintext against the keystream",
                confidence=0.7,
            )
        )

        output = tmp_path / "bundle.zip"
        export_workspace(locator.binary(mid), repo, output)
        _store.close()

        # Import into a fresh target workspace.
        target_mid = new_memory_id()
        target_paths = MemoryLocator(tmp_path / "target").binary(target_mid)
        target_store = WorkspaceStore.create(target_paths, owner_memory_id=target_mid)
        target_repo = SQLiteKnowledgeRepository(target_store, owner_memory_id=target_mid)

        result = import_workspace_bundle(output, target_repo)
        assert result.imported_count == 2

        facts = target_repo.list_memories()
        assert [f.type for f in facts].count("function_purpose") == 2
        # Both facts survive with valid 64-char lowercase SHA-256 hashes.
        assert len(target_store.list_facts()) == 2
        for fact in target_store.list_facts():
            assert re.fullmatch(r"[0-9a-f]{64}", fact.semantic_hash)
        target_store.close()
