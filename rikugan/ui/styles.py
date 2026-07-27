"""Theme helpers and stylesheet builders for Rikugan UI.

Historically this module also held global ``LIGHT_THEME`` / ``DARK_THEME``
QSS constants and a ``build_theme_stylesheet()`` helper. Those were removed
in the phase-1 theme unification: theme palette is now driven by
``ui.theme.manager.ThemeManager`` (token-based) and this module only owns
the host-inherit bridge plus per-widget stylesheet builders.
"""

from __future__ import annotations

_current_theme: str = "light"
# ``_effective_theme`` is the helper palette ('dark' or 'light') that
# inline-styled widgets should look up. For 'ida' the effective theme is
# supplied by the caller (panel core / IDA wrapper) based on the host's
# detected color brightness.
_effective_theme: str = "light"


def set_current_theme(theme: str, effective_theme: str | None = None) -> None:
    """Set the current theme for theme-aware style getters.

    Args:
        theme: The user-configured theme name (``"light"``, ``"dark"``,
            ``"ida"``). ``"ida"`` is treated as "inherit the host".
        effective_theme: Optional helper palette to use when *theme* is
            ``"ida"`` (or any non-``"dark"``/``"light"`` value). Must be
            ``"dark"`` or ``"light"``. When omitted, the previous
            effective theme is preserved (defaulting to ``"light"`` for
            the very first call).
    """
    global _current_theme, _effective_theme
    _current_theme = theme
    if effective_theme in ("dark", "light"):
        _effective_theme = effective_theme
    elif theme in ("dark", "light"):
        _effective_theme = theme
    # If theme is 'ida' and no effective theme was supplied, leave the
    # previous effective theme alone (defaults to "light").


def is_dark_theme() -> bool:
    """Check whether inline-styled widgets should use the dark palette."""
    return _effective_theme == "dark"


def get_current_theme() -> str:
    """Return the user-configured theme name (``"light"``/``"dark"``/``"ida"``).

    ``"ida"`` means "inherit the host's Qt theme".  Use :func:`is_host_theme`
    for a boolean check.
    """
    return _current_theme


def is_host_theme() -> bool:
    """True when the configured theme is ``"ida"`` (inherit the host palette).

    In host-theme mode, Rikugan does not apply its own themed stylesheet.
    Inline-styled widgets that have a per-widget stylesheet (e.g. the
    paginated history nav strip) should clear that inline stylesheet so
    the host's Qt stylesheet — or the host-aware minimal stylesheet added
    by the IDA panel wrapper — can take over.

    ``"auto"`` is treated as host-theme for the purposes of the legacy
    helper: the effective palette is decided by the live QApplication,
    and the legacy selectors do not have a separate "auto" branch.
    """
    return _current_theme in ("ida", "auto")


# Common UI widget styles moved to ui/theme/widgets_common.py


# Common UI widget style getters moved to ui/theme/widgets_common.py;
# re-exported here so existing 'from .styles import get_*' callers stay unchanged.
# Bulk-renamer style getters moved to ui/theme/widgets_bulk.py;
# re-exported here so existing ``from .styles import get_bulk_*`` callers
# keep working unchanged.
# Agent-tree style getters moved to ui/theme/widgets_agent.py;
# re-exported here so existing 'from .styles import get_agent_*' callers
# keep working unchanged.
from .theme.widgets_agent import (  # noqa: E402,F401 — re-export
    AGENT_STATUS_COLORS,
    get_agent_btn_style,
    get_agent_combo_style,
    get_agent_preview_style,
    get_agent_status_colors,
    get_agent_status_label_style,
    get_agent_tree_style,
)
from .theme.widgets_bulk import (  # noqa: E402,F401 — re-export
    BULK_STATUS_COLORS,
    get_bulk_btn_style,
    get_bulk_check_style,
    get_bulk_combo_style,
    get_bulk_filter_style,
    get_bulk_mode_label_style,
    get_bulk_num_input_style,
    get_bulk_progress_style,
    get_bulk_radio_style,
    get_bulk_selection_label_style,
    get_bulk_start_btn_style,
    get_bulk_status_colors,
    get_bulk_stop_btn_style,
    get_bulk_table_style,
)
from .theme.widgets_common import (  # noqa: E402,F401 — re-export
    TOOL_COLORS,
    get_add_tab_btn_style,
    get_cancel_btn_style,
    get_err_status_style,
    get_error_label_style,
    get_hint_status_style,
    get_history_close_btn_style,
    get_history_delete_btn_style,
    get_history_meta_style,
    get_history_nav_button_style,
    get_history_nav_frame_style,
    get_history_nav_label_style,
    get_history_panel_style,
    get_history_retry_btn_style,
    get_history_row_style,
    get_history_scope_style,
    get_history_search_style,
    get_history_status_style,
    get_history_title_style,
    get_message_dialog_style,
    get_mode_bar_style,
    get_ok_status_style,
    get_placeholder_style,
    get_settings_btn_style,
    get_small_btn_style,
    get_splitter_handle_style,
    get_tab_widget_style,
    get_tool_colors,
    get_tools_panel_btn_style,
    get_tools_panel_header_style,
    get_tools_panel_style,
)

# Mutation/tool-approval style getters moved to ui/theme/widgets_mutation.py;
# re-exported here so existing 'from .styles import get_mutation*' callers stay unchanged.
from .theme.widgets_mutation import (  # noqa: E402,F401 — re-export
    MUTATION_INDICATOR_STYLE,
    get_mutation_badge_style,
    get_mutation_count_style,
    get_mutation_desc_style,
    get_mutation_indicator_style,
    get_mutation_title_style,
    get_mutation_undo_btn_style,
    get_tool_approval_allow_btn_style,
    get_tool_approval_always_btn_style,
    get_tool_approval_code_editor_style,
    get_tool_approval_deny_btn_style,
    get_tool_approval_disabled_btn_style,
    get_tool_approval_frame_style,
    get_tool_approval_header_style,
    get_tool_result_editor_style,
)

# Orchestra/delegation/profiles style getters moved to ui/theme/widgets_orchestra.py;
# re-exported here so existing 'from .styles import get_*' callers stay unchanged.
from .theme.widgets_orchestra import (  # noqa: E402,F401 — re-export
    get_delegation_approval_widget_style,
    get_delegation_dialog_style,
    get_delegation_header_style,
    get_delegation_info_style,
    get_delegation_preview_style,
    get_orchestra_panel_style,
    get_orchestra_stats_style,
    get_profiles_btn_style,
    get_profiles_group_style,
    get_profiles_header_style,
)

# =============================================================================
# Theme-aware style shims (added for theme.manager integration)
# =============================================================================
#
# The production code (panel_core, message_widgets, settings_dialog, etc.)
# imports these helpers from .styles. They previously lived alongside the
# now-removed theme subsystem. The shims below preserve the call sites
# while delegating to either the new ThemeManager or a safe default.
#
# They are deliberately simple — for any visual refinement the manager
# and the styles constants in this module are the source of truth.


def use_native_host_theme() -> bool:
    """Return True when the current theme should inherit the host palette.

    Equivalent to ``is_host_theme()``; provided as a backwards-compatible
    name for callers migrated from the old theme subsystem.
    """
    return is_host_theme()


def host_stylesheet(css: str, fallback: str = "") -> str:
    """Return ``css`` unchanged if the host theme is active, else ``""``.

    When the user selected the "ida" theme we do NOT apply Rikugan's
    inline styles — the host's Qt theme is the source of truth and
    Rikugan's colors would clash with it. For all other themes the
    caller-supplied ``css`` is returned verbatim.

    Callers that want a host-friendly default pass a second argument;
    it is only used in the host-theme branch.
    """
    if is_host_theme():
        return fallback
    return css


def maybe_host_stylesheet(css: str, fallback: str = "") -> str:
    """Alias for :func:`host_stylesheet` (older name)."""
    return host_stylesheet(css, fallback)


def build_small_button_stylesheet(widget: object, danger: bool = False) -> str:
    """Build a small-button stylesheet; delegates to the token-driven getters.

    When ``danger=True`` the cancel/remove color palette is used so destructive
    actions (e.g. "Undo All") are visually distinct from regular buttons.
    """
    if danger:
        return get_cancel_btn_style()
    return get_small_btn_style()


# =============================================================================
# Explicit ThemeTokens-based stylesheet builders
# =============================================================================
#
# The functions below build a small QSS string from a ``ThemeTokens``
# instance.  They are used by ``SettingsDialog`` and ``InputArea`` so
# the user-selected Rikugan Light / Dark theme always paints the
# dialog body and the chat input with the chosen palette, even when
# the host's default Qt palette would otherwise leak through as a
# black background in light mode.
#
# The returned QSS uses a ``#rikugan_settings`` / ``#input_area``
# object-name selector so the styles do not bleed into the rest of
# the host application (e.g. IDA's main window).


def build_settings_dialog_stylesheet(tokens: object) -> str:
    """Build a ThemeTokens-driven QSS for ``SettingsDialog``.

    The returned stylesheet targets only widgets under
    ``#rikugan_settings`` (the dialog's object name) and the standard
    editor controls (``QLineEdit``, ``QComboBox``, ``QSpinBox``,
    ``QDoubleSpinBox``, ``QPlainTextEdit``, ``QCheckBox``, ``QLabel``,
    ``QGroupBox``, ``QTabWidget``) so we do not affect any other
    host UI.  In the host/IDA-native mode, returns an empty string
    so the host's palette remains the source of truth.
    """
    if is_host_theme():
        return ""
    base = getattr(tokens, "base", "#ffffff")
    alt_base = getattr(tokens, "alt_base", "#f3f3f3")
    text = getattr(tokens, "text", "#1e1e1e")
    button = getattr(tokens, "button", "#f0f0f0")
    button_text = getattr(tokens, "button_text", "#1e1e1e")
    highlight = getattr(tokens, "highlight", "#0066cc")
    highlight_text = getattr(tokens, "highlight_text", "#ffffff")
    mid = getattr(tokens, "mid", "#cccccc")
    window = getattr(tokens, "window", base)
    return (
        f"#rikugan_settings {{ background-color: {window}; color: {text}; }}"
        f"#rikugan_settings QWidget {{ background-color: {base}; color: {text}; }}"
        f"#rikugan_settings QLabel {{ background: transparent; color: {text}; }}"
        f"#rikugan_settings QGroupBox {{"
        f" background-color: {alt_base}; color: {text};"
        f" border: 1px solid {mid}; border-radius: 4px;"
        f" margin-top: 8px; padding-top: 12px;"
        f"}}"
        f"#rikugan_settings QGroupBox::title {{"
        f" subcontrol-origin: margin; left: 8px; padding: 0 4px;"
        f" color: {text};"
        f"}}"
        f"#rikugan_settings QTabWidget::pane {{"
        f" border: 1px solid {mid}; background: {base};"
        f"}}"
        f"#rikugan_settings QTabBar::tab {{"
        f" background: {alt_base}; color: {text}; padding: 4px 12px;"
        f" border: 1px solid {mid}; border-bottom: none;"
        f" border-top-left-radius: 4px; border-top-right-radius: 4px;"
        f"}}"
        f"#rikugan_settings QTabBar::tab:selected {{"
        f" background: {base}; color: {text};"
        f" border-bottom: 1px solid {base};"
        f"}}"
        f"#rikugan_settings QLineEdit, "
        f"#rikugan_settings QComboBox, "
        f"#rikugan_settings QSpinBox, "
        f"#rikugan_settings QDoubleSpinBox, "
        f"#rikugan_settings QPlainTextEdit {{"
        f" background-color: {base}; color: {text};"
        f" border: 1px solid {mid}; border-radius: 4px;"
        f" padding: 3px 6px; selection-background-color: {highlight};"
        f" selection-color: {highlight_text};"
        f"}}"
        f"#rikugan_settings QLineEdit:focus, "
        f"#rikugan_settings QComboBox:focus, "
        f"#rikugan_settings QSpinBox:focus, "
        f"#rikugan_settings QDoubleSpinBox:focus, "
        f"#rikugan_settings QPlainTextEdit:focus {{"
        f" border-color: {highlight};"
        f"}}"
        f"#rikugan_settings QComboBox QAbstractItemView {{"
        f" background-color: {base}; color: {text};"
        f" border: 1px solid {mid}; selection-background-color: {highlight};"
        f" selection-color: {highlight_text};"
        f"}}"
        f"#rikugan_settings QCheckBox {{ color: {text}; }}"
        f"#rikugan_settings QRadioButton {{ color: {text}; }}"
        f"#rikugan_settings QPushButton {{"
        f" background-color: {button}; color: {button_text};"
        f" border: 1px solid {mid}; border-radius: 4px; padding: 4px 12px;"
        f"}}"
        f"#rikugan_settings QPushButton:hover {{"
        f" background-color: {alt_base};"
        f"}}"
        f"#rikugan_settings QPushButton:pressed {{"
        f" background-color: {mid}; color: {text};"
        f"}}"
        f"#rikugan_settings QDialogButtonBox {{ background: transparent; }}"
    )


def build_input_area_stylesheet(tokens: object) -> str:
    """Build a ThemeTokens-driven QSS for the chat ``InputArea``.

    Targets the ``QPlainTextEdit#input_area`` object name only, so
    the styles do not bleed into other plain text editors in the
    host.  The stylesheet is applied in every theme mode — including
    host/IDA-native — so the editor border, focus, and selection
    colors are always token-driven.  Foreground/background/placeholder
    colors are applied through ``InputArea.apply_palette`` via
    ``QPalette`` roles because Qt does not expose those properties
    as QSS sub-controls.
    """
    base = getattr(tokens, "base", "#ffffff")
    mid = getattr(tokens, "mid", "#cccccc")
    highlight = getattr(tokens, "highlight", "#0066cc")
    highlight_text = getattr(tokens, "highlight_text", "#ffffff")
    return (
        f"QPlainTextEdit#input_area {{"
        f" background-color: {base};"
        f" selection-background-color: {highlight};"
        f" selection-color: {highlight_text};"
        f" border: 1px solid {mid}; border-radius: 6px;"
        f" padding: 6px;"
        f"}}"
        f"QPlainTextEdit#input_area QScrollBar {{ background: {base}; }}"
        f"QPlainTextEdit#input_area:focus {{ border-color: {highlight}; }}"
    )


def build_skill_popup_stylesheet(tokens: object) -> str:
    """Build a ThemeTokens-driven QSS for the ``_SkillPopup``.

    The popup is a small floating widget, so the QSS targets its
    object name (``#skill_popup``) and child ``QLabel`` children.
    Returns an empty string in host/IDA-native mode.
    """
    if is_host_theme():
        return ""
    alt_base = getattr(tokens, "alt_base", "#f3f3f3")
    text = getattr(tokens, "text", "#1e1e1e")
    mid = getattr(tokens, "mid", "#cccccc")
    highlight = getattr(tokens, "highlight", "#0066cc")
    highlight_text = getattr(tokens, "highlight_text", "#ffffff")
    return (
        f"QFrame#skill_popup {{"
        f" background-color: {alt_base}; color: {text};"
        f" border: 1px solid {mid}; border-radius: 4px; padding: 2px;"
        f"}}"
        f"QFrame#skill_popup QLabel {{"
        f" background: transparent; color: {text}; padding: 3px 8px;"
        f"}}"
        f'QFrame#skill_popup QLabel[selected="true"] {{'
        f" background-color: {highlight}; color: {highlight_text};"
        f" border-radius: 3px;"
        f"}}"
        f'QFrame#skill_popup QLabel[selected="false"] {{'
        f" background-color: {alt_base}; color: {text};"
        f"}}"
    )
