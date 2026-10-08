"""Orchestra / delegation / profiles widget style builders (token-driven).

Each builder renders QSS from the live :class:`ThemeTokens` resolved via
:class:`ThemeManager`. Public getter signatures are unchanged from the
legacy ``{dark, light}`` dict version.

Contrast fix carried over from the audit: ``ORCHESTRA_STATS_STYLE`` and
``DELEGATION_INFO_STYLE`` previously used ``#92898a`` on a warm ``#f8efe7``
background (2.81:1 fail). They now use the ``muted_text`` token, which
passes AA on both window and alt_base.
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


def _orchestra_panel_style() -> str:
    t = _tokens()
    return f"""
        QWidget#orchestra_panel {{
            background: {t.window};
        }}
        QLabel {{
            color: {t.text};
        }}
        QLabel.header {{
            font-size: inherit;
            font-weight: bold;
            color: {t.success};
        }}
        QTreeWidget {{
            background: {t.code_bg};
            color: {t.text};
            border: 1px solid {t.mid};
            border-radius: 4px;
            font-size: inherit;
        }}
        QTreeWidget::item {{
            padding: 3px;
        }}
        QTreeWidget::item:selected {{
            background: {t.selection};
            color: {t.highlight_text};
        }}
        QHeaderView::section {{
            background: {t.button};
            color: {t.muted_text};
            border: none;
            border-right: 1px solid {t.mid};
            border-bottom: 1px solid {t.mid};
            padding: 4px;
            font-size: inherit;
            font-weight: bold;
        }}
        QPushButton {{
            background: {t.button};
            color: {t.button_text};
            border: 1px solid {t.mid};
            border-radius: 4px;
            padding: 4px 12px;
            font-size: inherit;
        }}
        QPushButton:hover {{
            background: {t.alt_base};
            border-color: {t.accent};
        }}
        QPushButton:pressed {{
            background: {t.mid};
        }}
        QPushButton:focus {{
            border: 1px solid {t.accent};
        }}
        QPushButton:disabled {{
            background: {t.alt_base};
            color: {t.muted_text};
        }}
    """


def _orchestra_stats_style() -> str:
    t = _tokens()
    return f"color: {t.muted_text}; font-size: inherit;"


def _delegation_dialog_style() -> str:
    t = _tokens()
    return f"""
        QDialog {{
            background: {t.window};
            color: {t.text};
        }}
        QLabel {{
            color: {t.text};
        }}
        QLabel.header {{
            font-size: inherit;
            font-weight: bold;
            color: {t.success};
        }}
        QLabel.section {{
            font-size: inherit;
            font-weight: bold;
            color: {t.muted_text};
            margin-top: 8px;
        }}
        QTextEdit, QScrollArea {{
            background: {t.code_bg};
            color: {t.text};
            border: 1px solid {t.mid};
            border-radius: 4px;
            font-size: inherit;
        }}
        QScrollArea {{
            border: none;
        }}
        QTextEdit:read-only {{
            background: {t.alt_base};
        }}
        QDialogButtonBox {{
            button-layout: 0;
        }}
        QPushButton {{
            background: {t.button};
            color: {t.button_text};
            border: 1px solid {t.mid};
            border-radius: 4px;
            padding: 6px 16px;
            font-size: inherit;
        }}
        QPushButton:hover {{
            background: {t.alt_base};
            border-color: {t.accent};
        }}
        QPushButton:pressed {{
            background: {t.mid};
        }}
        QPushButton:focus {{
            border: 1px solid {t.accent};
        }}
        QPushButton#approve_btn {{
            background: {t.success};
            color: #ffffff;
            border-color: {t.success};
        }}
        QPushButton#approve_btn:hover {{
            background: {t.success};
            border-color: {t.accent};
        }}
        QPushButton#approve_btn:pressed {{
            background: {t.mid};
            color: {t.text};
        }}
        QPushButton#deny_btn {{
            background: {t.error};
            color: #ffffff;
            border-color: {t.error};
        }}
        QPushButton#deny_btn:hover {{
            background: {t.error};
            border-color: {t.accent};
        }}
        QPushButton#deny_btn:pressed {{
            background: {t.mid};
            color: {t.text};
        }}
    """


def _delegation_approval_widget_style() -> str:
    t = _tokens()
    return (
        f"QFrame#delegation_approval {{ border: 1px solid {t.success}; border-radius: 6px; background: {t.code_bg}; }}"
    )


def _delegation_header_style() -> str:
    t = _tokens()
    return f"color: {t.success}; font-weight: bold; font-size: inherit;"


def _delegation_info_style() -> str:
    t = _tokens()
    return f"color: {t.muted_text}; font-size: inherit;"


def _delegation_preview_style() -> str:
    t = _tokens()
    return f"color: {t.text}; font-size: inherit;"


# Profiles tab
def _profiles_btn_style() -> str:
    t = _tokens()
    return (
        f"QPushButton {{ background: {t.button}; color: {t.button_text}; border: 1px solid {t.mid}; "
        f"border-radius: 4px; padding: 4px 12px; }}"
        f"QPushButton:hover {{ background: {t.alt_base}; border-color: {t.accent}; }}"
        f"QPushButton:pressed {{ background: {t.mid}; }}"
        f"QPushButton:focus {{ border: 1px solid {t.accent}; }}"
    )


def _profiles_group_style() -> str:
    t = _tokens()
    return (
        f"QGroupBox {{ font-weight: bold; border: 1px solid {t.mid}; "
        f"border-radius: 4px; margin-top: 14px; padding-top: 4px; }}"
        f"QGroupBox::title {{ subcontrol-origin: margin; left: 10px; "
        f"padding: 0 6px; }}"
    )


def _profiles_header_style() -> str:
    t = _tokens()
    return f"color: {t.muted_text}; margin-top: 6px;"


# === Public getters (signatures unchanged) ==================================

# Legacy dict shapes kept (empty) for re-export compatibility.


def get_orchestra_panel_style() -> str:
    return _orchestra_panel_style()


def get_orchestra_stats_style() -> str:
    return _orchestra_stats_style()


def get_delegation_dialog_style() -> str:
    return _delegation_dialog_style()


def get_delegation_approval_widget_style() -> str:
    return _delegation_approval_widget_style()


def get_delegation_header_style() -> str:
    return _delegation_header_style()


def get_delegation_info_style() -> str:
    return _delegation_info_style()


def get_delegation_preview_style() -> str:
    return _delegation_preview_style()


def get_profiles_btn_style() -> str:
    return _profiles_btn_style()


def get_profiles_group_style() -> str:
    return _profiles_group_style()


def get_profiles_header_style() -> str:
    return _profiles_header_style()
