"""Tests for MemoryWorkspaceManager active-case binding."""

from __future__ import annotations

from pathlib import Path

import pytest

from lucnhan.core.config import LucNhanConfig
from lucnhan.memory.case_repository import CaseRepository
from lucnhan.memory.manager import MemoryWorkspaceManager
from lucnhan.memory.registry import MemoryRegistry
from lucnhan.memory.workspace import FilesystemIdentity, IdentityRequest, MemoryLocator


def _bind_workspace(tmp_path: Path) -> tuple[MemoryWorkspaceManager, str, str]:
    """Bind a binary workspace and return (manager, memory_id, db_instance_id)."""
    config = LucNhanConfig()
    config._config_dir = str(tmp_path)
    manager = MemoryWorkspaceManager(config)
    request = IdentityRequest(
        source_kind="idb",
        idb_path=str(tmp_path / "a.i64"),
        db_instance_id="uuid-a",
        display_name="a.i64",
        filesystem_identity=FilesystemIdentity("vol", "1"),
    )
    result = manager.bind(request)
    assert result.binding is not None
    return manager, result.binding.memory_id, "uuid-a"


class TestSetActiveCase:
    def test_set_active_case_requires_membership(self, tmp_path: Path) -> None:
        manager, _mid, _ = _bind_workspace(tmp_path)
        cases = CaseRepository(
            MemoryRegistry(MemoryLocator(Path(manager.locator.root)).registry_database()),
            manager.locator,
        )
        cases._registry.initialize()
        case = cases.create_case("Test Case")
        # Binary is NOT a member yet
        with pytest.raises(ValueError, match="not a current member"):
            manager.set_active_case(case.case_id)

    def test_set_active_case_succeeds_for_member(self, tmp_path: Path) -> None:
        manager, mid, _ = _bind_workspace(tmp_path)
        cases = CaseRepository(manager._registry, manager.locator)
        case = cases.create_case("Test Case")
        cases.add_member(case.case_id, mid, expected_case_revision=case.revision)

        ctx = manager.set_active_case(case.case_id)
        assert ctx.active_case_id == case.case_id

    def test_case_binding_generation_increments(self, tmp_path: Path) -> None:
        manager, mid, _ = _bind_workspace(tmp_path)
        cases = CaseRepository(manager._registry, manager.locator)
        case = cases.create_case("Test Case")
        cases.add_member(case.case_id, mid, expected_case_revision=case.revision)

        ctx1 = manager.run_context()
        manager.set_active_case(case.case_id)
        ctx2 = manager.run_context()

        assert ctx2.case_binding_generation > ctx1.case_binding_generation

    def test_clear_active_case(self, tmp_path: Path) -> None:
        manager, mid, _ = _bind_workspace(tmp_path)
        cases = CaseRepository(manager._registry, manager.locator)
        case = cases.create_case("Test Case")
        cases.add_member(case.case_id, mid, expected_case_revision=case.revision)

        manager.set_active_case(case.case_id)
        assert manager.active_case_id == case.case_id

        manager.clear_active_case()
        assert manager.active_case_id == ""

    def test_set_active_case_rejects_deleted_case(self, tmp_path: Path) -> None:
        manager, mid, _ = _bind_workspace(tmp_path)
        cases = CaseRepository(manager._registry, manager.locator)
        case = cases.create_case("Test Case")
        cases.add_member(case.case_id, mid, expected_case_revision=case.revision)
        current = cases.get_case(case.case_id)
        cases.soft_delete_case(case.case_id, expected_case_revision=current.revision)

        with pytest.raises(ValueError, match="deleted"):
            manager.set_active_case(case.case_id)
