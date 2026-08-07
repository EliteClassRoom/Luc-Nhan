"""Bounded CPU emulation tools for deobfuscation workflows.

Two read-only tools that wrap a per-call Unicorn engine to execute a
self-contained IDA code range without modifying the IDB or running the
target binary:

* ``emulate_code`` for arbitrary instruction ranges (decoder loops, custom
  crypto stubs, control-flow flattening reconstruction).
* ``resolve_emulated_string`` for the common string-extraction case where
  a known output buffer is captured after the same bounded run.

Design constraints (see plan: ``.kilo/plans/1784279972842-...``):

* The module imports no IDA symbols eagerly and never imports the
  ``unicorn`` SDK at module load. Importing stays lazy until the first
  tool call. If the runtime dependency is missing the tools raise
  ``ToolError`` with an actionable message rather than failing plugin
  startup, but the schema still advertises them.
* Execution is a strict half-open range ``[start, stop)``. Execution
  leaving that range — including the target of a call/branch/jump —
  immediately stops with status ``range_exit`` and a partial result.
* No API/syscall stubs. ``syscall`` / ``sysenter`` / ``int 0x2e`` /
  ``int 0x80`` are detected up front and reported as
  ``unsupported_instruction``. External call/branch targets fall out
  naturally through the ``range_exit`` rule.
* Memory is mapped from real IDA segments that contain the requested
  addresses. Source virtual addresses are preserved; aggregate mapped
  bytes are capped at 16 MiB. The synthetic stack is the only memory
  that is always writable. Writes to read-only IDB mappings produce a
  ``permission_error`` stop with the offending address.
* Instruction limit defaults to 100_000 with a 1_000_000 hard cap.

The module exposes pure helpers (``page_align_down``, ``merge_contiguous``,
``coerce_register_value``, ``_decode_string_candidates``, ``format_result``,
``_build_mapping_plan``) that unit tests can exercise without a live IDA
database or a Unicorn engine.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any

from ...core.errors import ToolError
from ...core.logging import log_debug
from ...tools.base import tool

# ---------------------------------------------------------------------------
# IDA imports — lazy ``importlib`` so the module loads without IDA Pro. The
# tool handlers run on the host's main thread through the existing registry
# dispatch wrapper (see ``rikugan/ida/tools/registry.py``).
# ---------------------------------------------------------------------------

ida_ida = ida_segment = ida_bytes = None
try:
    ida_ida = importlib.import_module("ida_ida")
    ida_segment = importlib.import_module("ida_segment")
    ida_bytes = importlib.import_module("ida_bytes")
except ImportError as e:  # pragma: no cover - exercised in non-IDA tests
    log_debug(f"IDA modules not available for emulation tools: {e}")


# ---------------------------------------------------------------------------
# Plan-mandated constants. Centralized here so tests import one location.
# ---------------------------------------------------------------------------

_PAGE_SIZE = 0x1000

# Aggregate mapped IDA bytes cap. Whole segments are mapped to keep source
# virtual addresses; this bounds the worst case.
_MAX_MAPPED_IDB_BYTES = 16 * 1024 * 1024

# Synthetic stack size.
_STACK_SIZE = 1 * 1024 * 1024

# Default + hard cap on emulator instructions per call.
_DEFAULT_INSTRUCTION_LIMIT = 100_000
_MAX_INSTRUCTION_LIMIT = 1_000_000

# Maximum payload length captured per output range.
_MAX_OUTPUT_BYTES = 4096

# Bounded summary of distinct write events captured in a run.
_MAX_WRITE_ENTRIES = 64

# Syscall / far-control opcodes detected up front and reported as
# ``unsupported_instruction`` before any instruction executes.
_UNSUPPORTED_OPCODE_PREFIXES = (
    b"\x0f\x05",  # syscall
    b"\x0f\x34",  # sysenter
    b"\xcd\x2e",  # int 0x2e (Windows syscall)
    b"\xcd\x80",  # int 0x80 (Linux syscall)
)


# ---------------------------------------------------------------------------
# Data classes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchMode:
    """Resolved Unicorn architecture/mode pair with its IDA source bitness."""

    label: str  # "x86" or "x64"
    arch_const: int  # unicorn.UC_ARCH_X86 (set by ``_resolve_arch``)
    mode_const: int  # unicorn.UC_MODE_32 or UC_MODE_64 (set by ``_resolve_arch``)
    ptr_size: int  # 4 or 8 — used for register-width validation
    ip_reg: str  # "eip" or "rip"
    sp_reg: str  # "esp" or "rsp"
    flags_reg: str  # "eflags" or "rflags"
    stack_base: int  # synthetic stack virtual address


@dataclass
class EmulationResult:
    """Structured return value of the runner (formatted by ``format_result``).

    ``status`` is one of: ``completed``, ``range_exit``, ``instruction_limit``,
    ``unmapped_memory``, ``permission_error``, ``unsupported_instruction``,
    ``emulator_error``.
    """

    status: str = "emulator_error"
    reason: str = "(not executed)"
    entry_pc: int = 0
    stop_pc: int = 0
    instruction_count: int = 0
    architecture: str = "x86"
    mapped_ranges: list[tuple[int, int, int]] = field(default_factory=list)
    final_registers: dict[str, int] = field(default_factory=dict)
    writes: list[dict[str, Any]] = field(default_factory=list)
    captures: dict[str, bytes] = field(default_factory=dict)
    captured_strings: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class CaptureRequest:
    """An output range the runner will read into ``captures`` at the end."""

    address: int
    size: int
    label: str  # human-readable key in the result dict


@dataclass
class MappingPlan:
    """Computed plan of memory regions to map into Unicorn."""

    page_aligned_regions: list[tuple[int, int, int, bool]]
    total_bytes: int
    stack_top: int
    stack_base: int


# ---------------------------------------------------------------------------
# Helpers — pure, no IDA / Unicorn dependency. Imported by unit tests.
# ---------------------------------------------------------------------------


def page_align_down(address: int) -> int:
    """Return *address* rounded down to the nearest page boundary."""

    return address & ~(_PAGE_SIZE - 1)


def page_align_up(address: int) -> int:
    """Return the next page boundary >= *address* (``0`` → ``PAGE_SIZE``)."""

    if address <= 0:
        return _PAGE_SIZE
    return ((address + _PAGE_SIZE - 1) // _PAGE_SIZE) * _PAGE_SIZE


def merge_contiguous(regions: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping or adjacent ``(start, end)`` ranges.

    ``end`` is exclusive. Adjacency is treated as overlap (``a.end == b.start``)
    so page-aligned boundaries collapse into one mapping.
    """

    if not regions:
        return []
    ordered = sorted(((int(s), int(e)) for s, e in regions), key=lambda r: r[0])
    merged: list[list[int]] = [list(ordered[0])]
    for s, e in ordered[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def is_hex_or_int(value: Any) -> bool:
    """True if *value* would round-trip through ``int(value, 0)``."""

    if isinstance(value, bool):  # bool is a subclass of int — reject explicitly
        return False
    if isinstance(value, int):
        return True
    if not isinstance(value, str):
        return False
    try:
        int(value, 0)
        return True
    except (TypeError, ValueError):
        return False


def _coerce_addr(value: Any, *, ctx: str) -> int:
    """Coerce an address the LLM supplied as int, integral float, or hex/decimal string.

    Addresses are always integers; ``is_hex_or_int`` rejects integral floats
    (e.g. ``4198400.0``) which LLMs routinely emit in JSON.
    """
    try:
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            return int(value, 0)
    except (TypeError, ValueError):
        pass
    raise ToolError(f"{ctx} must be an integer or hex string", tool_name="emulate_code")


def _unwrap_range_item(item: Any) -> Any:
    """Unwrap a ``{"$text": "<json>"}`` wrapper.

    Some LLM providers serialize nested structured tool args (lists of
    objects like ``memory_ranges``) as a ``$text`` JSON string. Unwrap it
    so the real object reaches range parsing.
    """
    if isinstance(item, Mapping) and len(item) == 1 and "$text" in item:
        raw = item["$text"]
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (ValueError, TypeError):
                return item
    return item


def coerce_register_value(value: Any, ptr_size: int) -> int:
    """Coerce a register value the LLM supplied as JSON int or hex string.

    Negative Python ints raise ``ToolError`` to make accidental bit-pattern
    mistakes obvious. Values that exceed the register width are masked down
    silently because the LLM routinely sends ``0xFFFFFFFFFFFFFFFF`` for
    32-bit flags where the upper bits are architecturally irrelevant.
    """

    if isinstance(value, bool) or value is None:
        raise ToolError(f"Invalid register value: {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise ToolError(f"Negative register values are not allowed: {value}")
        return value & ((1 << (8 * ptr_size)) - 1)
    if isinstance(value, str):
        try:
            coerced = int(value, 0)
        except (TypeError, ValueError) as e:
            raise ToolError(f"Invalid register value {value!r}: {e}") from e
        if coerced < 0:
            raise ToolError(f"Negative register values are not allowed: {coerced}")
        return coerced & ((1 << (8 * ptr_size)) - 1)
    raise ToolError(f"Invalid register value: {value!r}")


def detect_unsupported_opcodes(code: bytes) -> str | None:
    """Return a precise reason if *code* begins with a forbidden opcode."""

    for prefix in _UNSUPPORTED_OPCODE_PREFIXES:
        if code.startswith(prefix):
            return f"unsupported opcode at entry: {prefix.hex()}"
    return None


def _stack_base_for(ptr_size: int) -> int:
    """Return a synthetic stack base far above any IDB layout."""

    return 0x7FFE_0000 if ptr_size == 4 else 0x7FFE_0000_0000


# ---------------------------------------------------------------------------
# Architecture + register handling.
# ---------------------------------------------------------------------------


# Unified register names exposed to the LLM. Entries are
# ``(unified_name, register_width_bytes)``. ``ip_reg`` and ``sp_reg`` are
# special-cased — the former is set from ``start_address`` and cannot be
# overridden; the latter defaults to ``stack_top`` when omitted.
_UNIFIED_REGISTERS: dict[str, int] = {
    "eax": 4,
    "ebx": 4,
    "ecx": 4,
    "edx": 4,
    "esi": 4,
    "edi": 4,
    "ebp": 4,
    "esp": 4,
    "eflags": 4,
    "rax": 8,
    "rbx": 8,
    "rcx": 8,
    "rdx": 8,
    "rsi": 8,
    "rdi": 8,
    "rbp": 8,
    "rsp": 8,
    "rflags": 8,
    "r8": 8,
    "r9": 8,
    "r10": 8,
    "r11": 8,
    "r12": 8,
    "r13": 8,
    "r14": 8,
    "r15": 8,
}


def _load_unicorn() -> Any:
    """Import the Unicorn SDK or raise an actionable ``ToolError``."""

    try:
        return importlib.import_module("unicorn")
    except ImportError as e:
        raise ToolError(
            "Unicorn CPU emulator is not installed. Install project "
            "dependencies to use emulate_code/resolve_emulated_string: "
            f"{e}",
            tool_name="emulate_code",
        ) from e


def _ida_bitness() -> int:
    """Return binary bitness (16/32/64) via ``ida_ida.inf_get_app_bitness()``.

    IDA 9.x has no ``inf_is_32bit`` module function (only ``inf_is_32bit_exactly`` /
    ``inf_is_32bit_or_higher`` — the legacy ``is_32bit()`` meant "32-or-higher"), and
    ``get_inf_structure()`` was removed in 9.0. ``inf_get_app_bitness()`` (added in
    7.6) returns the exact bitness directly and is the single reliable source.
    """
    if ida_ida is None:
        raise ToolError(
            "IDA bitness API unavailable — cannot resolve x86/x64 mode",
            tool_name="emulate_code",
        )
    return int(ida_ida.inf_get_app_bitness())


def _resolve_arch(unicorn: Any) -> ArchMode:
    """Resolve architecture/mode pair from IDA's inf and known Unicorn consts."""

    if ida_ida is None:
        raise ToolError(
            "IDA bits/architecture not available — cannot resolve x86/x64 mode",
            tool_name="emulate_code",
        )
    try:
        procname = str(ida_ida.inf_get_procname() or "")
    except AttributeError as e:
        raise ToolError(f"IDA processor query failed: {e}", tool_name="emulate_code") from e
    bits = _ida_bitness()
    is_64 = bits == 64
    is_32 = bits == 32

    if procname.lower() not in ("metapc", "pc"):
        raise ToolError(
            f"Unsupported architecture {procname!r}: emulate_code/resolve_emulated_string "
            "currently supports only x86 and x64 IDA databases (metapc processor)",
            tool_name="emulate_code",
        )

    if is_64 and not is_32:
        return ArchMode(
            label="x64",
            arch_const=unicorn.UC_ARCH_X86,
            mode_const=unicorn.UC_MODE_64,
            ptr_size=8,
            ip_reg="rip",
            sp_reg="rsp",
            flags_reg="rflags",
            stack_base=_stack_base_for(8),
        )
    if is_32 and not is_64:
        return ArchMode(
            label="x86",
            arch_const=unicorn.UC_ARCH_X86,
            mode_const=unicorn.UC_MODE_32,
            ptr_size=4,
            ip_reg="eip",
            sp_reg="esp",
            flags_reg="eflags",
            stack_base=_stack_base_for(4),
        )
    raise ToolError(
        "IDA reports neither 32-bit nor 64-bit code — cannot select emulation mode",
        tool_name="emulate_code",
    )


def _register_id_map(unicorn: Any) -> dict[str, int]:
    """Build ``{unified_name: UC_X86_REG_*}`` for all registers we expose.

    Includes ``eip``/``rip`` even though we don't expose it as a unified
    register name — the runner needs the ID to set the entry IP from
    ``start_address``.
    """

    pony = unicorn.x86_const
    out: dict[str, int] = {}
    for unified in _UNIFIED_REGISTERS:
        attr = f"UC_X86_REG_{unified.upper()}"
        reg_id = getattr(pony, attr, None)
        if reg_id is None:
            continue
        out[unified] = int(reg_id)
    for ip_alias in ("eip", "rip"):
        attr = f"UC_X86_REG_{ip_alias.upper()}"
        reg_id = getattr(pony, attr, None)
        if reg_id is not None:
            out[ip_alias] = int(reg_id)
    return out


# ---------------------------------------------------------------------------
# IDA-side helpers (require real IDA; rejected when None during execution).
# ---------------------------------------------------------------------------


def _resolve_segment(ea: int) -> Any | None:
    """Return the IDA segment containing ``ea`` or ``None``."""

    if ida_segment is None:
        return None
    seg = ida_segment.getseg(ea)
    if seg is None:
        return None
    try:
        start = int(seg.start_ea)
        end = int(seg.end_ea)
    except (AttributeError, TypeError):
        return None
    if start <= ea < end:
        return seg
    return None


def _seg_perms(seg: Any) -> int:
    """Translate IDA segment permission bits to a translated R/W/X mask."""

    try:
        perm = int(seg.perm) & 0x7
    except (AttributeError, TypeError):
        return 1  # Read-only fallback
    out = 0
    if perm & 4:
        out |= 1  # R
    if perm & 2:
        out |= 2  # W
    if perm & 1:
        out |= 4  # X
    return out


def _read_segment_bytes(seg: Any) -> bytes | None:
    """Read the whole segment into a ``bytes`` (or ``None`` on failure)."""

    if ida_bytes is None or seg is None:
        return None
    try:
        start = int(seg.start_ea)
        size = int(seg.end_ea) - start
    except (AttributeError, TypeError):
        return None
    if size <= 0 or size > _MAX_MAPPED_IDB_BYTES:
        return None
    try:
        data = ida_bytes.get_bytes(start, size)
    except Exception as exc:
        log_debug(f"_read_segment_bytes failed at 0x{start:x}: {exc}")
        return None
    if data is None or len(data) != size:
        return None
    return bytes(data)


def _pick_permission_for_range(start: int, end: int) -> int:
    """Best-effort permission translation for a planned mapping."""

    if ida_segment is None:
        return 7  # RWX fallback when IDA is missing (test mode)
    perms = 1
    for ea in (start, (start + end) // 2, end - 1):
        seg = _resolve_segment(ea)
        if seg is not None:
            perms |= _seg_perms(seg)
    return perms or 1


def _build_mapping_plan(
    *,
    arch: ArchMode,
    start_address: int,
    stop_address: int,
    extra_ranges: Sequence[tuple[int, int]],
    captures: Sequence[CaptureRequest],
) -> MappingPlan:
    """Compute the page-aligned Unicorn mapping plan and validate constraints."""

    if start_address >= stop_address:
        raise ToolError(
            f"start_address (0x{start_address:x}) must be < stop_address (0x{stop_address:x})",
            tool_name="emulate_code",
        )

    raw_ranges: list[tuple[int, int]] = [(start_address, stop_address)]
    for addr, size in extra_ranges:
        if size <= 0:
            raise ToolError(f"memory range size must be positive: {size}", tool_name="emulate_code")
        raw_ranges.append((int(addr), int(addr) + int(size)))
    for cap in captures:
        if cap.size <= 0 or cap.size > _MAX_OUTPUT_BYTES:
            raise ToolError(
                f"capture range '{cap.label}' size {cap.size} outside 1..{_MAX_OUTPUT_BYTES}",
                tool_name="emulate_code",
            )
        raw_ranges.append((int(cap.address), int(cap.address) + int(cap.size)))

    merged = merge_contiguous(raw_ranges)
    if not merged:
        raise ToolError("No memory ranges resolved for emulation", tool_name="emulate_code")

    page_regions: list[tuple[int, int, int, bool]] = []
    total = 0

    # Page-align each input range first, then merge so adjacent ranges that
    # only touched on page boundaries collapse into a single mapping.
    aligned_pairs = [(page_align_down(start), page_align_up(end)) for start, end in merged]
    for aligned_start, aligned_end in merge_contiguous(aligned_pairs):
        size = aligned_end - aligned_start
        if size <= 0 or size > _MAX_MAPPED_IDB_BYTES:
            raise ToolError(
                f"memory range 0x{aligned_start:x}..0x{aligned_end:x} exceeds {_MAX_MAPPED_IDB_BYTES:#x} page",
                tool_name="emulate_code",
            )
        total += size
        if total > _MAX_MAPPED_IDB_BYTES:
            raise ToolError(
                f"aggregate mapped bytes {total:#x} exceed {_MAX_MAPPED_IDB_BYTES:#x} cap",
                tool_name="emulate_code",
            )
        perms = _pick_permission_for_range(aligned_start, aligned_end)
        page_regions.append((aligned_start, size, perms, False))

    page_regions.append((arch.stack_base, _STACK_SIZE, 3, True))  # R|W synthetic stack
    total += _STACK_SIZE
    if total > _MAX_MAPPED_IDB_BYTES:
        raise ToolError(
            f"aggregate mapped bytes {total:#x} (including stack) exceed cap",
            tool_name="emulate_code",
        )

    stack_top = arch.stack_base + _STACK_SIZE - 0x100
    return MappingPlan(
        page_aligned_regions=page_regions,
        total_bytes=total,
        stack_base=arch.stack_base,
        stack_top=stack_top,
    )


def _ida_perms_to_unicorn(perms: int, unicorn: Any) -> int:
    """Convert translated IDA R/W/X mask to a Unicorn ``UC_PROT_*`` value."""

    out = unicorn.UC_PROT_NONE
    if perms & 1:
        out |= unicorn.UC_PROT_READ
    if perms & 2:
        out |= unicorn.UC_PROT_WRITE
    if perms & 4:
        out |= unicorn.UC_PROT_EXEC
    if out == unicorn.UC_PROT_NONE:
        out = unicorn.UC_PROT_READ
    return out


def _write_segment_payloads(
    engine: Any,
    arch: ArchMode,
    start_address: int,
    stop_address: int,
    extra_ranges: Sequence[tuple[int, int]],
) -> None:
    """Copy the IDB bytes covered by the requested ranges into *engine*."""

    regions = [(start_address, stop_address)]
    regions.extend(extra_ranges)
    for start, end in merge_contiguous(regions):
        seg = _resolve_segment(start)
        if seg is None:
            continue
        payload = _read_segment_bytes(seg)
        if payload is None:
            continue
        va_start = page_align_down(start)
        offset = start - va_start
        length = end - start
        if offset + length > len(payload):
            length = len(payload) - offset
        if length <= 0:
            continue
        try:
            engine.mem_write(start, payload[offset : offset + length])
        except Exception as e:
            log_debug(f"_write_segment_payloads: write to 0x{start:x} failed: {e}")


# ---------------------------------------------------------------------------
# Emulation runner.  No side effects beyond the engine instance; returns a
# fully-populated ``EmulationResult``.  Bounded by ``max_instructions``.
# ---------------------------------------------------------------------------


def _run(
    *,
    arch: ArchMode,
    start_address: int,
    stop_address: int,
    registers: Mapping[str, Any],
    extra_ranges: Sequence[tuple[int, int]],
    captures: Sequence[CaptureRequest],
    max_instructions: int,
) -> EmulationResult:
    plan = _build_mapping_plan(
        arch=arch,
        start_address=start_address,
        stop_address=stop_address,
        extra_ranges=extra_ranges,
        captures=captures,
    )

    unicorn = _load_unicorn()
    reg_ids = _register_id_map(unicorn)
    if arch.ip_reg not in reg_ids or arch.sp_reg not in reg_ids:
        raise ToolError(
            f"Unicorn x86 register constants missing for {arch.label!r}",
            tool_name="emulate_code",
        )

    # Build the engine. Any failure here is a configuration / package issue,
    # not an emulation outcome — surface it as ``ToolError`` so the LLM sees
    # an actionable message instead of a partial ``emulator_error``.
    try:
        engine = unicorn.Uc(arch.arch_const, arch.mode_const)
    except Exception as e:
        raise ToolError(f"Failed to construct Unicorn engine: {e}", tool_name="emulate_code") from e

    # Map every region (IDB pages first, then synthetic stack).
    for start, size, perms, is_stack in plan.page_aligned_regions:
        if is_stack:
            try:
                engine.mem_map(
                    start,
                    size,
                    unicorn.UC_PROT_READ | unicorn.UC_PROT_WRITE,
                )
            except Exception as e:
                raise ToolError(
                    f"Failed to map synthetic stack at 0x{start:x}: {e}",
                    tool_name="emulate_code",
                ) from e
        else:
            try:
                engine.mem_map(start, size, _ida_perms_to_unicorn(perms, unicorn))
            except Exception as e:
                raise ToolError(
                    f"Failed to map IDB page 0x{start:x}..0x{start + size:x}: {e}",
                    tool_name="emulate_code",
                ) from e

    _write_segment_payloads(engine, arch, start_address, stop_address, extra_ranges)

    # Initial registers.
    final_registers: dict[str, int] = {}
    for unified, width in _UNIFIED_REGISTERS.items():
        value = registers.get(unified)
        if value is None:
            coerced = 0
        else:
            coerced = coerce_register_value(value, arch.ptr_size)
        # On x86, the 64-bit-only registers don't exist — skip them.
        if (
            arch.ptr_size == 4
            and width == 8
            and unified
            in {
                "r8",
                "r9",
                "r10",
                "r11",
                "r12",
                "r13",
                "r14",
                "r15",
                "rflags",
            }
        ):
            continue
        final_registers[unified] = coerced
        reg_id = reg_ids.get(unified)
        if reg_id is None:
            continue
        try:
            engine.reg_write(reg_id, coerced)
        except Exception:
            pass  # ignored — Unicorn will not run if a required register is invalid.

    # SP defaults to ``stack_top`` when the caller didn't supply ``esp``/``rsp``.
    sp_value = final_registers.get(arch.sp_reg)
    if sp_value is None or sp_value == 0:
        sp_value = plan.stack_top
        final_registers[arch.sp_reg] = sp_value
    sp_reg_id = reg_ids[arch.sp_reg]
    try:
        engine.reg_write(sp_reg_id, sp_value)
    except Exception:
        pass

    ip_reg_id = reg_ids[arch.ip_reg]
    flags_reg_id = reg_ids[arch.flags_reg]
    try:
        engine.reg_write(flags_reg_id, 0)
    except Exception:
        pass

    # Detect forbidden opcodes up front.
    try:
        entry_bytes = bytes(engine.mem_read(start_address, 4))
    except Exception as exc:
        return EmulationResult(
            status="emulator_error",
            reason=f"could not read entry opcode at 0x{start_address:x}: {exc}",
            entry_pc=start_address,
            stop_pc=start_address,
            instruction_count=0,
            architecture=arch.label,
            mapped_ranges=[(s, s + sz, p) for s, sz, p, is_stack in plan.page_aligned_regions if not is_stack],
            final_registers=final_registers,
        )
    forbidden = detect_unsupported_opcodes(entry_bytes)
    if forbidden is not None:
        return EmulationResult(
            status="unsupported_instruction",
            reason=forbidden,
            entry_pc=start_address,
            stop_pc=start_address,
            instruction_count=0,
            architecture=arch.label,
            mapped_ranges=[(s, s + sz, p) for s, sz, p, is_stack in plan.page_aligned_regions if not is_stack],
            final_registers=final_registers,
        )

    limit = max(1, min(_MAX_INSTRUCTION_LIMIT, int(max_instructions or _DEFAULT_INSTRUCTION_LIMIT)))
    stop_state = {"status": "completed", "reason": f"reached stop address 0x{stop_address:x}", "stop_pc": start_address}
    instruction_count = [0]

    def _update_reg_state() -> None:
        for unified in _UNIFIED_REGISTERS:
            reg_id = reg_ids.get(unified)
            if reg_id is None:
                continue
            if arch.ptr_size == 4 and unified in {
                "r8",
                "r9",
                "r10",
                "r11",
                "r12",
                "r13",
                "r14",
                "r15",
                "rflags",
            }:
                continue
            try:
                final_registers[unified] = int(engine.reg_read(reg_id)) & ((1 << (8 * arch.ptr_size)) - 1)
            except Exception:
                pass

    # Hook: code — track instruction count, range exit, instruction limit.
    def code_hook(uc, address, _size, _user):
        instruction_count[0] += 1
        try:
            next_ip = int(engine.reg_read(ip_reg_id))
        except Exception:
            next_ip = address
        if next_ip < start_address or next_ip >= stop_address:
            stop_state["status"] = "range_exit"
            stop_state["reason"] = f"PC left range at 0x{next_ip:x}"
            stop_state["stop_pc"] = next_ip
            uc.emu_stop()
            return
        if instruction_count[0] >= limit:
            stop_state["status"] = "instruction_limit"
            stop_state["reason"] = f"hit instruction limit {limit} at 0x{next_ip:x}"
            stop_state["stop_pc"] = next_ip
            uc.emu_stop()

    # Hook: memory-write — record bounded, coalesced changes.
    write_log: list[dict[str, Any]] = []

    def write_hook(uc, _access, address, size, value, _user):
        if len(write_log) < _MAX_WRITE_ENTRIES * 2:
            write_log.append(
                {
                    "address": int(address),
                    "size": int(size),
                    "hex_preview": f"0x{int(value) & ((1 << min(64, max(8, int(size) * 8))) - 1):x}",
                }
            )

    # Hook: invalid memory or instruction — translate to status.
    def invalid_mem_hook(uc, access, address, _size, _value, _user):
        try:
            next_ip = int(engine.reg_read(ip_reg_id))
        except Exception:
            next_ip = address
        # Distinguish unmapped vs. permission violations.
        if access & (unicorn.UC_MEM_READ_UNMAPPED | unicorn.UC_MEM_WRITE_UNMAPPED | unicorn.UC_MEM_FETCH_UNMAPPED):
            stop_state["status"] = "unmapped_memory"
            stop_state["reason"] = f"unmapped access (0x{int(address):x}) at PC 0x{next_ip:x}"
        else:
            stop_state["status"] = "permission_error"
            stop_state["reason"] = f"permission violation at 0x{int(address):x} (PC 0x{next_ip:x})"
        stop_state["stop_pc"] = next_ip
        uc.emu_stop()

    def invalid_insn_hook(uc, _user):
        try:
            next_ip = int(engine.reg_read(ip_reg_id))
        except Exception:
            next_ip = 0
        stop_state["status"] = "unsupported_instruction"
        stop_state["reason"] = f"unsupported instruction at PC 0x{next_ip:x}"
        stop_state["stop_pc"] = next_ip
        uc.emu_stop()

    try:
        engine.hook_add(unicorn.UC_HOOK_CODE, code_hook)
        engine.hook_add(unicorn.UC_HOOK_MEM_WRITE, write_hook)
        engine.hook_add(
            unicorn.UC_HOOK_MEM_READ_UNMAPPED
            | unicorn.UC_HOOK_MEM_WRITE_UNMAPPED
            | unicorn.UC_HOOK_MEM_FETCH_UNMAPPED
            | unicorn.UC_HOOK_MEM_READ_PROT
            | unicorn.UC_HOOK_MEM_WRITE_PROT
            | unicorn.UC_HOOK_MEM_FETCH_PROT,
            invalid_mem_hook,
        )
        engine.hook_add(unicorn.UC_HOOK_INSN_INVALID, invalid_insn_hook)
    except Exception as e:
        raise ToolError(f"Failed to register Unicorn hooks: {e}", tool_name="emulate_code") from e

    try:
        engine.emu_start(start_address, stop_address, timeout=0, count=limit + 1)
    except Exception as e:
        # UcError reached here only after our hooks already stopped the
        # engine, so ``stop_state`` is authoritative. Fall back to a
        # generic ``emulator_error`` only if state is still ``completed``.
        if stop_state["status"] == "completed":
            stop_state["status"] = "emulator_error"
            stop_state["reason"] = f"Unicorn raised {type(e).__name__}: {e}"
            try:
                stop_state["stop_pc"] = int(engine.reg_read(ip_reg_id))
            except Exception:
                stop_state["stop_pc"] = start_address

    _update_reg_state()

    # Capture ranges.
    captures_out: dict[str, bytes] = {}
    cap_strings: dict[str, dict[str, Any]] = {}
    for cap in captures:
        try:
            payload = bytes(engine.mem_read(cap.address, cap.size))
        except Exception as exc:
            payload = b""
            write_log.append(
                {
                    "address": int(cap.address),
                    "size": 0,
                    "hex_preview": f"capture failed: {exc}",
                }
            )
        captures_out[cap.label] = payload
        cap_strings[cap.label] = _decode_string_candidates(payload)

    mapped = [(s, s + sz, p) for s, sz, p, is_stack in plan.page_aligned_regions if not is_stack]
    return EmulationResult(
        status=stop_state["status"],
        reason=stop_state["reason"],
        entry_pc=start_address,
        stop_pc=int(stop_state["stop_pc"]),
        instruction_count=instruction_count[0],
        architecture=arch.label,
        mapped_ranges=mapped,
        final_registers=final_registers,
        writes=write_log[:_MAX_WRITE_ENTRIES],
        captures=captures_out,
        captured_strings=cap_strings,
    )


# ---------------------------------------------------------------------------
# Result formatting & string decoding helpers (pure, no Unicorn / IDA).
# ---------------------------------------------------------------------------


_HEX_CHUNK = 16


def _hex_dump(data: bytes, max_bytes: int = _MAX_OUTPUT_BYTES) -> str:
    if not data:
        return "(empty)"
    clipped = data[:max_bytes]
    lines: list[str] = []
    for off in range(0, len(clipped), _HEX_CHUNK):
        row = clipped[off : off + _HEX_CHUNK]
        hex_part = " ".join(f"{b:02x}" for b in row)
        ascii_part = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in row)
        lines.append(f"  0x{off:04x}  {hex_part:<48s} |{ascii_part}|")
    if len(data) > max_bytes:
        lines.append(f"  ... (truncated, total {len(data)} bytes)")
    return "\n".join(lines)


def _is_printable_ascii(byte: int) -> bool:
    return 0x20 <= byte < 0x7F


def _decode_string_candidates(data: bytes) -> dict[str, Any]:
    """Decode ASCII/UTF-8/UTF-16LE candidates from a captured buffer.

    NUL terminators: ``has_nul_terminator`` is ``True`` if the buffer ends
    with a single byte ``0x00`` *or* a UTF-16LE double-NUL. ASCII / UTF-8
    candidates are sliced at the first single-byte NUL; the UTF-16LE
    candidate is sliced at the first double-NUL aligned on an even byte
    boundary (UTF-16LE data must be two-byte aligned). When the leading
    byte of the wide candidate is itself ``0x00`` (an empty wide string),
    we fall back to ``ascii_run`` so the candidate stays useful.
    """

    nul = data.find(b"\x00")
    has_nul = nul >= 0
    wide_nul = data.find(b"\x00\x00")
    if wide_nul > 0 and wide_nul % 2 == 1:
        # Realign to the next even boundary so ``decode("utf-16le")`` does
        # not crash on odd-length payloads.
        wide_nul += 1
    has_wide_nul = wide_nul >= 0
    ascii_run = data[:nul] if has_nul else data
    utf8_run = ascii_run
    if 0 <= wide_nul < len(data):
        wide_payload = data[:wide_nul]
    else:
        wide_payload = ascii_run
    return {
        "raw_length": len(data),
        "has_nul_terminator": has_nul or has_wide_nul,
        "ascii": "".join(chr(b) if _is_printable_ascii(b) else "?" for b in ascii_run),
        "utf8": _safe_decode(utf8_run, "utf-8"),
        "utf16le": _safe_decode(wide_payload, "utf-16le"),
    }


def _safe_decode(payload: bytes, encoding: str) -> str:
    try:
        return payload.decode(encoding, errors="strict")
    except UnicodeDecodeError:
        return ""


def _indent(text: str, *, prefix: str) -> str:
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def format_result(result: EmulationResult) -> str:
    """Render an ``EmulationResult`` into a labelled multi-section string."""

    arch = result.architecture.upper()
    lines = [
        f"=== Unicorn emulation ({arch}) ===",
        f"Status: {result.status}",
        f"Reason: {result.reason}",
        f"Entry PC: 0x{result.entry_pc:x}",
        f"Stop PC:  0x{result.stop_pc:x}",
        f"Instructions executed: {result.instruction_count}",
    ]

    if result.mapped_ranges:
        lines.append("")
        lines.append("Mapped ranges:")
        for start, end, perms in result.mapped_ranges:
            lines.append(f"  0x{start:x}-0x{end:x}  perms={perms}")

    if result.final_registers:
        lines.append("")
        lines.append("Final registers:")
        for name in sorted(result.final_registers):
            lines.append(f"  {name} = 0x{result.final_registers[name]:x}")

    if result.writes:
        lines.append("")
        lines.append(f"Write events ({min(len(result.writes), _MAX_WRITE_ENTRIES)} shown):")
        for entry in result.writes[:_MAX_WRITE_ENTRIES]:
            lines.append(f"  0x{entry['address']:x}  size={entry['size']}  preview={entry['hex_preview']}")
        if len(result.writes) > _MAX_WRITE_ENTRIES:
            lines.append(f"  ... ({len(result.writes) - _MAX_WRITE_ENTRIES} more)")

    if result.captures:
        lines.append("")
        lines.append("Captured output:")
        for label, data in result.captures.items():
            lines.append(f"  [{label}] raw bytes ({len(data)}):")
            lines.append(_indent(_hex_dump(data), prefix="    "))
            meta = result.captured_strings.get(label) or _decode_string_candidates(data)
            lines.append(f"    ascii='{meta['ascii']}'")
            if meta["utf8"] and meta["utf8"] != meta["ascii"]:
                lines.append(f"    utf8='{meta['utf8']}'")
            if meta["utf16le"]:
                lines.append(f"    utf16le='{meta['utf16le']}'")
            lines.append(f"    terminated={meta['has_nul_terminator']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool-call argument normalisation.
# ---------------------------------------------------------------------------


def _normalize_memory_ranges(
    memory_ranges: Sequence[Any],
    *,
    tool_name: str,
) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for idx, item in enumerate(memory_ranges or ()):
        item = _unwrap_range_item(item)
        if not isinstance(item, Mapping):
            raise ToolError(
                f"{tool_name}: memory_ranges[{idx}] must be an object with 'address' and 'size'",
                tool_name=tool_name,
            )
        addr = _coerce_addr(item.get("address"), ctx=f"{tool_name}: memory_ranges[{idx}].address")
        size = item.get("size")
        if not isinstance(size, int) or size <= 0:
            raise ToolError(
                f"{tool_name}: memory_ranges[{idx}].size must be a positive integer",
                tool_name=tool_name,
            )
        out.append((addr, int(size)))
    return out


def _normalize_capture_ranges(
    capture_ranges: Sequence[Any],
    *,
    tool_name: str,
    default_label: str,
) -> list[CaptureRequest]:
    out: list[CaptureRequest] = []
    for idx, item in enumerate(capture_ranges or ()):
        item = _unwrap_range_item(item)
        if not isinstance(item, Mapping):
            raise ToolError(
                f"{tool_name}: capture_ranges[{idx}] must be an object with 'address' and 'size'",
                tool_name=tool_name,
            )
        addr = _coerce_addr(item.get("address"), ctx=f"{tool_name}: capture_ranges[{idx}].address")
        size = item.get("size")
        label = item.get("label") or f"{default_label}_{idx}"
        if not isinstance(size, int) or size <= 0 or size > _MAX_OUTPUT_BYTES:
            raise ToolError(
                f"{tool_name}: capture_ranges[{idx}].size must be in 1..{_MAX_OUTPUT_BYTES}",
                tool_name=tool_name,
            )
        out.append(CaptureRequest(address=addr, size=int(size), label=str(label)))
    return out


def _resolve_register_input(
    registers: Any,
    *,
    tool_name: str,
    arch: ArchMode,
) -> dict[str, int]:
    """Validate the LLM-supplied register object and coerce the values."""

    if not isinstance(registers, Mapping) or not registers:
        raise ToolError(
            f"{tool_name}: registers must be a non-empty object of explicit initial register values",
            tool_name=tool_name,
        )
    out: dict[str, Any] = {}
    for key, value in registers.items():
        name = str(key).lower()
        if name not in _UNIFIED_REGISTERS:
            raise ToolError(
                f"{tool_name}: unknown register {name!r}",
                tool_name=tool_name,
            )
        if name == arch.ip_reg:
            # ``eip`` / ``rip`` are controlled by start_address.
            continue
        out[name] = coerce_register_value(value, arch.ptr_size)
    return out


# ---------------------------------------------------------------------------
# Public tools. The Unicorn SDK is loaded lazily inside the runner so the
# module can be imported in environments without the dependency and the
# registry can still advertise both tool schemas.
# ---------------------------------------------------------------------------


@tool(category="emulation", timeout=30.0)
def emulate_code(
    start_address: Annotated[str, "First instruction to execute (inclusive hex address)"],
    stop_address: Annotated[
        str,
        "Exclusive emulation end — execution stops BEFORE this address",
    ],
    registers: Annotated[
        dict,
        "Explicit initial CPU register state. Keys are x86/x64 register names "
        "(eax/ebx/rax/r8/eflags/etc.), values are integers or '0x'-style hex "
        "strings. eip/rip are taken from start_address and cannot be overridden.",
    ],
    memory_ranges: Annotated[
        list[dict],
        "Optional extra IDB address ranges to map (encrypted input, key, "
        "lookup tables). Each entry is {address, size} where address is an "
        "int or '0x' hex string and size is a positive integer.",
    ] = (),
    capture_ranges: Annotated[
        list[dict],
        "Optional output buffers to read back at the end of emulation. "
        "Each entry is {address, size}; maximum 4096 bytes per capture.",
    ] = (),
    instruction_limit: Annotated[
        int,
        "Upper bound on instructions executed (default 100000, hard cap 1000000).",
    ] = _DEFAULT_INSTRUCTION_LIMIT,
) -> str:
    """Run a bounded, read-only Unicorn emulation of a self-contained IDA code range.

    Returns partial state — registers, instruction count, mapped ranges, write
    events, and bytes from any output buffers — plus a precise stop reason.
    Never modifies the IDB, follows no external calls, and does not auto-add
    API stubs or syscall handlers.

    Status values: ``completed``, ``range_exit``, ``instruction_limit``,
    ``unmapped_memory``, ``permission_error``, ``unsupported_instruction``,
    ``emulator_error``.
    """

    unicorn = _load_unicorn()
    arch = _resolve_arch(unicorn)

    try:
        start = int(start_address, 0)
        stop = int(stop_address, 0)
    except (TypeError, ValueError) as e:
        raise ToolError(
            f"emulate_code: start_address/stop_address must be int or hex strings: {e}",
            tool_name="emulate_code",
        ) from e

    if instruction_limit <= 0:
        raise ToolError("emulate_code: instruction_limit must be positive", tool_name="emulate_code")
    bounded_limit = min(_MAX_INSTRUCTION_LIMIT, int(instruction_limit))

    normalised_regs = _resolve_register_input(registers, tool_name="emulate_code", arch=arch)
    extras = _normalize_memory_ranges(memory_ranges, tool_name="emulate_code")
    captures = _normalize_capture_ranges(capture_ranges, tool_name="emulate_code", default_label="output")

    result = _run(
        arch=arch,
        start_address=start,
        stop_address=stop,
        registers=normalised_regs,
        extra_ranges=extras,
        captures=captures,
        max_instructions=bounded_limit,
    )
    return format_result(result)


@tool(category="emulation", timeout=30.0)
def resolve_emulated_string(
    start_address: Annotated[str, "First instruction to execute (inclusive hex address)"],
    stop_address: Annotated[
        str,
        "Exclusive emulation end — execution stops BEFORE this address",
    ],
    registers: Annotated[
        dict,
        "Explicit initial CPU register state (see emulate_code). eip/rip are always taken from start_address.",
    ],
    output_address: Annotated[
        str,
        "Address of the decoded-string output buffer (int or '0x' hex string).",
    ],
    max_output_size: Annotated[
        int,
        "Maximum bytes to scan for NUL terminators (default 4096, hard cap 4096).",
    ] = _MAX_OUTPUT_BYTES,
    memory_ranges: Annotated[
        list[dict],
        "Optional extra IDB address ranges to map (encrypted input, key, lookup).",
    ] = (),
    instruction_limit: Annotated[
        int,
        "Upper bound on instructions executed (default 100000, hard cap 1000000).",
    ] = _DEFAULT_INSTRUCTION_LIMIT,
) -> str:
    """Convenience wrapper around :func:`emulate_code` for decoded-string extraction.

    Runs the same bounded execution with an explicit output buffer and returns
    the raw bytes plus ASCII / UTF-8 / UTF-16LE candidate strings, plus the
    same status block ``emulate_code`` would report.
    """

    unicorn = _load_unicorn()
    arch = _resolve_arch(unicorn)

    try:
        start = int(start_address, 0)
        stop = int(stop_address, 0)
        out_addr = int(output_address, 0)
    except (TypeError, ValueError) as e:
        raise ToolError(
            f"resolve_emulated_string: start_address/stop_address/output_address must be int or hex strings: {e}",
            tool_name="resolve_emulated_string",
        ) from e

    if not (1 <= int(max_output_size) <= _MAX_OUTPUT_BYTES):
        raise ToolError(
            f"resolve_emulated_string: max_output_size must be in 1..{_MAX_OUTPUT_BYTES}",
            tool_name="resolve_emulated_string",
        )
    if instruction_limit <= 0:
        raise ToolError(
            "resolve_emulated_string: instruction_limit must be positive",
            tool_name="resolve_emulated_string",
        )

    captures = [
        CaptureRequest(address=out_addr, size=int(max_output_size), label="output"),
    ]

    normalised_regs = _resolve_register_input(registers, tool_name="resolve_emulated_string", arch=arch)
    extras = _normalize_memory_ranges(memory_ranges, tool_name="resolve_emulated_string")

    result = _run(
        arch=arch,
        start_address=start,
        stop_address=stop,
        registers=normalised_regs,
        extra_ranges=extras,
        captures=captures,
        max_instructions=min(_MAX_INSTRUCTION_LIMIT, int(instruction_limit)),
    )
    return format_result(result)
