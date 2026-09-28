"""Tests for rikugan.headless.runner."""

from __future__ import annotations

import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.headless.runner import (
    EXIT_APPROVAL_REQUIRED,
    EXIT_CANCELLED,
    EXIT_CONFIG_ERROR,
    EXIT_GENERIC_ERROR,
    EXIT_SUCCESS,
    EXIT_TOOL_FAILURE,
    run_prompt,
)


class _FakeRunner:
    """Simulates a BackgroundAgentRunner with an event queue."""

    def __init__(
        self,
        events: list | None = None,
        agent_loop=None,
    ):
        import queue

        self._queue: queue.Queue = queue.Queue()
        if events:
            for e in events:
                self._queue.put(e)
        self.agent_loop = agent_loop or MagicMock()

    def get_event(self, timeout: float = 0.5):
        import queue

        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


class _FakeController:
    """Simulates a HeadlessSessionController.

    After ``start_agent()``, the controller spawns a daemon thread that
    pushes events into the runner's queue.
    """

    def __init__(self, error_on_start: str | None = None):
        self.error_on_start = error_on_start
        self._runner: _FakeRunner | None = None
        self._is_running = False
        self._finished_called = False
        self.session = MagicMock()
        self.session.id = "test-session-id"
        self._pending_events: list = []

    def _set_events(self, events: list, agent_loop=None) -> None:
        self._pending_events = events
        self._agent_loop = agent_loop

    def start_agent(self, prompt: str) -> str | None:
        if self.error_on_start is not None:
            return self.error_on_start

        self._runner = _FakeRunner(agent_loop=getattr(self, "_agent_loop", None))
        self._is_running = True

        pending = self._pending_events
        runner = self._runner

        def _push() -> None:
            for e in pending:
                runner._queue.put(e)
            time.sleep(0.05)
            self._is_running = False

        import threading

        t = threading.Thread(target=_push, daemon=True)
        t.start()
        return None

    def get_runner(self):
        return self._runner

    @property
    def is_agent_running(self):
        return self._is_running

    def on_agent_finished(self):
        self._finished_called = True
        self._is_running = False


class TestRunPrompt(unittest.TestCase):
    """Tests for the run_prompt() function."""

    def test_config_error_on_start(self):
        ctrl = _FakeController(error_on_start="No API key configured")
        result = run_prompt(ctrl, "test prompt")
        self.assertEqual(result.exit_code, EXIT_CONFIG_ERROR)
        self.assertIn("No API key configured", result.errors)

    def test_no_runner_created(self):
        ctrl = _FakeController()
        ctrl.start_agent = lambda p: None
        result = run_prompt(ctrl, "test prompt")
        self.assertEqual(result.exit_code, EXIT_GENERIC_ERROR)

    def test_normal_completion(self):
        from rikugan.agent.turn import TurnEvent, TurnEventType

        ctrl = _FakeController()
        events = [
            TurnEvent(type=TurnEventType.TEXT_DELTA, text="Hello", turn_number=1),
            TurnEvent(type=TurnEventType.TEXT_DELTA, text=" world", turn_number=1),
            TurnEvent(
                type=TurnEventType.TEXT_DONE,
                text="Hello world",
                turn_number=1,
            ),
        ]
        ctrl._set_events(events)
        result = run_prompt(ctrl, "test prompt")
        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        self.assertEqual(result.final_text, "Hello world")
        self.assertTrue(ctrl._finished_called)

    def test_tool_failure_error(self):
        from rikugan.agent.turn import TurnEvent, TurnEventType

        ctrl = _FakeController()
        events = [
            TurnEvent(
                type=TurnEventType.ERROR,
                error="Decompilation failed",
                turn_number=1,
            ),
            TurnEvent(type=TurnEventType.TEXT_DONE, text="Sorry", turn_number=1),
        ]
        ctrl._set_events(events)
        result = run_prompt(ctrl, "test prompt")
        self.assertEqual(result.exit_code, EXIT_TOOL_FAILURE)
        self.assertIn("Decompilation failed", result.errors)

    def test_cancellation_event(self):
        from rikugan.agent.turn import TurnEvent, TurnEventType

        ctrl = _FakeController()
        events = [
            TurnEvent(type=TurnEventType.CANCELLED, turn_number=1),
        ]
        ctrl._set_events(events)
        result = run_prompt(ctrl, "test prompt")
        self.assertEqual(result.exit_code, EXIT_CANCELLED)

    def test_approval_auto_denied(self):
        from rikugan.agent.turn import TurnEvent, TurnEventType

        agent_loop = MagicMock()
        ctrl = _FakeController()

        events = [
            TurnEvent(
                type=TurnEventType.TOOL_APPROVAL_REQUEST,
                tool_name="execute_python",
                turn_number=1,
            ),
        ]
        ctrl._set_events(events, agent_loop=agent_loop)
        result = run_prompt(ctrl, "test prompt")
        self.assertEqual(result.exit_code, EXIT_APPROVAL_REQUIRED)
        self.assertIn("Approval required", result.errors[0])
        agent_loop.submit_tool_approval.assert_called_with("deny")

    def test_multiple_approval_events(self):
        from rikugan.agent.turn import TurnEvent, TurnEventType

        agent_loop = MagicMock()
        ctrl = _FakeController()

        events = [
            TurnEvent(type=TurnEventType.PLAN_GENERATED, turn_number=1),
            TurnEvent(type=TurnEventType.SAVE_APPROVAL_REQUEST, turn_number=2),
            TurnEvent(type=TurnEventType.USER_QUESTION, turn_number=3),
        ]
        ctrl._set_events(events, agent_loop=agent_loop)
        result = run_prompt(ctrl, "test prompt")
        self.assertEqual(result.exit_code, EXIT_APPROVAL_REQUIRED)
        self.assertEqual(len(result.errors), 3)
        agent_loop.submit_approval.assert_called()
        agent_loop.submit_user_answer.assert_called_with("")

    def test_on_agent_finished_called_on_error(self):
        ctrl = _FakeController()
        ctrl.start_agent = lambda p: None
        result = run_prompt(ctrl, "test prompt")
        self.assertEqual(result.exit_code, EXIT_GENERIC_ERROR)
        self.assertTrue(ctrl._finished_called)


# ---------------------------------------------------------------------------
# Pass-through: reasoning/recovery events must not change final_text/exit_code
# ---------------------------------------------------------------------------


def _run_with_events(events: list, *, json_events: bool = True):
    """Drive *events* through run_prompt and return the RunResult."""
    ctrl = _FakeController()
    ctrl._set_events(events)
    return run_prompt(ctrl, "test prompt", json_events=json_events)


class TestReasoningRecoveryPassThrough(unittest.TestCase):
    """GLM reasoning/recovery events must pass through the headless runner
    without changing final_text, exit_code, or status.  They are captured in
    the JSON event buffer via ``to_dict()`` so downstream consumers can
    observe the recovery lifecycle.
    """

    def test_reasoning_and_recovery_events_do_not_change_final_text_or_exit_code(self):
        from rikugan.agent.turn import TurnEvent

        result = _run_with_events(
            [
                TurnEvent.reasoning_event("hidden"),
                TurnEvent.recovery_start(
                    attempt=2,
                    reason="reasoning_degenerated",
                    discard_transient_reasoning=True,
                ),
                TurnEvent.text_done("visible"),
            ]
        )

        self.assertEqual(result.final_text, "visible")
        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        event_types = [event["type"] for event in result.events]
        self.assertEqual(event_types[:2], ["reasoning_delta", "recovery_start"])

    def test_tool_call_discarded_does_not_mutate_status(self):
        from rikugan.agent.turn import TurnEvent

        result = _run_with_events(
            [
                TurnEvent.tool_call_start("call_1", "read_bytes"),
                TurnEvent.tool_call_discarded("call_1", "read_bytes", "truncated"),
                TurnEvent.text_done("done"),
            ]
        )

        self.assertEqual(result.final_text, "done")
        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        event_types = [event["type"] for event in result.events]
        self.assertIn("tool_call_discarded", event_types)

    def test_recovery_failure_error_is_preserved_as_failure(self):
        """Recovery failure emits a real ERROR event — that IS a failure."""
        from rikugan.agent.turn import TurnEvent

        result = _run_with_events(
            [
                TurnEvent.recovery_start(
                    attempt=2,
                    reason="reasoning_degenerated",
                    discard_transient_reasoning=True,
                ),
                TurnEvent.error_event("Reasoning degeneration persisted after recovery attempt."),
            ]
        )

        self.assertEqual(result.exit_code, EXIT_TOOL_FAILURE)
        self.assertTrue(any("degeneration persisted" in e for e in result.errors))
