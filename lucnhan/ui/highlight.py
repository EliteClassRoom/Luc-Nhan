"""Pygments-based syntax highlighting for fenced code blocks.

Gracefully degrades when Pygments is not installed.
Output targets Qt RichText compatible HTML (inline styles only).

Performance note
----------------
Pygments is a heavy import (~10ms cold on CPython 3.13, more on first
highlight because lexers are lazy). We probe with
:func:`importlib.util.find_spec` (cheap, no code execution) and only
import the real modules on first call to :func:`highlight_code`.
"""

from __future__ import annotations

import html as _html
import importlib.util as _importlib_util

# Cheap probe: tells us pygments is on sys.path without executing its
# top-level code (which loads the entire lexer/formatter plugin tree).
_HAS_PYGMENTS = _importlib_util.find_spec("pygments") is not None

# Lazy-resolved on first call to ``_get_pygments_imports()``. ``None``
# means "not yet attempted"; the lookup caches the result.
_pygments_modules: tuple | None = None
_pygments_failed = False


def _get_pygments_imports() -> tuple | None:
    """Lazily import pygments modules on first use.

    Returns ``(highlight, HtmlFormatter, get_lexer_by_name, ClassNotFound)``
    on success, ``None`` if pygments is unavailable or has been seen to
    fail.  Cached after the first call.
    """
    global _pygments_modules, _pygments_failed
    if _pygments_modules is not None:
        return _pygments_modules
    if _pygments_failed or not _HAS_PYGMENTS:
        return None
    try:
        from pygments import highlight as _pygments_highlight
        from pygments.formatters import HtmlFormatter
        from pygments.lexers import get_lexer_by_name
        from pygments.util import ClassNotFound

        _pygments_modules = (_pygments_highlight, HtmlFormatter, get_lexer_by_name, ClassNotFound)
        return _pygments_modules
    except ImportError:
        _pygments_failed = True
        return None


# Cached formatters per style name (lazy singletons)
_formatter_cache: dict[str, object | None] = {}

# Cached highlight output keyed by (code, language, style_name).
#
# Pygments' lex + format is the dominant cost of markdown re-render when the
# user switches theme (~14 message widgets each re-running md_to_html). The
# output HTML has theme-specific colors baked in (noclasses=True inline
# styles), so the cache key includes the style name — a dark output and a
# light output are distinct entries. Flipping the theme back reuses the
# previously-rendered output with no re-lex/re-format. Bounded to 256 entries
# so it cannot grow without limit across a long session.
_OUTPUT_CACHE: dict[tuple[str, str, str], str] = {}
_OUTPUT_CACHE_MAX = 256


def _get_formatter(style_name: str) -> object | None:
    """Get (or create) a pygments HtmlFormatter for the given style.

    Cache is keyed by style name. Invalidate on theme change via
    ``clear_formatter_cache()``. Returns ``None`` when pygments is not
    installed so callers can degrade gracefully.
    """
    imports = _get_pygments_imports()
    if imports is None:
        return None
    _, HtmlFormatter, _, _ = imports
    if style_name not in _formatter_cache:
        try:
            _formatter_cache[style_name] = HtmlFormatter(
                style=style_name,
                nowrap=True,
                noclasses=True,
                nobackground=True,
            )
        except Exception:
            # Bad style name or pygments internal error — degrade to None
            # so highlight_code can fall back to plain text.
            _formatter_cache[style_name] = None
    return _formatter_cache[style_name]


def clear_formatter_cache() -> None:
    """Clear the pygments formatter cache. Call on theme change."""
    _formatter_cache.clear()


def highlight_code(code: str, language: str, is_dark: bool | None = None) -> str:
    """Highlight *code* in *language* using Pygments.

    Returns HTML with inline styles suitable for Qt RichText.
    Falls back to HTML-escaped plain text when Pygments is absent
    or the language is unknown.

    ``is_dark`` is optional — when omitted, the active theme's tokens
    drive the style choice (this is the recommended path; passing
    ``is_dark`` directly is kept for backward compatibility).
    """
    imports = _get_pygments_imports()
    if imports is None or not language:
        return _plain_code(code)
    _pygments_highlight, _, get_lexer_by_name, ClassNotFound = imports

    if is_dark is None:
        # Defer the import to avoid a hard dependency from this module
        # on the theme manager at import time.
        from .theme.manager import ThemeManager
        from .theme.tokens import is_dark_tokens

        is_dark = is_dark_tokens(ThemeManager.instance().tokens())

    style_name = "monokai" if is_dark else "default"

    # Output cache hit: reuse a previously-rendered result for this exact
    # (code, language, style) triple. The key includes the style name so dark
    # and light outputs live in separate entries; flipping the theme back
    # reuses the earlier render with no re-lex/re-format.
    cache_key = (code, language, style_name)
    cached = _OUTPUT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    formatter = _get_formatter(style_name)
    if formatter is None:
        return _plain_code(code)

    try:
        lexer = get_lexer_by_name(language)
    except ClassNotFound:
        # Try common aliases for RE context
        alias_map = {
            "asm": "nasm",
            "x86": "nasm",
            "arm": "asm",
            "objective-c": "objc",
            "shell": "bash",
            "conf": "ini",
        }
        mapped = alias_map.get(language.lower())
        if mapped:
            try:
                lexer = get_lexer_by_name(mapped)
            except ClassNotFound:
                return _plain_code(code)
        else:
            return _plain_code(code)

    highlighted = _pygments_highlight(code, lexer, formatter)
    # Bounded insert: drop the oldest half when the cap is reached so the
    # cache cannot grow without limit across a long session.
    if len(_OUTPUT_CACHE) >= _OUTPUT_CACHE_MAX:
        for _k in list(_OUTPUT_CACHE.keys())[: _OUTPUT_CACHE_MAX // 2]:
            _OUTPUT_CACHE.pop(_k, None)
    _OUTPUT_CACHE[cache_key] = highlighted
    return highlighted


def _plain_code(code: str) -> str:
    """Return HTML-escaped code for fallback rendering."""
    return _html.escape(code)


# === Theme change subscription ===
#
# Subscribe to ThemeManager.themeChanged at module load time. The signal
# payload is ignored — we only care that the cache is invalidated when
# the user (or IDAThemeWatcher) switches themes. Re-subscribing on each
# import is safe: highlight.py is imported at most once per process, and
# the ThemeManager singleton is reset between tests via ThemeManager.reset()
# which drops the prior manager instance. Any new instance() call returns
# a fresh QObject with no connections, so the module-level connect runs
# against whichever manager is alive at import time. If highlight.py is
# re-imported under a fresh manager (e.g. after reset() in a test
# setUp), the connect runs again against the new instance.


def _on_theme_changed(_tokens) -> None:
    clear_formatter_cache()


try:
    from .theme.manager import ThemeManager

    ThemeManager.instance().themeChanged.connect(_on_theme_changed)
except Exception:
    # Defensive: if the theme manager cannot be imported here (e.g. a
    # partial install, a missing PySide6, or a circular import in an
    # unusual embedding), the cache simply won't auto-invalidate. The
    # manual ``clear_formatter_cache()`` path still works.
    pass
