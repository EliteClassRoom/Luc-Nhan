"""Tests for the tool approval gate extension (requires_approval).

install_microcode_optimizer exec()s LLM-authored Python, so it must be
approval-gated like execute_python. These tests pin:

1. ``ToolDefinition.requires_approval`` exists (default False).
2. ``install_microcode_optimizer`` sets it to True.
3. ``AgentLoop._execute_single_tool`` routes any tool whose definition
   sets ``requires_approval`` through ``_wait_for_approval`` — deny must
   block execution, allow must run the handler, and plain tools must not
   trigger the gate.
"""

from __future__ import annotations

import os
import sys
import unittest
from collections.abc import Generator as GeneratorType
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.agent.loop import AgentLoop
from rikugan.agent.turn import TurnEventType
from rikugan.core.config import RikuganConfig
from rikugan.core.types import ModelInfo, ProviderCapabilities, ToolCall
from rikugan.providers.base import LLMProvider
from rikugan.state.session import SessionState
from rikugan.tools.base import ParameterSchema, ToolDefinition
from rikugan.tools.registry import ToolRegistry


class _NullProvider(LLMProvider):
    """Provider stub — the gate tests drive _execute_single_tool directly."""

    def __init__(self) -> None:
        super().__init__(api_key="test", model="mock-model")

    @property
    def name(self) -> str:
        return "mock"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def _get_client(self) -> None:
        return None

    def _fetch_models_live(self) -> list[ModelInfo]:
        return [ModelInfo(id="mock-model", name="Mock", provider="mock")]

    @staticmethod
    def _builtin_models() -> list[ModelInfo]:
        return [ModelInfo(id="mock-model", name="Mock", provider="mock")]

    def _format_messages(self, messages: list) -> list:
        return messages

    def _build_request_kwargs(self, messages, tools, temperature, max_tokens, system, **kwargs):
        return {}

    def _call_api(self, client, kwargs):
        return None

    def _normalize_response(self, raw):
        return raw

    def _handle_api_error(self, e: Exception) -> None:
        raise e

    def _stream_chunks(self, client, kwargs, cancel_event=None):
        yield from ()


def _drain_generator_with_return(generator: GeneratorType) -> tuple[list, Any]:
    """Drain a generator, returning events and return value."""
    events: list = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stopped:
            return events, stopped.value


class TestRequiresApprovalFlag(unittest.TestCase):
    def test_tool_definition_requires_approval_defaults_false(self):
        td = ToolDefinition(name="plain", description="d")
        self.assertIs(td.requires_approval, False)

    def test_tool_definition_accepts_requires_approval(self):
        td = ToolDefinition(
            name="x",
            description="d",
            parameters=[],
            handler=lambda: "",
            requires_approval=True,
        )
        self.assertIs(td.requires_approval, True)

    def test_install_microcode_optimizer_flagged_requires_approval(self):
        from rikugan.ida.tools.microcode import install_microcode_optimizer

        # @tool attaches the ToolDefinition as a function attribute.
        defn = getattr(install_microcode_optimizer, "_tool_definition", None)
        self.assertIsNotNone(defn)
        self.assertIs(defn.requires_approval, True)


class TestApprovalGateBehavior(unittest.TestCase):
    """The agent loop must gate requires_approval tools like execute_python."""

    def _make_loop(self, tools: ToolRegistry) -> AgentLoop:
        config = RikuganConfig()
        config.auto_context = False  # Skip IDA API calls
        session = SessionState(provider_name="mock", model_name="mock-model")
        return AgentLoop(provider=_NullProvider(), tool_registry=tools, config=config, session=session)

    @staticmethod
    def _guarded_registry(executed: list[str]) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="guarded_tool",
                description="Executes agent-authored code",
                parameters=[
                    ParameterSchema(name="code", type="string", description="code", required=True)
                ],
                handler=lambda code: (executed.append(code), f"ran {code}")[1],
                requires_approval=True,
            )
        )
        return registry

    def test_requires_approval_tool_requests_approval_and_honors_deny(self):
        executed: list[str] = []
        loop = self._make_loop(self._guarded_registry(executed))
        tc = ToolCall(id="call_gate_1", name="guarded_tool", arguments={"code": "boom"})

        loop._tool_approval_queue.put("deny")
        events, tr = _drain_generator_with_return(loop._execute_single_tool(tc))

        approval_events = [e for e in events if e.type == TurnEventType.TOOL_APPROVAL_REQUEST]
        self.assertEqual(len(approval_events), 1)
        self.assertEqual(approval_events[0].tool_name, "guarded_tool")
        self.assertEqual(executed, [], "denied tool must never execute")
        self.assertTrue(tr.is_error)
        self.assertIn("denied by user", tr.content)

    def test_requires_approval_tool_runs_after_allow(self):
        executed: list[str] = []
        loop = self._make_loop(self._guarded_registry(executed))
        tc = ToolCall(id="call_gate_2", name="guarded_tool", arguments={"code": "ok"})

        loop._tool_approval_queue.put("allow")
        events, tr = _drain_generator_with_return(loop._execute_single_tool(tc))

        self.assertEqual(executed, ["ok"])
        self.assertFalse(tr.is_error)
        self.assertIn("ran ok", tr.content)

    def test_plain_tool_skips_approval_gate(self):
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="plain_tool",
                description="No approval needed",
                parameters=[
                    ParameterSchema(name="code", type="string", description="c", required=True)
                ],
                handler=lambda code: f"ran {code}",
            )
        )
        loop = self._make_loop(registry)
        tc = ToolCall(id="call_gate_3", name="plain_tool", arguments={"code": "x"})

        events, tr = _drain_generator_with_return(loop._execute_single_tool(tc))

        types = [e.type for e in events]
        self.assertNotIn(TurnEventType.TOOL_APPROVAL_REQUEST, types)
        self.assertFalse(tr.is_error)
        self.assertIn("ran x", tr.content)


if __name__ == "__main__":
    unittest.main()
