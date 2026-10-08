"""Tests that Pygments highlight output is cached per (code, lang, style).

Perf regression: every theme switch re-ran ``_pygments_highlight`` (Pygments
lex + format — the dominant cost of markdown re-render across ~14 message
widgets) for every fenced code block, even when the code and language were
unchanged. The output HTML has theme-specific colors baked in (noclasses=True
inline styles), so the cache must key on the active style name too — a dark
output and a light output are distinct entries. When the theme flips back,
the previously-rendered output is reused with no re-lex/re-format.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from lucnhan.ui import highlight


class TestHighlightOutputCache(unittest.TestCase):
    def setUp(self):
        # Ensure a clean cache + fresh pygments-imports tuple for every test.
        highlight.clear_formatter_cache()
        self.addCleanup(highlight.clear_formatter_cache)
        if hasattr(highlight, "_OUTPUT_CACHE"):
            highlight._OUTPUT_CACHE.clear()
            self.addCleanup(highlight._OUTPUT_CACHE.clear)

    def _patch_highlight_fn(self):
        """Replace the pygments ``highlight`` callable with a counting mock.

        Keeps the real ``HtmlFormatter`` / ``get_lexer_by_name`` / ``ClassNotFound``
        so the lexer lookup + formatter construction paths stay exercised.
        """
        real = highlight._get_pygments_imports()
        if real is None:
            self.skipTest("pygments not installed in this environment")
        _real_highlight, HtmlFormatter, get_lexer_by_name, ClassNotFound = real
        fake_highlight = MagicMock(return_value="<span>highlighted</span>")
        return patch.object(
            highlight,
            "_pygments_modules",
            (fake_highlight, HtmlFormatter, get_lexer_by_name, ClassNotFound),
        ), fake_highlight

    def test_same_code_lang_style_uses_cache(self):
        patcher, fake_highlight = self._patch_highlight_fn()
        with patcher:
            highlight.highlight_code("x = 1", "python", is_dark=True)
            highlight.highlight_code("x = 1", "python", is_dark=True)
        # Second call must hit the cache — pygments highlight ran exactly once.
        fake_highlight.assert_called_once()

    def test_different_style_re_highlights(self):
        # Dark and light outputs are distinct cache entries (different colors
        # baked into the inline-styled HTML). Flipping to the other style must
        # run Pygments once for the new style.
        patcher, fake_highlight = self._patch_highlight_fn()
        with patcher:
            highlight.highlight_code("x = 1", "python", is_dark=True)
            highlight.highlight_code("x = 1", "python", is_dark=False)
        self.assertEqual(fake_highlight.call_count, 2)

    def test_different_code_distinct_entries(self):
        patcher, fake_highlight = self._patch_highlight_fn()
        with patcher:
            highlight.highlight_code("x = 1", "python", is_dark=True)
            highlight.highlight_code("y = 2", "python", is_dark=True)
        self.assertEqual(fake_highlight.call_count, 2)

    def test_cache_returns_identical_html(self):
        patcher = self._patch_highlight_fn()[0]
        with patcher:
            first = highlight.highlight_code("x = 1", "python", is_dark=True)
            second = highlight.highlight_code("x = 1", "python", is_dark=True)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
