"""Tests for lucnhan.agent.minify."""

from __future__ import annotations

from lucnhan.agent.minify import minify_messages, minify_text
from lucnhan.core.types import Message, ToolResult


class TestMinifyText:
    def test_empty_string(self):
        assert minify_text("") == ""

    def test_no_change_needed(self):
        assert minify_text("hello world") == "hello world"

    def test_strips_trailing_whitespace(self):
        assert minify_text("line   \nother") == "line\nother"

    def test_collapses_triple_blank_lines(self):
        result = minify_text("a\n\n\n\nb")
        assert result == "a\n\nb"

    def test_preserves_double_blank_line(self):
        result = minify_text("a\n\nb")
        assert result == "a\n\nb"

    def test_strips_leading_trailing_blank_lines(self):
        result = minify_text("\n\nhello\n\n")
        assert result == "hello"

    def test_preserves_indentation(self):
        code = "def foo():\n    return 1"
        assert minify_text(code) == code

    def test_multi_blank_collapse(self):
        result = minify_text("a\n\n\n\n\n\nb")
        assert result == "a\n\nb"


class TestMinifyMessages:
    def test_empty_list(self):
        assert minify_messages([]) == []

    def test_minifies_content(self):
        msg = Message(role="user", content="hello   \n\n\n\nworld")
        result = minify_messages([msg])
        assert result[0].content == "hello\n\nworld"

    def test_original_message_unchanged(self):
        msg = Message(role="user", content="hello   ")
        minify_messages([msg])
        assert msg.content == "hello   "

    def test_minifies_tool_results(self):
        tr = ToolResult(tool_call_id="1", name="t", content="result   \n\n\n\n", is_error=False)
        msg = Message(role="tool", content="", tool_results=[tr])
        result = minify_messages([msg])
        assert result[0].tool_results[0].content == "result"

    def test_none_content_unchanged(self):
        msg = Message(role="assistant", content=None)
        result = minify_messages([msg])
        assert result[0].content is None

    def test_preserves_role(self):
        msg = Message(role="assistant", content="hello")
        result = minify_messages([msg])
        assert result[0].role == "assistant"


# ---------------------------------------------------------------------------
# Context window compaction: reasoning rule
# ---------------------------------------------------------------------------


class TestCompactionReasoningRule:
    """When ``compact_messages()`` summarizes older messages, only visible
    ``content`` enters the summary — ``reasoning_content`` is dropped.
    Tail messages are untouched, preserving recent reasoning.
    """

    def test_summary_omits_old_reasoning_content(self):
        from lucnhan.agent.context_window import ContextWindowManager
        from lucnhan.core.types import Message, Role

        manager = ContextWindowManager()
        # 1 system + 6 middle + 4 tail = 11 messages (> 6 threshold).
        messages: list[Message] = [Message(role=Role.SYSTEM, content="system")]
        # Middle assistant message with reasoning that must NOT survive.
        messages.append(
            Message(
                role=Role.ASSISTANT,
                content="visible answer",
                reasoning_content="SECRET_REASONING_MUST_NOT_APPEAR",
            )
        )
        # Fill remaining middle messages.
        for i in range(5):
            messages.append(Message(role=Role.USER, content=f"msg {i}"))
        # Tail messages (last 4).
        for i in range(4):
            messages.append(Message(role=Role.USER, content=f"tail {i}"))

        compacted = manager.compact_messages(messages)

        # Find the summary message (second message after system).
        summary_text = compacted[1].content
        assert "SECRET_REASONING_MUST_NOT_APPEAR" not in summary_text

    def test_tail_messages_retain_reasoning_content(self):
        from lucnhan.agent.context_window import ContextWindowManager
        from lucnhan.core.types import Message, Role

        manager = ContextWindowManager()
        messages: list[Message] = [Message(role=Role.SYSTEM, content="system")]
        # Enough middle messages to trigger compaction.
        for i in range(6):
            messages.append(Message(role=Role.USER, content=f"middle {i}"))
        # Tail with reasoning that MUST be preserved.
        messages.append(
            Message(
                role=Role.ASSISTANT,
                content="tail visible",
                reasoning_content="RECENT_REASONING_PRESERVED",
            )
        )
        for i in range(3):
            messages.append(Message(role=Role.USER, content=f"tail {i}"))

        compacted = manager.compact_messages(messages)

        # The last messages must retain their reasoning_content unchanged.
        tail_assistants = [m for m in compacted if m.role == Role.ASSISTANT and m.reasoning_content]
        assert any("RECENT_REASONING_PRESERVED" in m.reasoning_content for m in tail_assistants)
