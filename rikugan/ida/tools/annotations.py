"""Mutation tools: rename, comment, set type."""

from __future__ import annotations

import importlib
from typing import Annotated

from ...core.host import HAS_HEXRAYS as _HAS_HEXRAYS
from ...core.logging import log_debug
from ...tools.base import parse_addr, tool

ida_funcs = ida_hexrays = ida_name = idc = None
try:
    ida_funcs = importlib.import_module("ida_funcs")
    ida_hexrays = importlib.import_module("ida_hexrays")
    ida_name = importlib.import_module("ida_name")
    idc = importlib.import_module("idc")
except ImportError:
    log_debug("IDA annotation modules not available — annotation tools will use guard checks")


@tool(category="annotations", mutating=True)
def rename_function(
    address: Annotated[str, "Function address (hex string)"],
    new_name: Annotated[str, "New function name"],
) -> str:
    """Rename a function."""

    ea = parse_addr(address)
    func = ida_funcs.get_func(ea)
    if func is None:
        return f"No function at 0x{ea:x}"

    old_name = ida_name.get_name(func.start_ea)
    ok = ida_name.set_name(func.start_ea, new_name, ida_name.SN_NOWARN | ida_name.SN_NOCHECK)
    if ok:
        return f"Renamed 0x{func.start_ea:x}: {old_name} \u2192 {new_name}"
    return f"Failed to rename function at 0x{func.start_ea:x}"


@tool(category="annotations", mutating=True)
def rename_variable(
    func_address: Annotated[str, "Function address (hex string)"],
    old_name: Annotated[str, "Current variable name"],
    new_name: Annotated[str, "New variable name"],
) -> str:
    """Rename a local variable in a decompiled function."""
    if not _HAS_HEXRAYS:
        return "Hex-Rays decompiler not available"

    ea = parse_addr(func_address)
    try:
        cfunc = ida_hexrays.decompile(ea)
    except Exception as e:
        return f"Decompilation failed: {e}"

    if cfunc is None:
        return f"No decompilation for 0x{ea:x}"

    lvars = cfunc.get_lvars()
    for lv in lvars:
        if lv.name == old_name:
            ok = ida_hexrays.rename_lvar(cfunc.entry_ea, lv.name, new_name)
            if ok:
                return f"Renamed variable: {old_name} \u2192 {new_name}"
            return f"Failed to rename variable {old_name}"

    return f"Variable '{old_name}' not found in function at 0x{ea:x}"


@tool(category="annotations", mutating=True)
def set_comment(
    address: Annotated[str, "Address (hex string)"],
    comment: Annotated[str, "Comment text"],
    repeatable: Annotated[bool, "Set as repeatable comment"] = False,
) -> str:
    """Set a comment at the given address."""

    ea = parse_addr(address)
    ok = idc.set_cmt(ea, comment, repeatable)
    if ok:
        kind = "repeatable " if repeatable else ""
        return f"Set {kind}comment at 0x{ea:x}: {comment}"
    return f"Failed to set comment at 0x{ea:x}"


@tool(category="annotations", mutating=True)
def set_function_comment(
    address: Annotated[str, "Function address (hex string)"],
    comment: Annotated[str, "Comment text"],
    repeatable: Annotated[bool, "Set as repeatable comment"] = False,
) -> str:
    """Set a function-level comment."""

    ea = parse_addr(address)
    func = ida_funcs.get_func(ea)
    if func is None:
        return f"No function at 0x{ea:x}"

    ok = ida_funcs.set_func_cmt(func, comment, repeatable)
    if ok:
        return f"Set function comment at 0x{func.start_ea:x}"
    return f"Failed to set function comment at 0x{func.start_ea:x}"


@tool(category="annotations", mutating=True)
def rename_address(
    address: Annotated[str, "Address (hex string)"],
    new_name: Annotated[str, "New name/label"],
) -> str:
    """Set or change the name/label at an address."""

    ea = parse_addr(address)
    old = ida_name.get_name(ea)
    ok = ida_name.set_name(ea, new_name, ida_name.SN_NOWARN | ida_name.SN_NOCHECK)
    if ok:
        return f"Named 0x{ea:x}: {old or '(unnamed)'} \u2192 {new_name}"
    return f"Failed to set name at 0x{ea:x}"


@tool(category="annotations", mutating=True)
def set_type(
    address: Annotated[str, "Address (hex string)"],
    type_string: Annotated[str, "C type string (e.g. 'int __fastcall(void*, int)')"],
) -> str:
    """Set the type of a function or data item."""

    ea = parse_addr(address)
    ok = idc.SetType(ea, type_string)
    if ok:
        return f"Set type at 0x{ea:x}: {type_string}"
    return f"Failed to set type at 0x{ea:x}. Check syntax: {type_string}"


@tool(category="annotations")
def get_comment(
    address: Annotated[str, "Address (hex string)"],
    repeatable: Annotated[bool, "Get repeatable comment"] = False,
) -> str:
    """Get the comment at an address.

    Returns a raw comment string suitable for mutation pre-state capture.
    """
    ea = parse_addr(address)
    return idc.get_cmt(ea, repeatable) or ""


@tool(category="annotations")
def get_function_comment(
    address: Annotated[str, "Function address (hex string)"],
    repeatable: Annotated[bool, "Get repeatable function comment"] = False,
) -> str:
    """Get the comment at a function.

    Returns a raw function-level comment string suitable for mutation pre-state capture.
    """
    ea = parse_addr(address)
    func = ida_funcs.get_func(ea)
    if func is None:
        return ""
    return ida_funcs.get_func_cmt(func, repeatable) or ""


RUGUGAN_EVIDENCE_TAG = "[Rikugan Evidence]"


def merge_evidence_line(existing: str, evidence: str) -> str:
    """Merge a single ``[Rikugan Evidence]`` line into *existing*.

    Replaces the previous tagged line if present; otherwise appends
    it. Other analyst text is preserved untouched. ``evidence`` is
    expected to be a single non-empty line.
    """
    if not evidence or not evidence.strip():
        return existing
    tag = RUGUGAN_EVIDENCE_TAG
    new_line = f"{tag} {evidence.strip()}"
    if tag in existing:
        head, _sep, tail = existing.partition(tag)
        rest = tail.split("\n", 1)
        after = rest[1] if len(rest) == 2 else ""
        # Avoid emitting a leading blank line when the existing
        # comment starts exactly with the tagged line.
        if head.rstrip():
            merged = head.rstrip() + "\n" + new_line
        else:
            merged = new_line
        if after:
            merged = merged + "\n" + after
        return merged
    if existing:
        return existing.rstrip() + "\n" + new_line
    return new_line


@tool(category="annotations")
def get_address_name(
    address: Annotated[str, "Address (hex string)"],
) -> str:
    """Get the current raw name at an address.

    Returns an empty string if the address has no name.
    Suitable for mutation pre-state capture.
    """
    ea = parse_addr(address)
    return ida_name.get_name(ea) or ""

__all__ = [
    "RUGUGAN_EVIDENCE_TAG",
    "rename_function",
    "rename_variable",
    "rename_address",
    "set_comment",
    "set_function_comment",
    "merge_evidence_line",
    "set_type",
    "get_comment",
    "get_function_comment",
    "get_address_name",
]
