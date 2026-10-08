"""Tests for rikugan.ui.markdown — Markdown-to-HTML converter."""

from __future__ import annotations

import unittest

from rikugan.ui.markdown import _inline, _inline_formatting, md_to_html


class TestMdToHtmlEmptyAndNone(unittest.TestCase):
    def test_empty_string_returns_empty(self):
        self.assertEqual(md_to_html(""), "")

    def test_plain_text_passthrough(self):
        result = md_to_html("hello world")
        self.assertIn("hello world", result)


class TestMdToHtmlHeaders(unittest.TestCase):
    def test_h1(self):
        result = md_to_html("# Title")
        self.assertIn("<div", result)
        self.assertIn("Title", result)
        self.assertIn("18px", result)

    def test_h2(self):
        result = md_to_html("## Heading")
        self.assertIn("16px", result)

    def test_h3(self):
        result = md_to_html("### Sub")
        self.assertIn("14px", result)

    def test_h4(self):
        result = md_to_html("#### Small")
        self.assertIn("13px", result)


class TestMdToHtmlHorizontalRule(unittest.TestCase):
    def test_triple_dash(self):
        result = md_to_html("---")
        self.assertIn("<hr", result)

    def test_triple_star(self):
        result = md_to_html("***")
        self.assertIn("<hr", result)

    def test_triple_underscore(self):
        result = md_to_html("___")
        self.assertIn("<hr", result)


class TestMdToHtmlBulletList(unittest.TestCase):
    def test_dash_list(self):
        result = md_to_html("- item one\n- item two")
        self.assertIn("<ul", result)
        self.assertIn("<li>", result)
        self.assertIn("item one", result)
        self.assertIn("item two", result)

    def test_star_list(self):
        result = md_to_html("* alpha\n* beta")
        self.assertIn("<ul", result)
        self.assertIn("alpha", result)


class TestMdToHtmlNumberedList(unittest.TestCase):
    def test_numbered_list_with_period(self):
        result = md_to_html("1. first\n2. second")
        self.assertIn("<ol", result)
        self.assertIn("first", result)
        self.assertIn("second", result)

    def test_numbered_list_with_paren(self):
        result = md_to_html("1) alpha\n2) beta")
        self.assertIn("<ol", result)
        self.assertIn("alpha", result)


class TestMdToHtmlFencedCodeBlock(unittest.TestCase):
    def test_code_block_rendered(self):
        result = md_to_html("```python\nx = 1\n```")
        # Pygments wraps individual tokens in <span> tags, so the literal
        # string "x = 1" may not appear contiguously.  Strip HTML tags to
        # confirm the code text is present and that preformatted styling
        # is applied.
        import re as _re

        text_only = _re.sub(r"<[^>]+>", "", result)
        self.assertIn("x = 1", text_only)
        self.assertIn("white-space:pre", result)

    def test_code_block_lang_not_displayed(self):
        # Regression: the language name from the fence info string
        # (e.g. ``python``) is used internally to pick a Pygments
        # lexer, but must NOT appear in the rendered HTML. Earlier
        # versions prepended a small "python" / "asm" label above
        # the code body which users misread as the first line of code.
        result = md_to_html("```python\ncode\n```")
        self.assertNotIn(">python<", result)
        # The actual code is still rendered.
        self.assertIn("code", result)

    def test_code_block_asm_lang_not_displayed(self):
        # The motivating bug: ```asm\nxor eax, eax\n``` used to
        # show "asm" as the first line of the code block.
        result = md_to_html("```asm\nxor eax, eax\n```")
        self.assertNotIn(">asm<", result)
        self.assertIn("xor", result)

    def test_code_block_without_lang(self):
        result = md_to_html("```\nraw code\n```")
        self.assertIn("raw code", result)

    def test_code_block_escapes_html(self):
        result = md_to_html("```\n<script>alert(1)</script>\n```")
        self.assertNotIn("<script>", result)
        self.assertIn("&lt;script&gt;", result)

    def test_code_block_not_processed_for_inline(self):
        result = md_to_html("```\n**not bold**\n```")
        self.assertNotIn("<b>not bold</b>", result)

    def test_code_block_without_lang_in_list_item(self):
        # Regression: an empty fence info string inside a list item
        # used to crash the nested fence handler with IndexError
        # because the code did ``"".strip().split()[0]`` without
        # guarding against an empty split result.
        result = md_to_html("- item\n\n  ```\n  raw code\n  ```\n")
        self.assertIn("raw code", result)


class TestMdToHtmlFencedCodeBlockEmojiStrip(unittest.TestCase):
    """Regression: code-block content should not contain emoji glyphs
    that render as tofu boxes in monospace fonts.

    The motivating case was an LLM emitting
    ```` ```\\n2️⃣ C2 HTTP Connection Orchestrator (sub_180139180)\\n``` ````
    which rendered as ``[tofu] C2 HTTP Connection Orchestrator (sub_180139180)``
    in IDA's monospace chat font.

    The fix is two-layered: a system-prompt section tells the LLM not to
    decorate code blocks, and the renderer strips emoji codepoints as a
    safety net.  These tests exercise the safety net.
    """

    @staticmethod
    def _strip_tags(html: str) -> str:
        import re as _re

        return _re.sub(r"<[^>]+>", "", html)

    def test_keycap_digit_strips_modifier_only(self):
        # The motivating input.  The keycap-2 emoji is ``2`` + VS-16
        # (FE0F) + combining keycap (20E3).  Stripping the modifiers
        # leaves the digit ``2`` plus the trailing space — the user
        # sees a normal digit instead of a tofu box.
        result = md_to_html("```\n2️⃣ C2 HTTP Connection Orchestrator (sub_180139180)\n```")
        text = self._strip_tags(result)
        # Modifier codepoints gone — VS-16 + combining keycap.
        self.assertNotIn("️", text)  # VS-16
        self.assertNotIn("⃣", text)  # combining keycap
        # Digit preserved (info > decoration).
        self.assertIn("2", text)
        # The function name + address still rendered.
        self.assertIn("C2 HTTP Connection Orchestrator", text)
        self.assertIn("sub_180139180", text)

    def test_pictograph_emoji_removed_entirely(self):
        # Single-codepoint emoji like 🎉 (U+1F389, in the
        # symbols & pictographs range) gets stripped with no
        # alphanumeric residue.
        result = md_to_html("```\n🎉 hello world\n```")
        text = self._strip_tags(result)
        self.assertNotIn("🎉", text)
        # Surrounding content preserved.
        self.assertIn("hello world", text)

    def test_alphanumeric_content_preserved(self):
        # No emoji → output unchanged.  Guard against an over-eager
        # regex that nukes legitimate text.
        result = md_to_html("```\nxor eax, eax\nret\n```")
        text = self._strip_tags(result)
        self.assertIn("xor eax, eax", text)
        self.assertIn("ret", text)

    def test_keycap_in_numbered_list_strips_modifier(self):
        # Regression: ``1️⃣`` in a numbered list item was rendering as
        # tofu (digit + VS-16 + combining keycap where the chat's
        # monospace font has no keycap glyph). The strip is applied at
        # the input of ``md_to_html`` so it covers list items,
        # paragraphs, and headings — not only fenced code blocks.
        result = md_to_html("1. 1️⃣ C2 Configuration Storage\n")
        text = self._strip_tags(result)
        self.assertNotIn("⃣", text)
        self.assertNotIn("️", text)
        self.assertIn("1", text)
        self.assertIn("C2 Configuration Storage", text)

    def test_paragraph_strips_pictograph_emoji(self):
        # Regression: ``🎉`` in a regular paragraph also rendered as
        # tofu in the chat font. Input-level strip removes it from
        # every output path (markdown-it and legacy fallback).
        result = md_to_html("Analysis complete 🎉\n")
        text = self._strip_tags(result)
        self.assertNotIn("🎉", text)
        self.assertIn("Analysis complete", text)

    def test_inline_code_strips_emoji(self):
        # Inline `` `foo` `` spans now also strip emoji: same
        # monospace-font rationale as fenced blocks. Only difference
        # vs. fenced blocks is the wrapping style (inline span vs.
        # block div).
        result = md_to_html("press the `🎉` key")
        text = self._strip_tags(result)
        self.assertNotIn("🎉", text)
        self.assertIn("press the", text)

    def test_legacy_path_strips_emoji(self):
        # The legacy regex fallback in ``_legacy_md_to_html`` must
        # also strip emoji — exercised when markdown-it-py is absent.
        from rikugan.ui.markdown import _legacy_md_to_html

        result = _legacy_md_to_html("```\n2️⃣ hello\n```")
        text = self._strip_tags(result)
        self.assertNotIn("⃣", text)
        self.assertNotIn("️", text)
        self.assertIn("hello", text)

    def test_indented_code_block_strips_emoji(self):
        # Regression: ``markdown-it-py`` emits a ``code_block`` token
        # for 4-space-indented code (separate from the ``fence`` token
        # for ```fenced``` blocks).  Both paths must strip emoji, or
        # the user sees tofu boxes for any indented code the LLM emits
        # (e.g. inside list items, or as a continuation paragraph).
        result = md_to_html("    2️⃣ C2 HTTP Connection Orchestrator (sub_180139180)\n")
        text = self._strip_tags(result)
        self.assertNotIn("⃣", text)
        self.assertNotIn("️", text)
        self.assertIn("C2 HTTP Connection Orchestrator", text)
        self.assertIn("sub_180139180", text)

    def test_fenced_code_block_in_list_item_strips_emoji(self):
        # Fenced code block nested under a list item — exercises the
        # ``_render_fence`` path inside a list_item_open /
        # list_item_close envelope.  Same code path as the standalone
        # fence test, but routed through the list-item renderer.
        result = md_to_html("- item\n\n  ```\n  2️⃣ fenced in list\n  ```\n")
        text = self._strip_tags(result)
        self.assertNotIn("⃣", text)
        self.assertNotIn("️", text)
        self.assertIn("fenced in list", text)


class TestPlainTextFastPath(unittest.TestCase):
    """Phase 6.1 — plain-text fast path skips markdown-it entirely."""

    def test_plain_text_skips_markdown(self):
        """Pure prose without markdown syntax is rendered as escaped HTML."""
        result = md_to_html("This is just plain text.")
        self.assertIn("This is just plain text.", result)
        # No <pre> blocks from a code-fence regex.
        self.assertNotIn("<pre>", result)

    def test_plain_text_escapes_html_chars(self):
        result = md_to_html("if (a < b && c > d)")
        # The "<" and ">" must be escaped to safe entities.
        self.assertIn("&lt;", result)
        self.assertIn("&gt;", result)
        self.assertNotIn("a < b", result)  # raw angle-bracket pair

    def test_markdown_still_uses_full_parser(self):
        """A heading must still be rendered as a heading, not plain text."""
        result = md_to_html("# Real heading")
        # Legacy / markdown-it render headings with a styled div.
        self.assertIn("Real heading", result)
        # The plain-text fast path wraps in a single <div>; the real
        # renderer emits a heading div with explicit font-size style.
        self.assertIn("font-size:", result)


class TestMdToHtmlParagraph(unittest.TestCase):
    def test_multiple_empty_lines_collapsed(self):
        result = md_to_html("a\n\n\n\nb")
        # Three or more consecutive <br> should be collapsed to two
        self.assertNotIn("<br><br><br>", result)

    def test_paragraph_div_does_not_end_with_br(self):
        # Regression: each paragraph ``<div>`` used to carry a trailing
        # ``<br>`` (``<div ...>inner<br></div>``). In Qt rich text a
        # ``<div>`` is already block-level, so the ``<br>`` adds an
        # extra blank line between consecutive paragraphs — visually
        # double-spacing that was reported as "thinking content has a
        # blank line inserted between every two lines". The fix removes
        # the trailing ``<br>``; the block break of ``<div>`` alone
        # produces the correct single inter-paragraph gap.
        result = md_to_html("para one\n\npara two")
        self.assertNotIn(
            "<br></div>",
            result,
            "Paragraph divs must not end with a trailing <br>; the "
            "block-level <div> already provides the inter-paragraph break "
            "in Qt rich text and the <br> causes visible double-spacing.",
        )

    def test_two_paragraphs_render_as_two_separate_divs(self):
        # Sanity: after removing the trailing <br>, the two paragraphs
        # must still render as distinct block-level elements (not merged).
        #
        # Phase 6.1 plain-text fast path: input has no markdown syntax,
        # so the fast path wraps each paragraph in its own ``<div>``.
        # The fast path emits a body-color style instead of the legacy
        # ``margin:2px 0`` style, but the *separation* contract — two
        # distinct block-level elements — is preserved.
        result = md_to_html("para one\n\npara two")
        self.assertEqual(result.count("<div"), 2)
        self.assertIn("para one", result)
        self.assertIn("para two", result)
        # The two paragraphs must not be concatenated into a single block.
        self.assertNotIn("one<br>para two", result)


class TestInlineFormatting(unittest.TestCase):
    def test_bold_double_star(self):
        result = _inline_formatting("**bold**")
        self.assertEqual(result, "<b>bold</b>")

    def test_bold_double_underscore(self):
        result = _inline_formatting("__bold__")
        self.assertEqual(result, "<b>bold</b>")

    def test_italic_single_star(self):
        result = _inline_formatting("*italic*")
        self.assertEqual(result, "<i>italic</i>")

    def test_italic_single_underscore(self):
        result = _inline_formatting("_italic_")
        self.assertEqual(result, "<i>italic</i>")

    def test_link(self):
        result = _inline_formatting("[text](http://example.com)")
        self.assertIn("<a", result)
        self.assertIn("href", result)
        self.assertIn("text", result)
        self.assertIn("http://example.com", result)

    def test_no_spurious_formatting(self):
        result = _inline_formatting("plain text")
        self.assertEqual(result, "plain text")


class TestInlineCodeSpans(unittest.TestCase):
    def test_backtick_code_rendered(self):
        result = _inline("use `foo()` here")
        self.assertIn("<span", result)
        self.assertIn("foo()", result)
        self.assertIn("font-family:monospace", result)

    def test_bold_inside_code_not_applied(self):
        result = _inline("`**not bold**`")
        self.assertNotIn("<b>", result)
        self.assertIn("**not bold**", result)

    def test_html_escaped_in_text(self):
        result = _inline("<b>not bold</b>")
        self.assertNotIn("<b>", result)
        self.assertIn("&lt;b&gt;", result)


class TestMdToHtmlHtmlInjection(unittest.TestCase):
    """Regression: raw HTML must never reach the Qt rich-text engine.

    Rikugan is a reverse-engineering tool where untrusted binary
    content (strings, decompiler output, function names) flows into
    the LLM prompt and back into the assistant's markdown response.
    CLAUDE.md section 3 names binary-as-prompt-injection as a top
    attack surface, so the markdown render boundary must escape — not
    emit verbatim — any raw HTML the model echoes.

    The ``commonmark`` preset of ``markdown-it-py`` defaults to
    ``html: True`` (verified at runtime), which emits ``html_block`` /
    ``html_inline`` tokens whose content the renderer passed through
    untouched. A binary that embeds ``<img src=x onerror=...>`` in a
    string the assistant quotes would render it in the chat QLabel.
    These tests pin the safe behaviour: raw HTML is escaped so the
    user sees the literal markup instead of the browser interpreting it.
    """

    def test_block_level_script_is_escaped(self):
        result = md_to_html("<script>alert(1)</script>")
        self.assertNotIn("<script>", result)
        # The literal text must still be visible to the user.
        self.assertIn("&lt;script&gt;", result)

    def test_inline_img_onerror_is_escaped(self):
        result = md_to_html("hi <img src=x onerror=alert(1)> bye")
        # The attack vector is the ``<img>`` *tag* reaching Qt's rich
        # text engine (which could load the image / fire onerror). Once
        # the tag is escaped to ``&lt;img&gt;`` the rest is inert text,
        # so we assert on the tag boundary, not on the literal word
        # "onerror" (an analyst may legitimately quote that word).
        self.assertNotIn("<img", result)
        self.assertIn("&lt;img", result)

    def test_inline_bold_tag_is_escaped_not_rendered(self):
        # A model echoing real HTML should show as text, not render as
        # bold in the chat bubble.
        result = md_to_html("plain <b>bold</b> text")
        self.assertNotIn("<b>", result)
        self.assertIn("&lt;b&gt;", result)

    def test_javascript_link_not_interpreted(self):
        # The javascript: scheme in a link must not survive as a real
        # href the Qt rich-text engine would honour on click.
        result = md_to_html("[click](javascript:alert(1))")
        self.assertNotIn('href="javascript:', result)


class TestMdToHtmlIntegration(unittest.TestCase):
    def test_mixed_content(self):
        md = "# Title\n\nSome **bold** and `code`.\n\n- item\n- item2"
        result = md_to_html(md)
        self.assertIn("<b>bold</b>", result)
        self.assertIn("<ul", result)
        self.assertIn("Title", result)

    def test_nested_inline_in_header(self):
        result = md_to_html("# **Bold Title**")
        self.assertIn("<b>Bold Title</b>", result)

    def test_link_in_list(self):
        result = md_to_html("- [link](http://x.com)")
        self.assertIn("href", result)
        self.assertIn("<li>", result)


_LEGACY_BODY = "**Report draft**\n\n# Draft\n\n```c\nint main(void) {return 0;}\n```\n\nTrailing line about entry."


class TestLegacyMarkdownFences(unittest.TestCase):
    """The legacy regex-based renderer is exercised when ``markdown-it``
    is not installed.  The renderer must still emit the inner code
    block and the trailing prose in that case.
    """

    def test_legacy_renders_inner_code_block_and_trailing(self):
        from rikugan.ui.markdown import _legacy_md_to_html

        out = _legacy_md_to_html(_LEGACY_BODY)
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


class TestMdToHtmlDispatchFallback(unittest.TestCase):
    """The public entry point must select the markdown-it path when
    available and produce HTML, not the empty string.
    """

    def test_md_to_html_returns_html(self):
        from rikugan.ui.markdown import md_to_html

        out = md_to_html(_LEGACY_BODY)
        # ``md_to_html`` must return a non-empty string.  The exact
        # markup depends on which engine was selected; the body must
        # survive either way.
        self.assertTrue(out)
        self.assertNotIn("\x00", out)


if __name__ == "__main__":
    unittest.main()
