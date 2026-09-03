"""Tests for rikugan.agent.context_window (compact_messages)."""

from __future__ import annotations

from rikugan.agent.context_window import ContextWindowManager
from rikugan.core.types import Message, Role, ToolCall, ToolResult


def system_msg() -> Message:
    return Message(role=Role.SYSTEM, content="system")


def user_msg(content: str) -> Message:
    return Message(role=Role.USER, content=content)


def assistant_msg(content: str) -> Message:
    return Message(role=Role.ASSISTANT, content=content)


def assistant_tool_call(name: str, call_id: str) -> Message:
    """Assistant message requesting a single tool call."""
    return Message(
        role=Role.ASSISTANT,
        content="",
        tool_calls=[ToolCall(id=call_id, name=name, arguments={})],
    )


def tool_result(name: str, call_id: str, content: str = "ok") -> Message:
    """Tool message carrying the result of a single tool call."""
    return Message(
        role=Role.TOOL,
        content="",
        tool_results=[ToolResult(tool_call_id=call_id, name=name, content=content)],
    )


def assert_no_orphaned_tool_results(compacted: list[Message]) -> None:
    """Every TOOL message after the retained head must directly follow the
    assistant message holding its tool_call — otherwise OpenAI/Anthropic
    reject the whole request with 400 (orphaned tool result).
    """
    for i in range(1, len(compacted)):
        msg = compacted[i]
        if msg.role != Role.TOOL:
            continue
        prev = compacted[i - 1]
        partner_ids = {tc.id for tc in prev.tool_calls}
        assert prev.role == Role.ASSISTANT and partner_ids, (
            f"orphaned TOOL at compacted index {i}"
        )
        for tr in msg.tool_results:
            assert tr.tool_call_id in partner_ids, (
                f"TOOL at compacted index {i} references missing tool_call "
                f"{tr.tool_call_id}"
            )


class TestCompactionToolResultBoundary:
    """The compaction tail cut must never start on a TOOL message whose
    assistant tool_call partner was summarized away.
    """

    def test_compaction_does_not_orphan_leading_tool_result(self):
        manager = ContextWindowManager()
        # len(messages) - 4 lands exactly on the TOOL message; the fixed
        # [-4:] cut starts the tail there and orphans the result while its
        # assistant partner is summarized into the middle.
        messages = [
            system_msg(),
            user_msg("u1"),
            user_msg("u2"),
            assistant_tool_call("decompile_function", "call_1"),
            tool_result("decompile_function", "call_1"),
            assistant_msg("a1"),
            user_msg("u3"),
            user_msg("u4"),
        ]
        compacted = manager.compact_messages(messages)
        assert_no_orphaned_tool_results(compacted)
        # The displaced tool pair must land in the summary, not vanish.
        summary_text = compacted[1].content
        assert "decompile_function" in summary_text

    def test_compaction_does_not_orphan_second_parallel_tool_result(self):
        manager = ContextWindowManager()
        # Two parallel tool results: the boundary lands on the SECOND one.
        # Both results must stay together with each other or with their
        # shared assistant partner.
        messages = [
            system_msg(),
            user_msg("u1"),
            user_msg("u2"),
            user_msg("u3"),
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[
                    ToolCall(id="call_a", name="list_functions", arguments={}),
                    ToolCall(id="call_b", name="get_function", arguments={}),
                ],
            ),
            tool_result("list_functions", "call_a"),
            tool_result("get_function", "call_b"),
            assistant_msg("a1"),
            user_msg("u4"),
            user_msg("u5"),
        ]
        compacted = manager.compact_messages(messages)
        assert_no_orphaned_tool_results(compacted)

    def test_complete_tool_pair_survives_in_tail(self):
        manager = ContextWindowManager()
        # Shape from the review finding: boundary lands on a USER message, so
        # the trailing tool pair is retained in full in the tail — it must
        # never be summarized away nor split from its partner.
        messages = [
            system_msg(),
            user_msg("u1"),
            assistant_tool_call("decompile_function", "call_1"),
            tool_result("decompile_function", "call_1"),
            assistant_msg("a1"),
            user_msg("u2"),
            assistant_tool_call("rename_function", "call_2"),
            tool_result("rename_function", "call_2"),
            assistant_msg("a2"),
        ]
        compacted = manager.compact_messages(messages)
        assert_no_orphaned_tool_results(compacted)
        # The recent tool pair stays in the tail; only the older one is
        # summarized.
        tail_tool_names = [
            tr.name for m in compacted if m.role == Role.TOOL for tr in m.tool_results
        ]
        assert tail_tool_names == ["rename_function"]

    def test_invariant_holds_for_tool_pair_at_every_position(self):
        manager = ContextWindowManager()
        # Slide a tool pair through every insertion point of a 9-message
        # conversation (tail boundary = len - 4 = 5). The invariant must hold
        # whichever side of the cut the pair ends up on.
        for pos in range(1, 7):
            pair = [
                assistant_tool_call("decompile_function", f"call_{pos}"),
                tool_result("decompile_function", f"call_{pos}"),
            ]
            fillers = [user_msg(f"u{i}") for i in range(6)]
            messages = [system_msg(), *fillers[:pos], *pair, *fillers[pos:]]
            assert len(messages) == 9
            compacted = manager.compact_messages(messages)
            assert_no_orphaned_tool_results(compacted)
