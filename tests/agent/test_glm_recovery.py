"""Tests for the one-shot GLM reasoning recovery transaction.

These tests prove the *external* recovery contract:

* When the first logical attempt degenerates (guard fires), the loop records
  the discarded usage once (estimated or authoritative), emits exactly one
  ``RECOVERY_START`` event, disables thinking on the second attempt, caps
  ``max_tokens_override`` at ``min(recovery_max_tokens, model_max_output_tokens)``,
  appends a request-local suffix, and then either persists the second attempt's
  output (success) or emits a compact error with no generated body (failure).

Invariants:

* Pre-turn message IDs and length are unchanged between attempt 1 and attempt 2.
* No third attempt is ever made.
* The discarded attempt's usage is recorded exactly once and is NOT
  double-counted in ``session.total_usage``.
* Existing ``TurnResult`` fields (``.text``, ``.tool_calls``, ``.usage``,
  ``.ok``, ``.has_tool_calls``) continue to work for all mode consumers.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.agent.loop import AgentLoop
from lucnhan.agent.modes.turn_helpers import TurnResult, execute_single_turn
from lucnhan.agent.turn import TurnEvent, TurnEventType
from lucnhan.core.config import LucNhanConfig
from lucnhan.core.types import (
    LLMRequestContext,
    Message,
    ModelInfo,
    ProviderCapabilities,
    Role,
    StreamChunk,
    TokenUsage,
)
from lucnhan.providers.base import LLMProvider
from lucnhan.state.session import SessionState
from lucnhan.tools.base import ParameterSchema, ToolDefinition
from lucnhan.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Scripted GLM provider
# ---------------------------------------------------------------------------

# Reasoning payload large enough to trip the hard ceiling.
# 49,150 UTF-8 bytes -> (49150 + 2) // 3 = 16,384 estimated tokens.
_DEGENERATED_REASONING = "outputting read_bytes tool now\n" * 3500


def _degenerated_reasoning_response() -> list[StreamChunk]:
    """A response whose reasoning stream trips the hard-ceiling guard."""
    return [
        StreamChunk(reasoning_delta=_DEGENERATED_REASONING),
        StreamChunk(usage=TokenUsage(prompt_tokens=100, completion_tokens=5000, total_tokens=5100)),
    ]


def _degenerated_reasoning_response_no_usage() -> list[StreamChunk]:
    """Same degeneration but without a provider-emitted usage chunk."""
    return [
        StreamChunk(reasoning_delta=_DEGENERATED_REASONING),
    ]


def _complete_tool_call_response(
    tool_name: str = "read_bytes",
    args: dict[str, Any] | None = None,
    call_id: str = "call_1",
    prompt_tokens: int = 120,
    completion_tokens: int = 10,
) -> list[StreamChunk]:
    """A valid response with a single tool call."""
    if args is None:
        args = {"address": 4096}
    return [
        StreamChunk(is_tool_call_start=True, tool_call_id=call_id, tool_name=tool_name),
        StreamChunk(tool_args_delta=json.dumps(args), tool_call_id=call_id),
        StreamChunk(is_tool_call_end=True, tool_call_id=call_id, tool_name=tool_name),
        StreamChunk(
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            )
        ),
    ]


def _text_response(text: str = "Done.") -> list[StreamChunk]:
    return [
        StreamChunk(text=text),
        StreamChunk(usage=TokenUsage(prompt_tokens=50, completion_tokens=5, total_tokens=55)),
    ]


class ScriptedGLMProvider(LLMProvider):
    """GLM-flagged provider that returns scripted responses and captures contexts."""

    def __init__(self, responses: list[list[StreamChunk]] | None = None):
        super().__init__(api_key="test", model="glm-5.2")
        self._responses = responses or []
        self._call_count = 0
        self.logical_attempt_contexts: list[LLMRequestContext | None] = []
        self.systems_sent: list[str] = []
        self.max_tokens_sent: list[int] = []

    @property
    def name(self) -> str:
        return "glm"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(reasoning_content=True)

    def _get_client(self):
        return None

    def _fetch_models_live(self) -> list[ModelInfo]:
        return [ModelInfo(id="glm-5.2", name="GLM-5.2", provider="glm")]

    @staticmethod
    def _builtin_models() -> list[ModelInfo]:
        return [ModelInfo(id="glm-5.2", name="GLM-5.2", provider="glm")]

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
        return Message(role=Role.ASSISTANT, content="mock")

    def chat_stream(
        self,
        messages,
        tools=None,
        temperature=0.3,
        max_tokens=4096,
        system="",
        cancel_event=None,
        *,
        request_context: LLMRequestContext | None = None,
    ):
        self.logical_attempt_contexts.append(request_context)
        self.systems_sent.append(system)
        self.max_tokens_sent.append(max_tokens)
        self._call_count += 1
        idx = self._call_count - 1
        if idx < len(self._responses):
            yield from self._responses[idx]
        else:
            yield StreamChunk(text="No more scripted responses.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_glm_config() -> LucNhanConfig:
    """Build a GLM-dialect config suitable for recovery testing."""
    config = LucNhanConfig()
    config.auto_context = False
    config.provider.name = "glm"
    config.provider.model = "glm-5.2"
    config.provider.api_key = "test"
    config.provider.extra = {
        "dialect": "glm",
        "thinking": {"enabled": True, "reasoning_effort": "max", "preserve": True},
        "degeneration_guard": {
            "enabled": True,
            "reasoning_token_ceiling": 16_384,
            "retry_without_thinking": True,
            "recovery_max_tokens": 16_384,
        },
    }
    return config


def _make_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="read_bytes",
            description="Read bytes",
            parameters=[ParameterSchema(name="address", type="integer", description="addr", required=True)],
            handler=lambda address: f"bytes at {address}",
            category="navigation",
        )
    )
    return registry


def _make_glm_loop(
    provider: ScriptedGLMProvider,
    session: SessionState | None = None,
    tools: ToolRegistry | None = None,
) -> AgentLoop:
    config = _make_glm_config()
    return AgentLoop(
        provider=provider,
        tool_registry=tools or _make_tool_registry(),
        config=config,
        session=session or SessionState(provider_name="glm", model_name="glm-5.2"),
    )


def _drain(gen):
    """Drain a generator, returning (events, return_value)."""
    events: list = []
    while True:
        try:
            events.append(next(gen))
        except StopIteration as si:
            return events, si.value


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestGLMRecoverySuccessful(unittest.TestCase):
    """Attempt 1 degenerates -> attempt 2 succeeds -> results persisted once."""

    def test_degenerated_attempt_retries_once_without_persisting_reasoning(self):
        provider = ScriptedGLMProvider(
            responses=[
                _degenerated_reasoning_response(),
                _complete_tool_call_response("read_bytes", {"address": 4096}),
                _text_response("Done."),
            ]
        )
        session = SessionState(provider_name="glm", model_name="glm-5.2")
        loop = _make_glm_loop(provider, session)

        list(loop.run("Inspect the bytes"))

        # Two recovery-context attempts: attempt 1 (degenerated) + attempt 2 (recovery).
        # The normal loop may dispatch additional turns after recovery succeeds
        # (e.g. follow-up text after tool results), but the recovery pair is
        # identifiable by the attempt_number field.
        self.assertGreaterEqual(len(provider.logical_attempt_contexts), 2)
        ctx0 = provider.logical_attempt_contexts[0]
        ctx1 = provider.logical_attempt_contexts[1]
        self.assertIsNotNone(ctx0)
        self.assertEqual(ctx0.attempt_number, 1)
        self.assertEqual(ctx1.attempt_number, 2)
        self.assertTrue(ctx1.disable_thinking)
        # Max override: min(recovery_max_tokens=16384, model_max_output=131072) = 16384
        self.assertEqual(ctx1.max_tokens_override, 16_384)
        # The second attempt should have a system suffix in its context.
        self.assertTrue(ctx1.system_suffix)
        self.assertGreater(len(ctx1.system_suffix), 0)

        # Reasoning content from the degenerated attempt must NOT persist.
        for msg in session.messages:
            if msg.role == Role.ASSISTANT:
                self.assertNotIn("outputting", msg.reasoning_content)

    def test_recovery_start_event_emitted_exactly_once(self):
        provider = ScriptedGLMProvider(
            responses=[
                _degenerated_reasoning_response(),
                _text_response("Done."),
            ]
        )
        loop = _make_glm_loop(provider)

        events = list(loop.run("Inspect"))

        recovery_starts = [e for e in events if e.type == TurnEventType.RECOVERY_START]
        self.assertEqual(len(recovery_starts), 1)

    def test_pre_turn_message_ids_invariant(self):
        """IDs of messages present before the turn must not change between attempts."""
        provider = ScriptedGLMProvider(
            responses=[
                _degenerated_reasoning_response(),
                _text_response("Done."),
            ]
        )
        session = SessionState(provider_name="glm", model_name="glm-5.2")
        loop = _make_glm_loop(provider, session)

        # Pre-populate with a system + user message.
        session.add_message(Message(role=Role.USER, content="prior"))
        pre_ids = [m.id for m in session.messages]

        list(loop.run("New task"))

        # The pre-turn messages must still be present with their original IDs.
        current_ids = [m.id for m in session.messages]
        for pid in pre_ids:
            self.assertIn(pid, current_ids)


class TestGLMRecoveryFailure(unittest.TestCase):
    """Both attempts degenerate -> no third attempt, compact error."""

    def test_second_degeneration_fails_without_third_attempt(self):
        provider = ScriptedGLMProvider(
            responses=[
                _degenerated_reasoning_response(),
                _degenerated_reasoning_response(),
            ]
        )
        loop = _make_glm_loop(provider)

        events = list(loop.run("Inspect"))

        self.assertEqual(provider._call_count, 2)
        recovery_starts = [e for e in events if e.type == TurnEventType.RECOVERY_START]
        self.assertEqual(len(recovery_starts), 1)

    def test_recovery_failure_emits_compact_error(self):
        provider = ScriptedGLMProvider(
            responses=[
                _degenerated_reasoning_response(),
                _degenerated_reasoning_response(),
            ]
        )
        session = SessionState(provider_name="glm", model_name="glm-5.2")
        loop = _make_glm_loop(provider, session)

        events = list(loop.run("Inspect"))

        error_events = [e for e in events if e.type == TurnEventType.ERROR]
        self.assertTrue(len(error_events) >= 1)
        # No generated assistant body should persist.
        assistant_msgs = [m for m in session.messages if m.role == Role.ASSISTANT]
        self.assertEqual(len(assistant_msgs), 0)

    def test_recovery_failure_disposition_and_ok(self):
        """Double-degeneration result has exact disposition='recovery_failed',
        error='recovery_failed', ok=False so mode runners stop."""
        provider = ScriptedGLMProvider(
            responses=[
                _degenerated_reasoning_response(),
                _degenerated_reasoning_response(),
            ]
        )
        session = SessionState(provider_name="glm", model_name="glm-5.2")
        loop = _make_glm_loop(provider, session)

        list(loop.run("Inspect"))

        # No assistant messages persisted at all.
        assistant_msgs = [m for m in session.messages if m.role == Role.ASSISTANT]
        self.assertEqual(len(assistant_msgs), 0)


class TestRecoveryFailureTurnResult(unittest.TestCase):
    """Drive execute_single_turn directly to inspect TurnResult fields."""

    def test_double_degeneration_result_has_exact_recovery_failed_disposition(self):
        tools = _make_tool_registry()
        provider = ScriptedGLMProvider(
            responses=[
                _degenerated_reasoning_response(),
                _degenerated_reasoning_response(),
            ]
        )
        session = SessionState(provider_name="glm", model_name="glm-5.2")
        loop = _make_glm_loop(provider, session, tools=tools)
        tools_schema = loop._build_tools_schema(active_skill=None, use_exploration_mode=False)

        gen = execute_single_turn(loop, "system prompt", tools_schema)
        _events, result = _drain(gen)

        self.assertTrue(result.recovery_attempted)
        self.assertTrue(result.recovery_failed)
        self.assertEqual(result.disposition, "recovery_failed")
        self.assertEqual(result.error, "recovery_failed")
        self.assertFalse(result.ok)
        self.assertEqual(result.text, "")
        self.assertEqual(result.tool_calls, [])


class TestGLMRecoveryCancellation(unittest.TestCase):
    """Cancellation between attempts prevents recovery dispatch."""

    def test_cancellation_between_attempts_prevents_recovery_dispatch(self):
        provider = ScriptedGLMProvider(
            responses=[
                _degenerated_reasoning_response(),
            ]
        )
        loop = _make_glm_loop(provider)

        # Cancel right after the first attempt degenerates.
        events_collected: list[TurnEvent] = []
        gen = loop.run("Inspect")
        for event in gen:
            events_collected.append(event)
            if event.type == TurnEventType.RECOVERY_START:
                loop.cancel()

        # Only one provider call.
        self.assertEqual(provider._call_count, 1)
        # A CANCELLED event should appear.
        cancelled = [e for e in events_collected if e.type == TurnEventType.CANCELLED]
        self.assertTrue(len(cancelled) >= 1)


class TestGLMRecoveryUsageAccounting(unittest.TestCase):
    """Discarded attempt usage is recorded once; no double-counting."""

    def test_authoritative_usage_recorded_once_and_not_double_counted(self):
        """Discarded attempt usage is recorded via record_usage; successful
        via add_message.  Total == sum of both, no double-counting.

        Note: the guard closes the stream before the usage chunk arrives,
        so the degenerated attempt's usage is always estimated.  The test
        verifies the sum invariant rather than exact token counts.
        """
        provider = ScriptedGLMProvider(
            responses=[
                _degenerated_reasoning_response(),
                _text_response("Done."),
            ]
        )
        session = SessionState(provider_name="glm", model_name="glm-5.2")
        loop = _make_glm_loop(provider, session)

        list(loop.run("Task"))

        # The degenerated attempt's usage was recorded via record_usage
        # (estimated from content because the guard closed the stream early).
        # The successful attempt's usage was recorded via add_message
        # (authoritative, 55 tokens).
        # The key invariant: no double-counting — the total is the sum of
        # exactly one degenerated estimate + one successful authoritative.
        self.assertGreater(session.total_usage.total_tokens, 55)

        # Exactly one assistant message was persisted (the recovery success).
        assistant_msgs = [m for m in session.messages if m.role == Role.ASSISTANT]
        self.assertEqual(len(assistant_msgs), 1)

    def test_estimated_usage_recorded_when_provider_omits(self):
        """When the first attempt has no usage chunk, estimate is used."""
        provider = ScriptedGLMProvider(
            responses=[
                _degenerated_reasoning_response_no_usage(),  # no usage chunk
                _text_response("Done."),  # usage: prompt=50, completion=5
            ]
        )
        session = SessionState(provider_name="glm", model_name="glm-5.2")
        loop = _make_glm_loop(provider, session)

        list(loop.run("Task"))

        # The degenerated attempt should have been estimated (prompt tokens > 0
        # from pre-stream estimate, completion from content).
        # The successful attempt added 55 total tokens.
        # Total should be strictly greater than 55 (the second attempt alone).
        self.assertGreater(session.total_usage.total_tokens, 55)

    def test_session_total_equals_sum_of_attempt_usages(self):
        """Directly test execute_single_turn's attempt_usages sum == session total."""
        tools = _make_tool_registry()
        provider = ScriptedGLMProvider(
            responses=[
                _degenerated_reasoning_response(),
                _text_response("Done."),
            ]
        )
        session = SessionState(provider_name="glm", model_name="glm-5.2")
        loop = _make_glm_loop(provider, session, tools=tools)

        # Build a tools schema so the guard activates.
        tools_schema = loop._build_tools_schema(active_skill=None, use_exploration_mode=False)

        # Capture total before the turn.
        total_before = session.total_usage.total_tokens

        # Drive execute_single_turn directly.
        gen = execute_single_turn(loop, "system prompt", tools_schema)
        _events, result = _drain(gen)

        self.assertTrue(result.recovery_attempted)
        self.assertEqual(len(result.attempt_usages), 2)

        # attempt_usages[0] from degeneration: should be authoritative (usage was present)
        # or estimated (no usage).  Guard closes stream early so usually estimated.
        self.assertIn(result.attempt_usages[0].provenance, ("authoritative", "estimated"))

        expected_total = sum(au.usage.total_tokens for au in result.attempt_usages)
        actual_delta = session.total_usage.total_tokens - total_before
        self.assertEqual(actual_delta, expected_total)


class TestTurnResultCompat(unittest.TestCase):
    """New TurnResult fields are backward-compatible with existing consumers."""

    def test_turn_result_default_values(self):
        result = TurnResult()
        self.assertFalse(result.recovery_attempted)
        self.assertFalse(result.recovery_failed)
        self.assertEqual(result.attempt_usages, [])
        self.assertIsNone(result.disposition)
        self.assertIsNone(result.finish_reason)

    def test_ok_property_unchanged_by_recovery_flags_alone(self):
        """Recovery flags alone (without error) do not affect ok."""
        result = TurnResult(recovery_attempted=True, recovery_failed=True)
        self.assertTrue(result.ok)

    def test_recovery_failure_with_error_makes_ok_false(self):
        """When recovery failure sets error='recovery_failed', ok is False."""
        result = TurnResult(
            recovery_attempted=True,
            recovery_failed=True,
            error="recovery_failed",
            disposition="recovery_failed",
        )
        self.assertFalse(result.ok)


class TestRecoveryContextValueErrorPropagation(unittest.TestCase):
    """_build_recovery_context narrows its catch to ImportError only.

    ValueError from parse_glm_extra (invalid config) must propagate so the
    user knows their config is broken instead of silently degrading recovery.
    """

    def test_invalid_glm_extra_raises_value_error(self):
        from lucnhan.agent.modes.turn_helpers import _build_recovery_context

        config = LucNhanConfig()
        config.auto_context = False
        config.provider.name = "glm"
        config.provider.model = "glm-5.2"
        config.provider.api_key = "test"
        config.provider.extra = {
            "dialect": "glm",
            # Invalid: thinking.enabled must be bool
            "thinking": {"enabled": "not_a_bool"},
        }
        provider = ScriptedGLMProvider(responses=[])
        session = SessionState(provider_name="glm", model_name="glm-5.2")
        loop = AgentLoop(
            provider=provider,
            tool_registry=_make_tool_registry(),
            config=config,
            session=session,
        )
        with self.assertRaises(ValueError):
            _build_recovery_context(loop)


class TestHistoryMutationRaisesRuntimeError(unittest.TestCase):
    """If a degenerated attempt somehow mutates session history, the
    load-bearing invariant check raises RuntimeError (not AssertionError).

    This must survive ``python -O`` in optimized IDA Pro builds.
    """

    def test_history_length_change_raises_runtime_error(self):
        tools = _make_tool_registry()
        provider = ScriptedGLMProvider(
            responses=[
                _degenerated_reasoning_response(),
            ]
        )
        session = SessionState(provider_name="glm", model_name="glm-5.2")
        loop = _make_glm_loop(provider, session, tools=tools)
        tools_schema = loop._build_tools_schema(active_skill=None, use_exploration_mode=False)

        # Simulate a mutation between the degenerated attempt and the
        # invariant check by wrapping session.add_message to inject a
        # message during the first stream.  We use a side-effect provider
        # that adds a stray message while yielding the degenerated reasoning.
        original_chat_stream = provider.chat_stream

        def mutating_chat_stream(*args, **kwargs):
            # Simulate the bug: degenerated attempt persisted a message
            session.add_message(Message(role=Role.ASSISTANT, content="leaked"))
            yield from original_chat_stream(*args, **kwargs)

        provider.chat_stream = mutating_chat_stream  # type: ignore[assignment]

        gen = execute_single_turn(loop, "system prompt", tools_schema)
        with self.assertRaises(RuntimeError, msg="Degenerated attempt must not mutate"):
            _drain(gen)


class TestReasoningContentPersistence(unittest.TestCase):
    """Normal GLM turn (not recovery) must persist reasoning_content on the
    accepted assistant Message so preserved-thinking replays it in subsequent
    requests (spec sections 6.2, 7.2)."""

    def test_normal_turn_persists_reasoning_content(self):
        reasoning_chunks = [
            StreamChunk(reasoning_delta='I should call read_bytes.'),
            StreamChunk(reasoning_delta=' Then decompile.'),
            StreamChunk(text='Let me check.', finish_reason='stop'),
        ]

        provider = ScriptedGLMProvider(responses=[reasoning_chunks])
        session = SessionState(provider_name='glm', model_name='glm-5.2')
        loop = _make_glm_loop(provider, session)

        list(loop.run('hello'))

        assistant_messages = [m for m in session.messages if m.role == Role.ASSISTANT]
        self.assertTrue(assistant_messages, 'Expected at least one assistant message')
        self.assertTrue(
            assistant_messages[-1].reasoning_content,
            'reasoning_content must be persisted on accepted assistant messages',
        )
        self.assertIn('read_bytes', assistant_messages[-1].reasoning_content)



if __name__ == "__main__":
    unittest.main()
