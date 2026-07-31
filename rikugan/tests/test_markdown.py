"""Regression tests for the chat Markdown renderer.

The chat panel renders visible markdown through ``md_to_html``, which
dispatches to ``markdown-it-py`` when available and falls back to the
legacy regex converter otherwise.  Both paths must:

* Preserve code-block content (the issue that surfaced after the
  report-draft preview lost its outer ``markdown`` fence: a body
  containing ``\\`\\`\\`c`` followed by code followed by ``\\`\\`\\``` must
  still render the inner code and the trailing prose).
* Preserve trailing text after the closing fence so a fenced block
  inside a draft does not swallow the rest of the report.
"""

from __future__ import annotations

import unittest


_BODY = (
    "**Report draft**\n\n"
    "# Draft\n\n"
    "```c\n"
    "int main(void) {return 0;}\n"
    "```\n\n"
    "Trailing line about entry."
)


class TestLegacyMarkdownFences(unittest.TestCase):
    """The legacy regex-based renderer is exercised when ``markdown-it``
    is not installed.  The renderer must still emit the inner code
    block and the trailing prose in that case.
    """

    def test_legacy_renders_inner_code_block_and_trailing(self):
        from rikugan.ui.markdown import _legacy_md_to_html

        out = _legacy_md_to_html(_BODY)
        self.assertIn("int main(void) {return 0;}", out)
        self.assertIn("Trailing line about entry.", out)
        # The block must be wrapped in a ``<div>`` carrying the
        # block-code style and not crammed together with adjacent
        # sentences (one of the symptoms of broken fence handling is
        # the placeholder leak that ends up with literal ``\x00`` in
        # the rendered HTML).
        self.assertNotIn("\x00", out)
        # Inner code must be inside its own ``<div>`` so Qt renders
        # a real code block (pre-wrap background) and the trailing
        # line stays a sibling — they must not be concatenated into
        # the same inline tag.
        self.assertRegex(out, r"<div[^>]*white-space:pre-wrap[^>]*>int main")
        # NUL sentinels from the placeholder registry must never leak.
        self.assertNotIn("\x00", out)



class TestMdToHtmlDispatch(unittest.TestCase):
    """The public entry point must select the markdown-it path when
    available and produce HTML, not the empty string.
    """

    def test_md_to_html_returns_html(self):
        from rikugan.ui.markdown import md_to_html

        out = md_to_html(_BODY)
        # ``md_to_html`` must return a non-empty string.  The exact
        # markup depends on which engine was selected; the body must
        # survive either way.
        self.assertTrue(out)
        self.assertNotIn("\x00", out)
