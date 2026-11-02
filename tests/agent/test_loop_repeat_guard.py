"""End-to-end test for the tool-call repeat ladder.

A real :class:`AgentLoop` drives a scripted provider that keeps issuing
the *same* tool call with the *same* arguments.  The loop must notice
and escalate:

* remind the model to change approach (once, rate-limited),
* stop advertising the tool and refuse to run it,
* finally disable tools for the turn.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.agent.loop import AgentLoop
from lucnhan.core.config import LucNhanConfig
from lucnhan.core.types import (
    Message,
    ModelInfo,
    ProviderCapabilities,
    Role,
    StreamChunk,
    ToolCall,
)
from lucnhan.providers.base import LLMProvider
from lucnhan.state.session import SessionState
from lucnhan.tools.base import ParameterSchema, ToolDefinition
from lucnhan.tools.registry import ToolRegistry

_TOOL_NAME = "decompile_function"
_TOOL_ARGS = {"address": "0x401000"}


class ScriptedProvider(LLMProvider):
    """Provider that always answers with the same tool call."""

    def __init__(self, tool_calls: int):
        super().__init__(api_key="test", model="mock-model")
        self._tool_calls = tool_calls
        self.call_count = 0
        self.tools_seen: list[list[str]] = []

    @property
    def name(self) -> str:
        return "mock"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def _get_client(self):
        return None

    def _fetch_models_live(self) -> list[ModelInfo]:
        return [ModelInfo(id="mock-model", name="Mock", provider="mock")]

    @staticmethod
    def _builtin_models() -> list[ModelInfo]:
        return [ModelInfo(id="mock-model", name="Mock", provider="mock")]

    def _format_messages(self, messages: list[Message]) -> Any:
        return messages

    def _normalize_response(self, raw: Any) -> Any:
        return raw

    def _build_request_kwargs(self, messages, tools, temperature, max_tokens, system, **kwargs):
        return {}

    def _call_api(self, client, kwargs):
        return None

    def _handle_api_error(self, e):
        raise e

    def _stream_chunks(self, client, kwargs, cancel_event=None):
        yield from ()

    def chat(self, messages, tools=None, temperature=0.3, max_tokens=4096, system="", **kwargs):
        return Message(role=Role.ASSISTANT, content="mock response")

    def chat_stream(
        self,
        messages,
        tools=None,
        temperature=0.3,
        max_tokens=4096,
        system="",
        cancel_event=None,
        *,
        request_context=None,
        **kwargs,
    ):
        index = self.call_count
        self.call_count += 1
        self.tools_seen.append([t.get("function", {}).get("name", "") for t in (tools or [])])
        call_id = f"call_{index}"
        if index < self._tool_calls:
            yield StreamChunk(is_tool_call_start=True, tool_call_id=call_id, tool_name=_TOOL_NAME)
            yield StreamChunk(tool_args_delta=json.dumps(_TOOL_ARGS), tool_call_id=call_id)
            yield StreamChunk(is_tool_call_end=True, tool_call_id=call_id, tool_name=_TOOL_NAME)
        else:
            yield StreamChunk(text="done")


def _make_loop(provider: ScriptedProvider, *, mutating: bool = False) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name=_TOOL_NAME,
            description="decompile a function",
            parameters=[ParameterSchema(name="address", type="string", description="addr", required=True)],
            handler=lambda address: f"pseudocode for {address}",
            category="test",
            mutating=mutating,
        )
    )
    config = LucNhanConfig()
    config.auto_context = False
    return AgentLoop(
        provider=provider,
        tool_registry=registry,
        config=config,
        session=SessionState(provider_name="mock", model_name="mock-model"),
    )


def test_repeat_ladder_warns_once_in_the_first_four_turns() -> None:
    """Four identical calls produce exactly one repeat reminder."""
    provider = ScriptedProvider(tool_calls=4)
    loop = _make_loop(provider)

    list(loop.run("decompile 0x401000"))

    assert provider.call_count == 5  # four tool calls, then one final text turn

    repeat_reminders = [
        m for m in loop.session.messages if m.role == "user" and "identical arguments" in (m.content or "")
    ]
    assert len(repeat_reminders) == 1
    assert _TOOL_NAME in repeat_reminders[0].content
    assert "3 times" in repeat_reminders[0].content

    # The tool is withheld once it has been called four times: the next
    # request must not advertise it.
    assert _TOOL_NAME in loop._blocked_tools
    schema_names = [t.get("function", {}).get("name", "") for t in loop._build_tools_schema(None, False)]
    assert _TOOL_NAME not in schema_names


def test_repeat_ladder_blocks_and_stops() -> None:
    """The fifth identical call is blocked; the eighth disables tools."""
    provider = ScriptedProvider(tool_calls=8)
    loop = _make_loop(provider)

    list(loop.run("decompile 0x401000"))

    # Eight identical tool calls, then one final text turn.
    assert provider.call_count == 9

    # The blocked tool disappears from the provider payload.  The block
    # is raised while turn 4 executes, so it takes effect from turn 5.
    assert _TOOL_NAME in provider.tools_seen[0]
    assert _TOOL_NAME in provider.tools_seen[3]
    assert _TOOL_NAME not in provider.tools_seen[4]
    assert _TOOL_NAME not in provider.tools_seen[7]

    # The tool is blocked from executing as well.
    blocked_results = [
        tr
        for m in loop.session.messages
        if m.role == "tool"
        for tr in m.tool_results
        if f"tool '{_TOOL_NAME}' is blocked" in (tr.content or "")
    ]
    assert blocked_results, "expected a blocked-tool error result"

    # After the stop threshold the tools are disabled for the next turn:
    # the final turn is sent without any tool schema at all.
    assert provider.tools_seen[8] == []


def test_blocked_tool_is_withheld_from_the_schema() -> None:
    provider = ScriptedProvider(tool_calls=5)
    loop = _make_loop(provider)

    list(loop.run("decompile 0x401000"))

    assert _TOOL_NAME in loop._blocked_tools
    schema_names = [t.get("function", {}).get("name", "") for t in loop._build_tools_schema(None, False)]
    assert _TOOL_NAME not in schema_names


def _drain(generator: Any) -> Any:
    """Consume *generator*, returning its return value."""
    while True:
        try:
            next(generator)
        except StopIteration as stopped:
            return stopped.value


def test_successful_mutation_records_progress() -> None:
    """A mutating tool that succeeds counts as progress for the run."""
    loop = _make_loop(ScriptedProvider(tool_calls=0), mutating=True)

    tc = ToolCall(id="x", name=_TOOL_NAME, arguments={"address": "0x401000"})
    result = _drain(loop._execute_single_tool(tc))

    assert result is not None
    assert loop._progress.run_made_progress() is True


def test_blocked_tool_is_refused_without_executing() -> None:
    """A blocked tool is refused instead of being run again."""
    loop = _make_loop(ScriptedProvider(tool_calls=0))
    loop._blocked_tools.add(_TOOL_NAME)

    tc = ToolCall(id="x", name=_TOOL_NAME, arguments={"address": "0x401000"})
    result = _drain(loop._execute_single_tool(tc))

    assert result.is_error is True
    assert f"tool '{_TOOL_NAME}' is blocked" in result.content
    # The refused call produced no progress.
    assert loop._progress.run_made_progress() is False
