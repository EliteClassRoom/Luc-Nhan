"""Bulk-renamer widget style builders + getters (token-driven).

Each builder renders QSS from the live :class:`ThemeTokens` resolved via
:class:`ThemeManager`. Public getter signatures are unchanged from the
legacy ``{dark, light}`` dict version. ``BULK_STATUS_COLORS`` remains a
branch-keyed dict (consumed as a mapping of status → color).

Contrast fix carried over from the audit: ``BULK_STATUS_COLORS["skipped"]``
was ``#d7ba7d`` which read at 1.87:1 as text on white (hard fail). The
"skipped" status is now a muted gray tone (passes AA) and is no longer
overloaded with the selected-row background, which moved to the
``selection`` token.
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


def _bulk_btn_style() -> str:
    t = _tokens()
    return (
        f"QPushButton {{ background: {t.button}; color: {t.button_text}; border: 1px solid {t.mid}; "
        f"border-radius: 4px; padding: 4px 10px; font-size: inherit; }}"
        f"QPushButton:hover {{ background: {t.alt_base}; border-color: {t.accent}; }}"
        f"QPushButton:pressed {{ background: {t.mid}; }}"
        f"QPushButton:focus {{ border: 1px solid {t.accent}; }}"
        f"QPushButton:disabled {{ color: {t.muted_text}; }}"
    )


def _bulk_stop_btn_style() -> str:
    """Stop button: error outline (destructive affordance)."""
    t = _tokens()
    return (
        f"QPushButton {{ background: {t.button}; color: {t.error}; border: 1px solid {t.error}; "
        f"border-radius: 4px; padding: 4px 10px; font-size: inherit; font-weight: bold; }}"
        f"QPushButton:hover {{ background: {t.alt_base}; }}"
        f"QPushButton:pressed {{ background: {t.mid}; }}"
        f"QPushButton:focus {{ border: 1px solid {t.accent}; }}"
        f"QPushButton:disabled {{ color: {t.muted_text}; border-color: {t.mid}; }}"
    )


def _bulk_start_btn_style() -> str:
    t = _tokens()
    return (
        f"QPushButton {{ background: {t.button}; color: {t.button_text}; border: 1px solid {t.text}; "
        f"border-radius: 4px; padding: 4px 14px; font-size: inherit; font-weight: bold; }}"
        f"QPushButton:hover {{ background: {t.alt_base}; border-color: {t.accent}; }}"
        f"QPushButton:pressed {{ background: {t.mid}; }}"
        f"QPushButton:focus {{ border: 1px solid {t.accent}; }}"
        f"QPushButton:disabled {{ color: {t.muted_text}; border-color: {t.mid}; }}"
    )


def _bulk_table_style() -> str:
    t = _tokens()
    return f"""
        QTableWidget {{
            background: {t.base};
            color: {t.text};
            border: 1px solid {t.mid};
            gridline-color: {t.mid};
            font-size: inherit;
            alternate-background-color: {t.alt_base};
        }}
        QTableWidget::item {{
            padding: 2px 4px;
        }}
        QTableWidget::item:selected {{
            background: {t.selection};
            color: {t.highlight_text};
        }}
        QHeaderView::section {{
            background: {t.button};
            color: {t.text};
            border: 1px solid {t.mid};
            padding: 3px 6px;
            font-size: inherit;
        }}
    """


def _bulk_filter_style() -> str:
    t = _tokens()
    return (
        f"QLineEdit {{ background: {t.base}; color: {t.text}; border: 1px solid {t.mid}; "
        f"border-radius: 3px; padding: 3px 6px; font-size: inherit; }}"
        f"QLineEdit:focus {{ border-color: {t.accent}; }}"
    )


def _bulk_combo_style() -> str:
    t = _tokens()
    return (
        f"QComboBox {{ background: {t.button}; color: {t.button_text}; border: 1px solid {t.mid}; "
        f"border-radius: 3px; padding: 3px 6px; font-size: inherit; }}"
        f"QComboBox:focus {{ border-color: {t.accent}; }}"
    )


def _bulk_num_input_style() -> str:
    t = _tokens()
    return (
        f"QLineEdit {{ background: {t.base}; color: {t.text}; border: 1px solid {t.mid}; "
        f"border-radius: 3px; padding: 2px 4px; font-size: inherit; }}"
        f"QLineEdit:focus {{ border-color: {t.accent}; }}"
    )


def _bulk_progress_style() -> str:
    t = _tokens()
    return (
        f"QProgressBar {{ background: {t.button}; border: 1px solid {t.mid}; "
        f"border-radius: 3px; text-align: center; color: {t.text}; font-size: inherit; }}"
        f"QProgressBar::chunk {{ background: {t.accent}; border-radius: 2px; }}"
    )


def _bulk_radio_style() -> str:
    t = _tokens()
    return f"QRadioButton {{ color: {t.text}; font-size: inherit; spacing: 4px; }}"


def _bulk_check_style() -> str:
    return "QCheckBox { spacing: 0px; } QCheckBox::indicator { width: 14px; height: 14px; }"


def _bulk_selection_label_style() -> str:
    t = _tokens()
    return f"color: {t.muted_text}; font-size: inherit;"


def _bulk_mode_label_style() -> str:
    t = _tokens()
    return f"color: {t.text}; font-size: inherit;"


# Bulk renamer status colors — branch-keyed dict (consumed as a mapping).
# "skipped" was #d7ba7d (1.87:1 fail as text); now a muted gray that passes AA
# and is no longer overloaded with the selection background.
BULK_STATUS_COLORS = {
    "dark": {
        "queued": "#9d9d9d",
        "analyzing": "#dcdcaa",
        "renamed": "#4ec9b0",
        "reverted": "#569cd6",
        "skipped": "#9d9d9d",
        "error": "#f44747",
    },
    "light": {
        "queued": "#6e6e6e",
        "analyzing": "#9e6a00",
        "renamed": "#218871",
        "reverted": "#0066cc",
        "skipped": "#6e6e6e",
        "error": "#c42b1c",
    },
}


# === Public getters (signatures unchanged) ==================================

# Legacy dict shapes kept (empty) for re-export compatibility.


def get_bulk_btn_style() -> str:
    return _bulk_btn_style()


def get_bulk_stop_btn_style() -> str:
    return _bulk_stop_btn_style()


def get_bulk_start_btn_style() -> str:
    return _bulk_start_btn_style()


def get_bulk_table_style() -> str:
    return _bulk_table_style()


def get_bulk_filter_style() -> str:
    return _bulk_filter_style()


def get_bulk_combo_style() -> str:
    return _bulk_combo_style()


def get_bulk_num_input_style() -> str:
    return _bulk_num_input_style()


def get_bulk_progress_style() -> str:
    return _bulk_progress_style()


def get_bulk_radio_style() -> str:
    return _bulk_radio_style()


def get_bulk_check_style() -> str:
    return _bulk_check_style()


def get_bulk_selection_label_style() -> str:
    return _bulk_selection_label_style()


def get_bulk_mode_label_style() -> str:
    return _bulk_mode_label_style()


def get_bulk_status_colors() -> dict[str, str]:
    return BULK_STATUS_COLORS[_branch()]
