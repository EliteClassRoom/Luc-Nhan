"""markdown-it-py renderer that emits Qt-RichText-compatible HTML.

This module pairs with :mod:`lucnhan.ui.markdown`. The renderer
walks the CommonMark token tree produced by ``markdown-it-py`` and
emits HTML with *inline* styles only — ``<span style="...">`` /
``<div style="...">``. Qt's rich-text engine is a strict subset
of HTML 4 / CSS 1, so we avoid classes (no ``<style>`` block, no
external CSS) and skip ``<thead>``/``<tbody>`` because Qt drops
them silently.

Public surface used by :mod:`lucnhan.ui.markdown`:

* :func:`_build_theme_styles` — returns a dict of inline-style
  fragments keyed by element.  ``source`` is an optional IDA source
  object (kept for parity with the legacy path; unused here).
* :class:`QtRenderer` — instantiable renderer with a single
  method, :meth:`QtRenderer.render_with_styles`.  The signature is
  ``render_with_styles(tokens, options, env, styles)`` so the
  caller can pass in a precomputed style map; the upstream
  default ``render(tokens, options, env)`` is provided too for
  parity with markdown-it conventions.
"""

from __future__ import annotations

import html as _html
import re as _re
import urllib.parse as _urlparse
from typing import Any

# Codepoints that turn into tofu boxes in monospace fonts (the default
# code-block font).  Covers:
#   - U+20E3 combining enclosing keycap
#   - U+FE0F variation selector-16 (forces emoji presentation)
#   - U+200D zero-width joiner (joins multi-codepoint emoji)
#   - U+1F1E6..U+1F1FF regional indicator symbols (flags)
#   - U+2600..U+27BF misc symbols + dingbats
#   - U+1F300..U+1F5FF symbols & pictographs (😀-🗿)
#   - U+1F600..U+1F64F emoticons
#   - U+1F680..U+1F6FF transport & map
#   - U+1F900..U+1F9FF supplemental symbols & pictographs
#   - U+1FA00..U+1FAFF symbols & pictographs extended-A
# The keycap-digit sequence ``2️⃣`` reduces to ``2`` (we
# strip the modifiers, not the digit), while ``🎉`` is removed
# entirely.  Alphanumerics, punctuation and whitespace are preserved.
_EMOJI_RE = _re.compile(
    "["
    "\u20e3"
    "\ufe0f"
    "\u200d"
    "\U0001f1e6-\U0001f1ff"
    "☀-➿"
    "\U0001f300-\U0001f5ff"
    "\U0001f600-\U0001f64f"
    "\U0001f680-\U0001f6ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001faff"
    "]"
)


def _strip_emoji(s: str) -> str:
    """Remove emoji codepoints that render as tofu in monospace fonts.

    Code blocks use a monospace font (Pygments runs against the same
    monospace family in Qt).  When the active font lacks a glyph for a
    keycap / pictograph codepoint, Qt falls back to ``.notdef`` —
    visually a small square ("ô vuông").  The LLM routinely emits
    emoji-decorated code blocks (e.g. ``2️⃣ func_name``)
    when reformatting decompiler output; the renderer strips those
    decorations so the user sees clean, copy-pasteable code.
    """
    return _EMOJI_RE.sub("", s)


def _highlight(code: str, language: str) -> str:
    """Highlight *code* with *language* via the Pygments bridge.

    Imported lazily so this module does not require Pygments at
    import time.  Returns inline-styled HTML, or plain escaped
    text when Pygments is unavailable, the lexer cannot be
    resolved, or the highlighter raises unexpectedly.  Failing
    closed to escaped text is the contract callers rely on so
    that a broken Pygments install can never poison the rendered
    chat with raw markup.
    """
    if not language:
        return _html.escape(code)
    try:
        from .highlight import highlight_code

        return highlight_code(code, language)
    except Exception:
        # Fail closed: never let a broken highlighter surface raw
        # or partially-rendered code to Qt rich text.  ``ui/highlight``
        # already swallows ``ClassNotFound`` and the lazy ``ImportError``
        # for missing Pygments; this catch is the final safety net
        # for any unexpected ``Exception`` raised inside the
        # formatter or lexer internals.
        return _html.escape(code)


# ---------------------------------------------------------------------------
# Safe link handling
# ---------------------------------------------------------------------------

# URL schemes that may be rendered as ``href`` attributes.  Qt rich text
# only follows ``http(s)`` / ``mailto`` reliably; everything else
# (``file:``, ``data:``, ``qrc:``, custom handlers, javascript-style
# pseudo-protocols) is dropped to an empty href so the link is rendered
# as text.  Relative URLs (empty scheme) are kept.
_ALLOWED_LINK_SCHEMES = frozenset({"", "http", "https", "mailto"})


def _safe_href(href: str) -> str:
    """Return *href* only when its URL scheme is safe for Qt rich text.

    Untrusted markdown (binary strings, LLM output) can contain links
    that trigger file reads, data-URI rendering, or custom protocol
    handlers.  We allowlist ``http`` / ``https`` / ``mailto`` plus
    empty-scheme (relative) URLs and reject everything else by
    returning an empty string — the link still renders as anchor
    text, but it has no actionable ``href``.
    """
    if not href:
        return ""
    scheme = _urlparse.urlparse(href).scheme.lower()
    if scheme not in _ALLOWED_LINK_SCHEMES:
        return ""
    return href


# ---------------------------------------------------------------------------
# Theme style builder
# ---------------------------------------------------------------------------


def _build_theme_styles(source: Any = None) -> dict[str, str]:
    """Return a dict of inline-style fragments for the active theme.

    The keys are stable identifiers used by :class:`QtRenderer` to
    pick the right CSS for each token.  ``source`` is accepted for
    API parity with the legacy converter but is otherwise ignored —
    the active palette is owned by :class:`ThemeManager`.
    """
    del source  # unused; kept for API parity
    from .styles import is_host_theme
    from .theme.manager import ThemeManager, blend_hex

    if is_host_theme():
        # Minimal host-friendly defaults — Qt picks up the host's
        # QPalette for body text in most cases, so we just need
        # enough CSS for code spans and links.
        return {
            "inline_code_style": "font-family:monospace;",
            "block_code_style": ("font-family:monospace; white-space:pre-wrap; padding:6px; border-radius:3px;"),
            "link_style": "text-decoration: underline;",
            "hr_style": "",
            "heading_style": "font-weight:bold;",
            "blockquote_style": ("border-left:3px solid #888; padding-left:8px; margin:4px 0; color:#666;"),
            "table_style": "border-collapse:collapse;",
            "th_style": ("border:1px solid #888; padding:4px 6px; font-weight:bold; text-align:left;"),
            "td_style": "border:1px solid #888; padding:4px 6px;",
            "strikethrough_style": "text-decoration:line-through;",
        }

    tokens = ThemeManager.instance().tokens()
    code_bg = blend_hex(tokens.base, tokens.window, 0.15)
    inline_fg = blend_hex(tokens.highlight, tokens.text, 0.3)
    border = blend_hex(tokens.mid, tokens.window, 0.35)
    heading = blend_hex(tokens.highlight, tokens.text, 0.15)
    muted_text = blend_hex(tokens.text, tokens.window, 0.45)

    return {
        "inline_code_style": (
            f"background-color:{code_bg}; color:{inline_fg}; "
            "padding:1px 4px; border-radius:3px; "
            "font-family:monospace; font-size:12px;"
        ),
        "block_code_style": (
            f"background-color:{tokens.base}; color:{tokens.text}; "
            f"border:1px solid {border}; border-radius:4px; "
            "padding:8px; font-family:monospace; font-size:12px; "
            "white-space:pre-wrap; word-break:break-all;"
        ),
        "link_style": f"color:{tokens.highlight};",
        "hr_style": f"border:1px solid {border};",
        "heading_style": f"color:{heading}; font-weight:bold;",
        "blockquote_style": (f"border-left:3px solid {border}; padding-left:8px; margin:4px 0; color:{muted_text};"),
        "table_style": "border-collapse:collapse;",
        "th_style": (
            f"border:1px solid {border}; padding:4px 6px; font-weight:bold; text-align:left; color:{heading};"
        ),
        "td_style": f"border:1px solid {border}; padding:4px 6px;",
        "strikethrough_style": "text-decoration:line-through;",
    }


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

# Shared inline style for bullet and ordered lists.  Repeated four
# times across the renderer (top-level + list-item body, bullet +
# ordered) before this refactor; the constant makes future tweaks
# touch one line.
_LIST_STYLE = "margin:2px 0 2px 16px;"


def _attr(tok: Any, name: str, default: str = "") -> str:
    """Read ``tok.attrs[name]`` with a safe default.

    markdown-it exposes ``attrs`` as either a list of pairs
    (``[["href", "..."], ...]``) or, in newer versions, a dict.
    This helper accepts both shapes.
    """
    attrs = getattr(tok, "attrs", None)
    if attrs is None:
        return default
    if isinstance(attrs, dict):
        return attrs.get(name, default) or default
    # list-of-pairs
    for k, v in attrs:
        if k == name:
            return v or default
    return default


class QtRenderer:
    """Render a markdown-it token list to Qt-compatible HTML.

    ``md`` is the :class:`markdown_it.MarkdownIt` instance.  We do
    not use ``md.renderer`` — the upstream default is HTML with
    external CSS, which Qt cannot consume.  The instance is kept
    around so future hooks (e.g. custom token rules) can introspect
    it.
    """

    def __init__(self, md: Any) -> None:
        self.md = md
        # Active style map (set by ``render_with_styles`` so the
        # recursive helpers can reach it without threading the
        # argument through every call).
        self._styles: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Style helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _escape_style(style: str) -> str:
        """Escape a CSS fragment for inclusion in a ``style="..."`` attribute.

        Always uses ``quote=True`` so the returned string is safe
        to drop between double quotes — the previous open-coded
        calls repeated this flag and were easy to forget on new
        branches.
        """
        return _html.escape(style, quote=True)

    @classmethod
    def _styled(cls, tag: str, style: str, inner: str) -> str:
        """Wrap *inner* in a ``<tag style="...">...</tag>`` element."""
        return f'<{tag} style="{cls._escape_style(style)}">{inner}</{tag}>'

    @staticmethod
    def _heading_level(tag: str) -> int:
        """Return a safe heading level (1-6) for *tag*.

        ``markdown-it`` tags are exactly ``h1``..``h6`` for valid
        CommonMark.  Anything else (a malformed token, a custom
        plugin, an empty string) falls back to level 3 so the
        renderer never crashes and the heading still appears.
        """
        return {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}.get(tag, 3)

    @staticmethod
    def _inline_children_after(tokens: list[Any], start: int) -> list[Any]:
        """Return the children of the ``inline`` token at ``start + 1``.

        Returns an empty list when the slice is too short, when the
        next token is not ``inline``, or when the inline token has
        no children.  This protects the open/inline/close triplet
        pattern from malformed or truncated token streams.
        """
        next_index = start + 1
        if next_index >= len(tokens):
            return []
        inline_tok = tokens[next_index]
        if getattr(inline_tok, "type", "") != "inline":
            return []
        return inline_tok.children or []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def render(
        self,
        tokens: list[Any],
        options: Any,
        env: Any,
    ) -> str:
        """Default markdown-it entry point — falls back to
        computing styles on the fly from the active theme.
        """
        styles = _build_theme_styles()
        return self.render_with_styles(tokens, options, env, styles)

    def render_with_styles(
        self,
        tokens: list[Any],
        options: Any,
        env: Any,
        styles: dict[str, str],
    ) -> str:
        """Render *tokens* to HTML using *styles* for inline CSS."""
        # ``options`` and ``env`` mirror the upstream markdown-it
        # renderer signature; we drive rendering off the token
        # list, so they are intentionally unused here.  The
        # single-underscore binding (only inside this frame, no
        # assignment to a module-level name) keeps static analyzers
        # happy without removing the public parameters that
        # ``ui/markdown.py`` and any third-party callers rely on.
        _ = options, env
        self._styles = styles
        out: list[str] = []
        i = 0
        n = len(tokens)
        while i < n:
            tok = tokens[i]
            ttype = tok.type
            if ttype == "heading_open":
                level = self._heading_level(getattr(tok, "tag", ""))
                # Spec: heading_open, inline, heading_close.  The
                # inline-child helper returns [] on a malformed
                # slice so the heading degrades gracefully to an
                # empty content instead of crashing.
                inner = self._render_inline(self._inline_children_after(tokens, i))
                size = {1: 18, 2: 16, 3: 14, 4: 13, 5: 12, 6: 11}.get(level, 13)
                heading_style = f"{styles['heading_style']}font-size:{size}px;margin:6px 0 2px 0;"
                out.append(self._styled("div", heading_style, inner))
                i += 3
                continue
            if ttype == "paragraph_open":
                inner = self._render_inline(self._inline_children_after(tokens, i))
                # No trailing ``<br>``: in Qt rich text a ``<div>`` is
                # already block-level, so the closing tag alone produces
                # a single inter-paragraph gap. A trailing ``<br>``
                # previously added an *extra* blank line between
                # consecutive paragraphs, which surfaced as visible
                # double-spacing in the thinking block and assistant
                # bubble (the ``margin:2px 0`` already sizes the gap).
                out.append(f'<div style="margin:2px 0;">{inner}</div>')
                i += 3  # paragraph_open, inline, paragraph_close
                continue
            if ttype == "bullet_list_open":
                html, i = self._render_list_block(tokens, i, ordered=False)
                out.append(html)
                continue
            if ttype == "ordered_list_open":
                html, i = self._render_list_block(tokens, i, ordered=True)
                out.append(html)
                continue
            if ttype == "blockquote_open":
                end = self._find_close(tokens, i, "blockquote_open", "blockquote_close")
                inner = self._render_blocks(tokens[i + 1 : end])
                out.append(self._styled("div", styles["blockquote_style"], inner))
                i = end + 1
                continue
            if ttype == "fence":
                out.append(self._render_fence(tok))
                i += 1
                continue
            if ttype == "code_block":
                # Indented code block.  markdown-it-py emits 4-space-
                # indented code as a ``code_block`` token (distinct
                # from the ``fence`` token for ```` ``` ```` blocks).
                # Without the strip+escape, LLM output like
                # ``    2️⃣ func_name`` inside a list item or as a
                # continuation paragraph renders as a tofu box in the
                # monospace chat font.
                out.append(self._render_code_block(tok.content))
                i += 1
                continue
            if ttype == "hr":
                hr_style = styles.get("hr_style", "")
                style_attr = f' style="{self._escape_style(hr_style)}"' if hr_style else ""
                out.append(f"<hr{style_attr}>")
                i += 1
                continue
            if ttype == "html_block":
                # Defensive: the parser is configured with
                # ``html: False`` (see lucnhan/ui/markdown.py) so raw
                # HTML never reaches this branch — it is emitted as
                # escaped text via the CommonMark path instead. If a
                # future change re-enables HTML, escape here rather
                # than passing through verbatim, since binary content
                # is an injection vector.
                out.append(_html.escape(tok.content))
                i += 1
                continue
            if ttype == "table_open":
                end = self._find_close(tokens, i, "table_open", "table_close")
                inner = self._render_table(tokens[i + 1 : end])
                out.append(
                    f'<table style="{self._escape_style(styles["table_style"])}" '
                    f'border="0" cellpadding="0" cellspacing="0">{inner}</table>'
                )
                i = end + 1
                continue
            # Defensive fallback: skip unknown block tokens but
            # render any inline payload they carry.
            if getattr(tok, "children", None):
                out.append(self._render_inline(tok.children))
            i += 1
        return "".join(out)

    # ------------------------------------------------------------------
    # Block-level helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_close(
        tokens: list[Any],
        start: int,
        open_type: str,
        close_type: str,
    ) -> int:
        depth = 0
        for j in range(start, len(tokens)):
            if tokens[j].type == open_type:
                depth += 1
            elif tokens[j].type == close_type:
                depth -= 1
                if depth == 0:
                    return j
        # Unbalanced; return a safe index so callers advance and
        # do not loop forever.  An empty slice would otherwise
        # produce ``-1`` and re-feed the open token into the
        # caller's loop indefinitely.
        if not tokens:
            return start
        return max(start, len(tokens) - 1)

    def _render_blocks(self, tokens: list[Any]) -> str:
        """Render a sub-slice of block-level tokens.

        Used for blockquote bodies and similar nested contexts.
        """
        # Reuse the public entry point with the current styles.
        return self.render_with_styles(tokens, None, {}, self._styles)

    def _render_list_block(
        self,
        tokens: list[Any],
        start: int,
        ordered: bool,
    ) -> tuple[str, int]:
        """Render a bullet or ordered list starting at *start*.

        Returns ``(html, next_index)``.  ``next_index`` is the
        index immediately after the matching ``*_close`` token so
        the caller's main loop can advance in one step.
        """
        if ordered:
            open_type = "ordered_list_open"
            close_type = "ordered_list_close"
            tag = "ol"
        else:
            open_type = "bullet_list_open"
            close_type = "bullet_list_close"
            tag = "ul"
        end = self._find_close(tokens, start, open_type, close_type)
        inner = self._render_list_items(tokens[start + 1 : end])
        return f"<{tag} style='{_LIST_STYLE}'>{inner}</{tag}>", end + 1

    def _render_code_block(self, content: str, info: str = "") -> str:
        """Render a fenced or indented code block.

        ``info`` is the language hint from the ``fence`` token
        (``tok.info``).  Indented ``code_block`` tokens have no
        language hint and fall back to escaped plain text.  The
        body is passed through :func:`_strip_emoji` so keycap /
        pictograph codepoints don't render as tofu in the
        monospace font.  Highlighted output goes through
        :func:`_highlight`, which itself fails closed to escaped
        text when Pygments is unavailable.
        """
        lang = ""
        info = info.strip()
        if info:
            lang = info.split()[0]
        code = _strip_emoji(content)
        body = _highlight(code, lang) if lang else _html.escape(code)
        return self._styled("div", self._styles["block_code_style"], body)

    def _render_fence(self, tok: Any) -> str:
        """Render a fenced code block token to Qt-compatible HTML.

        Centralised so the top-level and nested-in-list-item
        call sites stay in lock-step.  ``tok.info`` may be
        ``None`` or an empty string (e.g. for ```` ``` ```` with
        no language) — in that case we skip Pygments highlighting.
        The active style map is read from ``self._styles``.
        """
        return self._render_code_block(tok.content, tok.info or "")

    def _render_list_items(self, tokens: list[Any]) -> str:
        out: list[str] = []
        i = 0
        n = len(tokens)
        while i < n:
            tok = tokens[i]
            if tok.type == "list_item_open":
                end = self._find_close(tokens, i, "list_item_open", "list_item_close")
                inner = self._render_list_item_body(tokens[i + 1 : end])
                out.append(f"<li>{inner}</li>")
                i = end + 1
                continue
            if getattr(tok, "children", None):
                out.append(self._render_inline(tok.children))
            i += 1
        return "".join(out)

    def _render_list_item_body(self, tokens: list[Any]) -> str:
        """Render the body of a ``<li>`` (may contain paragraphs,
        nested lists, code blocks, etc.)."""
        out: list[str] = []
        i = 0
        n = len(tokens)
        while i < n:
            tok = tokens[i]
            ttype = tok.type
            if ttype == "paragraph_open":
                inner = self._render_inline(self._inline_children_after(tokens, i))
                out.append(inner)
                i += 3  # paragraph_open, inline, paragraph_close
                continue
            if ttype == "bullet_list_open":
                html, i = self._render_list_block(tokens, i, ordered=False)
                out.append(html)
                continue
            if ttype == "ordered_list_open":
                html, i = self._render_list_block(tokens, i, ordered=True)
                out.append(html)
                continue
            if ttype == "fence":
                out.append(self._render_fence(tok))
                i += 1
                continue
            if ttype == "code_block":
                # Indented code inside a list item was previously
                # silently dropped; route it through the same shared
                # code-block renderer as the fenced and top-level
                # paths so the output is consistent and the
                # emoji-strip still runs.
                out.append(self._render_code_block(tok.content))
                i += 1
                continue
            if getattr(tok, "children", None):
                out.append(self._render_inline(tok.children))
            i += 1
        return "".join(out)

    def _render_table(self, tokens: list[Any]) -> str:
        """Render the inner tokens of a table block.

        Qt's rich text engine is happy with ``<tr>`` / ``<th>`` /
        ``<td>`` but does not honour ``<thead>`` / ``<tbody>``.
        We collapse both into a single row stream.
        """
        styles = self._styles
        out: list[str] = []
        i = 0
        n = len(tokens)
        while i < n:
            tok = tokens[i]
            ttype = tok.type
            if ttype == "tr_open":
                out.append("<tr>")
                i += 1
                continue
            if ttype == "tr_close":
                out.append("</tr>")
                i += 1
                continue
            if ttype == "th_open":
                inner = self._render_inline(self._inline_children_after(tokens, i))
                out.append(self._styled("th", styles["th_style"], inner))
                i += 3  # th_open, inline, th_close
                continue
            if ttype == "td_open":
                inner = self._render_inline(self._inline_children_after(tokens, i))
                out.append(self._styled("td", styles["td_style"], inner))
                i += 3  # td_open, inline, td_close
                continue
            # thead_open / tbody_open / etc — Qt drops these, skip.
            i += 1
        return "".join(out)

    # ------------------------------------------------------------------
    # Inline rendering — state-machine over ``inline`` children
    # ------------------------------------------------------------------

    _EM_OPEN = "em_open"
    _EM_CLOSE = "em_close"
    _STRONG_OPEN = "strong_open"
    _STRONG_CLOSE = "strong_close"
    _S_OPEN = "s_open"
    _S_CLOSE = "s_close"
    _LINK_OPEN = "link_open"
    _LINK_CLOSE = "link_close"
    _IMAGE = "image"
    _TEXT = "text"
    _CODE_INLINE = "code_inline"
    _SOFTBREAK = "softbreak"
    _HARDBREAK = "hardbreak"
    _HTML_INLINE = "html_inline"

    def _render_inline(self, children: list[Any]) -> str:
        """Render an ``inline`` token's children to HTML.

        ``markdown-it`` emits paired ``_open`` / ``_close`` tokens
        for emphasis, strong, strikethrough and links.  We walk
        the children linearly and track an *open-tag stack* so the
        matching closer does not re-emit the inner content.
        """
        styles = self._styles
        out: list[str] = []
        i = 0
        n = len(children)
        # Each entry is the *raw opener tag* (e.g. ``<i>``,
        # ``<b>``, ``<a ...>``).  The matching close token causes
        # us to pop the entry and emit the corresponding closing
        # tag.
        stack: list[str] = []
        while i < n:
            tok = children[i]
            ttype = tok.type
            if ttype == self._TEXT:
                out.append(_html.escape(tok.content))
            elif ttype == self._CODE_INLINE:
                out.append(self._styled("span", styles["inline_code_style"], _html.escape(tok.content)))
            elif ttype == self._SOFTBREAK:
                out.append(" ")
            elif ttype == self._HARDBREAK:
                out.append("<br>")
            elif ttype == self._EM_OPEN:
                out.append("<i>")
                stack.append("</i>")
            elif ttype == self._EM_CLOSE:
                if stack and stack[-1] == "</i>":
                    out.append(stack.pop())
            elif ttype == self._STRONG_OPEN:
                out.append("<b>")
                stack.append("</b>")
            elif ttype == self._STRONG_CLOSE:
                if stack and stack[-1] == "</b>":
                    out.append(stack.pop())
            elif ttype == self._S_OPEN:
                # Qt does not render ``<s>`` reliably; use a
                # styled span.
                out.append(f'<span style="{self._escape_style(styles["strikethrough_style"])}">')
                stack.append("</span>")
            elif ttype == self._S_CLOSE:
                if stack and stack[-1] == "</span>":
                    out.append(stack.pop())
            elif ttype == self._LINK_OPEN:
                href = _safe_href(_attr(tok, "href"))
                title = _attr(tok, "title")
                title_attr = f' title="{_html.escape(title, quote=True)}"' if title else ""
                opener = (
                    f'<a style="{self._escape_style(styles["link_style"])}" '
                    f'href="{_html.escape(href, quote=True)}"{title_attr}>'
                )
                out.append(opener)
                stack.append("</a>")
            elif ttype == self._LINK_CLOSE:
                if stack and stack[-1] == "</a>":
                    out.append(stack.pop())
            elif ttype == self._IMAGE:
                # Images are not supported by Qt rich text — fall
                # back to a textual alt string so the user still
                # sees the intent.
                src = _attr(tok, "src")
                alt = tok.content
                out.append(f"[image: {_html.escape(alt or src)}]")
            elif ttype == self._HTML_INLINE:
                # Defensive: parser config disables raw HTML, but escape
                # inline HTML too if a future plugin or parser option
                # emits it.  Untrusted strings flowing through the
                # LLM and back into assistant output must never reach
                # the Qt rich-text engine as raw markup.
                out.append(_html.escape(tok.content))
            else:
                # Unknown inline token — render its content as
                # text so we never drop information.
                if tok.content:
                    out.append(_html.escape(tok.content))
            i += 1
        # If the stack is non-empty (malformed input), close
        # everything cleanly so the produced HTML stays balanced.
        for close_tag in reversed(stack):
            out.append(close_tag)
        return "".join(out)
