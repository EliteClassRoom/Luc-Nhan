"""Tests for provider streaming (chat_stream) paths.

These tests exercise the complex streaming state machines in each provider
using mock stream objects, without requiring the actual SDK packages.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.core.types import (
    LLMRequestContext,
    Message,
    ProviderCapabilities,
    Role,
    StreamChunk,
)
from lucnhan.providers.base import LLMProvider


class TestAnthropicStreaming(unittest.TestCase):
    """Test AnthropicProvider.chat_stream with mock Anthropic stream events."""

    def _make_provider(self):
        from lucnhan.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(api_key="test-key", model="claude-test")
        return p

    def _mock_stream_events(self, events):
        """Create a mock context manager that yields events."""
        stream_mock = MagicMock()
        stream_mock.__iter__ = MagicMock(return_value=iter(events))
        stream_mock.__enter__ = MagicMock(return_value=stream_mock)
        stream_mock.__exit__ = MagicMock(return_value=False)
        return stream_mock

    def test_text_only_stream(self):
        """Stream with text-only content blocks."""
        events = [
            SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="text", text="")),
            SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="Hello ")),
            SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="world")),
            SimpleNamespace(type="content_block_stop"),
            SimpleNamespace(type="message_delta", delta=SimpleNamespace(stop_reason="end_turn")),
        ]

        p = self._make_provider()
        mock_client = MagicMock()
        mock_client.messages.stream.return_value = self._mock_stream_events(events)
        p._client = mock_client

        chunks = list(p.chat_stream([Message(role=Role.USER, content="Hi")]))

        texts = [c.text for c in chunks if c.text]
        self.assertEqual(texts, ["Hello ", "world"])
        finish = [c for c in chunks if c.finish_reason]
        self.assertEqual(len(finish), 1)
        self.assertEqual(finish[0].finish_reason, "end_turn")

    def test_tool_call_stream(self):
        """Stream with a tool_use content block."""
        events = [
            SimpleNamespace(
                type="content_block_start", content_block=SimpleNamespace(type="tool_use", id="tc_1", name="get_info")
            ),
            SimpleNamespace(
                type="content_block_delta", delta=SimpleNamespace(type="input_json_delta", partial_json='{"key":')
            ),
            SimpleNamespace(
                type="content_block_delta", delta=SimpleNamespace(type="input_json_delta", partial_json='"val"}')
            ),
            SimpleNamespace(type="content_block_stop"),
        ]

        p = self._make_provider()
        mock_client = MagicMock()
        mock_client.messages.stream.return_value = self._mock_stream_events(events)
        p._client = mock_client

        chunks = list(p.chat_stream([Message(role=Role.USER, content="Use tool")]))

        starts = [c for c in chunks if c.is_tool_call_start]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].tool_call_id, "tc_1")
        self.assertEqual(starts[0].tool_name, "get_info")

        args_deltas = [c.tool_args_delta for c in chunks if c.tool_args_delta and not c.is_tool_call_end]
        self.assertEqual(args_deltas, ['{"key":', '"val"}'])

        ends = [c for c in chunks if c.is_tool_call_end]
        self.assertEqual(len(ends), 1)

    def test_message_start_usage(self):
        """Stream emits usage from message_start event."""
        events = [
            SimpleNamespace(type="message_start", message=SimpleNamespace(usage=SimpleNamespace(input_tokens=42))),
            SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="text", text="")),
            SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="OK")),
            SimpleNamespace(type="content_block_stop"),
        ]

        p = self._make_provider()
        mock_client = MagicMock()
        mock_client.messages.stream.return_value = self._mock_stream_events(events)
        p._client = mock_client

        chunks = list(p.chat_stream([Message(role=Role.USER, content="Hi")]))
        usage_chunks = [c for c in chunks if c.usage is not None]
        self.assertEqual(len(usage_chunks), 1)
        self.assertEqual(usage_chunks[0].usage.prompt_tokens, 42)


class TestOpenAIStreaming(unittest.TestCase):
    """Test OpenAIProvider.chat_stream with mock OpenAI stream chunks."""

    def _make_provider(self):
        from lucnhan.providers.openai_provider import OpenAIProvider

        return OpenAIProvider(api_key="test-key", model="gpt-test")

    def test_text_only_stream(self):
        """Stream with text-only deltas."""
        stream_chunks = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="Hello ", tool_calls=None),
                        finish_reason=None,
                    )
                ],
                usage=None,
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="world", tool_calls=None),
                        finish_reason=None,
                    )
                ],
                usage=None,
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=None, tool_calls=None),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            ),
        ]

        p = self._make_provider()
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = iter(stream_chunks)
        p._client = mock_client

        chunks = list(p.chat_stream([Message(role=Role.USER, content="Hi")]))

        texts = [c.text for c in chunks if c.text]
        self.assertEqual(texts, ["Hello ", "world"])
        finish = [c for c in chunks if c.finish_reason]
        self.assertEqual(len(finish), 1)
        self.assertEqual(finish[0].finish_reason, "stop")

    def test_tool_call_stream(self):
        """Stream with tool call deltas."""
        stream_chunks = [
            # First chunk: tool call start
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="tc_1",
                                    function=SimpleNamespace(name="get_info", arguments=""),
                                )
                            ],
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
            ),
            # Second chunk: tool args
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id=None,
                                    function=SimpleNamespace(name=None, arguments='{"x": 1}'),
                                )
                            ],
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
            ),
            # Third chunk: finish
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=None, tool_calls=None),
                        finish_reason="tool_calls",
                    )
                ],
                usage=None,
            ),
        ]

        p = self._make_provider()
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = iter(stream_chunks)
        p._client = mock_client

        chunks = list(p.chat_stream([Message(role=Role.USER, content="Use tool")]))

        starts = [c for c in chunks if c.is_tool_call_start]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].tool_call_id, "tc_1")
        self.assertEqual(starts[0].tool_name, "get_info")

        ends = [c for c in chunks if c.is_tool_call_end]
        self.assertEqual(len(ends), 1)

    def test_usage_chunk(self):
        """Stream reports usage in final chunk."""
        stream_chunks = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="OK", tool_calls=None),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=10,
                    completion_tokens=5,
                    total_tokens=15,
                ),
            ),
        ]

        p = self._make_provider()
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = iter(stream_chunks)
        p._client = mock_client

        chunks = list(p.chat_stream([Message(role=Role.USER, content="Hi")]))
        usage_chunks = [c for c in chunks if c.usage is not None]
        self.assertEqual(len(usage_chunks), 1)
        self.assertEqual(usage_chunks[0].usage.total_tokens, 15)


# ---------------------------------------------------------------------------
# Cancellation: verify chat_stream honors a cancel_event by force-closing the
# underlying HTTP stream. Without this, user-clicks-Stop during a long model
# response has no effect until the next SSE chunk arrives (could be minutes).
# ---------------------------------------------------------------------------


class _BlockingAnthropicStream:
    """Fake Anthropic stream that yields one chunk then blocks on iteration.

    Simulates a real SDK stream waiting on HTTP recv() for the next SSE event.
    The ``close()`` method unblocks iteration (real SDKs do this on socket
    close, raising ``httpx.RemoteProtocolError`` inside the consumer).
    """

    def __init__(self) -> None:
        self.close_called = threading.Event()
        self.iter_started = threading.Event()

    def __iter__(self):
        self.iter_started.set()
        yield SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="text", text=""),
        )
        yield SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="hi"),
        )
        # Block until close() is called (or test times out)
        if not self.close_called.wait(timeout=5.0):
            raise RuntimeError("test bug: stream close() never called")
        # Real SDK raises on closed connection
        raise RuntimeError("stream closed by client")

    def close(self) -> None:
        self.close_called.set()

    def __enter__(self) -> _BlockingAnthropicStream:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.close()
        return False


class TestAnthropicCancelDuringStream(unittest.TestCase):
    """User clicks Stop while model is mid-stream. Verify close() is called."""

    def test_cancel_event_closes_stream_promptly(self) -> None:
        from lucnhan.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(api_key="test-key", model="claude-test")
        blocking = _BlockingAnthropicStream()

        mock_client = MagicMock()
        mock_client.messages.stream.return_value = blocking
        p._client = mock_client

        cancel = threading.Event()
        cancel.set()  # simulate Stop already clicked before we start consuming

        start = time.monotonic()
        consumer_exc: list = []

        def consume() -> None:
            try:
                # The provider must accept ``cancel_event`` (new API surface).
                list(
                    p.chat_stream(
                        [Message(role=Role.USER, content="hi")],
                        cancel_event=cancel,
                    )
                )
            except Exception as e:
                consumer_exc.append(e)

        t = threading.Thread(target=consume, daemon=True)
        t.start()
        t.join(timeout=1.0)
        elapsed = time.monotonic() - start

        self.assertFalse(
            t.is_alive(),
            f"consumer thread did not exit within 1s (elapsed={elapsed:.2f}s) "
            f"— cancel_event did not interrupt the streaming read",
        )
        self.assertTrue(
            blocking.close_called.is_set(),
            "stream.close() was never called — watchdog never fired",
        )


class _BlockingOpenAIStream:
    """Fake OpenAI stream: yields one chunk then blocks; close() unblocks."""

    def __init__(self) -> None:
        self.close_called = threading.Event()

    def __iter__(self):
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="hi", tool_calls=None),
                    finish_reason=None,
                )
            ],
            usage=None,
        )
        if not self.close_called.wait(timeout=5.0):
            raise RuntimeError("test bug: stream never closed")
        raise RuntimeError("stream closed by client")

    def close(self) -> None:
        self.close_called.set()


class TestOpenAICancelDuringStream(unittest.TestCase):
    """User clicks Stop while OpenAI model is mid-stream."""

    def test_cancel_event_closes_stream_promptly(self) -> None:
        from lucnhan.providers.openai_provider import OpenAIProvider

        p = OpenAIProvider(api_key="test-key", model="gpt-test")
        blocking = _BlockingOpenAIStream()

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = blocking
        p._client = mock_client

        cancel = threading.Event()
        cancel.set()

        start = time.monotonic()

        def consume() -> None:
            try:
                list(
                    p.chat_stream(
                        [Message(role=Role.USER, content="hi")],
                        cancel_event=cancel,
                    )
                )
            except Exception:
                pass

        t = threading.Thread(target=consume, daemon=True)
        t.start()
        t.join(timeout=1.0)
        elapsed = time.monotonic() - start

        self.assertFalse(
            t.is_alive(),
            f"OpenAI consumer thread did not exit within 1s (elapsed={elapsed:.2f}s)",
        )
        self.assertTrue(
            blocking.close_called.is_set(),
            "OpenAI stream.close() was never called",
        )


# ---------------------------------------------------------------------------
# Request context forwarding (Task 5)
# ---------------------------------------------------------------------------
#
# The base ``chat_stream`` pipeline must thread an optional
# ``LLMRequestContext`` keyword argument through to
# ``_build_request_kwargs`` so providers can adapt payload to attempt-local
# state (e.g. GLM one-shot recovery).  This sentinel test pins the
# contract: passing ``request_context=`` to ``chat_stream`` lands the
# *same* instance on the override, untouched.


class ContextCapturingProvider(LLMProvider):
    """Minimal LLMProvider subclass that captures the request_context it sees.

    The override accepts ``request_context`` as a keyword-only parameter
    but ignores it — the assertion is that the pipeline forwards the
    object all the way through ``chat_stream`` to this hook.
    """

    def __init__(self) -> None:
        super().__init__(api_key="test", model="test")
        self.seen_context: LLMRequestContext | None = None

    @property
    def name(self) -> str:
        return "context-capturing"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def _get_client(self):
        return object()

    def _fetch_models_live(self):
        return []

    @staticmethod
    def _builtin_models():
        return []

    def _format_messages(self, messages):
        return messages

    def _build_request_kwargs(
        self,
        messages,
        tools,
        temperature,
        max_tokens,
        system,
        *,
        request_context=None,
    ):
        self.seen_context = request_context
        return {}

    def _call_api(self, client, kwargs):
        return None

    def _normalize_response(self, raw):
        return Message(role=Role.ASSISTANT)

    def _handle_api_error(self, error):
        raise error

    def _stream_chunks(self, client, kwargs, cancel_event=None):
        if False:
            yield StreamChunk()


class TestRequestContextForwarding(unittest.TestCase):
    """``chat_stream`` must forward ``request_context`` to the override."""

    def test_chat_stream_forwards_request_context(self) -> None:
        provider = ContextCapturingProvider()
        context = LLMRequestContext(attempt_number=2, recovery=True)

        list(provider.chat_stream([], request_context=context))

        # ``chat_stream`` replaces the context with a copy that has
        # ``streaming=True``, so we assert field-by-field equality rather
        # than object identity.
        seen = provider.seen_context
        self.assertIsNotNone(seen, "chat_stream did not forward request_context")
        assert seen is not None  # for type-checker
        self.assertEqual(seen.attempt_number, 2)
        self.assertEqual(seen.recovery, True)
        self.assertTrue(
            seen.streaming,
            "chat_stream must set streaming=True on the context before forwarding it to _build_request_kwargs.",
        )


# ---------------------------------------------------------------------------
# Reviewer fixes: explicit None handling for ``max_tokens_override`` and a
# defined suffix-separator policy.  These tests pin the contract that
# ``_resolve_effective_kwargs`` (and therefore ``chat`` / ``chat_stream``)
# apply:
#
#   1. ``max_tokens_override = None`` (or absent) means "use the caller's
#      ``max_tokens``".
#   2. A positive override replaces the caller's value.
#   3. A 0 or negative override raises ``ValueError`` before kwargs are
#      built.  The plan/global config minimum is 1; 0 cannot mean "produce
#      nothing" and a negative value would obviously never be honoured.
#   4. ``system_suffix`` joins onto ``safe_system`` with exactly ``"\n\n"``
#      when BOTH halves are non-empty; suffix alone or system alone are
#      passed through unchanged.
# ---------------------------------------------------------------------------


class SystemCapturingProvider(LLMProvider):
    """Captures the ``(system, max_tokens)`` pair the pipeline resolves.

    Unlike :class:`ContextCapturingProvider` (which only pins the
    ``request_context`` object identity), this provider records the
    *merged* values the override actually sees.  Tests use this to assert
    the suffix separator policy and max_tokens override behaviour.
    """

    def __init__(self) -> None:
        super().__init__(api_key="test", model="test")
        self.calls: list[dict[str, object]] = []

    @property
    def name(self) -> str:
        return "system-capturing"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def _get_client(self):
        return object()

    def _fetch_models_live(self):
        return []

    @staticmethod
    def _builtin_models():
        return []

    def _format_messages(self, messages):
        return messages

    def _build_request_kwargs(
        self,
        messages,
        tools,
        temperature,
        max_tokens,
        system,
        *,
        request_context=None,
    ):
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "system": system,
                "request_context": request_context,
            }
        )
        return {}

    def _call_api(self, client, kwargs):
        return None

    def _normalize_response(self, raw):
        return Message(role=Role.ASSISTANT)

    def _handle_api_error(self, error):
        raise error

    def _stream_chunks(self, client, kwargs, cancel_event=None):
        if False:
            yield StreamChunk()


class TestResolveEffectiveKwargs(unittest.TestCase):
    """``_resolve_effective_kwargs`` is the single source of truth for
    the system-suffix and max_tokens-override merge.  These tests cover
    every case the policy specifies."""

    def _resolve(self, context, max_tokens=4096, system="safe-system"):
        from lucnhan.providers.base import LLMProvider

        return LLMProvider._resolve_effective_kwargs(context, max_tokens, system)

    def test_both_halves_non_empty_uses_separator(self) -> None:
        """safe_system + suffix must join with exactly ``"\n\n"``."""
        ctx = LLMRequestContext(system_suffix="tail")
        eff_system, eff_max, _ = self._resolve(ctx, system="head")
        self.assertEqual(eff_system, "head\n\ntail")
        self.assertEqual(eff_max, 4096)

    def test_system_alone_drops_separator(self) -> None:
        """Empty suffix is a no-op — safe_system passes through."""
        ctx = LLMRequestContext(system_suffix="")
        eff_system, _, _ = self._resolve(ctx, system="head")
        self.assertEqual(eff_system, "head")

    def test_suffix_alone_drops_separator(self) -> None:
        """Empty safe_system: suffix is passed through verbatim with
        no leading ``"\n\n"``."""
        ctx = LLMRequestContext(system_suffix="only-suffix")
        eff_system, _, _ = self._resolve(ctx, system="")
        self.assertEqual(eff_system, "only-suffix")

    def test_both_empty_yields_empty(self) -> None:
        ctx = LLMRequestContext(system_suffix="")
        eff_system, _, _ = self._resolve(ctx, system="")
        self.assertEqual(eff_system, "")

    def test_no_context_yields_input_passthrough(self) -> None:
        """Without a context, system and max_tokens pass through
        untouched (this is the non-GLM default equivalence case)."""
        eff_system, eff_max, ctx = self._resolve(None, max_tokens=8192, system="safe")
        self.assertEqual(eff_system, "safe")
        self.assertEqual(eff_max, 8192)
        self.assertIsInstance(ctx, LLMRequestContext)
        self.assertEqual(ctx.system_suffix, "")
        self.assertIsNone(ctx.max_tokens_override)

    def test_explicit_zero_suffix_system_alone(self) -> None:
        """``LLMRequestContext(system_suffix="")`` (no other fields set)
        behaves identically to no context for the system half."""
        eff_system, eff_max, _ = self._resolve(LLMRequestContext(), system="x")
        self.assertEqual(eff_system, "x")
        self.assertEqual(eff_max, 4096)


class TestMaxTokensOverride(unittest.TestCase):
    """``LLMRequestContext.max_tokens_override`` semantics:

    * ``None`` (or omitted) → caller's ``max_tokens`` wins.
    * positive int → replaces caller's value.
    * 0 / negative → ``ValueError`` (plan minimum is 1; 0 is not "produce nothing").
    """

    def _resolve(self, context, max_tokens=4096, system=""):
        from lucnhan.providers.base import LLMProvider

        return LLMProvider._resolve_effective_kwargs(context, max_tokens, system)

    def test_none_uses_callers_max_tokens(self) -> None:
        _, eff_max, _ = self._resolve(LLMRequestContext(), max_tokens=2048)
        self.assertEqual(eff_max, 2048)

    def test_positive_override_replaces_callers_value(self) -> None:
        ctx = LLMRequestContext(max_tokens_override=131072)
        _, eff_max, _ = self._resolve(ctx, max_tokens=4096)
        self.assertEqual(eff_max, 131072)

    def test_positive_override_uses_default_when_caller_omits(self) -> None:
        """The default ``max_tokens=4096`` must also be replaceable."""
        ctx = LLMRequestContext(max_tokens_override=8192)
        _, eff_max, _ = self._resolve(ctx)
        self.assertEqual(eff_max, 8192)

    def test_zero_override_raises_value_error(self) -> None:
        ctx = LLMRequestContext(max_tokens_override=0)
        with self.assertRaises(ValueError) as cm:
            self._resolve(ctx, max_tokens=4096)
        msg = str(cm.exception)
        self.assertIn("max_tokens_override", msg)
        self.assertIn("positive", msg.lower())
        self.assertIn("produce nothing", msg.lower())

    def test_negative_override_raises_value_error(self) -> None:
        ctx = LLMRequestContext(max_tokens_override=-1)
        with self.assertRaises(ValueError):
            self._resolve(ctx, max_tokens=4096)

    def test_zero_override_does_not_reach_build_request_kwargs(self) -> None:
        """A bad override must be rejected before ``_build_request_kwargs``
        is invoked — otherwise the error would land inside the SDK's
        request builder."""
        provider = SystemCapturingProvider()
        ctx = LLMRequestContext(max_tokens_override=0)
        with self.assertRaises(ValueError):
            list(provider.chat_stream([], request_context=ctx))
        self.assertEqual(
            provider.calls,
            [],
            "_build_request_kwargs must NOT be invoked when the override is invalid.",
        )


class TestNonGLMDefaultEquivalence(unittest.TestCase):
    """Without a context (or with a context whose defaults are all empty
    / ``None``), the kwargs an override sees MUST be identical to those
    produced by the pre-context code path.  This is the non-GLM
    "default equivalence" assertion.

    The previous Task 5 implementation already pinned the context-payload
    equivalence per-provider; this test pins the *system + max_tokens*
    equivalence at the base pipeline level (independent of any concrete
    provider override)."""

    def test_no_context_system_and_max_tokens_pass_through(self) -> None:
        provider = SystemCapturingProvider()
        list(provider.chat_stream([], system="sys", max_tokens=4096))
        self.assertEqual(len(provider.calls), 1)
        call = provider.calls[0]
        self.assertEqual(call["system"], "sys")
        self.assertEqual(call["max_tokens"], 4096)

    def test_empty_default_context_does_not_change_kwargs(self) -> None:
        """A bare ``LLMRequestContext()`` (everything default) must not
        alter the kwargs the override sees."""
        provider = SystemCapturingProvider()
        baseline = list(provider.chat_stream([], system="sys", max_tokens=4096))
        provider.calls.clear()
        contextual = list(
            provider.chat_stream(
                [],
                system="sys",
                max_tokens=4096,
                request_context=LLMRequestContext(),
            )
        )
        self.assertEqual(baseline, contextual)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0]["system"], "sys")
        self.assertEqual(provider.calls[0]["max_tokens"], 4096)

    def test_recovery_flag_does_not_change_kwargs(self) -> None:
        """``recovery=True`` is metadata, not a payload knob — the kwargs
        must still match the no-context call."""
        provider = SystemCapturingProvider()
        provider.calls.clear()
        list(
            provider.chat_stream(
                [],
                system="sys",
                max_tokens=4096,
                request_context=LLMRequestContext(recovery=True),
            )
        )
        self.assertEqual(provider.calls[0]["system"], "sys")
        self.assertEqual(provider.calls[0]["max_tokens"], 4096)


if __name__ == "__main__":
    unittest.main()
