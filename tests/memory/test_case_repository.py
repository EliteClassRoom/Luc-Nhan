"""Tests for CaseRepository: CRUD, membership, revision safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from lucnhan.memory.case_repository import CaseRepository
from lucnhan.memory.case_schema import CaseRecord
from lucnhan.memory.registry import MemoryRegistry
from lucnhan.memory.workspace import MemoryLocator


def _setup(tmp_path: Path) -> tuple[CaseRepository, MemoryRegistry, str]:
    locator = MemoryLocator(tmp_path / "memory")
    registry = MemoryRegistry(locator.registry_database())
    registry.initialize()
    cases = CaseRepository(registry, locator)
    member = registry.create_workspace("binary", "loader.i64")
    return cases, registry, member.memory_id


class TestCaseCrud:
    def test_create_and_get_case(self, tmp_path: Path) -> None:
        cases, _, _ = _setup(tmp_path)
        case = cases.create_case("Incident 2026-07")

        assert isinstance(case, CaseRecord)
        assert case.name == "Incident 2026-07"
        assert case.state == "active"
        assert case.revision == 1

        fetched = cases.get_case(case.case_id)
        assert fetched is not None
        assert fetched.case_id == case.case_id

    def test_list_cases_excludes_deleted_by_default(self, tmp_path: Path) -> None:
        cases, _, _ = _setup(tmp_path)
        case = cases.create_case("To Delete")

        cases.soft_delete_case(case.case_id, expected_case_revision=case.revision)
        active = cases.list_cases()
        assert all(c.state != "deleted" for c in active)
        assert case.case_id not in {c.case_id for c in active}

    def test_rename_case_increments_revision(self, tmp_path: Path) -> None:
        cases, _, _ = _setup(tmp_path)
        case = cases.create_case("Old Name")

        renamed = cases.rename_case(case.case_id, "New Name", expected_case_revision=case.revision)
        assert renamed.name == "New Name"
        assert renamed.revision == case.revision + 1

    def test_stale_revision_rejected(self, tmp_path: Path) -> None:
        cases, _, _ = _setup(tmp_path)
        case = cases.create_case("Test")

        with pytest.raises(ValueError, match="stale"):
            cases.rename_case(case.case_id, "X", expected_case_revision=case.revision + 5)


class TestCaseMembership:
    def test_add_and_list_member(self, tmp_path: Path) -> None:
        cases, _, mid = _setup(tmp_path)
        case = cases.create_case("Case A")

        cases.add_member(case.case_id, mid, expected_case_revision=case.revision)
        members = cases.list_members(case.case_id)
        assert len(members) == 1
        assert members[0].memory_id == mid

    def test_remove_member(self, tmp_path: Path) -> None:
        cases, _, mid = _setup(tmp_path)
        case = cases.create_case("Case A")
        cases.add_member(case.case_id, mid, expected_case_revision=case.revision)
        current = cases.get_case(case.case_id)

        cases.remove_member(case.case_id, mid, expected_case_revision=current.revision)
        assert cases.list_members(case.case_id) == []
        assert cases.list_members(case.case_id, current_only=False) != []

    def test_readd_removed_member_reactivates(self, tmp_path: Path) -> None:
        cases, _, mid = _setup(tmp_path)
        case = cases.create_case("Case A")
        cases.add_member(case.case_id, mid, expected_case_revision=case.revision)
        current = cases.get_case(case.case_id)
        cases.remove_member(case.case_id, mid, expected_case_revision=current.revision)
        current = cases.get_case(case.case_id)

        cases.add_member(case.case_id, mid, expected_case_revision=current.revision)
        members = cases.list_members(case.case_id)
        assert len(members) == 1

    def test_is_current_member(self, tmp_path: Path) -> None:
        cases, _, mid = _setup(tmp_path)
        case = cases.create_case("Case A")

        assert cases.is_current_member(case.case_id, mid) is False
        cases.add_member(case.case_id, mid, expected_case_revision=case.revision)
        assert cases.is_current_member(case.case_id, mid) is True

    def test_list_cases_for_memory(self, tmp_path: Path) -> None:
        cases, _, mid = _setup(tmp_path)
        case_a = cases.create_case("A")

        cases.add_member(case_a.case_id, mid, expected_case_revision=case_a.revision)
        result = cases.list_cases_for_memory(mid)
        assert len(result) == 1
        assert result[0].case_id == case_a.case_id


class TestSoftDelete:
    def test_soft_delete_does_not_remove_workspaces(self, tmp_path: Path) -> None:
        cases, registry, mid = _setup(tmp_path)
        case = cases.create_case("To Delete")
        cases.add_member(case.case_id, mid, expected_case_revision=case.revision)
        current = cases.get_case(case.case_id)

        cases.soft_delete_case(case.case_id, expected_case_revision=current.revision)

        deleted = cases.get_case(case.case_id)
        assert deleted.state == "deleted"
        # Workspace still exists
        assert registry.get_workspace(mid) is not None
