"""Hardcoded dark theme — VS Code Dark+ inspired, matches existing Luc Nhan look."""

from __future__ import annotations

from .tokens import ThemeTokens

DARK_TOKENS = ThemeTokens(
    # QPalette-aligned (12)
    window="#1e1e1e",
    window_text="#d4d4d4",
    base="#1e1e1e",
    alt_base="#252526",
    text="#d4d4d4",
    button="#2d2d2d",
    button_text="#d4d4d4",
    highlight="#0e639c",
    highlight_text="#ffffff",
    mid="#3c3c3c",
    light="#5a5a5a",
    dark="#1a1a1a",
    # Semantic (5)
    success="#4ec9b0",
    warning="#dcdcaa",
    error="#f48771",
    code_text="#d4d4d4",
    code_bg="#1a1a1a",
    # Interaction (3) — accent navigates/focuses, selection unifies list
    # highlight (was scattered across #264f78/#2d4a4a/#0e639c), muted_text
    # is the secondary text tone (was ad-hoc #808080).
    accent="#569cd6",
    selection="#264f78",
    muted_text="#9d9d9d",
)
