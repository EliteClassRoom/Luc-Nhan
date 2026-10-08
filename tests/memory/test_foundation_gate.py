"""Foundation integration gate: stable types and enabled flow."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from lucnhan.core.config import LucNhanConfig
from lucnhan.memory.manager import MemoryWorkspaceManager, PersistenceDisabled
from lucnhan.memory.workspace import (
    FilesystemIdentity,
    IdentityRequest,
    new_record_id,
)


class TestStableTypes:
    """All public types must be importable from their canonical modules."""

    def test_workspace_module_exports(self) -> None:
        mod = importlib.import_module("lucnhan.memory.workspace")
        for name in (
            "FilesystemIdentity",
            "IdentityRequest",
            "MemoryLocator",
            "MemoryRunContext",
            "WorkspaceBinding",
            "WorkspacePaths",
            "new_memory_id",
            "new_case_id",
            "new_record_id",
            "validate_memory_id",
            "validate_case_id",
            "validate_record_id",
        ):
            assert hasattr(mod, name), f"workspace module missing {name}"

    def test_identity_module_exports(self) -> None:
        mod = importlib.import_module("lucnhan.memory.identity")
        for name in (
            "IdentityChoice",
            "MemoryIdentityResolver",
            "ResolutionStatus",
            "IdentityResolution",
            "get_filesystem_identity",
            "hash_raw_binary",
        ):
            assert hasattr(mod, name), f"identity module missing {name}"

    def test_registry_module_exports(self) -> None:
        mod = importlib.import_module("lucnhan.memory.registry")
        for name in ("MemoryRegistry", "WorkspaceRecord", "EvidenceConflictError"):
            assert hasattr(mod, name), f"registry module missing {name}"

    def test_workspace_store_module_exports(self) -> None:
        mod = importlib.import_module("lucnhan.memory.workspace_store")
        for name in (
            "WorkspaceStore",
            "FactRecord",
            "StaleRevisionError",
            "ProjectionState",
        ):
            assert hasattr(mod, name), f"workspace_store module missing {name}"

    def test_markdown_module_exports(self) -> None:
        mod = importlib.import_module("lucnhan.memory.markdown")
        for name in (
            "MemoryProjector",
            "parse_memory_document",
            "render_memory_document",
            "ManagedRegionError",
        ):
            assert hasattr(mod, name), f"markdown module missing {name}"

    def test_manager_module_exports(self) -> None:
        mod = importlib.import_module("lucnhan.memory.manager")
        for name in ("MemoryWorkspaceManager", "PersistenceDisabled"):
            assert hasattr(mod, name), f"manager module missing {name}"


class TestEndToEndFlow:
    """Full flow: bind → context → persistence."""

    def test_unbound_manager_rejects_persistent_paths(self, tmp_path: Path) -> None:
        """A manager with no active binding rejects persistent paths."""
        config = LucNhanConfig()
        config._config_dir = str(tmp_path)
        manager = MemoryWorkspaceManager(config)

        # bind() never called — binding is None
        with pytest.raises(PersistenceDisabled):
            manager.require_persistent_paths()

    def test_enabled_full_flow(self, tmp_path: Path) -> None:
        """Enabled mode: bind → create store → project → verify."""
        from lucnhan.memory.markdown import MemoryProjector
        from lucnhan.memory.workspace_store import WorkspaceStore

        config = LucNhanConfig()
        config._config_dir = str(tmp_path)
        manager = MemoryWorkspaceManager(config)

        request = IdentityRequest(
            source_kind="idb",
            idb_path=str(tmp_path / "test.i64"),
            db_instance_id="uuid-y",
            display_name="test.i64",
            filesystem_identity=FilesystemIdentity("vol", "2"),
        )
        result = manager.bind(request)
        assert result.binding is not None
        assert result.binding.state == "active"

        paths = manager.require_persistent_paths()
        store = WorkspaceStore.create(paths, owner_memory_id=result.binding.memory_id)
        fid = new_record_id("fact")
        store.put_fact(fid, "algorithm", "RC4", "Uses RC4", 0.8, expected_revision=0)

        projector = MemoryProjector()
        projector.project(paths, store)

        content = paths.markdown.read_text(encoding="utf-8")
        assert "Uses RC4" in content

        store.close()
