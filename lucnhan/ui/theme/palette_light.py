"""Hardcoded light theme — VS Code Light+ inspired, neutral with high readability."""

from __future__ import annotations

from .tokens import ThemeTokens

LIGHT_TOKENS = ThemeTokens(
    # QPalette-aligned (12) — cool-neutral (VS Code Light+)
    window="#ffffff",
    window_text="#1e1e1e",
    base="#ffffff",
    alt_base="#f3f3f3",
    text="#1e1e1e",
    button="#f0f0f0",
    button_text="#1e1e1e",
    highlight="#0066cc",
    highlight_text="#ffffff",
    mid="#cccccc",
    light="#ffffff",
    dark="#a0a0a0",
    # Semantic (5) — darker variants for light bg
    success="#2c8a4a",
    warning="#a67900",
    error="#c42b1c",
    code_text="#1e1e1e",
    code_bg="#f3f3f3",
    # Interaction (3) — accent matches highlight for light (nav/focus),
    # selection unifies list highlight (was ad-hoc #d7ba7d), muted_text is
    # the secondary tone (was ad-hoc #92898a which failed 2.81:1 on warm bg).
    accent="#0066cc",
    selection="#cce4ff",
    muted_text="#6e6e6e",
)
