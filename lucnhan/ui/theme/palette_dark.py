"""Hardcoded dark theme — compact technical workspace (GUI contract G1).

Flat charcoal surfaces with a single mint accent for navigation / focus /
primary action.  These constants are the *target* palette of the redesign;
the live values are resolved through :class:`ThemeTokens` so widgets never
hardcode a colour.  AUTO / IDA modes ignore this file and follow the host
``QPalette`` (``palette_ida.py``).
"""

from __future__ import annotations

from .tokens import ThemeTokens

DARK_TOKENS = ThemeTokens(
    # QPalette-aligned (12)
    window="#202427",
    window_text="#e0e5e7",
    base="#1b1f21",
    alt_base="#282d30",
    text="#e0e5e7",
    button="#282d30",
    button_text="#e0e5e7",
    highlight="#86c7b8",
    highlight_text="#142621",
    mid="#363d41",
    light="#282d30",
    dark="#1b1f21",
    # Semantic (5)
    success="#9bc7a7",
    warning="#dbbd82",
    error="#e5a0a0",
    code_text="#e0e5e7",
    code_bg="#1b1f21",
    # Interaction (3) — accent drives navigation/focus/primary fill,
    # selection is the active-nav / selected-row fill, muted_text is the
    # secondary tone for hints / counts / captions.
    accent="#86c7b8",
    selection="#293e38",
    muted_text="#a0abb0",
)