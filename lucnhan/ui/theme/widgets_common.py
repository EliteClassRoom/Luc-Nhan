"""Common UI widget style builders + getters (token-driven).

Each builder renders a QSS string from the live :class:`ThemeTokens`
resolved via :class:`ThemeManager`, so a theme switch (or the
host-inherited IDA-native palette) flows through to every widget. The
public getter signatures are unchanged from the legacy ``{dark, light}``
dict version so the ~40 call sites in ``panel_core`` / ``message_widgets``
/etc. keep working.

Color-only status dicts (``TOOL_COLORS``) remain branch-keyed dicts:
they return several related colors at once and are consumed as a mapping,
so a builder would add friction without value.
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


# Tool call widget colors — branch-keyed dict (consumed as a mapping).
TOOL_COLORS = {
    "dark": {
        "bullet": "#dcdcaa",
        "status_spinner": "#dcdcaa",
        "status_error": "#f44747",
        "status_success": "#4ec9b0",
        "preview": "#808080",
        "result_header": "#808080",
    },
    "light": {
        "bullet": "#b16803",
        "status_spinner": "#b16803",
        "status_error": "#ce4770",
        "status_success": "#218871",
        "preview": "#92898a",
        "result_header": "#92898a",
    },
}


# === Button builders (all gain :focus + :pressed) ===========================
#
# Every interactive button now carries a visible ``:focus`` ring (border =
# accent token) and a ``:pressed`` tactile feedback, satisfying the
# focus-states + state-clarity rules. The border on hover nudges toward the
# accent so keyboard focus and hover read distinctly.


def _button_qss(t, hover_bg: str, *, object_name: str | None = None) -> str:
    """Shared small-button QSS: button/alt_base/mid/accent tokens.

    ``object_name`` optionally scopes the selectors (e.g. ``#history_nav_btn``)
    so a widget-local stylesheet does not leak to sibling buttons.
    """
    sel = f"QPushButton#{object_name}" if object_name else "QPushButton"
    # GUI contract G1: 4 px radius, 1 px border.  G2: button min-height
    # 28 px, padding 4 / 9 px, chrome text 12 px, and an accent focus
    # *outline* (G9) rather than a plain border swap.
    return (
        f"{sel} {{ background: {t.button}; color: {t.button_text}; "
        f"border: 1px solid {t.mid}; border-radius: 4px; padding: 4px 9px; "
        f"min-height: 20px; font-size: 12px; }}"
        f"{sel}:hover {{ background: {hover_bg}; border-color: {t.accent}; }}"
        f"{sel}:pressed {{ background: {t.mid}; }}"
        f"{sel}:focus {{ border: 1px solid {t.accent}; outline: 2px solid {t.accent}; }}"
    )


def _small_btn_style() -> str:
    t = _tokens()
    return _button_qss(t, t.alt_base)


def _cancel_btn_style() -> str:
    """Danger variant: error-colored text so destructive actions read."""
    t = _tokens()
    return (
        f"QPushButton {{ background: {t.button}; color: {t.error}; "
        f"border: 1px solid {t.mid}; border-radius: 4px; padding: 4px 9px; "
        f"min-height: 20px; font-size: 12px; }}"
        f"QPushButton:hover {{ background: {t.alt_base}; border-color: {t.error}; }}"
        f"QPushButton:pressed {{ background: {t.mid}; }}"
        f"QPushButton:focus {{ border: 1px solid {t.error}; outline: 2px solid {t.accent}; }}"
    )


# === Workspace chrome (GUI contract G2 geometry) =============================
#
# All sizes below are Qt *logical* px and follow the contract table: main
# header min-height 38 px (padding 3 / 10), session strip min-height 31 px
# (inset 9, gap 7, active underline 2), context row min-height 27 px
# (padding 4 / 12, gap 9), composer wrapper padding 7 / 10 / 5, footer
# min-height 25 px (padding 2 / 11).  Type scale: 13 px body, 12 px nav,
# 11 px context, 10-11 px footer.


def _header_frame_style() -> str:
    """Main header strip (min-height 38 px, padding 3 / 10)."""
    t = _tokens()
    return (
        f"QFrame#panel_header {{ background: {t.window}; border: none; "
        f"border-bottom: 1px solid {t.mid}; }}"
        f"QFrame#panel_header QLabel {{ background: transparent; border: none; }}"
        f"QFrame#panel_header QToolButton {{ background: transparent; border: none; }}"
    )


def _header_title_style() -> str:
    """``Luc Nhan`` wordmark (13 px, semibold) — inherits the host font."""
    t = _tokens()
    return f"color: {t.text}; font-size: 13px; font-weight: 600;"


def _header_status_style() -> str:
    """Agent status label (11 px muted).  Status is never colour-only:
    the text itself carries the state (GUI contract G1)."""
    t = _tokens()
    return f"color: {t.muted_text}; font-size: 11px;"


def _context_bar_style() -> str:
    """Context row (min-height 27 px, padding 4 / 12, gap 9).

    ``CONTEXT`` is one of the few places uppercase is allowed — a small
    metadata label.  ``ADDR:``/``FUNC:`` keep their shipped spelling.
    """
    t = _tokens()
    return (
        f"QFrame#context_bar {{ background: {t.window}; border: none; "
        f"border-bottom: 1px solid {t.mid}; }}"
        f"QFrame#context_bar QLabel {{ background: transparent; border: none; }}"
        f"QFrame#context_bar QLabel#context_label {{ color: {t.muted_text}; font-size: 11px; }}"
        f"QFrame#context_bar QLabel#context_value {{ color: {t.text}; font-size: 11px; }}"
    )


def _footer_style() -> str:
    """Footer strip (min-height 25 px, padding 2 / 11) pinned below the
    composer; model/thinking left, token usage right."""
    t = _tokens()
    return (
        f"QFrame#panel_footer {{ background: {t.window}; border: none; "
        f"border-top: 1px solid {t.mid}; }}"
        f"QFrame#panel_footer QLabel {{ background: transparent; border: none; }}"
        f"QFrame#panel_footer QToolButton {{ background: transparent; border: none; }}"
    )


def _footer_picker_trigger_style() -> str:
    """Footer picker trigger — flat chrome, focus ring on keyboard only.

    The trigger is chrome, not a button: no border, no fill while idle,
    an ``alt_base`` hover and a 2 px ``accent`` focus ring (G1/G9).
    """
    t = _tokens()
    return (
        f"QToolButton#footer_picker_trigger {{ background: transparent; border: none; "
        f"border-radius: 4px; padding: 1px 4px; color: {t.text}; font-size: 11px; }}"
        f"QToolButton#footer_picker_trigger:hover {{ background: {t.alt_base}; "
        f"border: 1px solid {t.mid}; padding: 0px 3px; }}"
        f"QToolButton#footer_picker_trigger:focus {{ background: {t.alt_base}; "
        f"border: 2px solid {t.accent}; padding: 0px 2px; }}"
    )


def _model_picker_popup_style() -> str:
    """Model picker popup — surface ``window``, 1 px border, 5 px radius.

    GUI contract G6: padding 12 px, radius 5 px, border 1 px.  The tree
    carries the list chrome; the frame itself is the only elevation.
    """
    t = _tokens()
    return (
        f"QFrame#model_picker {{ background: {t.window}; border: 1px solid {t.mid}; "
        f"border-radius: 5px; }}"
        f"QFrame#model_picker QLabel {{ background: transparent; border: none; }}"
        f"QFrame#model_picker QLabel#model_picker_title {{ color: {t.text}; "
        f"font-size: 12px; font-weight: semibold; }}"
        f"QFrame#model_picker QLabel#model_picker_thinking_label, "
        f"QFrame#model_picker QLabel#model_picker_hint {{ color: {t.muted_text}; font-size: 11px; }}"
        f"QFrame#model_picker QLineEdit {{ background: {t.base}; color: {t.text}; "
        f"border: 1px solid {t.mid}; border-radius: 4px; padding: 4px 7px; font-size: 12px; }}"
        f"QFrame#model_picker QLineEdit:focus {{ border: 2px solid {t.accent}; padding: 3px 6px; }}"
        f"QFrame#model_picker QTreeWidget {{ background: {t.base}; color: {t.text}; "
        f"border: 1px solid {t.mid}; border-radius: 4px; font-size: 12px; "
        f"outline: none; show-decoration-selected: 0; }}"
        f"QFrame#model_picker QTreeWidget::item {{ border: none; padding: 0px; }}"
        f"QFrame#model_picker QTreeWidget::item:selected {{ background: {t.selection}; color: {t.text}; }}"
        f"QFrame#model_picker QTreeWidget::item:hover {{ background: {t.alt_base}; }}"
        f"QFrame#model_picker QComboBox {{ background: {t.button}; color: {t.button_text}; "
        f"border: 1px solid {t.mid}; border-radius: 4px; padding: 3px 7px; font-size: 12px; "
        f"min-height: 20px; }}"
        f"QFrame#model_picker QComboBox:focus {{ border: 2px solid {t.accent}; padding: 2px 6px; }}"
        f"QFrame#model_picker QPushButton {{ background: transparent; color: {t.accent}; "
        f"border: 1px solid {t.mid}; border-radius: 4px; padding: 4px 8px; font-size: 11px; "
        f"text-align: left; }}"
        f"QFrame#model_picker QPushButton:hover {{ background: {t.alt_base}; border-color: {t.accent}; }}"
    )


# Session strip (chat tabs) — active tab uses selection fill + accent underline
def _tab_widget_style() -> str:
    t = _tokens()
    return (
        f"QTabWidget::pane {{ border: none; }}"
        # G2: session strip min-height 31 px, inset 9 px, gap 7 px.
        f"QTabBar {{ background: {t.window}; border: none; "
        f"border-bottom: 1px solid {t.mid}; }}"
        f"QTabBar::tab {{ background: transparent; color: {t.muted_text}; "
        f"padding: 2px 9px; margin: 2px 2px 0 2px; min-width: 40px; "
        f"border: none; border-bottom: 2px solid transparent; "
        f"font-size: 12px; max-width: 160px; }}"
        # G1/G3: active nav = selection fill + accent underline; the label
        # uses ``text`` (``highlight_text`` is white in light mode and
        # would be invisible on a near-white surface).
        f"QTabBar::tab:selected {{ background: {t.selection}; color: {t.text}; "
        f"border-bottom: 2px solid {t.accent}; }}"
        f"QTabBar::tab:hover:!selected {{ background: {t.alt_base}; color: {t.text}; }}"
        f"QTabBar::close-button {{ image: none; border: none; padding: 1px; }}"
        f"QTabBar::close-button:hover {{ background: {t.error}; border-radius: 2px; }}"
    )


# Header / placeholder labels (text + muted_text tokens)
def _tools_panel_header_style() -> str:
    t = _tokens()
    return f"color: {t.text}; font-weight: bold; font-size: inherit;"


def _placeholder_style() -> str:
    t = _tokens()
    return f"color: {t.muted_text}; padding: 20px;"


# Tools panel button — radius 4, accent focus
def _tools_panel_btn_style() -> str:
    t = _tokens()
    return (
        f"QPushButton {{ background: {t.button}; color: {t.button_text}; border: 1px solid {t.mid}; "
        f"border-radius: 4px; padding: 2px 8px; font-size: inherit; }}"
        f"QPushButton:hover {{ background: {t.alt_base}; border-color: {t.accent}; }}"
        f"QPushButton:pressed {{ background: {t.mid}; }}"
        f"QPushButton:focus {{ border: 1px solid {t.accent}; }}"
    )


# Tools panel container — accent underline on selected tab
def _tools_panel_style() -> str:
    t = _tokens()
    return f"""
        QWidget#tools_panel {{
            background: {t.window};
        }}
        QTabWidget::pane {{
            border: none;
            background: {t.window};
        }}
        QTabBar::tab {{
            background: {t.button};
            color: {t.muted_text};
            border: 1px solid {t.mid};
            border-bottom: none;
            padding: 5px 14px;
            font-size: inherit;
            min-width: 60px;
        }}
        QTabBar::tab:selected {{
            background: {t.base};
            color: {t.text};
            border-bottom: 2px solid {t.accent};
        }}
        QTabBar::tab:hover:!selected {{
            background: {t.alt_base};
            color: {t.text};
        }}
    """


# Add-tab button (QToolButton) in the chat tab bar
def _add_tab_btn_style() -> str:
    t = _tokens()
    return (
        f"QToolButton {{ color: {t.text}; font-size: inherit; font-weight: bold; "
        f"border: none; background: transparent; }}"
        f"QToolButton:hover {{ background: {t.alt_base}; border-radius: 3px; }}"
        f"QToolButton:focus {{ border: 1px solid {t.accent}; border-radius: 3px; }}"
    )


# Splitter handle
def _splitter_handle_style() -> str:
    t = _tokens()
    return f"QSplitter::handle {{ background: {t.mid}; }}"


# Message dialog (new-chat confirmation) — token-driven, accent focus
def _message_dialog_style() -> str:
    t = _tokens()
    return (
        f"QMessageBox {{ background: {t.window}; color: {t.text}; }}"
        f"QPushButton {{ background: {t.button}; color: {t.button_text}; border: 1px solid {t.mid}; "
        f"border-radius: 4px; padding: 6px 16px; font-size: inherit; min-width: 80px; }}"
        f"QPushButton:hover {{ background: {t.alt_base}; border-color: {t.accent}; }}"
        f"QPushButton:pressed {{ background: {t.mid}; }}"
        f"QPushButton:focus {{ border: 1px solid {t.accent}; }}"
    )


# Error / status labels — semantic tokens
def _error_label_style() -> str:
    t = _tokens()
    return f"color: {t.error};"


def _ok_status_style() -> str:
    t = _tokens()
    return f"color: {t.success}; font-weight: bold;"


def _hint_status_style() -> str:
    t = _tokens()
    return f"color: {t.muted_text};"


def _err_status_style() -> str:
    t = _tokens()
    return f"color: {t.error};"


# History navigation strip (paginated restore)
def _history_nav_frame_style() -> str:
    t = _tokens()
    return (
        f"QFrame#history_nav {{ background: {t.alt_base}; border: 1px solid {t.mid}; "
        f"border-radius: 4px; padding: 2px 4px; }}"
    )


def _history_nav_button_style() -> str:
    t = _tokens()
    return _button_qss(t, t.alt_base, object_name="history_nav_btn") + (
        f"QPushButton#history_nav_btn:disabled {{ color: {t.muted_text}; "
        f"background: {t.alt_base}; border-color: {t.mid}; }}"
    )


def _history_nav_label_style() -> str:
    t = _tokens()
    return f"color: {t.muted_text}; font-size: inherit; padding: 0 6px;"


# === History side panel (Task 6) ============================================
#
# These styles are applied by ``HistoryPanel`` and ``HistoryRowWidget``.
# They are deliberately scoped to ``#history_panel`` / ``#history_row``
# object-names so the QSS does not leak into sibling panels.
#
# In IDA-native mode (host palette), :func:`maybe_host_stylesheet` returns
# an empty string; the panel calls ``setStyleSheet("")`` so the host
# palette takes over without an explicit per-widget override.


def _history_panel_style() -> str:
    t = _tokens()
    return (
        f"QFrame#history_panel {{ background: {t.window}; color: {t.text}; "
        f"border-left: 1px solid {t.mid}; }}"
        f"QFrame#history_header {{ background: {t.window}; "
        f"border-bottom: 1px solid {t.mid}; }}"
    )


def _history_row_style() -> str:
    t = _tokens()
    return (
        f"QFrame#history_row {{ background: {t.base}; border-bottom: 1px solid {t.mid}; }}"
        f"QFrame#history_row:hover {{ background: {t.alt_base}; }}"
    )


def _history_title_style() -> str:
    t = _tokens()
    return f"color: {t.text}; font-size: inherit; font-weight: bold;"


def _history_meta_style() -> str:
    t = _tokens()
    return f"color: {t.muted_text}; font-size: inherit;"


def _history_scope_style() -> str:
    t = _tokens()
    # GUI contract G2: compact inline badge, radius 3 px.  ``Ignored``
    # width policy (set by the panel) lets the scope badge drop out at
    # narrow pane widths instead of squeezing the title — the header
    # still names the pane and keeps Close reachable.
    return f"color: {t.muted_text}; font-size: 11px; border-radius: 3px;"


def _history_status_style() -> str:
    t = _tokens()
    return f"color: {t.muted_text}; font-size: inherit;"


def _history_search_style() -> str:
    t = _tokens()
    return (
        f"QLineEdit#history_search {{ background: {t.base}; color: {t.text}; "
        f"border: 1px solid {t.mid}; border-radius: 4px; padding: 4px 6px; "
        f"selection-background-color: {t.highlight}; "
        f"selection-color: {t.highlight_text}; }}"
    )


def _history_close_btn_style() -> str:
    t = _tokens()
    return _button_qss(t, t.alt_base, object_name="history_close_btn")


def _history_retry_btn_style() -> str:
    t = _tokens()
    return (
        f"QPushButton#history_retry_btn {{ background: {t.button}; color: {t.button_text}; "
        f"border: 1px solid {t.accent}; border-radius: 4px; padding: 4px 12px; "
        f"font-size: inherit; }}"
        f"QPushButton#history_retry_btn:hover {{ background: {t.alt_base}; "
        f"border-color: {t.accent}; }}"
        f"QPushButton#history_retry_btn:pressed {{ background: {t.mid}; }}"
        f"QPushButton#history_retry_btn:focus {{ border: 1px solid {t.accent}; }}"
    )


def _history_delete_btn_style() -> str:
    """Delete affordance on each row (Task 4 — passive delete).

    The button is muted by default so it does not compete with the
    title for visual weight, and only the error token activates on
    hover so a destructive action reads as such. ``:focus`` borrows the
    accent border (matching every other history button) so keyboard
    navigation is visually consistent.
    """
    t = _tokens()
    return (
        f"QPushButton#history_delete_btn {{"
        f"color: {t.muted_text}; background: transparent; border: 1px solid transparent;"
        f"padding: 2px 4px; border-radius: 3px;"
        f"}}"
        f"QPushButton#history_delete_btn:hover {{"
        f"color: {t.error}; background: {t.alt_base}; border-color: {t.error};"
        f"}}"
        f"QPushButton#history_delete_btn:focus {{"
        f"color: {t.error}; border-color: {t.accent};"
        f"}}"
        f"QPushButton#history_delete_btn:disabled {{"
        f"color: {t.muted_text}; background: transparent; border-color: transparent;"
        f"}}"
    )


# === Settings composition (GUI contract G7) ==================================
#
# Four builders cover the whole restructured dialog: the heading row, the
# horizontal subnavigation strip, the section separators used inside the
# scrollable body, and the pinned Cancel/Save footer.


def _settings_heading_style() -> str:
    """``Settings`` page heading (G2: 15 px semibold)."""
    t = _tokens()
    return f"color: {t.text}; font-size: 15px; font-weight: 600; background: transparent;"


def _settings_subnav_style() -> str:
    """Horizontal subnav strip: padding 5/10, gap 3, item min-height 29.

    Active item = ``selection`` fill + ``accent`` text (G1).  The strip is
    a plain ``QWidget`` row of checkable buttons rather than a ``QTabBar``
    so the active item keeps the selection fill instead of an underline
    and so the row scrolls horizontally on narrow widths instead of
    overflowing the dialog.
    """
    t = _tokens()
    return (
        f"QWidget#settings_subnav {{ background: {t.window}; border-bottom: 1px solid {t.mid}; }}"
        f"QPushButton#settings_subnav_btn {{ background: transparent; color: {t.muted_text};"
        f" border: none; border-radius: 4px; padding: 5px 10px; min-height: 19px;"
        f" font-size: 12px; text-align: left; }}"
        f"QPushButton#settings_subnav_btn:hover {{ background: {t.alt_base}; color: {t.text}; }}"
        f"QPushButton#settings_subnav_btn:checked {{ background: {t.selection}; color: {t.accent};"
        f" font-weight: 600; }}"
        f"QPushButton#settings_subnav_btn:focus {{ border: 1px solid {t.accent}; }}"
    )


def _settings_section_style() -> str:
    """Flat section block inside the body — 1 px separator, no card chrome."""
    t = _tokens()
    return (
        f"QFrame#settings_section {{ background: transparent; border: none;"
        f" border-top: 1px solid {t.mid}; }}"
        # Qt QSS has no ``:first-of-type``; the leading section of a page
        # carries the distinct ``settings_section_first`` object name.
        f"QFrame#settings_section_first {{ background: transparent; border: none;"
        f" border-top: none; }}"
        f"QLabel#settings_section_title {{ color: {t.text}; font-size: 12px;"
        f" font-weight: 600; background: transparent; }}"
        f"QLabel#settings_form_label {{ color: {t.muted_text}; font-size: 12px;"
        f" background: transparent; }}"
    )


def _settings_footer_style() -> str:
    """Pinned action strip: hint left, Cancel then Save right."""
    t = _tokens()
    return (
        f"QFrame#settings_footer {{ background: {t.window}; border-top: 1px solid {t.mid}; }}"
        f"QLabel#settings_footer_hint {{ color: {t.muted_text}; font-size: 11px;"
        f" background: transparent; }}"
        f"QPushButton#settings_footer_btn {{ background: {t.button}; color: {t.button_text};"
        f" border: 1px solid {t.mid}; border-radius: 4px; padding: 4px 9px;"
        f" min-width: 55px; min-height: 20px; font-size: 12px; }}"
        f"QPushButton#settings_footer_btn:hover {{ background: {t.alt_base}; border-color: {t.accent}; }}"
        f"QPushButton#settings_footer_btn:pressed {{ background: {t.mid}; }}"
        f"QPushButton#settings_footer_btn:focus {{ border-color: {t.accent}; outline: 2px solid {t.accent}; }}"
        f"QPushButton#settings_footer_btn#settings_footer_save {{ background: {t.highlight};"
        f" color: {t.highlight_text}; border-color: {t.highlight}; }}"
        f"QPushButton#settings_footer_btn#settings_footer_save:hover {{ background: {t.accent};"
        f" border-color: {t.accent}; }}"
    )


# === Tools composition (GUI contract G8) ======================================


def _tools_subnav_style() -> str:
    """Tools subnav strip — same geometry as the Settings subnav.

    Shares the G7 strip geometry so Settings and Tools read as the same
    navigation surface; only the object name differs.
    """
    t = _tokens()
    return (
        f"QWidget#tools_subnav {{ background: {t.window}; border-bottom: 1px solid {t.mid}; }}"
        f"QPushButton#tools_subnav_btn {{ background: transparent; color: {t.muted_text};"
        f" border: none; border-radius: 4px; padding: 5px 10px; min-height: 19px;"
        f" font-size: 12px; text-align: left; }}"
        f"QPushButton#tools_subnav_btn:hover {{ background: {t.alt_base}; color: {t.text}; }}"
        f"QPushButton#tools_subnav_btn:checked {{ background: {t.selection}; color: {t.accent};"
        f" font-weight: 600; }}"
        f"QPushButton#tools_subnav_btn:focus {{ border: 1px solid {t.accent}; }}"
    )


def _tools_section_style() -> str:
    """Flat section block for the Agents / A2A / Knowledge pages."""
    t = _tokens()
    return (
        f"QFrame#tools_section {{ background: transparent; border: none;"
        f" border-top: 1px solid {t.mid}; }}"
        f"QLabel#tools_section_title {{ color: {t.text}; font-size: 12px;"
        f" font-weight: 600; background: transparent; }}"
        f"QTableWidget#tools_table {{ background: {t.base}; color: {t.text};"
        f" border: 1px solid {t.mid}; gridline-color: {t.mid}; }}"
        f"QTableWidget#tools_table::item {{ padding: 6px 8px; }}"
        f"QTableWidget#tools_table::item:selected {{ background: {t.selection}; color: {t.text}; }}"
        f"QHeaderView::section {{ background: {t.button}; color: {t.text};"
        f" border: none; border-bottom: 1px solid {t.mid}; padding: 3px 6px; font-size: 12px; }}"
        f"QPlainTextEdit#tools_detail {{ background: {t.code_bg}; color: {t.code_text};"
        f" border: 1px solid {t.mid}; padding: 7px; }}"
        f"QTextEdit#tools_detail {{ background: {t.code_bg}; color: {t.code_text};"
        f" border: 1px solid {t.mid}; padding: 7px; }}"
    )


# Settings button (bold, accent focus)
def _settings_btn_style() -> str:
    t = _tokens()
    return (
        f"QPushButton {{ background: {t.button}; color: {t.button_text}; border: 1px solid {t.mid}; "
        f"border-radius: 4px; font-weight: bold; }}"
        f"QPushButton:hover {{ background: {t.alt_base}; border-color: {t.accent}; }}"
        f"QPushButton:pressed {{ background: {t.mid}; }}"
        f"QPushButton:focus {{ border: 1px solid {t.accent}; }}"
    )


# === Public getters (signatures unchanged from the legacy dict version) =====


def get_small_btn_style() -> str:
    return _small_btn_style()


def get_cancel_btn_style() -> str:
    return _cancel_btn_style()


def get_header_frame_style() -> str:
    return _header_frame_style()


def get_header_title_style() -> str:
    return _header_title_style()


def get_header_status_style() -> str:
    return _header_status_style()


def get_context_bar_style() -> str:
    return _context_bar_style()


def get_footer_style() -> str:
    return _footer_style()


def get_footer_picker_trigger_style() -> str:
    return _footer_picker_trigger_style()


def get_model_picker_popup_style() -> str:
    return _model_picker_popup_style()


def get_tab_widget_style() -> str:
    return _tab_widget_style()


def get_tools_panel_header_style() -> str:
    return _tools_panel_header_style()


def get_placeholder_style() -> str:
    return _placeholder_style()


def get_tools_panel_btn_style() -> str:
    return _tools_panel_btn_style()


def get_tools_panel_style() -> str:
    return _tools_panel_style()


def get_add_tab_btn_style() -> str:
    return _add_tab_btn_style()


def get_splitter_handle_style() -> str:
    return _splitter_handle_style()


def get_message_dialog_style() -> str:
    return _message_dialog_style()


def get_error_label_style() -> str:
    return _error_label_style()


def get_ok_status_style() -> str:
    return _ok_status_style()


def get_hint_status_style() -> str:
    return _hint_status_style()


def get_err_status_style() -> str:
    return _err_status_style()


def get_settings_btn_style() -> str:
    return _settings_btn_style()


def get_settings_heading_style() -> str:
    return _settings_heading_style()


def get_settings_subnav_style() -> str:
    return _settings_subnav_style()


def get_settings_section_style() -> str:
    return _settings_section_style()


def get_settings_footer_style() -> str:
    return _settings_footer_style()


def get_tools_subnav_style() -> str:
    return _tools_subnav_style()


def get_tools_section_style() -> str:
    return _tools_section_style()


def get_history_nav_frame_style() -> str:
    return _history_nav_frame_style()


def get_history_nav_button_style() -> str:
    return _history_nav_button_style()


def get_history_nav_label_style() -> str:
    return _history_nav_label_style()


# === History side panel (Task 6) getters ==================================


def get_history_panel_style() -> str:
    return _history_panel_style()


def get_history_row_style() -> str:
    return _history_row_style()


def get_history_title_style() -> str:
    return _history_title_style()


def get_history_meta_style() -> str:
    return _history_meta_style()


def get_history_scope_style() -> str:
    return _history_scope_style()


def get_history_status_style() -> str:
    return _history_status_style()


def get_history_search_style() -> str:
    return _history_search_style()


def get_history_close_btn_style() -> str:
    return _history_close_btn_style()


def get_history_retry_btn_style() -> str:
    return _history_retry_btn_style()


def get_history_delete_btn_style() -> str:
    return _history_delete_btn_style()


def get_tool_colors() -> dict[str, str]:
    return TOOL_COLORS[_branch()]
