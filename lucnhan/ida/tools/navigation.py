"""Navigation tools: cursor position, jump, current function."""

from __future__ import annotations

import importlib
from typing import Annotated

from ...core.logging import log_debug
from ...tools.base import parse_addr, tool

try:
    ida_funcs = importlib.import_module("ida_funcs")
    ida_kernwin = importlib.import_module("ida_kernwin")
    ida_name = importlib.import_module("ida_name")
    idaapi = importlib.import_module("idaapi")
    idc = importlib.import_module("idc")
except ImportError as e:
    log_debug(f"IDA modules not available: {e}")


@tool(category="navigation", requires=["ida_ui"])
def get_cursor_position() -> str:
    """Get the current cursor address in the IDA disassembly view.

    Unavailable in headless mode — needs an interactive UI.
    """
    ea = idc.get_screen_ea()
    return f"0x{ea:x}"


@tool(category="navigation", requires=["ida_ui"])
def get_current_function() -> str:
    """Get information about the function at the current cursor position.

    Unavailable in headless mode — needs an interactive UI.
    """

    ea = idc.get_screen_ea()
    func = ida_funcs.get_func(ea)
    if func is None:
        return "No function at current address"

    name = ida_name.get_name(func.start_ea)
    size = func.end_ea - func.start_ea
    return f"Name: {name}\nStart: 0x{func.start_ea:x}\nEnd: 0x{func.end_ea:x}\nSize: {size} bytes"


@tool(category="navigation", requires=["ida_ui"])
def jump_to(
    address: Annotated[str, "Address to jump to (hex string, e.g. '0x401000')"],
) -> str:
    """Jump the IDA disassembly view to the specified address.

    Unavailable in headless mode — needs an interactive UI.
    """

    ea = parse_addr(address)
    success = ida_kernwin.jumpto(ea)
    if success:
        return f"Jumped to 0x{ea:x}"
    return f"Failed to jump to 0x{ea:x}"


@tool(category="navigation")
def get_name_at(address: Annotated[str, "Address to query (hex string)"]) -> str:
    """Get the name/label at the specified address."""

    ea = parse_addr(address)
    name = ida_name.get_name(ea)
    return name if name else f"No name at 0x{ea:x}"


@tool(category="navigation")
def get_address_of(name: Annotated[str, "Symbol name to look up"]) -> str:
    """Get the address of a named symbol."""

    ea = ida_name.get_name_ea(0, name)
    if ea == idaapi.BADADDR:
        return f"Symbol not found: {name}"
    return f"0x{ea:x}"
