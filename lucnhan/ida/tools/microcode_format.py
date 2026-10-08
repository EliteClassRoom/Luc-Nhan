"""Microcode formatting and operand detail utilities.

Handles maturity levels, instruction/block/MBA text formatting,
pseudocode extraction, and operand type name resolution.
"""

from __future__ import annotations

import importlib

from ...core.errors import ToolError
from ...core.host import HAS_HEXRAYS as _HAS_HEXRAYS
from ...core.logging import log_debug

ida_hexrays = ida_lines = ida_name = None
try:
    ida_hexrays = importlib.import_module("ida_hexrays")
    ida_lines = importlib.import_module("ida_lines")
    ida_name = importlib.import_module("ida_name")
except ImportError as e:
    log_debug(f"IDA modules not available: {e}")

# ---------------------------------------------------------------------------
# Maturity level helpers
# ---------------------------------------------------------------------------
_MATURITY_NAMES = {
    0: "MMAT_GENERATED",
    1: "MMAT_PREOPTIMIZED",
    2: "MMAT_LOCOPT",
    3: "MMAT_CALLS",
    4: "MMAT_GLBOPT1",
    5: "MMAT_GLBOPT2",
    6: "MMAT_GLBOPT3",
    7: "MMAT_LVARS",
}

_MATURITY_LEVELS = {v: k for k, v in _MATURITY_NAMES.items()}


def require_hexrays() -> None:
    if not _HAS_HEXRAYS:
        raise ToolError("Hex-Rays decompiler is not available", tool_name="microcode")


def parse_maturity(name: str) -> int:
    """Convert a maturity name like 'MMAT_LOCOPT' or number to int."""
    name = name.strip().upper()
    if name in _MATURITY_LEVELS:
        return _MATURITY_LEVELS[name]
    try:
        val = int(name)
    except ValueError:
        val = None
    if val is not None and 0 <= val <= 7:
        return val
    raise ToolError(
        f"Unknown maturity level: {name!r}. Valid levels: {', '.join(_MATURITY_LEVELS.keys())}",
        tool_name="microcode",
    )


def maturity_label(level: int) -> str:
    return _MATURITY_NAMES.get(level, f"MMAT_{level}")


# ---------------------------------------------------------------------------
# Instruction / block / MBA formatting
# ---------------------------------------------------------------------------


def insn_text(ins) -> str:
    """Readable text for a single minsn_t."""
    try:
        return ins.dstr()
    except (AttributeError, RuntimeError):
        return f"<opcode {ins.opcode}>"


def format_block(blk, include_insns: bool = True) -> str:
    """Format one mblock_t as readable text."""
    preds = [blk.pred(j) for j in range(blk.npred())]
    succs = [blk.succ(j) for j in range(blk.nsucc())]
    blk_type = ""
    try:
        blk_type = f" type={blk.type}"
    except AttributeError as e:
        log_debug(f"format_block blk.type unavailable: {e}")

    hdr = f"--- Block {blk.serial} [{blk.start:#x}..{blk.end:#x}] preds={preds} succs={succs}{blk_type} ---"

    if not include_insns:
        return hdr

    lines = [hdr]
    ins = blk.head
    count = 0
    while ins is not None:
        ea_str = f"{ins.ea:#010x}" if ins.ea != 0xFFFFFFFFFFFFFFFF else "  --------"
        text = insn_text(ins)
        lines.append(f"  {ea_str}  {text}")
        count += 1
        if ins == blk.tail:
            break
        ins = ins.next
    return "\n".join(lines)


def format_mba(mba) -> str:
    """Format an entire mba_t as readable text."""
    total_insns = 0
    for i in range(mba.qty):
        blk = mba.get_mblock(i)
        ins = blk.head
        while ins is not None:
            total_insns += 1
            if ins == blk.tail:
                break
            ins = ins.next

    parts = [
        f"Maturity: {maturity_label(mba.maturity)} ({mba.maturity})",
        f"Blocks: {mba.qty}, Instructions: {total_insns}",
        "",
    ]
    for i in range(mba.qty):
        blk = mba.get_mblock(i)
        parts.append(format_block(blk))
        parts.append("")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Pseudocode extraction
# ---------------------------------------------------------------------------


def get_pseudocode_text(cfunc) -> str:
    """Extract pseudocode text from a cfunc_t."""
    lines = []
    sv = cfunc.get_pseudocode()
    for i, sl in enumerate(sv):
        text = ida_lines.tag_remove(sl.line)
        lines.append(f"{i + 1:4d}  {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Function name helper
# ---------------------------------------------------------------------------


def func_name(pfn) -> str:
    """Get a readable name for a function."""
    try:
        name = ida_name.get_name(pfn.start_ea)
        if name:
            return f"{name} ({pfn.start_ea:#x})"
    except (NameError, AttributeError) as e:
        log_debug(f"func_name_str: ida_name unavailable: {e}")
    return f"sub_{pfn.start_ea:x}"


# ---------------------------------------------------------------------------
# Operand detail
# ---------------------------------------------------------------------------

_MOP_TYPE_NAMES = {}
_MOP_FORMATTERS = {}

if _HAS_HEXRAYS:
    _MOP_TYPE_NAMES = {
        ida_hexrays.mop_z: "mop_z",
        ida_hexrays.mop_r: "mop_r",
        ida_hexrays.mop_n: "mop_n",
        ida_hexrays.mop_d: "mop_d",
        ida_hexrays.mop_S: "mop_S",
        ida_hexrays.mop_v: "mop_v",
        ida_hexrays.mop_b: "mop_b",
        ida_hexrays.mop_f: "mop_f",
        ida_hexrays.mop_l: "mop_l",
        ida_hexrays.mop_a: "mop_a",
        ida_hexrays.mop_h: "mop_h",
        ida_hexrays.mop_str: "mop_str",
        ida_hexrays.mop_c: "mop_c",
        ida_hexrays.mop_fn: "mop_fn",
        ida_hexrays.mop_p: "mop_p",
        ida_hexrays.mop_sc: "mop_sc",
    }

    def _fmt_n(op):
        val = op.nnn.value
        return f"imm {val:#x} ({val}) size={op.size}"

    def _fmt_d(op):
        parts = [f"nested_insn size={op.size}"]
        try:
            parts.append(f"  => {insn_text(op.d)}")
        except (AttributeError, RuntimeError) as e:
            log_debug(f"_fmt_d nested insn_text failed: {e}")
        return " ".join(parts)

    _MOP_FORMATTERS = {
        ida_hexrays.mop_z: lambda op: "(empty)",
        ida_hexrays.mop_r: lambda op: f"reg r{op.r} size={op.size}",
        ida_hexrays.mop_n: _fmt_n,
        ida_hexrays.mop_d: _fmt_d,
        ida_hexrays.mop_S: lambda op: f"stkvar off={op.s.off:#x} size={op.size}",
        ida_hexrays.mop_v: lambda op: f"global addr={op.g:#x} size={op.size}",
        ida_hexrays.mop_b: lambda op: f"block @{op.b}",
        ida_hexrays.mop_f: lambda op: f"callinfo size={op.size}",
        ida_hexrays.mop_l: lambda op: f"local idx={op.l.idx} off={op.l.off:#x} size={op.size}",
        ida_hexrays.mop_a: lambda op: f"addr_of size={op.size}",
        ida_hexrays.mop_h: lambda op: f"helper '{op.helper}' size={op.size}",
        ida_hexrays.mop_str: lambda op: f"string '{op.cstr}' size={op.size}",
        ida_hexrays.mop_c: lambda op: f"cases size={op.size}",
        ida_hexrays.mop_fn: lambda op: f"funcnum size={op.size}",
        ida_hexrays.mop_p: lambda op: f"pair size={op.size}",
        ida_hexrays.mop_sc: lambda op: f"scattered size={op.size}",
    }


def operand_detail(op) -> str:
    """Detailed string for a microcode operand, including type info."""
    if not _HAS_HEXRAYS:
        return "?"

    t = op.t
    formatter = _MOP_FORMATTERS.get(t)
    if formatter:
        try:
            detail = formatter(op)
        except AttributeError:
            type_name = _MOP_TYPE_NAMES.get(t, f"mop_{t}")
            detail = f"{type_name} size={op.size}"
    else:
        detail = f"unknown_type({t}) size={op.size}"

    type_name = _MOP_TYPE_NAMES.get(t, f"mop_{t}")
    return f"[{type_name}] {detail}"
