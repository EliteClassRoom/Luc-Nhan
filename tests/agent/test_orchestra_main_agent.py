"""Tests for OrchestraMainAgent registry-tool dispatch approval refusal.

The orchestra dispatcher (``run``) executes registry tools directly and has
no UI approval queue, so any tool that requires interactive user approval —
``ToolDefinition.requires_approval=True``, or ``execute_python`` by name —
must be *refused* there (error tool result back to the model), never
silently executed.
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan import constants
from rikugan.core.config import RikuganConfig
from rikugan.tools.base import ToolDefinition
from rikugan.tools.registry import ToolRegistry


def _tool_call_chunk(call_id: str, name: str, args: str = "{}") -> SimpleNamespace:
    """A single stream chunk that opens, fills, and closes one tool call."""
    return SimpleNamespace(
        text="",
        is_tool_call_start=True,
        tool_call_id=call_id,
        tool_name=name,
        tool_args_delta=args,
        is_tool_call_end=True,
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="get_metadata",
            description="plain tool",
            parameters=[],
            handler=lambda: "ran plain",
        )
    )
    registry.register(
        ToolDefinition(
            name="install_microcode_optimizer",
            description="approval-gated tool",
            parameters=[],
            handler=lambda: "ran gated",
            requires_approval=True,
        )
    )
    registry.register(
        ToolDefinition(
            name=constants.EXECUTE_PYTHON_TOOL_NAME,
            description="execute_python under its registry name",
            parameters=[],
            handler=lambda: "ran execute_python",
        )
    )
    return registry


class TestOrchestraApprovalRefusal(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # tests/tools/test_chat_view.py leaves a MagicMock stub module for
        # rikugan.agent.turn in sys.modules; any module imported after it —
        # this test module and main_agent itself — would bind stub
        # TurnEvent factories. Pop the stub and re-import the real modules
        # (same recovery pattern as test_chat_view.TestExecutePythonRouting).
        for mod_name in ("rikugan.agent.turn", "rikugan.agent.orchestra.main_agent"):
            sys.modules.pop(mod_name, None)
        from rikugan.agent.orchestra.main_agent import OrchestraMainAgent
        from rikugan.agent.turn import TurnEventType

        cls.OrchestraMainAgent = OrchestraMainAgent
        cls.TurnEventType = TurnEventType

    def _make_agent(self, registry: ToolRegistry, chunks: list) -> Any:
        """Build an OrchestraMainAgent via __new__ with only what run() needs."""
        provider = MagicMock()
        provider.chat_stream.return_value = iter(chunks)

        agent = self.OrchestraMainAgent.__new__(self.OrchestraMainAgent)
        agent.provider = provider
        agent.tools = registry
        agent.config = RikuganConfig()
        agent.session = MagicMock()
        agent.session.get_messages_for_provider.return_value = []
        agent._build_system_prompt = lambda: "system"  # type: ignore[method-assign]
        agent._get_tools_schema = lambda: []  # type: ignore[method-assign]
        agent._cancelled = threading.Event()
        agent._running = False
        agent._subagent_manager = MagicMock()
        agent._subagent_manager.running_count.return_value = 0
        agent._subagent_manager.poll_event.return_value = None
        return agent

    def _run_one(self, tool_name: str, tool_args: str = "{}") -> tuple[list, ToolRegistry]:
        registry = _registry()
        agent = self._make_agent(registry, [_tool_call_chunk("call_1", tool_name, tool_args)])
        # Spy on the dispatcher's execution path — refusal must not reach it.
        registry.execute = MagicMock(wraps=registry.execute)  # type: ignore[method-assign]
        events = list(agent.run("go"))
        return events, registry

    def _tool_result_events(self, events: list) -> list:
        return [e for e in events if e.type == self.TurnEventType.TOOL_RESULT]

    def test_refuses_tool_with_requires_approval_flag(self):
        events, registry = self._run_one("install_microcode_optimizer")
        results = self._tool_result_events(events)
        self.assertEqual(len(results), 1, f"events: {events}")
        ev = results[0]
        self.assertEqual(ev.tool_call_id, "call_1")
        self.assertEqual(ev.tool_name, "install_microcode_optimizer")
        self.assertTrue(ev.tool_is_error)
        # The refusal must tell the model why: interactive approval is
        # impossible in this mode.
        self.assertIn("approval", ev.tool_result)
        self.assertIn("unavailable", ev.tool_result)
        registry.execute.assert_not_called()

    def test_refuses_execute_python_by_name(self):
        # Defense in depth: the registry definition carries no approval flag,
        # but the execute_python name itself is always approval-gated.
        events, registry = self._run_one(constants.EXECUTE_PYTHON_TOOL_NAME)
        results = self._tool_result_events(events)
        self.assertEqual(len(results), 1, f"events: {events}")
        self.assertTrue(results[0].tool_is_error)
        self.assertIn("approval", results[0].tool_result)
        registry.execute.assert_not_called()

    def test_plain_tool_still_executes(self):
        events, registry = self._run_one("get_metadata")
        results = self._tool_result_events(events)
        self.assertEqual(len(results), 1, f"events: {events}")
        self.assertEqual(results[0].tool_name, "get_metadata")
        self.assertFalse(results[0].tool_is_error)
        self.assertEqual(results[0].tool_result, "ran plain", f"events: {events}")
        registry.execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
