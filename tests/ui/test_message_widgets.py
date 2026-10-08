"""Tests for message_widgets module.

Unit tests for the thinking-content split helpers
(``_extract_thinking_text``, ``_extract_visible_text``,
``_split_thinking``) and ``AssistantMessageWidget`` UI properties.
"""

import unittest

import pytest


class TestSplitThinking(unittest.TestCase):
    """Tests for the thinking-content split helpers."""

    def test_basic_thinking_block(self):
        """Single <think>...</think> block extracts cleanly."""
        from rikugan.ui.message_widgets import _split_thinking

        text = "<think>Let me analyze this.</think>The function is a handler."
        thinking, visible = _split_thinking(text)
        self.assertEqual(thinking, "Let me analyze this.")
        self.assertEqual(visible, "The function is a handler.")

    def test_thinking_block_with_surrounding_text(self):
        """Text before and after a <think>...</think> block is preserved."""
        from rikugan.ui.message_widgets import _split_thinking

        text = "Let me check this. <think>Checking the binary structure.</think>And here's the result."
        thinking, visible = _split_thinking(text)
        self.assertEqual(thinking, "Checking the binary structure.")
        self.assertEqual(visible, "Let me check this. And here's the result.")

    def test_multiple_thinking_blocks(self):
        """Multiple <think>...</think> blocks get joined with newlines."""
        from rikugan.ui.message_widgets import _split_thinking

        text = "<think>First thought.</think>Something.<think>Second thought.</think>End."
        thinking, visible = _split_thinking(text)
        self.assertEqual(thinking, "First thought.\n\nSecond thought.")
        self.assertEqual(visible, "Something.End.")

    def test_no_thinking_block(self):
        """Text with no thinking blocks: thinking is empty, visible is the full text."""
        from rikugan.ui.message_widgets import _split_thinking

        text = "Just regular output without any thinking."
        thinking, visible = _split_thinking(text)
        self.assertEqual(thinking, "")
        self.assertEqual(visible, "Just regular output without any thinking.")

    def test_unclosed_thinking_tag(self):
        """Unclosed <think> at end (streaming in progress) → partial thinking + visible before."""
        from rikugan.ui.message_widgets import _split_thinking

        text = "Some text before. <think>Still thinking here"
        thinking, visible = _split_thinking(text)
        self.assertEqual(thinking, "Still thinking here")
        self.assertEqual(visible, "Some text before.")

    def test_only_unclosed_thinking(self):
        """Only an unclosed thinking tag: thinking is the partial content, visible is empty."""
        from rikugan.ui.message_widgets import _split_thinking

        text = "<think>Just thinking, no close yet"
        thinking, visible = _split_thinking(text)
        self.assertEqual(thinking, "Just thinking, no close yet")
        self.assertEqual(visible, "")

    def test_empty_thinking_block(self):
        """Empty <think>...</think>: thinking is empty, visible is the rest."""
        from rikugan.ui.message_widgets import _split_thinking

        text = "<think></think>No thinking content."
        thinking, visible = _split_thinking(text)
        self.assertEqual(thinking, "")
        self.assertEqual(visible, "No thinking content.")

    def test_thoughtful_content_with_markdown(self):
        """Thinking block may contain markdown-like content; visible is rendered after."""
        from rikugan.ui.message_widgets import _split_thinking

        text = "<think>**analysis**: Looking at *function* `main`.</think>Output here."
        thinking, visible = _split_thinking(text)
        self.assertIn("**analysis**:", thinking)
        self.assertIn("Output here.", visible)


class TestExtractVisibleText(unittest.TestCase):
    """Tests for the unstripped visible-text helper.

    ``_extract_visible_text`` is the streaming counterpart to
    :func:`_split_thinking`: it returns the visible portion of the
    text WITHOUT stripping leading/trailing whitespace.  The chat
    view uses it so per-chunk appends preserve the inter-chunk
    boundary spaces that would otherwise vanish.
    """

    def test_no_thinking_preserves_trailing_whitespace(self):
        """Trailing whitespace is preserved (so the next chunk's leading
        space is not the only separator)."""
        from rikugan.ui.message_widgets import _extract_visible_text

        text = "I am "
        self.assertEqual(_extract_visible_text(text), "I am ")

    def test_no_thinking_preserves_leading_whitespace(self):
        """Leading whitespace of a mid-stream chunk is preserved so it
        can concatenate correctly with the previous chunk's content."""
        from rikugan.ui.message_widgets import _extract_visible_text

        text = " about"
        self.assertEqual(_extract_visible_text(text), " about")

    def test_complete_think_in_chunk_preserves_leading_visible_space(self):
        """For '<think>A</think> B' the leading space of the visible
        portion is preserved (the original LLM stream had a space
        between the think close and the next word)."""
        from rikugan.ui.message_widgets import _extract_visible_text

        text = "<think>A</think> B"
        self.assertEqual(_extract_visible_text(text), " B")

    def test_streaming_simulation_no_lost_spaces(self):
        """The bug this helper fixes: streaming chunks whose boundary
        sits at a space must concatenate to a single space, not
        'I amthinking' (which is what ``_split_thinking`` produced
        when its stripped visible was appended per chunk)."""
        from rikugan.ui.message_widgets import _extract_visible_text

        chunks = ["I am ", "thinking", " about", " code"]
        accumulated = "".join(_extract_visible_text(c) for c in chunks)
        self.assertEqual(accumulated, "I am thinking about code")

    def test_unclosed_thinking_strips_post_open_tag(self):
        """Unclosed <think> returns only the visible portion BEFORE the tag."""
        from rikugan.ui.message_widgets import _extract_visible_text

        text = "Some text before. <think>Still thinking here"
        self.assertEqual(_extract_visible_text(text), "Some text before. ")

    def test_empty_string(self):
        """Empty input → empty output."""
        from rikugan.ui.message_widgets import _extract_visible_text

        self.assertEqual(_extract_visible_text(""), "")


class TestExtractThinkingText(unittest.TestCase):
    """Tests for the unstripped thinking-content helper."""

    def test_single_block(self):
        from rikugan.ui.message_widgets import _extract_thinking_text

        text = "<think>Reasoning here.</think>Visible text."
        self.assertEqual(_extract_thinking_text(text), "Reasoning here.")

    def test_multiple_blocks_joined(self):
        from rikugan.ui.message_widgets import _extract_thinking_text

        text = "<think>First.</think>X<think>Second.</think>Y"
        self.assertEqual(_extract_thinking_text(text), "First.\n\nSecond.")

    def test_strips_inner_block_whitespace(self):
        """Leading/trailing whitespace INSIDE the <think> block is stripped."""
        from rikugan.ui.message_widgets import _extract_thinking_text

        text = "<think>\n  Thinking content.\n</think>Visible."
        self.assertEqual(_extract_thinking_text(text), "Thinking content.")

    def test_unclosed_thinking_keeps_partial(self):
        from rikugan.ui.message_widgets import _extract_thinking_text

        text = "Some text. <think>Still thinking here"
        self.assertEqual(_extract_thinking_text(text), "Still thinking here")

    def test_no_thinking_returns_empty(self):
        from rikugan.ui.message_widgets import _extract_thinking_text

        self.assertEqual(_extract_thinking_text("Just visible."), "")


class TestAssistantMessageWidgetUI(unittest.TestCase):
    """Structural / UI tests for AssistantMessageWidget.

    A ``qapp`` fixture must be active (provided by conftest.py) before
    constructing any Qt widgets.
    """

    @pytest.fixture(autouse=True)
    def setup_qapp(self, qapp):
        self.qapp = qapp

    def test_streaming_chunks_preserve_boundary_spaces(self):
        """Regression: appending per-chunk visible text preserves
        inter-chunk boundary spaces.

        The previous code passed ``_split_thinking``'s stripped visible
        to :meth:`append_text`, which dropped trailing whitespace on
        the first chunk and leading whitespace on subsequent chunks,
        so a stream like ``["I am ", "thinking", " about", " code"]``
        accumulated to ``"I amthinkingaboutcode"`` instead of
        ``"I am thinking about code"``.
        """
        from rikugan.ui.message_widgets import (
            AssistantMessageWidget,
            _extract_visible_text,
        )

        w = AssistantMessageWidget()
        chunks = ["I am ", "thinking", " about", " code"]
        for c in chunks:
            w.append_text(_extract_visible_text(c))
        self.assertEqual(w.full_text(), "I am thinking about code")


class TestKnowledgeContextWidget(unittest.TestCase):
    """The chat indicator for ``KNOWLEDGE_RETRIEVED`` must use a
    dedicated widget that does NOT render the misleading
    ``Subagent ... knowledge`` label.
    """

    @pytest.fixture(autouse=True)
    def setup_qapp(self, qapp):
        self.qapp = qapp

    def test_label_is_retrieved_knowledge_not_subagent(self):
        from rikugan.ui.message_widgets import KnowledgeContextWidget

        items = [
            {"kind": "memory", "id": "mem:crypto:0x401000:abc", "title": "RC4 decrypts"},
            {"kind": "entity", "id": "func:0x401000", "name": "rc4_ksa"},
            {"kind": "relation", "id": "rel:1:2", "src": "a", "predicate": "calls", "dst": "b"},
        ]
        w = KnowledgeContextWidget(
            summary="5 memories, 2 entities, 4 relations for 0x401000",
            items=items,
        )
        text = w._label.text()
        self.assertNotIn("Subagent", text)
        self.assertEqual(text, "Retrieved Knowledge")

    def test_renders_up_to_three_item_labels(self):
        from rikugan.ui.message_widgets import KnowledgeContextWidget

        items = [
            {"kind": "memory", "title": "alpha"},
            {"kind": "memory", "title": "beta"},
            {"kind": "memory", "title": "gamma"},
            {"kind": "memory", "title": "delta"},
        ]
        w = KnowledgeContextWidget(summary="x", items=items)
        assert w._detail is not None
        detail = w._detail.text()
        self.assertIn("alpha", detail)
        self.assertIn("beta", detail)
        self.assertIn("gamma", detail)
        # Fourth item is dropped to keep the pill compact.
        self.assertNotIn("delta", detail)
