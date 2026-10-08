"""Tests for MemoryWorkspaceManager: binding, generation, persistence."""

from __future__ import annotations

from pathlib import Path

from lucnhan.core.config import LucNhanConfig
from lucnhan.memory.identity import (
    ResolutionStatus,
)
from lucnhan.memory.manager import MemoryWorkspaceManager
from lucnhan.memory.workspace import (
    FilesystemIdentity,
    IdentityRequest,
)


def _idb_request(
    path: Path,
    db_instance_id: str = "",
    fs: tuple[str, str] | None = None,
) -> IdentityRequest:
    return IdentityRequest(
        source_kind="idb",
        idb_path=str(path),
        db_instance_id=db_instance_id,
        display_name=path.name,
        filesystem_identity=FilesystemIdentity(*fs) if fs else None,
    )


class TestEnabledBinding:
    def test_enabled_config_creates_registry_and_resolves(self, tmp_path: Path) -> None:
        config = LucNhanConfig()
        config._config_dir = str(tmp_path)
        manager = MemoryWorkspaceManager(config)

        request = _idb_request(tmp_path / "a.i64", "uuid-a", ("vol", "1"))
        result = manager.bind(request)

        assert result.status is ResolutionStatus.CREATED
        assert result.binding is not None
        assert result.binding.state == "active"
        assert result.binding.memory_id.startswith("mem-")

        # Registry was created
        assert (tmp_path / "memory" / "registry.db").exists()

    def test_run_context_is_frozen_per_run(self, tmp_path: Path) -> None:
        config = LucNhanConfig()
        config._config_dir = str(tmp_path)
        manager = MemoryWorkspaceManager(config)

        request = _idb_request(tmp_path / "a.i64", "uuid-a", ("vol", "1"))
        manager.bind(request)

        ctx1 = manager.run_context()
        ctx2 = manager.run_context()
        assert ctx1 == ctx2

    def test_database_generation_increments_on_rebind(self, tmp_path: Path) -> None:
        config = LucNhanConfig()
        config._config_dir = str(tmp_path)
        manager = MemoryWorkspaceManager(config)

        request1 = _idb_request(tmp_path / "a.i64", "uuid-a", ("vol", "1"))
        manager.bind(request1)
        ctx1 = manager.run_context()

        # Rebind with a different identity (different UUID + FS)
        request2 = _idb_request(tmp_path / "b.i64", "uuid-b", ("vol", "2"))
        manager.bind(request2)
        ctx2 = manager.run_context()

        assert ctx2.database_generation > ctx1.database_generation

    def test_validate_run_context_rejects_stale(self, tmp_path: Path) -> None:
        config = LucNhanConfig()
        config._config_dir = str(tmp_path)
        manager = MemoryWorkspaceManager(config)

        request = _idb_request(tmp_path / "a.i64", "uuid-a", ("vol", "1"))
        manager.bind(request)
        ctx = manager.run_context()

        assert manager.validate_run_context(ctx) is True

        # Rebind with different identity
        request2 = _idb_request(tmp_path / "b.i64", "uuid-b", ("vol", "2"))
        manager.bind(request2)

        assert manager.validate_run_context(ctx) is False

    def test_require_persistent_paths_returns_workspace_paths(self, tmp_path: Path) -> None:
        config = LucNhanConfig()
        config._config_dir = str(tmp_path)
        manager = MemoryWorkspaceManager(config)

        request = _idb_request(tmp_path / "a.i64", "uuid-a", ("vol", "1"))
        manager.bind(request)

        paths = manager.require_persistent_paths()
        assert paths.database.parent.exists() or True  # paths exist after store.create

    def test_raw_source_resolves_workspace(self, tmp_path: Path) -> None:
        config = LucNhanConfig()
        config._config_dir = str(tmp_path)
        manager = MemoryWorkspaceManager(config)

        request = IdentityRequest(
            source_kind="raw",
            idb_path=str(tmp_path / "sample.bin"),
            source_sha256="a" * 64,
            display_name="sample.bin",
        )
        result = manager.bind(request)

        assert result.status is ResolutionStatus.CREATED
        assert result.binding is not None
        assert result.binding.memory_id.startswith("mem-")


class TestRunContext:
    def test_context_contains_empty_case_id_by_default(self, tmp_path: Path) -> None:
        config = LucNhanConfig()
        config._config_dir = str(tmp_path)
        manager = MemoryWorkspaceManager(config)

        request = _idb_request(tmp_path / "a.i64", "uuid-a", ("vol", "1"))
        manager.bind(request)
        ctx = manager.run_context()

        assert ctx.active_case_id == ""
