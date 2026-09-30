"""Regression tests for the History row-click wiring.

Bug 1 (the click wiring):

  The ``HistoryPanel`` exposes a ``session_open_requested(str)``
  signal that is connected to ``RikuganPanelCore._on_history_open_requested``
  in ``_build_main_splitter`` (panel_core.py:788).  When the user
  clicks a row, ``HistoryRowWidget.mouseReleaseEvent`` emits the
  signal with the bound entry's session id, and PanelCore looks up
  the persisted session, attaches it to a tab, and replays the
  messages through ``ChatView.restore_from_messages_async``.

The existing ``tests/integration/test_history_on_demand.py`` test
short-circuits this wiring: it calls ``self._panel._on_history_open_requested``
DIRECTLY and replaces every widget with a recording fake, so the
row click → row signal → panel signal → PanelCore slot → controller
load → ChatView restore chain is never exercised end-to-end.  A
regression that breaks the row signal connection (e.g. drops the
``row.session_open_requested.connect(self.session_open_requested.emit)``
in ``HistoryPanel._render_rows``) is invisible to that test.

These tests exercise the REAL click chain:

  1. A REAL ``HistoryPanel`` is built and ``set_entries`` is called.
  2. A REAL ``QMouseEvent`` / ``QTest.mouseClick`` is sent to a
     ``HistoryRowWidget`` (the kind of event PySide6 dispatches in
     production).
  3. The captured ``session_open_requested`` payload is asserted to
     carry the right session id (wiring 1: row → panel signal).
  4. The same click is sent through a REAL ``RikuganPanelCore``
     (built via the ``__new__`` idiom that bypasses ``__init__`` —
     the established pattern in this repo) and asserted to reach the
     ``ChatView`` whose async restore paints the loaded messages
     (wiring 2: panel signal → PanelCore → controller → chat view).

The pattern matches the existing repo convention: heavy ``__init__``
is bypassed, the coordinator fields the click chain actually touches
are seeded (``_tab_bar`` from ``_tab_widget.tabBar()``, ``_history_btn``,
``_mutation_panel``, ``_mutations_btn``, etc.), and the lazy
``SessionHistory`` / ``SessionState`` globals are seeded on
``SessionControllerBase`` so the worker's ``SessionHistory(...)`` call
does not blow up.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock

# Headless test environments need an offscreen Qt platform.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Re-import safety: drop any ``types.ModuleType`` stubs a sibling test
# may have left behind so we import the real chat_view /
# history_panel / panel_core / session_controller_base.
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
    HistoryLoadResult,
    HistoryRequestStatus,
    HistoryScope,
    SessionHistoryEntry,
)
from rikugan.ui.history_panel import HistoryPanel, HistoryRowWidget
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


def _entry(
    session_id: str,
    title: str,
    *,
    updated_at: float = 1.0,
    message_count: int = 1,
) -> SessionHistoryEntry:
    return SessionHistoryEntry(
        session_id=session_id,
        title=title,
        created_at=0.0,
        updated_at=updated_at,
        provider="",
        model="",
        message_count=message_count,
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
class TestRowClickEmitsSessionId(unittest.TestCase):
    """Wiring 1: a REAL ``QMouseEvent`` on a ``HistoryRowWidget``
    fires ``session_open_requested(str)`` with the bound session id.

    The existing ``tests/ui/test_history_panel.py::TestSignals``
    bypasses PySide6's dispatch path by calling
    ``row.mouseReleaseEvent(None)`` directly.  That bypassed call
    would NOT catch a regression that breaks the row's ``mouseReleaseEvent``
    binding to the panel's signal (e.g. a future refactor that
    replaces the class method with a stale instance attribute).
    Sending a real ``QMouseEvent`` exercises the SAME dispatch path
    PySide6 uses in production — ``QApplication.sendEvent`` calls the
    widget's ``event()`` → ``mouseReleaseEvent()`` just like the
    windowing system would.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls._qapp = QApplication.instance() or QApplication([])

    def test_real_qmouseevent_on_row_emits_session_open_requested(self) -> None:
        panel = HistoryPanel()
        self.addCleanup(panel.shutdown)
        self.addCleanup(panel.deleteLater)

        panel.set_entries(
            [
                _entry("session-A", "Analyze parser"),
                _entry("session-B", "Triage imports"),
            ]
        )
        self.assertEqual(
            len(panel._row_widgets),
            2,
            "set_entries must produce one row per visible entry.",
        )

        captured: list[str] = []
        panel.session_open_requested.connect(captured.append)

        # Drive a REAL mouse press + release on the first row.  Both
        # events are required so Qt's mouse handler reaches the
        # ``mouseReleaseEvent`` path (the row's signal fires on
        # release).  ``QTest.mouseClick`` synthesises both events in
        # one call.
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        row = panel._row_widgets[0]
        # Show the widget so QTest's coordinate mapping is happy.
        from PySide6.QtWidgets import QApplication

        row.show()
        QApplication.instance().processEvents()
        QTest.mouseClick(row, Qt.MouseButton.LeftButton)

        # ``session_open_requested`` must have fired exactly once
        # with the row's bound session id.
        self.assertEqual(
            captured,
            ["session-A"],
            "A real mouse click on a HistoryRowWidget must fire "
            "session_open_requested with the row's session_id. "
            "Empty capture means the click → signal chain is broken.",
        )

    def test_real_qmouseevent_on_second_row_emits_its_session_id(self) -> None:
        """A click on the SECOND row must emit its own id, not the first.

        This guards against a regression where the row's signal gets
        bound to the wrong entry (e.g. closure capture stale, list
        index off-by-one).  Each row carries its own bound session id
        and the wiring must honour that per-row.
        """
        panel = HistoryPanel()
        self.addCleanup(panel.shutdown)
        self.addCleanup(panel.deleteLater)

        panel.set_entries(
            [
                _entry("session-A", "Analyze parser"),
                _entry("session-B", "Triage imports"),
            ]
        )
        captured: list[str] = []
        panel.session_open_requested.connect(captured.append)

        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        row_b = panel._row_widgets[1]
        row_b.show()
        from PySide6.QtWidgets import QApplication

        QApplication.instance().processEvents()
        QTest.mouseClick(row_b, Qt.MouseButton.LeftButton)

        self.assertEqual(
            captured,
            ["session-B"],
            "The click wiring must carry the SECOND row's session id, "
            "not the first — a capture bug would emit the wrong id.",
        )


@requires_real_qt
class TestRowClickReachesPanelCoreSlot(unittest.TestCase):
    """Wiring 2: a real row click reaches PanelCore and drives the
    chat restore for the loaded session.

    The integration test
    (``tests/integration/test_history_on_demand.py``) short-circuits
    this chain by calling ``self._panel._on_history_open_requested``
    directly and replacing every widget with a recording fake.  This
    test instead:

      * Builds a REAL ``HistoryPanel`` (the passive widget).
      * Builds a REAL ``RikuganPanelCore`` via ``__new__`` +
        ``QWidget.__init__`` (the established repo idiom; bypasses
        the heavy ``__init__`` while still making the widget a real
        ``QWidget`` so Qt's signal dispatch works).
      * Seeds the coordinator fields the click chain actually touches
        (``_tab_bar``, ``_history_btn``, ``_mutation_panel``,
        ``_mutations_btn``, ``_chat_views``, ``_pending_restore_messages``,
        ``_history_panel``, ``_ctrl``, etc.).
      * Seeds the lazy ``SessionHistory`` / ``SessionState`` globals
        on ``SessionControllerBase`` (otherwise the worker's
        ``SessionHistory(...)`` call hits ``NoneType``).
      * Wires the panel's ``session_open_requested`` to the real
        ``_on_history_open_requested`` slot, just like
        ``_build_main_splitter`` does in production.
      * Sends a REAL mouse click to a real row.
      * Asserts the click reaches ``_on_history_open_requested`` with
        the right id, AND that the resulting load attaches the
        session to the chat view whose async restore paints the
        messages.

    This catches any of: signal connection missing, signal carrying
    wrong id, slot missing, controller method missing, restore path
    not painting, etc.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls._qapp = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        # Reset QApplication state between tests so a previous test's
        # ``deleteLater`` / event-loop residue does not flake the
        # widget construction.  ``sendPostedEvents`` drains pending
        # deleteLater queues; ``processEvents`` drains timer ticks.
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtWidgets import QApplication

        QCoreApplication.sendPostedEvents()
        QApplication.instance().processEvents()

    def _seed_session_controller_globals(self) -> None:
        """``SessionControllerBase.__init__`` populates lazy module
        globals ``SessionHistory`` and ``SessionState`` so the worker's
        ``SessionHistory(self._ctrl.config)`` call resolves.  When
        ``__init__`` is bypassed (via ``__new__``), those globals
        stay ``None`` and ``_history_load_worker`` blows up with a
        ``TypeError: 'NoneType' object is not callable`` that the
        broad ``except Exception`` silently swallows into an empty
        failure.  Seed them here so the worker actually runs.
        """
        import rikugan.ui.session_controller_base as scb

        if scb.SessionHistory is None:
            from rikugan.state.history import SessionHistory

            scb.SessionHistory = SessionHistory
        if scb.SessionState is None:
            from rikugan.state.session import SessionState

            scb.SessionState = SessionState

    def _build_panel(self):
        """Construct a real ``RikuganPanelCore`` via ``__new__`` and
        seed the coordinator fields the click chain touches.

        Returns ``(panel, ctrl)`` so the test can arm the fake
        controller's behaviour.
        """
        from PySide6.QtWidgets import QApplication, QTabWidget, QWidget

        from rikugan.ui.panel_core import RikuganPanelCore

        panel = RikuganPanelCore.__new__(RikuganPanelCore)
        # ``QWidget.__init__`` is required so the panel is a real Qt
        # widget (signal dispatch needs the C++ side wired up).  We
        # do NOT call ``RikuganPanelCore.__init__`` because that
        # would touch every IDA / provider / Qt dependency.
        QWidget.__init__(panel)
        # Coordinator fields the click chain actually touches.
        panel._is_shutdown = False
        panel._history_panel = HistoryPanel()  # the real HistoryPanel
        panel._history_btn = MagicMock()
        panel._mutation_panel = MagicMock()
        panel._mutations_btn = MagicMock()
        # ``_tab_widget`` is a real ``QTabWidget`` so ``addTab`` /
        # ``setCurrentIndex`` / ``indexOf`` work; the ``_tab_bar`` is
        # the real ``tabBar()`` because some click-chain code paths
        # call ``_tab_bar.tabRect(...)`` / ``setVisible(...)``.
        panel._tab_widget = QTabWidget()
        panel._tab_bar = panel._tab_widget.tabBar()
        panel._chat_views = {}
        panel._pending_restore_messages = {}
        panel._ctx_bar = MagicMock()
        panel._count_label = MagicMock()
        # Wire the panel's signal to the slot exactly like
        # ``_build_main_splitter`` does in production (panel_core.py:818).
        panel._history_panel.session_open_requested.connect(panel._on_history_open_requested)
        # History coordinator fields the slot's helper path touches.
        import threading
        import queue as _queue

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
        panel._stop_history_poll_timer = lambda: None  # type: ignore[assignment]
        panel._stop_history_delete_watchdog = lambda: None  # type: ignore[assignment]
        # Ensure the HistoryPanel is "visible" so ``_drain_history_results``
        # does not stop the poll timer mid-flight (the production
        # condition is ``panel.isVisible() and _history_pending``).
        panel._history_panel.setVisible(True)
        # Provide a minimal real controller (the panel reads
        # ``self._ctrl.find_tab_for_session`` / ``load_history_session``
        # / ``capture_history_scope`` / ``attach_history_session`` /
        # ``active_tab_id`` / ``tab_label`` etc.).
        ctrl = self._build_controller(panel)
        panel._ctrl = ctrl
        # Seed the panel's draft tab so the click chain has a tab to
        # attach to (mirrors ``_create_tab`` in ``_build_ui``).
        from rikugan.ui.chat_view import ChatView

        draft = _chat_view_cls()()
        draft.setProperty("tab_id", ctrl.active_tab_id)
        panel._chat_views[ctrl.active_tab_id] = draft
        panel._tab_widget.addTab(draft, "New Chat")
        # Tear the panel down deterministically. The history load runs on
        # a real ThreadPoolExecutor; if it outlives the test it keeps a
        # worker thread alive that can touch Qt objects after the
        # widgets are gone, which is the Shiboken use-after-free this
        # repo's AGENTS.md §1 warns about (observed as a native access
        # violation when a sibling real-Qt test file runs first).
        self.addCleanup(self._teardown_panel, panel)
        return panel, ctrl

    @staticmethod
    def _teardown_panel(panel) -> None:
        """Stop the history executor and every ChatView before teardown.

        Runs under ``addCleanup`` LIFO, so it may execute after a sibling
        cleanup already scheduled a widget for deletion; every Qt touch
        is guarded because touching a deleted C++ object raises
        ``RuntimeError`` and must not abort the executor join.
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
                pass
        panel._chat_views.clear()

    def _build_controller(self, panel):
        """Build a stub ``IdaSessionController`` that resolves the
        methods the click chain calls.

        The real ``IdaSessionController`` would touch IDA, so this
        is a MagicMock-based stub that returns canned values:
          * ``capture_history_scope(generation)`` returns a fresh
            ``HistoryScope`` for the current IDB.
          * ``find_tab_for_session(session_id)`` returns ``None`` for
            unknown ids (so the slot submits a load).
          * ``load_history_session(session_id, scope)`` returns a
            ``LOADED`` ``HistoryLoadResult`` carrying a real
            ``SessionState`` populated with the messages.
          * ``attach_history_session(result)`` returns an
            ``OPENED`` ``HistoryAttachResult`` with a fresh tab id
            (the controller's real implementation would do this).
        """
        from rikugan.state.session import SessionState
        from rikugan.state.history_types import (
            HistoryAttachResult,
            HistoryAttachStatus,
        )

        ctrl = MagicMock()
        ctrl.config = MagicMock()
        ctrl.active_tab_id = "draft-tab"

        def _capture_scope(generation: int) -> HistoryScope:
            return HistoryScope(
                idb_path="",
                db_instance_id="test-instance",
                generation=generation,
            )

        ctrl.capture_history_scope.side_effect = _capture_scope

        def _find_tab_for_session(session_id: str):
            return None  # No pre-existing tab → load path is taken.

        ctrl.find_tab_for_session.side_effect = _find_tab_for_session

        def _load_session(session_id: str, scope: HistoryScope) -> HistoryLoadResult:
            session = SessionState(idb_path="", db_instance_id="test-instance")
            session.id = session_id
            session.add_message(_user_message("hello", msg_id="u1"))
            session.add_message(_assistant_message("hi", msg_id="a1"))
            return HistoryLoadResult(
                HistoryRequestStatus.LOADED,
                scope,
                session=session,
            )

        ctrl.load_history_session.side_effect = _load_session

        def _attach(result: HistoryLoadResult) -> HistoryAttachResult:
            # Match the production OPENED path: a fresh tab id, the
            # loaded session attached.
            new_tab_id = "history-tab-xyz"
            return HistoryAttachResult(
                status=HistoryAttachStatus.OPENED,
                tab_id=new_tab_id,
                session=result.session,
            )

        ctrl.attach_history_session.side_effect = _attach
        ctrl.tab_label.return_value = "Loaded chat"
        return ctrl

    def _wait_for_async_restore(self, chat_view, expected_user: int, expected_assistant: int, timeout_s: float = 2.0) -> None:
        """Pump the event loop until the async restore on ``chat_view``
        paints the expected number of user/assistant widgets.

        Same deterministic pump as ``tests/ui/test_restore_paints.py`` —
        bounded ``processEvents`` loop, no ``time.sleep`` waits.
        """
        from PySide6.QtWidgets import QApplication

        import time as _time

        app = QApplication.instance()
        deadline_deadline = _time.monotonic() + timeout_s
        while _time.monotonic() < deadline_deadline:
            app.processEvents()
            users = chat_view.findChildren(_user_widget_cls())
            assistants = chat_view.findChildren(_assistant_widget_cls())
            if len(users) >= expected_user and len(assistants) >= expected_assistant:
                # Spin a couple more events so the finished sentinel
                # lands and the restore completes cleanly.
                for _ in range(5):
                    app.processEvents()
                return
            _time.sleep(0.001)
        raise AssertionError(
            f"Async restore did not paint widgets within {timeout_s}s "
            f"(observed user={len(chat_view.findChildren(_user_widget_cls()))}, "
            f"assistant={len(chat_view.findChildren(_assistant_widget_cls()))}; "
            f"expected user>={expected_user}, assistant>={expected_assistant})."
        )

    def test_real_row_click_drives_panel_core_to_attach_and_restore(self) -> None:
        """The full chain: row click → row signal → panel signal →
        PanelCore slot → controller load → chat-view async restore
        paints user + assistant widgets.
        """
        self._seed_session_controller_globals()

        panel, ctrl = self._build_panel()
        self.addCleanup(panel.deleteLater)

        # Stage the History panel with two rows.
        entries = [
            _entry("session-load-1", "Analyze parser"),
            _entry("session-load-2", "Triage imports"),
        ]
        panel._history_panel.set_entries(entries)

        # The click target is the second row.
        target_row: HistoryRowWidget = panel._history_panel._row_widgets[0]
        target_row.show()
        from PySide6.QtWidgets import QApplication

        QApplication.instance().processEvents()

        # Fire a REAL Qt mouse click.
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        QTest.mouseClick(target_row, Qt.MouseButton.LeftButton)
        # Pump until the load worker has produced its result AND the
        # drain has applied it (open path: load result → attach →
        # create tab → restore messages).
        # Wait for the controller's ``load_history_session`` to be
        # invoked at least once (proves the slot reached the load
        # path).
        import time as _time

        deadline = _time.monotonic() + 3.0
        while _time.monotonic() < deadline:
            QApplication.instance().processEvents()
            if ctrl.load_history_session.call_count >= 1:
                break
            _time.sleep(0.001)
        self.assertGreaterEqual(
            ctrl.load_history_session.call_count,
            1,
            "A real row click must reach PanelCore._on_history_open_requested "
            "and trigger _ctrl.load_history_session. Zero calls means the "
            "click → signal → slot chain is broken.",
        )
        # The controller was called with the right session id.
        call_args = ctrl.load_history_session.call_args
        self.assertEqual(
            call_args.args[0] if call_args.args else call_args.kwargs.get("session_id"),
            "session-load-1",
            "load_history_session must be called with the row's session id.",
        )

        # The load worker runs on a real ThreadPoolExecutor; wait for
        # the executor to finish so the typed ``HistoryLoadResult``
        # lands in the result queue, then drain.  This is more
        # robust than racing the executor's shutdown against an
        # already-drained panel.
        executor = panel._history_executor
        if executor is not None:
            executor.shutdown(wait=True)
            # Re-create the executor so subsequent operations stay
            # safe (the production panel re-creates lazily on the
            # next request — match that behaviour).
            from concurrent.futures import ThreadPoolExecutor as _TPE

            panel._history_executor = _TPE(
                max_workers=1,
                thread_name_prefix="rikugan-history",
            )
        # Pump the drain multiple times so any chained work (the
        # apply path → OPENED → create tab → restore messages) has
        # its events delivered.
        for _ in range(10):
            QApplication.instance().processEvents()
        panel._drain_history_results()

        # Pump until the async restore paints the messages in the new tab.
        # The OPENED path attaches the session, then
        # ``_restore_messages_if_needed`` calls
        # ``chat_view.restore_from_messages_async(messages)`` — the same
        # path Bug 2 exercises.  After the drain, BOTH widgets must
        # be in the new tab's layout.
        # Wait for the panel to create a new tab via ``_create_tab``.
        # Use a generous deadline — the load worker runs on a real
        # ThreadPoolExecutor, and a slow CI box may take several
        # hundred ms before the result lands.
        deadline = _time.monotonic() + 5.0
        chat_view_for_new_tab = None
        while _time.monotonic() < deadline:
            QApplication.instance().processEvents()
            for tab_id, view in panel._chat_views.items():
                if tab_id != ctrl.active_tab_id:
                    chat_view_for_new_tab = view
                    break
            if chat_view_for_new_tab is not None:
                break
            _time.sleep(0.001)
        self.assertIsNotNone(
            chat_view_for_new_tab,
            "After clicking a History row, PanelCore must create a NEW "
            "ChatView tab for the loaded session (the OPENED branch of "
            "attach_history_session).",
        )
        # Now pump until the new tab's async restore paints the widgets.
        self._wait_for_async_restore(
            chat_view_for_new_tab,
            expected_user=1,
            expected_assistant=1,
            timeout_s=5.0,
        )


if __name__ == "__main__":
    unittest.main()
