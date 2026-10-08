"""Mutation-log + tool-approval widget style builders (token-driven).

Each builder renders QSS from the live :class:`ThemeTokens` resolved via
:class:`ThemeManager`. Public getter signatures are unchanged from the
legacy ``{dark, light}`` dict version.

Contrast fixes carried over from the audit:
- ``TOOL_APPROVAL_DISABLED_BTN_STYLE`` previously used ``#1a5c2d`` bg +
  ``#808080``/``#92898a`` text (1.87-2.36:1). Disabled state now uses
  reduced-opacity fill + muted_text, conveying disabled via both color
  and luminance (not color alone).
"""

from __future__ import annotations


def _tokens():
    """Return the live ThemeTokens (lazy import to avoid a cycle)."""
    from .manager import ThemeManager

    return ThemeManager.instance().tokens()


def _branch() -> str:
    """Return ``'dark'`` or ``'light'`` for the active effective theme."""
    from ..styles import is_dark_theme  # lazy import to break the cycle

    return "dark" if is_dark_theme() else "light"


# === Mutation log ===========================================================


def _mutation_indicator_style(reversible: bool) -> str:
    t = _tokens()
    color = t.success if reversible else t.muted_text
    return f"color: {color}; font-size: inherit;"


def _mutation_desc_style() -> str:
    t = _tokens()
    return f"color: {t.text}; font-size: inherit;"


def _mutation_badge_style() -> str:
    t = _tokens()
    return f"color: {t.muted_text}; font-size: inherit; padding: 1px 4px; background: {t.button}; border-radius: 3px;"


def _mutation_undo_btn_style() -> str:
    """Undo button: success outline, accent focus, muted disabled."""
    t = _tokens()
    return (
        f"QPushButton {{ color: {t.success}; background: {t.button}; "
        f"border: 1px solid {t.success}; border-radius: 3px; "
        f"padding: 3px 10px; font-size: inherit; }}"
        f"QPushButton:hover {{ background: {t.alt_base}; }}"
        f"QPushButton:pressed {{ background: {t.mid}; }}"
        f"QPushButton:focus {{ border: 1px solid {t.accent}; }}"
        f"QPushButton:disabled {{ color: {t.muted_text}; border-color: {t.mid}; }}"
    )


def _mutation_title_style() -> str:
    t = _tokens()
    return f"color: {t.text}; font-weight: bold; font-size: inherit;"


def _mutation_count_style() -> str:
    t = _tokens()
    return f"color: {t.muted_text}; font-size: inherit;"


# === Tool approval ==========================================================


def _tool_approval_frame_style() -> str:
    t = _tokens()
    return f"QFrame#message_question {{ border: 1px solid {t.warning}; border-radius: 6px; background: {t.code_bg}; }}"


def _tool_approval_header_style() -> str:
    t = _tokens()
    return f"color: {t.warning}; font-weight: bold; font-size: inherit;"


def _tool_approval_code_editor_style() -> str:
    t = _tokens()
    return (
        f"QPlainTextEdit {{ "
        f"  color: {t.code_text}; background: {t.code_bg}; "
        f"  font-size: inherit; border: 1px solid {t.mid}; border-radius: 4px; "
        f"  padding: 4px; "
        f"}}"
        f"QScrollBar:vertical {{ width: 8px; background: {t.code_bg}; }}"
        f"QScrollBar::handle:vertical {{ background: {t.mid}; border-radius: 4px; }}"
        f"QScrollBar:horizontal {{ height: 8px; background: {t.code_bg}; }}"
        f"QScrollBar::handle:horizontal {{ background: {t.mid}; border-radius: 4px; }}"
    )


def _tool_result_editor_style(text_color: str | None = None) -> str:
    """QSS for the read-only result editor used by ExecutePythonWidget.

    Mirrors :func:`_tool_approval_code_editor_style` but lets the caller
    override the foreground colour — used to paint error output red.
    QSS keeps the first matching rule, so the override must replace the
    ``color`` value in the ``QPlainTextEdit`` rule rather than append a
    new rule after it.
    """
    t = _tokens()
    fg = text_color if text_color is not None else t.code_text
    return (
        f"QPlainTextEdit {{ "
        f"  color: {fg}; background: {t.code_bg}; "
        f"  font-size: inherit; border: 1px solid {t.mid}; border-radius: 4px; "
        f"  padding: 4px; "
        f"}}"
        f"QScrollBar:vertical {{ width: 8px; background: {t.code_bg}; }}"
        f"QScrollBar::handle:vertical {{ background: {t.mid}; border-radius: 4px; }}"
        f"QScrollBar:horizontal {{ height: 8px; background: {t.code_bg}; }}"
        f"QScrollBar::handle:horizontal {{ background: {t.mid}; border-radius: 4px; }}"
    )


def _tool_approval_allow_btn_style() -> str:
    t = _tokens()
    return (
        f"QToolButton {{ background: {t.success}; color: #ffffff; border: none; "
        f"border-radius: 4px; padding: 4px 16px; font-size: inherit; font-weight: bold; }}"
        f"QToolButton:hover {{ background: {t.success}; border: 1px solid {t.accent}; }}"
        f"QToolButton:pressed {{ background: {t.mid}; color: {t.text}; }}"
        f"QToolButton:focus {{ border: 1px solid {t.accent}; }}"
    )


def _tool_approval_always_btn_style() -> str:
    """Always-allow: deeper success tone (a shade darker than Allow) so
    the two green approval buttons are visually distinct, not a misclick trap."""
    t = _tokens()
    # Blend success toward dark for a deeper green that still reads as "allow".
    from .manager import blend_hex

    always_bg = blend_hex(t.success, t.dark, 0.45)
    return (
        f"QToolButton {{ background: {always_bg}; color: #ffffff; border: none; "
        f"border-radius: 4px; padding: 4px 16px; font-size: inherit; font-weight: bold; }}"
        f"QToolButton:hover {{ background: {t.success}; }}"
        f"QToolButton:pressed {{ background: {t.mid}; color: {t.text}; }}"
        f"QToolButton:focus {{ border: 1px solid {t.accent}; }}"
    )


def _tool_approval_deny_btn_style() -> str:
    t = _tokens()
    return (
        f"QToolButton {{ background: {t.error}; color: #ffffff; border: none; "
        f"border-radius: 4px; padding: 4px 16px; font-size: inherit; font-weight: bold; }}"
        f"QToolButton:hover {{ background: {t.error}; border: 1px solid {t.accent}; }}"
        f"QToolButton:pressed {{ background: {t.mid}; color: {t.text}; }}"
        f"QToolButton:focus {{ border: 1px solid {t.accent}; }}"
    )


def _tool_approval_disabled_btn_style() -> str:
    """Disabled approval button — reduced-opacity fill + muted text.

    Previously ``#1a5c2d`` bg + ``#808080`` text failed contrast (2.04:1)
    AND conveyed disabled via color alone. Now: the disabled state is
    carried by the ``:disabled`` selector (Qt also drops opacity), and the
    text uses ``muted_text`` so it still reads as "not actionable" without
    relying on a near-invisible dim green.
    """
    t = _tokens()
    return (
        f"QToolButton {{ background: {t.button}; color: {t.muted_text}; border: none; "
        f"border-radius: 4px; padding: 4px 16px; font-size: inherit; }}"
        f"QToolButton:disabled {{ background: {t.alt_base}; color: {t.muted_text}; "
        f"border: 1px solid {t.mid}; }}"
    )


# === Public getters (signatures unchanged) ==================================


# Legacy dict shapes kept (empty) for re-export compatibility.
MUTATION_INDICATOR_STYLE: dict = {
    "dark": {"reversible": "", "irreversible": ""},
    "light": {"reversible": "", "irreversible": ""},
}


def get_mutation_indicator_style(reversible: bool) -> str:
    return _mutation_indicator_style(reversible)


def get_mutation_desc_style() -> str:
    return _mutation_desc_style()


def get_mutation_badge_style() -> str:
    return _mutation_badge_style()


def get_mutation_undo_btn_style() -> str:
    return _mutation_undo_btn_style()


def get_mutation_title_style() -> str:
    return _mutation_title_style()


def get_mutation_count_style() -> str:
    return _mutation_count_style()


def get_tool_approval_frame_style() -> str:
    return _tool_approval_frame_style()


def get_tool_approval_header_style() -> str:
    return _tool_approval_header_style()


def get_tool_approval_code_editor_style() -> str:
    return _tool_approval_code_editor_style()


def get_tool_result_editor_style(text_color: str | None = None) -> str:
    return _tool_result_editor_style(text_color)


def get_tool_approval_allow_btn_style() -> str:
    return _tool_approval_allow_btn_style()


def get_tool_approval_always_btn_style() -> str:
    return _tool_approval_always_btn_style()


def get_tool_approval_deny_btn_style() -> str:
    return _tool_approval_deny_btn_style()


def get_tool_approval_disabled_btn_style() -> str:
    return _tool_approval_disabled_btn_style()
