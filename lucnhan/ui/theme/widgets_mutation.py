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


def _role_size(px: int) -> str:
    """Typography role size, honouring the user's font override.

    Imported lazily for the same reason as :func:`_tokens`: this module
    sits under the theme package, which ``styles`` imports back.
    """
    from ..styles import role_size

    return role_size(px)


def _branch() -> str:
    """Return ``'dark'`` or ``'light'`` for the active effective theme."""
    from ..styles import is_dark_theme  # lazy import to break the cycle

    return "dark" if is_dark_theme() else "light"


# === Mutation log ===========================================================


def _mutation_indicator_style(reversible: bool) -> str:
    t = _tokens()
    color = t.success if reversible else t.muted_text
    return f"color: {color}; font-size: {_role_size(11)};"


def _mutation_desc_style() -> str:
    t = _tokens()
    return f"color: {t.text}; font-size: {_role_size(13)};"


def _mutation_meta_style() -> str:
    """Muted time + reversibility status under each record (G2 meta line)."""
    t = _tokens()
    return f"color: {t.muted_text}; font-size: {_role_size(11)};"


def _mutation_badge_style() -> str:
    t = _tokens()
    return f"color: {t.muted_text}; font-size: {_role_size(10)}; padding: 1px 4px; background: {t.button}; border-radius: 3px;"


def _mutation_undo_btn_style() -> str:
    """Undo button: success outline, accent focus, muted disabled."""
    t = _tokens()
    return (
        f"QPushButton {{ color: {t.success}; background: {t.button}; "
        f"border: 1px solid {t.success}; border-radius: 3px; "
        f"padding: 3px 10px; font-size: {_role_size(13)}; }}"
        f"QPushButton:hover {{ background: {t.alt_base}; }}"
        f"QPushButton:pressed {{ background: {t.mid}; }}"
        f"QPushButton:focus {{ border: 1px solid {t.accent}; }}"
        f"QPushButton:disabled {{ color: {t.muted_text}; border-color: {t.mid}; }}"
    )


def _mutation_title_style() -> str:
    t = _tokens()
    return f"color: {t.text}; font-weight: bold; font-size: {_role_size(12)};"


def _mutation_count_style() -> str:
    t = _tokens()
    return f"color: {t.muted_text}; font-size: {_role_size(11)};"


# === Tool approval ==========================================================


def _tool_approval_frame_style() -> str:
    """Warning-border exception card (G4): 1px ``warning``, radius 4.

    Radius 4 is the contract's control radius; the previous 6px read as
    an oversized card against the 4px buttons inside it.
    """
    t = _tokens()
    return f"QFrame#message_question {{ border: 1px solid {t.warning}; border-radius: 4px; background: {t.window}; }}"


def _tool_approval_pending_row_style() -> str:
    """Warning-border exception for a gate hosted in a tool row (G4).

    ``ExecutePythonWidget`` keeps its tool-row object name (the chat
    groups rows by it), so it cannot use the ``#message_question``
    selector above while pending. Same tokens, same exception: approval
    is the one bordered surface in the transcript.
    """
    t = _tokens()
    return f"QFrame#message_tool {{ border: 1px solid {t.warning}; border-radius: 4px; background: {t.window}; }}"


def _tool_approval_header_style() -> str:
    t = _tokens()
    return f"color: {t.warning}; font-weight: bold; font-size: {_role_size(12)};"


def _tool_approval_state_style() -> str:
    """Header state chip (``Paused``) — muted caption, 10-11px role (G2)."""
    t = _tokens()
    return f"color: {t.muted_text}; font-size: {_role_size(11)};"


def _tool_approval_body_style() -> str:
    """Description/target line inside the approval card."""
    t = _tokens()
    return f"color: {t.text}; font-size: {_role_size(13)};"


def _tool_row_style() -> str:
    """Compact tool summary row (G2/G4): one row, min-height 30, no card
    chrome. Padding 5/6, gap 8, radius 4 (control radius).

    The collapse chevron is flat: left to the host's button chrome it
    painted a dark filled square in Light.
    """
    t = _tokens()
    return (
        "QFrame#message_tool { background-color: transparent; border: none; }"
        f"QFrame#message_tool QToolButton#collapse_button {{ background: transparent; border: none; "
        f"border-radius: 3px; color: {t.muted_text}; padding: 0px; }}"
        f"QFrame#message_tool QToolButton#collapse_button:hover {{ background: {t.alt_base}; color: {t.text}; }}"
    )


def _tool_row_name_style() -> str:
    """Tool name — 12px semibold role (G2)."""
    t = _tokens()
    return f"color: {t.text}; font-weight: bold; font-size: {_role_size(12)};"


def _tool_row_target_style() -> str:
    """Target/argument column — 11px monospace (G2)."""
    t = _tokens()
    return f'color: {t.muted_text}; font-family: Consolas, "Courier New", monospace; font-size: {_role_size(11)};'


def _tool_row_meta_style() -> str:
    """Right-hand count/duration — muted caption (G2/G4)."""
    t = _tokens()
    return f"color: {t.muted_text}; font-size: {_role_size(11)};"


def _tool_detail_surface_style() -> str:
    """Expanded detail inset surface (G4): padding 7 top / 10 right-bottom /
    22 left. Flat ``base`` fill, 1px ``mid`` border, radius 3 (compact badge
    radius — it is an inset code surface, not a control)."""
    t = _tokens()
    return (
        f"QFrame#tool_detail {{ background: {t.base}; border: 1px solid {t.mid}; border-radius: 3px; }}"
        f"QFrame#tool_detail QLabel#tool_content {{ color: {t.text}; "
        f'font-family: Consolas, "Courier New", monospace; font-size: {_role_size(12)}; }}'
    )


def _tool_approval_code_editor_style() -> str:
    t = _tokens()
    return (
        f"QPlainTextEdit {{ "
        f"  color: {t.code_text}; background: {t.code_bg}; "
        f'  font-family: Consolas, "Courier New", monospace; font-size: {_role_size(12)}; '
        f"  border: 1px solid {t.mid}; border-radius: 4px; "
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
        f'  font-family: Consolas, "Courier New", monospace; font-size: {_role_size(12)}; '
        f"  border: 1px solid {t.mid}; border-radius: 4px; "
        f"  padding: 4px; "
        f"}}"
        f"QScrollBar:vertical {{ width: 8px; background: {t.code_bg}; }}"
        f"QScrollBar::handle:vertical {{ background: {t.mid}; border-radius: 4px; }}"
        f"QScrollBar:horizontal {{ height: 8px; background: {t.code_bg}; }}"
        f"QScrollBar::handle:horizontal {{ background: {t.mid}; border-radius: 4px; }}"
    )


def _tool_approval_allow_btn_style() -> str:
    t = _tokens()
    # Mint accent fill with ``highlight_text`` on top (G1): a fixed white
    # foreground is unreadable on the Light accent (#326d5b reads 5.9:1
    # with #ffffff but 3.4:1 with the dark-theme text tone), and the
    # token already carries the right side for each palette.
    return (
        f"QToolButton {{ background: {t.accent}; color: {t.highlight_text}; border: none; "
        f"border-radius: 4px; padding: 4px 16px; font-size: {_role_size(13)}; font-weight: bold; }}"
        f"QToolButton:hover {{ background: {t.accent}; border: 1px solid {t.text}; }}"
        f"QToolButton:pressed {{ background: {t.mid}; color: {t.text}; }}"
        f"QToolButton:focus {{ border: 1px solid {t.accent}; }}"
    )


def _tool_approval_always_btn_style() -> str:
    """Always-allow (non-script tools only): quieter than ``Allow once``.

    It must stay visually distinct from the one-shot grant without
    reading as a second saturated fill, so it is a neutral outline on
    the success tone.
    """
    t = _tokens()
    return (
        f"QToolButton {{ background: transparent; color: {t.success}; "
        f"border: 1px solid {t.success}; border-radius: 4px; padding: 4px 16px; "
        f"font-size: {_role_size(13)}; font-weight: bold; }}"
        f"QToolButton:hover {{ background: {t.success}; color: {t.window}; }}"
        f"QToolButton:pressed {{ background: {t.mid}; color: {t.text}; }}"
        f"QToolButton:focus {{ border: 1px solid {t.success}; outline: 2px solid {t.accent}; }}"
    )


def _tool_approval_deny_btn_style() -> str:
    t = _tokens()
    # Destructive, but as a quiet OUTLINE (G1: the decision strip is not
    # a row of saturated fills, and only one button should read as the
    # primary action). The label uses ``error`` on the base surface so it
    # stays readable in Light, Dark and any host palette, and gains the
    # error fill on hover.
    return (
        f"QToolButton {{ background: transparent; color: {t.error}; "
        f"border: 1px solid {t.error}; border-radius: 4px; padding: 4px 16px; "
        f"font-size: {_role_size(13)}; font-weight: bold; }}"
        f"QToolButton:hover {{ background: {t.error}; color: {t.window}; }}"
        f"QToolButton:pressed {{ background: {t.mid}; color: {t.text}; }}"
        f"QToolButton:focus {{ border: 1px solid {t.error}; outline: 2px solid {t.accent}; }}"
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
        f"border-radius: 4px; padding: 4px 16px; font-size: {_role_size(13)}; }}"
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


def get_mutation_meta_style() -> str:
    return _mutation_meta_style()


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


def get_tool_approval_pending_row_style() -> str:
    return _tool_approval_pending_row_style()


def get_tool_approval_header_style() -> str:
    return _tool_approval_header_style()


def get_tool_approval_state_style() -> str:
    return _tool_approval_state_style()


def get_tool_approval_body_style() -> str:
    return _tool_approval_body_style()


def get_tool_row_style() -> str:
    return _tool_row_style()


def get_tool_row_name_style() -> str:
    return _tool_row_name_style()


def get_tool_row_target_style() -> str:
    return _tool_row_target_style()


def get_tool_row_meta_style() -> str:
    return _tool_row_meta_style()


def get_tool_detail_surface_style() -> str:
    return _tool_detail_surface_style()


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
