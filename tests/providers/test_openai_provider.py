"""Tests for OpenAI provider: message formatting, normalization, error handling."""

from __future__ import annotations

import json
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.core.types import (
    LLMRequestContext,
    Message,
    Role,
    ToolCall,
    ToolResult,
)  # noqa: E402


def _make_provider():
    from rikugan.providers.openai_provider import OpenAIProvider

    return OpenAIProvider(api_key="test-key", model="gpt-test")


class TestOpenAIFormatMessages(unittest.TestCase):
    def test_user_message(self):
        p = _make_provider()
        msgs = [Message(role=Role.USER, content="Hello")]
        result = p._format_messages(msgs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "user")

    def test_system_message_included(self):
        """OpenAI keeps system messages in the message array."""
        p = _make_provider()
        msgs = [
            Message(role=Role.SYSTEM, content="You are a helper"),
            Message(role=Role.USER, content="Hi"),
        ]
        result = p._format_messages(msgs)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["role"], "system")

    def test_assistant_with_tool_calls(self):
        p = _make_provider()
        msgs = [
            Message(
                role=Role.ASSISTANT,
                content="Checking",
                tool_calls=[ToolCall(id="tc_1", name="get_info", arguments={"x": 1})],
            )
        ]
        result = p._format_messages(msgs)
        self.assertEqual(result[0]["role"], "assistant")
        self.assertEqual(result[0]["content"], "Checking")
        self.assertEqual(len(result[0]["tool_calls"]), 1)
        tc = result[0]["tool_calls"][0]
        self.assertEqual(tc["id"], "tc_1")
        self.assertEqual(tc["type"], "function")
        self.assertEqual(tc["function"]["name"], "get_info")
        self.assertEqual(json.loads(tc["function"]["arguments"]), {"x": 1})

    def test_tool_results_use_tool_role(self):
        """OpenAI keeps tool results as 'tool' role messages."""
        p = _make_provider()
        # Include the matching assistant tool_call so the tool result
        # has a valid antecedent — otherwise the dedup/rewrite path
        # would generate a fresh ``call_dedup_*`` id, which is exercised
        # separately by the rewrite/dedupe tests below.
        msgs = [
            Message(
                role=Role.ASSISTANT,
                content="Checking",
                tool_calls=[ToolCall(id="tc_1", name="get_info", arguments={"x": 1})],
            ),
            Message(
                role=Role.TOOL,
                tool_results=[
                    ToolResult(tool_call_id="tc_1", name="get_info", content="result"),
                ],
            ),
        ]
        result = p._format_messages(msgs)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["role"], "assistant")
        self.assertEqual(result[1]["role"], "tool")
        self.assertEqual(result[1]["tool_call_id"], "tc_1")
        self.assertEqual(result[1]["content"], "result")


class TestOpenAIToolCallIdRewriteAndDedupe(unittest.TestCase):
    """Focused tests for the tool-call id rewrite / dedup contract.

    OpenAI rejects requests that contain duplicate assistant
    ``tool_calls[].id`` values, but a restored or agent-loop
    session history can contain such duplicates.  The provider
    must therefore:

    1. Generate a fresh replacement id for any assistant
       ``tool_call`` whose id is empty or collides with one
       already emitted in the request.
    2. Rewrite the corresponding TOOL result's ``tool_call_id``
       so the orphan result still references the (rewritten)
       assistant id.
    3. Repair TOOL results whose ``tool_call_id`` does not
       appear in any assistant tool_call seen so far in the
       request — OpenAI's API would otherwise return 400 because
       the tool result references a non-existent tool_call.
    """

    def test_orphan_tool_result_gets_dedup_id(self) -> None:
        """A TOOL message with no matching assistant tool_call must
        be repaired with a fresh ``call_dedup_*`` id so the OpenAI
        request does not reference a non-existent tool_call.
        """
        p = _make_provider()
        msgs = [
            Message(
                role=Role.TOOL,
                tool_results=[
                    ToolResult(tool_call_id="orphan_1", name="get_info", content="r"),
                ],
            )
        ]
        result = p._format_messages(msgs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "tool")
        self.assertNotEqual(
            result[0]["tool_call_id"],
            "orphan_1",
            "Orphan tool result id must be rewritten to a call_dedup_* id "
            "so the request does not reference a non-existent tool_call.",
        )
        self.assertTrue(
            result[0]["tool_call_id"].startswith("call_dedup_"),
            f"Expected a call_dedup_* replacement id, got {result[0]['tool_call_id']!r}.",
        )

    def test_duplicate_assistant_tool_call_ids_get_rewritten(self) -> None:
        """Two assistant tool_calls with the same id must be
        rewritten to unique replacement ids; the corresponding
        tool results must follow the same replacement.
        """
        p = _make_provider()
        msgs = [
            Message(
                role=Role.ASSISTANT,
                content="two calls",
                tool_calls=[
                    ToolCall(id="dup_1", name="t", arguments={}),
                    ToolCall(id="dup_1", name="t", arguments={}),
                ],
            ),
            Message(
                role=Role.TOOL,
                tool_results=[
                    ToolResult(tool_call_id="dup_1", name="t", content="r1"),
                    ToolResult(tool_call_id="dup_1", name="t", content="r2"),
                ],
            ),
        ]
        result = p._format_messages(msgs)
        self.assertEqual(result[0]["role"], "assistant")
        assistant_ids = [tc["id"] for tc in result[0]["tool_calls"]]
        self.assertEqual(
            len(set(assistant_ids)),
            2,
            f"Assistant tool_calls[].id values must be unique, got {assistant_ids!r}.",
        )
        # The first assistant tool_call keeps the original id; the
        # second is rewritten to a call_dedup_* id.
        self.assertEqual(assistant_ids[0], "dup_1")
        self.assertTrue(assistant_ids[1].startswith("call_dedup_"))
        # The TOOL result ids must reference the assistant ids.
        # Specifically: the rewrite queue pops first, so the first
        # tool result gets the rewritten id; the second tool result
        # then falls through to the (still-valid) original "dup_1"
        # because that id is in used_ids.
        self.assertEqual(result[1]["role"], "tool")
        self.assertEqual(result[2]["role"], "tool")
        self.assertEqual(result[1]["tool_call_id"], assistant_ids[1])
        self.assertEqual(result[2]["tool_call_id"], "dup_1")
        # Sanity: together the two tool result ids cover both
        # assistant ids in some order.
        self.assertEqual(
            sorted([result[1]["tool_call_id"], result[2]["tool_call_id"]]),
            sorted(assistant_ids),
        )

    def test_empty_assistant_tool_call_id_is_replaced(self) -> None:
        """An empty assistant tool_calls[].id must be replaced
        with a fresh call_dedup_* id; the corresponding tool
        result must follow.
        """
        p = _make_provider()
        msgs = [
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="", name="t", arguments={})],
            ),
            Message(
                role=Role.TOOL,
                tool_results=[ToolResult(tool_call_id="", name="t", content="r")],
            ),
        ]
        result = p._format_messages(msgs)
        self.assertTrue(result[0]["tool_calls"][0]["id"].startswith("call_dedup_"))
        self.assertTrue(result[1]["tool_call_id"].startswith("call_dedup_"))
        # Both replacements must be the same id (the result must
        # reference the same rewritten id as the assistant call).
        self.assertEqual(
            result[0]["tool_calls"][0]["id"],
            result[1]["tool_call_id"],
            "Rewritten tool_call_id on the assistant side and the matching tool result must agree.",
        )

    def test_dedup_does_not_mutate_input_messages(self) -> None:
        """``_format_messages`` must not mutate the input ``Message``
        objects — the rewrite is only applied to the outgoing dicts.
        """
        p = _make_provider()
        assistant = Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="dup", name="t", arguments={})],
        )
        assistant.tool_calls.append(ToolCall(id="dup", name="t", arguments={}))
        tool_msg = Message(
            role=Role.TOOL,
            tool_results=[
                ToolResult(tool_call_id="dup", name="t", content="r1"),
                ToolResult(tool_call_id="dup", name="t", content="r2"),
            ],
        )
        msgs = [assistant, tool_msg]
        p._format_messages(msgs)
        # Original ids must be unchanged.
        self.assertEqual(assistant.tool_calls[0].id, "dup")
        self.assertEqual(assistant.tool_calls[1].id, "dup")
        self.assertEqual(tool_msg.tool_results[0].tool_call_id, "dup")
        self.assertEqual(tool_msg.tool_results[1].tool_call_id, "dup")


class TestOpenAINormalizeResponse(unittest.TestCase):
    def test_text_response(self):
        p = _make_provider()
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="Hello", tool_calls=None),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
        msg = p._normalize_response(response)
        self.assertEqual(msg.content, "Hello")
        self.assertEqual(msg.tool_calls, [])
        self.assertEqual(msg.token_usage.total_tokens, 15)

    def test_tool_call_response(self):
        p = _make_provider()
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="tc_1",
                                function=SimpleNamespace(
                                    name="test_tool",
                                    arguments='{"key": "val"}',
                                ),
                            )
                        ],
                    ),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=10, total_tokens=30),
        )
        msg = p._normalize_response(response)
        self.assertEqual(msg.content, "")
        self.assertEqual(len(msg.tool_calls), 1)
        self.assertEqual(msg.tool_calls[0].name, "test_tool")
        self.assertEqual(msg.tool_calls[0].arguments, {"key": "val"})

    def test_no_usage(self):
        p = _make_provider()
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="OK", tool_calls=None),
                )
            ],
            usage=None,
        )
        msg = p._normalize_response(response)
        self.assertEqual(msg.token_usage.total_tokens, 0)


class TestOpenAIHandleApiError(unittest.TestCase):
    def test_generic_error_raises_provider_error(self):
        from rikugan.core.errors import ProviderError

        p = _make_provider()
        with self.assertRaises(ProviderError):
            p._handle_api_error(RuntimeError("something broke"))

    def test_context_length_string(self):
        from rikugan.core.errors import ProviderError

        p = _make_provider()
        with self.assertRaises(ProviderError):
            p._handle_api_error(RuntimeError("maximum context length exceeded"))


# ---------------------------------------------------------------------------
# Streaming tool-call lifecycle tests
# ---------------------------------------------------------------------------
#
# The streaming tests below drive ``_iter_stream_chunks`` (the inner
# loop of ``_stream_chunks``) against synthetic chunk iterables, so they
# do not need a real OpenAI client.  Each chunk is a SimpleNamespace
# shaped like the openai SDK's ``CompletionChunk``: ``choices[0].delta``
# exposes ``content``, ``reasoning_content``, and ``tool_calls``;
# ``choices[0].finish_reason`` is set on the final chunk.


def _delta_chunk(*, content: str = "", tool_calls=None, finish_reason=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=content or None,
                    reasoning_content=None,
                    tool_calls=tool_calls,
                ),
                finish_reason=finish_reason,
            )
        ],
        usage=None,
    )


def _tc_delta(index: int, *, id: str | None = None, name: str | None = None, arguments: str = ""):
    return SimpleNamespace(
        index=index,
        id=id,
        function=SimpleNamespace(
            name=name,
            arguments=arguments,
        ),
    )


class TestOpenAIStreamLateIdArgsReplay(unittest.TestCase):
    """Regression: a tool-call id may arrive AFTER the first argument
    fragment.  Earlier code buffered the early argument bytes but
    never emitted a ``tool_args_delta`` for them, so the consumer
    saw only the second fragment — the JSON arguments came out
    truncated.

    These tests pin the corrected behaviour: the early fragment is
    emitted as soon as the id arrives, and the full concatenated
    argument string reaches the consumer in order.
    """

    def test_late_id_replays_early_argument_fragment(self) -> None:
        """A delta with arguments but no id must NOT cause the
        argument bytes to be dropped.  After the id arrives on a
        later delta the consumer must see the full argument
        stream in order, with no duplicate emission of any byte.
        """
        p = _make_provider()
        chunks = [
            # Delta 1: index 0, no id yet, name + first arg fragment.
            _delta_chunk(
                tool_calls=[
                    _tc_delta(index=0, id=None, name="do_thing", arguments='{"a"'),
                ],
            ),
            # Delta 2: index 0, id arrives with the second arg fragment.
            _delta_chunk(
                tool_calls=[
                    _tc_delta(index=0, id="call_1", arguments=": 1}"),
                ],
            ),
            # Final chunk: tool_calls finish.
            _delta_chunk(finish_reason="tool_calls"),
        ]
        emitted = list(p._iter_stream_chunks(iter(chunks)))

        starts = [c for c in emitted if c.is_tool_call_start]
        ends = [c for c in emitted if c.is_tool_call_end]
        arg_deltas = [c.tool_args_delta for c in emitted if c.tool_args_delta]

        # Exactly one start for call_1 (the early delta had no id,
        # so the start was deferred until the id-bearing delta).
        self.assertEqual(len(starts), 1, f"expected one start, got {len(starts)}")
        self.assertEqual(starts[0].tool_call_id, "call_1")
        # Exactly one end for call_1.
        self.assertEqual(len(ends), 1, f"expected one end, got {len(ends)}")
        self.assertEqual(ends[0].tool_call_id, "call_1")
        # The two argument fragments must be emitted in order and
        # concatenated back to the original JSON.
        self.assertEqual(
            "".join(arg_deltas),
            '{"a": 1}',
            f"argument bytes lost or out of order: {arg_deltas!r}",
        )
        # And the finish reason must be emitted exactly once.
        finish = [c.finish_reason for c in emitted if c.finish_reason]
        self.assertEqual(finish, ["tool_calls"])

    def test_early_id_no_replay(self) -> None:
        """The standard case (id arrives with the first fragment) must
        not regress: the start fires immediately and the argument
        deltas are emitted as they arrive — no late replay needed,
        no double emission."""
        p = _make_provider()
        chunks = [
            _delta_chunk(
                tool_calls=[
                    _tc_delta(index=0, id="call_1", name="do_thing", arguments='{"a"'),
                ],
            ),
            _delta_chunk(
                tool_calls=[
                    _tc_delta(index=0, id="call_1", arguments=": 1}"),
                ],
            ),
            _delta_chunk(finish_reason="tool_calls"),
        ]
        emitted = list(p._iter_stream_chunks(iter(chunks)))

        starts = [c for c in emitted if c.is_tool_call_start]
        ends = [c for c in emitted if c.is_tool_call_end]
        arg_deltas = [c.tool_args_delta for c in emitted if c.tool_args_delta]

        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].tool_call_id, "call_1")
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0].tool_call_id, "call_1")
        self.assertEqual("".join(arg_deltas), '{"a": 1}')
        # No duplicate emissions of either fragment.
        self.assertEqual(len(arg_deltas), 2)

    def test_duplicate_start_id_is_suppressed(self) -> None:
        """If the upstream proxy re-emits the same start/id on a
        later delta, only one ``is_tool_call_start`` is yielded."""
        p = _make_provider()
        chunks = [
            _delta_chunk(
                tool_calls=[
                    _tc_delta(index=0, id="call_1", name="do_thing", arguments='{"a"'),
                ],
            ),
            _delta_chunk(
                tool_calls=[
                    _tc_delta(index=0, id="call_1", name="do_thing", arguments=": 1}"),
                ],
            ),
            _delta_chunk(
                tool_calls=[
                    _tc_delta(index=0, id="call_1", name="do_thing", arguments=""),
                ],
            ),
            _delta_chunk(finish_reason="tool_calls"),
        ]
        emitted = list(p._iter_stream_chunks(iter(chunks)))
        starts = [c for c in emitted if c.is_tool_call_start]
        self.assertEqual(len(starts), 1, "duplicate start id must be suppressed")

    def test_duplicate_end_id_is_suppressed(self) -> None:
        """If finish_reason is reported twice (some proxies re-emit
        the final chunk) only one ``is_tool_call_end`` is yielded."""
        p = _make_provider()
        chunks = [
            _delta_chunk(
                tool_calls=[
                    _tc_delta(index=0, id="call_1", name="do_thing", arguments="{}"),
                ],
            ),
            _delta_chunk(finish_reason="tool_calls"),
            _delta_chunk(finish_reason="tool_calls"),
        ]
        emitted = list(p._iter_stream_chunks(iter(chunks)))
        ends = [c for c in emitted if c.is_tool_call_end]
        self.assertEqual(len(ends), 1, "duplicate end id must be suppressed")

    def test_cumulative_usage_yielded_once(self) -> None:
        """A final usage-only chunk (choices == []) yields exactly
        one usage StreamChunk — duplicates from re-emitted chunks
        are suppressed.
        """
        p = _make_provider()
        usage_chunk = SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
        # First usage chunk, then a duplicate, then another duplicate.
        emitted = list(p._iter_stream_chunks(iter([usage_chunk, usage_chunk, usage_chunk])))
        usage_chunks = [c for c in emitted if c.usage is not None]
        self.assertEqual(len(usage_chunks), 1, "duplicate usage must be suppressed")
        self.assertEqual(usage_chunks[0].usage.total_tokens, 15)


# ---------------------------------------------------------------------------
# Reasoning content + tool call boundary tests.
#
# Plain OpenAI (capability.reasoning_content == False) lanes legacy o-series
# reasoning into the visible-text channel wrapped in ``<think>...</think>``
# markers. The close marker is emitted once the reasoning lane ends, which
# always precedes the TOOL_CALL_END event. The lifecycle must stay complete
# (one START/END pair) and the wrapper must be closed before the end so the
# agent loop never carries an unclosed ``<think>`` block past the tool call.
# ---------------------------------------------------------------------------


def _reasoning_chunk(reasoning_content: str):
    """A delta whose only payload is reasoning_content (the legacy o-series
    path used by plain OpenAI when capability.reasoning_content is False)."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=None,
                    reasoning_content=reasoning_content,
                    tool_calls=None,
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )


class TestOpenAIStreamReasoningAndToolCall(unittest.TestCase):
    def test_inline_think_then_tool_call_emit_order(self) -> None:
        """When reasoning arrives before a structured tool call in the
        plain OpenAI legacy ``<think>`` lane, the provider emits the
        open, the reasoning text, then the close as soon as the
        reasoning lane ends — before the tool-call lifecycle in this
        scenario — and the tool call still gets a complete
        START/END pair with the wrapper closed before the END."""
        p = _make_provider()
        chunks = [
            _reasoning_chunk("thinking"),
            _delta_chunk(
                tool_calls=[
                    _tc_delta(index=0, id="call_1", name="do_thing", arguments="{}"),
                ],
            ),
            _delta_chunk(finish_reason="tool_calls"),
        ]
        emitted = list(p._iter_stream_chunks(iter(chunks)))

        # Find lifecycle markers by type and position.
        text_events = [(i, c.text) for i, c in enumerate(emitted) if c.text]
        opens = [i for i, t in text_events if t == "<think>"]
        closes = [i for i, t in text_events if t == "</think>\n"]
        starts = [i for i, c in enumerate(emitted) if c.is_tool_call_start]
        ends = [i for i, c in enumerate(emitted) if c.is_tool_call_end]
        self.assertEqual(len(opens), 1)
        self.assertEqual(len(closes), 1)
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(ends), 1)
        self.assertLess(opens[0], closes[0])
        self.assertLess(starts[0], ends[0])
        # The wrapper must be closed before the tool-call lifecycle ends.
        self.assertLess(closes[0], ends[0])
        # Balanced, contiguous wrapper in the visible channel.
        self.assertEqual(
            "".join(t for _, t in text_events), "<think>thinking</think>\n"
        )

    def test_reasoning_then_tool_call_in_same_chunk(self) -> None:
        """A single delta with both reasoning_content and tool_calls
        must still emit a complete tool-call lifecycle: the reasoning
        wrapper is opened, the tool call starts, and the ``<think>``
        close fires on the finish-chunk — between START and END —
        so the wrapper is closed before the tool call ends."""
        # Build a delta that carries both reasoning_content and a tool
 # call, then a finish-chunk.
        combined = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content="thinking",
                        tool_calls=[
                            _tc_delta(index=0, id="call_1", name="do_thing", arguments="{}"),
                        ],
                    ),
                finish_reason=None,
                ),
            ],
            usage=None,
        )
        finish = _delta_chunk(finish_reason="tool_calls")
        p = _make_provider()
        emitted = list(p._iter_stream_chunks(iter([combined, finish])))

        text_events = [(i, c.text) for i, c in enumerate(emitted) if c.text]
        opens = [i for i, t in text_events if t == "<think>"]
        closes = [i for i, t in text_events if t == "</think>\n"]
        starts = [i for i, c in enumerate(emitted) if c.is_tool_call_start]
        ends = [i for i, c in enumerate(emitted) if c.is_tool_call_end]
        self.assertEqual(len(opens), 1)
        self.assertEqual(len(closes), 1)
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(ends), 1)
        self.assertLess(opens[0], closes[0])
        self.assertLess(starts[0], ends[0])
        # Same-delta reasoning + tool call: the close fires on the
        # finish chunk, between START and END, still before the END.
        self.assertLess(starts[0], closes[0])
        self.assertLess(closes[0], ends[0])
        self.assertEqual(
            "".join(t for _, t in text_events), "<think>thinking</think>\n"
        )


# ---------------------------------------------------------------------------
# Task 5: payload equivalence.
#
# Threading an ``LLMRequestContext`` through the pipeline must not change
# the wire payload for non-GLM providers.  This regression pin guarantees
# that adding the keyword-only argument is a pure pass-through.
# ---------------------------------------------------------------------------


class TestOpenAIRequestContextPayloadEquivalence(unittest.TestCase):
    def test_request_context_does_not_change_openai_payload(self) -> None:
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
            "OpenAI payload differs when request_context is provided — "
            "the context must be a pure pass-through for non-GLM "
            "providers.",
        )


if __name__ == "__main__":
    unittest.main()
