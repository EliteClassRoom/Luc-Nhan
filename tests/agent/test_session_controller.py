"""Tests for iris.ui.session_controller."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from concurrent.futures import CancelledError, Future
from typing import ClassVar
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.core.config import RikuganConfig  # noqa: E402
from rikugan.core.types import Message, Role, TokenUsage, ToolCall, ToolResult  # noqa: E402
from rikugan.ida.ui.session_controller import IdaSessionController  # noqa: E402
from rikugan.state.history import SessionHistory  # noqa: E402
from rikugan.state.history_types import (  # noqa: E402
    HistoryAttachStatus,
    HistoryDeleteStatus,
    HistoryRequestStatus,
)


class TestIdaSessionController(unittest.TestCase):
    def setUp(self):
        self.cfg = RikuganConfig()
        self.cfg._config_dir = tempfile.mkdtemp()
        self.ctrl = IdaSessionController(self.cfg)

    def tearDown(self):
        # Flush any pending async saves BEFORE shutting down so the
        # background ``_SAVE_EXECUTOR`` finishes touching the tempdir.
        SessionHistory.flush_saves()
        self.ctrl.shutdown()
        # ``_config_dir`` is a per-test tempdir — clean up so we don't
        # leak hundreds of them across the cumulative test run.
        shutil.rmtree(self.cfg._config_dir, ignore_errors=True)

    def test_initial_session_state(self):
        self.assertIsNotNone(self.ctrl.session)
        self.assertEqual(self.ctrl.session.provider_name, self.cfg.provider.name)
        self.assertEqual(self.ctrl.session.model_name, self.cfg.provider.model)

    def test_is_agent_running_initially_false(self):
        self.assertFalse(self.ctrl.is_agent_running)

    def test_get_event_without_runner_returns_none(self):
        self.assertIsNone(self.ctrl.get_event())

    def test_queue_and_drain_messages(self):
        self.ctrl.queue_message("first")
        self.ctrl.queue_message("second")

        # on_agent_finished now returns the head of the pending queue
        # so the UI layer can drain one queued message per run completion
        # instead of silently discarding user input.
        next_msg = self.ctrl.on_agent_finished()
        self.assertEqual(next_msg, "first")

        # The second queued message is returned on the next finish.
        next_msg = self.ctrl.on_agent_finished()
        self.assertEqual(next_msg, "second")

        # Subsequent calls return None once the queue is empty.
        self.assertIsNone(self.ctrl.on_agent_finished())

    def test_cancel_clears_pending_messages(self):
        self.ctrl.queue_message("will be cancelled")
        self.ctrl.cancel()
        next_msg = self.ctrl.on_agent_finished()
        self.assertIsNone(next_msg)

    def test_new_chat_creates_fresh_session(self):
        old_id = self.ctrl.session.id
        self.ctrl.session.add_message(Message(role=Role.USER, content="hello"))
        self.ctrl.new_chat()

        self.assertNotEqual(self.ctrl.session.id, old_id)
        self.assertEqual(len(self.ctrl.session.messages), 0)

    def test_new_chat_clears_pending_messages(self):
        self.ctrl.queue_message("pending")
        self.ctrl.new_chat()
        self.assertIsNone(self.ctrl.on_agent_finished())

    def test_update_settings_syncs_session(self):
        self.cfg.provider.name = "test_provider"
        self.cfg.provider.model = "test_model"
        self.ctrl.update_settings()

        self.assertEqual(self.ctrl.session.provider_name, "test_provider")
        self.assertEqual(self.ctrl.session.model_name, "test_model")

    def test_skill_slugs_returns_list(self):
        slugs = self.ctrl.skill_slugs
        self.assertIsInstance(slugs, list)

    def test_on_agent_finished_auto_saves(self):
        self.cfg.checkpoint_auto_save = True
        self.ctrl.session.add_message(Message(role=Role.USER, content="test"))
        self.ctrl.on_agent_finished()

        # on_agent_finished saves off-main-thread now (end-of-turn freeze
        # fix); flush before asserting on-disk state.
        SessionHistory.flush_saves()

        # Verify session was saved to disk
        history = SessionHistory(self.cfg)
        sessions = history.list_sessions(db_instance_id=self.ctrl._db_instance_id)
        self.assertTrue(any(s["id"] == self.ctrl.session.id for s in sessions))

    def test_restore_session(self):
        # Persist a session through the on-agent-finished auto-save path.
        self.ctrl.session.add_message(Message(role=Role.USER, content="persisted"))
        self.cfg.checkpoint_auto_save = True
        self.ctrl.on_agent_finished()
        saved_id = self.ctrl.session.id

        # on_agent_finished + new_chat both save off-main-thread now; flush
        # before reading the session back from disk.
        SessionHistory.flush_saves()

        # New chat, then load -> attach through the new history API.
        self.ctrl.new_chat()
        SessionHistory.flush_saves()
        self.assertNotEqual(self.ctrl.session.id, saved_id)

        scope = self.ctrl.capture_history_scope(generation=1)
        loaded = self.ctrl.load_history_session(saved_id, scope)
        assert loaded.status is HistoryRequestStatus.LOADED  # type: ignore[union-attr]
        before = len(self.ctrl.tab_ids)
        attached = self.ctrl.attach_history_session(loaded)
        # new_chat() left the active tab as an empty draft, so attach
        # reuses it (REUSED) instead of creating a new tab.
        self.assertIs(attached.status, HistoryAttachStatus.REUSED)
        self.assertEqual(len(self.ctrl.tab_ids), before)
        self.assertEqual(self.ctrl._sessions[attached.tab_id].id, saved_id)
        self.assertEqual(len(self.ctrl._sessions[attached.tab_id].messages), 1)
        self.assertEqual(self.ctrl._sessions[attached.tab_id].messages[0].content, "persisted")

    def test_restore_preserves_token_usage(self):
        """Full round-trip: save with token usage -> load+attach -> verify preserved."""
        usage = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        self.ctrl.session.add_message(Message(role=Role.USER, content="question"))
        self.ctrl.session.add_message(
            Message(role=Role.ASSISTANT, content="answer", token_usage=usage),
        )
        self.cfg.checkpoint_auto_save = True
        self.ctrl.on_agent_finished()
        saved_id = self.ctrl.session.id

        # Flush the off-main-thread save before a fresh controller reads it.
        SessionHistory.flush_saves()

        # Create fresh controller and load+attach through the new path.
        ctrl2 = IdaSessionController(self.cfg)
        try:
            scope = ctrl2.capture_history_scope(generation=1)
            loaded = ctrl2.load_history_session(saved_id, scope)
            assert loaded.status is HistoryRequestStatus.LOADED  # type: ignore[union-attr]
            attached = ctrl2.attach_history_session(loaded)
            # Fresh controller's active tab is an empty draft -> REUSED.
            self.assertIs(attached.status, HistoryAttachStatus.REUSED)
            self.assertIsNotNone(attached.session)
            self.assertEqual(attached.session.id, saved_id)  # type: ignore[union-attr]
            self.assertEqual(len(attached.session.messages), 2)  # type: ignore[union-attr]
            self.assertEqual(
                attached.session.messages[1].content,
                "answer",  # type: ignore[union-attr]
            )
        finally:
            ctrl2.shutdown()

    def test_restore_preserves_tool_calls(self):
        """Full round-trip: save with tool calls -> load+attach -> verify preserved."""
        tc = ToolCall(id="tc_1", name="get_info", arguments={"addr": "0x1000"})
        tr = ToolResult(tool_call_id="tc_1", name="get_info", content="data here")
        self.ctrl.session.add_message(Message(role=Role.USER, content="analyze"))
        self.ctrl.session.add_message(
            Message(role=Role.ASSISTANT, content="", tool_calls=[tc]),
        )
        self.ctrl.session.add_message(Message(role=Role.TOOL, tool_results=[tr]))
        self.cfg.checkpoint_auto_save = True
        self.ctrl.on_agent_finished()
        saved_id = self.ctrl.session.id
        SessionHistory.flush_saves()

        ctrl2 = IdaSessionController(self.cfg)
        try:
            scope = ctrl2.capture_history_scope(generation=1)
            loaded = ctrl2.load_history_session(saved_id, scope)
            assert loaded.status is HistoryRequestStatus.LOADED  # type: ignore[union-attr]
            attached = ctrl2.attach_history_session(loaded)
            # Fresh controller's active tab is an empty draft -> REUSED.
            self.assertIs(attached.status, HistoryAttachStatus.REUSED)
            self.assertIsNotNone(attached.session)
            self.assertEqual(len(attached.session.messages), 3)  # type: ignore[union-attr]
            self.assertEqual(
                len(attached.session.messages[1].tool_calls),
                1,  # type: ignore[union-attr]
            )
            self.assertEqual(
                attached.session.messages[1].tool_calls[0].name,
                "get_info",  # type: ignore[union-attr]
            )
            self.assertEqual(
                attached.session.messages[2].tool_results[0].content,
                "data here",  # type: ignore[union-attr]
            )
        finally:
            ctrl2.shutdown()

    # ------------------------------------------------------------------
    # Task 5 — Controller History APIs (capture / list / load / attach)
    # ------------------------------------------------------------------

    def _save_history_session(self, instance_id: str) -> str:
        """Persist a session directly through SessionHistory (no auto-save path)."""
        from rikugan.state.session import SessionState

        session = SessionState(
            id="saved-history",
            idb_path=self.ctrl._idb_path,
            db_instance_id=instance_id,
        )
        session.add_message(Message(role=Role.USER, content="Analyze parser"))
        SessionHistory(self.cfg).save_session(session)
        return session.id

    def _loaded_result_for_current_idb(self, generation: int):
        """Save a session for the live IDB, then load it through the new API."""
        saved_id = self._save_history_session(self.ctrl._db_instance_id)
        scope = self.ctrl.capture_history_scope(generation)
        return self.ctrl.load_history_session(saved_id, scope)

    def _load_result_for_instance(self, *, saved_instance: str, live_instance: str):
        """Set the live IDB, save a session for ``saved_instance``, then load it."""
        self.ctrl._db_instance_id = live_instance
        saved_id = self._save_history_session(saved_instance)
        scope = self.ctrl.capture_history_scope(generation=1)
        return self.ctrl.load_history_session(saved_id, scope)

    def test_capture_history_scope_carries_live_identity(self) -> None:
        scope = self.ctrl.capture_history_scope(generation=7)
        self.assertEqual(scope.idb_path, self.ctrl._idb_path)
        self.assertEqual(scope.db_instance_id, self.ctrl._db_instance_id)
        self.assertEqual(scope.generation, 7)

    def test_load_does_not_mutate_sessions(self) -> None:
        saved_id = self._save_history_session(self.ctrl._db_instance_id)
        scope = self.ctrl.capture_history_scope(generation=3)
        before = dict(self.ctrl._sessions)

        result = self.ctrl.load_history_session(saved_id, scope)

        self.assertIs(result.status, HistoryRequestStatus.LOADED)
        self.assertEqual(self.ctrl._sessions, before)

    def test_attach_returns_already_open_for_persisted_id(self) -> None:
        saved_id = self._save_history_session(self.ctrl._db_instance_id)
        scope = self.ctrl.capture_history_scope(generation=3)
        loaded = self.ctrl.load_history_session(saved_id, scope)
        first = self.ctrl.attach_history_session(loaded)

        second = self.ctrl.attach_history_session(loaded)

        # First attach reuses the empty active tab (REUSED), second sees
        # the persisted id already open (ALREADY_OPEN).
        self.assertIs(first.status, HistoryAttachStatus.REUSED)
        self.assertIs(second.status, HistoryAttachStatus.ALREADY_OPEN)
        self.assertEqual(second.tab_id, first.tab_id)

    def test_attach_reuses_empty_active_tab(self) -> None:
        """Loading a session into an empty active draft reuses its tab."""
        saved_id = self._save_history_session(self.ctrl._db_instance_id)
        active_tab_id = self.ctrl.active_tab_id
        scope = self.ctrl.capture_history_scope(generation=3)
        loaded = self.ctrl.load_history_session(saved_id, scope)

        result = self.ctrl.attach_history_session(loaded)

        self.assertIs(result.status, HistoryAttachStatus.REUSED)
        self.assertEqual(result.tab_id, active_tab_id)
        self.assertEqual(self.ctrl.session.id, saved_id)

    def test_attach_creates_new_tab_when_active_has_messages(self) -> None:
        """A non-empty active tab keeps the historical 'new tab' behaviour."""
        saved_id = self._save_history_session(self.ctrl._db_instance_id)
        # Make the active draft non-empty so it cannot be reused.
        self.ctrl.session.add_message(Message(role=Role.USER, content="in-progress"))
        scope = self.ctrl.capture_history_scope(generation=3)
        loaded = self.ctrl.load_history_session(saved_id, scope)

        result = self.ctrl.attach_history_session(loaded)

        self.assertIs(result.status, HistoryAttachStatus.OPENED)
        self.assertNotEqual(result.tab_id, self.ctrl.active_tab_id)

    def test_attach_rejects_stale_live_scope(self) -> None:
        loaded = self._loaded_result_for_current_idb(generation=4)
        # Simulate an IDB switch on the Qt main thread between load and attach.
        self.ctrl._db_instance_id = "b" * 32

        result = self.ctrl.attach_history_session(loaded)

        self.assertIs(result.status, HistoryAttachStatus.STALE_SCOPE)

    def test_wrong_idb_payload_is_not_loaded(self) -> None:
        result = self._load_result_for_instance(saved_instance="a" * 32, live_instance="b" * 32)
        self.assertIs(result.status, HistoryRequestStatus.WRONG_IDB)
        self.assertIsNone(result.session)

    def test_find_tab_matches_session_id_not_tab_id(self) -> None:
        tab_id = self.ctrl.active_tab_id
        persisted_id = self.ctrl.session.id
        self.assertEqual(self.ctrl.find_tab_for_session(persisted_id), tab_id)
        self.assertIsNone(self.ctrl.find_tab_for_session(tab_id))

    def test_list_history_sessions_returns_entries_without_loading_payload(self) -> None:
        saved_id = self._save_history_session(self.ctrl._db_instance_id)
        scope = self.ctrl.capture_history_scope(generation=2)

        entries = self.ctrl.list_history_sessions(scope)

        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.session_id, saved_id)
        self.assertEqual(entry.message_count, 1)
        self.assertEqual(entry.title, "Analyze parser")

    # ------------------------------------------------------------------
    # Task 3 — Delete boundary (Qt-free controller method)
    # ------------------------------------------------------------------

    def test_delete_history_session_maps_deleted_outcome(self) -> None:
        saved_id = self._save_history_session(self.ctrl._db_instance_id)
        scope = self.ctrl.capture_history_scope(generation=9)

        result = self.ctrl.delete_history_session(saved_id, scope)

        self.assertIs(result.status, HistoryDeleteStatus.DELETED)
        self.assertEqual(result.scope, scope)
        self.assertEqual(result.session_id, saved_id)

    def test_delete_history_session_rejects_stale_live_scope(self) -> None:
        scope = self.ctrl.capture_history_scope(generation=3)
        self.ctrl._db_instance_id = "f" * 32

        with patch("rikugan.state.history.SessionHistory.delete_session_async") as delete_async:
            result = self.ctrl.delete_history_session("saved-history", scope)

        self.assertIs(result.status, HistoryDeleteStatus.WRONG_IDB)
        delete_async.assert_not_called()

    def test_delete_history_session_translates_persistence_failure(self) -> None:
        scope = self.ctrl.capture_history_scope(generation=4)
        failed = Future()
        failed.set_exception(PermissionError("locked path"))

        with patch(
            "rikugan.state.history.SessionHistory.delete_session_async",
            return_value=failed,
        ):
            result = self.ctrl.delete_history_session("saved-history", scope)

        self.assertIs(result.status, HistoryDeleteStatus.FAILED)
        self.assertIn("PermissionError", result.error)

    def test_delete_history_session_does_not_swallow_cancelled_error(self) -> None:
        scope = self.ctrl.capture_history_scope(generation=4)
        cancelled = Future()
        cancelled.cancel()

        with patch(
            "rikugan.state.history.SessionHistory.delete_session_async",
            return_value=cancelled,
        ):
            with self.assertRaises(CancelledError):
                self.ctrl.delete_history_session("saved-history", scope)

    def test_tab_label_uses_shared_helper(self) -> None:
        """tab_label must derive from the shared helper at max_chars=20."""
        self.ctrl.session.add_message(Message(role=Role.USER, content="A" * 50))
        label = self.ctrl.tab_label(self.ctrl.active_tab_id)
        self.assertEqual(len(label), 20)
        self.assertTrue(label.startswith("A"))

    def test_tab_label_empty_session_is_new_chat(self) -> None:
        self.assertEqual(self.ctrl.tab_label(self.ctrl.active_tab_id), "New Chat")

    def test_shutdown_is_idempotent(self):
        self.ctrl.shutdown()
        self.ctrl.shutdown()  # Should not raise

    def test_wire_central_memory_routes_existing_workspace_through_backup_helper(self) -> None:
        """Second-run _wire_central_memory must open via open_workspace_for_write."""
        from unittest.mock import MagicMock
        from unittest.mock import patch as mock_patch

        from rikugan.memory import workspace_open
        from rikugan.memory.workspace import FilesystemIdentity

        # Force the controller to bind to a stable identity that the
        # registry will accept; the in-memory mock's filesystem identity
        # would otherwise resolve as "ephemeral" and _wire_central_memory
        # would bail before reaching the open branch.
        self.ctrl._db_instance_id = "a" * 32
        self.ctrl._idb_path = "/fake/test.i64"
        # _wire_central_memory reads from session.idb_path / session.db_instance_id,
        # not from the controller attributes, so mirror the values onto the active
        # tab's session for the duration of this test.
        session = self.ctrl.session
        session.idb_path = self.ctrl._idb_path
        session.db_instance_id = self.ctrl._db_instance_id

        def _fake_fs_identity(_path: str) -> FilesystemIdentity:
            return FilesystemIdentity("vol", "test-volume")

        with mock_patch("rikugan.memory.identity.get_filesystem_identity", _fake_fs_identity):
            # First call seeds memory.db on disk (first-run create path).
            self.ctrl._wire_central_memory(MagicMock())

            calls: list[tuple] = []
            real_open = workspace_open.open_workspace_for_write

            def _tracking_open(paths_arg, owner_arg, backup_dir_arg):
                calls.append((paths_arg, owner_arg, backup_dir_arg))
                return real_open(paths_arg, owner_arg, backup_dir_arg)

            with mock_patch.object(workspace_open, "open_workspace_for_write", _tracking_open):
                self.ctrl._wire_central_memory(MagicMock())

            self.assertEqual(len(calls), 1)
            _paths, owner, _backup_dir = calls[0]
            # Owner memory_id is whatever binding resolved in the first call.
            self.assertTrue(isinstance(owner, str) and len(owner) > 0)

    def test_fork_session_copies_messages(self):
        """Forking should create a new tab with a deep copy of messages."""
        self.ctrl.session.add_message(Message(role=Role.USER, content="hello"))
        self.ctrl.session.add_message(Message(role=Role.ASSISTANT, content="hi"))
        source_tab = self.ctrl.active_tab_id

        new_tab_id = self.ctrl.fork_session(source_tab)
        self.assertIsNotNone(new_tab_id)
        self.assertNotEqual(new_tab_id, source_tab)

        forked = self.ctrl._sessions[new_tab_id]
        self.assertEqual(len(forked.messages), 2)
        self.assertEqual(forked.messages[0].content, "hello")
        self.assertEqual(forked.messages[1].content, "hi")
        self.assertNotEqual(forked.id, self.ctrl.session.id)

    def test_fork_session_deep_copies(self):
        """Modifications to forked session should not affect the original."""
        self.ctrl.session.add_message(Message(role=Role.USER, content="original"))
        source_tab = self.ctrl.active_tab_id

        new_tab_id = self.ctrl.fork_session(source_tab)
        forked = self.ctrl._sessions[new_tab_id]
        forked.add_message(Message(role=Role.USER, content="forked-only"))

        self.assertEqual(len(self.ctrl.session.messages), 1)
        self.assertEqual(len(forked.messages), 2)

    def test_fork_nonexistent_tab_returns_none(self):
        result = self.ctrl.fork_session("nonexistent")
        self.assertIsNone(result)

    def test_fork_records_metadata(self):
        """Forked session should have forked_from metadata."""
        source_tab = self.ctrl.active_tab_id
        source_id = self.ctrl.session.id

        new_tab_id = self.ctrl.fork_session(source_tab)
        forked = self.ctrl._sessions[new_tab_id]
        self.assertEqual(forked.metadata.get("forked_from"), source_id)


class TestEnsureAdvancedToolsReady(unittest.TestCase):
    """Regression tests for ``ensure_advanced_tools_ready``.

    These tests exercise the controller in isolation (no IDA, no
    agent loop) and verify the defensive paths that the
    review remediation plan flagged as commit blockers.
    """

    def _make_controller(self, ensure_tools_ready):
        """Build a bare controller instance for ``ensure_advanced_tools_ready`` tests."""
        cfg = RikuganConfig()
        cfg._config_dir = tempfile.mkdtemp()
        # Use ``SessionControllerBase`` directly so the IDA tool
        # registry / background runtime init are bypassed.
        from rikugan.ui.session_controller_base import SessionControllerBase

        class _StubToolRegistry:
            def set_capabilities(self, _caps):
                return None

        ctrl = SessionControllerBase.__new__(SessionControllerBase)
        ctrl._advanced_tools_registered = False
        ctrl._ensure_tools_ready = ensure_tools_ready
        ctrl._reset_deferred_tools = None
        # ``ensure_advanced_tools_ready`` passes the live
        # ``self.tool_registry`` getter result to the host callback.
        # We don't want to construct a real registry here, so we
        # bypass the property and attach a stub directly.
        ctrl._tool_registry = _StubToolRegistry()
        # ``_make_controller`` does not go through ``setUp/tearDown``
        # (each test method builds its own controller), so register
        # the tempdir cleanup on the test case explicitly.
        self.addCleanup(shutil.rmtree, cfg._config_dir, ignore_errors=True)
        return ctrl

    def test_returns_false_without_raising_when_callback_raises(self):
        """A raising host callback must not propagate — the controller
        returns ``False`` so the retry path can fire later."""

        def _boom(_registry):
            raise RuntimeError("advanced tool registration crashed")

        ctrl = self._make_controller(_boom)
        try:
            result = ctrl.ensure_advanced_tools_ready()
        except NameError as e:
            self.fail(f"ensure_advanced_tools_ready leaked NameError: {e}")
        self.assertFalse(result)
        # Stays un-registered so subsequent calls will retry.
        self.assertFalse(ctrl._advanced_tools_registered)

    def test_returns_true_when_callback_returns_truthy(self):
        """A successful callback marks the controller as registered."""

        class _OkResult:
            ok: bool = True
            registered: int = 7
            failed_modules: ClassVar[list[str]] = []

        ctrl = self._make_controller(lambda _r: _OkResult())
        self.assertTrue(ctrl.ensure_advanced_tools_ready())
        self.assertTrue(ctrl._advanced_tools_registered)

    def test_returns_false_when_callback_reports_failure(self):
        """A callback returning ``ok=False`` must be reported as
        ``False`` (so a retry can be scheduled) but must not raise."""

        class _FailResult:
            ok: bool = False
            registered: int = 3
            failed_modules: ClassVar[list[str]] = ["ida.decompiler"]

        ctrl = self._make_controller(lambda _r: _FailResult())
        self.assertFalse(ctrl.ensure_advanced_tools_ready())
        # Still flagged as not-registered so a retry is possible.
        self.assertFalse(ctrl._advanced_tools_registered)

    def test_short_circuits_when_already_registered(self):
        """After a successful first call, the controller must not
        invoke the host callback a second time."""
        calls: list[int] = []

        class _OkResult:
            ok: bool = True
            registered: int = 1
            failed_modules: ClassVar[list[str]] = []

        def _cb(_registry):
            calls.append(1)
            return _OkResult()

        ctrl = self._make_controller(_cb)
        self.assertTrue(ctrl.ensure_advanced_tools_ready())
        self.assertTrue(ctrl.ensure_advanced_tools_ready())
        self.assertEqual(calls, [1])

    def test_reset_deferred_tools_allows_retry(self):
        """``reset_deferred_tools`` must clear the cached flag so the
        next call to ``ensure_advanced_tools_ready`` re-invokes the
        callback."""

        class _OkResult:
            ok: bool = True
            registered: int = 1
            failed_modules: ClassVar[list[str]] = []

        calls: list[int] = []

        def _cb(_registry):
            calls.append(1)
            return _OkResult()

        ctrl = self._make_controller(_cb)
        self.assertTrue(ctrl.ensure_advanced_tools_ready())
        # Reset must be safe to call even when the controller
        # never provided a ``reset_deferred_tools`` callback.
        ctrl.reset_deferred_tools()
        self.assertFalse(ctrl._advanced_tools_registered)
        self.assertTrue(ctrl.ensure_advanced_tools_ready())
        self.assertEqual(calls, [1, 1])


# ---------------------------------------------------------------------------
# IDA function enumeration import-failure defensive contract.
# ---------------------------------------------------------------------------
#
# These tests pin the contract that a failed IDA import inside
# ``begin_function_enumeration`` / ``next_function_chunk`` does NOT
# leave a stale ``_funcs_iter`` behind.  The review remediation
# plan flagged the previous revision as leaving a stale iterator
# if ``idautils`` / ``ida_funcs`` / ``ida_name`` were missing or
# import-time-broken, so the controller would silently resume
# draining a previous enumeration whose IDA modules no longer
# exist.
#
# We exercise the contract by monkey-patching
# ``importlib.import_module`` so the specific modules used by the
# enumeration methods raise ``ImportError``.


class TestIdaFunctionEnumerationImportFailures(unittest.TestCase):
    """A failed IDA import must not leave a stale enumeration iterator.

    Pinning contract: when ``idautils`` / ``ida_funcs`` / ``ida_name``
    cannot be imported during ``begin_function_enumeration`` or
    ``next_function_chunk``, ``_funcs_iter`` MUST be cleared and the
    ``ImportError`` MUST propagate so callers can recover.

    These tests pass reliably in isolation, in the full suite, and
    under repeat runs (verified 20/20) because they patch
    ``importlib.import_module`` to raise ``ImportError`` for the
    target module name only — earlier test files loading the real
    ``idautils``/``ida_funcs``/``ida_name`` modules into
    :data:`sys.modules` is irrelevant: the patched import path is
    taken regardless of whether the real module is already cached.

    The earlier ``@unittest.expectedFailure`` marker was stale —
    it was added before the production-side fix landed
    (``begin_function_enumeration`` / ``next_function_chunk`` now
    clear ``_funcs_iter`` on import failure) and was retained as a
    stale flag.  Re-evaluated: removed.
    """

    def _make_controller(self):
        from rikugan.ida.ui.session_controller import IdaSessionController

        cfg = RikuganConfig()
        cfg._config_dir = tempfile.mkdtemp()
        # Each test builds its own controller — register the tempdir
        # cleanup on the test case so the per-test tempdir is reclaimed.
        # ``addCleanup`` is LIFO: shutdown runs first (closer in time to
        # the test body), rmtree runs last so any pending async save
        # finishes touching the tempdir before we delete it.
        self.addCleanup(shutil.rmtree, cfg._config_dir, ignore_errors=True)
        self.addCleanup(SessionHistory.flush_saves)
        return IdaSessionController(cfg)

    def test_begin_function_enumeration_clears_state_on_idautils_import_error(self) -> None:
        """If ``idautils`` cannot be imported, ``_funcs_iter`` must
        be ``None`` and the ``ImportError`` must propagate so
        callers can recover."""
        import importlib

        original = importlib.import_module
        call_count = {"n": 0}

        def _failing_import(name, *args, **kwargs):
            call_count["n"] += 1
            if name == "idautils":
                raise ImportError("simulated missing idautils")
            return original(name, *args, **kwargs)

        ctrl = self._make_controller()
        # Pre-set a non-None ``_funcs_iter`` to prove the fix
        # clears it on import failure (the previous revision left
        # the stale reference behind).
        sentinel = iter([0xDEAD, 0xBEEF])
        ctrl._funcs_iter = sentinel

        with unittest.mock.patch.object(importlib, "import_module", side_effect=_failing_import):
            with self.assertRaises(ImportError):
                ctrl.begin_function_enumeration()
        # The fix must clear the stale iterator reference.
        self.assertIsNone(ctrl._funcs_iter)
        self.addCleanup(ctrl.shutdown)

    def test_next_function_chunk_clears_state_on_ida_funcs_import_error(self) -> None:
        """If ``ida_funcs`` / ``ida_name`` cannot be imported during
        a chunk pull, ``_funcs_iter`` must be ``None`` so the next
        enumeration cycle starts from a clean slate."""
        import importlib

        original = importlib.import_module
        fail_ida_funcs = {"on": False}

        def _failing_import(name, *args, **kwargs):
            if name == "ida_funcs" and fail_ida_funcs["on"]:
                raise ImportError("simulated missing ida_funcs")
            return original(name, *args, **kwargs)

        ctrl = self._make_controller()
        # Plant a fake iterator so we can prove the chunk pull is
        # actually mid-enumeration when the import fails.
        fake_iter = iter([0x1000, 0x1100])
        ctrl._funcs_iter = fake_iter

        with unittest.mock.patch.object(importlib, "import_module", side_effect=_failing_import):
            fail_ida_funcs["on"] = True
            with self.assertRaises(ImportError):
                ctrl.next_function_chunk(limit=10)
        # The fix must clear the stale iterator reference, not
        # leave it pointing at the half-drained fake iterator.
        self.assertIsNone(ctrl._funcs_iter)
        self.addCleanup(ctrl.shutdown)

    def test_next_function_chunk_clears_state_on_ida_name_import_error(self) -> None:
        """Symmetric guarantee: an ``ida_name`` import failure also
        clears enumeration state and propagates the exception."""
        import importlib

        original = importlib.import_module
        fail_ida_name = {"on": False}

        def _failing_import(name, *args, **kwargs):
            if name == "ida_name" and fail_ida_name["on"]:
                raise ImportError("simulated missing ida_name")
            return original(name, *args, **kwargs)

        ctrl = self._make_controller()
        ctrl._funcs_iter = iter([0x2000])

        with unittest.mock.patch.object(importlib, "import_module", side_effect=_failing_import):
            fail_ida_name["on"] = True
            with self.assertRaises(ImportError):
                ctrl.next_function_chunk(limit=10)
        self.assertIsNone(ctrl._funcs_iter)
        self.addCleanup(ctrl.shutdown)


class TestUpdateSettingsSkillReload(unittest.TestCase):
    """Perf regression: ``update_settings`` must not reload skills when only
    non-skill config changed.

    Root cause: ``update_settings`` called ``_reload_skills`` (filesystem
    scan via ``SkillRegistry.discover``) unconditionally on every Settings
    OK — including theme-only and provider-only changes. Switching the
    theme or model therefore paid the full skill-discovery cost for no
    reason. The fix tracks a signature of the skill-relevant config
    fields (``enabled_external_skills``, ``disabled_skills``) and skips
    the reload when they are unchanged.
    """

    def setUp(self):
        from unittest.mock import patch

        self._patch = patch
        self.cfg = RikuganConfig()
        self.cfg._config_dir = tempfile.mkdtemp()
        self.ctrl = IdaSessionController(self.cfg)
        # Wait for the background runtime-init thread to finish its own
        # discover() call so the patched discover is the only source of
        # count changes during the test.
        self.ctrl._runtime_init_done.wait(timeout=10)

    def tearDown(self):
        # Flush any pending async saves before tearing the tempdir.
        SessionHistory.flush_saves()
        self.ctrl.shutdown()
        shutil.rmtree(self.cfg._config_dir, ignore_errors=True)

    def test_provider_only_change_does_not_reload_skills(self):
        # Changing only provider/model/theme must NOT trigger a skill
        # filesystem rescan.
        self.cfg.provider.name = "different_provider"
        self.cfg.provider.model = "different_model"
        self.cfg.theme = "dark"

        with self._patch.object(self.ctrl._skill_registry, "discover") as mock_discover:
            self.ctrl.update_settings()

        mock_discover.assert_not_called()

    def test_skill_field_change_reloads_skills(self):
        # Changing enabled_external_skills (a skill-relevant field) must
        # still trigger the reload so newly enabled external skills
        # appear without an IDA restart.
        self.cfg.enabled_external_skills = ["claude:some-skill"]

        with self._patch.object(self.ctrl._skill_registry, "discover") as mock_discover:
            self.ctrl.update_settings()

        mock_discover.assert_called_once()


if __name__ == "__main__":
    unittest.main()
