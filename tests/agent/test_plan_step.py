"""Plan mode step execution: status reporting for ``plan_step_done``.

Pin: ``_execute_step`` must always emit exactly one ``plan_step_done``
event whose status reflects the actual outcome (``completed``,
``turn_limit``, ``error``). Without that signal the UI step is stuck
"running" forever on every turn-limit or error path.
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.agent.loop import AgentLoop
from rikugan.agent.modes import plan as plan_mode
from rikugan.agent.modes.turn_helpers import TurnResult
from rikugan.agent.turn import TurnEvent, TurnEventType
from rikugan.core.config import RikuganConfig
from rikugan.state.session import SessionState
from rikugan.tests.knowledge._helpers import fresh_store


def _make_loop() -> AgentLoop:
    provider = SimpleNamespace(name="mock", capabilities=SimpleNamespace())
    loop = object.__new__(AgentLoop)
    loop.provider = provider
    loop.session = SessionState()
    loop.config = RikuganConfig()
    loop._cancelled = threading.Event()
    return loop


def _result_ok(*, has_tool_calls: bool = False, tool_calls=None) -> TurnResult:
    return TurnResult(
        text="",
        tool_calls=list(tool_calls or []),
        usage=None,
        error=None,
        cancelled=False,
        disposition="ok",
        recovery_attempted=False,
        recovery_failed=False,
    )


def _result_err() -> TurnResult:
    return TurnResult(
        text="",
        tool_calls=[],
        usage=None,
        error="boom",
        cancelled=False,
        disposition="error",
        recovery_attempted=False,
        recovery_failed=False,
    )


class TestPlanStepStatus(unittest.TestCase):
    def test_completed_status_on_clean_finish(self):
        loop = _make_loop()

        def fake_single_turn(loop_arg, sys_prompt, tools_schema):  # noqa: ARG001
            yield TurnEvent.text_done("done")
            return _result_ok(has_tool_calls=False)

        with patch.object(plan_mode, "execute_single_turn", side_effect=fake_single_turn):
            events = list(plan_mode._execute_step(loop, 0, "do thing", "sys", []))
        dones = [e for e in events if e.type == TurnEventType.PLAN_STEP_DONE]
        self.assertEqual(len(dones), 1)
        self.assertEqual(dones[0].text, "completed")

    def test_error_status_emitted_on_failure(self):
        loop = _make_loop()

        def fake_single_turn(loop_arg, sys_prompt, tools_schema):  # noqa: ARG001
            yield TurnEvent.text_done("partial")
            return _result_err()

        with patch.object(plan_mode, "execute_single_turn", side_effect=fake_single_turn):
            events = list(plan_mode._execute_step(loop, 1, "another", "sys", []))
        dones = [e for e in events if e.type == TurnEventType.PLAN_STEP_DONE]
        self.assertEqual(len(dones), 1)
        self.assertEqual(dones[0].text, "error")

    def test_turn_limit_status_after_max_turns(self):
        loop = _make_loop()

        def fake_single_turn(loop_arg, sys_prompt, tools_schema):  # noqa: ARG001
            # Always returns tool calls -> exhausts the 20-turn budget.
            tc = SimpleNamespace(id="tc_1", name="f", arguments={"x": 1})
            yield TurnEvent.text_done("loop")
            return _result_ok(has_tool_calls=True, tool_calls=[tc])

        with patch.object(plan_mode, "execute_single_turn", side_effect=fake_single_turn):
            events = list(plan_mode._execute_step(loop, 0, "loop forever", "sys", []))
        dones = [e for e in events if e.type == TurnEventType.PLAN_STEP_DONE]
        self.assertEqual(len(dones), 1)
        self.assertEqual(dones[0].text, "turn_limit")


if __name__ == "__main__":
    unittest.main()