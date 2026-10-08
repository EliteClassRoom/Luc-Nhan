"""Disassembly reading tools."""

from __future__ import annotations

import importlib
from typing import Annotated

from ...tools.base import parse_addr, tool

ida_funcs = idautils = idc = None  # populated below when IDA is available
try:
    ida_funcs = importlib.import_module("ida_funcs")
    idautils = importlib.import_module("idautils")
    idc = importlib.import_module("idc")
except ImportError:
    ida_funcs = idautils = idc = None  # IDA not present — tools unavailable in non-IDA context


@tool(category="disassembly")
def read_disassembly(
    address: Annotated[str, "Start address (hex string)"],
    count: Annotated[int, "Number of instructions to disassemble"] = 30,
) -> str:
    """Read disassembly listing starting at the given address."""

    ea = parse_addr(address)
    lines = []
    for _ in range(count):
        mnem = idc.print_insn_mnem(ea)
        if not mnem:
            break
        ops = idc.print_operand(ea, 0)
        op2 = idc.print_operand(ea, 1)
        if op2:
            ops += f", {op2}"
        op3 = idc.print_operand(ea, 2)
        if op3:
            ops += f", {op3}"

        comment = idc.get_cmt(ea, 0) or ""
        rep_comment = idc.get_cmt(ea, 1) or ""
        cmt = ""
        if comment:
            cmt = f"  ; {comment}"
        elif rep_comment:
            cmt = f"  ; {rep_comment}"

        lines.append(f"  0x{ea:08x}  {mnem:8s} {ops}{cmt}")
        ea = idc.next_head(ea, ea + 0x1000)
        if ea == idc.BADADDR:
            break
    return "\n".join(lines)


@tool(category="disassembly")
def read_function_disassembly(
    address: Annotated[str, "Function address (hex string)"],
) -> str:
    """Read the full disassembly of a function."""

    ea = parse_addr(address)
    func = ida_funcs.get_func(ea)
    if func is None:
        return f"No function at 0x{ea:x}"

    lines = [f"; Function at 0x{func.start_ea:x}"]
    for head in idautils.FuncItems(func.start_ea):
        mnem = idc.print_insn_mnem(head)
        if not mnem:
            continue
        ops = idc.print_operand(head, 0)
        op2 = idc.print_operand(head, 1)
        if op2:
            ops += f", {op2}"
        op3 = idc.print_operand(head, 2)
        if op3:
            ops += f", {op3}"

        comment = idc.get_cmt(head, 0) or idc.get_cmt(head, 1) or ""
        cmt = f"  ; {comment}" if comment else ""
        lines.append(f"  0x{head:08x}  {mnem:8s} {ops}{cmt}")

    return "\n".join(lines)


@tool(category="disassembly")
def get_instruction_info(
    address: Annotated[str, "Instruction address (hex string)"],
) -> str:
    """Get detailed info about a single instruction."""

    ea = parse_addr(address)
    mnem = idc.print_insn_mnem(ea)
    if not mnem:
        return f"No instruction at 0x{ea:x}"

    size = idc.get_item_size(ea)
    ops = []
    for i in range(6):
        op = idc.print_operand(ea, i)
        if op:
            ops.append(op)

    # Get bytes
    byte_str = " ".join(f"{idc.get_wide_byte(ea + i):02x}" for i in range(size))

    parts = [
        f"Address: 0x{ea:x}",
        f"Mnemonic: {mnem}",
        f"Operands: {', '.join(ops) if ops else '(none)'}",
        f"Size: {size} bytes",
        f"Bytes: {byte_str}",
    ]

    comment = idc.get_cmt(ea, 0)
    if comment:
        parts.append(f"Comment: {comment}")

    return "\n".join(parts)
