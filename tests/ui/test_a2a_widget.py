"""Tests for lucnhan.ui.a2a_widget.A2ABridgeWidget.

Strategy: use MagicMock to stand in for the Qt widgets so the test is
hermetic and doesn't depend on the qt_stubs API surface. The
A2ABridgeWidget is built with a custom QWidget base that pre-injects
mock children; we then verify the widget's logic by introspecting the
mock state.

The dispatcher itself is mocked so no subprocess or HTTP is exercised
— those have their own integration tests.

================================================================
THREADING MODEL — see lucnhan/issues tracking the rewrite task
----------------------------------------------------------------
The a2a_widget threading model uses stdlib ``threading.Thread`` with a
``queue.Queue`` polled by a ``QTimer`` (see ``_A2ATaskRunner`` +
``_poll_task_events``). Tests instantiate the real runner, never mock
it. Tests wait for threads to finish using ``runner.join(timeout)`` —
``time.sleep`` is avoided to keep tests non-flaky.

Tracking issue: https://github.com/EliteClassRoom/Luc-Nhan/issues/3
================================================================
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks
from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()
install_ida_mocks()


def _empty_run_task(*args, **kwargs):
    """Default ``dispatcher.run_task`` side_effect — yields nothing, returns ``""``.

    ``iter([])`` is a one-shot empty iterator; calling ``next()`` on it
    raises ``StopIteration`` immediately, which the runner treats as a
    clean completion with an empty result. Each invocation creates a
    fresh iterator so concurrent runners don't share state.
    """
    return iter([])


def _build_widget_with_mocks(agents: list | None = None) -> tuple:
    """Build an A2ABridgeWidget with a stub dispatcher.

    Returns ``(widget, mocks_dict)`` so tests can inspect both.
    """
    from lucnhan.agent.a2a.types import ExternalAgentConfig
    from lucnhan.ui.a2a_widget import A2ABridgeWidget

    if agents is None:
        agents = [
            ExternalAgentConfig(
                name="claude",
                transport="subprocess",
                endpoint="claude",
                capabilities=["code_generation"],
            )
        ]

    fake_dispatcher = MagicMock()
    fake_dispatcher.discover.return_value = agents
    # Default ``run_task`` returns an empty iterator so the background
    # thread doesn't crash. Tests that need specific events should
    # override ``fake_dispatcher.run_task.side_effect`` after building.
    fake_dispatcher.run_task.side_effect = _empty_run_task

    # Bypass the real __init__ (which builds Qt widgets) and manually
    # wire the dependencies. This lets the tests run without a real
    # Qt event loop.
    w = A2ABridgeWidget.__new__(A2ABridgeWidget)
    # Initialise the widget base so class-level Signal descriptors bind
    # to the instance. Use ``QWidget.__init__`` (the *direct* base),
    # not ``QObject.__init__``: shiboken rejects
    # ``QObject.__init__(widget)`` with "QObject isn't a direct base
    # class" whenever real PySide6 is loaded — which happens when a
    # sibling test (e.g. ``test_markdown``) imports ``lucnhan.ui.*``
    # before this file runs, leaving real ``PySide6`` modules in
    # ``sys.modules`` (``ensure_pyside6_stubs`` uses ``setdefault`` and
    # keeps them). ``QWidget`` subclasses ``QObject`` so the init still
    # wires the signal machinery.
    from lucnhan.ui.qt_compat import QWidget

    QWidget.__init__(w)

    w._dispatcher = fake_dispatcher
    w._history = {}
    w._inflight = {}
    # Pre-populate the stub-fallback agent list so ``_lookup_target_agent``
    # works without going through ``_refresh_agents``.
    w._target_combo_agents = list(agents)
    # ``_target_combo.currentIndex`` must return a real int for the
    # safe-int wrapper to accept.
    w._target_combo = MagicMock()
    w._target_combo.currentIndex.return_value = 0
    w._target_combo.count.return_value = len(agents)
    w._agent_list = MagicMock()
    w._target_combo = MagicMock()
    w._task_edit = MagicMock()
    w._task_edit.toPlainText.return_value = ""
    w._history_table = MagicMock()
    w._history_table.rowCount.return_value = 0
    w._history_table.columnCount.return_value = 5
    w._agent_count_label = MagicMock()
    w._send_btn = MagicMock()
    w._cancel_btn = MagicMock()
    w._view_output_btn = MagicMock()
    w._clear_history_btn = MagicMock()
    w._refresh_agents_btn = MagicMock()
    w._include_context_check = MagicMock()
    w._include_context_check.isChecked.return_value = False
    # The poll loop touches ``self._poll_timer`` via ``_safe_call`` at
    # the end of every drain. Without a real ``__init__`` the attribute
    # is missing, which raises ``AttributeError`` on attribute lookup
    # before ``_safe_call`` can swallow it. A bare MagicMock absorbs
    # the ``stop()`` call without affecting anything we observe.
    w._poll_timer = MagicMock()
    return w, {"dispatcher": fake_dispatcher, "agents": agents}


def _join_runner(widget, task_id: str, timeout: float = 2.0) -> None:
    """Wait for a runner's background thread to exit.

    Used by tests that spin up a real ``_A2ATaskRunner`` (via
    ``_on_send_clicked``) so the daemon thread doesn't outlive the
    test process. Idempotent if the runner was already drained and
    removed from ``_inflight`` (a missing runner is a no-op).
    """
    runner = widget._inflight.get(task_id)
    if runner is not None:
        runner.join(timeout)


class _FakeAgent:
    def __init__(self, name: str = "claude", transport: str = "subprocess") -> None:
        self.name = name
        self.transport = transport
        self.endpoint = name
        self.capabilities = ["code_generation"]


class TestAgentsPane(unittest.TestCase):
    def test_count_label_uses_qt_alignment_flag(self) -> None:
        """PySide6 QLabel rejects a raw integer alignment value."""
        from lucnhan.ui import a2a_widget

        label = MagicMock()
        with (
            patch.object(a2a_widget, "QLabel", return_value=label),
            patch.object(a2a_widget, "QGroupBox", return_value=MagicMock()),
            patch.object(a2a_widget, "QVBoxLayout", return_value=MagicMock()),
            patch.object(a2a_widget, "QListWidget", return_value=MagicMock()),
            patch.object(a2a_widget, "QHBoxLayout", return_value=MagicMock()),
            patch.object(a2a_widget, "QPushButton", return_value=MagicMock()),
        ):
            widget = a2a_widget.A2ABridgeWidget.__new__(a2a_widget.A2ABridgeWidget)
            widget._build_agents_pane()

        label.setAlignment.assert_called_once_with(a2a_widget.Qt.AlignmentFlag.AlignRight)


class TestRefreshAgents(unittest.TestCase):
    """discover() is forwarded to the dispatcher; UI state is updated."""

    def test_refresh_uses_dispatcher(self) -> None:
        w, mocks = _build_widget_with_mocks()
        w._refresh_agents()
        mocks["dispatcher"].discover.assert_called_once()

    def test_refresh_passes_agents_to_lists(self) -> None:
        agents = [_FakeAgent("claude"), _FakeAgent("codex")]
        w, _ = _build_widget_with_mocks(agents)
        w._refresh_agents()
        # QListWidget.addItem called once per agent
        self.assertEqual(w._agent_list.addItem.call_count, 2)
        self.assertEqual(w._target_combo.addItem.call_count, 2)

    def test_refresh_with_no_agents_disables_send(self) -> None:
        w, _ = _build_widget_with_mocks([])
        w._refresh_agents()
        # send button was disabled (call_count >= 1 with falsy arg)
        # We use ``not c.args[0]`` instead of ``is False`` because
        # MagicMock doesn't always preserve identity for booleans.
        disabled_calls = [c for c in w._send_btn.setEnabled.call_args_list if c.args and not c.args[0]]
        self.assertGreaterEqual(len(disabled_calls), 1)

    def test_refresh_with_agents_enables_send(self) -> None:
        w, _ = _build_widget_with_mocks([_FakeAgent()])
        w._refresh_agents()
        # The most recent setEnabled call should be True (enable)
        self.assertTrue(w._send_btn.setEnabled.called)
        last_call = w._send_btn.setEnabled.call_args_list[-1]
        self.assertTrue(last_call.args[0])

    def test_refresh_bails_when_widgets_already_deleted(self) -> None:
        """A PaletteChange arriving after teardown must not crash.

        Shiboken raises RuntimeError when the C++ QListWidget is already
        deleted (e.g. the panel was closed between the event being queued
        and delivered). _refresh_agents should bail out silently rather
        than propagate the RuntimeError or attempt discover() on dead UI.
        """
        w, mocks = _build_widget_with_mocks([_FakeAgent()])
        # Simulate the list widget's C++ side being gone.
        w._agent_list.clear.side_effect = RuntimeError(
            "Internal C++ object (PySide6.QtWidgets.QListWidget) already deleted."
        )
        # Must not raise.
        w._refresh_agents()
        # Bailed before touching the dispatcher — no discovery on dead UI.
        mocks["dispatcher"].discover.assert_not_called()


class TestSendClick(unittest.TestCase):
    def test_send_with_empty_task_does_nothing(self) -> None:
        w, _ = _build_widget_with_mocks()
        w._task_edit.toPlainText.return_value = "  \n  "  # whitespace
        w._on_send_clicked()
        # No history row added; no runner spawned (early-return).
        self.assertEqual(w._history_table.insertRow.call_count, 0)

    def test_send_with_no_agent_does_nothing(self) -> None:
        w, _ = _build_widget_with_mocks([])
        w._task_edit.toPlainText.return_value = "test"
        w._on_send_clicked()
        self.assertEqual(w._history_table.insertRow.call_count, 0)

    def test_send_appends_history_row(self) -> None:
        from lucnhan.ui.a2a_widget import _HistoryRow

        w, _ = _build_widget_with_mocks([_FakeAgent()])
        w._task_edit.toPlainText.return_value = "summarize the binary"
        # No QThread patch needed — the real runner's background
        # thread runs with our mocked dispatcher (empty stream) and
        # exits cleanly.
        w._on_send_clicked()

        # Row created synchronously by ``_on_send_clicked``.
        self.assertEqual(w._history_table.insertRow.call_count, 1)
        self.assertEqual(len(w._history), 1)
        task_id = next(iter(w._history.keys()))
        row = w._history[task_id]
        self.assertIsInstance(row, _HistoryRow)
        self.assertEqual(row.agent_name, "claude")
        # _HistoryStatus is a ``str`` Enum — ``QUEUED == "queued"`` is True.
        self.assertEqual(row.status, "queued")
        # 5 cells set per row (time, agent, task, status, result).
        self.assertEqual(w._history_table.setItem.call_count, 5)

        # Wait for the daemon background thread to finish so it
        # doesn't outlive the test.
        _join_runner(w, task_id)

    def test_signal_emitted_on_send(self) -> None:
        w, _ = _build_widget_with_mocks([_FakeAgent()])
        w._task_edit.toPlainText.return_value = "test"
        captured: list = []
        w.task_dispatched.connect(lambda tid, name, exc: captured.append((tid, name, exc)))

        w._on_send_clicked()

        # Signal is emitted synchronously inside ``_on_send_clicked``.
        self.assertEqual(len(captured), 1)
        _tid, name, exc = captured[0]
        self.assertEqual(name, "claude")
        self.assertEqual(exc, "test")

        # Wait for the daemon background thread to finish.
        task_id = next(iter(w._history.keys()))
        _join_runner(w, task_id)


class TestPollEventHandlers(unittest.TestCase):
    """``_poll_task_events`` routes queued events to the right UI handler.

    The new threading model uses a ``_A2ATaskRunner`` (background
    ``threading.Thread`` + ``queue.Queue``) and the widget polls the
    queue every ``_POLL_INTERVAL_MS`` via ``_poll_task_events``. Tests
    construct a runner manually (without starting the thread) so the
    queue stays empty until the test puts events in it — this avoids
    race conditions where the runner's own STARTED / COMPLETED events
    would pollute the test's expectations.
    """

    def _spawn(self) -> tuple:
        """Create a widget with a runner already registered. No thread is started."""
        from lucnhan.ui.a2a_widget import _A2ATaskRunner, _HistoryRow

        w, _ = _build_widget_with_mocks([_FakeAgent()])
        task_id = "test-tid"
        row = _HistoryRow(task_id=task_id, agent_name="claude", task_excerpt="x")
        w._history[task_id] = row
        cancel_event = threading.Event()
        runner = _A2ATaskRunner(w._dispatcher, "claude", "x", "", cancel_event, task_id)
        w._inflight[task_id] = runner
        return w, task_id

    def test_started_sets_running(self) -> None:
        from lucnhan.ui.a2a_widget import _A2ARunnerEventType, _A2ATaskEvent

        w, task_id = self._spawn()
        runner = w._inflight[task_id]
        runner.queue.put(_A2ATaskEvent(type=_A2ARunnerEventType.STARTED, task_id=task_id))
        w._poll_task_events()
        self.assertEqual(w._history[task_id].status, "running")

    def test_output_appends(self) -> None:
        from lucnhan.ui.a2a_widget import _A2ARunnerEventType, _A2ATaskEvent

        w, task_id = self._spawn()
        runner = w._inflight[task_id]
        runner.queue.put(_A2ATaskEvent(type=_A2ARunnerEventType.OUTPUT, task_id=task_id, text="first chunk\n"))
        runner.queue.put(_A2ATaskEvent(type=_A2ARunnerEventType.OUTPUT, task_id=task_id, text="second chunk\n"))
        w._poll_task_events()
        self.assertIn("first chunk", w._history[task_id].result_text)
        self.assertIn("second chunk", w._history[task_id].result_text)

    def test_completed_marks_status(self) -> None:
        from lucnhan.ui.a2a_widget import _A2ARunnerEventType, _A2ATaskEvent

        w, task_id = self._spawn()
        runner = w._inflight[task_id]
        runner.queue.put(_A2ATaskEvent(type=_A2ARunnerEventType.COMPLETED, task_id=task_id))
        w._poll_task_events()
        self.assertEqual(w._history[task_id].status, "completed")
        # Terminal events also remove the runner from ``_inflight`` so
        # the next poll tick doesn't re-process them.
        self.assertNotIn(task_id, w._inflight)

    def test_failed_marks_error(self) -> None:
        from lucnhan.ui.a2a_widget import _A2ARunnerEventType, _A2ATaskEvent

        w, task_id = self._spawn()
        runner = w._inflight[task_id]
        runner.queue.put(_A2ATaskEvent(type=_A2ARunnerEventType.FAILED, task_id=task_id, text="boom"))
        w._poll_task_events()
        self.assertEqual(w._history[task_id].status, "failed")
        self.assertEqual(w._history[task_id].error_text, "boom")

    def test_cancelled_marks_status(self) -> None:
        from lucnhan.ui.a2a_widget import _A2ARunnerEventType, _A2ATaskEvent

        w, task_id = self._spawn()
        runner = w._inflight[task_id]
        runner.queue.put(_A2ATaskEvent(type=_A2ARunnerEventType.CANCELLED, task_id=task_id))
        w._poll_task_events()
        self.assertEqual(w._history[task_id].status, "cancelled")
        self.assertNotIn(task_id, w._inflight)


class TestHistoryRowHelpers(unittest.TestCase):
    def test_excerpt_short(self) -> None:
        w, _ = _build_widget_with_mocks()
        self.assertEqual(w._format_result_excerpt("short"), "short")

    def test_excerpt_long(self) -> None:
        w, _ = _build_widget_with_mocks()
        long = "x" * 200
        excerpt = w._format_result_excerpt(long)
        self.assertLessEqual(len(excerpt), 60)
        self.assertTrue(excerpt.startswith("…"))

    def test_excerpt_strips_whitespace(self) -> None:
        w, _ = _build_widget_with_mocks()
        # 30 leading spaces + 50 chars = 80 chars, but strip() removes
        # the whitespace. The result should be the unstripped portion
        # of the cleaned text.
        text = " " * 30 + "actual content here" + " " * 30
        excerpt = w._format_result_excerpt(text)
        # The result is the last 57 chars of the cleaned text
        self.assertLessEqual(len(excerpt), 60)


class TestCancelHandler(unittest.TestCase):
    def test_cancel_sets_event(self) -> None:
        """Cancel button click must set the cancel_event for the in-flight task.

        With the new threading model, ``runner.cancel()`` wraps
        ``self._cancel_event.set()``. We assert on the underlying
        ``threading.Event`` rather than the runner's public surface so
        the test catches accidental removals of the cancel mechanism.
        """
        w, _ = _build_widget_with_mocks([_FakeAgent()])
        w._task_edit.toPlainText.return_value = "x"
        w._on_send_clicked()
        task_id = next(iter(w._history.keys()))
        runner = w._inflight[task_id]
        # Simulate the cancel click: the handler reads the selected
        # row from the history table. We mock ``currentRow`` + ``item``
        # so it looks like the user clicked on the row we just created.
        w._history_table.currentRow.return_value = 0
        item_mock = MagicMock()
        item_mock.data.return_value = task_id
        w._history_table.item.return_value = item_mock

        w._on_cancel_clicked()

        # The cancel event was set by ``runner.cancel()``.
        self.assertTrue(runner._cancel_event.is_set())

        # Wait for the daemon background thread to finish.
        _join_runner(w, task_id)


class TestShutdown(unittest.TestCase):
    def test_shutdown_cancels_inflight(self) -> None:
        w, _ = _build_widget_with_mocks([_FakeAgent()])
        w._task_edit.toPlainText.return_value = "x"

        # Use a dispatcher that respects ``cancel_event`` so the
        # runner's thread can exit cleanly during ``shutdown()``'s
        # join window. With an empty iterator the thread would race
        # to completion before ``shutdown()`` could observe the
        # cancel; a blocking generator makes the cancel observable.
        def blocking_run(*args, **kwargs):
            kwargs["cancel_event"].wait(timeout=5.0)
            return "done"

        w._dispatcher.run_task.side_effect = blocking_run

        w._on_send_clicked()
        task_id = next(iter(w._history.keys()))
        runner = w._inflight[task_id]

        # Sanity check: runner thread is alive and blocked on the
        # cancel_event wait.
        runner.join(0.05)
        self.assertTrue(runner.is_alive())

        # ``shutdown()`` must cancel every runner, join them, and
        # remove any that exited from ``_inflight``.
        w.shutdown()

        # The cancel event was set (proves ``shutdown()`` reached
        # ``runner.cancel()``).
        self.assertTrue(runner._cancel_event.is_set())
        # The thread exited within shutdown's join window because
        # the dispatcher honored the cancel event.
        self.assertFalse(runner.is_alive())
        # The runner was popped from ``_inflight``.
        self.assertEqual(len(w._inflight), 0)


class TestHistoryRowModel(unittest.TestCase):
    """The _HistoryRow dataclass behaves as expected."""

    def test_status_defaults_to_queued(self) -> None:
        from lucnhan.ui.a2a_widget import _HistoryRow

        row = _HistoryRow(task_id="abc", agent_name="claude", task_excerpt="x")
        self.assertEqual(row.status, "queued")
        self.assertEqual(row.result_text, "")
        self.assertEqual(row.error_text, "")
        # started_at is a recent timestamp
        import time

        self.assertLess(time.time() - row.started_at, 5.0)


class TestTaskRunner(unittest.TestCase):
    """The ``_A2ATaskRunner`` drives the dispatcher and enqueues events.

    These tests exercise the runner in isolation — no widget involved.
    We start the real background thread, wait for it to finish via
    ``runner.join(timeout)``, then drain the queue to verify the
    emitted events.
    """

    def test_runner_stores_arguments(self) -> None:
        from lucnhan.ui.a2a_widget import _A2ATaskRunner

        cancel = threading.Event()
        fake_dispatcher = MagicMock()
        runner = _A2ATaskRunner(fake_dispatcher, "claude", "do thing", "ctx", cancel, "tid-1")
        self.assertEqual(runner._agent_name, "claude")
        self.assertEqual(runner._task, "do thing")
        self.assertEqual(runner._include_context, "ctx")
        self.assertIs(runner._cancel_event, cancel)
        self.assertEqual(runner.task_id, "tid-1")

    def test_runner_run_emits_started_output_completed(self) -> None:
        from lucnhan.agent.turn import TurnEvent, TurnEventType
        from lucnhan.ui.a2a_widget import _A2ARunnerEventType, _A2ATaskRunner

        cancel = threading.Event()

        def fake_run(agent_name, task, **kwargs):
            yield TurnEvent(type=TurnEventType.TEXT_DELTA, text="hello")
            return "final result"

        fake_dispatcher = MagicMock()
        fake_dispatcher.run_task.side_effect = fake_run

        runner = _A2ATaskRunner(fake_dispatcher, "claude", "x", "", cancel, "tid")
        runner.start()
        # Wait deterministically for the thread to finish — no
        # ``time.sleep`` to keep the test from flaking.
        runner.join(2.0)
        self.assertFalse(runner.is_alive(), "runner thread did not exit within join window")

        # Drain the queue and verify the event sequence.
        events = []
        while True:
            try:
                ev = runner.queue.get_nowait()
            except queue.Empty:
                break
            events.append(ev)

        self.assertEqual(len(events), 3)
        self.assertEqual(events[0].type, _A2ARunnerEventType.STARTED)
        self.assertEqual(events[1].type, _A2ARunnerEventType.OUTPUT)
        self.assertEqual(events[1].text, "hello")
        self.assertEqual(events[2].type, _A2ARunnerEventType.COMPLETED)
        self.assertEqual(events[2].text, "final result")

    def test_runner_run_emits_failed_on_exception(self) -> None:
        from lucnhan.ui.a2a_widget import _A2ARunnerEventType, _A2ATaskRunner

        cancel = threading.Event()

        # Generator function that raises on the first iteration. This
        # matches the runner's protected ``try/except`` block — a plain
        # ``fake_dispatcher.run_task.side_effect = RuntimeError("boom")``
        # would raise *outside* the try/except (before the generator is
        # even returned) and skip the FAILED path entirely.
        def raising_run(agent_name, task, **kwargs):
            raise RuntimeError("boom")
            yield  # unreachable; makes ``raising_run`` a generator function

        fake_dispatcher = MagicMock()
        fake_dispatcher.run_task.side_effect = raising_run

        runner = _A2ATaskRunner(fake_dispatcher, "claude", "x", "", cancel, "tid")
        runner.start()
        runner.join(2.0)
        self.assertFalse(runner.is_alive(), "runner thread did not exit within join window")

        events = []
        while True:
            try:
                ev = runner.queue.get_nowait()
            except queue.Empty:
                break
            events.append(ev)

        # Started enqueued first, then FAILED from the caught exception.
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].type, _A2ARunnerEventType.STARTED)
        self.assertEqual(events[1].type, _A2ARunnerEventType.FAILED)
        self.assertIn("boom", events[1].text)


if __name__ == "__main__":
    unittest.main()
