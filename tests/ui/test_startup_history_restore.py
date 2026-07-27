"""Startup auto-restore tests for the panel's newest-session opener.

Asserts that:
  * When the panel schedules a startup list request and the result
    contains at least one entry, the panel issues a hidden load for
    the newest entry and clears the startup flags.
  * When the startup list is empty or the load fails, the panel keeps
    the blank draft tab and does not surface a History error.
  * A subsequent IDB-switch invalidation clears the startup flags so an
    old-IDB result cannot attach a session to a new IDB.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from tests.qt_stubs import ensure_pyside6_stubs  # noqa: E402

ensure_pyside6_stubs()

from rikugan.state.history_types import (  # noqa: E402
    HistoryListResult,
    HistoryLoadResult,
    HistoryRequestStatus,
    HistoryScope,
    SessionHistoryEntry,
)


def _build_panel():
    from rikugan.ui.panel_core import RikuganPanelCore

    panel = RikuganPanelCore.__new__(RikuganPanelCore)
    panel._history_panel = MagicMock()
    panel._history_generation = 1
    panel._history_pending = False
    panel._history_delete_intents = set()
    panel._history_retry_delete_session_id = None
    panel._history_last_delete_session_id = None
    panel._history_retry_load_session_id = None
    panel._history_last_load_session_id = None
    panel._history_result_queue = MagicMock()
    panel._history_result_queue.get_nowait.side_effect = queue.Empty
    panel._is_shutdown = False
    panel._startup_restore_pending = True
    panel._startup_restore_load_pending = False
    return panel


def _list_result(entries, scope):
    return HistoryListResult(HistoryRequestStatus.LISTED, scope, tuple(entries))


def _entry(session_id, updated_at, title="t"):
    return SessionHistoryEntry(
        session_id=session_id,
        title=title,
        created_at=0.0,
        updated_at=updated_at,
        provider="p",
        model="m",
        message_count=1,
    )


def _build_loaded_result(status, session=None):
    return HistoryLoadResult(
        status,
        HistoryScope(idb_path="", db_instance_id="abc", generation=1),
        session=session,
        error="",
    )


class TestStartupRestoreListResult(unittest.TestCase):
    def test_listed_with_entries_triggers_load(self) -> None:
        panel = _build_panel()
        scope = HistoryScope(idb_path="", db_instance_id="abc", generation=1)
        older = _entry("older", updated_at=10.0)
        newer = _entry("newer", updated_at=20.0)
        with patch.object(panel, "_start_history_load") as start_load:
            panel._apply_history_list_result(_list_result([older, newer], scope))
        start_load.assert_called_once_with("newer")
        self.assertFalse(panel._startup_restore_pending)
        self.assertTrue(panel._startup_restore_load_pending)
        panel._history_panel.set_entries.assert_not_called()
        panel._history_panel.set_error.assert_not_called()

    def test_listed_empty_does_not_load_or_touch_panel(self) -> None:
        panel = _build_panel()
        scope = HistoryScope(idb_path="", db_instance_id="abc", generation=1)
        with patch.object(panel, "_start_history_load") as start_load:
            panel._apply_history_list_result(_list_result([], scope))
        start_load.assert_not_called()
        self.assertFalse(panel._startup_restore_pending)
        self.assertFalse(panel._startup_restore_load_pending)
        panel._history_panel.set_entries.assert_not_called()
        panel._history_panel.set_error.assert_not_called()

    def test_failed_list_does_not_load_or_touch_panel(self) -> None:
        panel = _build_panel()
        scope = HistoryScope(idb_path="", db_instance_id="abc", generation=1)
        result = HistoryListResult(HistoryRequestStatus.FAILED, scope, error="boom")
        with patch.object(panel, "_start_history_load") as start_load:
            panel._apply_history_list_result(result)
        start_load.assert_not_called()
        self.assertFalse(panel._startup_restore_pending)
        self.assertFalse(panel._startup_restore_load_pending)
        panel._history_panel.set_entries.assert_not_called()
        panel._history_panel.set_error.assert_not_called()

    def test_save_flush_timeout_does_not_load_or_touch_panel(self) -> None:
        panel = _build_panel()
        scope = HistoryScope(idb_path="", db_instance_id="abc", generation=1)
        result = HistoryListResult(HistoryRequestStatus.SAVE_FLUSH_TIMEOUT, scope)
        with patch.object(panel, "_start_history_load") as start_load:
            panel._apply_history_list_result(result)
        start_load.assert_not_called()
        self.assertFalse(panel._startup_restore_pending)
        self.assertFalse(panel._startup_restore_load_pending)
        panel._history_panel.set_entries.assert_not_called()
        panel._history_panel.set_error.assert_not_called()


class TestStartupLoadFailureClearsFlag(unittest.TestCase):
    def _build_panel(self):
        from rikugan.ui.panel_core import RikuganPanelCore

        panel = RikuganPanelCore.__new__(RikuganPanelCore)
        panel._is_shutdown = False
        panel._history_panel = MagicMock()
        panel._history_delete_intents = set()
        panel._history_retry_load_session_id = None
        panel._history_last_load_session_id = None
        panel._startup_restore_load_pending = True
        panel._startup_restore_pending = False
        panel._ctrl = MagicMock()
        return panel

    def test_load_not_found_clears_flag(self) -> None:
        panel = self._build_panel()
        result = _build_loaded_result(HistoryRequestStatus.NOT_FOUND)
        panel._apply_history_loaded(result)
        self.assertFalse(panel._startup_restore_load_pending)
        panel._history_panel.set_error.assert_not_called()

    def test_load_wrong_idb_clears_flag(self) -> None:
        panel = self._build_panel()
        result = _build_loaded_result(HistoryRequestStatus.WRONG_IDB)
        panel._apply_history_loaded(result)
        self.assertFalse(panel._startup_restore_load_pending)
        panel._history_panel.set_error.assert_not_called()


class TestInvalidateClearsStartupFlags(unittest.TestCase):
    def test_invalidate_resets_both_flags(self) -> None:
        from rikugan.ui.panel_core import RikuganPanelCore

        panel = RikuganPanelCore.__new__(RikuganPanelCore)
        panel._startup_restore_pending = True
        panel._startup_restore_load_pending = True
        # Minimal state the invalidate helper touches.
        panel._history_generation = 1
        panel._history_closing = threading.Event()
        panel._history_pending = False
        panel._history_retry_load_session_id = None
        panel._history_last_load_session_id = None
        panel._history_retry_delete_session_id = None
        panel._history_last_delete_session_id = None
        panel._history_delete_intents = set()
        panel._history_executor = None
        panel._history_poll_timer = None
        panel._history_panel = None
        panel._stop_history_delete_watchdog = lambda: None
        panel._stop_history_poll_timer = lambda: None
        panel._history_result_queue = MagicMock()
        panel._history_result_queue.get_nowait.side_effect = queue.Empty
        panel._invalidate_history(clear_panel=False)
        self.assertFalse(panel._startup_restore_pending)
        self.assertFalse(panel._startup_restore_load_pending)


if __name__ == "__main__":
    unittest.main()
