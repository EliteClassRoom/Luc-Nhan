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
  bytes are capped at 16 MiB. The requested code range is always mapped
  executable (packed binaries mark ``.text`` R|W); the synthetic stack is
  the only memory that is always writable and IDB read-only data pages
  stay read-only, so a write to one produces a ``permission_error`` stop
  with the offending address.
* Instruction limit defaults to 100_000 with a 1_000_000 hard cap.

The module exposes pure helpers (``page_align_down``, ``page_align_up``,
``merge_contiguous``, ``coerce_register_value``, ``format_result``) that unit
tests can exercise without a live IDA database or a Unicorn engine.
``_build_mapping_plan`` and ``_decode_string_candidates`` are private but
exercised directly by ``tests/ida/test_emulation.py``.
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

# Bytes left unused at the top of the synthetic stack: the initial ``esp``/
# ``rsp`` points here rather than at the very top, so a routine that pushes
# a small frame before reading its arguments does not run off the mapping.
_STACK_TOP_RESERVED = 0x100

# Internal permission mask: a compact R/W/X encoding used everywhere between
# ``_seg_perms`` (IDA -> internal) and ``_ida_perms_to_unicorn``
# (internal -> ``UC_PROT_*``). The values deliberately coincide with Unicorn's
# ``UC_PROT_READ``/``WRITE``/``EXEC``, but the two are distinct
# representations — never pass an internal mask where a ``UC_PROT_*`` is
# expected.
_PERM_R = 1
_PERM_W = 2
_PERM_X = 4
_PERM_RW = _PERM_R | _PERM_W  # 3 — synthetic stack
_PERM_RWX = _PERM_R | _PERM_W | _PERM_X  # 7 — fallback when IDA is absent

# IDA's own ``SEGPERM_*`` bits, the encoding ``_seg_perms`` translates
# *from*. Note these are the reverse order of the internal mask above:
# IDA's read bit is 4 where the internal read bit is 1.
_IDA_PERM_EXEC = 1
_IDA_PERM_WRITE = 2
_IDA_PERM_READ = 4
_IDA_PERM_MASK = _IDA_PERM_READ | _IDA_PERM_WRITE | _IDA_PERM_EXEC

# Write-preview bounds. The hex preview of a recorded write is masked to at
# most 64 bits so a ``qword`` write does not render a 128-bit-narrow slice of
# garbage, and to at least 8 so a single-byte write still shows one full byte.
_PREVIEW_MAX_BITS = 64
_PREVIEW_MIN_BITS = 8

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


def _coerce_addr(value: Any, *, ctx: str) -> int:
    """Coerce an address the LLM supplied as int, integral float, or hex/decimal string.

    Accepts ``int``, a ``float`` with no fractional part (LLMs routinely emit
    addresses as ``4198400.0`` in JSON), and any string ``int(value, 0)``
    parses (``"0x401000"``, ``"401000"``, ``"0X401000"``). Everything else —
    ``bool`` (an ``int`` subclass, but never a meaningful address), ``None``,
    non-integral floats, lists, unparsable strings — raises ``ToolError``.
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


def _coerce_positive_int(value: Any, *, ctx: str, maximum: int | None = None) -> int:
    """Coerce a positive integer the LLM supplied as int, float, or hex/decimal string.

    Sizes and limits arrive as JSON ints, integral floats, or ``"0x1000"``
    strings depending on the provider — all three are accepted. Anything
    else (bool, ``None``, garbage, negative, non-integral float) is a
    ``ToolError`` naming the offending argument.
    """

    try:
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, int):
            coerced = value
        elif isinstance(value, float) and value.is_integer():
            coerced = int(value)
        elif isinstance(value, str):
            coerced = int(value, 0)
        else:
            raise ValueError
    except (TypeError, ValueError):
        raise ToolError(f"{ctx} must be a positive integer or hex string", tool_name="emulate_code") from None
    if coerced <= 0:
        raise ToolError(f"{ctx} must be positive (got {coerced})", tool_name="emulate_code")
    if maximum is not None and coerced > maximum:
        raise ToolError(f"{ctx} must be in 1..{maximum} (got {coerced})", tool_name="emulate_code")
    return coerced


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

# 32- and 64-bit names alias the same physical register. The runner only
# ever writes canonical-width names, so a user-supplied ``eax`` and ``rax``
# can never clobber each other through Unicorn's alias table. ``r8``-``r15``
# are 64-bit-only and therefore absent from the 32-bit map.
_ALIAS_TO_CANONICAL_64: dict[str, str] = {
    "eax": "rax",
    "ebx": "rbx",
    "ecx": "rcx",
    "edx": "rdx",
    "esi": "rsi",
    "edi": "rdi",
    "ebp": "rbp",
    "esp": "rsp",
    "eflags": "rflags",
}
_ALIAS_TO_CANONICAL_32: dict[str, str] = {
    "rax": "eax",
    "rbx": "ebx",
    "rcx": "ecx",
    "rdx": "edx",
    "rsi": "esi",
    "rdi": "edi",
    "rbp": "ebp",
    "rsp": "esp",
    "rflags": "eflags",
}


def _canonical_registers(ptr_size: int) -> tuple[str, ...]:
    """Physical register names for *ptr_size*, in ``_UNIFIED_REGISTERS`` order.

    4 → ``eax``…``esp``/``eflags``; 8 → ``rax``…``rsp``/``rflags``/``r8``-``r15``.
    """

    return tuple(name for name, width in _UNIFIED_REGISTERS.items() if width == ptr_size)


def _canonical_register_name(name: str, ptr_size: int) -> str:
    """Return the width-*ptr_size* name for *name* (identity when already canonical)."""

    if _UNIFIED_REGISTERS.get(name) == ptr_size:
        return name
    alias_map = _ALIAS_TO_CANONICAL_64 if ptr_size == 8 else _ALIAS_TO_CANONICAL_32
    return alias_map.get(name, name)


def _is_mode_register(name: str, ptr_size: int) -> bool:
    """True if *name* only exists in the other mode (e.g. ``r8`` on x86)."""

    return _UNIFIED_REGISTERS.get(name, 0) != ptr_size and name not in (
        _ALIAS_TO_CANONICAL_64 if ptr_size == 8 else _ALIAS_TO_CANONICAL_32
    )


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
    try:
        return int(ida_ida.inf_get_app_bitness())
    except AttributeError as e:
        # Pre-7.6 IDA Python lacks the symbol entirely.
        raise ToolError(
            f"IDA bitness query failed: {e}",
            tool_name="emulate_code",
        ) from e


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
        perm = int(seg.perm) & _IDA_PERM_MASK
    except (AttributeError, TypeError):
        return _PERM_R  # Read-only fallback
    out = 0
    if perm & _IDA_PERM_READ:
        out |= _PERM_R
    if perm & _IDA_PERM_WRITE:
        out |= _PERM_W
    if perm & _IDA_PERM_EXEC:
        out |= _PERM_X
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
        return _PERM_RWX  # RWX fallback when IDA is missing (test mode)
    perms = _PERM_R
    for ea in (start, (start + end) // 2, end - 1):
        seg = _resolve_segment(ea)
        if seg is not None:
            perms |= _seg_perms(seg)
    return perms or _PERM_R


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
    code_lo = page_align_down(start_address)
    code_hi = page_align_up(stop_address)
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
        if aligned_start < code_hi and aligned_start + size > code_lo:
            # The caller explicitly asked to execute this range, so it is
            # mapped executable even when IDA flags the segment R|W (packed
            # binaries) or unreadable. Data-only ranges keep faithful segment
            # permissions so stray writes still surface as permission_error.
            perms |= _PERM_X
        page_regions.append((aligned_start, size, perms, False))

    page_regions.append((arch.stack_base, _STACK_SIZE, _PERM_RW, True))  # R|W synthetic stack
    total += _STACK_SIZE
    if total > _MAX_MAPPED_IDB_BYTES:
        raise ToolError(
            f"aggregate mapped bytes {total:#x} (including stack) exceed cap",
            tool_name="emulate_code",
        )

    stack_top = arch.stack_base + _STACK_SIZE - _STACK_TOP_RESERVED
    return MappingPlan(
        page_aligned_regions=page_regions,
        total_bytes=total,
        stack_base=arch.stack_base,
        stack_top=stack_top,
    )


def _ida_perms_to_unicorn(perms: int, unicorn: Any) -> int:
    """Convert translated IDA R/W/X mask to a Unicorn ``UC_PROT_*`` value."""

    out = unicorn.UC_PROT_NONE
    if perms & _PERM_R:
        out |= unicorn.UC_PROT_READ
    if perms & _PERM_W:
        out |= unicorn.UC_PROT_WRITE
    if perms & _PERM_X:
        out |= unicorn.UC_PROT_EXEC
    if out == unicorn.UC_PROT_NONE:
        out = unicorn.UC_PROT_READ
    return out


def _write_segment_payloads(
    engine: Any,
    start_address: int,
    stop_address: int,
    extra_ranges: Sequence[tuple[int, int]],
    captures: Sequence[CaptureRequest] = (),
) -> None:
    """Copy the IDB bytes covered by the requested ranges into *engine*.

    *extra_ranges* are ``(address, size)`` pairs — the shape
    ``_normalize_memory_ranges`` produces and both tool entry points forward
    verbatim — and are converted to ``(start, end)`` here so they merge
    correctly with the code range. ``start_address``/``stop_address`` are
    already an end-form range.

    Offsets are **segment-relative**: payloads are read from
    ``segment.start_ea``, so a range that begins mid-segment (a function at
    ``0x408345`` inside a ``0x401000``-based segment) gets the bytes that
    actually live at that address. Each merged region is walked segment by
    segment, so a range spanning adjacent segments is filled from each in
    turn. Capture ranges are filled too — they are frequently the *input*
    buffer (encrypted blob, verifying bytes) the routine reads before
    writing, and leaving them zero-filled returns bogus captures.
    """

    regions: list[tuple[int, int]] = [(start_address, stop_address)]
    regions.extend((int(addr), int(addr) + int(size)) for addr, size in extra_ranges)
    regions.extend((cap.address, cap.address + cap.size) for cap in captures)

    for region_start, region_end in merge_contiguous(regions):
        cursor = region_start
        while cursor < region_end:
            segment = _resolve_segment(cursor)
            if segment is None:
                # No IDB segment covers this address — leave the page as
                # Unicorn mapped it (zero-filled) and resync on a boundary.
                cursor = page_align_up(cursor + 1)
                continue
            segment_start_ea = int(segment.start_ea)
            segment_end = int(segment.end_ea)
            if segment_end <= cursor:
                cursor = page_align_up(cursor + 1)  # defensive: never spin
                continue
            write_end = min(region_end, segment_end)
            payload = _read_segment_bytes(segment)
            if payload is not None:
                # segment-relative — NOT page-relative: payloads are read
                # from segment_start_ea, so a mid-segment entry point indexes
                # the bytes that actually live at that address.
                offset = cursor - segment_start_ea
                chunk = payload[offset : offset + (write_end - cursor)]
                if chunk:
                    try:
                        engine.mem_write(cursor, chunk)
                    except Exception as e:
                        log_debug(f"_write_segment_payloads: write to 0x{cursor:x} failed: {e}")
            cursor = write_end


# ---------------------------------------------------------------------------
# Emulation runner.  No side effects beyond the engine instance; returns a
# fully-populated ``EmulationResult``.  Bounded by ``max_instructions``.
# ---------------------------------------------------------------------------


def _idb_mapped_ranges(plan: MappingPlan) -> list[tuple[int, int, int]]:
    """Mapped IDB regions as ``(start, end, perms)`` for the result block.

    The synthetic stack is excluded — it is runner scaffolding, not a
    user-visible mapping, and reporting it would tell the caller their IDB
    contains a 1 MiB R|W region at ``0x1000000`` that it does not.
    """

    return [(s, s + sz, p) for s, sz, p, is_stack in plan.page_aligned_regions if not is_stack]


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
    """Execute ``[start_address, stop_address)`` and report what happened.

    Phases, in order: build the mapping plan → construct the engine → map
    every region → fill IDB payloads → seed registers → probe the entry point
    for forbidden opcodes → install hooks → ``emu_start`` → read registers
    back → capture the requested ranges.

    Register seeding is safe because *only* user-supplied canonical registers
    are written. A fresh ``Uc`` is already zero-filled, so omitting a register
    never means "clobber it with 0"; ``esp``/``rsp`` and ``eflags``/``rflags``
    get defaults only when the caller omitted them. ``_resolve_register_input``
    has already canonicalised every supplied name to this arch's width, so an
    alias write can no longer overwrite a user value with a zeroed one.

    Every hook that halts the engine writes ``stop_state`` through ``_stop``
    rather than returning a result directly, so a status decided mid-run has
    the same precedence regardless of which hook observed it.
    """

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

    _write_segment_payloads(engine, start_address, stop_address, extra_ranges, captures=captures)

    def _write_reg(reg_id: int, value: int, name: str) -> None:
        """Write *value* to *reg_id*, logging (never raising) on failure.

        A register Unicorn refuses is left as-is; the engine then fails with
        a concrete ``emulator_error`` instead of an opaque construction error
        part-way through seeding.
        """

        try:
            engine.reg_write(reg_id, value)
        except Exception as e:
            log_debug(f"_run: reg_write({name}) failed: {e}")

    def _read_reg(reg_id: int, name: str) -> int | None:
        """Read *reg_id* masked to the arch's pointer width, or None on failure."""

        try:
            return int(engine.reg_read(reg_id)) & ((1 << (8 * arch.ptr_size)) - 1)
        except Exception as e:
            log_debug(f"_run: reg_read({name}) failed: {e}")
            return None

    # Initial registers. ``_resolve_register_input`` already resolved every
    # supplied name to the canonical width for this arch, so writing only
    # those names is safe: a fresh ``Uc`` starts zero-filled, and alias
    # writes (eax/rax) can no longer clobber user values with zeros.
    final_registers: dict[str, int] = {}
    for unified in _canonical_registers(arch.ptr_size):
        value = registers.get(unified)
        if value is None:
            continue
        final_registers[unified] = value
        reg_id = reg_ids.get(unified)
        if reg_id is not None:
            _write_reg(reg_id, value, unified)

    # SP defaults to ``stack_top`` when the caller didn't supply ``esp``/``rsp``.
    ip_reg_id = reg_ids[arch.ip_reg]
    sp_reg_id = reg_ids[arch.sp_reg]
    if final_registers.get(arch.sp_reg) is None:
        final_registers[arch.sp_reg] = plan.stack_top
    _write_reg(sp_reg_id, final_registers[arch.sp_reg], arch.sp_reg)
    flags_reg_id = reg_ids[arch.flags_reg]
    if final_registers.get(arch.flags_reg) is None:
        # Unicorn refuses to run with undefined flags; only default when the
        # caller did not supply one (its value must survive verbatim).
        final_registers[arch.flags_reg] = 0
        _write_reg(flags_reg_id, 0, arch.flags_reg)

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
            mapped_ranges=_idb_mapped_ranges(plan),
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
            mapped_ranges=_idb_mapped_ranges(plan),
            final_registers=final_registers,
        )

    limit = max(1, min(_MAX_INSTRUCTION_LIMIT, int(max_instructions or _DEFAULT_INSTRUCTION_LIMIT)))
    stop_state = {"status": "completed", "reason": f"reached stop address 0x{stop_address:x}", "stop_pc": start_address}
    instruction_count = [0]

    def uc_stop() -> None:
        """Halt the engine; every terminating hook ends with this."""

        engine.emu_stop()

    def _stop(status: str, reason: str, pc: int) -> None:
        """Record a terminal status and halt the engine from inside a hook."""

        stop_state["status"] = status
        stop_state["reason"] = reason
        stop_state["stop_pc"] = pc
        uc_stop()

    def _update_reg_state() -> None:
        # Read back only canonical names: aliases would report the same
        # physical register twice (``eax`` and ``rax`` on x64). A register
        # that fails to read keeps its seeded value rather than vanishing.
        for unified in _canonical_registers(arch.ptr_size):
            reg_id = reg_ids.get(unified)
            if reg_id is None:
                continue
            read_back = _read_reg(reg_id, unified)
            if read_back is not None:
                final_registers[unified] = read_back

    # Hook: code — track instruction count, range exit, instruction limit.
    def code_hook(uc, address, _size, _user):
        instruction_count[0] += 1
        next_ip = _read_reg(ip_reg_id, arch.ip_reg)
        if next_ip is None:
            next_ip = address
        if next_ip < start_address or next_ip >= stop_address:
            _stop("range_exit", f"PC left range at 0x{next_ip:x}", next_ip)
            return
        if instruction_count[0] >= limit:
            _stop("instruction_limit", f"hit instruction limit {limit} at 0x{next_ip:x}", next_ip)

    # Hook: memory-write — record bounded, coalesced changes.
    write_log: list[dict[str, Any]] = []

    def write_hook(uc, _access, address, size, value, _user):
        if len(write_log) < _MAX_WRITE_ENTRIES * 2:
            write_log.append(
                {
                    "address": int(address),
                    "size": int(size),
                    "hex_preview": f"0x{int(value) & ((1 << min(_PREVIEW_MAX_BITS, max(_PREVIEW_MIN_BITS, int(size) * 8))) - 1):x}",
                }
            )

    # Hook: invalid memory or instruction — translate to status.
    def invalid_mem_hook(uc, access, address, _size, _value, _user):
        next_ip = _read_reg(ip_reg_id, arch.ip_reg)
        if next_ip is None:
            next_ip = address
        # Distinguish unmapped vs. permission violations.
        if access & (unicorn.UC_MEM_READ_UNMAPPED | unicorn.UC_MEM_WRITE_UNMAPPED | unicorn.UC_MEM_FETCH_UNMAPPED):
            _stop(
                "unmapped_memory",
                f"unmapped access (0x{int(address):x}) at PC 0x{next_ip:x}",
                next_ip,
            )
        else:
            _stop(
                "permission_error",
                f"permission violation at 0x{int(address):x} (PC 0x{next_ip:x})",
                next_ip,
            )

    def invalid_insn_hook(uc, _user):
        next_ip = _read_reg(ip_reg_id, arch.ip_reg)
        if next_ip is None:
            next_ip = 0
        _stop("unsupported_instruction", f"unsupported instruction at PC 0x{next_ip:x}", next_ip)

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
            read_back = _read_reg(ip_reg_id, arch.ip_reg)
            stop_state["stop_pc"] = start_address if read_back is None else read_back

    if stop_state["status"] == "completed" and stop_state["stop_pc"] == start_address:
        # A completed run finished at ``stop_address``; report the PC the
        # engine actually holds rather than leaving the entry address.
        read_back = _read_reg(ip_reg_id, arch.ip_reg)
        stop_state["stop_pc"] = stop_address if read_back is None else read_back

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

    mapped = _idb_mapped_ranges(plan)
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
    boundary (UTF-16LE data must be two-byte aligned), or falls back to the
    ASCII run when the buffer holds no double-NUL.
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
        size = _coerce_positive_int(item.get("size"), ctx=f"{tool_name}: memory_ranges[{idx}].size")
        out.append((addr, size))
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
        label = item.get("label") or f"{default_label}_{idx}"
        size = _coerce_positive_int(
            item.get("size"),
            ctx=f"{tool_name}: capture_ranges[{idx}].size",
            maximum=_MAX_OUTPUT_BYTES,
        )
        out.append(CaptureRequest(address=addr, size=size, label=str(label)))
    return out


def _resolve_register_input(
    registers: Any,
    *,
    tool_name: str,
    arch: ArchMode,
) -> dict[str, int]:
    """Validate the LLM-supplied register object and coerce the values.

    Supplied names are resolved to the canonical (pointer-width) register of
    the current arch, so ``eax`` and ``rax`` both land on the same physical
    register instead of alias-clobbering each other. Supplying both names
    with different values is rejected as a conflict rather than silently
    picking one.
    """

    if not isinstance(registers, Mapping) or not registers:
        raise ToolError(
            f"{tool_name}: registers must be a non-empty object of explicit initial register values",
            tool_name=tool_name,
        )
    ptr_size = arch.ptr_size
    out: dict[str, int] = {}
    origin: dict[str, str] = {}
    for key, value in registers.items():
        name = str(key).lower()
        if name in ("eip", "rip"):
            # Controlled by start_address on every mode — reject rather than
            # silently ignore, otherwise the caller thinks it took effect.
            # Checked before the whitelist because neither name is a
            # "unified" register we expose.
            raise ToolError(
                f"{tool_name}: eip/rip are taken from start_address and cannot be set",
                tool_name=tool_name,
            )
        if name not in _UNIFIED_REGISTERS:
            raise ToolError(
                f"{tool_name}: unknown register {name!r}",
                tool_name=tool_name,
            )
        if _is_mode_register(name, ptr_size):
            raise ToolError(
                f"{tool_name}: register {name!r} is not available in 32-bit mode",
                tool_name=tool_name,
            )
        canonical = _canonical_register_name(name, ptr_size)
        coerced = coerce_register_value(value, ptr_size)
        if canonical in out and out[canonical] != coerced:
            raise ToolError(
                f"{tool_name}: conflicting values for {canonical!r} via {origin[canonical]!r} and {name!r}",
                tool_name=tool_name,
            )
        out[canonical] = coerced
        origin.setdefault(canonical, name)
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

    start = _coerce_addr(start_address, ctx="emulate_code: start_address")
    stop = _coerce_addr(stop_address, ctx="emulate_code: stop_address")
    bounded_limit = min(
        _MAX_INSTRUCTION_LIMIT,
        _coerce_positive_int(instruction_limit, ctx="emulate_code: instruction_limit"),
    )

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

    start = _coerce_addr(start_address, ctx="resolve_emulated_string: start_address")
    stop = _coerce_addr(stop_address, ctx="resolve_emulated_string: stop_address")
    out_addr = _coerce_addr(output_address, ctx="resolve_emulated_string: output_address")
    out_size = _coerce_positive_int(
        max_output_size,
        ctx="resolve_emulated_string: max_output_size",
        maximum=_MAX_OUTPUT_BYTES,
    )
    bounded_limit = min(
        _MAX_INSTRUCTION_LIMIT,
        _coerce_positive_int(instruction_limit, ctx="resolve_emulated_string: instruction_limit"),
    )

    captures = [
        CaptureRequest(address=out_addr, size=out_size, label="output"),
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
        max_instructions=bounded_limit,
    )
    return format_result(result)
