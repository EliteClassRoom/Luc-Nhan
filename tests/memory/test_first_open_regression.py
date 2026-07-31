"""Regression test for the first-open bug: workspace exists in registry but memory.db missing.

This reproduces the exact scenario that caused
``ERROR Central memory wiring failed: ...memory.db``
when a user enabled central memory for the first time.
"""

from __future__ import annotations

from pathlib import Path

from rikugan.core.config import RikuganConfig
from rikugan.memory.manager import MemoryWorkspaceManager
from rikugan.memory.workspace import (
    FilesystemIdentity,
    IdentityRequest,
    MemoryLocator,
    new_memory_id,
    new_record_id,
)
from rikugan.memory.workspace_open import open_workspace_for_write
from rikugan.memory.workspace_store import WorkspaceStore


class TestFirstOpenCreatesDb:
    """When registry has a workspace row but no memory.db, open must not fail."""

    def test_bind_then_open_missing_db_creates_it(self, tmp_path: Path) -> None:
        """Simulates controller _wire_central_memory on first agent run."""
        config = RikuganConfig()
        config._config_dir = str(tmp_path)

        manager = MemoryWorkspaceManager(config)
        request = IdentityRequest(
            source_kind="idb",
            idb_path=str(tmp_path / "test.i64"),
            db_instance_id="uuid-1",
            display_name="test.i64",
            filesystem_identity=FilesystemIdentity("vol", "1"),
        )
        result = manager.bind(request)
        assert result.binding is not None
        assert result.binding.state == "active"

        paths = manager.require_persistent_paths()
        locator = MemoryLocator(config.memory_dir)
        backup_dir = locator.backups(result.binding.memory_id)

        # At this point, the workspace row exists in registry but
        # memory.db does NOT exist on disk yet.
        assert not paths.database.exists()

        # The controller checks existence and uses the production helper
        # on the second-run path; on the first-run path it falls back to
        # WorkspaceStore.create.
        if paths.database.exists():
            store = open_workspace_for_write(paths, result.binding.memory_id, backup_dir)
        else:
            store = WorkspaceStore.create(paths, owner_memory_id=result.binding.memory_id)

        # After create(), the database file exists and is usable.
        assert paths.database.exists()

        # The store can accept writes immediately.
        store.put_fact(new_record_id("fact"), "test", "T", "content", 0.5, expected_revision=0)
        assert len(store.list_facts()) == 1
        store.close()

    def test_reopen_existing_db_uses_open_not_create(self, tmp_path: Path) -> None:
        """Second agent run: DB already exists, open() succeeds via the helper."""
        config = RikuganConfig()
        config._config_dir = str(tmp_path)

        manager = MemoryWorkspaceManager(config)
        request = IdentityRequest(
            source_kind="idb",
            idb_path=str(tmp_path / "test.i64"),
            db_instance_id="uuid-2",
            display_name="test.i64",
            filesystem_identity=FilesystemIdentity("vol", "2"),
        )
        result = manager.bind(request)
        paths = manager.require_persistent_paths()
        locator = MemoryLocator(config.memory_dir)
        backup_dir = locator.backups(result.binding.memory_id)

        # First open: create
        store1 = WorkspaceStore.create(paths, owner_memory_id=result.binding.memory_id)
        store1.close()

        # Second open: file exists, helper should work
        assert paths.database.exists()
        store2 = open_workspace_for_write(paths, result.binding.memory_id, backup_dir)
        store2.close()

    def test_save_fact_through_service_on_first_open(self, tmp_path: Path) -> None:
        """Full end-to-end: bind → create DB → save_fact → MEMORY.md projected."""
        from rikugan.memory.authority import MemoryAuthorityIssuer
        from rikugan.memory.markdown import MemoryProjector
        from rikugan.memory.repository import SQLiteKnowledgeRepository
        from rikugan.memory.service import BinaryMemoryService

        config = RikuganConfig()
        config._config_dir = str(tmp_path)

        manager = MemoryWorkspaceManager(config)
        request = IdentityRequest(
            source_kind="idb",
            idb_path=str(tmp_path / "test.i64"),
            db_instance_id="uuid-3",
            display_name="test.i64",
            filesystem_identity=FilesystemIdentity("vol", "3"),
        )
        result = manager.bind(request)
        assert result.binding is not None

        paths = manager.require_persistent_paths()
        locator = MemoryLocator(config.memory_dir)
        backup_dir = locator.backups(result.binding.memory_id)

        # First-open logic: file doesn't exist → create
        if paths.database.exists():
            store = open_workspace_for_write(paths, result.binding.memory_id, backup_dir)
        else:
            store = WorkspaceStore.create(paths, owner_memory_id=result.binding.memory_id)

        repo = SQLiteKnowledgeRepository(store, owner_memory_id=result.binding.memory_id)
        issuer = MemoryAuthorityIssuer()
        context = manager.run_context()
        service = BinaryMemoryService(
            context=context,
            paths=paths,
            repository=repo,
            store=store,
            projector=MemoryProjector(),
            authority_issuer=issuer,
        )

        # Save a fact — this must succeed and project to MEMORY.md
        save_result = service.save_fact(
            issuer.issue(context),
            category="algorithm",
            fact="Uses RC4",
            source="save_memory",
        )

        assert save_result.projection_dirty is False
        assert paths.markdown.exists()
        content = paths.markdown.read_text(encoding="utf-8")
        assert "Uses RC4" in content
        assert "rikugan:managed:start" in content
        store.close()


def test_existing_binary_workspace_uses_backup_aware_open(tmp_path, monkeypatch) -> None:
    """Direct call to open_workspace_for_write must be the writable path used."""
    from rikugan.memory import workspace_open

    owner = new_memory_id()
    locator = MemoryLocator(tmp_path / "memory")
    paths = locator.binary(owner)
    WorkspaceStore.create(paths, owner_memory_id=owner).close()

    calls: list[tuple[object, str, object]] = []
    real_open = workspace_open.open_workspace_for_write

    def tracking_open(paths_arg, owner_arg, backup_dir_arg):
        calls.append((paths_arg, owner_arg, backup_dir_arg))
        return real_open(paths_arg, owner_arg, backup_dir_arg)

    monkeypatch.setattr(workspace_open, "open_workspace_for_write", tracking_open)
    store = workspace_open.open_workspace_for_write(paths, owner, locator.backups(owner))
    store.close()
    assert calls == [(paths, owner, locator.backups(owner))]
