"""Tests for the MiniMax provider.

Covers:

* Default model and builtin metadata follow current docs.
* Automatic thinking for M3 (and only M3).
* ``cache_control`` stripping on the outgoing request.
* ``_build_request_kwargs`` payload equivalence — ``request_context``
  must be a pure pass-through (Task 5 contract).
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.core.types import LLMRequestContext, Message, Role, StreamChunk  # noqa: E402

# ---------------------------------------------------------------------------
# Default model and builtin metadata
# ---------------------------------------------------------------------------


class TestMiniMaxDefaultsAndMetadata(unittest.TestCase):
    """MiniMax default model and builtin metadata follow current docs."""

    def test_default_model_is_minimax_m3(self) -> None:
        from rikugan.core.config import PROVIDER_DEFAULT_MODELS

        self.assertEqual(PROVIDER_DEFAULT_MODELS["minimax"], "MiniMax-M3")

    def test_minimax_provider_default_model_is_m3(self) -> None:
        from rikugan.providers.minimax_provider import MiniMaxProvider

        provider = MiniMaxProvider(api_key="sk-test")
        self.assertEqual(provider.model, "MiniMax-M3")

    def test_builtin_models_include_m3_with_documented_limits(self) -> None:
        from rikugan.providers.minimax_provider import MiniMaxProvider

        models = MiniMaxProvider._builtin_models()
        ids = [m.id for m in models]
        self.assertIn("MiniMax-M3", ids)
        self.assertIn("MiniMax-M2.7", ids)
        self.assertIn("MiniMax-M2.7-highspeed", ids)
        m3 = next(m for m in models if m.id == "MiniMax-M3")
        self.assertEqual(m3.context_window, 1_000_000)
        self.assertEqual(m3.max_output_tokens, 524_288)
        self.assertTrue(m3.supports_vision)  # M3 multimodal per docs
        for m in models:
            if m.id.startswith("MiniMax-M2"):
                self.assertEqual(m.context_window, 204_800)
                self.assertEqual(m.max_output_tokens, 204_800)
                self.assertFalse(m.supports_vision)  # M2.x text-only per docs

    def test_capabilities_reflect_largest_documented_model(self) -> None:
        from rikugan.providers.minimax_provider import MiniMaxProvider

        caps = MiniMaxProvider(api_key="sk-test").capabilities
        self.assertEqual(caps.max_context_window, 1_000_000)
        self.assertEqual(caps.max_output_tokens, 524_288)
        self.assertTrue(caps.tool_use)
        self.assertTrue(caps.vision)  # largest model (M3) is multimodal


# ---------------------------------------------------------------------------
# Automatic thinking (M3 only)
# ---------------------------------------------------------------------------


class TestMiniMaxAutomaticThinking(unittest.TestCase):
    """``_build_request_kwargs`` must enable automatic thinking for M3
    and not add a manual thinking budget for M2.x."""

    def _kwargs(self, model: str, max_tokens: int = 8192):
        from rikugan.providers.minimax_provider import MiniMaxProvider

        provider = MiniMaxProvider(api_key="sk-test", model=model)
        return provider._build_request_kwargs(
            messages=[],
            tools=None,
            temperature=0.5,
            max_tokens=max_tokens,
            system="",
        )

    def test_m3_includes_adaptive_thinking(self) -> None:
        kwargs = self._kwargs("MiniMax-M3", max_tokens=131072)
        self.assertEqual(kwargs.get("thinking"), {"type": "adaptive"})
        # Caller's max_tokens preserved exactly (no override).
        self.assertEqual(kwargs.get("max_tokens"), 131072)

    def test_m3_thinking_case_insensitive(self) -> None:
        kwargs = self._kwargs("minimax-m3")
        self.assertEqual(kwargs.get("thinking"), {"type": "adaptive"})

    def test_m2_does_not_add_thinking_payload(self) -> None:
        """M2.x models cannot disable thinking; we must not add a
        separate ``budget_tokens`` or other manual thinking field."""
        kwargs = self._kwargs("MiniMax-M2.5", max_tokens=65536)
        self.assertNotIn("thinking", kwargs)
        # No budget_tokens field should leak into the top-level kwargs.
        self.assertNotIn("budget_tokens", kwargs)
        self.assertEqual(kwargs.get("max_tokens"), 65536)

    def test_m27_does_not_add_thinking_payload(self) -> None:
        kwargs = self._kwargs("MiniMax-M2.7", max_tokens=65536)
        self.assertNotIn("thinking", kwargs)

    def test_strips_cache_control_from_request(self) -> None:
        """The MiniMax adapter continues to strip unsupported ``cache_control``."""
        kwargs = self._kwargs("MiniMax-M3")

        # system: empty string passes through; tools: None → not in kwargs.
        # The strip is defensive — assert no ``cache_control`` keys leaked.
        def _walk(obj):
            if isinstance(obj, dict):
                if "cache_control" in obj:
                    yield obj
                for v in obj.values():
                    yield from _walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    yield from _walk(v)

        self.assertEqual(list(_walk(kwargs)), [])


# ---------------------------------------------------------------------------
# Task 5: request-context pass-through equivalence
# ---------------------------------------------------------------------------


class TestMiniMaxRequestContextPayloadEquivalence(unittest.TestCase):
    """Task 5: ``request_context`` must be a pure pass-through for the
    MiniMax provider — its override inherits from AnthropicProvider and
    currently strips ``cache_control`` / enables ``thinking`` for M3."""

    def test_request_context_does_not_change_minimax_payload(self) -> None:
        from rikugan.providers.minimax_provider import MiniMaxProvider

        provider = MiniMaxProvider(api_key="sk-test", model="MiniMax-M3")
        messages = [Message(role=Role.USER, content="hello")]
        baseline = provider._build_request_kwargs(messages, None, 0.3, 4096, "system")
        contextual = provider._build_request_kwargs(
            messages,
            None,
            0.3,
            4096,
            "system",
            request_context=LLMRequestContext(recovery=True),
        )

        self.assertEqual(
            contextual,
            baseline,
            "MiniMax payload differs when request_context is provided — "
            "the context must be a pure pass-through for non-GLM "
            "providers.",
        )


# ---------------------------------------------------------------------------
# Task 5 reviewer fix: MiniMax must also see the same suffix separator
# behaviour the base pipeline documents.  Because MiniMax inherits from
# AnthropicProvider and overrides ``_build_request_kwargs`` only to strip
# ``cache_control`` and inject M3 thinking, the suffix is already merged
# upstream — so for MiniMax the equivalence test above covers it
# implicitly.  This class keeps MiniMax-specific overrides (recovery /
# disable_thinking) out of the suite until a future task wires them.
# ---------------------------------------------------------------------------


class TestMiniMaxInheritsAnthropicStreamingCoercion(unittest.TestCase):
    """``MiniMaxProvider`` inherits ``AnthropicProvider`` and therefore
    inherits its ``message_delta`` token-coercion safety.  This test
    exercises the inherited ``_stream_chunks`` path through a real
    ``MiniMaxProvider`` instance to catch any future override that
    bypasses the coercion.
    """

    def _fake_stream(self, events):
        class _FakeAnthropicStream:
            def __init__(self, events):
                self._events = events

            def __enter__(self):
                return iter(self._events)

            def __exit__(self, exc_type, exc, tb):
                return False

        class _FakeAnthropicMessages:
            def __init__(self, events):
                self._events = events

            def stream(self, **_kwargs):
                return _FakeAnthropicStream(self._events)

        class _FakeAnthropicClient:
            def __init__(self, events):
                self.messages = _FakeAnthropicMessages(events)

        return _FakeAnthropicClient(events)

    def test_minimax_inherits_anthropic_message_delta_token_coercion(self) -> None:
        from rikugan.providers.minimax_provider import MiniMaxProvider

        provider = MiniMaxProvider(
            api_key="sk-test",
            model="MiniMax-M2.5",
        )
        events = [
            SimpleNamespace(
                type="message_delta",
                delta=SimpleNamespace(stop_reason=None),
                usage=SimpleNamespace(output_tokens="12"),
            )
        ]
        chunks = list(provider._stream_chunks(self._fake_stream(events), {}))
        usage_chunks = [c for c in chunks if c.usage is not None]
        self.assertEqual(len(usage_chunks), 1)
        assert usage_chunks[0].usage is not None
        self.assertEqual(usage_chunks[0].usage.completion_tokens, 12)


# ---------------------------------------------------------------------------
# Native tool-call recovery (the "]<]minimax[>[" text leak)
# ---------------------------------------------------------------------------


#: Byte-exact replay of the user-reported failure: thinking enabled, the
#: MiniMax Anthropic endpoint leaked the native tool-call XML + sentinel
#: tokens into the text channel and the chat stopped after this text.
_USER_REPORTED_OUTPUT = (
    "Let me also check the JSON helpers.]<]minimax[>[<tool_call> ]<]minimax[>["
    '<invoke name="get_function_info">]<]minimax[>[<address>0x10004620]<]minimax[>['
    "</address>]<]minimax[>[</invoke> ]<]minimax[>["
    '<invoke name="rename_function">]<]minimax[>[<address>0x10008A70]<]minimax[>['
    "</address>]<]minimax[>[<new_name>FormatInt64ToWString]<]minimax[>["
    "</new_name>]<]minimax[>[</invoke> ]<]minimax[>["
)


class TestMiniMaxNativeToolCallRecovery(unittest.TestCase):
    """Streamed native tool-call XML must become structured tool calls."""

    def _provider(self):
        from rikugan.providers.minimax_provider import MiniMaxProvider

        return MiniMaxProvider(api_key="sk-test", model="MiniMax-M3")

    def _fake_client(self, deltas, *, with_usage=True):
        """Fake anthropic client streaming the given text deltas."""
        events = [SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="text", text=""))]
        for d in deltas:
            events.append(
                SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text=d),
                )
            )
        events.append(SimpleNamespace(type="content_block_stop"))
        if with_usage:
            events.append(
                SimpleNamespace(
                    type="message_delta",
                    delta=SimpleNamespace(stop_reason="end_turn"),
                    usage=SimpleNamespace(output_tokens=100),
                )
            )

        class _Stream:
            def __init__(self, evts):
                self._evts = evts

            def __enter__(self):
                return iter(self._evts)

            def __exit__(self, *a):
                return False

        class _Messages:
            def stream(self, **_kw):
                return _Stream(events)

        class _Client:
            messages = _Messages()

        return _Client()

    def _run(self, deltas, **kw):
        provider = self._provider()
        return list(provider._stream_chunks(self._fake_client(deltas, **kw), {}))

    def test_user_reported_output_parsed_into_tool_calls(self):
        chunks = self._run([_USER_REPORTED_OUTPUT])
        starts = [c for c in chunks if c.is_tool_call_start]
        ends = [c for c in chunks if c.is_tool_call_end]
        self.assertEqual(len(starts), 2)
        self.assertEqual(len(ends), 2)
        self.assertEqual([c.tool_name for c in starts], ["get_function_info", "rename_function"])
        # Args arrive as one JSON delta per call and round-trip through json.loads
        for c in chunks:
            if c.tool_args_delta and not c.is_tool_call_end:
                json.loads(c.tool_args_delta)
        rename = next(c for c in starts if c.tool_name == "rename_function")
        args_chunks = [
            c.tool_args_delta
            for c in chunks
            if c.tool_call_id == rename.tool_call_id and c.tool_args_delta and not c.is_tool_call_end
        ]
        self.assertEqual(
            json.loads("".join(args_chunks)), {"address": "0x10008A70", "new_name": "FormatInt64ToWString"}
        )
        # Visible text: prose only — no sentinel, no XML
        text = "".join(c.text for c in chunks if c.text)
        self.assertTrue(text.startswith("Let me also check the JSON helpers."))
        self.assertNotIn("]<]minimax[>[", text)
        self.assertNotIn("<invoke", text)
        self.assertNotIn("</tool_call>", text)

    def test_char_by_char_split_deltas_still_detected(self):
        """Markers/sentinels split across arbitrary delta boundaries."""
        chunks = self._run(list(_USER_REPORTED_OUTPUT))
        starts = [c for c in chunks if c.is_tool_call_start]
        self.assertEqual(len(starts), 2)
        text = "".join(c.text for c in chunks if c.text)
        self.assertNotIn("]<]minimax", text)
        self.assertNotIn("<invoke", text)

    def test_plain_text_passes_through_unchanged(self):
        chunks = self._run(["Hello ", "world — no XML here <not-a-tool>"])
        self.assertEqual("".join(c.text for c in chunks if c.text), "Hello world — no XML here <not-a-tool>")
        self.assertFalse([c for c in chunks if c.is_tool_call_start])

    def test_trailing_sentinel_alone_is_stripped(self):
        chunks = self._run(["Done.", "]<]minimax[>["])
        text = "".join(c.text for c in chunks if c.text)
        self.assertEqual(text, "Done.")

    def test_truncated_stream_salvages_closed_invokes(self):
        """Stream cut before </tool_call> — closed invokes still execute."""
        chunks = self._run(
            [
                "Check this.",
                "]<]minimax[>[<tool_call> <invoke name='decompile_function'>"
                "<address>0x401000</address></invoke> <invoke name='get_",
            ]
        )
        starts = [c for c in chunks if c.is_tool_call_start]
        self.assertEqual([c.tool_name for c in starts], ["decompile_function"])

    def test_parameter_name_form_also_parsed(self):
        """Documented M2 form: <parameter name=\"k\">v</parameter>."""
        chunks = self._run(
            ['<tool_call><invoke name="get_weather"><parameter name="location">Tokyo</parameter></invoke></tool_call>']
        )
        starts = [c for c in chunks if c.is_tool_call_start]
        self.assertEqual(len(starts), 1)
        args = [c.tool_args_delta for c in chunks if c.tool_args_delta and not c.is_tool_call_end]
        self.assertEqual(json.loads(args[0]), {"location": "Tokyo"})

    def test_text_after_closed_block_continues_as_text(self):
        chunks = self._run(
            [
                '<tool_call><invoke name="f"><a>1</a></invoke></tool_call>',
                "All done now.",
            ]
        )
        text = "".join(c.text for c in chunks if c.text)
        self.assertIn("All done now.", text)

    def test_server_tool_use_chunks_disable_filter(self):
        """Server sent real tool_use chunks — text must pass through raw."""
        from rikugan.providers.minimax_provider import _NativeToolCallFilter

        flt = _NativeToolCallFilter()
        chunks = list(flt.feed(StreamChunk(tool_call_id="srv_1", tool_name="f", is_tool_call_start=True)))
        chunks += list(flt.feed(StreamChunk(text=_USER_REPORTED_OUTPUT)))
        chunks += list(flt.flush())
        self.assertEqual(len([c for c in chunks if c.is_tool_call_start]), 1)
        self.assertIn("<invoke", "".join(c.text for c in chunks if c.text))  # passthrough

    def test_raw_parts_rewritten_for_next_turn(self):
        """raw_parts: text cleaned + tool_use blocks with matching ids."""
        provider = self._provider()
        client = self._fake_client([_USER_REPORTED_OUTPUT])
        chunks = list(provider._stream_chunks(client, {}))
        raw = next(c for c in chunks if c.raw_parts is not None).raw_parts
        self.assertTrue(isinstance(raw, list))
        tool_use = [b for b in raw if b.get("type") == "tool_use"]
        self.assertEqual(len(tool_use), 2)
        starts = [c for c in chunks if c.is_tool_call_start]
        self.assertEqual([b["id"] for b in tool_use], [c.tool_call_id for c in starts])
        self.assertEqual(tool_use[0]["input"], {"address": "0x10004620"})
        text_blocks = [b for b in raw if b.get("type") == "text"]
        self.assertEqual(len(text_blocks), 1)
        self.assertNotIn("]<]minimax[>[", text_blocks[0]["text"])

    def test_normalize_response_recovers_native_calls(self):
        """Non-streaming path: same recovery on a finished message."""
        provider = self._provider()
        response = SimpleNamespace(
            content=[
                SimpleNamespace(type="thinking", thinking="hmm", signature="sig"),
                SimpleNamespace(type="text", text=_USER_REPORTED_OUTPUT),
            ],
            usage=SimpleNamespace(input_tokens=10, output_tokens=20),
        )
        msg = provider._normalize_response(response)
        self.assertEqual([tc.name for tc in msg.tool_calls], ["get_function_info", "rename_function"])
        self.assertNotIn("]<]minimax[>[", msg.content)
        self.assertIn("Let me also check", msg.content)
        raw = msg._raw_parts
        tool_use = [b for b in raw if b.get("type") == "tool_use"]
        self.assertEqual(len(tool_use), 2)


if __name__ == "__main__":
    unittest.main()
