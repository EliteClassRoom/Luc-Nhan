"""Tests for _ThinkingBlock render gating during reasoning streaming.

Background: GLM streams REASONING_DELTA events at high frequency
(50+ deltas/s).  Without time-gating, each delta triggers a full
``md_to_html(full_accumulated_text)`` call on the main thread —
67-99ms per call on IDA's Python 3.13 when reasoning exceeds ~8k
chars (production PROFILE log).  This blocks the main thread and
freezes IDA.

``AssistantMessageWidget`` already solves this with a batch+time
gate (``_RENDER_INTERVAL_S``, ``_RENDER_BATCH_MIN/MAX``).  The
same pattern is applied to ``_ThinkingBlock.append_reasoning`` so
intermediate reasoning renders are capped to ~10fps, while the
final ``set_thinking`` (on TEXT_DELTA/TEXT_DONE) still renders the
complete content once.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()

from rikugan.ui.message_widgets import _ThinkingBlock  # noqa: E402


class TestThinkingBlockRenderGating(unittest.TestCase):
    """``append_reasoning`` must batch md_to_html calls via a time gate."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls._qapp = QApplication.instance() or QApplication([])

    def test_append_reasoning_does_not_render_every_delta(self):
        """Rapid deltas below the batch minimum must not trigger md_to_html."""
        block = _ThinkingBlock()
        with patch.object(_ThinkingBlock, "_render_content", return_value=None) as mock_render:
            for _ in range(50):
                block.append_reasoning("x" * 10)
        # With default batch min (30 chars) and time gate (100ms),
        # 50 rapid deltas of 10 chars each = 500 total chars.
        # Some renders may fire (batch max 500 triggers unconditional),
        # but it must be far fewer than 50.
        self.assertLess(
            mock_render.call_count,
            50,
            f"Expected fewer than 50 renders, got {mock_render.call_count}",
        )

    def test_append_reasoning_burst_flushes_at_batch_max(self):
        """A single very large delta must trigger at least one render."""
        block = _ThinkingBlock()
        with patch.object(_ThinkingBlock, "_render_content", return_value=None) as mock_render:
            block.append_reasoning("x" * 600)
        self.assertGreaterEqual(mock_render.call_count, 1)

    def test_set_thinking_always_renders(self):
        """``set_thinking`` (final call) must always render, ignoring gate."""
        block = _ThinkingBlock()
        with patch.object(_ThinkingBlock, "_render_content", return_value=None) as mock_render:
            block.set_thinking("final reasoning", in_progress=False)
        self.assertEqual(mock_render.call_count, 1)

    def test_append_reasoning_accumulates_source_text(self):
        """Deltas must accumulate into _source_text even when not rendered."""
        block = _ThinkingBlock()
        block.append_reasoning("hello ")
        block.append_reasoning("world")
        self.assertEqual(block._source_text, "hello world")


if __name__ == "__main__":
    unittest.main()
