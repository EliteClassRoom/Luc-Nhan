"""Regression tests for Rikugan's token-usage normalization and the
agent-loop accumulation path that previously crashed with
``TypeError: unsupported operand type(s) for +: 'NoneType' and 'int'``.

These tests are pure-Python: they do not require Qt or IDA Pro.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from rikugan.core.types import (
    Message,
    Role,
    TokenUsage,
    coerce_token_count,
)
from rikugan.providers.anthropic_provider import AnthropicProvider
from rikugan.providers.openai_provider import OpenAIProvider


class TestCoerceTokenCount(unittest.TestCase):
    """``coerce_token_count`` must turn any value into a non-negative int."""

    def test_none_becomes_zero(self):
        self.assertEqual(coerce_token_count(None), 0)

    def test_int_passes_through(self):
        self.assertEqual(coerce_token_count(42), 42)
        self.assertEqual(coerce_token_count(0), 0)

    def test_negative_clamps_to_zero(self):
        self.assertEqual(coerce_token_count(-5), 0)

    def test_string_of_digits(self):
        self.assertEqual(coerce_token_count("123"), 123)

    def test_unparseable_string_becomes_zero(self):
        self.assertEqual(coerce_token_count("abc"), 0)

    def test_float_truncates(self):
        self.assertEqual(coerce_token_count(3.7), 3)

    def test_garbage_object_becomes_zero(self):
        self.assertEqual(coerce_token_count(object()), 0)


class TestTokenUsageNormalization(unittest.TestCase):
    """``TokenUsage.__post_init__`` normalizes every field."""

    def test_all_none_becomes_all_zero(self):
        u = TokenUsage(
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            cache_read_tokens=None,
            cache_creation_tokens=None,
        )
        for field in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
        ):
            self.assertEqual(getattr(u, field), 0, field)
            self.assertIsInstance(getattr(u, field), int, field)

    def test_prompt_plus_completion_derives_total(self):
        u = TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=None)
        self.assertEqual(u.total_tokens, 15)

    def test_explicit_total_is_preserved(self):
        u = TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=20)
        self.assertEqual(u.total_tokens, 20)

    def test_negative_inputs_clamps(self):
        u = TokenUsage(prompt_tokens=-1, completion_tokens=-2, total_tokens=-3)
        self.assertEqual(u.prompt_tokens, 0)
        self.assertEqual(u.completion_tokens, 0)
        # total_tokens is 0 after coercion; we don't synthesize from
        # negatives, so it stays 0.
        self.assertEqual(u.total_tokens, 0)

    def test_context_tokens_sums_caches(self):
        u = TokenUsage(
            prompt_tokens=10,
            completion_tokens=5,
            cache_read_tokens=3,
            cache_creation_tokens=2,
        )
        self.assertEqual(u.context_tokens, 15)

    def test_context_tokens_handles_none_fields(self):
        # Field defaults are 0, but the dataclass accepts ints only.
        # ``context_tokens`` should never see None after __post_init__,
        # but verify it does not crash if called on a partially-built object.
        u = TokenUsage(prompt_tokens=0, cache_read_tokens=0, cache_creation_tokens=0)
        self.assertEqual(u.context_tokens, 0)


class TestMessageFromDictNulls(unittest.TestCase):
    """``Message.from_dict`` must accept JSON ``null`` token fields."""

    def test_token_usage_dict_with_all_nulls(self):
        md = {
            "role": "assistant",
            "content": "Hello",
            "token_usage": {
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "cache_read_tokens": None,
                "cache_creation_tokens": None,
            },
        }
        msg = Message.from_dict(md)
        self.assertIsNotNone(msg.token_usage)
        for field in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
        ):
            self.assertEqual(getattr(msg.token_usage, field), 0, field)
            self.assertIsInstance(getattr(msg.token_usage, field), int, field)

    def test_token_usage_dict_with_only_prompt_set(self):
        md = {
            "role": "assistant",
            "content": "Hi",
            "token_usage": {
                "prompt_tokens": 42,
                "completion_tokens": None,
                "total_tokens": None,
            },
        }
        msg = Message.from_dict(md)
        self.assertEqual(msg.token_usage.prompt_tokens, 42)
        self.assertEqual(msg.token_usage.completion_tokens, 0)
        # total_tokens derived from prompt + completion
        self.assertEqual(msg.token_usage.total_tokens, 42)

    def test_token_usage_is_null_at_top_level(self):
        md = {"role": "user", "content": "ping", "token_usage": None}
        msg = Message.from_dict(md)
        # The "token_usage" key being present but None should produce a
        # zeroed TokenUsage (the dict branch), not leave token_usage=None.
        # If the key is absent entirely, token_usage remains None.
        self.assertIsNotNone(msg.token_usage)
        self.assertEqual(msg.token_usage.prompt_tokens, 0)

    def test_token_usage_absent_leaves_none(self):
        md = {"role": "user", "content": "ping"}
        msg = Message.from_dict(md)
        # No "token_usage" key — message has no usage info.
        self.assertIsNone(msg.token_usage)

    def test_round_trip_json(self):
        """A message that round-trips through JSON preserves int token counts."""
        original = Message(
            role=Role.ASSISTANT,
            content="ok",
            token_usage=TokenUsage(prompt_tokens=10, completion_tokens=4),
        )
        j = json.dumps(original.to_dict())
        restored = Message.from_dict(json.loads(j))
        self.assertEqual(restored.token_usage.prompt_tokens, 10)
        self.assertEqual(restored.token_usage.completion_tokens, 4)
        self.assertEqual(restored.token_usage.total_tokens, 14)


class TestAccumulateChunkUsage(unittest.TestCase):
    """Exercise the agent-loop accumulator against the original crash."""

    def _accumulate(self, last, chunk):
        from rikugan.agent.loop import AgentLoop

        # _accumulate_chunk_usage is a method on AgentLoop. We invoke it
        # without constructing a full instance by binding ``self`` to a
        # throwaway SimpleNamespace.
        loop_self = SimpleNamespace()
        return AgentLoop._accumulate_chunk_usage(loop_self, last, chunk)

    def test_original_crash_reproduction_does_not_raise(self):
        """The exact crash: first chunk has None prompt_tokens, second
        chunk adds a completion. The accumulator must not raise."""
        # First chunk — Anthropic-style message_start with None prompt_tokens.
        last = self._accumulate(
            None,
            TokenUsage(prompt_tokens=None, completion_tokens=0, total_tokens=None),
        )
        # All fields should be ints now.
        self.assertEqual(last.prompt_tokens, 0)
        self.assertEqual(last.total_tokens, 0)
        # Second chunk — completion delta. This used to crash with
        # ``TypeError: unsupported operand type(s) for +: 'NoneType' and 'int'``.
        merged = self._accumulate(
            last,
            TokenUsage(prompt_tokens=0, completion_tokens=12, total_tokens=12),
        )
        self.assertEqual(merged.prompt_tokens, 0)
        self.assertEqual(merged.completion_tokens, 12)
        self.assertIsInstance(merged.total_tokens, int)

    def test_anthropic_prompt_plus_completion_chunks(self):
        # message_start: input_tokens=100
        u = self._accumulate(
            None,
            TokenUsage(prompt_tokens=100, completion_tokens=0, total_tokens=100),
        )
        self.assertEqual(u.prompt_tokens, 100)
        self.assertEqual(u.total_tokens, 100)
        # message_delta: output_tokens=42 (Anthropic sends prompt=0 here)
        u2 = self._accumulate(
            u,
            TokenUsage(prompt_tokens=0, completion_tokens=42, total_tokens=None),
        )
        self.assertEqual(u2.prompt_tokens, 100)
        self.assertEqual(u2.completion_tokens, 42)
        self.assertEqual(u2.total_tokens, 142)

    def test_cache_fields_accumulate(self):
        u = self._accumulate(
            None,
            TokenUsage(
                prompt_tokens=10,
                completion_tokens=0,
                cache_read_tokens=3,
                cache_creation_tokens=1,
            ),
        )
        u2 = self._accumulate(
            u,
            TokenUsage(
                prompt_tokens=0,
                completion_tokens=2,
                cache_read_tokens=4,
                cache_creation_tokens=0,
            ),
        )
        self.assertEqual(u2.cache_read_tokens, 7)
        self.assertEqual(u2.cache_creation_tokens, 1)

    def test_chunk_total_larger_than_derived_preserved(self):
        # Some providers report a total that includes overhead, larger
        # than prompt+completion.
        u = self._accumulate(
            None,
            TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=20),
        )
        self.assertEqual(u.total_tokens, 20)

    def test_subsequent_chunk_with_only_prompt_adds(self):
        u = self._accumulate(
            None,
            TokenUsage(prompt_tokens=10, completion_tokens=0, total_tokens=10),
        )
        u2 = self._accumulate(
            u,
            TokenUsage(prompt_tokens=5, completion_tokens=0, total_tokens=5),
        )
        self.assertEqual(u2.prompt_tokens, 15)
        self.assertEqual(u2.completion_tokens, 0)
        self.assertEqual(u2.total_tokens, 15)


class TestFinalizeStreamUsage(unittest.TestCase):
    """The finalize path must coerce nullable fields and patch estimates."""

    def _finalize(self, last_usage, estimated_usage, estimated_prompt_tokens):
        from rikugan.agent.loop import AgentLoop

        return AgentLoop._finalize_stream_usage(SimpleNamespace(), last_usage, estimated_usage, estimated_prompt_tokens)

    def test_no_last_usage_returns_estimated(self):
        est = TokenUsage(prompt_tokens=200, total_tokens=200)
        result, needs_update = self._finalize(None, est, 200)
        self.assertIs(result, est)
        self.assertFalse(needs_update)

    def test_patches_prompt_tokens_from_estimate(self):
        # Provider reported only completion tokens (very common when
        # streaming usage only contains the delta).  The previous
        # ``total_tokens`` value of 42 reflects prompt=None, completion=42.
        # After we patch in the prompt estimate (200), the total should
        # be re-derived from the new prompt + completion.
        last = TokenUsage(prompt_tokens=None, completion_tokens=42, total_tokens=42)
        result, needs_update = self._finalize(last, None, 200)
        self.assertTrue(needs_update)
        self.assertEqual(result.prompt_tokens, 200)
        self.assertEqual(result.completion_tokens, 42)
        # The new total must at least cover prompt + completion.
        self.assertGreaterEqual(result.total_tokens, 200 + 42)

    def test_no_patch_when_prompt_already_positive(self):
        last = TokenUsage(prompt_tokens=50, completion_tokens=42, total_tokens=92)
        result, needs_update = self._finalize(last, None, 9999)
        self.assertFalse(needs_update)
        self.assertIs(result, last)

    def test_does_not_raise_with_all_null_last_usage(self):
        last = TokenUsage(prompt_tokens=None, completion_tokens=None, total_tokens=None)
        # estimated_prompt_tokens=0 => no patch path; should still return last safely.
        result, _ = self._finalize(last, None, 0)
        # The last_usage object's prompt_tokens is 0 after __post_init__,
        # so no patch is applied and we get last back as-is.
        self.assertIsNotNone(result)
        self.assertEqual(result.prompt_tokens, 0)


class TestOpenAINormalizeResponse(unittest.TestCase):
    """OpenAI response normalization must not crash when usage is partial."""

    def test_normalize_with_none_token_fields(self):
        # The provider's _normalize_response is called on a non-streaming
        # response. Construct a minimal mock with usage fields set to None.
        fake_usage = SimpleNamespace(prompt_tokens=None, completion_tokens=5, total_tokens=None)
        fake_msg = SimpleNamespace(content="hi", reasoning_content=None, tool_calls=None)
        fake_response = SimpleNamespace(choices=[SimpleNamespace(message=fake_msg)], usage=fake_usage)
        # Use the provider's private method via an unbound call.
        provider = OpenAIProvider(api_key="x", model="gpt-4o")
        try:
            msg = provider._normalize_response(fake_response)
        except Exception as e:
            self.fail(f"_normalize_response raised: {e}")
        self.assertEqual(msg.role, Role.ASSISTANT)
        self.assertEqual(msg.token_usage.completion_tokens, 5)
        # prompt_tokens and total_tokens must be ints, never None.
        self.assertEqual(msg.token_usage.prompt_tokens, 0)
        self.assertIsInstance(msg.token_usage.total_tokens, int)

    def test_normalize_with_no_usage_object(self):
        fake_msg = SimpleNamespace(content="hi", reasoning_content=None, tool_calls=None)
        fake_response = SimpleNamespace(choices=[SimpleNamespace(message=fake_msg)], usage=None)
        provider = OpenAIProvider(api_key="x", model="gpt-4o")
        msg = provider._normalize_response(fake_response)
        self.assertIsNotNone(msg.token_usage)
        self.assertEqual(msg.token_usage.prompt_tokens, 0)


class TestOpenAIStreamingFinalUsageChunk(unittest.TestCase):
    """The OpenAI streaming code must yield a usage chunk when the
    final SSE message has ``choices=[]`` and a populated ``usage``."""

    def test_final_usage_only_chunk_is_yielded(self):
        provider = OpenAIProvider(api_key="x", model="gpt-4o")

        # Mock OpenAI streaming response. The first chunk is a normal
        # content delta. The second chunk is the final usage-only
        # chunk (choices=[], usage=...).
        content_chunk = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="hello", reasoning_content=None, tool_calls=None),
                    finish_reason=None,
                )
            ],
            usage=None,
        )
        usage_chunk = SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=7, total_tokens=19),
        )

        def fake_create(**_kwargs):
            return iter([content_chunk, usage_chunk])

        fake_client = MagicMock()
        fake_client.chat.completions.create = fake_create

        kwargs = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16,
            "temperature": 0.0,
        }
        chunks = list(provider._stream_chunks(fake_client, kwargs))

        # We should have a text chunk and a usage chunk from the
        # choices=[] / usage=... final SSE message.
        usage_chunks = [c for c in chunks if c.usage is not None]
        self.assertTrue(
            len(usage_chunks) >= 1,
            f"Expected at least one usage chunk from final usage-only SSE; got {[c.usage for c in chunks]}",
        )
        # Token counts must be non-negative ints, never None.
        for c in usage_chunks:
            self.assertIsInstance(c.usage.prompt_tokens, int)
            self.assertIsInstance(c.usage.completion_tokens, int)
            self.assertIsInstance(c.usage.total_tokens, int)
        # Find the final usage-only chunk and verify its token counts.
        final_usage = usage_chunks[-1].usage
        self.assertEqual(final_usage.prompt_tokens, 12)
        self.assertEqual(final_usage.completion_tokens, 7)
        self.assertEqual(final_usage.total_tokens, 19)


class TestAnthropicNormalizeResponse(unittest.TestCase):
    """Anthropic response normalization must not raise on None usage."""

    def test_normalize_with_none_usage_object(self):
        # Anthropic SDK normally guarantees ``response.usage`` but
        # custom backends / mocks may not.
        fake_response = SimpleNamespace(
            content=[],
            usage=None,
        )
        provider = AnthropicProvider(api_key="sk-test", model="claude-sonnet-4-6")
        try:
            msg = provider._normalize_response(fake_response)
        except Exception as e:
            self.fail(f"_normalize_response raised: {e}")
        self.assertIsNotNone(msg.token_usage)
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            self.assertEqual(getattr(msg.token_usage, field), 0)
            self.assertIsInstance(getattr(msg.token_usage, field), int)

    def test_normalize_with_none_token_fields(self):
        # Construct a usage object whose token fields are None.
        fake_block = SimpleNamespace(type="text", text="ok")
        fake_response = SimpleNamespace(
            content=[fake_block],
            usage=SimpleNamespace(
                input_tokens=None,
                output_tokens=5,
                cache_read_input_tokens=None,
                cache_creation_input_tokens=None,
            ),
        )
        provider = AnthropicProvider(api_key="sk-test", model="claude-sonnet-4-6")
        msg = provider._normalize_response(fake_response)
        self.assertEqual(msg.token_usage.completion_tokens, 5)
        self.assertEqual(msg.token_usage.prompt_tokens, 0)
        # Anthropic total is computed from prompt + completion.
        self.assertEqual(msg.token_usage.total_tokens, 5)


if __name__ == "__main__":
    unittest.main()
