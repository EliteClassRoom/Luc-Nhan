"""Microcode reading, inspection, and manipulation tools.

Provides the AI agent with direct access to IDA's Hex-Rays microcode layer.
The agent can read microcode at any maturity level, NOP junk instructions,
and install custom Python-based optimizers to clean decompiler output.

Typical workflow for cleaning obfuscated code:
  1. get_microcode()        — read the raw microcode, identify junk patterns
  2. nop_microcode()        — NOP specific junk instructions by address
     or install_optimizer() — install a pattern-matching rule
  3. redecompile()          — force redecompilation, see cleaned pseudocode
"""

from __future__ import annotations

import importlib
from typing import Annotated

from ...core.errors import ToolError
from ...core.host import HAS_HEXRAYS as _HAS_HEXRAYS
from ...core.logging import log_debug
from ...tools.base import parse_addr, tool
from .microcode_format import (
    format_mba,
    func_name,
    get_pseudocode_text,
    insn_text,
    maturity_label,
    operand_detail,
    parse_maturity,
    require_hexrays,
)
from .microcode_optim import (
    DynamicBlockOptimizer,
    DynamicInsnOptimizer,
    NopOptimizer,
    compile_optimizer,
    installed_optimizers,
    remove_optimizer,
)

ida_hexrays = ida_funcs = ida_range = None
try:
    ida_hexrays = importlib.import_module("ida_hexrays")
    ida_funcs = importlib.import_module("ida_funcs")
    ida_range = importlib.import_module("ida_range")
except ImportError as e:
    log_debug(f"IDA modules not available: {e}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

if _HAS_HEXRAYS:

    class _MBACapture(ida_hexrays.Hexrays_Hooks):  # type: ignore[misc]
        """Hexrays hook that captures the MBA at a target maturity level."""

        def __init__(self, target_maturity: int) -> None:
            super().__init__()
            self.target_maturity = target_maturity
            self.captured = None

        def maturity(self, cfunc, new_maturity):
            try:
                cur = cfunc.mba.maturity
                if self.captured is None and cur == self.target_maturity:
                    self.captured = format_mba(cfunc.mba)
            except (AttributeError, RuntimeError) as e:
                log_debug(f"_MBACapture hook: mba not yet available: {e}")
            return 0


def _get_func_or_raise(ea: int):
    pfn = ida_funcs.get_func(ea)
    if pfn is None:
        raise ToolError(f"No function at {ea:#x}", tool_name="microcode")
    return pfn


def _gen_microcode_at(ea: int, maturity: int):
    """Generate microcode for the function containing *ea* at *maturity*.

    Uses gen_microcode() for intermediate levels and decompile() for
    MMAT_LVARS.  Falls back to a Hexrays_Hooks capture if gen_microcode
    is unavailable or fails.
    """
    pfn = _get_func_or_raise(ea)

    # --- MMAT_LVARS: full decompilation ---
    if maturity >= 7:  # MMAT_LVARS
        cfunc = ida_hexrays.decompile(pfn.start_ea)
        if cfunc is None:
            raise ToolError(f"Decompilation failed for {pfn.start_ea:#x}")
        return cfunc.mba

    # --- Try gen_microcode for intermediate levels ---
    try:
        mbr = ida_hexrays.mba_ranges_t()
        mbr.ranges.push_back(ida_range.range_t(pfn.start_ea, pfn.end_ea))
        hf = ida_hexrays.hexrays_failure_t()
        retlist = ida_hexrays.mlist_t()
        mba = ida_hexrays.gen_microcode(
            mbr,
            hf,
            retlist,
            ida_hexrays.DECOMP_NO_WAIT,
            maturity,
        )
        if mba is not None:
            return mba
    except (AttributeError, RuntimeError) as e:
        log_debug(f"gen_microcode unavailable or failed for {pfn.start_ea:#x}: {e}; using hook capture")

    # --- Fallback: capture via Hexrays_Hooks during decompile() ---
    hook = _MBACapture(maturity)
    hook.hook()
    try:
        ida_hexrays.decompile(pfn.start_ea)
    except (ida_hexrays.DecompilationFailure, RuntimeError) as e:
        log_debug(f"_capture_at_maturity decompile failed (expected for hook capture): {e}")
    finally:
        hook.unhook()

    if hook.captured is not None:
        return hook.captured

    raise ToolError(
        f"Could not generate microcode at {maturity_label(maturity)} for {pfn.start_ea:#x}",
        tool_name="microcode",
    )


# ---------------------------------------------------------------------------
# Tool: get_microcode
# ---------------------------------------------------------------------------


@tool(category="microcode", requires_decompiler=True)
def get_microcode(
    address: Annotated[str, "Function address (hex string, e.g. '0x401000')"],
    maturity: Annotated[str, "Maturity level name or number (0-7)"] = "MMAT_LVARS",
) -> str:
    """Get the microcode for a function at a specific maturity level.

    Maturity levels from lowest (raw) to highest (optimized):
      MMAT_GENERATED (0)    — raw microcode from processor
      MMAT_PREOPTIMIZED (1) — after basic preoptimization
      MMAT_LOCOPT (2)       — after local optimization
      MMAT_CALLS (3)        — after call analysis
      MMAT_GLBOPT1 (4)      — after first global optimization
      MMAT_GLBOPT2 (5)      — after second global optimization
      MMAT_GLBOPT3 (6)      — after third global optimization
      MMAT_LVARS (7)        — final: local variables allocated

    Lower levels show more raw detail (useful for spotting obfuscation
    patterns).  Higher levels show the optimized form closer to the
    decompiler output.
    """
    require_hexrays()
    ea = parse_addr(address)
    mat = parse_maturity(maturity)
    pfn = _get_func_or_raise(ea)

    result = _gen_microcode_at(ea, mat)

    # If the fallback hook returned pre-formatted text instead of mba_t
    if isinstance(result, str):
        header = f"=== Microcode for {func_name(pfn)} at {maturity_label(mat)} ===\n"
        return header + result

    header = (
        f"=== Microcode for {func_name(pfn)} at {maturity_label(mat)} ===\n"
        f"Function: {pfn.start_ea:#x} - {pfn.end_ea:#x}\n"
    )
    return header + format_mba(result)


@tool(category="microcode", requires_decompiler=True)
def get_microcode_block(
    address: Annotated[str, "Function address (hex string)"],
    block_index: Annotated[int, "Block index (0-based)"],
    maturity: Annotated[str, "Maturity level"] = "MMAT_LVARS",
) -> str:
    """Get detailed microcode for a single basic block.

    Use get_microcode() first to see the block layout, then inspect
    individual blocks with this tool for detailed operand information.
    """
    require_hexrays()
    ea = parse_addr(address)
    mat = parse_maturity(maturity)
    pfn = _get_func_or_raise(ea)

    mba_or_text = _gen_microcode_at(ea, mat)
    if isinstance(mba_or_text, str):
        return f"Block detail not available at this maturity (captured via hook).\n{mba_or_text}"

    mba = mba_or_text
    if block_index < 0 or block_index >= mba.qty:
        return f"Block index {block_index} out of range [0, {mba.qty - 1}]"

    blk = mba.get_mblock(block_index)
    lines = [
        f"=== Block {blk.serial} of {func_name(pfn)} at {maturity_label(mat)} ===",
        "",
    ]

    # Block metadata
    preds = [blk.pred(j) for j in range(blk.npred())]
    succs = [blk.succ(j) for j in range(blk.nsucc())]
    lines.append(f"Range: {blk.start:#x} .. {blk.end:#x}")
    lines.append(f"Predecessors: {preds}")
    lines.append(f"Successors:   {succs}")
    try:
        lines.append(f"Block type:   {blk.type}")
    except AttributeError as e:
        log_debug(f"get_microcode_block blk.type unavailable: {e}")
    lines.append("")

    # Instructions with detailed operand dump
    lines.append("Instructions:")
    ins = blk.head
    idx = 0
    while ins is not None:
        ea_str = f"{ins.ea:#010x}" if ins.ea != 0xFFFFFFFFFFFFFFFF else "  --------"
        text = insn_text(ins)
        lines.append(f"  [{idx:3d}] {ea_str}  {text}")

        for label, op in [("L", ins.l), ("R", ins.r), ("D", ins.d)]:
            if op.t != ida_hexrays.mop_z:
                lines.append(f"         {label}: {operand_detail(op)}")

        idx += 1
        if ins == blk.tail:
            break
        ins = ins.next

    lines.append(f"\nTotal: {idx} instructions")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: nop_microcode
# ---------------------------------------------------------------------------


@tool(category="microcode", requires_decompiler=True, mutating=True)
def nop_microcode(
    func_address: Annotated[str, "Address of the function to patch"],
    instruction_addresses: Annotated[
        str,
        "Comma-separated hex addresses of instructions to NOP (e.g. '0x401004,0x401008,0x40100c')",
    ],
    optimizer_name: Annotated[str, "Name for this NOP rule (for later removal)"] = "",
) -> str:
    """NOP out specific microcode instructions and redecompile.

    Installs a persistent instruction optimizer that replaces the
    targeted instructions with m_nop during decompilation.  The
    function is immediately redecompiled to show the cleaned result.

    The optimizer stays active for subsequent redecompilations.
    Use remove_microcode_optimizer() to undo.
    """
    require_hexrays()
    func_ea = parse_addr(func_address)
    pfn = _get_func_or_raise(func_ea)

    # Parse target addresses
    raw_addrs = [s.strip() for s in instruction_addresses.split(",") if s.strip()]
    if not raw_addrs:
        return "No instruction addresses provided."

    target_eas = set()
    for a in raw_addrs:
        target_eas.add(parse_addr(a))

    # Name the optimizer
    name = optimizer_name or f"nop_{pfn.start_ea:#x}_{len(installed_optimizers)}"
    if name in installed_optimizers:
        remove_optimizer(name)

    # Install
    opt = NopOptimizer(name, target_eas, pfn.start_ea)
    opt.install()
    installed_optimizers[name] = opt

    # Redecompile to show result
    try:
        cfunc = ida_hexrays.decompile(pfn.start_ea)
        if cfunc is None:
            return f"Optimizer '{name}' installed (NOPs {len(target_eas)} EAs) but redecompilation failed."
        pseudocode = get_pseudocode_text(cfunc)
    except Exception as e:
        return f"Optimizer '{name}' installed (NOPs {len(target_eas)} EAs) but redecompilation error: {e}"

    return (
        f"Optimizer '{name}' installed \u2014 NOPing {len(target_eas)} instructions.\n"
        f"Applied {opt.applied_count} times during redecompilation.\n"
        f"\n--- Cleaned pseudocode ---\n{pseudocode}"
    )


# ---------------------------------------------------------------------------
# Tool: install_microcode_optimizer
# ---------------------------------------------------------------------------


@tool(category="microcode", requires_decompiler=True, mutating=True, requires_approval=True)
def install_microcode_optimizer(
    name: Annotated[str, "Unique name for this optimizer (used to remove it later)"],
    description: Annotated[str, "What this optimizer does (for list_microcode_optimizers)"],
    optimizer_type: Annotated[
        str,
        "Type: 'instruction' (called per-instruction) or 'block' (called per-block)",
    ],
    python_code: Annotated[
        str,
        "Python code defining an 'optimize' function. "
        "For instruction type: def optimize(blk, ins) -> int. "
        "For block type: def optimize(blk) -> int. "
        "Return the number of changes made (0 = no change). "
        "All ida_hexrays constants (m_nop, m_mov, mop_r, etc.) are available.",
    ],
) -> str:
    """Install a custom microcode optimizer written in Python.

    The optimizer runs during every subsequent decompilation until removed.
    Use redecompile_function() to test it.

    ## Instruction optimizer example (optimizer_type='instruction'):

        def optimize(blk, ins):
            # Remove xor reg, same_reg (dead clearing in obfuscated code)
            if ins.opcode == m_xor:
                if ins.l.t == mop_r and ins.r.t == mop_r and ins.l.r == ins.r.r:
                    ins.opcode = m_nop
                    return 1
            return 0

    ## Block optimizer example (optimizer_type='block'):

        def optimize(blk):
            count = 0
            ins = blk.head
            while ins is not None:
                nxt = ins.next
                # Remove mov reg, reg (self-assignment)
                if ins.opcode == m_mov:
                    if ins.l.t == mop_r and ins.d.t == mop_r:
                        if ins.l.r == ins.d.r and ins.l.size == ins.d.size:
                            blk.make_nop(ins)
                            count += 1
                if ins == blk.tail:
                    break
                ins = nxt
            return count

    ## Available in the optimizer namespace:
    - All ida_hexrays module contents
    - Opcode constants: m_nop, m_mov, m_add, m_sub, m_mul, m_xor, m_and,
      m_or, m_shl, m_shr, m_jcnd, m_jnz, m_jz, m_goto, m_call, m_ret, etc.
    - Operand types: mop_z, mop_r, mop_n, mop_d, mop_S, mop_v, mop_b,
      mop_f, mop_l, mop_a, mop_h, mop_str, mop_c, mop_fn, mop_p, mop_sc
    """
    require_hexrays()

    if name in installed_optimizers:
        return f"Optimizer '{name}' already exists. Remove it first with remove_microcode_optimizer."

    opt_type = optimizer_type.strip().lower()
    if opt_type not in ("instruction", "block"):
        return "optimizer_type must be 'instruction' or 'block'."

    try:
        optimize_fn = compile_optimizer(name, python_code)
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Failed to compile optimizer code: {e}"

    if opt_type == "instruction":
        opt = DynamicInsnOptimizer(name, description, optimize_fn)
    else:
        opt = DynamicBlockOptimizer(name, description, optimize_fn)

    opt.install()
    installed_optimizers[name] = opt

    return (
        f"Optimizer '{name}' ({opt_type}) installed successfully.\n"
        f"Description: {description}\n"
        f"Use redecompile_function() to test it on a function."
    )


# ---------------------------------------------------------------------------
# Tool: remove_microcode_optimizer
# ---------------------------------------------------------------------------


@tool(category="microcode", requires_decompiler=True, mutating=True)
def remove_microcode_optimizer(
    name: Annotated[str, "Name of the optimizer to remove"],
) -> str:
    """Remove a previously installed microcode optimizer."""
    require_hexrays()
    if name not in installed_optimizers:
        available = list(installed_optimizers.keys()) or ["(none)"]
        return f"No optimizer named '{name}'. Active optimizers: {', '.join(available)}"
    remove_optimizer(name)
    return f"Optimizer '{name}' removed."


# ---------------------------------------------------------------------------
# Tool: list_microcode_optimizers
# ---------------------------------------------------------------------------


@tool(category="microcode", requires_decompiler=True)
def list_microcode_optimizers() -> str:
    """List all currently installed microcode optimizers."""
    require_hexrays()
    if not installed_optimizers:
        return "No microcode optimizers installed."

    lines = [f"Installed optimizers ({len(installed_optimizers)}):"]
    for opt_name, opt in installed_optimizers.items():
        kind = "instruction" if isinstance(opt, (NopOptimizer, DynamicInsnOptimizer)) else "block"
        desc = getattr(opt, "description", "")
        if isinstance(opt, NopOptimizer):
            desc = f"NOP {len(opt.target_eas)} addresses (applied {opt.applied_count}x)"
        lines.append(f"  {opt_name} [{kind}] \u2014 {desc}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: redecompile_function
# ---------------------------------------------------------------------------


@tool(category="microcode", requires_decompiler=True)
def redecompile_function(
    address: Annotated[str, "Function address to redecompile (hex string)"],
) -> str:
    """Force redecompilation of a function and return cleaned pseudocode.

    Call this after installing or removing microcode optimizers to see
    the effect on the decompiler output.
    """
    require_hexrays()
    ea = parse_addr(address)
    pfn = _get_func_or_raise(ea)

    # Clear cached decompilation to force re-analysis
    try:
        ida_hexrays.mark_cfunc_dirty(pfn.start_ea)
    except (AttributeError, RuntimeError) as e:
        log_debug(f"redecompile mark_cfunc_dirty unavailable: {e}")

    try:
        cfunc = ida_hexrays.decompile(pfn.start_ea)
    except ida_hexrays.DecompilationFailure as e:
        return f"Decompilation failed for {pfn.start_ea:#x}: {e}"

    if cfunc is None:
        return f"Decompilation returned None for {pfn.start_ea:#x}"

    pseudocode = get_pseudocode_text(cfunc)
    active = list(installed_optimizers.keys())
    status = f"Active optimizers: {', '.join(active)}" if active else "No optimizers active"

    return f"=== Redecompiled {func_name(pfn)} ===\n{status}\n\n{pseudocode}"
