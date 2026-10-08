"""Agent-tree widget style builders + getters (token-driven).

Each builder renders QSS from the live :class:`ThemeTokens` resolved via
:class:`ThemeManager`. Public getter signatures are unchanged from the
legacy ``{dark, light}`` dict version. ``AGENT_STATUS_COLORS`` remains a
branch-keyed dict (consumed as a mapping of status → color).
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


# Agent button
def _agent_btn_style() -> str:
    t = _tokens()
    return (
        f"QPushButton {{ background: {t.button}; color: {t.button_text}; border: 1px solid {t.mid}; "
        f"border-radius: 4px; padding: 4px 10px; font-size: inherit; }}"
        f"QPushButton:hover {{ background: {t.alt_base}; border-color: {t.accent}; }}"
        f"QPushButton:pressed {{ background: {t.mid}; }}"
        f"QPushButton:focus {{ border: 1px solid {t.accent}; }}"
        f"QPushButton:disabled {{ color: {t.muted_text}; }}"
    )


# Agent tree — selection token unifies list highlight
def _agent_tree_style() -> str:
    t = _tokens()
    return f"""
        QTreeWidget {{
            background: {t.base};
            color: {t.text};
            border: 1px solid {t.mid};
            font-size: inherit;
            alternate-background-color: {t.alt_base};
        }}
        QTreeWidget::item {{
            padding: 2px 4px;
        }}
        QTreeWidget::item:selected {{
            background: {t.selection};
            color: {t.highlight_text};
        }}
        QTreeWidget::item:hover {{
            background: {t.alt_base};
        }}
        QHeaderView::section {{
            background: {t.button};
            color: {t.text};
            border: 1px solid {t.mid};
            padding: 3px 6px;
            font-size: inherit;
        }}
    """


def _agent_combo_style() -> str:
    t = _tokens()
    return (
        f"QComboBox {{ background: {t.button}; color: {t.button_text}; border: 1px solid {t.mid}; "
        f"border-radius: 4px; padding: 3px 6px; font-size: inherit; }}"
        f"QComboBox:focus {{ border-color: {t.accent}; }}"
    )


def _agent_status_label_style() -> str:
    t = _tokens()
    return f"color: {t.muted_text}; font-size: inherit;"


def _agent_preview_style() -> str:
    t = _tokens()
    return (
        f"QTextEdit {{ background: {t.alt_base}; color: {t.text}; border: 1px solid {t.mid}; "
        f"font-size: inherit; padding: 4px; }}"
    )


# Agent status colors — branch-keyed dict (consumed as a mapping).
AGENT_STATUS_COLORS = {
    "dark": {
        "PENDING": "#9d9d9d",
        "RUNNING": "#dcdcaa",
        "COMPLETED": "#4ec9b0",
        "FAILED": "#f44747",
        "CANCELLED": "#9d9d9d",
    },
    "light": {
        "PENDING": "#6e6e6e",
        "RUNNING": "#9e6a00",
        "COMPLETED": "#218871",
        "FAILED": "#c42b1c",
        "CANCELLED": "#6e6e6e",
    },
}

# Orchestra panel styles moved to ui/theme/widgets_orchestra.py


# Legacy dict shapes kept (empty) for re-export compatibility.


def get_agent_btn_style() -> str:
    return _agent_btn_style()


def get_agent_tree_style() -> str:
    return _agent_tree_style()


def get_agent_combo_style() -> str:
    return _agent_combo_style()


def get_agent_status_label_style() -> str:
    return _agent_status_label_style()


def get_agent_preview_style() -> str:
    return _agent_preview_style()


def get_agent_status_colors() -> dict[str, str]:
    return AGENT_STATUS_COLORS[_branch()]
