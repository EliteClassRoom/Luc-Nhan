"""Shared argument coercion helpers for ToolRegistry and mutation tracking.

This module provides a single source of truth for boolean coercion so that
ToolRegistry._coerce_arguments() and capture_pre_state() / build_reverse_record()
in mutation.py cannot diverge.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Truthy / falsy string sets
# ---------------------------------------------------------------------------
_TRUE_STRINGS: frozenset[str] = frozenset({"true", "1", "yes", "y", "on"})
_FALSE_STRINGS: frozenset[str] = frozenset({"false", "0", "no", "n", "off", ""})


def coerce_bool(value: Any, default: bool = False) -> bool:
    """Coerce a value to ``bool`` with consistent rules.

    Rules (checked in order):

    1. ``bool`` values pass through unchanged.
    2. ``None`` returns *default*.
    3. ``int`` values are coerced via Python's ``bool()`` (0 → False,
       everything else → True).
    4. ``str`` values are stripped of leading/trailing whitespace and
       lower-cased before matching against known truthy / falsy sets.
    5. Any other type returns *default*.

    Truthy strings: ``"true"``, ``"1"``, ``"yes"``, ``"y"``, ``"on"``
    Falsy  strings: ``"false"``, ``"0"``, ``"no"``, ``"n"``, ``"off"``, ``""``
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_STRINGS:
            return True
        if normalized in _FALSE_STRINGS:
            return False
    return default
