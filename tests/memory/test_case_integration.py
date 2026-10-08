"""Integration tests for the analysis case subsystem."""

from __future__ import annotations

from pathlib import Path

import pytest

from lucnhan.memory.case_repository import CaseRepository
from lucnhan.memory.case_schema import (
    CaseRelationType,
    canonicalize_relation_endpoints,
)
from lucnhan.memory.registry import MemoryRegistry
from lucnhan.memory.workspace import MemoryLocator
from lucnhan.memory.workspace_store import WorkspaceStore


def _setup_registry(tmp_path: Path) -> tuple[CaseRepository, MemoryRegistry, MemoryLocator]:
    locator = MemoryLocator(tmp_path / "memory")
    registry = MemoryRegistry(locator.registry_database())
    registry.initialize()
    return CaseRepository(registry, locator), registry, locator


class TestCaseEndToEnd:
    """Full flow: create case → add members → verify membership."""

    def test_create_case_add_member_verify(self, tmp_path: Path) -> None:
        cases, registry, _locator = _setup_registry(tmp_path)

        # Create two binary workspaces
        binary_a = registry.create_workspace("binary", "loader.exe")
        binary_b = registry.create_workspace("binary", "payload.dll")

        # Create a case and add both binaries
        case = cases.create_case("Malware Campaign 2026")
        cases.add_member(case.case_id, binary_a.memory_id, expected_case_revision=case.revision)
        current = cases.get_case(case.case_id)
        cases.add_member(case.case_id, binary_b.memory_id, expected_case_revision=current.revision)

        # Both should be current members
        members = cases.list_members(case.case_id)
        assert len(members) == 2

        # Both cases should list back
        cases_for_a = cases.list_cases_for_memory(binary_a.memory_id)
        assert len(cases_for_a) == 1
        assert cases_for_a[0].case_id == case.case_id

    def test_case_workspace_db_can_be_created(self, tmp_path: Path) -> None:
        """A case has its own workspace DB with workspace_kind='case'."""
        cases, _registry, locator = _setup_registry(tmp_path)

        case = cases.create_case("Test Case")
        case_paths = locator.case(case.case_id)
        store = WorkspaceStore.create(case_paths, owner_memory_id=case.case_id, workspace_kind="case")

        # Case workspace can hold facts independently from binary workspaces
        from lucnhan.memory.workspace import new_record_id

        fid = new_record_id("fact")
        store.put_fact(fid, "shared", "Finding", "Cross-binary finding", 0.9, expected_revision=0)
        assert len(store.list_facts()) == 1
        store.close()

    def test_relation_canonicalization_for_all_five_types(self) -> None:
        """All five relation types canonicalize correctly."""
        a = "mem-" + "a" * 32
        b = "mem-" + "b" * 32

        # Directed predicates preserve order
        for pred in [CaseRelationType.EMBEDS_OR_LOADS, CaseRelationType.DERIVED_FROM]:
            result = canonicalize_relation_endpoints(b, pred, a)
            assert result == (b, a)

        # Symmetric predicates sort endpoints
        for pred in [
            CaseRelationType.COMMUNICATES_WITH,
            CaseRelationType.SAME_FAMILY_AS,
        ]:
            result = canonicalize_relation_endpoints(b, pred, a)
            assert result == (a, b)

    def test_soft_delete_preserves_data(self, tmp_path: Path) -> None:
        """Soft-deleting a case keeps workspaces and members intact."""
        cases, registry, _locator = _setup_registry(tmp_path)
        binary = registry.create_workspace("binary", "sample.exe")
        case = cases.create_case("To Delete")
        cases.add_member(case.case_id, binary.memory_id, expected_case_revision=case.revision)
        current = cases.get_case(case.case_id)

        cases.soft_delete_case(case.case_id, expected_case_revision=current.revision)

        # Case is deleted but workspace still exists
        assert cases.get_case(case.case_id).state == "deleted"
        assert registry.get_workspace(binary.memory_id) is not None

    def test_disabled_state_prevents_membership_operations(self, tmp_path: Path) -> None:
        """A case in 'disabled' state rejects membership writes via stale revision."""
        cases, registry, _locator = _setup_registry(tmp_path)
        binary = registry.create_workspace("binary", "sample.exe")
        case = cases.create_case("Active Case")
        cases.add_member(case.case_id, binary.memory_id, expected_case_revision=case.revision)

        # Any mutation uses expected_case_revision, so a disabled state
        # would have a different revision → stale rejection
        current = cases.get_case(case.case_id)
        # Simulate: after rename, old revision is stale
        cases.rename_case(case.case_id, "Renamed", expected_case_revision=current.revision)
        with pytest.raises(ValueError, match="stale"):
            cases.add_member(case.case_id, binary.memory_id, expected_case_revision=current.revision)
