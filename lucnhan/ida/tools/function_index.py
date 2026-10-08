"""IDA function index — caches per-binary function metadata.

Phase 5 of the performance plan.  The default IDA enumeration path
(``list_functions``, ``_enumerate_all_functions``, ``search_functions``,
``get_function_info``) repeatedly calls ``idautils.Functions()``,
``ida_funcs.get_func(...)``, and ``ida_name.get_name(...)`` — each call
is O(n) over the function table on a large binary.  This module
provides a single in-memory snapshot keyed on the underlying binary
state, invalidated conservatively on mutating tools (rename, delete,
type changes).

Conservative invalidation
-------------------------
The first iteration invalidates the full index whenever
:func:`invalidate_function_index` is called.  Specific tools
(rename_function, set_type, etc.) call this hook after a successful
mutation.  This is correct but slightly less optimal than per-entry
invalidations would be; per-entry invalidation is a future refinement.

Non-IDA environments
--------------------
All IDA API modules are imported via ``importlib.import_module()``
inside try/except.  When IDA is not available (test environments,
headless CI), every helper degrades to a no-op or returns empty data,
so callers can keep their usual code path.
"""

from __future__ import annotations

import importlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ...core.logging import log_debug

# ---------------------------------------------------------------------------
# IDA module imports — tolerant of non-IDA environments
# ---------------------------------------------------------------------------
_idautils = None
_ida_funcs = None
_ida_name = None
_ida_segment = None

try:
    _idautils = importlib.import_module("idautils")
    _ida_funcs = importlib.import_module("ida_funcs")
    _ida_name = importlib.import_module("ida_name")
except ImportError:
    log_debug("function_index: IDA modules unavailable, index is a no-op")


try:
    _ida_segment = importlib.import_module("ida_segment")
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FunctionEntry:
    """One row in the function index.

    Fields mirror the keys returned by :func:`_enumerate_all_functions`
    so callers can swap between index-backed and direct-enumeration paths
    without changing the shape of the data.
    """

    start_ea: int
    end_ea: int
    name: str
    is_import: bool
    size_bytes: int


@dataclass
class _FunctionIndex:
    """In-memory snapshot of all functions in the current IDB.

    Built once per binary state.  ``by_start`` provides O(1) lookup
    by start address, ``name_lower`` powers case-insensitive substring
    search, and ``ranges`` is a sorted list of ``(start, end, entry)``
    tuples for ``find_containing_function`` (used by xref tools).
    """

    entries: list[FunctionEntry]
    by_start: dict[int, FunctionEntry]
    name_lower: list[tuple[str, FunctionEntry]]
    ranges: list[tuple[int, int, FunctionEntry]]
    built_at: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_INDEX: _FunctionIndex | None = None


def _build_index() -> _FunctionIndex:
    """Walk the IDB once and build all lookup structures."""
    entries: list[FunctionEntry] = []
    if _idautils is None or _ida_funcs is None or _ida_name is None:
        # No IDA — return an empty index so callers can stay on the same code path.
        return _FunctionIndex(
            entries=[],
            by_start={},
            name_lower=[],
            ranges=[],
        )

    for ea in _idautils.Functions():
        func = _ida_funcs.get_func(ea)
        if func is None:
            continue
        name = _ida_name.get_name(func.start_ea) or ""
        # Import detection via segment name (matches existing logic).
        is_import = False
        if _ida_segment is not None:
            try:
                seg = _ida_segment.getseg(func.start_ea)
                if seg is not None:
                    seg_type_name = getattr(_ida_segment, "get_segm_name", lambda s: "")(seg)
                    if seg_type_name in (".idata", ".extern", "extern"):
                        is_import = True
            except Exception:
                pass
        size_bytes = max(0, func.end_ea - func.start_ea)
        entries.append(
            FunctionEntry(
                start_ea=func.start_ea,
                end_ea=func.end_ea,
                name=name,
                is_import=is_import,
                size_bytes=size_bytes,
            )
        )

    by_start = {e.start_ea: e for e in entries}
    name_lower = [(e.name.lower(), e) for e in entries]
    ranges = [(e.start_ea, e.end_ea, e) for e in entries]
    # Sort ranges by start_ea for binary-search containment lookups.
    ranges.sort(key=lambda r: r[0])

    log_debug(f"function_index: built {len(entries)} entries")
    return _FunctionIndex(
        entries=entries,
        by_start=by_start,
        name_lower=name_lower,
        ranges=ranges,
    )


def get_function_index(refresh: bool = False) -> _FunctionIndex:
    """Return the cached function index, building it on first access.

    *refresh=True* forces a rebuild (used after explicit invalidation
    hooks that already cleared the cache, or for debugging).
    """
    global _INDEX
    with _LOCK:
        if _INDEX is None or refresh:
            _INDEX = _build_index()
        return _INDEX


def invalidate_function_index() -> None:
    """Drop the cached index so the next read rebuilds it.

    Called after mutating tools (rename, delete, retype, comment edit).
    Conservative: a single mutation flushes the entire index.  This is
    correct and far cheaper than the original O(n) per-call enumeration
    in the typical case where the LLM triggers a few mutating tools in
    a row and many more read-only lookups.
    """
    global _INDEX
    with _LOCK:
        _INDEX = None


def find_containing_function(ea: int) -> FunctionEntry | None:
    """Return the function entry whose ``[start_ea, end_ea)`` contains *ea*.

    Falls back to ``None`` when no function contains *ea* (e.g. data,
    imports).  Used by xref tools to resolve caller/callee addresses
    without a fresh ``ida_funcs.get_func`` call per xref.
    """
    idx = get_function_index()
    if not idx.ranges:
        return None
    # Linear scan is fine — functions are commonly O(thousands), and
    # Python's ``bisect`` requires the list be already sorted, which it
    # is.  Inlining the binary search keeps the helper self-contained.
    lo, hi = 0, len(idx.ranges) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        start, end, entry = idx.ranges[mid]
        if ea < start:
            hi = mid - 1
        elif ea >= end:
            lo = mid + 1
        else:
            return entry
    return None


def search_function_names(query: str, limit: int) -> list[FunctionEntry]:
    """Return up to *limit* entries whose name contains *query* (case-insensitive)."""
    idx = get_function_index()
    if not query or not idx.name_lower:
        return []
    q = query.lower()
    matches = [entry for name, entry in idx.name_lower if q in name]
    return matches[:limit]


def list_function_entries(offset: int, limit: int) -> tuple[list[FunctionEntry], int]:
    """Return ``(entries, total_count)`` for paginated UI consumption.

    *limit* <= 0 means "return everything from *offset* onward".  The
    *total_count* is the length of the unfiltered entry list (used by
    paginators to display "showing X-Y of N").
    """
    idx = get_function_index()
    total = len(idx.entries)
    if limit <= 0:
        return idx.entries[offset:], total
    return idx.entries[offset : offset + limit], total


def function_count() -> int:
    """Return the total number of functions in the current IDB.

    Backed by the cached index; cheaper than re-running
    ``len(list(idautils.Functions()))`` on every UI tick.
    """
    return len(get_function_index().entries)
