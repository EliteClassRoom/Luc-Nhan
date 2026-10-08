"""End-to-end integration tests for the analysis case subsystem.

Tests the full flow: create case → add members → save facts → create
relations → promote → peer retrieval → verify persistence.
"""

from __future__ import annotations

from pathlib import Path

from lucnhan.core.config import LucNhanConfig
from lucnhan.memory.authority import MemoryAuthorityIssuer
from lucnhan.memory.case_repository import CaseRepository
from lucnhan.memory.case_schema import CaseRelationType
from lucnhan.memory.case_service import CaseMemoryService
from lucnhan.memory.manager import MemoryWorkspaceManager
from lucnhan.memory.peer_retrieval import PeerMemoryRetriever
from lucnhan.memory.repository import SQLiteKnowledgeRepository
from lucnhan.memory.schema import KnowledgeMemory
from lucnhan.memory.workspace import (
    FilesystemIdentity,
    IdentityRequest,
    new_record_id,
)
from lucnhan.memory.workspace_store import WorkspaceStore


def _full_setup(tmp_path: Path) -> dict:
    """Set up complete case subsystem with 2 binaries and a case."""
    config = LucNhanConfig()
    config._config_dir = str(tmp_path)
    manager = MemoryWorkspaceManager(config)

    # Binary A
    manager.bind(
        IdentityRequest(
            source_kind="idb",
            idb_path=str(tmp_path / "loader.i64"),
            db_instance_id="uuid-a",
            display_name="loader.exe",
            filesystem_identity=FilesystemIdentity("vol", "1"),
        )
    )
    mid_a = manager._binding.memory_id
    paths_a = manager.locator.binary(mid_a)
    store_a = WorkspaceStore.create(paths_a, owner_memory_id=mid_a)
    repo_a = SQLiteKnowledgeRepository(store_a, owner_memory_id=mid_a)
    repo_a.upsert_memory(
        KnowledgeMemory(
            id=new_record_id("fact"),
            binary_id=mid_a,
            type="c2",
            title="C2 Server",
            content="Connects to evil.example.com",
            confidence=0.9,
        )
    )

    # Binary B
    manager.bind(
        IdentityRequest(
            source_kind="idb",
            idb_path=str(tmp_path / "payload.i64"),
            db_instance_id="uuid-b",
            display_name="payload.dll",
            filesystem_identity=FilesystemIdentity("vol", "2"),
        )
    )
    mid_b = manager._binding.memory_id
    paths_b = manager.locator.binary(mid_b)
    store_b = WorkspaceStore.create(paths_b, owner_memory_id=mid_b)
    repo_b = SQLiteKnowledgeRepository(store_b, owner_memory_id=mid_b)
    repo_b.upsert_memory(
        KnowledgeMemory(
            id=new_record_id("fact"),
            binary_id=mid_b,
            type="transport",
            title="HTTP POST",
            content="Exfiltrates via HTTP POST",
            confidence=0.8,
        )
    )

    # Rebind to A for the case operations
    manager.bind(
        IdentityRequest(
            source_kind="idb",
            idb_path=str(tmp_path / "loader.i64"),
            db_instance_id="uuid-a",
            display_name="loader.exe",
            filesystem_identity=FilesystemIdentity("vol", "1"),
        )
    )

    cases = CaseRepository(manager._registry, manager.locator)
    case_service = CaseMemoryService(cases, binary_repository=repo_a)
    issuer = MemoryAuthorityIssuer()
    context = manager.run_context()

    return {
        "config": config,
        "manager": manager,
        "cases": cases,
        "case_service": case_service,
        "issuer": issuer,
        "context": context,
        "mid_a": mid_a,
        "mid_b": mid_b,
        "repo_a": repo_a,
        "repo_b": repo_b,
        "store_a": store_a,
        "store_b": store_b,
    }


class TestCaseEndToEnd:
    def test_full_workflow(self, tmp_path: Path) -> None:
        env = _full_setup(tmp_path)
        cases = env["cases"]
        mid_a = env["mid_a"]
        mid_b = env["mid_b"]

        # 1. Create case and add both binaries
        case = cases.create_case("Malware Campaign")
        cases.add_member(case.case_id, mid_a, expected_case_revision=case.revision)
        current = cases.get_case(case.case_id)
        cases.add_member(case.case_id, mid_b, expected_case_revision=current.revision)
        assert len(cases.list_members(case.case_id)) == 2

        # 2. Create a cross-binary relation
        rel = cases.put_case_relation(
            case.case_id,
            mid_a,
            CaseRelationType.COMMUNICATES_WITH,
            mid_b,
            confidence=0.9,
        )
        assert rel.predicate is CaseRelationType.COMMUNICATES_WITH

        # 3. List relations
        rels = cases.list_case_relations(case.case_id)
        assert len(rels) == 1

        # 4. Peer retrieval from A's perspective
        retriever = PeerMemoryRetriever(cases, env["manager"].locator)
        pack = retriever.retrieve(case.case_id, active_memory_id=mid_a)
        assert len(pack.peers) == 1
        assert pack.peers[0].memory_id == mid_b
        assert len(pack.records) > 0
        assert "HTTP POST" in pack.records[0].content

        # 5. Promote a fact from A into the case
        facts_a = env["repo_a"].list_memories()
        promotion = env["case_service"].promote(
            env["issuer"].issue(env["context"]),
            env["context"],
            case.case_id,
            facts_a[0].id,
        )
        assert promotion.case_id == case.case_id
        assert promotion.source.source_memory_id == mid_a

        # 6. Soft-delete preserves workspaces
        current = cases.get_case(case.case_id)
        cases.soft_delete_case(case.case_id, expected_case_revision=current.revision)
        assert cases.get_case(case.case_id).state == "deleted"
        assert env["manager"]._registry.get_workspace(mid_a) is not None
        assert env["manager"]._registry.get_workspace(mid_b) is not None

    def test_active_case_binding_flow(self, tmp_path: Path) -> None:
        env = _full_setup(tmp_path)
        manager = env["manager"]
        cases = env["cases"]
        mid_a = env["mid_a"]

        case = cases.create_case("Active Test")
        cases.add_member(case.case_id, mid_a, expected_case_revision=case.revision)

        ctx1 = manager.run_context()
        manager.set_active_case(case.case_id)
        ctx2 = manager.run_context()

        assert ctx2.active_case_id == case.case_id
        assert ctx2.case_binding_generation > ctx1.case_binding_generation

        manager.clear_active_case()
        ctx3 = manager.run_context()
        assert ctx3.active_case_id == ""

    def test_source_drift_detection(self, tmp_path: Path) -> None:
        env = _full_setup(tmp_path)
        cases = env["cases"]
        mid_a = env["mid_a"]

        case = cases.create_case("Drift Test")
        cases.add_member(case.case_id, mid_a, expected_case_revision=case.revision)

        facts = env["repo_a"].list_memories()
        promotion = env["case_service"].promote(
            env["issuer"].issue(env["context"]),
            env["context"],
            case.case_id,
            facts[0].id,
        )

        # Source is current
        state = env["case_service"].evaluate_source_state(case.case_id, promotion.source)
        assert state.status == "current"

        # Update the source fact → drift
        env["repo_a"].upsert_memory(
            KnowledgeMemory(
                id=facts[0].id,
                binary_id=mid_a,
                type=facts[0].type,
                title=facts[0].title,
                content="CHANGED CONTENT",
                confidence=0.5,
            )
        )
        state2 = env["case_service"].evaluate_source_state(case.case_id, promotion.source)
        assert state2.status == "changed"
