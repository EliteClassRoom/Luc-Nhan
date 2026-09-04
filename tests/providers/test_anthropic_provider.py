"""Tests for Anthropic provider: message formatting, normalization, error handling, auth."""

from __future__ import annotations

import copy as _copy
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.core.types import LLMRequestContext, Message, Role, ToolCall, ToolResult


def _make_provider():
    from rikugan.providers.anthropic_provider import AnthropicProvider

    return AnthropicProvider(api_key="test-key", model="claude-test")


class TestAnthropicFormatMessages(unittest.TestCase):
    """Test AnthropicProvider._format_messages."""

    def test_user_message(self):
        p = _make_provider()
        msgs = [Message(role=Role.USER, content="Hello")]
        result = p._format_messages(msgs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "user")
        self.assertEqual(result[0]["content"], "Hello")

    def test_system_message_skipped(self):
        p = _make_provider()
        msgs = [
            Message(role=Role.SYSTEM, content="You are a helper"),
            Message(role=Role.USER, content="Hi"),
        ]
        result = p._format_messages(msgs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "user")

    def test_assistant_with_tool_calls(self):
        p = _make_provider()
        msgs = [
            Message(
                role=Role.ASSISTANT,
                content="Let me check",
                tool_calls=[ToolCall(id="tc_1", name="get_info", arguments={"x": 1})],
            )
        ]
        result = p._format_messages(msgs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "assistant")
        content = result[0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[0]["text"], "Let me check")
        self.assertEqual(content[1]["type"], "tool_use")
        self.assertEqual(content[1]["id"], "tc_1")
        self.assertEqual(content[1]["name"], "get_info")
        self.assertEqual(content[1]["input"], {"x": 1})

    def test_tool_results_become_user_messages(self):
        """Anthropic maps tool results to user messages with tool_result content."""
        p = _make_provider()
        msgs = [
            Message(
                role=Role.TOOL,
                tool_results=[
                    ToolResult(tool_call_id="tc_1", name="get_info", content="result1"),
                    ToolResult(tool_call_id="tc_2", name="get_more", content="result2", is_error=True),
                ],
            )
        ]
        result = p._format_messages(msgs)
        self.assertEqual(len(result), 2)
        for r in result:
            self.assertEqual(r["role"], "user")
            self.assertIsInstance(r["content"], list)
            self.assertEqual(r["content"][0]["type"], "tool_result")

        self.assertEqual(result[0]["content"][0]["tool_use_id"], "tc_1")
        self.assertFalse(result[0]["content"][0]["is_error"])
        self.assertEqual(result[1]["content"][0]["tool_use_id"], "tc_2")
        self.assertTrue(result[1]["content"][0]["is_error"])

    def test_full_conversation(self):
        p = _make_provider()
        msgs = [
            Message(role=Role.USER, content="Hello"),
            Message(
                role=Role.ASSISTANT, content="Hi there", tool_calls=[ToolCall(id="tc_1", name="test", arguments={})]
            ),
            Message(role=Role.TOOL, tool_results=[ToolResult(tool_call_id="tc_1", name="test", content="done")]),
            Message(role=Role.ASSISTANT, content="All done"),
        ]
        result = p._format_messages(msgs)
        self.assertEqual(len(result), 4)
        self.assertEqual(result[0]["role"], "user")
        self.assertEqual(result[1]["role"], "assistant")
        self.assertEqual(result[2]["role"], "user")  # tool result
        self.assertEqual(result[3]["role"], "assistant")


class TestAnthropicFormatTools(unittest.TestCase):
    def test_converts_openai_format_to_anthropic(self):
        p = _make_provider()
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "test_tool",
                    "description": "A test tool",
                    "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}},
                },
            }
        ]
        result = p._format_tools(tools)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "test_tool")
        self.assertEqual(result[0]["description"], "A test tool")
        self.assertIn("properties", result[0]["input_schema"])


class TestAnthropicNormalizeResponse(unittest.TestCase):
    def test_text_response(self):
        p = _make_provider()
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Hello world")],
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )
        msg = p._normalize_response(response)
        self.assertEqual(msg.role, Role.ASSISTANT)
        self.assertEqual(msg.content, "Hello world")
        self.assertEqual(msg.tool_calls, [])
        self.assertEqual(msg.token_usage.total_tokens, 15)

    def test_tool_use_response(self):
        p = _make_provider()
        response = SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="Let me check"),
                SimpleNamespace(type="tool_use", id="tc_1", name="get_info", input={"key": "val"}),
            ],
            usage=SimpleNamespace(
                input_tokens=20,
                output_tokens=10,
                cache_read_input_tokens=5,
                cache_creation_input_tokens=2,
            ),
        )
        msg = p._normalize_response(response)
        self.assertEqual(msg.content, "Let me check")
        self.assertEqual(len(msg.tool_calls), 1)
        self.assertEqual(msg.tool_calls[0].name, "get_info")
        self.assertEqual(msg.tool_calls[0].arguments, {"key": "val"})
        self.assertEqual(msg.token_usage.cache_read_tokens, 5)
        self.assertEqual(msg.token_usage.cache_creation_tokens, 2)


class TestAnthropicHandleApiError(unittest.TestCase):
    def test_generic_error_raises_provider_error(self):
        from rikugan.core.errors import ProviderError

        p = _make_provider()
        with self.assertRaises(ProviderError):
            p._handle_api_error(RuntimeError("something broke"))

    def test_context_length_error(self):
        from rikugan.core.errors import ProviderError

        p = _make_provider()
        with self.assertRaises(ProviderError):
            p._handle_api_error(RuntimeError("context window exceeded token limit"))


class TestAnthropicAuthResolution(unittest.TestCase):
    """Test resolve_anthropic_auth priority order."""

    def test_explicit_api_key(self):
        from rikugan.providers.anthropic_provider import resolve_anthropic_auth

        token, auth_type = resolve_anthropic_auth("sk-ant-api03-test")
        self.assertEqual(token, "sk-ant-api03-test")
        self.assertEqual(auth_type, "api_key")

    def test_explicit_oauth_token(self):
        from rikugan.providers.anthropic_provider import resolve_anthropic_auth

        token, auth_type = resolve_anthropic_auth("sk-ant-oat01-test")
        self.assertEqual(token, "sk-ant-oat01-test")
        self.assertEqual(auth_type, "oauth")

    def test_env_var_api_key(self):
        from rikugan.providers.anthropic_provider import resolve_anthropic_auth

        old = os.environ.get("ANTHROPIC_API_KEY")
        try:
            os.environ["ANTHROPIC_API_KEY"] = "sk-from-env"
            token, auth_type = resolve_anthropic_auth("")
            self.assertEqual(token, "sk-from-env")
            self.assertEqual(auth_type, "api_key")
        finally:
            if old is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = old

    def test_empty_returns_empty(self):
        from rikugan.providers.anthropic_provider import resolve_anthropic_auth

        old_api = os.environ.pop("ANTHROPIC_API_KEY", None)
        old_oauth = os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        try:
            token, auth_type = resolve_anthropic_auth("")
            # May find Keychain token on macOS, but should not crash
            self.assertIsInstance(token, str)
            self.assertIsInstance(auth_type, str)
        finally:
            if old_api is not None:
                os.environ["ANTHROPIC_API_KEY"] = old_api
            if old_oauth is not None:
                os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = old_oauth

    def test_auth_status_with_key(self):
        from rikugan.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(api_key="sk-test", model="test")
        label, status = p.auth_status()
        self.assertEqual(status, "ok")

    def test_auth_status_oauth(self):
        from rikugan.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(api_key="sk-ant-oat01-test", model="test")
        label, status = p.auth_status()
        self.assertEqual(status, "ok")
        self.assertEqual(label, "OAuth")


# ---------------------------------------------------------------------------
# Task 5: payload equivalence.
# ---------------------------------------------------------------------------


class TestAnthropicRequestContextPayloadEquivalence(unittest.TestCase):
    def test_request_context_does_not_change_anthropic_payload(self) -> None:
        provider = _make_provider()
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
            "Anthropic payload differs when request_context is provided — "
            "the context must be a pure pass-through for non-GLM "
            "providers.",
        )


class TestAnthropicRawPartsDeepCopy(unittest.TestCase):
    """Mutations during _build_request_kwargs must never leak back into
    Message._raw_parts. Two requests with the same Message must produce
    equal payloads and the source Message must be byte-equal after both
    calls (round-trip parity through the assistant raw-part replay path).
    """

    def _make_assistant(self) -> Message:
        msg = Message(role=Role.ASSISTANT, content="")
        msg._raw_parts = [
            {"type": "thinking", "thinking": "hmm", "signature": "sig"},
            {"type": "text", "text": "checking"},
            {
                "type": "tool_use",
                "id": "tc_1",
                "name": "look",
                "input": {"x": 1},
            },
        ]
        return msg

    def test_format_messages_deep_copies_replayed_raw_blocks(self) -> None:
        # Direct call to _format_messages: deep-copy contract must hold at
        # the source (no shared references on top-level block dicts OR
        # nested dicts inside tool_use.input), regardless of any
        # downstream mutation. This catches the regression independently
        # of _build_request_kwargs and stays meaningful even if the
        # cache_control injection site moves.
        provider = _make_provider()
        assistant = self._make_assistant()
        original_blocks = _copy.deepcopy(assistant._raw_parts)
        original_inner_input = _copy.deepcopy(assistant._raw_parts[-1]["input"])

        formatted = provider._format_messages([assistant])
        self.assertEqual(len(formatted), 1)
        replayed = formatted[0]["content"]

        # Every top-level block must be a fresh dict, not the source.
        self.assertEqual(len(replayed), len(assistant._raw_parts))
        for src_block, repl_block in zip(assistant._raw_parts, replayed):
            self.assertIsNot(
                repl_block,
                src_block,
                "_format_messages must deep-copy each top-level block.",
            )

        # Nested dicts must also be fresh — tool_use.input especially.
        self.assertIsNot(
            replayed[-1]["input"],
            assistant._raw_parts[-1]["input"],
            "_format_messages must deep-copy tool_use.input dict.",
            )

        # Mutating the formatted slot must NOT touch the source.
        replayed[-1]["cache_control"] = {"type": "ephemeral"}
        replayed[-1]["input"]["x"] = 999
        self.assertEqual(assistant._raw_parts, original_blocks)
        self.assertEqual(assistant._raw_parts[-1]["input"], original_inner_input)

    def test_two_requests_with_same_message_round_trip(self) -> None:
        # Wired through _build_request_kwargs: cache_control injection on
        # the trailing assistant block must land on the formatted slot,
        # NOT on Message._raw_parts. This catches the dropped-continue
        # bug (cache_control lands on a *different* block when the
        # raw_parts replay falls through to the fallback branch).
        provider = _make_provider()
        assistant = self._make_assistant()
        snapshot_before = _copy.deepcopy(assistant._raw_parts)

        messages = [
            Message(role=Role.USER, content="hi"),
            Message(role=Role.TOOL, tool_results=[ToolResult(tool_call_id="tc_1", name="look", content="ok")]),
            Message(role=Role.ASSISTANT, content="prev"),
            Message(role=Role.USER, content="again?"),
            assistant,  # last — qualifies for cache_control injection
        ]

        kw1 = provider._build_request_kwargs(messages, None, 0.3, 4096, "system")
        kw2 = provider._build_request_kwargs(messages, None, 0.3, 4096, "system")
        snapshot_after = _copy.deepcopy(assistant._raw_parts)

        # Payload equality across two independent requests.
        self.assertEqual(kw1, kw2)

        # Original Message._raw_parts byte-equal before and after both calls.
        self.assertEqual(snapshot_after, snapshot_before)

        # No block inside _raw_parts picked up a cache_control field
        # injected by _build_request_kwargs — it must land on the deep
        # copy, not the source.
        for block in assistant._raw_parts:
            self.assertNotIn(
                "cache_control",
                block,
                "build_request_kwargs leaked cache_control into Message._raw_parts — "
                "raw blocks must be deep-copied before mutation.",
            )

        # Cache_control from request one matches request two (round-trip
        # parity) and was emitted on the *deep-copied* slot, not the
        # original. Also: cache_control must be on the LAST block of the
        # raw_parts replay — NOT on an unrelated empty-content fallback
        # block (which would mean the replay branch fell through and
        # produced a duplicate formatted entry).
        msgs_out = kw1["messages"]
        last = msgs_out[-1]
        last_block = last["content"][-1]
        self.assertIn("cache_control", last_block)
        self.assertEqual(
            last_block.get("type"),
            assistant._raw_parts[-1].get("type"),
            "cache_control landed on the wrong block — _format_messages "
            "likely double-appended (replay + fallback).",
        )
        same_in_kw2 = kw2["messages"][-1]["content"][-1].get("cache_control")
        self.assertEqual(same_in_kw2, last_block["cache_control"])
        self.assertIsNot(last_block, assistant._raw_parts[-1])
if __name__ == "__main__":
    unittest.main()
