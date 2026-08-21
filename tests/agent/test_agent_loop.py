"""Tests for the agent loop."""

from __future__ import annotations

import json
import os
import sys
import unittest
from collections.abc import Generator as GeneratorType
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.agent.exploration_mode import ExplorationState
from rikugan.agent.loop import AgentLoop, BackgroundAgentRunner
from rikugan.agent.turn import TurnEvent, TurnEventType
from rikugan.core.config import RikuganConfig
from rikugan.core.types import (
    Message,
    ModelInfo,
    ProviderCapabilities,
    Role,
    StreamChunk,
    TokenUsage,
    ToolCall,
    TurnDisposition,
    TurnOutcome,
)
from rikugan.providers.base import LLMProvider
from rikugan.state.session import SessionState
from rikugan.tools.base import ParameterSchema, ToolDefinition
from rikugan.tools.registry import ToolRegistry


class MockProvider(LLMProvider):
    """Mock LLM provider that returns scripted responses."""

    def __init__(self, responses: list[list[StreamChunk]] | None = None):
        super().__init__(api_key="test", model="mock-model")
        self._responses = responses or []
        self._call_count = 0

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

    def _format_messages(self, messages):
        return messages

    def _normalize_response(self, raw):
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
        if self._call_count < len(self._responses):
            chunks = self._responses[self._call_count]
            self._call_count += 1
            for chunk in chunks:
                yield chunk
        else:
            yield StreamChunk(text="No more scripted responses.")


def _text_response(text: str) -> list[StreamChunk]:
    """Create a simple text-only response."""
    return [
        StreamChunk(text=text),
        StreamChunk(usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)),
    ]


def _text_response_no_usage(text: str) -> list[StreamChunk]:
    """Create a text response with no usage metadata (compat provider behavior)."""
    return [StreamChunk(text=text)]


def _tool_call_response(tool_name: str, args: dict[str, Any], call_id: str = "call_1") -> list[StreamChunk]:
    """Create a response with a tool call."""
    return [
        StreamChunk(is_tool_call_start=True, tool_call_id=call_id, tool_name=tool_name),
        StreamChunk(tool_args_delta=json.dumps(args), tool_call_id=call_id),
        StreamChunk(is_tool_call_end=True, tool_call_id=call_id, tool_name=tool_name),
        StreamChunk(usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)),
    ]


def _drain_generator_with_return(
    generator: GeneratorType,
) -> tuple[list, Any]:
    """Drain a generator, returning events and return value."""
    events: list = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stopped:
            return events, stopped.value


class TestAgentLoop(unittest.TestCase):
    def _make_loop(self, provider: MockProvider, tools: ToolRegistry | None = None) -> AgentLoop:
        config = RikuganConfig()
        config.auto_context = False  # Skip IDA API calls
        session = SessionState(provider_name="mock", model_name="mock-model")
        return AgentLoop(
            provider=provider,
            tool_registry=tools or ToolRegistry(),
            config=config,
            session=session,
        )

    def test_doctor_dispatch_resolves_handler(self):
        """Regression: /doctor must dispatch without NameError.

        The loop_commands import once dropped ``_handle_doctor_command``
        while the dispatcher still referenced it, raising NameError only
        when the user invoked /doctor.
        """
        loop = self._make_loop(MockProvider(responses=[]))
        cmd = SimpleNamespace(direct_command="/doctor", direct_arg="")
        handler = loop._resolve_direct_handler(cmd)
        self.assertIsNotNone(handler)
        events = list(handler())
        joined = "\n".join(e.text for e in events if getattr(e, "text", None))
        self.assertIn("**Luc Nhan Doctor**", joined)

    def test_simple_text_response(self):
        provider = MockProvider(responses=[_text_response("Hello!")])
        loop = self._make_loop(provider)

        events = list(loop.run("Hi"))
        types = [e.type for e in events]
        self.assertIn(TurnEventType.TURN_START, types)
        self.assertIn(TurnEventType.TEXT_DELTA, types)
        self.assertIn(TurnEventType.TEXT_DONE, types)
        self.assertIn(TurnEventType.TURN_END, types)

        text_done = next(e for e in events if e.type == TurnEventType.TEXT_DONE)
        self.assertEqual(text_done.text, "Hello!")

    def test_session_records_messages(self):
        provider = MockProvider(responses=[_text_response("Hi there")])
        config = RikuganConfig()
        config.auto_context = False
        session = SessionState()
        loop = AgentLoop(provider, ToolRegistry(), config, session)

        list(loop.run("Hello"))
        self.assertEqual(len(session.messages), 2)
        self.assertEqual(session.messages[0].role, Role.USER)
        self.assertEqual(session.messages[0].content, "Hello")
        self.assertEqual(session.messages[1].role, Role.ASSISTANT)
        self.assertEqual(session.messages[1].content, "Hi there")

    def test_tool_call_and_result(self):
        # Set up a tool
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="echo_tool",
                description="Echo the input",
                parameters=[ParameterSchema(name="text", type="string", description="Text to echo", required=True)],
                handler=lambda text: f"Echo: {text}",
                category="test",
            )
        )

        # Turn 1: tool call, Turn 2: text response
        provider = MockProvider(
            responses=[
                _tool_call_response("echo_tool", {"text": "hello"}, call_id="call_1"),
                _text_response("The echo returned hello"),
            ]
        )
        loop = self._make_loop(provider, tools=registry)

        events = list(loop.run("Echo hello"))
        types = [e.type for e in events]
        self.assertIn(TurnEventType.TOOL_CALL_START, types)
        self.assertIn(TurnEventType.TOOL_CALL_DONE, types)
        self.assertIn(TurnEventType.TOOL_RESULT, types)

        tool_result = next(e for e in events if e.type == TurnEventType.TOOL_RESULT)
        # TurnEvent now carries the sanitized (wrapped) result, not the raw string.
        self.assertIn("Echo: hello", tool_result.tool_result)
        self.assertFalse(tool_result.tool_is_error)

    def test_reasoning_then_tool_call_end_to_end(self):
        """A reasoning-then-tool-call stream must run the tool call and
        continue the loop. Regression: when reasoning arrives before a
        structured tool call, the agent must NOT stop as a text-only
        completion — the tool call is executable and the turn must
        hand off to tool execution."""
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="echo",
                description="echo",
                parameters=[ParameterSchema(name="text", type="string", description="t", required=True)],
                handler=lambda text: f"Echo: {text}",
                category="test",
            )
        )
        # Turn 1: reasoning deltas, then a structured tool call, then
        # the close. Turn 2: text reply after the tool result.
        tool_call_chunks = [
            StreamChunk(reasoning_delta="Let me think about this. "),
            StreamChunk(reasoning_delta="I need to call echo."),
            StreamChunk(is_tool_call_start=True, tool_call_id="call_e1", tool_name="echo"),
            StreamChunk(tool_args_delta='{"text":', tool_call_id="call_e1"),
            StreamChunk(tool_args_delta='"hi"}', tool_call_id="call_e1"),
            StreamChunk(is_tool_call_end=True, tool_call_id="call_e1", tool_name="echo"),
            StreamChunk(usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)),
        ]
        provider = MockProvider(responses=[tool_call_chunks, _text_response("Done")])
        loop = self._make_loop(provider, tools=registry)

        events = list(loop.run("Think then call echo"))
        types = [e.type for e in events]

        # The reasoning deltas must surface as REASONING_DELTA events.
        self.assertIn(TurnEventType.REASONING_DELTA, types)
        # The tool call lifecycle must be honoured despite the leading
        # reasoning — the agent must NOT report a text-only completion.
        self.assertIn(TurnEventType.TOOL_CALL_START, types)
        self.assertIn(TurnEventType.TOOL_CALL_DONE, types)
        self.assertIn(TurnEventType.TOOL_RESULT, types)
        # The follow-up text reply must reach the stream.
        self.assertIn(TurnEventType.TEXT_DONE, types)
        tool_result = next(e for e in events if e.type == TurnEventType.TOOL_RESULT)
        self.assertIn("Echo: hi", tool_result.tool_result)

    def test_inline_think_then_tool_call_runs_tool(self):
        """Legacy OpenAI o-series streams surface reasoning as inline
        ``<​think>...<​/think>`` text instead of a separate reasoning
        channel. A structured tool call wrapped in this inline thinking
        block must still execute — the agent must not stop at the
        unclosed ``<​think>`` marker."""
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="echo",
                description="echo",
                parameters=[ParameterSchema(name="text", type="string", description="t", required=True)],
                handler=lambda text: f"Echo: {text}",
                category="test",
            )
        )
        # Turn 1: inline 思考 wrapping a tool call. The provider emits
        # ``<​think>thinking`` first, then the tool call lifecycle, then
        # ``<​/think>`` on the closing chunk. Turn 2: text reply.
        chunks = [
            StreamChunk(text="<think>thinking"),
            StreamChunk(is_tool_call_start=True, tool_call_id="call_e2", tool_name="echo"),
            StreamChunk(tool_args_delta='{"text": "hi"}', tool_call_id="call_e2"),
            StreamChunk(is_tool_call_end=True, tool_call_id="call_e2", tool_name="echo"),
            StreamChunk(text="</think>\n", finish_reason="tool_calls"),
            StreamChunk(usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)),
        ]
        provider = MockProvider(responses=[chunks, _text_response("Done")])
        loop = self._make_loop(provider, tools=registry)

        events = list(loop.run("Think then call echo"))
        types = [e.type for e in events]

        # Tool call lifecycle reaches the agent even when the inline
        # thinking close arrives after the tool call.
        self.assertIn(TurnEventType.TOOL_CALL_START, types)
        self.assertIn(TurnEventType.TOOL_CALL_DONE, types)
        self.assertIn(TurnEventType.TOOL_RESULT, types)
        self.assertIn(TurnEventType.TEXT_DONE, types)
        tool_result = next(e for e in events if e.type == TurnEventType.TOOL_RESULT)
        self.assertIn("Echo: hi", tool_result.tool_result)


    def test_tool_error(self):
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="failing_tool",
                description="Always fails",
                parameters=[],
                handler=lambda: (_ for _ in ()).throw(ValueError("bad input")),
                category="test",
            )
        )

        provider = MockProvider(
            responses=[
                _tool_call_response("failing_tool", {}, call_id="call_1"),
                _text_response("Tool failed"),
            ]
        )
        loop = self._make_loop(provider, tools=registry)

        events = list(loop.run("Run failing tool"))
        tool_result = next(e for e in events if e.type == TurnEventType.TOOL_RESULT)
        self.assertTrue(tool_result.tool_is_error)

    def test_cancellation_mid_tool_loop(self):
        """Cancel during a multi-turn tool loop."""
        registry = ToolRegistry()

        def cancel_handler():
            # Cancel during tool execution
            loop.cancel()
            return "done"

        registry.register(
            ToolDefinition(
                name="cancel_trigger",
                description="Triggers cancel",
                parameters=[],
                handler=cancel_handler,
                category="test",
            )
        )

        provider = MockProvider(
            responses=[
                _tool_call_response("cancel_trigger", {}, call_id="call_1"),
                _text_response("Should not reach"),
            ]
        )
        loop = self._make_loop(provider, tools=registry)

        events = list(loop.run("Trigger cancel"))
        types = [e.type for e in events]
        self.assertIn(TurnEventType.CANCELLED, types)
        # Should not reach the second response
        self.assertNotIn(TurnEventType.TEXT_DONE, types)

    def test_is_running_flag(self):
        provider = MockProvider(responses=[_text_response("Done")])
        loop = self._make_loop(provider)
        self.assertFalse(loop.is_running)

        list(loop.run("Hi"))  # consume generator
        self.assertFalse(loop.is_running)

    def test_usage_tracked(self):
        provider = MockProvider(responses=[_text_response("Hi")])
        config = RikuganConfig()
        config.auto_context = False
        session = SessionState()
        loop = AgentLoop(provider, ToolRegistry(), config, session)

        events = list(loop.run("Hello"))
        usage_events = [e for e in events if e.type == TurnEventType.USAGE_UPDATE]
        self.assertTrue(len(usage_events) > 0)
        # Session should accumulate usage; prompt tokens dominate a single text response
        self.assertGreater(session.total_usage.total_tokens, 0)
        self.assertGreater(session.total_usage.prompt_tokens, 0)
        self.assertLess(session.total_usage.completion_tokens, session.total_usage.prompt_tokens)
        # Session total should match the final usage event
        last_usage = usage_events[-1].usage
        self.assertEqual(session.total_usage.total_tokens, last_usage.total_tokens)

    def test_usage_fallback_when_provider_omits_usage(self):
        provider = MockProvider(responses=[_text_response_no_usage("Hi")])
        config = RikuganConfig()
        config.auto_context = False
        session = SessionState()
        loop = AgentLoop(provider, ToolRegistry(), config, session)

        events = list(loop.run("Hello"))
        usage_events = [e for e in events if e.type == TurnEventType.USAGE_UPDATE]

        # Local estimation should still drive token/context tracking.
        self.assertGreater(len(usage_events), 0)
        self.assertGreater(session.last_prompt_tokens, 0)
        self.assertGreater(session.total_usage.total_tokens, 0)

    def test_truncated_output_finish_reason_length_warns_user(self):
        """When finish_reason='length' (output cut by max_tokens), the loop
        MUST surface a warning so the user knows the response is incomplete.

        Without this, the chat appears to end normally mid-sentence — the
        original "chat bị ngắt đột ngột" symptom. The provider already
        streams the partial text via TEXT_DELTA; the loop must additionally
        emit an ERROR event describing the truncation.
        """
        chunks = [
            StreamChunk(text="The answer is partially"),
            StreamChunk(finish_reason="length"),
        ]
        provider = MockProvider(responses=[chunks])
        loop = self._make_loop(provider)

        events = list(loop.run("Tell me"))
        types = [e.type for e in events]

        # Text still streams through so the partial answer is visible.
        self.assertIn(TurnEventType.TEXT_DELTA, types)
        # A warning event must be emitted — currently NONE exists, so this
        # assertion fails until the loop handles finish_reason.
        self.assertIn(TurnEventType.ERROR, types)
        warn = next(e for e in events if e.type == TurnEventType.ERROR)
        self.assertIn("length", (warn.error or "").lower())

    def test_normal_stop_finish_reason_emits_no_warning(self):
        """finish_reason='stop' is a deliberate, complete response — the loop
        must NOT emit a spurious ERROR warning (false positives would train the
        user to ignore real truncation warnings)."""
        chunks = [
            StreamChunk(text="All done."),
            StreamChunk(finish_reason="stop"),
        ]
        provider = MockProvider(responses=[chunks])
        loop = self._make_loop(provider)

        events = list(loop.run("Hi"))
        types = [e.type for e in events]
        self.assertNotIn(TurnEventType.ERROR, types)

    def test_finish_reason_tool_calls_emits_no_warning(self):
        """finish_reason='tool_calls' ends a turn that hands control to tools —
        not a truncation, so no warning."""
        chunks = [
            StreamChunk(text="Let me check."),
            StreamChunk(finish_reason="tool_calls"),
        ]
        provider = MockProvider(responses=[chunks])
        loop = self._make_loop(provider)

        events = list(loop.run("Hi"))
        types = [e.type for e in events]
        self.assertNotIn(TurnEventType.ERROR, types)

    def test_missing_finish_reason_emits_no_warning(self):
        """Some OpenAI-compatible proxies never send a finish_reason. The loop
        must not warn on a missing value (None) — otherwise every response from
        such proxies would show a spurious warning."""
        chunks = [StreamChunk(text="No finish reason here.")]
        provider = MockProvider(responses=[chunks])
        loop = self._make_loop(provider)

        events = list(loop.run("Hi"))
        types = [e.type for e in events]
        self.assertNotIn(TurnEventType.ERROR, types)

    def test_anthropic_max_tokens_stop_reason_warns_user(self):
        """Anthropic's stop_reason uses 'max_tokens' instead of OpenAI's
        'length'. The normalization must map it to the same truncation warning
        so Anthropic users also see why the response was cut."""
        chunks = [
            StreamChunk(text="Partial answer"),
            StreamChunk(finish_reason="max_tokens"),
        ]
        provider = MockProvider(responses=[chunks])
        loop = self._make_loop(provider)

        events = list(loop.run("Hi"))
        types = [e.type for e in events]
        self.assertIn(TurnEventType.ERROR, types)
        warn = next(e for e in events if e.type == TurnEventType.ERROR)
        self.assertIn("length", (warn.error or "").lower())

    def test_anthropic_end_turn_emits_no_warning(self):
        """Anthropic's normal completion stop_reason is 'end_turn' — must be
        treated like OpenAI's 'stop' (no warning)."""
        chunks = [
            StreamChunk(text="Complete answer"),
            StreamChunk(finish_reason="end_turn"),
        ]
        provider = MockProvider(responses=[chunks])
        loop = self._make_loop(provider)

        events = list(loop.run("Hi"))
        types = [e.type for e in events]
        self.assertNotIn(TurnEventType.ERROR, types)

    def test_anthropic_tool_use_stop_reason_emits_no_warning(self):
        """Anthropic's stop_reason 'tool_use' means the model wants to invoke
        a tool — this is a deliberate, complete turn (tool execution follows),
        NOT a truncation.  Must not be treated as an unknown/unexpected reason.

        Regression: ``tool_use`` was missing from the deliberate-completion
        set, so Anthropic streams raised a spurious
        '⚠️ The response ended unexpectedly (finish_reason=tool_use)' warning
        on every tool-calling turn.
        """
        chunks = [
            StreamChunk(text="Let me check."),
            StreamChunk(finish_reason="tool_use"),
        ]
        provider = MockProvider(responses=[chunks])
        loop = self._make_loop(provider)

        events = list(loop.run("Hi"))
        types = [e.type for e in events]
        self.assertNotIn(TurnEventType.ERROR, types)

    def test_content_filter_finish_reason_warns_user(self):
        """finish_reason='content_filter' means the provider suppressed output;
        the user must be told why the response is empty/odd."""
        chunks = [
            StreamChunk(text=""),
            StreamChunk(finish_reason="content_filter"),
        ]
        provider = MockProvider(responses=[chunks])
        loop = self._make_loop(provider)

        events = list(loop.run("Hi"))
        types = [e.type for e in events]
        self.assertIn(TurnEventType.ERROR, types)
        warn = next(e for e in events if e.type == TurnEventType.ERROR)
        self.assertIn("content_filter", (warn.error or "").lower())

    def test_broken_stream_after_partial_text_persists_assistant_message(self):
        """When the SSE stream breaks mid-generation (after partial text),
        the loop MUST:

        1. emit the partial TEXT_DELTA / TEXT_DONE so the user keeps what
           was already streamed,
        2. emit an ERROR event explaining the failure,
        3. persist the assistant message into the session so "continue"
           works and history is not silently dropped.

        Without this, a network drop mid-stream loses everything the user
        already saw — the "chat bị ngắt đột ngột" symptom where text
        disappears and history has a gap.
        """
        from rikugan.core.errors import ProviderError

        class BrokenStreamProvider(MockProvider):
            """Provider whose chat_stream yields partial text then raises a
            non-retryable ProviderError, simulating an SSE stream that drops
            mid-generation (e.g. httpx.RemoteProtocolError classified as a
            generic, non-retryable ProviderError by _handle_api_error)."""

            def chat_stream(
                self, messages, tools=None, temperature=0.3, max_tokens=4096, system="", cancel_event=None, **kwargs
            ):
                yield StreamChunk(text="Partial answer that the user already ")
                yield StreamChunk(text="saw stream by.")
                raise ProviderError(
                    "Connection reset mid-stream",
                    provider="mock",
                    retryable=False,
                )

        provider = BrokenStreamProvider()
        loop = self._make_loop(provider)

        events = list(loop.run("Hi"))
        types = [e.type for e in events]

        # Partial text still visible.
        self.assertIn(TurnEventType.TEXT_DELTA, types)
        self.assertIn(TurnEventType.TEXT_DONE, types)
        # User is told why it stopped.
        self.assertIn(TurnEventType.ERROR, types)
        # Assistant message persisted with the partial text (not dropped).
        assistant_msgs = [m for m in loop.session.messages if m.role == Role.ASSISTANT]
        self.assertEqual(len(assistant_msgs), 1)
        self.assertIn("Partial answer", assistant_msgs[0].content)

    def test_broken_stream_before_any_output_still_raises_to_retry_layer(self):
        """If the stream fails BEFORE any chunk was streamed, there is no
        partial output to preserve — the error must propagate up so the
        retry layer in _stream_llm_turn can handle it as before.  Catching
        it here would silently turn every cold-connection failure into a
        no-op turn."""
        from rikugan.core.errors import ProviderError

        class ColdFailProvider(MockProvider):
            def chat_stream(
                self, messages, tools=None, temperature=0.3, max_tokens=4096, system="", cancel_event=None, **kwargs
            ):
                raise ProviderError("Connection refused", provider="mock", retryable=False)

        provider = ColdFailProvider()
        loop = self._make_loop(provider)

        events = list(loop.run("Hi"))
        types = [e.type for e in events]

        # No partial output → error surfaces as an ERROR event (from run()'s
        # top-level try/except), no TEXT_DONE, and crucially NO assistant
        # message persisted.
        self.assertIn(TurnEventType.ERROR, types)
        self.assertNotIn(TurnEventType.TEXT_DONE, types)
        assistant_msgs = [m for m in loop.session.messages if m.role == Role.ASSISTANT]
        self.assertEqual(len(assistant_msgs), 0)

    def test_cancellation_during_stream_propagates_as_cancelled_event(self):
        """A cancellation raised mid-stream must NOT be swallowed as a
        'partial output' warning — it must become a CANCELLED event so the
        UI's cancellation UX works.  This guards the CancellationError
        re-raise branch in the new try/except."""
        from rikugan.core.errors import CancellationError

        class CancelMidStreamProvider(MockProvider):
            def chat_stream(
                self, messages, tools=None, temperature=0.3, max_tokens=4096, system="", cancel_event=None, **kwargs
            ):
                yield StreamChunk(text="Streaming")
                raise CancellationError("Cancelled mid-stream")

        provider = CancelMidStreamProvider()
        loop = self._make_loop(provider)

        events = list(loop.run("Hi"))
        types = [e.type for e in events]

        self.assertIn(TurnEventType.CANCELLED, types)
        # The partial-warning path must not have fired.
        self.assertNotIn(TurnEventType.ERROR, types)

    def test_broken_stream_with_partial_tool_call_keeps_completed_calls(self):
        """If the stream breaks after some tool calls completed (is_tool_call_end
        seen) but before the turn finished, completed tool calls are preserved
        and executed; an incomplete tool call (only start, no end) is dropped."""
        from rikugan.core.errors import ProviderError

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="echo",
                description="echo",
                parameters=[ParameterSchema(name="text", type="string", description="t", required=True)],
                handler=lambda text: f"Echo: {text}",
                category="test",
            )
        )

        class MixedStreamProvider(MockProvider):
            def chat_stream(
                self, messages, tools=None, temperature=0.3, max_tokens=4096, system="", cancel_event=None, **kwargs
            ):
                # Completed tool call
                yield StreamChunk(is_tool_call_start=True, tool_call_id="c1", tool_name="echo")
                yield StreamChunk(tool_args_delta='{"text": "hi"}', tool_call_id="c1")
                yield StreamChunk(is_tool_call_end=True, tool_call_id="c1", tool_name="echo")
                # Then the stream breaks
                raise ProviderError("dropped", provider="mock", retryable=False)

        provider = MixedStreamProvider()
        loop = self._make_loop(provider, tools=registry)

        events = list(loop.run("Hi"))
        types = [e.type for e in events]

        # Completed tool call result is still emitted and the break is warned.
        self.assertIn(TurnEventType.TOOL_RESULT, types)
        self.assertIn(TurnEventType.ERROR, types)

    def test_execute_python_requires_approval_even_in_explore_only(self):
        provider = MockProvider()
        loop = self._make_loop(provider)
        loop._exploration_state = ExplorationState(explore_only=True)  # /explore context

        tc = ToolCall(
            id="call_approval_test",
            name="execute_python",
            arguments={"code": "print('hi')"},
        )

        gate = loop._wait_for_approval(tc)
        event = next(gate)
        self.assertEqual(event.type, TurnEventType.TOOL_APPROVAL_REQUEST)
        self.assertEqual(event.tool_name, "execute_python")

        loop.submit_tool_approval("allow")
        with self.assertRaises(StopIteration) as done:
            next(gate)
        self.assertTrue(done.exception.value)

    def _collect_question_options(self, loop: AgentLoop, arguments: dict[str, Any]) -> list[str]:
        """Drive _handle_ask_user_tool up to the USER_QUESTION event.

        Feeds an empty answer so the generator completes without blocking.
        Returns the normalized ``options`` list from the event metadata.
        """
        tc = ToolCall(id="call_ask_user_test", name="ask_user", arguments=arguments)
        loop.submit_user_answer("")  # unblock the _wait_for_queue() call
        gen = loop._handle_ask_user_tool(tc)
        question_event = next(gen)
        # Drain remaining events (TOOL_RESULT) so the generator closes cleanly
        try:
            while True:
                next(gen)
        except StopIteration:
            pass
        return list(question_event.metadata.get("options", []))

    def test_ask_user_strips_empty_string_options(self):
        """Empty-string options must be filtered before reaching the UI.

        Regression guard: some LLMs send ``options: [""]`` for open-ended
        questions. The panel treats ``bool([""])`` as truthy, locking the
        text input and rendering a single empty button.
        """
        provider = MockProvider()
        loop = self._make_loop(provider)
        options = self._collect_question_options(loop, {"question": "Where to save?", "options": [""]})
        self.assertEqual(options, [])

    def test_ask_user_preserves_valid_options_when_filtering(self):
        """A mix of empty and valid options keeps only the valid ones."""
        provider = MockProvider()
        loop = self._make_loop(provider)
        options = self._collect_question_options(
            loop,
            {"question": "Continue?", "options": ["", "Yes", "", "No", "   "]},
        )
        self.assertEqual(options, ["Yes", "No"])

    def test_ask_user_missing_options_yields_empty_list(self):
        """No options field at all → empty list (free-text question)."""
        provider = MockProvider()
        loop = self._make_loop(provider)
        options = self._collect_question_options(loop, {"question": "Thoughts?"})
        self.assertEqual(options, [])

    def test_stream_turn_returns_typed_completed_outcome(self):
        provider = MockProvider(responses=[_text_response("done")])
        loop = self._make_loop(provider)
        generator = loop._stream_llm_turn("system", None)
        _events, outcome = _drain_generator_with_return(generator)
        assert isinstance(outcome, TurnOutcome)
        assert outcome.visible_text == "done"
        assert outcome.disposition == TurnDisposition.COMPLETED
        assert outcome.tool_calls == []

    def test_stream_turn_returns_tool_use_outcome(self):
        provider = MockProvider(responses=[_tool_call_response("echo", {"text": "hi"})])
        loop = self._make_loop(provider)
        generator = loop._stream_llm_turn("system", None)
        _events, outcome = _drain_generator_with_return(generator)
        assert isinstance(outcome, TurnOutcome)
        assert outcome.disposition == TurnDisposition.TOOL_USE
        assert len(outcome.tool_calls) == 1
        assert outcome.tool_calls[0].name == "echo"

    def test_stream_turn_returns_truncated_text_outcome(self):
        chunks = [StreamChunk(text="partial answer"), StreamChunk(finish_reason="length")]
        provider = MockProvider(responses=[chunks])
        loop = self._make_loop(provider)
        generator = loop._stream_llm_turn("system", None)
        _events, outcome = _drain_generator_with_return(generator)
        assert isinstance(outcome, TurnOutcome)
        assert outcome.disposition == TurnDisposition.TRUNCATED_TEXT
        assert outcome.visible_text == "partial answer"

    def test_stream_turn_returns_truncated_partial_tool_use(self):
        chunks = [
            StreamChunk(text="thinking..."),
            StreamChunk(is_tool_call_start=True, tool_call_id="c1", tool_name="echo"),
            StreamChunk(tool_args_delta='{"text":', tool_call_id="c1"),
            StreamChunk(finish_reason="length"),
        ]
        provider = MockProvider(responses=[chunks])
        loop = self._make_loop(provider)
        generator = loop._stream_llm_turn("system", None)
        _events, outcome = _drain_generator_with_return(generator)
        assert isinstance(outcome, TurnOutcome)
        assert outcome.disposition == TurnDisposition.TRUNCATED_PARTIAL_TOOL_USE

    def test_stream_turn_returns_stream_broken_outcome(self):
        from rikugan.core.errors import ProviderError

        class BrokenStreamProvider(MockProvider):
            def chat_stream(
                self, messages, tools=None, temperature=0.3, max_tokens=4096, system="", cancel_event=None, **kwargs
            ):
                yield StreamChunk(text="partial")
                raise ProviderError("dropped", provider="mock", retryable=False)

        provider = BrokenStreamProvider()
        loop = self._make_loop(provider)
        generator = loop._stream_llm_turn("system", None)
        _events, outcome = _drain_generator_with_return(generator)
        assert isinstance(outcome, TurnOutcome)
        assert outcome.disposition == TurnDisposition.STREAM_BROKEN
        assert outcome.visible_text == "partial"

    def test_stream_turn_chunked_read_error_triggers_retry(self):
        """A transport-layer ``incomplete chunked read`` must surface to
        the retry layer, not be swallowed as a partial response.

        Regression: previously the partial-output branch kept any
        error with text already streamed, even when the error was a
        transient connection drop. The provider's prefill noise was
        shown to the user as a complete answer, which was wrong.
        """
        from rikugan.core.errors import ProviderError

        call_count = {"n": 0}

        class ChunkedReadProvider(MockProvider):
            def chat_stream(
                self,
                messages,
                tools=None,
                temperature=0.3,
                max_tokens=4096,
                system="",
                cancel_event=None,
                **kwargs,
            ):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    # First attempt: prefill noise then transport drop.
                    yield StreamChunk(text="partial prefill")
                    raise ProviderError(
                        "peer closed connection without sending complete message body (incomplete chunked read)",
                        provider="mock",
                        retryable=True,
                    )
                # Second attempt: clean full response.
                yield StreamChunk(text="Final answer.")
                yield StreamChunk(
                    usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
                )

        provider = ChunkedReadProvider()
        loop = self._make_loop(provider)
        loop.config.max_retries = 3
        loop.config.silent_retry_mode = True
        generator = loop._stream_llm_turn("system", None)
        _events, outcome = _drain_generator_with_return(generator)
        # The provider was called twice: once for the dropped attempt
        # and once for the successful retry.
        assert call_count["n"] == 2, (
            f"expected exactly one retry, got {call_count['n']} calls"
        )
        assert isinstance(outcome, TurnOutcome)
        assert outcome.disposition == TurnDisposition.COMPLETED
        assert outcome.visible_text == "Final answer."
        # The prefill noise from the dropped attempt must NOT leak
        # into the final visible text.
        assert "partial prefill" not in outcome.visible_text

    def test_stream_turn_reasoning_accumulated_separately(self):
        chunks = [
            StreamChunk(reasoning_delta="Thinking step 1. "),
            StreamChunk(reasoning_delta="Step 2. "),
            StreamChunk(text="Final answer."),
            StreamChunk(usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)),
        ]
        provider = MockProvider(responses=[chunks])
        loop = self._make_loop(provider)
        generator = loop._stream_llm_turn("system", None)
        _events, outcome = _drain_generator_with_return(generator)
        assert isinstance(outcome, TurnOutcome)
        assert outcome.visible_text == "Final answer."
        assert "Thinking step 1." in outcome.reasoning_content
        assert "Step 2." in outcome.reasoning_content
        assert "Final answer." not in outcome.reasoning_content

    def test_stream_turn_reasoning_emits_reasoning_events(self):
        chunks = [StreamChunk(reasoning_delta="thinking..."), StreamChunk(text="answer")]
        provider = MockProvider(responses=[chunks])
        loop = self._make_loop(provider)
        generator = loop._stream_llm_turn("system", None)
        _events, _ = _drain_generator_with_return(generator)
        reasoning_events = [e for e in _events if e.type == TurnEventType.REASONING_DELTA]
        assert len(reasoning_events) == 1
        assert "thinking" in reasoning_events[0].reasoning

    def test_orchestra_gate_returns_before_session_prompt_retrieval_or_provider(self):
        """``/orchestra`` is gated: returns one TEXT_DONE event and exits
        before any skill resolution, system prompt, tool schema, session
        append, or provider call. The runtime Orchestra path is currently
        disabled pending its execution and context-isolation hardening.
        """
        provider = MockProvider(responses=[])
        loop = self._make_loop(provider)
        original_messages = list(loop.session.messages)
        loop.session.metadata["active_mode"] = "research"

        with (
            patch.object(loop, "_resolve_skill", side_effect=AssertionError("skill resolution ran")),
            patch.object(loop, "_build_system_prompt", side_effect=AssertionError("prompt built")),
            patch.object(loop, "_build_tools_schema", side_effect=AssertionError("schemas built")),
        ):
            events = list(loop.run("/orchestra analyze this binary"))

        assert [(event.type, event.text) for event in events] == [
            (
                TurnEventType.TEXT_DONE,
                "Orchestra is temporarily disabled while its execution and context isolation contracts are being hardened.",
            )
        ]
        assert loop.session.messages == original_messages
        assert loop.session.metadata["active_mode"] == "research"
        assert provider._call_count == 0

    def test_build_retrieved_knowledge_section_uses_sqlite_when_service_wired(self):
        """When memory_service is not None, the section reads from SQLite."""
        from unittest.mock import MagicMock

        provider = MockProvider(responses=[_text_response("done")])
        loop = self._make_loop(provider)

        mock_service = MagicMock()
        mock_repo = MagicMock()
        mock_repo.list_memories.return_value = []
        mock_repo.list_entities.return_value = []
        mock_repo.list_relations.return_value = []
        mock_repo.count_observations.return_value = 0
        mock_service.repository = mock_repo
        loop.memory_service = mock_service

        loop._build_retrieved_knowledge_section(
            current_address="0x401000",
            current_function="main @ 0x401000",
            profile=loop.config.get_active_profile(),
        )
        # The section may be empty (no memories), but the key assertion is
        # that the repository was queried, proving the SQLite path was taken.
        mock_repo.list_memories.assert_called()

    def test_build_retrieved_knowledge_section_sqlite_path_honors_budget(self):
        """SQLite path passes the configured ``knowledge_max_context_*`` budget
        to ``repository_to_retrieval_pack`` — not ``None``.

        Regression guard: previously the SQLite branch hard-coded
        ``budget=None``, silently ignoring the user's caps and falling back
        to the default ``NORMAL_BUDGET``.
        """
        from unittest.mock import MagicMock, patch

        from rikugan.memory.context import NORMAL_BUDGET, budget_from_config

        provider = MockProvider(responses=[_text_response("done")])
        loop = self._make_loop(provider)

        # Tighten the caps below the defaults so any budget-passthrough
        # failure is observable in the captured kwargs.
        loop.config.knowledge_max_context_items = 4
        loop.config.knowledge_max_context_chars = 1_200

        expected_budget = budget_from_config(loop.config, active_mode="normal")

        mock_service = MagicMock()
        mock_repo = MagicMock()
        mock_repo.list_memories.return_value = []
        mock_repo.list_entities.return_value = []
        mock_repo.list_relations.return_value = []
        mock_repo.count_observations.return_value = 0
        mock_service.repository = mock_repo
        loop.memory_service = mock_service

        with patch("rikugan.memory.sqlite_retrieval.repository_to_retrieval_pack") as pack_mock:
            # Return value must look like a RetrievalPack-shaped object so
            # ``build_section_from_pack`` / ``build_retrieval_metadata`` do
            # not blow up downstream.
            from rikugan.memory.retrieve import RetrievalPack

            pack_mock.return_value = RetrievalPack(
                memories=[],
                entities=[],
                relations=[],
                notes=[],
            )
            loop._build_retrieved_knowledge_section(
                current_address="0x401000",
                current_function="main @ 0x401000",
                profile=loop.config.get_active_profile(),
            )

        pack_mock.assert_called_once()
        kwargs = pack_mock.call_args.kwargs
        passed_budget = kwargs.get("budget")
        self.assertIsNotNone(
            passed_budget,
            "SQLite path must pass a non-None budget; the previous regression "
            "hard-coded budget=None and silently bypassed user caps.",
        )
        # The configured items cap is 4; the default budget holds 12+6+6+2=26.
        self.assertLess(
            sum(
                [
                    passed_budget.max_memories,
                    passed_budget.max_entities,
                    passed_budget.max_relations,
                    passed_budget.max_notes,
                ]
            ),
            NORMAL_BUDGET.max_memories
            + NORMAL_BUDGET.max_entities
            + NORMAL_BUDGET.max_relations
            + NORMAL_BUDGET.max_notes,
            "Passed budget must reflect the tightened user caps, not defaults.",
        )
        self.assertEqual(passed_budget.max_total_chars, expected_budget.max_total_chars)

    def test_build_retrieved_knowledge_section_falls_back_to_jsonl_when_service_none(self):
        """When memory_service is None, the section reads from JSONL via make_store."""
        from unittest.mock import patch

        provider = MockProvider(responses=[_text_response("done")])
        loop = self._make_loop(provider)
        loop.memory_service = None

        # Patch make_store so we don't actually touch the filesystem.
        with patch("rikugan.memory.ingest.make_store") as make_store_mock:
            make_store_mock.return_value = (None, None)
            section = loop._build_retrieved_knowledge_section(
                current_address="0x401000",
                current_function="main",
                profile=loop.config.get_active_profile(),
            )
        # make_store was called (JSONL path was taken), and no error was raised.
        make_store_mock.assert_called()
        # With make_store returning (None, None) the section collapses to "".
        self.assertEqual(section, "")

    def test_build_retrieved_knowledge_section_empty_pack_metadata(self):
        """An empty SQLite pack produces empty counts metadata and empty section."""
        from unittest.mock import MagicMock

        provider = MockProvider(responses=[_text_response("done")])
        loop = self._make_loop(provider)

        mock_service = MagicMock()
        mock_repo = MagicMock()
        mock_repo.list_memories.return_value = []
        mock_repo.list_entities.return_value = []
        mock_repo.list_relations.return_value = []
        mock_repo.count_observations.return_value = 0
        mock_service.repository = mock_repo
        loop.memory_service = mock_service

        section = loop._build_retrieved_knowledge_section(
            current_address="0x401000",
            current_function="main @ 0x401000",
            profile=loop.config.get_active_profile(),
        )
        # session metadata should record the (empty) retrieval counts.
        self.assertIn("last_knowledge_retrieval", loop.session.metadata)
        meta = loop.session.metadata["last_knowledge_retrieval"]
        self.assertIn("counts", meta)
        self.assertEqual(section, "")


class TestStreamOutcomeGuardIntegration(unittest.TestCase):
    def _make_loop(self, provider, tools=None):
        config = RikuganConfig()
        config.auto_context = False
        session = SessionState(provider_name="mock", model_name="mock-model")
        return AgentLoop(provider=provider, tool_registry=tools or ToolRegistry(), config=config, session=session)

    def test_guard_not_created_without_tools(self):
        provider = MockProvider(responses=[_text_response("done")])
        loop = self._make_loop(provider)
        generator = loop._stream_llm_turn("system", None)
        _events, outcome = _drain_generator_with_return(generator)
        assert outcome.disposition == TurnDisposition.COMPLETED
        assert outcome.guard_trigger == ""

    def test_guard_not_created_for_non_glm_provider(self):
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="echo",
                description="echo",
                parameters=[ParameterSchema(name="text", type="string", description="t", required=True)],
                handler=lambda text: f"Echo: {text}",
                category="test",
            )
        )
        provider = MockProvider(responses=[_text_response("done")])
        loop = self._make_loop(provider, tools=registry)
        tools_schema = registry.to_provider_format()
        generator = loop._stream_llm_turn("system", tools_schema)
        _events, outcome = _drain_generator_with_return(generator)
        assert outcome.disposition == TurnDisposition.COMPLETED
        assert outcome.guard_trigger == ""

    def test_guard_trigger_estimates_nonzero_completion(self):
        """When the guard triggers with no provider usage, the AttemptUsage
        must have nonzero exact completion computed via the ceil(bytes/3)
        formula and provenance='estimated'."""
        import math

        # Build a large reasoning payload that will hit the ceiling.
        # The default ceiling is 16384 tokens = 49150 UTF8 bytes.
        # We will send a single chunk with enough bytes.
        big_reasoning = ("Calling tool tool calling execute outputting now." + chr(10)) * 5000

        # No usage chunk emitted -- guard triggers before provider sends usage.
        chunks = [StreamChunk(reasoning_delta=big_reasoning)]

        class GLMMockProvider(MockProvider):
            @property
            def name(self):
                return "glm"

        # Configure GLM with low ceiling for faster trigger
        config = RikuganConfig()
        config.auto_context = False
        config.provider.extra = {"dialect": "glm", "degeneration_guard": {"reasoning_token_ceiling": 1024}}

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="echo",
                description="echo",
                parameters=[ParameterSchema(name="text", type="string", description="t", required=True)],
                handler=lambda text: f"Echo: {text}",
                category="test",
            )
        )

        provider = GLMMockProvider(responses=[chunks])
        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
            session=SessionState(provider_name="glm", model_name="glm-4.6"),
        )
        tools_schema = registry.to_provider_format()
        generator = loop._stream_llm_turn("system", tools_schema)
        _events, outcome = _drain_generator_with_return(generator)

        assert outcome.disposition == TurnDisposition.DEGENERATED
        assert outcome.attempt_usage is not None
        assert outcome.attempt_usage.provenance == "estimated"
        # Nonzero completion
        assert outcome.attempt_usage.usage.completion_tokens > 0
        # Exact: ceil(total_bytes / 3)
        expected = math.ceil(len(big_reasoning.encode("utf-8")) / 3)
        assert outcome.attempt_usage.usage.completion_tokens == expected

    def test_authoritative_usage_wins_over_estimate(self):
        """When provider emits usage before guard trigger, provenance
        must be 'authoritative' even though the guard fires."""
        # Build reasoning that will hit ceiling but usage arrives first.
        big_reasoning = ("Calling tool tool calling execute outputting now." + chr(10)) * 5000

        # Usage chunk arrives BEFORE the guard-triggering reasoning.
        auth_usage = TokenUsage(prompt_tokens=42, completion_tokens=99, total_tokens=141)
        chunks = [
            StreamChunk(usage=auth_usage),
            StreamChunk(reasoning_delta=big_reasoning),
        ]

        class GLMMockProvider(MockProvider):
            @property
            def name(self):
                return "glm"

        config = RikuganConfig()
        config.auto_context = False
        config.provider.extra = {"dialect": "glm", "degeneration_guard": {"reasoning_token_ceiling": 1024}}

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="echo",
                description="echo",
                parameters=[ParameterSchema(name="text", type="string", description="t", required=True)],
                handler=lambda text: f"Echo: {text}",
                category="test",
            )
        )

        provider = GLMMockProvider(responses=[chunks])
        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
            session=SessionState(provider_name="glm", model_name="glm-4.6"),
        )
        tools_schema = registry.to_provider_format()
        generator = loop._stream_llm_turn("system", tools_schema)
        _events, outcome = _drain_generator_with_return(generator)

        assert outcome.disposition == TurnDisposition.DEGENERATED
        assert outcome.attempt_usage is not None
        assert outcome.attempt_usage.provenance == "authoritative"
        assert outcome.attempt_usage.usage.prompt_tokens == 42
        assert outcome.attempt_usage.usage.completion_tokens == 99

    def test_invalid_glm_extra_raises_not_silently_skipped(self):
        """Invalid GLM config values must surface as ValueError, not be
        silently swallowed by a broad except in _maybe_create_guard."""
        from rikugan.core.glm_config import parse_glm_extra

        # An invalid reasoning_token_ceiling (string instead of int) must raise.
        invalid_extra = {"dialect": "glm", "degeneration_guard": {"reasoning_token_ceiling": "not_an_int"}}

        # Verify the parser itself raises
        with self.assertRaises(ValueError):
            parse_glm_extra(invalid_extra, "glm-4.6")

        # Verify the guard creation surfaces the error
        class GLMMockProvider(MockProvider):
            @property
            def name(self):
                return "glm"

        config = RikuganConfig()
        config.auto_context = False
        config.provider.extra = invalid_extra

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="echo",
                description="echo",
                parameters=[ParameterSchema(name="text", type="string", description="t", required=True)],
                handler=lambda text: f"Echo: {text}",
                category="test",
            )
        )

        provider = GLMMockProvider(responses=[[_text_response("done")]])
        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
            session=SessionState(provider_name="glm", model_name="glm-4.6"),
        )
        tools_schema = registry.to_provider_format()

        with self.assertRaises(ValueError):
            loop._maybe_create_guard(tools_schema)


# ---------------------------------------------------------------------------
# GLM strict partial-tool-call handling (Task 9)
# ---------------------------------------------------------------------------


class GLMMockProvider(MockProvider):
    """Mock provider that advertises itself as GLM."""

    @property
    def name(self) -> str:
        return "glm"


def _make_glm_loop(
    responses: list[list[StreamChunk]],
    tools: ToolRegistry | None = None,
) -> AgentLoop:
    """Create an AgentLoop configured as GLM with the given scripted responses."""
    config = RikuganConfig()
    config.auto_context = False
    config.provider.extra = {
        "dialect": "glm",
        "degeneration_guard": {"reasoning_token_ceiling": 65536},
    }
    session = SessionState(provider_name="glm", model_name="glm-4.6")
    provider = GLMMockProvider(responses=responses)
    return AgentLoop(
        provider=provider,
        tool_registry=tools or ToolRegistry(),
        config=config,
        session=session,
    )


def _glm_tool_schema(tools: ToolRegistry | None) -> list | None:
    if tools is None or len(tools._tools) == 0:
        return None
    return tools.to_provider_format()


def run_glm_stream(
    chunks: list[StreamChunk],
    tools: ToolRegistry | None = None,
) -> tuple[TurnOutcome, list]:
    """Stream chunks through a GLM-configured loop's inner stream."""
    loop = _make_glm_loop([chunks], tools=tools)
    generator = loop._stream_llm_turn_inner("system", _glm_tool_schema(tools))
    events, outcome = _drain_generator_with_return(generator)
    return outcome, events


def _echo_registry() -> ToolRegistry:
    """A minimal registry with read_bytes and get_pseudocode tools."""
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="read_bytes",
            description="Read bytes at address",
            parameters=[ParameterSchema(name="address", type="integer", description="addr", required=True)],
            handler=lambda address: f"bytes at {address}",
            category="test",
        )
    )
    registry.register(
        ToolDefinition(
            name="get_pseudocode",
            description="Get pseudocode",
            parameters=[ParameterSchema(name="address", type="integer", description="addr", required=True)],
            handler=lambda address: f"pseudocode at {address}",
            category="test",
        )
    )
    return registry


class TestGLMStrictPartialToolCalls(unittest.TestCase):
    """GLM must reject incomplete/malformed tool calls instead of
    silently falling back to {}."""

    def test_length_cutoff_keeps_only_safe_completed_tool_prefix(self):
        chunks = [
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", is_tool_call_start=True),
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", tool_args_delta='{"address":4096}'),
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", is_tool_call_end=True),
            StreamChunk(tool_call_id="call_2", tool_name="get_pseudocode", is_tool_call_start=True),
            StreamChunk(tool_call_id="call_2", tool_name="get_pseudocode", tool_args_delta='{"address":'),
            StreamChunk(finish_reason="length"),
        ]
        outcome, events = run_glm_stream(chunks, tools=_echo_registry())

        assert outcome.disposition == TurnDisposition.TRUNCATED_PARTIAL_TOOL_USE
        assert [call.id for call in outcome.tool_calls] == ["call_1"]
        discarded = [e for e in events if e.type == TurnEventType.TOOL_CALL_DISCARDED and e.tool_call_id == "call_2"]
        assert len(discarded) == 1

    def test_first_call_incomplete_discards_everything(self):
        chunks = [
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", is_tool_call_start=True),
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", tool_args_delta='{"address":'),
            StreamChunk(tool_call_id="call_2", tool_name="get_pseudocode", is_tool_call_start=True),
            StreamChunk(tool_call_id="call_2", tool_name="get_pseudocode", tool_args_delta='{"address":8192}'),
            StreamChunk(tool_call_id="call_2", tool_name="get_pseudocode", is_tool_call_end=True),
            StreamChunk(finish_reason="length"),
        ]
        outcome, events = run_glm_stream(chunks, tools=_echo_registry())

        assert outcome.disposition == TurnDisposition.TRUNCATED_PARTIAL_TOOL_USE
        assert outcome.tool_calls == []
        discarded_ids = {e.tool_call_id for e in events if e.type == TurnEventType.TOOL_CALL_DISCARDED}
        assert discarded_ids == {"call_1", "call_2"}

    def test_glm_malformed_json_at_end_is_discarded_not_fallback_empty(self):
        chunks = [
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", is_tool_call_start=True),
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", tool_args_delta='{"address":'),
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", is_tool_call_end=True),
            StreamChunk(finish_reason="length"),
        ]
        outcome, events = run_glm_stream(chunks, tools=_echo_registry())

        assert outcome.disposition == TurnDisposition.TRUNCATED_PARTIAL_TOOL_USE
        assert outcome.tool_calls == []
        discarded = [e for e in events if e.type == TurnEventType.TOOL_CALL_DISCARDED]
        assert len(discarded) == 1
        assert discarded[0].tool_call_id == "call_1"

    def test_non_glm_keeps_malformed_json_fallback(self):
        config = RikuganConfig()
        config.auto_context = False
        session = SessionState(provider_name="mock", model_name="mock-model")
        registry = _echo_registry()
        provider = MockProvider(
            responses=[
                [
                    StreamChunk(tool_call_id="call_1", tool_name="read_bytes", is_tool_call_start=True),
                    StreamChunk(tool_call_id="call_1", tool_name="read_bytes", tool_args_delta='{"broken":'),
                    StreamChunk(tool_call_id="call_1", tool_name="read_bytes", is_tool_call_end=True),
                    StreamChunk(finish_reason="stop"),
                ],
            ]
        )
        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
            session=session,
        )
        generator = loop._stream_llm_turn_inner("system", registry.to_provider_format())
        events, outcome = _drain_generator_with_return(generator)

        assert len(outcome.tool_calls) == 1
        assert outcome.tool_calls[0].arguments == {}
        warn_events = [e for e in events if e.type == TurnEventType.ERROR]
        assert len(warn_events) >= 1
        discarded = [e for e in events if e.type == TurnEventType.TOOL_CALL_DISCARDED]
        assert len(discarded) == 0

    def test_duplicate_end_chunk_does_not_double_discard(self):
        chunks = [
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", is_tool_call_start=True),
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", tool_args_delta='{"address":'),
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", is_tool_call_end=True),
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", is_tool_call_end=True),
            StreamChunk(finish_reason="length"),
        ]
        _outcome, events = run_glm_stream(chunks, tools=_echo_registry())

        discarded = [e for e in events if e.type == TurnEventType.TOOL_CALL_DISCARDED and e.tool_call_id == "call_1"]
        assert len(discarded) == 1

    def test_completed_then_malformed_at_length_persists_safe_prefix(self):
        chunks = [
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", is_tool_call_start=True),
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", tool_args_delta='{"address":4096}'),
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", is_tool_call_end=True),
            StreamChunk(tool_call_id="call_2", tool_name="get_pseudocode", is_tool_call_start=True),
            StreamChunk(tool_call_id="call_2", tool_name="get_pseudocode", tool_args_delta='{"address":'),
            StreamChunk(tool_call_id="call_2", tool_name="get_pseudocode", is_tool_call_end=True),
            StreamChunk(finish_reason="length"),
        ]
        outcome, events = run_glm_stream(chunks, tools=_echo_registry())

        assert [call.id for call in outcome.tool_calls] == ["call_1"]
        assert [call.arguments for call in outcome.tool_calls] == [{"address": 4096}]
        discarded = {e.tool_call_id for e in events if e.type == TurnEventType.TOOL_CALL_DISCARDED}
        assert discarded == {"call_2"}

    def test_glm_all_calls_complete_no_discard(self):
        chunks = [
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", is_tool_call_start=True),
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", tool_args_delta='{"address":4096}'),
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", is_tool_call_end=True),
            StreamChunk(tool_call_id="call_2", tool_name="get_pseudocode", is_tool_call_start=True),
            StreamChunk(tool_call_id="call_2", tool_name="get_pseudocode", tool_args_delta='{"address":8192}'),
            StreamChunk(tool_call_id="call_2", tool_name="get_pseudocode", is_tool_call_end=True),
            StreamChunk(finish_reason="tool_calls"),
        ]
        outcome, events = run_glm_stream(chunks, tools=_echo_registry())

        assert outcome.disposition == TurnDisposition.TOOL_USE
        assert [call.id for call in outcome.tool_calls] == ["call_1", "call_2"]
        discarded = [e for e in events if e.type == TurnEventType.TOOL_CALL_DISCARDED]
        assert len(discarded) == 0

    def test_glm_tool_states_ordered_by_start_not_dict_order(self):
        """Tool-call states must be tracked by start order, not dict
        hash order.  call_2 starts first and is incomplete; the
        contiguous safe prefix is empty regardless of call_1 completing."""
        chunks = [
            StreamChunk(tool_call_id="call_2", tool_name="get_pseudocode", is_tool_call_start=True),
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", is_tool_call_start=True),
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", tool_args_delta='{"address":4096}'),
            StreamChunk(tool_call_id="call_1", tool_name="read_bytes", is_tool_call_end=True),
            StreamChunk(tool_call_id="call_2", tool_name="get_pseudocode", tool_args_delta='{"address":'),
            StreamChunk(finish_reason="length"),
        ]
        outcome, events = run_glm_stream(chunks, tools=_echo_registry())

        # call_2 started first and is incomplete -> safe prefix is empty
        assert outcome.tool_calls == []
        discarded_ids = {e.tool_call_id for e in events if e.type == TurnEventType.TOOL_CALL_DISCARDED}
        assert discarded_ids == {"call_1", "call_2"}


class TestGLMPartialToolCallPersistence(unittest.TestCase):
    """Verify that persisted assistant message and tool-result message
    contain exactly the safe prefix -- one-to-one call/result pairing."""

    def test_one_safe_call_plus_one_incomplete_persists_one_to_one(self):
        registry = _echo_registry()
        config = RikuganConfig()
        config.auto_context = False
        config.provider.extra = {
            "dialect": "glm",
            "degeneration_guard": {"reasoning_token_ceiling": 65536},
        }
        session = SessionState(provider_name="glm", model_name="glm-4.6")
        provider = GLMMockProvider(
            responses=[
                [
                    StreamChunk(tool_call_id="call_1", tool_name="read_bytes", is_tool_call_start=True),
                    StreamChunk(tool_call_id="call_1", tool_name="read_bytes", tool_args_delta='{"address":4096}'),
                    StreamChunk(tool_call_id="call_1", tool_name="read_bytes", is_tool_call_end=True),
                    StreamChunk(tool_call_id="call_2", tool_name="get_pseudocode", is_tool_call_start=True),
                    StreamChunk(tool_call_id="call_2", tool_name="get_pseudocode", tool_args_delta='{"address":'),
                    StreamChunk(finish_reason="length"),
                ],
            ]
        )
        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
            session=session,
        )
        tools_schema = registry.to_provider_format()

        from rikugan.agent.modes.turn_helpers import execute_single_turn

        list(execute_single_turn(loop, "system", tools_schema))

        assistant_msgs = [m for m in session.messages if m.role == Role.ASSISTANT]
        assert len(assistant_msgs) == 1
        assistant = assistant_msgs[0]
        assert [call.id for call in assistant.tool_calls] == ["call_1"]

        tool_msgs = [m for m in session.messages if m.role == Role.TOOL]
        assert len(tool_msgs) == 1
        tool_message = tool_msgs[0]
        assert [r.tool_call_id for r in tool_message.tool_results] == ["call_1"]


class TestBackgroundAgentRunner(unittest.TestCase):
    def test_run_in_background(self):
        provider = MockProvider(responses=[_text_response("Background response")])
        config = RikuganConfig()
        config.auto_context = False
        session = SessionState()
        loop = AgentLoop(provider, ToolRegistry(), config, session)
        runner = BackgroundAgentRunner(loop)

        runner.start("Hello from background")

        events = []
        while True:
            event = runner.get_event(timeout=2.0)
            if event is None:
                break
            events.append(event)

        types = [e.type for e in events]
        self.assertIn(TurnEventType.TEXT_DONE, types)
        text_done = next(e for e in events if e.type == TurnEventType.TEXT_DONE)
        self.assertEqual(text_done.text, "Background response")


class TestSkillInvocation(unittest.TestCase):
    def test_skill_rewrite(self):
        """Test that /slug messages get rewritten with skill body."""
        import tempfile

        from rikugan.skills.registry import SkillRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = os.path.join(tmpdir, "test-skill")
            os.makedirs(skill_dir)
            with open(os.path.join(skill_dir, "SKILL.md"), "w") as f:
                f.write("---\nname: Test Skill\ndescription: A test\n---\nYou are a test skill.\n")

            registry = SkillRegistry(tmpdir)
            registry.discover()

            provider = MockProvider(responses=[_text_response("Skill response")])
            config = RikuganConfig()
            config.auto_context = False
            session = SessionState()
            loop = AgentLoop(provider, ToolRegistry(), config, session, skill_registry=registry)

            list(loop.run("/test-skill do something"))

            # The user message in session should contain the skill body
            user_msg = session.messages[0]
            self.assertIn("[Skill: Test Skill]", user_msg.content)
            self.assertIn("You are a test skill.", user_msg.content)
            self.assertIn("do something", user_msg.content)


class TestProfileEnforcement(unittest.TestCase):
    """Test that analysis profiles are enforced in the agent loop."""

    def _make_loop_with_profile(
        self,
        profile_name: str,
        provider: MockProvider,
        tools: ToolRegistry = None,
        custom_profiles: dict = None,
    ) -> AgentLoop:
        config = RikuganConfig()
        config.auto_context = False
        config.active_profile = profile_name
        if custom_profiles:
            config.custom_profiles = custom_profiles
        session = SessionState(provider_name="mock", model_name="mock-model")
        return AgentLoop(
            provider=provider,
            tool_registry=tools or ToolRegistry(),
            config=config,
            session=session,
        )

    def test_private_profile_skips_binary_info(self):
        """Private profile should not call get_binary_info."""
        config = RikuganConfig()
        config.auto_context = True  # Enable auto-context
        config.active_profile = "private"

        registry = ToolRegistry()
        calls = []

        def track_binary_info():
            calls.append("get_binary_info")
            return "Binary: test.exe"

        registry.register(
            ToolDefinition(
                name="get_binary_info",
                description="Get binary info",
                parameters=[],
                handler=track_binary_info,
                category="context",
            )
        )

        provider = MockProvider(responses=[_text_response("Done")])
        session = SessionState(provider_name="mock", model_name="mock-model")
        loop = AgentLoop(provider, registry, config, session)

        list(loop.run("Hi"))
        # get_binary_info should NOT have been called because private profile hides metadata
        self.assertEqual(calls, [])

    def test_ioc_stripping_in_tool_results(self):
        """ioc_filters should strip hashes/IPs from tool results."""
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="test_tool",
                description="Returns IOC data",
                parameters=[],
                handler=lambda: "Hash: d41d8cd98f00b204e9800998ecf8427e, IP: 10.0.0.1",
                category="test",
            )
        )

        # Use private profile which has all ioc_filters enabled
        config = RikuganConfig()
        config.auto_context = False
        config.active_profile = "private"
        session = SessionState(provider_name="mock", model_name="mock-model")

        provider = MockProvider(
            responses=[
                _tool_call_response("test_tool", {}, call_id="call_ioc"),
                _text_response("Done"),
            ]
        )
        loop = AgentLoop(provider, registry, config, session)

        events = list(loop.run("Run test"))
        tool_result_event = next(
            (e for e in events if e.type == TurnEventType.TOOL_RESULT),
            None,
        )
        self.assertIsNotNone(tool_result_event)
        # IOCs should be redacted
        self.assertIn("[HASH_REDACTED]", tool_result_event.tool_result)
        self.assertIn("[IP_REDACTED]", tool_result_event.tool_result)

    def test_denied_tools_filtered_from_schema(self):
        """Denied tools should not appear in the tools schema."""
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="allowed_tool",
                description="Allowed",
                parameters=[],
                handler=lambda: "ok",
                category="test",
            )
        )
        registry.register(
            ToolDefinition(
                name="denied_tool",
                description="Denied",
                parameters=[],
                handler=lambda: "ok",
                category="test",
            )
        )

        custom_profiles = {
            "restricted": {
                "name": "restricted",
                "denied_tools": ["denied_tool"],
            }
        }

        provider = MockProvider(responses=[_text_response("Done")])
        loop = self._make_loop_with_profile(
            "restricted",
            provider,
            tools=registry,
            custom_profiles=custom_profiles,
        )

        schema = loop._build_tools_schema(None, False)
        tool_names = [t.get("function", {}).get("name") for t in schema]
        self.assertIn("allowed_tool", tool_names)
        self.assertNotIn("denied_tool", tool_names)

    def test_granular_ioc_filter_only_selected(self):
        """Only selected IOC categories should be redacted."""
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="test_tool",
                description="Returns mixed IOCs",
                parameters=[],
                handler=lambda: "Hash: d41d8cd98f00b204e9800998ecf8427e, IP: 10.0.0.1, url: http://evil.com/bad",
                category="test",
            )
        )

        # Custom profile with only hashes enabled
        custom_profiles = {
            "hash-only": {
                "name": "hash-only",
                "ioc_filters": {"hashes": True, "ipv4": False, "urls": False},
            }
        }
        config = RikuganConfig()
        config.auto_context = False
        config.active_profile = "hash-only"
        config.custom_profiles = custom_profiles
        session = SessionState(provider_name="mock", model_name="mock-model")

        provider = MockProvider(
            responses=[
                _tool_call_response("test_tool", {}, call_id="call_granular"),
                _text_response("Done"),
            ]
        )
        loop = AgentLoop(provider, registry, config, session)

        events = list(loop.run("Run test"))
        tool_result_event = next(
            (e for e in events if e.type == TurnEventType.TOOL_RESULT),
            None,
        )
        self.assertIsNotNone(tool_result_event)
        self.assertIn("[HASH_REDACTED]", tool_result_event.tool_result)
        # IP and URL should NOT be redacted
        self.assertIn("10.0.0.1", tool_result_event.tool_result)
        self.assertIn("http://evil.com/bad", tool_result_event.tool_result)

    def test_custom_filter_rule_in_tool_result(self):
        """Custom filter rules should be applied to tool results."""
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="test_tool",
                description="Returns sensitive data",
                parameters=[],
                handler=lambda: "hostname: DESKTOP-VICTIM, key: sk-abcdef1234567890",
                category="test",
            )
        )

        custom_profiles = {
            "custom-rules": {
                "name": "custom-rules",
                "ioc_filters": {},
                "custom_filter_rules": [
                    {"name": "host", "pattern": "DESKTOP-VICTIM", "is_regex": False, "replacement": "[HOST]"},
                    {"name": "key", "pattern": r"sk-[a-zA-Z0-9]+", "is_regex": True, "replacement": "[KEY]"},
                ],
            }
        }
        config = RikuganConfig()
        config.auto_context = False
        config.active_profile = "custom-rules"
        config.custom_profiles = custom_profiles
        session = SessionState(provider_name="mock", model_name="mock-model")

        provider = MockProvider(
            responses=[
                _tool_call_response("test_tool", {}, call_id="call_custom"),
                _text_response("Done"),
            ]
        )
        loop = AgentLoop(provider, registry, config, session)

        events = list(loop.run("Run test"))
        tool_result_event = next(
            (e for e in events if e.type == TurnEventType.TOOL_RESULT),
            None,
        )
        self.assertIsNotNone(tool_result_event)
        self.assertIn("[HOST]", tool_result_event.tool_result)
        self.assertIn("[KEY]", tool_result_event.tool_result)
        self.assertNotIn("DESKTOP-VICTIM", tool_result_event.tool_result)

    def test_default_profile_no_filtering(self):
        """Default profile should not strip IOCs or hide metadata."""
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="test_tool",
                description="Returns data",
                parameters=[],
                handler=lambda: "Hash: d41d8cd98f00b204e9800998ecf8427e",
                category="test",
            )
        )

        config = RikuganConfig()
        config.auto_context = False
        config.active_profile = "default"
        session = SessionState(provider_name="mock", model_name="mock-model")

        provider = MockProvider(
            responses=[
                _tool_call_response("test_tool", {}, call_id="call_def"),
                _text_response("Done"),
            ]
        )
        loop = AgentLoop(provider, registry, config, session)

        events = list(loop.run("Run test"))
        tool_result_event = next(
            (e for e in events if e.type == TurnEventType.TOOL_RESULT),
            None,
        )
        self.assertIsNotNone(tool_result_event)
        # Default profile does NOT strip IOCs
        self.assertNotIn("[HASH_REDACTED]", tool_result_event.tool_result)

    def test_denied_tool_blocked_at_execution(self):
        """Denied tools should be blocked at execution time, not just schema filtering."""
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="list_functions",
                description="Lists functions",
                parameters=[],
                handler=lambda: "func1\nfunc2\nfunc3",
                category="functions",
            )
        )

        custom_profiles = {
            "restricted": {
                "name": "restricted",
                "denied_tools": ["list_functions"],
            }
        }

        # LLM tries to call the denied tool anyway
        provider = MockProvider(
            responses=[
                _tool_call_response("list_functions", {}, call_id="call_denied"),
                _text_response("Done"),
            ]
        )
        loop = self._make_loop_with_profile(
            "restricted",
            provider,
            tools=registry,
            custom_profiles=custom_profiles,
        )

        events = list(loop.run("list functions"))
        tool_result_event = next(
            (e for e in events if e.type == TurnEventType.TOOL_RESULT),
            None,
        )
        self.assertIsNotNone(tool_result_event)
        # Tool should be blocked with an error, not executed
        self.assertIn("denied by the active profile", tool_result_event.tool_result)
        self.assertNotIn("func1", tool_result_event.tool_result)


class TestReasoningRunnerCoalescing(unittest.TestCase):
    """Verify BackgroundAgentRunner keeps reasoning and text buffers separate
    and flushes correctly on type switch, RECOVERY_START boundary, exception,
    and finally path."""

    def _run_sequence_under_backpressure(self, events: list[TurnEvent]) -> list[TurnEvent]:
        """Feed events through a runner whose queue always reports full.

        Uses a custom queue subclass that overrides ``full()`` to return
        ``True``, forcing every delta into the buffer/coalescing branch
        while ``put`` still succeeds normally. This deterministically
        exercises the coalescing path without timing races.
        """
        import queue as queue_mod

        class _AlwaysFullQueue(queue_mod.Queue):
            """Queue that always reports full so deltas get buffered."""

            def full(self) -> bool:  # type: ignore[override]
                return True

        provider = MockProvider(responses=[_text_response("x")])
        config = RikuganConfig()
        config.auto_context = False
        session = SessionState()
        loop = AgentLoop(provider, ToolRegistry(), config, session)
        loop.run = lambda user_message: iter(events)  # type: ignore[assignment]

        runner = BackgroundAgentRunner(loop)
        runner.event_queue = _AlwaysFullQueue(maxsize=1000)
        runner.start("test")

        collected: list[TurnEvent] = []
        while True:
            event = runner.get_event(timeout=5.0)
            if event is None:
                break
            collected.append(event)
        return collected

    def test_reasoning_and_text_buffers_stay_separate(self):
        events = [
            TurnEvent.reasoning_event("r1"),
            TurnEvent.reasoning_event("r2"),
            TurnEvent.text_delta("v1"),
            TurnEvent.recovery_start(
                attempt=2,
                reason="reasoning_degenerated",
                discard_transient_reasoning=True,
            ),
        ]
        emitted = self._run_sequence_under_backpressure(events)

        types = [event.type for event in emitted]
        # Reasoning deltas coalesce into one, text is separate, recovery
        # is a hard boundary.
        assert types == [
            TurnEventType.REASONING_DELTA,
            TurnEventType.TEXT_DELTA,
            TurnEventType.RECOVERY_START,
        ], f"unexpected order: {types}"
        assert emitted[0].reasoning == "r1r2"

    def test_reasoning_flushed_on_exception(self):
        """When the loop raises after buffered reasoning, the reasoning
        buffer must be flushed (or explicitly discarded) before the error
        event reaches the queue."""

        class _Exploder:
            def __call__(self, user_message: str):
                yield TurnEvent.reasoning_event("partial")
                raise RuntimeError("boom")

        events: list[TurnEvent] = []
        provider = MockProvider(responses=[_text_response("x")])
        config = RikuganConfig()
        config.auto_context = False
        session = SessionState()
        loop = AgentLoop(provider, ToolRegistry(), config, session)
        loop.run = _Exploder()  # type: ignore[assignment]

        runner = BackgroundAgentRunner(loop)
        runner.start("test")
        while True:
            event = runner.get_event(timeout=5.0)
            if event is None:
                break
            events.append(event)

        types = [e.type for e in events]
        # The reasoning delta must appear before the error event
        # (flushed in the except block).
        if TurnEventType.REASONING_DELTA in types:
            assert types.index(TurnEventType.REASONING_DELTA) < types.index(TurnEventType.ERROR)
        # Error must be present
        assert TurnEventType.ERROR in types

    def test_text_delta_pass_through_low_latency(self):
        """With a default-sized queue (not full), each TEXT_DELTA is
        delivered individually — no buffering. The first text chunk
        must be obtainable before TEXT_DONE arrives, proving streaming
        latency is preserved."""
        provider = MockProvider(responses=[_text_response("x")])
        config = RikuganConfig()
        config.auto_context = False
        session = SessionState()
        loop = AgentLoop(provider, ToolRegistry(), config, session)

        chunks = [
            TurnEvent.text_delta("chunk1"),
            TurnEvent.text_delta("chunk2"),
            TurnEvent.text_done("chunk1chunk2"),
        ]
        loop.run = lambda user_message: iter(chunks)  # type: ignore[assignment]

        runner = BackgroundAgentRunner(loop)
        # Default queue (500) — no backpressure.
        runner.start("test")

        first_event = runner.get_event(timeout=5.0)
        assert first_event is not None
        assert first_event.type == TurnEventType.TEXT_DELTA
        assert first_event.text == "chunk1"

        second_event = runner.get_event(timeout=5.0)
        assert second_event is not None
        assert second_event.type == TurnEventType.TEXT_DELTA
        assert second_event.text == "chunk2"

        third_event = runner.get_event(timeout=5.0)
        assert third_event is not None
        assert third_event.type == TurnEventType.TEXT_DONE

        sentinel = runner.get_event(timeout=5.0)
        assert sentinel is None

    def test_control_events_never_dropped_under_backpressure(self):
        """When the queue is full and a control event (TURN_END)
        arrives, it must not be lost — the _safe_put helper retries
        with timeout instead of blocking forever."""
        import queue as queue_mod

        provider = MockProvider(responses=[_text_response("x")])
        config = RikuganConfig()
        config.auto_context = False
        session = SessionState()
        loop = AgentLoop(provider, ToolRegistry(), config, session)

        events = [
            TurnEvent.text_delta("hello"),
            TurnEvent.turn_end(1),
        ]
        loop.run = lambda user_message: iter(events)  # type: ignore[assignment]

        runner = BackgroundAgentRunner(loop)
        runner.event_queue = queue_mod.Queue(maxsize=1)
        runner.start("test")

        collected = []
        while True:
            event = runner.get_event(timeout=5.0)
            if event is None:
                break
            collected.append(event)

        types = [e.type for e in collected]
        assert TurnEventType.TEXT_DELTA in types
        assert TurnEventType.TURN_END in types


if __name__ == "__main__":
    unittest.main()
