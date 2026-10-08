"""Hardcoded light theme — compact technical workspace (GUI contract G1).

Off-white neutral surfaces with a deep green accent for navigation / focus /
primary action, mirroring the dark theme's role map so the two modes read
as the same workspace.  AUTO / IDA modes follow the host ``QPalette``.
"""

from __future__ import annotations

from .tokens import ThemeTokens

LIGHT_TOKENS = ThemeTokens(
    # QPalette-aligned (12) — off-white neutral surface set
    window="#ffffff",
    window_text="#252e2a",
    base="#edf1ee",
    alt_base="#eef1ef",
    text="#252e2a",
    button="#eef1ef",
    button_text="#252e2a",
    highlight="#326d5b",
    highlight_text="#ffffff",
    mid="#d4dcd7",
    light="#eef1ef",
    dark="#edf1ee",
    # Semantic (5) — darker variants so status reads on the light surface
    success="#356c43",
    warning="#846020",
    error="#a23838",
    code_text="#252e2a",
    code_bg="#edf1ee",
    # Interaction (3) — same roles as the dark palette
    accent="#326d5b",
    selection="#e0ece5",
    muted_text="#596a61",
)