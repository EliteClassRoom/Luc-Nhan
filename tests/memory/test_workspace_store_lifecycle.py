"""Lifecycle test: WorkspaceStore connection must not leak across agent runs.

Each call to ``SessionControllerBase._wire_central_memory`` constructs a fresh
``WorkspaceStore`` (with a fresh ``sqlite3.Connection``). Without explicit
``close()``, every agent run accumulates one open connection on the same
``memory.db``. This is the Phase 3 leak.

The fix:
* ``WorkspaceStore.close()`` is idempotent — safe to call twice.
* The controller tracks the per-tab store in ``self._memory_stores`` and
  closes the previous one when a new run replaces it AND when the run ends
  via ``on_agent_finished``.
* If ``_wire_central_memory`` raises after the store is constructed, the new
  store is closed before the exception bubbles out (no orphan).

NOTE on import order: ``tests/agent/test_session_controller.py`` calls
``install_ida_mocks()`` at module level so ``rikugan.core.host`` captures
the mocked ``idaapi``. Importing ``rikugan.memory.workspace`` BEFORE that
hook freezes ``host._idaapi`` to ``None``. This file therefore defers every
rikugan import to inside functions/fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# No rikugan imports at the module level. See the note above.


@pytest.fixture
def ctrl(tmp_path: Path):
    """Yield a real ``IdaSessionController`` wired with a fake IDB."""
    from rikugan.core.config import RikuganConfig
    from rikugan.ida.ui.session_controller import IdaSessionController
    from rikugan.memory.workspace import FilesystemIdentity
    from rikugan.memory.workspace_store import WorkspaceStore
    from rikugan.state.history import SessionHistory

    cfg = RikuganConfig()
    cfg._config_dir = str(tmp_path)
    controller = IdaSessionController(cfg)
    controller._idb_path = "/fake/test.i64"
    controller._db_instance_id = "a" * 32
    session = controller.session
    session.idb_path = controller._idb_path
    session.db_instance_id = controller._db_instance_id

    yield controller

    SessionHistory.flush_saves()
    controller.shutdown()
    # Silence unused-import lint for fixture-only symbols.
    _ = (FilesystemIdentity, WorkspaceStore)


def _patch_fs_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``get_filesystem_identity`` so registry bind succeeds."""
    from rikugan.memory.workspace import FilesystemIdentity

    def _fake_fs_identity(_path: str) -> FilesystemIdentity:
        return FilesystemIdentity("vol", "test-volume")

    monkeypatch.setattr("rikugan.memory.identity.get_filesystem_identity", _fake_fs_identity)


def _patch_close_tracker(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record every WorkspaceStore.close invocation. Returns the call list."""
    from rikugan.memory.workspace_store import WorkspaceStore

    calls: list[int] = []
    original_close = WorkspaceStore.close

    def tracking_close(self) -> None:
        calls.append(id(self))
        return original_close(self)

    monkeypatch.setattr(WorkspaceStore, "close", tracking_close)
    return calls


class TestWorkspaceStoreCloseIdempotent:
    """``WorkspaceStore.close()`` must be safe to call twice."""

    def test_double_close_does_not_raise(self, tmp_path: Path) -> None:
        from rikugan.memory.workspace import MemoryLocator, new_memory_id
        from rikugan.memory.workspace_store import WorkspaceStore

        owner = new_memory_id()
        paths = MemoryLocator(tmp_path / "memory").binary(owner)
        store = WorkspaceStore.create(paths, owner_memory_id=owner)
        store.close()
        store.close()
        store.close()
        store.close()


class TestWireCentralMemoryClosesOnSwap:
    """Each new agent run replaces the previous per-tab store; the previous
    store's connection must be closed (no leak across runs).
    """

    def test_six_runs_close_five_displaced_stores(self, ctrl, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import MagicMock

        from rikugan.memory import workspace_open

        _patch_fs_identity(monkeypatch)
        calls = _patch_close_tracker(monkeypatch)

        real_open = workspace_open.open_workspace_for_write

        def passthrough_open(paths_arg, owner_arg, backup_dir_arg):
            return real_open(paths_arg, owner_arg, backup_dir_arg)

        monkeypatch.setattr(workspace_open, "open_workspace_for_write", passthrough_open)

        # First wire — create path, no prior store to close.
        ctrl._wire_central_memory(MagicMock())
        assert calls == [], "First wire must not close anything"

        # Five more wires — each must close the displaced previous store.
        extra_runs = 5
        for _ in range(extra_runs):
            ctrl._wire_central_memory(MagicMock())

        assert len(calls) == extra_runs, (
            f"Expected {extra_runs} swap-closes after {1 + extra_runs} wires, got {len(calls)}"
        )
        assert len(calls) == len(set(calls)), "Same store closed multiple times"

    def test_run_ends_with_close_in_on_agent_finished(self, ctrl, monkeypatch: pytest.MonkeyPatch) -> None:
        """``on_agent_finished`` must close the per-tab store."""
        from unittest.mock import MagicMock

        _patch_fs_identity(monkeypatch)
        calls = _patch_close_tracker(monkeypatch)

        ctrl._wire_central_memory(MagicMock())
        assert calls == [], "Wire must not close its own store"

        # Simulate the run finishing — the per-run store is released.
        ctrl.on_agent_finished()
        assert len(calls) == 1, "on_agent_finished must close the per-run store"

        # A second on_agent_finished for the same tab must not double-close.
        before = len(calls)
        ctrl.on_agent_finished()
        assert len(calls) == before


class TestWireCentralMemoryExceptionSafety:
    """If wiring raises after the store is created, the new store is closed."""

    def test_exception_after_store_creation_closes_store(self, ctrl, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import MagicMock

        from rikugan.memory import service

        _patch_fs_identity(monkeypatch)
        calls = _patch_close_tracker(monkeypatch)

        original_init = service.BinaryMemoryService.__init__

        def boom(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            original_init(self, *args, **kwargs)
            raise RuntimeError("simulated wiring failure")

        monkeypatch.setattr(service.BinaryMemoryService, "__init__", boom)

        ctrl._wire_central_memory(MagicMock())

        assert len(calls) >= 1, "Exception-safety: no store was closed after wiring failure"


class TestWorkspaceStoreCloseIdempotentAfterSwap:
    """If a store is closed on swap AND on run end, double-close must be safe."""

    def test_double_close_via_swap_then_finish(self, ctrl, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wire → finish → wire → finish must work even though the same
        instance could be touched by both the finish hook and the next
        wire-up's swap path.
        """
        from unittest.mock import MagicMock

        _patch_fs_identity(monkeypatch)

        ctrl._wire_central_memory(MagicMock())
        ctrl.on_agent_finished()
        ctrl._wire_central_memory(MagicMock())
        # If close() is not idempotent, the second wire-up's swap-close
        # path raises a ProgrammingError on the same instance.
        ctrl.on_agent_finished()
