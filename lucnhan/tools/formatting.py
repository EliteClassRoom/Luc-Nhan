"""Shared text formatting helpers used by host tool implementations.

These functions are pure string formatters with no host-API dependencies,
so they live in the shared ``lucnhan.tools`` framework rather than in a
host-specific subpackage. Both ``format_function_summary`` and
``format_callers_callees`` are reused by the IDA host's ``functions.py``
and ``xrefs.py`` tool modules — keeping them here avoids the
copy-paste drift that previously existed between the framework and
host copies.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

_T = TypeVar("_T")

#: Names listed inline in a one-line summary (the rest are counted).
_MAX_INLINE_NAMES = 40

#: Names listed one per line by :func:`format_callers_callees`.
_MAX_LISTED_NAMES = 200


def _capped(items: list[_T], limit: int) -> tuple[list[_T], int]:
    """Return the first *limit* items plus the number omitted."""
    return items[:limit], max(0, len(items) - limit)


def _inline(items: list[str], limit: int = _MAX_INLINE_NAMES) -> str:
    """Join *items* for one line, summarising the overflow."""
    shown, hidden = _capped(items, limit)
    text = ", ".join(shown)
    return f"{text}, ... and {hidden} more" if hidden else text


def format_function_summary(
    name: str,
    start: int,
    end: int,
    size: int,
    blocks: int,
    instrs: int,
    callers: list[str],
    callees: list[str],
) -> str:
    """Format a function info summary string shared by function tools."""
    parts = [
        f"Name: {name}",
        f"Address: 0x{start:x} \u2013 0x{end:x}",
        f"Size: {size} bytes",
        f"Basic blocks: {blocks}",
        f"Instructions: {instrs}",
    ]
    if callers:
        parts.append(f"Callers ({len(callers)}): {_inline(callers)}")
    if callees:
        parts.append(f"Callees ({len(callees)}): {_inline(callees)}")
    return "\n".join(parts)


def format_callers_callees(
    fname: str,
    start: int,
    callers: Iterable[str],
    callees: Iterable[str],
) -> str:
    """Format a function callers/callees summary shared by xref tools."""
    caller_list = sorted(callers)
    callee_list = sorted(callees)
    parts = [f"Function: {fname} (0x{start:x})"]
    for label, names in (("Callers", caller_list), ("Callees", callee_list)):
        shown, hidden = _capped(names, _MAX_LISTED_NAMES)
        parts.append(f"\n{label} ({len(names)}):")
        parts.extend(f"  {name}" for name in shown)
        if hidden:
            parts.append(f"  ... and {hidden} more")
    return "\n".join(parts)
