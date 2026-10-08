"""ThemeMode enum and ThemeTokens dataclass — 20 semantic color keys.

The 12 QPalette-aligned keys (window, window_text, base, alt_base, text,
button, button_text, highlight, highlight_text, mid, light, dark) are
derived from QPalette in IDA_NATIVE mode and hardcoded in DARK/LIGHT
modes. The 5 semantic keys (success, warning, error, code_text, code_bg)
are derived per-theme (no QPalette equivalent). The 3 interaction keys
(accent, selection, muted_text) are widget-facing: accent drives
navigation/focus affordances (tab underline, focus borders), selection
unifies list-item highlight backgrounds, and muted_text is the secondary
text tone for hints/counts/stats.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


def hex_luminance(hex_color: str) -> float:
    """sRGB-linearized luminance for a ``#rrggbb`` color (0..1).

    Uses the IEC 61966-2-1 EOTF inverse + BT.709 luminance
    coefficients. Lives in this leaf module so both ``manager.py``
    and external callers (e.g. ``message_widgets.py``) can share a
    single source of truth.
    """
    h = hex_color.strip().lower()
    if not (len(h) == 7 and h.startswith("#")):
        return 0.0
    r = int(h[1:3], 16) / 255.0
    g = int(h[3:5], 16) / 255.0
    b = int(h[5:7], 16) / 255.0

    def _srgb_to_linear(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r_lin, g_lin, b_lin = _srgb_to_linear(r), _srgb_to_linear(g), _srgb_to_linear(b)
    return 0.2126 * r_lin + 0.7152 * g_lin + 0.0722 * b_lin


class ThemeMode(str, Enum):
    """User-selectable theme mode.

    AUTO: follow host — IDA→native palette, standalone→Dark.
    DARK: Luc Nhan hardcoded dark theme.
    LIGHT: Luc Nhan VS Code Light+ theme.
    IDA_NATIVE: always transparent, follow IDA palette (non-IDA falls
        back to DARK with a warning).
    """

    AUTO = "auto"
    DARK = "dark"
    LIGHT = "light"
    IDA_NATIVE = "ida"


@dataclass(frozen=True)
class ThemeTokens:
    """20 semantic color tokens, immutable."""

    # QPalette-aligned (12)
    window: str
    window_text: str
    base: str
    alt_base: str
    text: str
    button: str
    button_text: str
    highlight: str
    highlight_text: str
    mid: str
    light: str
    dark: str
    # Semantic (5) — derived per-theme
    success: str
    warning: str
    error: str
    code_text: str
    code_bg: str
    # Interaction (3) — widget-facing, unified across widget QSS
    accent: str
    selection: str
    muted_text: str


def is_dark_tokens(tokens: ThemeTokens) -> bool:
    """Return True when the token's window color is dark (luminance < 0.5)."""
    if not (len(tokens.window) == 7 and tokens.window.startswith("#")):
        return False
    return hex_luminance(tokens.window) < 0.5
