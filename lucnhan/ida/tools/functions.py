"""Function listing, searching, and info tools."""

from __future__ import annotations

import importlib
from typing import Annotated, Any

from ...core.logging import log_debug
from ...tools.base import parse_addr, tool
from ...tools.formatting import format_function_summary
from ...tools.pagination import format_page
from . import function_index

try:
    ida_funcs = importlib.import_module("ida_funcs")
    ida_gdl = importlib.import_module("ida_gdl")
    ida_name = importlib.import_module("ida_name")
    idc = importlib.import_module("idc")
    idautils = importlib.import_module("idautils")
except ImportError as e:
    log_debug(f"IDA modules not available: {e}")


@tool(category="functions")
def list_functions(
    offset: Annotated[int, "Start index for pagination"] = 0,
    limit: Annotated[int, "Max number of functions to return"] = 50,
) -> str:
    """List functions in the binary with pagination."""

    # Phase 5: serve from the cached function index. Falls back to the
    # original ``idautils.Functions()`` walk in non-IDA environments.
    entries, _total = function_index.list_function_entries(offset, limit)
    if entries:
        rows = [f"  0x{e.start_ea:x}  {e.name}" for e in entries]
    else:
        # No index (no IDA or empty IDB) — fall back to direct enumeration
        # so non-IDA test environments still get a result.
        funcs = list(idautils.Functions())
        rows = [f"  0x{ea:x}  {ida_name.get_name(ea)}" for ea in funcs[offset : offset + limit]]
    return format_page(rows, offset=offset, limit=limit, title="Functions")


def _enumerate_all_functions(
    offset: int = 0,
    limit: int = 0,
) -> list[dict]:
    """Return function metadata as raw dicts for UI components.

    This is a non-tool helper for UI components (bulk renamer) that need
    the full function list without the overhead of text formatting or
    repeated enumeration.  Returns a list of dicts with keys:
    "address", "name", "is_import", "size_bytes" (end_ea - start_ea,
    0 when not computed).

    Phase 5: served from the cached function index when available so the
    bulk-renamer UI does not re-walk ``idautils.Functions()`` on every
    refresh.  Non-IDA environments fall back to the original loop.

    Follows the project import discipline: all IDA API modules are
    imported via ``importlib.import_module()`` inside try/except blocks.
    """
    entries, _total = function_index.list_function_entries(offset, limit)
    if not entries:
        # Fall back to direct enumeration when the index is empty
        # (no IDA / empty IDB).  Keeps the contract identical to the
        # original implementation.
        ida_segment = _noisy_ida_import("ida_segment")
        addrs = list(idautils.Functions())
        if limit > 0:
            addrs = addrs[offset : offset + limit]
        elif offset > 0:
            addrs = addrs[offset:]
        out = []
        for ea in addrs:
            name = ida_name.get_name(ea)
            is_import = False
            if ida_segment is not None:
                try:
                    seg = ida_segment.getseg(ea)
                    if seg is not None:
                        seg_type_name = getattr(ida_segment, "get_segm_name", lambda s: "")(seg)
                        if seg_type_name in (".idata", ".extern", "extern"):
                            is_import = True
                except Exception as seg_err:
                    log_debug(f"Segment detection failed for 0x{ea:x}: {seg_err}")
            size_bytes = 0
            func = ida_funcs.get_func(ea)
            if func is not None:
                size_bytes = func.end_ea - func.start_ea
            out.append(
                {
                    "address": ea,
                    "name": name,
                    "is_import": is_import,
                    "size_bytes": size_bytes,
                }
            )
        return out

    # Index-backed path: zero IDA calls per row.
    return [
        {
            "address": e.start_ea,
            "name": e.name,
            "is_import": e.is_import,
            "size_bytes": e.size_bytes,
        }
        for e in entries
    ]


def _get_function_count() -> int:
    """Return the total number of functions in the IDB."""
    count = function_index.function_count()
    if count == 0:
        # Fallback for non-IDA environments.
        return len(list(idautils.Functions()))
    return count


def _noisy_ida_import(module_name: str) -> Any:
    """Import an IDA API module via importlib, returning None on failure."""
    try:
        return importlib.import_module(module_name)
    except ImportError as e:
        log_debug(f"IDA API module {module_name!r} not available: {e}")
        return None


@tool(category="functions")
def get_function_info(
    address: Annotated[str, "Function address (hex string) or function name"],
) -> str:
    """Get detailed information about a specific function."""

    ea = parse_addr(address)
    func = ida_funcs.get_func(ea)
    if func is None:
        return f"No function at 0x{ea:x}"

    name = ida_name.get_name(func.start_ea)
    size = func.end_ea - func.start_ea
    # Count basic blocks and instructions
    blocks = 0
    instrs = 0
    try:
        fc = ida_gdl.FlowChart(func)
        for block in fc:
            blocks += 1
            head = block.start_ea
            while head < block.end_ea:
                instrs += 1
                head = idc.next_head(head, block.end_ea)
    except Exception as e:
        log_debug(f"FlowChart analysis failed for 0x{ea:x}: {e}")

    # Phase 5: caller/callee resolution goes through the function index
    # to avoid a fresh ``ida_funcs.get_func`` per xref.  Falls back to
    # direct IDA calls when the index is empty (non-IDA test environments).
    index = function_index.get_function_index()
    use_index = bool(index.entries)

    callers: list[str] = []
    for ref in idautils.CodeRefsTo(func.start_ea, 0):
        caller_name = None
        if use_index:
            entry = function_index.find_containing_function(ref)
            if entry is not None:
                caller_name = entry.name
        if caller_name is None:
            caller_func = ida_funcs.get_func(ref)
            if caller_func:
                caller_name = ida_name.get_name(caller_func.start_ea)
        if caller_name:
            callers.append(caller_name)
    callers = list(set(callers))[:10]

    callees: list[str] = []
    for item in idautils.FuncItems(func.start_ea):
        for ref in idautils.CodeRefsFrom(item, 0):
            callee_name = None
            if use_index:
                entry = function_index.find_containing_function(ref)
                if entry is not None and entry.start_ea != func.start_ea:
                    callee_name = entry.name
            if callee_name is None:
                callee_func = ida_funcs.get_func(ref)
                if callee_func and callee_func.start_ea != func.start_ea:
                    callee_name = ida_name.get_name(callee_func.start_ea)
            if callee_name:
                callees.append(callee_name)
    callees = list(set(callees))[:10]

    return format_function_summary(name, func.start_ea, func.end_ea, size, blocks, instrs, callers, callees)


@tool(category="functions")
def search_functions(
    query: Annotated[str, "Search string (substring match on function name)"],
    limit: Annotated[int, "Max results"] = 20,
) -> str:
    """Search for functions by name substring."""

    # Phase 5: serve from the cached index when populated. Fallback to
    # a direct walk only when IDA is unavailable.
    matches = function_index.search_function_names(query, limit)
    if matches:
        return f"Found {len(matches)} function(s):\n" + "\n".join(f"  0x{e.start_ea:x}  {e.name}" for e in matches)

    # No IDA → fall back to direct enumeration so the contract is preserved.
    results = []
    q = query.lower()
    for ea in idautils.Functions():
        name = ida_name.get_name(ea)
        if q in name.lower():
            results.append(f"  0x{ea:x}  {name}")
            if len(results) >= limit:
                break

    if not results:
        return f"No functions matching '{query}'"
    return f"Found {len(results)} function(s):\n" + "\n".join(results)


@tool(category="functions")
def get_function_name(
    address: Annotated[str, "Function address (hex string) or function name"],
) -> str:
    """Get the current name of a function at an address.

    Returns a raw name string suitable for mutation pre-state capture.
    """
    ea = parse_addr(address)
    func = ida_funcs.get_func(ea)
    if func is None:
        return ""
    return ida_name.get_name(func.start_ea) or ""
