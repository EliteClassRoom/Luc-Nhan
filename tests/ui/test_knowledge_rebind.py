"""Knowledge-tab rebind tests: history load / tab switch / IDB change.

The Knowledge panel reads the *active session's* per-binary store, so
it must re-populate whenever the active binary may have changed: a
loaded chat history, a tab switch, and an IDB change must all trigger
the debounced ``_on_knowledge_event_refresh`` hook. These tests pin
the three call sites with bare-panel fixtures (no Qt event loop, no
controller) following the ``test_startup_history_restore`` pattern.
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from tests.qt_stubs import ensure_pyside6_stubs  # noqa: E402

ensure_pyside6_stubs()

from rikugan.state.history_types import (  # noqa: E402
    HistoryAttachStatus,
    HistoryLoadResult,
    HistoryRequestStatus,
    HistoryScope,
)


def _build_loaded_result(status, session=None):
    return HistoryLoadResult(
        status,
        HistoryScope(idb_path="", db_instance_id="abc", generation=1),
        session=session,
        error="",
    )


class TestHistoryLoadRebindsKnowledge(unittest.TestCase):
    def _build_panel(self):
        from rikugan.ui.panel_core import RikuganPanelCore

        panel = RikuganPanelCore.__new__(RikuganPanelCore)
        panel._is_shutdown = False
        panel._history_panel = MagicMock()
        panel._history_delete_intents = set()
        panel._history_retry_load_session_id = None
        panel._history_last_load_session_id = None
        panel._startup_restore_load_pending = False
        panel._startup_restore_pending = False
        panel._pending_restore_messages = {}
        panel._ctrl = MagicMock()
        return panel

    def test_loaded_opened_session_triggers_knowledge_refresh(self) -> None:
        panel = self._build_panel()
        session = SimpleNamespace(id="s1", messages=[])
        result = _build_loaded_result(HistoryRequestStatus.LOADED, session=session)
        panel._ctrl.attach_history_session = MagicMock(
            return_value=SimpleNamespace(status=HistoryAttachStatus.OPENED, tab_id="t1", session=session)
        )
        with (
            patch.object(panel, "_create_tab"),
            patch.object(panel, "_restore_messages_if_needed"),
            patch.object(panel, "_focus_tab"),
            patch.object(panel, "_on_knowledge_event_refresh") as refresh,
        ):
            panel._apply_history_loaded(result)
        refresh.assert_called_once_with("history_loaded")
        self.assertIsNone(panel._history_retry_load_session_id)

    def test_loaded_already_open_session_triggers_knowledge_refresh(self) -> None:
        panel = self._build_panel()
        session = SimpleNamespace(id="s2", messages=[])
        result = _build_loaded_result(HistoryRequestStatus.LOADED, session=session)
        panel._ctrl.attach_history_session = MagicMock(
            return_value=SimpleNamespace(status=HistoryAttachStatus.ALREADY_OPEN, tab_id="t2", session=session)
        )
        with (
            patch.object(panel, "_focus_tab"),
            patch.object(panel, "_on_knowledge_event_refresh") as refresh,
        ):
            panel._apply_history_loaded(result)
        refresh.assert_called_once_with("history_loaded")

    def test_non_loaded_status_does_not_refresh(self) -> None:
        panel = self._build_panel()
        result = _build_loaded_result(HistoryRequestStatus.NOT_FOUND)
        with (
            patch.object(panel, "_start_history_list_request"),
            patch.object(panel, "_on_knowledge_event_refresh") as refresh,
        ):
            panel._apply_history_loaded(result)
        refresh.assert_not_called()


class TestTabChangeRebindsKnowledge(unittest.TestCase):
    def test_tab_switch_triggers_knowledge_refresh(self) -> None:
        from rikugan.ui.panel_core import RikuganPanelCore

        panel = RikuganPanelCore.__new__(RikuganPanelCore)
        panel._is_shutdown = False
        panel._chat_views = {}
        panel._ctrl = MagicMock()
        with (
            patch.object(panel, "_tab_id_at_index", return_value="t1") as tid,
            patch.object(panel, "_restore_messages_if_needed"),
            patch.object(panel, "_update_token_display"),
            patch.object(panel, "_on_knowledge_event_refresh") as refresh,
        ):
            panel._on_tab_changed(1)
        tid.assert_called_once_with(1)
        panel._ctrl.switch_tab.assert_called_once_with("t1")
        refresh.assert_called_once_with("tab_changed")

    def test_negative_index_does_not_refresh(self) -> None:
        from rikugan.ui.panel_core import RikuganPanelCore

        panel = RikuganPanelCore.__new__(RikuganPanelCore)
        panel._is_shutdown = False
        with patch.object(panel, "_on_knowledge_event_refresh") as refresh:
            panel._on_tab_changed(-1)
        refresh.assert_not_called()


class TestDatabaseChangeRebindsKnowledge(unittest.TestCase):
    def test_idb_change_triggers_knowledge_refresh(self) -> None:
        from rikugan.ui.panel_core import RikuganPanelCore

        panel = RikuganPanelCore.__new__(RikuganPanelCore)
        panel._is_shutdown = False
        panel._ctrl = MagicMock()
        panel._ctrl._idb_path = "OLD"
        panel._chat_views = {}
        panel._pending_restore_messages = {}
        panel._tab_widget = MagicMock()
        panel._tab_widget.count.return_value = 0
        # ``_invalidate_history`` (called by on_database_changed) touches
        # these directly; seed them like test_startup_history_restore does.
        panel._history_executor = None
        panel._history_poll_timer = None
        with (
            patch.object(panel, "_create_tab") as create,
            patch.object(panel, "_on_knowledge_event_refresh") as refresh,
        ):
            panel.on_database_changed("")
        create.assert_called_once()
        refresh.assert_called_once_with("database_changed")

    def test_same_idb_does_not_refresh(self) -> None:
        from rikugan.ui.panel_core import RikuganPanelCore

        panel = RikuganPanelCore.__new__(RikuganPanelCore)
        panel._is_shutdown = False
        panel._ctrl = MagicMock()
        target = os.path.normcase(os.path.realpath(os.path.abspath("C:\\SAME")))
        panel._ctrl._idb_path = target
        with patch.object(panel, "_on_knowledge_event_refresh") as refresh:
            panel.on_database_changed("C:\\SAME")
        refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
