"""End-to-end tests for the startup auto-restore outcome.

The user has reversed the 1.12.0 "fresh blank New Chat on open"
decision: on open, the panel must surface the most recent saved
session for the current IDB (or, if none exists, leave the blank
draft).  The exact trigger (which method calls
``_start_history_list_request`` at construction time) is owned by
the StartupRestore slice; this test asserts the OUTCOME so a future
refactor of the trigger does not break the contract.

The test drives the real ``_apply_history_list_result`` +
``_apply_history_loaded`` sequence on a real ``RikuganPanelCore``
(via ``__new__`` + ``QWidget.__init__``, the established repo
idiom) seeded with a stub controller and a real ``ChatView`` for
the draft tab.  The OUTCOME assertion is the visible widget count
on the ChatView: after the probe completes, the chat must either
hold the persisted session's messages (one persisted session case)
or be empty (no persisted sessions case).

Two cases:

  1. ``test_startup_with_persisted_sessions_renders_most_recent``:
     the list result contains two persisted sessions; the load path
     surfaces the newest one.  The ChatView must end up with both
     messages rendered.

  2. ``test_startup_without_persisted_sessions_keeps_blank_draft``:
     the list result is empty; the chat must remain the empty
     draft tab (no ``_pending_restore_messages``, no widget land).

A user-initiated History open that races the startup probe is NOT
exercised here (covered by the integration test) — this test
focuses on the cold-start outcome only.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

# Headless test environments need an offscreen Qt platform.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Re-import safety: drop any ``types.ModuleType`` stubs a sibling test
# may have left behind so we import the real chat_view /
# history_panel / panel_core.
import sys as _sys

_STUB_TARGETS = (
    "rikugan.core.types",
    "rikugan.core.config",
    "rikugan.core.host",
    "rikugan.agent.turn",
    "rikugan.ui.chat_view",
    "rikugan.ui.history_panel",
    "rikugan.ui.panel_core",
    "rikugan.ui.session_controller_base",
    "rikugan.ui.styles",
    "rikugan.ui.theme",
    "rikugan.ui.theme.manager",
    "rikugan.ui.theme.tokens",
    "rikugan.ui.theme.palette_dark",
    "rikugan.ui.theme.palette_light",
    "rikugan.ui.theme.palette_ida",
    "rikugan.ui.markdown",
    "rikugan.ui.message_widgets",
    "rikugan.ui.plan_view",
    "rikugan.ui.tool_widgets",
    "rikugan.ui.qt_compat",
    "rikugan.ui.input_area",
    "rikugan.ui.context_bar",
)
for _name in list(_sys.modules):
    if _name in _STUB_TARGETS:
        _sys.modules.pop(_name, None)

# Repair real ``QFont`` if a sibling test clobbered it.
try:
    import PySide6  # type: ignore[import-not-found]

    _real_qfont = getattr(PySide6, "_real_qfont_backup", None)
    if _real_qfont is not None:
        import PySide6.QtGui  # type: ignore[import-not-found]

        PySide6.QtGui.QFont = _real_qfont
except ImportError:
    pass

from rikugan.core.types import Message, Role
from rikugan.state.history_types import (
    HistoryAttachResult,
    HistoryAttachStatus,
    HistoryListResult,
    HistoryLoadResult,
    HistoryRequestStatus,
    HistoryScope,
    SessionHistoryEntry,
)
from rikugan.ui.message_widgets import (
    AssistantMessageWidget,
    UserMessageWidget,
)
from tests.qt_real import live_class, requires_real_qt


def _user_widget_cls() -> type:
    """Live ``UserMessageWidget`` class.

    Resolved per call: sibling test files purge ``rikugan.ui.*`` from
    ``sys.modules`` at import time, so a class captured at this
    module's import can be a different object from the one the live
    widget tree was built from. ``findChildren`` matches by exact type,
    so a stale reference reports zero widgets on a fully painted chat.
    """
    return live_class("rikugan.ui.message_widgets.UserMessageWidget")


def _assistant_widget_cls() -> type:
    """Live ``AssistantMessageWidget`` class — see :func:`_user_widget_cls`."""
    return live_class("rikugan.ui.message_widgets.AssistantMessageWidget")


def _chat_view_cls() -> type:
    """Live ``ChatView`` class — see :func:`_user_widget_cls`."""
    return live_class("rikugan.ui.chat_view.ChatView")


def _entry(session_id: str, title: str, updated_at: float) -> SessionHistoryEntry:
    return SessionHistoryEntry(
        session_id=session_id,
        title=title,
        created_at=0.0,
        updated_at=updated_at,
        provider="",
        model="",
        message_count=2,
    )


def _user_message(content: str, msg_id: str = "") -> Message:
    msg = Message(role=Role.USER, content=content)
    msg.id = msg_id
    return msg


def _assistant_message(content: str, msg_id: str = "") -> Message:
    msg = Message(role=Role.ASSISTANT, content=content)
    msg.id = msg_id
    return msg


@requires_real_qt
class TestStartupOutcome(unittest.TestCase):
    """Drives ``_apply_history_list_result`` then
    ``_apply_history_loaded`` end-to-end against a real
    ``RikuganPanelCore`` + real ``ChatView`` and asserts the
    visible chat outcome.

    A real ``ChatView`` (not the ``__new__`` harness) is used so
    ``findChildren(_user_widget_cls())`` walks real widgets — the
    same surface the user actually sees.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls._qapp = QApplication.instance() or QApplication([])

    def _seed_session_controller_globals(self) -> None:
        """``SessionControllerBase.__init__`` populates lazy module
        globals ``SessionHistory`` and ``SessionState`` so worker
        ``SessionHistory(...)`` calls resolve.  When ``__init__`` is
        bypassed (via ``__new__``), those globals stay ``None`` and
        the list worker blows up with ``TypeError`` which the broad
        ``except Exception`` silently swallows.  Seed them here.
        """
        import rikugan.ui.session_controller_base as scb

        if scb.SessionHistory is None:
            from rikugan.state.history import SessionHistory

            scb.SessionHistory = SessionHistory
        if scb.SessionState is None:
            from rikugan.state.session import SessionState

            scb.SessionState = SessionState

    def _build_panel_for_outcome_test(
        self,
        *,
        newer_session_messages: list[Message] | None,
        older_session_messages: list[Message] | None,
    ):
        """Build a real ``RikuganPanelCore`` (via ``__new__`` +
        ``QWidget.__init__``) seeded with a stub controller that
        returns canned list / load results.

        When ``newer_session_messages`` is not ``None``, the load
        result returns those messages (so the OPENED branch renders
        them in the new tab).  When ``None``, the load result is
        FAILED (the most-recent-session restore path produced no
        usable session).

        Returns ``(panel, draft_chat_view)`` so the test can drive
        the apply path and inspect the draft + new tabs.
        """
        from PySide6.QtWidgets import QApplication, QTabWidget, QWidget

        from rikugan.ui.chat_view import ChatView
        from rikugan.ui.history_panel import HistoryPanel
        from rikugan.ui.panel_core import RikuganPanelCore

        self._seed_session_controller_globals()

        panel = RikuganPanelCore.__new__(RikuganPanelCore)
        QWidget.__init__(panel)

        # Build a real ChatView for the draft tab — the panel reads
        # ``_chat_views[active_tab_id]`` when the user opens history
        # OR when the startup probe creates a new tab.  A real view
        # (not a MagicMock) lets ``findChildren`` walk real widgets.
        draft_view = _chat_view_cls()()
        draft_view.resize(640, 480)
        # NOTE: no ``addCleanup(draft_view.deleteLater)`` here. Teardown
        # is owned by ``_teardown_panel`` below, which shuts the view
        # down and then deletes it in the correct order (executor join
        # first). A separate cleanup would run first under LIFO and
        # destroy the C++ object while the panel still references it.

        # Coordinator fields the click / startup chain touches.
        panel._is_shutdown = False
        panel._history_panel = HistoryPanel()  # Hidden by default — startup path.
        panel._history_btn = MagicMock()
        panel._mutation_panel = MagicMock()
        panel._mutations_btn = MagicMock()
        panel._tab_widget = QTabWidget()
        panel._tab_bar = panel._tab_widget.tabBar()
        panel._chat_views = {}
        panel._pending_restore_messages = {}
        panel._ctx_bar = MagicMock()
        panel._count_label = MagicMock()
        # History coordinator state.
        import queue as _queue
        import threading

        panel._history_generation = 0
        panel._history_pending = False
        panel._history_executor = None
        panel._history_poll_timer = None
        panel._history_closing = threading.Event()
        panel._history_result_queue = _queue.Queue()
        panel._history_retry_load_session_id = None
        panel._history_last_load_session_id = None
        panel._history_delete_intents = set()
        panel._history_retry_delete_session_id = None
        panel._history_last_delete_session_id = None
        panel._history_delete_watchdog = None
        # Startup auto-restore flags. ``__init__`` normally
        # initialises these; ``__new__`` bypasses it so we seed
        # them by hand.
        panel._startup_restore_pending = False
        panel._startup_restore_load_pending = False
        panel._stop_history_poll_timer = lambda: None  # type: ignore[assignment]
        panel._stop_history_delete_watchdog = lambda: None  # type: ignore[assignment]
        # Hidden history panel — startup probe runs before the user opens
        # the panel, so the visibility guard must yield to the probe.
        panel._history_panel.setVisible(False)
        # Stub controller that returns canned list / load results.
        ctrl = MagicMock()
        # A bare ``MagicMock()`` config stringifies (its ``__fspath__``)
        # into the RELATIVE path ``MagicMock/mock.config/<id>``, so the
        # ``SessionHistory`` the list worker builds would mkdir junk
        # inside the repo.  Bind a real config on a tempdir instead.
        from rikugan.core.config import RikuganConfig

        config = RikuganConfig()
        config._config_dir = tempfile.mkdtemp(prefix="rikugan-startup-cfg-")
        self.addCleanup(shutil.rmtree, config._config_dir, ignore_errors=True)
        ctrl.config = config
        ctrl.active_tab_id = "draft-tab"

        def _capture_scope(generation: int) -> HistoryScope:
            return HistoryScope(
                idb_path="",
                db_instance_id="test-instance",
                generation=generation,
            )

        ctrl.capture_history_scope.side_effect = _capture_scope
        ctrl.find_tab_for_session.return_value = None  # always take the load path

        def _load_session(session_id: str, scope: HistoryScope) -> HistoryLoadResult:
            # Return the messages for the requested session id (newer
            # is the candidate the startup probe picks).
            from rikugan.state.session import SessionState

            if newer_session_messages is not None and session_id == "newer":
                session = SessionState(idb_path="", db_instance_id="test-instance")
                session.id = session_id
                for msg in newer_session_messages:
                    session.add_message(msg)
                return HistoryLoadResult(
                    HistoryRequestStatus.LOADED,
                    scope,
                    session=session,
                )
            return HistoryLoadResult(HistoryRequestStatus.FAILED, scope, error="")

        ctrl.load_history_session.side_effect = _load_session

        def _attach(result: HistoryLoadResult) -> HistoryAttachResult:
            if result.status is HistoryRequestStatus.LOADED and result.session is not None:
                return HistoryAttachResult(
                    status=HistoryAttachStatus.OPENED,
                    tab_id="history-tab",
                    session=result.session,
                )
            # FAILED — no tab.
            return HistoryAttachResult(
                status=HistoryAttachStatus.STALE_SCOPE,
            )

        ctrl.attach_history_session.side_effect = _attach
        ctrl.tab_label.return_value = "Loaded chat"
        panel._ctrl = ctrl

        # Seed the draft tab so the OPENED branch creates a NEW tab
        # alongside it (mirrors production's ``_create_tab`` initial
        # call in ``_build_ui``).
        draft_view.setProperty("tab_id", ctrl.active_tab_id)
        panel._chat_views[ctrl.active_tab_id] = draft_view
        panel._tab_widget.addTab(draft_view, "New Chat")
        # Tear down deterministically: a history load worker that
        # outlives the test can touch Qt objects after they are gone
        # (the Shiboken use-after-free in AGENTS.md §1, observed as a
        # native access violation when a sibling real-Qt file runs
        # first). Shutting the executor down and stopping every
        # ChatView here keeps the worker joined before teardown.
        self.addCleanup(self._teardown_panel, panel)
        return panel, draft_view

    @staticmethod
    def _teardown_panel(panel) -> None:
        """Stop the history executor and every ChatView before teardown.

        Runs under ``addCleanup`` LIFO, i.e. AFTER sibling cleanups that
        may already have scheduled widgets for deletion — so every Qt
        touch is guarded: a widget whose C++ object is gone raises
        ``RuntimeError`` on access, and that must not abort the rest of
        the teardown (the executor join especially).
        """
        executor = getattr(panel, "_history_executor", None)
        if executor is not None:
            try:
                executor.shutdown(wait=True)
            except Exception:  # defensive: never block teardown
                pass
            panel._history_executor = None
        for view in list(getattr(panel, "_chat_views", {}).values()):
            try:
                view.shutdown()
            except (RuntimeError, AttributeError, TypeError):
                # C++ object already deleted — nothing left to stop.
                pass
            try:
                view.deleteLater()
            except (RuntimeError, AttributeError, TypeError):
                pass
        panel._chat_views.clear()

    def _pump_for_async_restore(
        self, chat_view, expected_user: int, expected_assistant: int, timeout_s: float = 2.0
    ) -> tuple[int, int]:
        """Pump the event loop until ``chat_view``'s async restore
        paints the expected number of user/assistant widgets.

        Same deterministic pump as the other new tests — bounded
        ``processEvents`` loop with a deadline, never ``time.sleep``
        as the primary wait primitive.
        """
        from PySide6.QtWidgets import QApplication

        import time as _time

        app = QApplication.instance()
        deadline = _time.monotonic() + timeout_s
        last_user = -1
        last_assistant = -1
        while _time.monotonic() < deadline:
            app.processEvents()
            last_user = len(chat_view.findChildren(_user_widget_cls()))
            last_assistant = len(chat_view.findChildren(_assistant_widget_cls()))
            if last_user >= expected_user and last_assistant >= expected_assistant:
                # Spin a few more to flush late queue items.
                for _ in range(5):
                    app.processEvents()
                return last_user, last_assistant
            _time.sleep(0.001)
        return last_user, last_assistant

    def _drive_startup_probe(self, panel, list_entries: list[SessionHistoryEntry]) -> None:
        """Simulate the full startup probe sequence end-to-end.

        1. Call the production trigger method (whatever name the
           StartupRestore slice chose — see ``_startup_trigger_names``
           below).  The method sets ``_startup_restore_pending`` and
           submits a list request via ``_start_history_list_request``.
        2. Build a canned ``HistoryListResult`` (with the matching
           generation) and inject it directly into the result queue.
        3. Drive ``_drain_history_results`` — the apply path
           (``_apply_history_list_result``) routes through the
           startup branch.
        4. If entries exist, the startup branch fires
           ``_start_history_load(newest)`` and sets
           ``_startup_restore_load_pending = True``.
        5. Build a canned ``HistoryLoadResult`` (from the stub
           controller's ``load_history_session`` side_effect) and
           inject it directly into the queue.  Drive the drain
           again — the apply path (``_apply_history_loaded``)
           routes through the startup-load branch.

        We avoid the real ``ThreadPoolExecutor`` (which races the
        test's shutdown) by injecting both results into the queue
        directly.  This mirrors the production data flow exactly
        without depending on async worker timing.
        """
        import queue as _queue

        # Call whichever trigger method name the StartupRestore slice
        # chose.  ``getattr`` with ``default=`` keeps the test
        # resilient to a future rename — the trigger is the panel's
        # responsibility, the OUTCOME is ours.
        trigger_names = (
            "_arm_startup_restore_if_idle",
            "_arm_startup_restore",
            "_trigger_startup_restore",
        )
        called_trigger = None
        for name in trigger_names:
            method = getattr(panel, name, None)
            if callable(method):
                method()
                called_trigger = name
                break
        if called_trigger is None:
            self.fail(
                f"Panel has no startup-restore trigger method. Looked for: "
                f"{trigger_names}. The StartupRestore slice must add one "
                f"(e.g. via QTimer.singleShot(0, ...) at the end of "
                f"_build_ui) so the OUTCOME assertions below can fire."
            )
        self.assertTrue(
            panel._startup_restore_pending,
            f"{called_trigger} must set _startup_restore_pending so the "
            f"next list result routes through the startup branch.",
        )
        # Build a canned list result.  Use the NEW generation that
        # ``_start_history_list_request`` bumped so the drain's
        # generation-aware guard accepts it.
        list_scope = HistoryScope(
            idb_path="",
            db_instance_id="test-instance",
            generation=panel._history_generation,
        )
        list_result = HistoryListResult(
            HistoryRequestStatus.LISTED,
            list_scope,
            tuple(list_entries),
        )
        # Clear the queue (defensive — production normally has at
        # most one entry at a time, but the test may be re-using a
        # panel across cases).
        try:
            while True:
                panel._history_result_queue.get_nowait()
        except _queue.Empty:
            pass
        panel._history_result_queue.put(list_result)
        # Drive the drain.  The drain auto-clears ``_history_pending``
        # BEFORE applying; the apply-side ``_start_history_load``
        # sets it True again if a load is submitted.
        panel._history_pending = True
        panel._drain_history_results()
        if list_entries:
            self.assertTrue(
                panel._startup_restore_load_pending,
                "After the list result lands, the startup branch must "
                "set _startup_restore_load_pending and submit a load "
                "for the newest entry.",
            )
            # The startup branch submitted a load via the real
            # ``_start_history_load`` path.  Build the canned load
            # result the stub controller's side_effect produces and
            # inject it directly into the queue.
            newest_entry = max(list_entries, key=lambda e: e.updated_at)
            newest_id = newest_entry.session_id
            canned = panel._ctrl.load_history_session(newest_id, list_scope)
            if canned.status is HistoryRequestStatus.LOADED and canned.session is not None:
                load_scope = HistoryScope(
                    idb_path="",
                    db_instance_id="test-instance",
                    generation=panel._history_generation,
                )
                load_result = HistoryLoadResult(
                    HistoryRequestStatus.LOADED,
                    load_scope,
                    session=canned.session,
                )
                panel._history_result_queue.put(load_result)
                panel._history_pending = True
                panel._drain_history_results()

    def test_startup_with_persisted_sessions_renders_most_recent(self) -> None:
        """Outcome (positive case): when two persisted sessions exist,
        the startup probe surfaces the newest one in the chat view.

        The OUTCOME is asserted via ``findChildren`` — both the
        ``UserMessageWidget`` and ``AssistantMessageWidget`` from the
        loaded session must land on the new tab.  The draft tab
        remains untouched (still empty).
        """
        newer_messages = [
            _user_message("newer-question", msg_id="u-newer"),
            _assistant_message("newer-answer", msg_id="a-newer"),
        ]
        panel, draft_view = self._build_panel_for_outcome_test(
            newer_session_messages=newer_messages,
            older_session_messages=[
                _user_message("older-question", msg_id="u-older"),
                _assistant_message("older-answer", msg_id="a-older"),
            ],
        )

        entries = [
            _entry("older", "Old chat", updated_at=10.0),
            _entry("newer", "Newer chat", updated_at=20.0),
        ]
        self._drive_startup_probe(panel, entries)

        # The OPENED attach created a new tab.  Pump its async restore.
        new_view = panel._chat_views.get("history-tab")
        self.assertIsNotNone(
            new_view,
            "After the startup probe, the OPENED attach must create a "
            "new ChatView tab whose _chat_views entry is 'history-tab'.",
        )
        user_count, assistant_count = self._pump_for_async_restore(
            new_view, expected_user=1, expected_assistant=1, timeout_s=2.0
        )
        self.assertGreaterEqual(
            user_count,
            1,
            "The newer session's USER message must paint a UserMessageWidget on the new tab.",
        )
        self.assertGreaterEqual(
            assistant_count,
            1,
            "The newer session's ASSISTANT message must paint an AssistantMessageWidget on the new tab.",
        )
        # The draft tab must remain untouched — the OLDER session's
        # content must NOT leak into the draft tab.
        draft_user = len(draft_view.findChildren(_user_widget_cls()))
        draft_assistant = len(draft_view.findChildren(_assistant_widget_cls()))
        self.assertEqual(
            draft_user,
            0,
            "The draft tab must remain empty; the startup probe must "
            "NOT paint anything on the draft tab (it creates a NEW tab).",
        )
        self.assertEqual(
            draft_assistant,
            0,
            "The draft tab must remain empty; the startup probe must "
            "NOT paint anything on the draft tab (it creates a NEW tab).",
        )

    def test_startup_without_persisted_sessions_keeps_blank_draft(self) -> None:
        """Outcome (negative case): when no persisted sessions exist,
        the startup probe leaves the blank draft tab intact and does
        not surface any error.
        """
        panel, draft_view = self._build_panel_for_outcome_test(
            newer_session_messages=None,
            older_session_messages=None,
        )

        # Empty list — no entries.
        self._drive_startup_probe(panel, [])

        # The draft tab must remain empty.
        draft_user = len(draft_view.findChildren(_user_widget_cls()))
        draft_assistant = len(draft_view.findChildren(_assistant_widget_cls()))
        self.assertEqual(
            draft_user,
            0,
            "With no persisted sessions, the draft tab must remain empty.",
        )
        self.assertEqual(
            draft_assistant,
            0,
            "With no persisted sessions, the draft tab must remain empty.",
        )
        # No new tab was created (no entry → no load → no OPENED).
        self.assertEqual(
            len(panel._chat_views),
            1,
            "With no persisted sessions, no new tab must be created; only the draft tab remains.",
        )
        # The startup flag must have been consumed.
        self.assertFalse(
            panel._startup_restore_pending,
            "The startup flag must be cleared once the empty list result is applied.",
        )
        self.assertFalse(
            panel._startup_restore_load_pending,
            "No load was submitted (empty list), so the load-pending flag must remain False.",
        )


if __name__ == "__main__":
    unittest.main()
