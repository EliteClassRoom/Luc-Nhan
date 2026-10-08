"""Derive ThemeTokens from the current QApplication palette.

Used when ThemeMode is AUTO (in IDA) or IDA_NATIVE. Reads 12 QPalette
roles and derives 5 semantic tokens (success/warning/error/code_text/code_bg)
by blending fixed base hues toward the active text luminance.
"""

from __future__ import annotations

from typing import Any

# QPalette comes from the shared Qt compatibility layer. Direct
# PySide6/PyQt5 imports are forbidden here because palette_ida is loaded
# on the panel startup path in IDA, where loading Qt6 into a Qt5 host can
# crash the process.
try:
    from ..qt_compat import QPalette

    _HAS_QT = True
except (ImportError, ModuleNotFoundError):  # pragma: no cover
    QPalette = None  # type: ignore[assignment]
    _HAS_QT = False

from .manager import blend_hex, hex_luminance
from .tokens import ThemeTokens

# Fixed reference hues for semantic tokens.
_SUCCESS_BASE = "#4ec9b0"
_WARNING_BASE = "#dcdcaa"
_ERROR_BASE = "#f48771"
# Interaction reference hues — blended toward text like semantic tokens.
_ACCENT_BASE = "#569cd6"
_MUTED_DARK = "#9d9d9d"
_MUTED_LIGHT = "#6e6e6e"

_ROLE_KEYS: list[tuple[Any, str]] = []
if _HAS_QT and QPalette is not None:
    _ROLE_KEYS = [
        (QPalette.ColorRole.Window, "window"),
        (QPalette.ColorRole.WindowText, "window_text"),
        (QPalette.ColorRole.Base, "base"),
        (QPalette.ColorRole.AlternateBase, "alt_base"),
        (QPalette.ColorRole.Text, "text"),
        (QPalette.ColorRole.Button, "button"),
        (QPalette.ColorRole.ButtonText, "button_text"),
        (QPalette.ColorRole.Highlight, "highlight"),
        (QPalette.ColorRole.HighlightedText, "highlight_text"),
        (QPalette.ColorRole.Mid, "mid"),
        (QPalette.ColorRole.Dark, "dark"),
        (QPalette.ColorRole.Light, "light"),
    ]


def _read_qpalette_colors(source: Any) -> dict[str, str]:
    """Read 12 QPalette role colors as a dict of hex strings."""
    pal = source.palette()
    out: dict[str, str] = {}
    for role, key in _ROLE_KEYS:
        try:
            out[key] = pal.color(role).name()
        except Exception:
            out[key] = "#000000"
    return out


def _derive_semantic_tokens(qp_colors: dict[str, str]) -> dict[str, str]:
    text = qp_colors.get("text", "#000000")
    alt_base = qp_colors.get("alt_base", "#000000")
    is_dark = hex_luminance(qp_colors.get("window", "#000000")) < 0.5
    amount = 0.15 if is_dark else 0.35

    return {
        "success": blend_hex(_SUCCESS_BASE, text, amount),
        "warning": blend_hex(_WARNING_BASE, text, amount),
        "error": blend_hex(_ERROR_BASE, text, amount),
        "code_text": text,
        "code_bg": alt_base,
    }


def _derive_interaction_tokens(qp_colors: dict[str, str]) -> dict[str, str]:
    """Derive the 3 interaction tokens from the host QPalette.

    accent: navigation/focus affordance — blend the fixed accent hue toward
        text so it tracks host luminance (bright host → darker accent).
    selection: list-item highlight background — use the host Highlight role
        for dark palettes (already high-contrast) or a tinted blend of the
        accent toward the window color for light palettes (Highlight is
        often too saturated for large list areas in light hosts).
    muted_text: secondary text tone — pick a dark/light reference and blend
        toward text so it stays readable on both window and alt_base.
    """
    text = qp_colors.get("text", "#000000")
    window = qp_colors.get("window", "#ffffff")
    highlight = qp_colors.get("highlight", "#0e639c")
    is_dark = hex_luminance(window) < 0.5

    muted_ref = _MUTED_DARK if is_dark else _MUTED_LIGHT
    selection = highlight if is_dark else blend_hex(_ACCENT_BASE, window, 0.78)

    return {
        "accent": blend_hex(_ACCENT_BASE, text, 0.15 if is_dark else 0.35),
        "selection": selection,
        "muted_text": blend_hex(muted_ref, text, 0.2),
    }


def derive_ida_tokens(source: Any) -> ThemeTokens:
    """Build a full ThemeTokens from a QApplication-like `source`."""
    qp = _read_qpalette_colors(source)
    semantic = _derive_semantic_tokens(qp)
    interaction = _derive_interaction_tokens(qp)
    return ThemeTokens(**qp, **semantic, **interaction)
