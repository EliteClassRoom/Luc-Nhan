"""Regression tests for ``A2ADispatcher`` subprocess failure propagation.

These tests cover the contract that the A2A dispatcher surfaces
``A2AEvent(type="error")`` and ``A2AEvent(type="cancelled")`` from
``SubprocessBridge`` as a single ``TurnEventType.ERROR`` and returns
``""`` from the ``run_task`` generator. The previous implementation
silently dropped these events and returned the last-seen stdout,
which made subprocess failures invisible to the UI and to the LLM
tool surface.

The tests are pure Python — no Qt, no real subprocess, no real PATH
discovery. They use a fake ``SubprocessBridge`` wired in by
monkeypatching the dispatcher's lazy accessor and the ``discover``
method, keeping the test fully deterministic.

The second half covers ``A2ABridgeWidget._A2ATaskRunner`` directly:
StopIteration.value capture and the cancel-event-vs-ERROR
classification that decides whether a TurnEvent.error_event surfaces
as ``cancelled`` (clean cancel) or ``failed`` (real failure).
"""

from __future__ import annotations

import threading
import time
import unittest
from collections.abc import Generator
from typing import Any

from rikugan.agent.a2a import A2ADispatcher
from rikugan.agent.a2a.types import A2AEvent, ExternalAgentConfig
from rikugan.agent.turn import TurnEvent, TurnEventType
from rikugan.ui.a2a_widget import (
    _A2ARunnerEventType,
    _A2ATaskEvent,
    _A2ATaskRunner,
)


def _drain(
    gen: Generator[TurnEvent, None, str],
) -> tuple[list[TurnEvent], str]:
    """Drain a generator while preserving its return value.

    A plain ``for`` loop on a generator discards ``StopIteration.value``,
    so we manually iterate and capture it. This is the same idiom the
    widget runner uses to obtain the dispatcher's final result.
    """
    events: list[TurnEvent] = []
    while True:
        try:
            events.append(next(gen))
        except StopIteration as stop:
            return events, stop.value


class _FakeSubprocessBridge:
    """Stand-in for ``SubprocessBridge`` that yields scripted events."""

    def __init__(self, events: list[A2AEvent], return_value: str = "") -> None:
        self._events = events
        self._return_value = return_value
        self.calls: list[dict[str, Any]] = []

    def run_task(
        self,
        agent: ExternalAgentConfig,
        task: str,
        timeout: int = 300,
        cancel_event: Any = None,
    ) -> Generator[A2AEvent, None, str]:
        self.calls.append({"agent": agent, "task": task, "timeout": timeout, "cancel_event": cancel_event})
        yield from self._events
        return self._return_value


class _FakeDispatcher:
    """Stand-in for ``A2ADispatcher`` that yields scripted ``TurnEvent``s.

    The runner's only contract with the dispatcher is that ``run_task``
    returns a generator yielding ``TurnEvent``s and returns the final
    result string. We don't subclass the real dispatcher so the test
    doesn't drag in any registry / subprocess discovery.

    Tests pass an explicit ``events`` list (which may include a single
    ``TurnEventType.ERROR`` to exercise the runner's classification
    logic). The generator returns ``final_text`` after the list is
    drained. Use ``event=None`` semantics by leaving ``events`` empty
    for a clean completion.
    """

    def __init__(
        self,
        events: list[TurnEvent],
        final_text: str = "",
    ) -> None:
        self._events = events
        self._final_text = final_text
        self.calls: list[dict[str, Any]] = []

    def run_task(
        self,
        agent_name: str,
        task: str,
        *,
        cancel_event: Any = None,
        include_context: str = "",
    ) -> Generator[TurnEvent, None, str]:
        self.calls.append(
            {
                "agent_name": agent_name,
                "task": task,
                "cancel_event": cancel_event,
                "include_context": include_context,
            }
        )
        yield from self._events
        return self._final_text


class TestA2ADispatcherSubprocessFailure(unittest.TestCase):
    """A2ADispatcher must surface subprocess bridge errors as TurnEvent.ERROR."""

    def _build_dispatcher(self, bridge: _FakeSubprocessBridge) -> A2ADispatcher:
        dispatcher = A2ADispatcher(auto_discover=False)
        # ``_get_subprocess_bridge`` is the lazy accessor used by
        # ``_run_subprocess``. Pre-populating the private field
        # short-circuits the lazy constructor and avoids importing
        # or touching the real ``SubprocessBridge``.
        dispatcher._subprocess_bridge = bridge  # type: ignore[assignment]
        # ``_find_agent`` calls ``self.discover()``; replacing
        # ``discover`` keeps the registry out of the picture.
        fake_agent = ExternalAgentConfig(name="fake", transport="subprocess", endpoint="fake")
        dispatcher.discover = lambda: [fake_agent]  # type: ignore[method-assign]
        return dispatcher

    def test_error_event_yields_turn_event_error_and_empty_return(self) -> None:
        """A subprocess ``error`` event must surface as a ``TurnEventType.ERROR``
        with the bridge's text in the ``error`` field, and the generator
        return value must be ``""`` so callers don't see stale stdout."""
        bridge = _FakeSubprocessBridge(
            events=[A2AEvent(type="error", text="boom", done=True)],
            return_value="",
        )
        dispatcher = self._build_dispatcher(bridge)

        events, result = _drain(dispatcher.run_task("fake", "do work"))

        # At least one ERROR event was yielded.
        error_events = [e for e in events if e.type == TurnEventType.ERROR]
        self.assertEqual(len(error_events), 1, f"expected 1 ERROR, got {events}")
        self.assertIn("boom", error_events[0].error or "")

        # The generator's return value is the authoritative result
        # for the caller — it must be empty, not a stale stdout.
        self.assertEqual(result, "")

    def test_cancelled_event_yields_turn_event_error_with_cancellation_message(
        self,
    ) -> None:
        """A subprocess ``cancelled`` event must surface as a
        ``TurnEventType.ERROR`` carrying the bridge's cancellation
        message, and the generator return value must be ``""``."""
        bridge = _FakeSubprocessBridge(
            events=[
                A2AEvent(
                    type="cancelled",
                    text="Task cancelled by user",
                    done=True,
                )
            ],
            return_value="",
        )
        dispatcher = self._build_dispatcher(bridge)

        events, result = _drain(dispatcher.run_task("fake", "do work"))

        error_events = [e for e in events if e.type == TurnEventType.ERROR]
        self.assertEqual(len(error_events), 1, f"expected 1 ERROR, got {events}")
        # The cancellation message must be preserved verbatim so the
        # UI can show it to the user verbatim.
        self.assertEqual(error_events[0].error, "Task cancelled by user")
        self.assertEqual(result, "")

    def test_error_after_stdout_still_yields_error(self) -> None:
        """The previous bug: stdout events set ``last_text`` and a
        subsequent error event was swallowed, leaving ``last_text`` as
        the return value. The fix yields the error event AND clears
        the return value."""
        bridge = _FakeSubprocessBridge(
            events=[
                A2AEvent(type="stdout", text="partial output\n"),
                A2AEvent(type="error", text="crashed", done=True),
            ],
            return_value="",
        )
        dispatcher = self._build_dispatcher(bridge)

        events, result = _drain(dispatcher.run_task("fake", "do work"))

        # The stdout was streamed (TEXT_DELTA) and the error surfaced
        # as a separate ERROR event. The return value is empty —
        # NOT the last stdout line.
        text_deltas = [e for e in events if e.type == TurnEventType.TEXT_DELTA]
        self.assertTrue(
            any("partial output" in (e.text or "") for e in text_deltas),
            f"expected a TEXT_DELTA carrying the stdout: {events}",
        )
        error_events = [e for e in events if e.type == TurnEventType.ERROR]
        self.assertEqual(len(error_events), 1, f"expected 1 ERROR, got {events}")
        self.assertEqual(error_events[0].error, "crashed")
        # This is the exact regression: a stale stdout return value
        # would be "partial output" — the bug. With the fix it is "".
        self.assertEqual(result, "")

    def test_stdout_then_completed_streams_both_and_returns_completed_text(
        self,
    ) -> None:
        """Success path: a stdout line followed by a ``completed``
        event must stream both as ``TEXT_DELTA`` events, and the
        generator's return value must be the completed text (the
        JSON-parsed result). Regression-guards the previous fix
        where the dispatcher silently swallowed the completed event
        and returned the trailing stdout."""
        bridge = _FakeSubprocessBridge(
            events=[
                A2AEvent(type="stdout", text="partial output\n"),
                A2AEvent(
                    type="completed",
                    text="Final answer: 42",
                    done=True,
                ),
            ],
            return_value="",
        )
        dispatcher = self._build_dispatcher(bridge)

        events, result = _drain(dispatcher.run_task("fake", "do work"))

        # Both pieces must be visible to the LLM/UI as separate
        # streaming deltas. The completed line is also a TEXT_DELTA
        # because ``_translate_subprocess_event`` maps it back into
        # the same channel so the UI sees a single continuous stream.
        text_deltas = [e for e in events if e.type == TurnEventType.TEXT_DELTA]
        joined = "".join(e.text or "" for e in text_deltas)
        self.assertIn("partial output", joined)
        self.assertIn("Final answer: 42", joined)

        # The generator's final return value is the dispatcher's
        # authoritative completion payload — for the subprocess
        # transport, ``_run_subprocess`` returns the last translated
        # text, which equals the completed line because completed
        # events overwrite ``last_text``. The user-facing runner
        # uses this as the final result_text when ``stop.value`` is
        # non-empty.
        self.assertEqual(result, "Final answer: 42")

        # No error event must leak.
        error_events = [e for e in events if e.type == TurnEventType.ERROR]
        self.assertEqual(error_events, [], f"unexpected ERROR events: {events}")

    def test_stdout_then_cancelled_returns_error_and_empty_string(self) -> None:
        """Cancel-after-output path: a stdout line followed by a
        bridge ``cancelled`` event must continue to stream the stdout
        AND surface the cancellation as an ERROR event, with an
        empty final result. The widget's cancel button relied on
        this exact behaviour — without it, the UI never knew the
        task had been cancelled and left the row marked ``running``."""
        bridge = _FakeSubprocessBridge(
            events=[
                A2AEvent(type="stdout", text="partial output\n"),
                A2AEvent(
                    type="cancelled",
                    text="Task cancelled by user",
                    done=True,
                ),
            ],
            return_value="",
        )
        dispatcher = self._build_dispatcher(bridge)

        events, result = _drain(dispatcher.run_task("fake", "do work"))

        # The stdout still streamed. The cancellation surfaced as a
        # verbatim ERROR event.
        text_deltas = [e for e in events if e.type == TurnEventType.TEXT_DELTA]
        self.assertTrue(
            any("partial output" in (e.text or "") for e in text_deltas),
            f"expected a TEXT_DELTA carrying the stdout: {events}",
        )
        error_events = [e for e in events if e.type == TurnEventType.ERROR]
        self.assertEqual(len(error_events), 1, f"expected 1 ERROR, got {events}")
        self.assertEqual(error_events[0].error, "Task cancelled by user")

        # Empty return value — never a stale stdout fragment.
        self.assertEqual(result, "")


class TestA2ATaskRunner(unittest.TestCase):
    """Regression coverage for ``A2ABridgeWidget._A2ATaskRunner``.

    These tests exercise the runner in isolation, without spinning up
    the Qt widget. They cover:
      - ``StopIteration.value`` capture (so the widget sees the
        dispatcher's authoritative final result);
      - cancel-vs-failed classification of ``TurnEventType.ERROR``
        events using the runner's public ``is_alive`` / ``cancel`` API.
    """

    def _drain_runner_queue(
        self,
        runner: _A2ATaskRunner,
        timeout: float = 2.0,
    ) -> list[_A2ATaskEvent]:
        """Pull every event the runner enqueued until its thread exits.

        Bounded by ``timeout`` seconds so a hung thread can't deadlock
        the test process. The runner is a daemon thread, so it won't
        block Python exit on its own.
        """
        # Make sure the runner is actually running before we wait;
        # ``start()`` is idempotent for daemon threads but checks the
        # ``is_alive`` state to avoid double-start.
        runner.start()
        deadline = time.monotonic() + timeout
        events: list[_A2ATaskEvent] = []
        while True:
            try:
                events.append(runner.queue.get(timeout=0.05))
            except Exception:
                # ``queue.Empty`` — subclass of ``OSError``; catch a
                # broad set so the qt_stubs (which may mock ``get``)
                # don't surprise us.
                pass
            if not runner.is_alive() and runner.queue.empty():
                break
            if time.monotonic() > deadline:
                self.fail(f"runner did not drain in {timeout}s; events={events}")

        # Drain anything that arrived between the last empty-check
        # and our exit decision. Bounded so a runaway producer can't
        # spin the test forever.
        while not runner.queue.empty():
            events.append(runner.queue.get_nowait())
        return events

    def test_runner_captures_stopiteration_value_as_completed_event(
        self,
    ) -> None:
        """The dispatcher's ``return`` value (``StopIteration.value``)
        must surface as the ``text`` of the terminal COMPLETED event.
        A plain ``for event in gen`` would discard it — regression
        tests the manual ``StopIteration`` handling in ``_run``."""
        dispatcher = _FakeDispatcher(
            events=[
                TurnEvent(type=TurnEventType.TEXT_DELTA, text="streaming chunk"),
            ],
            final_text="FINAL_AUTHORITATIVE_RESULT",
        )
        cancel_event = threading.Event()
        runner = _A2ATaskRunner(
            dispatcher=dispatcher,  # type: ignore[arg-type]
            agent_name="fake",
            task="say hi",
            include_context="",
            cancel_event=cancel_event,
            task_id="task-success",
        )

        events = self._drain_runner_queue(runner)

        # First event is the STARTED marker, last is COMPLETED with
        # the generator's return value.
        self.assertEqual(events[0].type, _A2ARunnerEventType.STARTED)
        terminal = events[-1]
        self.assertEqual(terminal.type, _A2ARunnerEventType.COMPLETED)
        self.assertEqual(terminal.text, "FINAL_AUTHORITATIVE_RESULT")
        # And the streaming chunk was emitted in between.
        types = [e.type for e in events]
        self.assertIn(_A2ARunnerEventType.OUTPUT, types)

    def test_runner_classifies_error_as_failed_when_cancel_event_clear(
        self,
    ) -> None:
        """With the cancel event unset (i.e. the user did NOT hit
        Cancel), a ``TurnEventType.ERROR`` from the dispatcher must
        surface as ``FAILED`` — not ``CANCELLED``. Cancellation is
        only inferred when the cancel event is set."""
        dispatcher = _FakeDispatcher(
            events=[
                TurnEvent(
                    type=TurnEventType.ERROR,
                    error="dispatcher mid-stream error",
                ),
            ],
            final_text="",
        )
        cancel_event = threading.Event()
        runner = _A2ATaskRunner(
            dispatcher=dispatcher,  # type: ignore[arg-type]
            agent_name="fake",
            task="oops",
            include_context="",
            cancel_event=cancel_event,
            task_id="task-failed",
        )

        events = self._drain_runner_queue(runner)

        terminal = events[-1]
        self.assertEqual(terminal.type, _A2ARunnerEventType.FAILED)
        self.assertIn("dispatcher mid-stream error", terminal.text)
        self.assertNotEqual(terminal.type, _A2ARunnerEventType.CANCELLED)
        # The runner's ``is_alive`` contract must hold: thread has
        # exited by the time the queue drained.
        self.assertFalse(runner.is_alive())

    def test_runner_classifies_error_as_cancelled_when_cancel_event_set(
        self,
    ) -> None:
        """Symmetric case: when the cancel event is set
        (the dispatcher polls it and terminates with ERROR), the
        runner MUST surface the same event as ``CANCELLED`` so the
        UI shows the right status. This is the only signal the
        widget uses to decide between ``_on_task_failed`` and
        ``_on_task_cancelled``."""
        dispatcher = _FakeDispatcher(
            events=[
                TurnEvent(
                    type=TurnEventType.ERROR,
                    error="dispatcher mid-stream error",
                ),
            ],
            final_text="",
        )
        cancel_event = threading.Event()
        runner = _A2ATaskRunner(
            dispatcher=dispatcher,  # type: ignore[arg-type]
            agent_name="fake",
            task="please cancel",
            include_context="",
            cancel_event=cancel_event,
            task_id="task-cancelled",
        )

        # Set the cancel event BEFORE starting so the dispatcher's
        # mid-stream ERROR is observed with the event already set.
        cancel_event.set()
        events = self._drain_runner_queue(runner)

        terminal = events[-1]
        self.assertEqual(terminal.type, _A2ARunnerEventType.CANCELLED)
        # Still carrying the dispatcher message for View Output.
        self.assertIn("dispatcher mid-stream error", terminal.text)


if __name__ == "__main__":
    unittest.main()
