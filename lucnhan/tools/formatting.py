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
        parts.append(f"Callers ({len(callers)}): {', '.join(callers)}")
    if callees:
        parts.append(f"Callees ({len(callees)}): {', '.join(callees)}")
    return "\n".join(parts)


def format_callers_callees(
    fname: str,
    start: int,
    callers: Iterable[str],
    callees: Iterable[str],
) -> str:
    """Format a function callers/callees summary shared by xref tools."""
    callers = sorted(callers)
    callees = sorted(callees)
    parts = [f"Function: {fname} (0x{start:x})"]
    parts.append(f"\nCallers ({len(callers)}):")
    for c in callers:
        parts.append(f"  {c}")
    parts.append(f"\nCallees ({len(callees)}):")
    for c in callees:
        parts.append(f"  {c}")
    return "\n".join(parts)
