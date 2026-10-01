"""Bounded CPU emulation tools for deobfuscation workflows.

Two read-only tools that wrap a per-call Unicorn engine to execute a
self-contained region of an IDA database without modifying the IDB or
running the target binary:

* ``emulate_code`` for instruction ranges (decoder loops, custom crypto
  stubs, control-flow flattening reconstruction) and for whole functions
  through an explicit calling convention.
* ``resolve_emulated_string`` for the common string-extraction case where
  a known output buffer is captured after the same bounded run.

Design constraints:

* The module imports no IDA symbols eagerly and never imports the
  ``unicorn`` SDK at module load. If the runtime dependency is missing
  the tools raise ``ToolError`` with an actionable message rather than
  failing plugin startup, but the schema still advertises them.
* The two tools opt out of the registry's outer host dispatch
  (``main_thread=False``). They snapshot architecture + memory on the
  host thread via :func:`run_on_host_thread` and run Unicorn on the
  worker that the registry already handed them. No IDA handle, segment
  object or ``ida_*`` module reference survives into the CPU phase.
* No API/syscall stubs. Every ``syscall`` / ``sysenter`` / ``int imm8`` /
  ``int3`` / ``int1`` / ``into`` / ``ud2`` is rejected *before it executes* —
  including behind an instruction prefix (REX only in 64-bit mode, where
  0x40-0x4f are prefixes; in 32-bit mode the same bytes are ``inc``/``dec``
  and run normally). A blocked instruction is not counted as executed.
* Memory comes from the IDA snapshot
  (:func:`rikugan.ida.tools.emulation_memory.snapshot_memory`): exact
  segment-intersection bytes, segment permissions kept faithful except that
  the requested code pages (entry range + ``code_ranges``) gain X (and R) so
  packed-binary ``.text`` marked R|W still runs — W is never added; plus a
  writable synthetic stack and explicitly declared non-executable scratch
  buffers. Reads/writes into page padding are denied instead of silently
  zero-filled.
* Runtime is bounded by an instruction budget and a real wall-clock
  deadline that starts before the host-side memory snapshot. A timeout or a
  cancellation never claims completion: when the CPU phase was initialised it
  returns registers, captures and discoveries, and when the snapshot itself
  was aborted there is no memory state to report, so the result carries only
  the status and the reason.
"""

from __future__ import annotations

import importlib
import json
import math
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from rikugan.core.errors import ToolError
from rikugan.core.logging import log_debug
from rikugan.ida.tools.emulation_memory import SnapshotAborted, snapshot_memory
from rikugan.ida.tools.emulation_output import (
    decode_string_candidates,
    extract_strings,
    format_result,
)
from rikugan.ida.tools.emulation_types import (
    ArchMode,
    CaptureRequest,
    EmulationResult,
    MemoryBuffer,
    MemorySnapshot,
)
from rikugan.tools.base import tool
from rikugan.tools.execution import get_execution_context, run_on_host_thread

# ---------------------------------------------------------------------------
# IDA imports — lazy ``importlib`` so the module loads without IDA Pro.
# Only the architecture probe below touches them, and it always runs on
# the host thread.
# ---------------------------------------------------------------------------

ida_ida = None
try:
    ida_ida = importlib.import_module("ida_ida")
except ImportError as e:  # pragma: no cover - exercised in non-IDA tests
    log_debug(f"IDA modules not available for emulation tools: {e}")

# ---------------------------------------------------------------------------
# Limits. Centralized so tests import one location.
# ---------------------------------------------------------------------------

_PAGE_SIZE = 0x1000

# Synthetic stack size (also the aggregate cap contributor).
_STACK_SIZE = 1 * 1024 * 1024

# Aggregate mapped bytes cap, stack included.
_MAX_MAPPED_BYTES = 16 * 1024 * 1024

# Default + hard cap on emulator instructions per call.
_DEFAULT_INSTRUCTION_LIMIT = 100_000
_MAX_INSTRUCTION_LIMIT = 1_000_000

# Default + hard cap on the wall-clock runtime budget (seconds).
_DEFAULT_TIMEOUT_SECONDS = 5.0
_MAX_TIMEOUT_SECONDS = 20.0

# Maximum payload length captured per output range / capture count.
_MAX_OUTPUT_BYTES = 4096
_MAX_CAPTURES = 16

# Bounded write-event summary; the *total* is reported separately as
# ``EmulationResult.write_event_count``.
_MAX_WRITE_ENTRIES = 64

# Discovery cap for ``collect_strings`` plus its scan budget.
_MAX_DISCOVERY_CANDIDATES = 64
_DISCOVERY_MARGIN = 64  # unchanged bytes kept around a changed neighbourhood
_DISCOVERY_WINDOW_CANDIDATES = 64  # per-window cap before filtering
_DISCOVERY_SCAN_CAP = 256 * 1024  # total bytes decoded across all windows

# Execution modes.
_MODE_RANGE = "range"
_MODE_FUNCTION = "function"

# Syscall / system-instruction opcodes rejected before they execute. Every
# ``int`` form is refused, plus ``int3`` (0xcc), ``int1`` (0xf1) and ``into``
# (0xce), which are traps or only conditionally trap. ``syscall`` /
# ``sysenter`` may sit behind instruction prefixes, so the check walks the
# prefix chain rather than looking at the entry bytes only.
_SYSCALL_OPCODES = (b"\x0f\x05", b"\x0f\x34")

# Opcodes that are architecturally invalid on x86 and would fault inside the
# engine instead of running: ``ud2`` is the common deliberate trap.
_INVALID_OPCODES = (b"\x0f\x0b",)

# Instruction prefixes skipped before the opcode is inspected. 0x40-0x4F are
# legacy prefixes too; in 64-bit mode they are REX, and in 32-bit mode they
# are ``inc``/``dec`` — real instructions the scan must not skip.
_PREFIX_BYTES = frozenset({0x66, 0x67, 0xF0, 0xF2, 0xF3, 0x2E, 0x3E, 0x26, 0x36, 0x64, 0x65})

# Bytes that are prefixes only in 64-bit mode (REX).
_REX_BYTES = frozenset(range(0x40, 0x50))

_MAX_INSN_BYTES = 15

# ---------------------------------------------------------------------------
# Architecture resolution (IDA-backed; runs on the host thread).
# ---------------------------------------------------------------------------


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

    IDA 9.x has no ``inf_is_32bit`` module function and removed
    ``get_inf_structure()``; ``inf_get_app_bitness()`` (added in 7.6)
    returns the exact bitness directly.
    """
    if ida_ida is None:
        raise ToolError(
            "IDA bitness API unavailable — cannot resolve x86/x64 mode",
            tool_name="emulate_code",
        )
    try:
        return int(ida_ida.inf_get_app_bitness())
    except AttributeError as e:
        raise ToolError(
            f"IDA bitness query failed: {e}",
            tool_name="emulate_code",
        ) from e


def _stack_base_for(ptr_size: int) -> int:
    """Return a synthetic stack base far above any IDB layout."""

    return 0x7FFE_0000 if ptr_size == 4 else 0x7FFE_0000_0000


def _resolve_arch(unicorn: Any) -> ArchMode:
    """Resolve architecture/mode pair from IDA's inf and Unicorn consts."""

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

    if procname.lower() not in ("metapc", "pc"):
        raise ToolError(
            f"Unsupported architecture {procname!r}: emulate_code/resolve_emulated_string "
            "currently supports only x86 and x64 IDA databases (metapc processor)",
            tool_name="emulate_code",
        )

    if bits == 64:
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
    if bits == 32:
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
        f"IDA reports {bits}-bit code — emulate_code/resolve_emulated_string "
        "support only x86 (32-bit) and x64 databases",
        tool_name="emulate_code",
    )


def _register_id_map(unicorn: Any) -> dict[str, int]:
    """Build ``{register_name: UC_X86_REG_*}`` for every register we touch."""

    pony = unicorn.x86_const
    out: dict[str, int] = {}
    for name in _REGISTER_SLOTS["x86"] | _REGISTER_SLOTS["x64"]:
        reg_id = getattr(pony, f"UC_X86_REG_{name.upper()}", None)
        if reg_id is not None:
            out[name] = int(reg_id)
    for ip_alias in ("eip", "rip"):
        reg_id = getattr(pony, f"UC_X86_REG_{ip_alias.upper()}", None)
        if reg_id is not None:
            out[ip_alias] = int(reg_id)
    return out


# ---------------------------------------------------------------------------
# Register names. Every alias the tools accept maps onto one native
# register; conflicting alias values are rejected instead of being
# resolved by dict ordering.
# ---------------------------------------------------------------------------

_REGISTER_SLOTS: dict[str, dict[str, tuple[str, ...]]] = {
    "x86": {
        "eax": ("eax", "ax", "al", "ah"),
        "ebx": ("ebx", "bx", "bl", "bh"),
        "ecx": ("ecx", "cx", "cl", "ch"),
        "edx": ("edx", "dx", "dl", "dh"),
        "esi": ("esi", "si"),
        "edi": ("edi", "di"),
        "ebp": ("ebp", "bp"),
        "esp": ("esp", "sp"),
        "eflags": ("eflags",),
    },
    "x64": {
        "rax": ("rax", "eax", "ax", "al", "ah"),
        "rbx": ("rbx", "ebx", "bx", "bl", "bh"),
        "rcx": ("rcx", "ecx", "cx", "cl", "ch"),
        "rdx": ("rdx", "edx", "dx", "dl", "dh"),
        "rsi": ("rsi", "esi", "si", "sil"),
        "rdi": ("rdi", "edi", "di", "dil"),
        "rbp": ("rbp", "ebp", "bp", "bpl"),
        "rsp": ("rsp", "esp", "sp", "spl"),
        "r8": ("r8", "r8d", "r8w", "r8b"),
        "r9": ("r9", "r9d", "r9w", "r9b"),
        "r10": ("r10", "r10d", "r10w", "r10b"),
        "r11": ("r11", "r11d", "r11w", "r11b"),
        "r12": ("r12", "r12d", "r12w", "r12b"),
        "r13": ("r13", "r13d", "r13w", "r13b"),
        "r14": ("r14", "r14d", "r14w", "r14b"),
        "r15": ("r15", "r15d", "r15w", "r15b"),
        "rflags": ("rflags", "eflags"),
    },
}

# Explicit widths for the sub-registers; everything else is derived from
# the name suffix below. ``sil``/``dil``/``bpl``/``spl`` are 64-bit-mode
# names only and appear in the x64 slot table alone.
_NAMED_WIDTHS: dict[str, int] = {
    "al": 1, "ah": 1, "bl": 1, "bh": 1, "cl": 1, "ch": 1, "dl": 1, "dh": 1,
    "sil": 1, "dil": 1, "bpl": 1, "spl": 1,
    "ax": 2, "bx": 2, "cx": 2, "dx": 2, "si": 2, "di": 2, "bp": 2, "sp": 2,
}  # fmt: skip
for _n in range(8, 16):
    _NAMED_WIDTHS[f"r{_n}b"] = 1
    _NAMED_WIDTHS[f"r{_n}w"] = 2

# Unified register names reported in results (x86 sees only its own).
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


def _register_width(name: str) -> int:
    """Width in bytes of a register alias (``eax`` -> 4, ``r8w`` -> 2)."""

    width = _NAMED_WIDTHS.get(name)
    if width is not None:
        return width
    if name.endswith("d"):
        return 4
    if name.startswith("e"):
        return 4
    return 8


# ---------------------------------------------------------------------------
# Value coercion helpers (pure).
# ---------------------------------------------------------------------------


def _coerce_int(value: Any, *, ctx: str, tool_name: str) -> int:
    """Coerce an LLM-supplied non-negative integer (int, hex string, integral float)."""

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
        raise ToolError(
            f"{ctx} must be an integer or hex string, got {value!r}",
            tool_name=tool_name,
        ) from None
    if coerced < 0:
        raise ToolError(
            f"{ctx} must not be negative, got {coerced}",
            tool_name=tool_name,
        )
    return coerced


def _coerce_signed_int(value: Any, *, ctx: str, tool_name: str) -> int:
    """Coerce a signed integer or hex string (e.g. ``-16`` / ``"-0x10"``)."""

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
        raise ToolError(
            f"{ctx} must be a signed integer or hex string, got {value!r}",
            tool_name=tool_name,
        ) from None
    return coerced


def _coerce_addr(value: Any, *, ctx: str, tool_name: str = "emulate_code") -> int:
    """Coerce an address the LLM supplied as int, integral float, or hex string."""

    return _coerce_int(value, ctx=ctx, tool_name=tool_name)


def _unwrap_range_item(item: Any) -> Any:
    """Unwrap a ``{"$text": "<json>"}`` wrapper around a structured argument.

    Some providers serialize nested structured tool args (lists of
    objects) as a ``$text`` JSON string.
    """

    if isinstance(item, Mapping) and len(item) == 1 and "$text" in item:
        raw = item["$text"]
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (ValueError, TypeError):
                return item
    return item


def _normalize_registers(raw: Any, arch: ArchMode, *, tool_name: str) -> dict[str, int]:
    """Validate the register object and collapse aliases onto native registers.

    ``{"eax": 41, "rax": 43}`` is a conflict, not a "last one wins" —
    dict ordering would decide the answer, and the model cannot see that.
    """

    if not isinstance(raw, Mapping):
        raise ToolError(
            f"{tool_name}: registers must be an object of register name -> value",
            tool_name=tool_name,
        )
    slots = _REGISTER_SLOTS[arch.label]
    owner: dict[str, str] = {}
    for base, aliases in slots.items():
        for alias in aliases:
            owner[alias] = base

    supplied: dict[str, list[tuple[str, int]]] = {}
    for key, value in raw.items():
        name = str(key).lower()
        if name in ("eip", "rip", "ip") or name == arch.ip_reg:
            raise ToolError(
                f"{tool_name}: {name} is taken from start_address and cannot be overridden",
                tool_name=tool_name,
            )
        if arch.ptr_size == 4 and name in _UNIFIED_REGISTERS and _UNIFIED_REGISTERS[name] == 8:
            raise ToolError(
                f"{tool_name}: register {name!r} does not exist in a 32-bit database",
                tool_name=tool_name,
            )
        owner_base = owner.get(name)
        if owner_base is None:
            raise ToolError(f"{tool_name}: unknown register {name!r}", tool_name=tool_name)
        supplied.setdefault(owner_base, []).append(
            (name, _coerce_int(value, ctx=f"{tool_name}: registers.{name}", tool_name=tool_name))
        )

    bits = 8 * arch.ptr_size
    resolved: dict[str, int] = {}
    for base, entries in supplied.items():
        value = 0
        claimed = 0  # bits already named by another alias of this register
        for name, raw_value in entries:
            width = _register_width(name)
            byte_offset = 1 if name in ("ah", "bh", "ch", "dh") else 0
            shift = 8 * byte_offset
            mask = ((1 << (8 * width)) - 1) << shift
            chunk = (raw_value & ((1 << (8 * width)) - 1)) << shift
            # Every bit two aliases both name must carry the same value.
            # ``rax=0x141`` with ``eax=0x41`` contradicts (bit 8), and so does
            # ``eax=0x100`` with ``al=0x42`` (byte 0) — neither is decided by
            # dict ordering, and a zero is a stated value here.
            if (claimed & mask) and (value ^ chunk) & mask & claimed:
                raise ToolError(
                    f"{tool_name}: conflicting values for {base} ({'/'.join(n for n, _ in entries)})",
                    tool_name=tool_name,
                )
            value |= chunk
            claimed |= mask
        resolved[base] = value & ((1 << bits) - 1)
    return resolved


# ---------------------------------------------------------------------------
# Tool-argument normalisation (pure; runs before any IDA call).
# ---------------------------------------------------------------------------


def _normalize_memory_ranges(
    memory_ranges: Sequence[Any],
    *,
    tool_name: str,
) -> list[tuple[int, int]]:
    """``{address, size}`` -> ``[(address, size)]``."""

    out: list[tuple[int, int]] = []
    for idx, item in enumerate(memory_ranges or ()):
        item = _unwrap_range_item(item)
        if not isinstance(item, Mapping):
            raise ToolError(
                f"{tool_name}: memory_ranges[{idx}] must be an object with 'address' and 'size'",
                tool_name=tool_name,
            )
        addr = _coerce_addr(item.get("address"), ctx=f"{tool_name}: memory_ranges[{idx}].address", tool_name=tool_name)
        size = item.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ToolError(
                f"{tool_name}: memory_ranges[{idx}].size must be a positive integer",
                tool_name=tool_name,
            )
        out.append((addr, int(size)))
    return out


def _normalize_code_ranges(code_ranges: Sequence[Any], *, tool_name: str) -> list[tuple[int, int]]:
    """Explicit executable allowlist in the IDB: ``{address, size}``."""

    return _normalize_memory_ranges(code_ranges, tool_name=tool_name)


def _normalize_buffers(
    memory_buffers: Sequence[Any],
    *,
    tool_name: str,
) -> list[MemoryBuffer]:
    """``{address, size, data_hex, permissions}`` -> scratch/input buffers.

    Every cap — per buffer and aggregate — is checked against the *declared*
    size before the hex string is decoded, so an oversized payload never
    allocates.
    """

    out: list[MemoryBuffer] = []
    seen: set[int] = set()
    declared_total = 0
    for idx, item in enumerate(memory_buffers or ()):
        item = _unwrap_range_item(item)
        if not isinstance(item, Mapping):
            raise ToolError(
                f"{tool_name}: memory_buffers[{idx}] must be an object with "
                "'address', 'size', 'data_hex' and 'permissions'",
                tool_name=tool_name,
            )
        ctx = f"{tool_name}: memory_buffers[{idx}]"
        addr = _coerce_addr(item.get("address"), ctx=f"{ctx}.address", tool_name=tool_name)
        size = item.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ToolError(f"{ctx}.size must be a positive integer", tool_name=tool_name)
        declared_total += size
        if declared_total > _MAX_MAPPED_BYTES:
            raise ToolError(
                f"{tool_name}: memory_buffers declare {declared_total} bytes, over the "
                f"{_MAX_MAPPED_BYTES} aggregate mapping cap",
                tool_name=tool_name,
            )
        perms = str(item.get("permissions") or "rw").strip().lower()
        if perms not in ("r", "rw"):
            raise ToolError(
                f"{ctx}.permissions must be 'r' or 'rw' (scratch memory is never executable)",
                tool_name=tool_name,
            )
        data_hex = item.get("data_hex")
        if not isinstance(data_hex, str):
            raise ToolError(f"{ctx}.data_hex must be a hex string", tool_name=tool_name)
        if len(data_hex) % 2:
            raise ToolError(f"{ctx}.data_hex must have an even number of hex digits", tool_name=tool_name)
        if len(data_hex) // 2 > size:
            raise ToolError(
                f"{ctx}.data_hex holds {len(data_hex) // 2} bytes but size is {size}",
                tool_name=tool_name,
            )
        try:
            data = bytes.fromhex(data_hex)
        except ValueError:
            raise ToolError(f"{ctx}.data_hex is not valid hexadecimal", tool_name=tool_name) from None
        if addr in seen:
            raise ToolError(f"{ctx}.address 0x{addr:x} is supplied more than once", tool_name=tool_name)
        seen.add(addr)
        out.append(
            MemoryBuffer(
                address=addr,
                size=int(size),
                data=data,
                permissions=1 if perms == "r" else 3,
            )
        )
    return out


def _normalize_capture_specs(
    capture_ranges: Sequence[Any],
    *,
    tool_name: str,
    default_label: str,
) -> list[dict[str, Any]]:
    """Validate capture specs before the stack layout is known.

    Each spec carries exactly one of ``address`` / ``stack_offset``.
    ``stack_offset`` is resolved against the initial SP after ABI setup.
    """

    specs: list[dict[str, Any]] = []
    labels: set[str] = set()
    for idx, item in enumerate(capture_ranges or ()):
        item = _unwrap_range_item(item)
        ctx = f"{tool_name}: capture_ranges[{idx}]"
        if not isinstance(item, Mapping):
            raise ToolError(
                f"{ctx} must be an object with exactly one of 'address' or 'stack_offset', plus 'size' and 'label'",
                tool_name=tool_name,
            )
        has_addr = "address" in item and item.get("address") not in (None, "")
        has_off = "stack_offset" in item and item.get("stack_offset") is not None
        if has_addr == has_off:
            raise ToolError(
                f"{ctx} must supply exactly one of 'address' or 'stack_offset'",
                tool_name=tool_name,
            )
        size = item.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or not (1 <= size <= _MAX_OUTPUT_BYTES):
            raise ToolError(f"{ctx}.size must be in 1..{_MAX_OUTPUT_BYTES}", tool_name=tool_name)
        label = str(item.get("label") or f"{default_label}_{idx}")
        if label in labels:
            raise ToolError(f"{ctx}.label {label!r} is used twice", tool_name=tool_name)
        labels.add(label)
        spec: dict[str, Any] = {"size": int(size), "label": label}
        if has_addr:
            spec["address"] = _coerce_addr(item.get("address"), ctx=f"{ctx}.address", tool_name=tool_name)
        else:
            spec["stack_offset"] = _coerce_signed_int(
                item.get("stack_offset"), ctx=f"{ctx}.stack_offset", tool_name=tool_name
            )
        specs.append(spec)
    if len(specs) > _MAX_CAPTURES:
        raise ToolError(
            f"{tool_name}: at most {_MAX_CAPTURES} capture ranges are allowed",
            tool_name=tool_name,
        )
    return specs


def _normalize_arguments(arguments: Sequence[Any], *, tool_name: str) -> list[int]:
    """ABI argument values (integers or hex strings)."""

    if isinstance(arguments, (str, Mapping)):
        raise ToolError(
            f"{tool_name}: arguments must be a list of integer or hex-string values",
            tool_name=tool_name,
        )
    out: list[int] = []
    for idx, item in enumerate(arguments or ()):
        item = _unwrap_range_item(item)
        if isinstance(item, (list, tuple)):
            raise ToolError(
                f"{tool_name}: arguments[{idx}] must be a single value, not a nested list",
                tool_name=tool_name,
            )
        out.append(_coerce_int(item, ctx=f"{tool_name}: arguments[{idx}]", tool_name=tool_name))
    return out


def _validate_timeout(timeout_seconds: Any, *, tool_name: str) -> float:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise ToolError(
            f"{tool_name}: timeout_seconds must be a number of seconds",
            tool_name=tool_name,
        )
    value = float(timeout_seconds)
    if not math.isfinite(value) or value <= 0:
        raise ToolError(
            f"{tool_name}: timeout_seconds must be finite and > 0 (got {timeout_seconds!r})",
            tool_name=tool_name,
        )
    return min(value, _MAX_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# Calling convention / ABI setup (pure).
# ---------------------------------------------------------------------------

_ABI_X86: dict[str, tuple[tuple[str, ...], int]] = {
    # convention -> (argument registers, first stack-argument offset from SP)
    "cdecl": ((), 4),
    "stdcall": ((), 4),
    "fastcall": (("ecx", "edx"), 4),
}

_ABI_X64: dict[str, tuple[tuple[str, ...], int]] = {
    "win64": (("rcx", "rdx", "r8", "r9"), 40),  # 32-byte shadow space above the return address
    "sysv64": (("rdi", "rsi", "rdx", "rcx", "r8", "r9"), 8),
}

_MAX_ABI_ARGUMENTS = 32


@dataclass(frozen=True)
class _AbiSetup:
    """Register state + stack layout derived from the calling convention."""

    registers: dict[str, int]
    stack_pointer: int
    stack_writes: tuple[tuple[int, bytes], ...]


def _build_abi(
    arch: ArchMode,
    *,
    tool_name: str,
    convention: str,
    arguments: Sequence[int],
    registers: dict[str, int],
    return_address: int | None,
) -> _AbiSetup:
    """Lay out arguments for *convention* on the synthetic stack.

    ``return_address`` is the stop sentinel planted at ``[SP]`` in function
    mode. In range mode (``convention`` empty) nothing is planted and an
    explicitly supplied ESP/RSP is kept as-is, so range mode never needs a
    calling convention. A supplied SP in function mode must be inside the
    synthetic stack and is aligned to the convention's entry alignment —
    it is never silently replaced by the default.
    """

    state = dict(registers)
    ptr = arch.ptr_size
    stack_top = arch.stack_base + _STACK_SIZE - 0x100
    stack_limit = arch.stack_base + _STACK_SIZE
    # `in`, not truthiness: an explicit rsp=0 is a stated (invalid) value, not
    # "unset".
    if arch.sp_reg in state:
        supplied_sp = int(state[arch.sp_reg])
        if not (arch.stack_base <= supplied_sp < stack_limit):
            raise ToolError(
                f"{tool_name}: {arch.sp_reg}=0x{supplied_sp:x} is outside the 1 MiB synthetic stack",
                tool_name=tool_name,
            )
    else:
        supplied_sp = stack_top

    if not convention:
        # Range mode: no convention, no sentinel, no argument layout.
        return _AbiSetup(
            registers=state,
            stack_pointer=int(supplied_sp),
            stack_writes=(),
        )

    table = _ABI_X86 if ptr == 4 else _ABI_X64
    if convention not in table:
        allowed = ", ".join(sorted(table))
        raise ToolError(
            f"{tool_name}: calling_convention {convention!r} is not valid for {arch.label}; expected one of {allowed}",
            tool_name=tool_name,
        )
    if len(arguments) > _MAX_ABI_ARGUMENTS:
        raise ToolError(
            f"{tool_name}: at most {_MAX_ABI_ARGUMENTS} arguments are supported",
            tool_name=tool_name,
        )

    arg_regs, stack_offset = table[convention]
    if ptr == 8:
        # SysV / Win64 require RSP % 16 == 8 at function entry, i.e. the
        # return address sits on a 16-byte-aligned frame.
        stack_pointer = (int(supplied_sp) & ~0xF) + 8
    else:
        stack_pointer = int(supplied_sp) & ~0xF

    def _stack_slot(address: int, length: int, what: str) -> None:
        if address < arch.stack_base or address + length > stack_limit:
            raise ToolError(
                f"{tool_name}: {what} at 0x{address:x}..0x{address + length:x} does not fit in "
                "the 1 MiB synthetic stack",
                tool_name=tool_name,
            )

    writes: list[tuple[int, bytes]] = []
    for index, value in enumerate(arguments):
        masked = value & ((1 << (8 * ptr)) - 1)
        if index < len(arg_regs):
            reg = arg_regs[index]
            if reg in state and state[reg] != masked:
                raise ToolError(
                    f"{tool_name}: argument {index} = 0x{masked:x} conflicts with explicit {reg} = 0x{state[reg]:x}",
                    tool_name=tool_name,
                )
            state[reg] = masked
            continue
        address = stack_pointer + stack_offset + (index - len(arg_regs)) * ptr
        _stack_slot(address, ptr, f"argument {index}")
        writes.append((address, masked.to_bytes(ptr, "little")))

    if return_address is not None:
        _stack_slot(stack_pointer, ptr, "return sentinel")
        writes.insert(0, (stack_pointer, int(return_address).to_bytes(ptr, "little")))
    if ptr == 8 and convention == "win64":
        # Reserve the callee's 32-byte shadow space; never widen page permissions.
        _stack_slot(stack_pointer + 8, 32, "win64 shadow space")
    return _AbiSetup(
        registers=state,
        stack_pointer=stack_pointer,
        stack_writes=tuple(writes),
    )


# ---------------------------------------------------------------------------
# Instruction-level helpers (pure).
# ---------------------------------------------------------------------------


def _merge_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping/adjacent ``(address, size)`` pairs into spans.

    The pairs are sizes, not ends: a range request is ``(address, size)``,
    so the exclusive end is only computed here.
    """

    ordered = sorted((int(s), int(s) + int(size)) for s, size in ranges if int(size) > 0)
    merged: list[list[int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def _within(ranges: Sequence[tuple[int, int]], start: int, size: int) -> bool:
    """True if ``[start, start+size)`` fits inside a single merged *range*."""

    end = start + max(1, size)
    for lo, hi in ranges:
        if start >= lo and end <= hi:
            return True
    return False


def _scan_forbidden(data: bytes, *, ptr_size: int = 8) -> str | None:
    """Return a reason if *data* is a syscall / system / interrupt trap.

    Instruction prefixes are skipped, so ``66 0f 05`` is caught as
    ``syscall`` too. In 64-bit mode bytes 0x40-0x4f are REX prefixes and are
    skipped; in 32-bit mode the same bytes are ``inc``/``dec`` and must run.

    Only the *start* of the instruction is inspected — immediates are never
    treated as opcodes. Every ``int`` form is refused, plus ``int3``,
    ``int1`` and ``into``: none of them has a meaning here, and Unicorn
    would either execute them or raise an opaque engine exception.
    """

    prefixes = _PREFIX_BYTES | _REX_BYTES if ptr_size == 8 else _PREFIX_BYTES
    index = 0
    while index < len(data):
        byte = data[index]
        if byte in prefixes:
            index += 1
            continue
        chunk = data[index : index + 2]
        for opcode in _SYSCALL_OPCODES:
            if chunk == opcode:
                return f"unsupported instruction: {opcode.hex()} (no system-call emulation)"
        for opcode in _INVALID_OPCODES:
            if chunk == opcode:
                return f"unsupported instruction: {opcode.hex()} (architecturally invalid)"
        if chunk == b"\x0f\x01":
            return "unsupported instruction: 0f01 (no system instruction emulation)"
        if byte == 0xCD and index + 1 < len(data):
            return f"unsupported instruction: cd{data[index + 1]:02x} (no software-interrupt emulation)"
        if byte in (0xCC, 0xF1, 0xCE):
            name = {0xCC: "int3", 0xF1: "int1", 0xCE: "into"}[byte]
            return f"unsupported instruction: {name} (no trap emulation)"
        break
    return None


# ---------------------------------------------------------------------------
# CPU runner. No IDA references past this line: everything it needs is in
# the ``MemorySnapshot``.
# ---------------------------------------------------------------------------


def run_emulation(
    *,
    arch: ArchMode,
    snapshot: MemorySnapshot,
    entry_pc: int,
    stop_pc: int,
    execution_mode: str,
    registers: Mapping[str, int],
    stack_writes: Sequence[tuple[int, bytes]],
    captures: Sequence[CaptureRequest],
    code_ranges: Sequence[tuple[int, int]] = (),
    max_instructions: int = _DEFAULT_INSTRUCTION_LIMIT,
    collect_strings: bool = False,
    deadline: float | None = None,
    cancel_event: threading.Event | None = None,
    tool_name: str = "emulate_code",
) -> EmulationResult:
    """Execute the mapped snapshot on Unicorn and return a bounded result.

    The engine runs at the snapshot's own virtual addresses: the tools must
    report the addresses the IDB holds, and every operand form Unicorn 2.1.4
    decodes (``[rsp+8]``, ``[rbx]``, absolute ``moffs``) resolves against them.

    ``deadline`` is an absolute ``time.monotonic()`` bound; it is handed
    to Unicorn in microseconds and re-checked from the code hook.
    """

    unicorn = _load_unicorn()
    reg_ids = _register_id_map(unicorn)
    missing = [n for n in (arch.ip_reg, arch.sp_reg, arch.flags_reg) if n not in reg_ids]
    if missing:
        raise ToolError(
            f"Unicorn x86 register constants missing for {arch.label}: {', '.join(missing)}",
            tool_name=tool_name,
        )

    try:
        engine = unicorn.Uc(arch.arch_const, arch.mode_const)
    except Exception as e:
        raise ToolError(f"Failed to construct Unicorn engine: {e}", tool_name=tool_name) from e

    mapped_ranges = [
        (region.address, region.address + region.size, region.permissions)
        for region in snapshot.regions
        if not region.synthetic
    ]
    result = EmulationResult(
        entry_pc=entry_pc,
        stop_pc=entry_pc,
        architecture=arch.label,
        mapped_ranges=mapped_ranges,
    )

    for region in snapshot.regions:
        prot = unicorn.UC_PROT_NONE
        if region.permissions & 1:
            prot |= unicorn.UC_PROT_READ
        if region.permissions & 2:
            prot |= unicorn.UC_PROT_WRITE
        if region.permissions & 4:
            prot |= unicorn.UC_PROT_EXEC
        try:
            engine.mem_map(region.address, region.size, prot)
        except Exception as e:
            raise ToolError(
                f"Failed to map 0x{region.address:x}..0x{region.address + region.size:x}: {e}",
                tool_name=tool_name,
            ) from e
        if region.data:
            engine.mem_write(region.address, region.data)

    # Stack initialization happens inside the already-mapped synthetic
    # stack; no page permission is ever widened for it.
    for address, payload in stack_writes:
        engine.mem_write(address, payload)

    # Registers: native slots only, defaults 0 (explicit flags survive).
    slot_names = _REGISTER_SLOTS[arch.label]
    ptr_mask = (1 << (8 * arch.ptr_size)) - 1
    initial: dict[str, int] = {}
    for base in slot_names:
        value = int(registers.get(base, 0)) & ptr_mask
        initial[base] = value
        reg_id = reg_ids.get(base)
        if reg_id is None:
            continue
        try:
            engine.reg_write(reg_id, value)
        except Exception as e:
            # Silently continuing would emulate with a state the caller
            # never asked for; surface it instead.
            raise ToolError(
                f"Failed to initialise register {base} on the {arch.label} engine: {e}",
                tool_name=tool_name,
            ) from e
    engine.reg_write(reg_ids[arch.ip_reg], entry_pc)
    result.initial_registers = _expand_registers(initial, arch)

    allowed = _merge_ranges([(entry_pc, max(0, stop_pc - entry_pc)), *code_ranges])
    # Byte coverage is independent of permissions (including write-only IDB
    # segments). Unicorn enforces page permissions; padding stays invalid.
    valid = _merge_ranges((start, end - start) for start, end, _perms in snapshot.valid_ranges)
    executable_valid = _merge_ranges((start, end - start) for start, end, perms in snapshot.valid_ranges if perms & 4)

    limit = max(1, min(_MAX_INSTRUCTION_LIMIT, int(max_instructions or _DEFAULT_INSTRUCTION_LIMIT)))
    ip_reg_id = reg_ids[arch.ip_reg]
    state: dict[str, Any] = {
        "status": "",
        "reason": "",
        "stop_pc": entry_pc,
        "count": 0,
        "writes": [],
        "write_events": 0,
        "dirty_pages": set(),
    }

    def _pc() -> int:
        try:
            return int(engine.reg_read(ip_reg_id))
        except Exception:
            return entry_pc

    def _stop(status: str, reason: str, pc: int | None = None) -> None:
        state["status"] = status
        state["reason"] = reason
        state["stop_pc"] = _pc() if pc is None else pc
        engine.emu_stop()

    # Snapshot the initial bytes of mapped pages once, so string discovery
    # can diff modified pages without re-reading memory per instruction.
    page_initial: dict[int, bytes] = {}
    if collect_strings:
        for region in snapshot.regions:
            start = region.address
            end = region.address + region.size
            offset = 0
            while start + offset < end:
                page = (start + offset) & ~(_PAGE_SIZE - 1)
                if page not in page_initial:
                    chunk = engine.mem_read(page, _PAGE_SIZE)
                    page_initial[page] = bytes(chunk)
                offset += _PAGE_SIZE

    def code_hook(_uc, address, size, _user):
        # Runs *before* the instruction at `address`. Reached-stop has
        # precedence, then the "may we run this" gates, then the budget:
        # `count` is the number of instructions that already executed, so
        # `limit=1` runs exactly one.
        next_ip = _pc()
        if next_ip == stop_pc:
            reached = (
                f"function returned to 0x{next_ip:x}"
                if execution_mode == _MODE_FUNCTION
                else f"reached stop address 0x{next_ip:x}"
            )
            _stop("completed", reached, next_ip)
            return
        # Some Unicorn builds hand the hook a bogus size for an instruction
        # the decoder rejected; never read a huge span on that report.
        if not (0 < size <= _MAX_INSN_BYTES):
            _stop("unsupported_instruction", f"undecodable instruction at 0x{next_ip:x}", next_ip)
            return
        if not _within(allowed, address, size):
            _stop("range_exit", f"execution left the allowed ranges at 0x{next_ip:x}", next_ip)
            return
        if not _within(executable_valid, address, size):
            _stop(
                "range_exit",
                f"instruction at 0x{next_ip:x} is not backed by executable IDB bytes",
                next_ip,
            )
            return
        forbidden = _scan_forbidden(bytes(engine.mem_read(address, size)), ptr_size=arch.ptr_size)
        if forbidden is not None:
            _stop("unsupported_instruction", f"{forbidden} at 0x{address:x}", next_ip)
            return
        if cancel_event is not None and cancel_event.is_set():
            _stop("cancelled", "cancelled before the next instruction", next_ip)
            return
        if state["count"] >= limit:
            _stop("instruction_limit", f"instruction limit {limit} reached", next_ip)
            return
        if deadline is not None and state["count"] % 64 == 0 and time.monotonic() >= deadline:
            _stop("timeout", "wall-clock deadline reached", next_ip)
            return
        state["count"] += 1

    def write_hook(_uc, _access, address, size, value, _user):
        state["write_events"] += 1
        span = (int(address) & ~(_PAGE_SIZE - 1), int(address) + max(1, int(size)))
        state["dirty_pages"].update(range(span[0], span[1], _PAGE_SIZE))
        if len(state["writes"]) < _MAX_WRITE_ENTRIES:
            width = max(1, min(_PAGE_SIZE, int(size)))
            state["writes"].append(
                {
                    "address": int(address),
                    "size": int(size),
                    "hex_preview": f"{int(value) & ((1 << min(64, 8 * width)) - 1):x}",
                }
            )

    def _validity_reason(access: int, address: int, size: int) -> str | None:
        if not _within(valid, address, size):
            return f"access to unmapped/byte padding at 0x{address:x} (size {size}, PC 0x{_pc():x})"
        return None

    def range_guard_hook(_uc, access, address, size, _value, _user):
        # Deny reads/writes into page padding instead of mapping zero pages.
        reason = _validity_reason(access, int(address), int(size))
        if reason is None:
            return True
        _stop("unmapped_memory", reason)
        return False

    def invalid_mem_hook(_uc, access, address, size, _value, _user):
        fault = int(address)
        fault_size = max(1, int(size))
        in_valid = _within(valid, fault, fault_size)
        if access == unicorn.UC_MEM_READ_UNMAPPED:
            status, kind = "unmapped_memory", "read from unmapped memory"
        elif access == unicorn.UC_MEM_WRITE_UNMAPPED:
            status, kind = "unmapped_memory", "write to unmapped memory"
        elif access == unicorn.UC_MEM_READ_PROT:
            # A read-protected fault on a page we mapped read-write is a
            # padding hole; on a valid range the page simply is not readable.
            status = "permission_error" if in_valid else "unmapped_memory"
            kind = "read from unreadable memory" if in_valid else "read into page padding"
        elif access == unicorn.UC_MEM_WRITE_PROT:
            status = "permission_error" if in_valid else "unmapped_memory"
            kind = "write to read-only memory" if in_valid else "write into page padding"
        elif not _within(allowed, fault, fault_size):
            # A transfer to code we were never allowed to run.
            status = "range_exit"
            kind = "transfer to code outside the allowed ranges"
        else:
            # Allowed, but nothing mapped there: the allowlist promised bytes
            # the snapshot could not back.
            status = "unmapped_memory"
            kind = (
                "fetch from unmapped memory"
                if access == unicorn.UC_MEM_FETCH_UNMAPPED
                else "fetch from non-executable memory"
            )
        _stop(
            status,
            f"{kind} at 0x{fault:x} (PC 0x{_pc():x})",
            fault if status == "range_exit" else None,
        )

    def invalid_insn_hook(_uc, _user):
        _stop("unsupported_instruction", f"unsupported instruction at PC 0x{_pc():x}")

    try:
        engine.hook_add(unicorn.UC_HOOK_CODE, code_hook)
        engine.hook_add(unicorn.UC_HOOK_MEM_WRITE, write_hook)
        engine.hook_add(unicorn.UC_HOOK_MEM_READ, range_guard_hook)
        engine.hook_add(unicorn.UC_HOOK_MEM_WRITE, range_guard_hook)
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
        raise ToolError(f"Failed to register Unicorn hooks: {e}", tool_name=tool_name) from e

    timeout_us = 0
    if deadline is not None:
        timeout_us = max(1, int((deadline - time.monotonic()) * 1_000_000))

    if cancel_event is not None and cancel_event.is_set():
        _stop("cancelled", "cancelled before emulation started", entry_pc)
    else:
        # The engine's own counter is one ahead of our hook: it stops *at*
        # the ``count``-th instruction, so the budget is enforced in the code
        # hook and the native counter is only a runaway backstop. Native
        # arrival at the sentinel never fires the code hook, so the PC check
        # below is the only way to see it.
        try:
            engine.emu_start(entry_pc, stop_pc, timeout=timeout_us, count=limit + 1)
        except Exception as e:
            if not state["status"]:
                state["status"] = "emulator_error"
                state["reason"] = f"Unicorn raised {type(e).__name__}: {e}"
                state["stop_pc"] = _pc()

    if not state["status"]:
        # The engine returned on its own. A PC that really reached the
        # sentinel is `completed` regardless of a cancel or a late clock;
        # otherwise ask Unicorn *why* it stopped instead of guessing from
        # wall-clock noise.
        final_pc = _pc()
        try:
            timed_out = int(engine.query(unicorn.UC_QUERY_TIMEOUT)) != 0
        except Exception:  # pragma: no cover - depends on SDK build
            timed_out = deadline is not None and time.monotonic() >= deadline
        if final_pc == stop_pc:
            state["status"] = "completed"
            state["reason"] = (
                f"function returned to 0x{final_pc:x}"
                if execution_mode == _MODE_FUNCTION
                else f"reached stop address 0x{final_pc:x}"
            )
            state["stop_pc"] = final_pc
        elif cancel_event is not None and cancel_event.is_set():
            state["status"] = "cancelled"
            state["reason"] = "cancelled during emulation"
            state["stop_pc"] = final_pc
        elif timed_out:
            state["status"] = "timeout"
            state["reason"] = "wall-clock deadline reached inside the engine"
            state["stop_pc"] = final_pc
        else:
            state["status"] = "emulator_error"
            state["reason"] = f"engine stopped at 0x{final_pc:x} without reaching 0x{stop_pc:x}"
            state["stop_pc"] = final_pc

    result.status = str(state["status"])
    result.reason = str(state["reason"])
    result.stop_pc = int(state["stop_pc"])
    result.instruction_count = int(state["count"])

    final_slots: dict[str, int] = {}
    for base in slot_names:
        reg_id = reg_ids.get(base)
        if reg_id is None:
            continue
        try:
            final_slots[base] = int(engine.reg_read(reg_id)) & ptr_mask
        except Exception:  # pragma: no cover - depends on SDK build
            continue
    result.final_registers = _expand_registers(final_slots, arch)
    result.final_registers[arch.ip_reg] = int(engine.reg_read(ip_reg_id))

    capture_notes: list[str] = []
    for capture in captures:
        try:
            payload = bytes(engine.mem_read(capture.address, capture.size))
        except Exception as exc:
            payload = b""
            capture_notes.append(f"{capture.label}: capture failed ({exc})")
        result.captures[capture.label] = payload
        result.captured_strings[capture.label] = decode_string_candidates(payload)
    if capture_notes:
        result.reason = f"{result.reason}; " + "; ".join(capture_notes)

    result.writes = list(state["writes"])
    result.write_event_count = int(state["write_events"])

    if collect_strings:
        result.discovered_strings, result.discovery_truncated = _discover_strings(
            engine=engine,
            page_initial=page_initial,
            dirty_pages=state["dirty_pages"],
            valid=valid,
        )
    return result


def _expand_registers(slots: Mapping[str, int], arch: ArchMode) -> dict[str, int]:
    """Project native register values onto every reported alias."""

    out: dict[str, int] = {}
    for name, width in _UNIFIED_REGISTERS.items():
        if width == 8 and arch.ptr_size == 4:
            continue
        for base, aliases in _REGISTER_SLOTS[arch.label].items():
            if name in aliases and base in slots:
                out[name] = int(slots[base]) & ((1 << (8 * width)) - 1)
                break
    return out


def _candidate_span(candidate: Any) -> int:
    """Byte length a discovered candidate occupies in emulated memory."""

    if candidate.encoding == "utf16le":
        return len(candidate.text.encode("utf-16-le"))
    if candidate.encoding == "utf8":
        return len(candidate.text.encode("utf-8"))
    return len(candidate.text)


def _changed_runs(before: bytes, after: bytes) -> list[tuple[int, int]]:
    """Maximal index runs where *after* differs from *before*."""

    runs: list[list[int]] = []
    for index, (old, new) in enumerate(zip(before, after, strict=True)):
        if old != new:
            if runs and runs[-1][1] == index:
                runs[-1][1] = index + 1
            else:
                runs.append([index, index + 1])
    return [(lo, hi) for lo, hi in runs]


def _contiguous_pages(pages: set[int]) -> list[tuple[int, int]]:
    """Group page-aligned addresses into contiguous page runs."""

    out: list[list[int]] = []
    for page in sorted(pages):
        if out and page == out[-1][1]:
            out[-1][1] = page + _PAGE_SIZE
        else:
            out.append([page, page + _PAGE_SIZE])
    return [(lo, hi) for lo, hi in out]


def _discover_strings(
    *,
    engine: Any,
    page_initial: dict[int, bytes],
    dirty_pages: set[int],
    valid: Sequence[tuple[int, int]],
) -> tuple[list[Any], bool]:
    """Diff dirty pages against their initial bytes and pull strings out.

    Discovery reads the modified pages, not the (capped) write-event log, so
    a run with thousands of writes still surfaces the string it produced.
    A candidate survives only if it overlaps a byte the run actually
    changed — an untouched string sitting between two writes is never
    reported. Windows are the changed neighbourhoods widened by
    ``_DISCOVERY_MARGIN`` unchanged bytes so the surrounding string bytes
    are decoded as one candidate, and the 64-result cap is applied *after*
    filtering so a pre-existing string cannot push a new one out.
    """

    found: list[Any] = []
    seen: set[tuple[int, str, str]] = set()
    truncated = False
    scanned = 0

    for group_lo, group_hi in _contiguous_pages(dirty_pages):
        for valid_lo, valid_hi in valid:
            lo = max(group_lo, valid_lo)
            hi = min(group_hi, valid_hi)
            if hi - lo <= 0:
                continue
            base = lo & ~(_PAGE_SIZE - 1)
            pages = range(base, hi, _PAGE_SIZE)
            before_parts: list[bytes] = []
            after_parts: list[bytes] = []
            complete = True
            for page in pages:
                chunk = page_initial.get(page)
                if chunk is None:
                    complete = False
                    break
                try:
                    after_parts.append(bytes(engine.mem_read(page, len(chunk))))
                except Exception:  # pragma: no cover - engine torn down
                    complete = False
                    break
                before_parts.append(chunk)
            if not complete:
                continue
            # The assembled pages start at the page base, while every index
            # below is relative to `lo` — valid coverage can begin mid-page.
            offset = lo - base
            size = hi - lo
            before = b"".join(before_parts)[offset : offset + size]
            after = b"".join(after_parts)[offset : offset + size]
            changed = _changed_runs(before, after)
            if not changed:
                continue
            for start, end in changed:
                window_lo = max(0, start - _DISCOVERY_MARGIN)
                window_hi = min(size, end + _DISCOVERY_MARGIN)
                if window_hi - window_lo > _DISCOVERY_SCAN_CAP - scanned:
                    truncated = True
                    return found, truncated
                scanned += window_hi - window_lo
                candidates = extract_strings(
                    lo + window_lo,
                    after[window_lo:window_hi],
                    min_length=4,
                    max_candidates=_DISCOVERY_WINDOW_CANDIDATES,
                )
                for candidate in candidates:
                    span = _candidate_span(candidate)
                    cand_lo = candidate.address - lo
                    cand_hi = cand_lo + span
                    if cand_hi <= window_lo or cand_lo >= window_hi:
                        continue
                    if not any(before[i] != after[i] for i in range(max(cand_lo, window_lo), min(cand_hi, window_hi))):
                        continue  # unchanged bytes only
                    key = (candidate.address, candidate.encoding, candidate.text)
                    if key in seen:
                        continue
                    seen.add(key)
                    if len(found) >= _MAX_DISCOVERY_CANDIDATES:
                        truncated = True
                        continue
                    found.append(candidate)
    return found, truncated


# ---------------------------------------------------------------------------
# Shared tool front-end: validate -> host snapshot -> CPU run -> format.
# ---------------------------------------------------------------------------


def _run_tool(
    *,
    tool_name: str,
    start: int,
    stop: int,
    registers: Mapping[str, Any],
    memory_ranges: Sequence[Any],
    code_ranges: Sequence[Any],
    memory_buffers: Sequence[Any],
    capture_specs: Sequence[dict[str, Any]],
    implicit_capture: dict[str, Any] | None,
    execution_mode: str,
    calling_convention: str,
    arguments: Sequence[Any],
    instruction_limit: Any,
    timeout_seconds: Any,
    collect_strings: Any,
) -> EmulationResult:
    """Validate arguments, snapshot memory on the host, run the CPU phase.

    Returns the structured result; the public tools only render it.
    """

    context = get_execution_context()
    # The budget covers the whole call, not just the CPU phase: a slow
    # snapshot must not silently extend it.
    deadline = time.monotonic() + _validate_timeout(timeout_seconds, tool_name=tool_name)
    if context.deadline is not None:
        deadline = min(deadline, context.deadline)
    cancel = context.cancel_event

    if execution_mode not in (_MODE_RANGE, _MODE_FUNCTION):
        raise ToolError(
            f"{tool_name}: execution_mode must be 'range' or 'function', got {execution_mode!r}",
            tool_name=tool_name,
        )
    if start >= stop:
        raise ToolError(
            f"{tool_name}: start_address (0x{start:x}) must be < stop_address (0x{stop:x})",
            tool_name=tool_name,
        )
    if isinstance(instruction_limit, bool) or not isinstance(instruction_limit, int):
        raise ToolError(f"{tool_name}: instruction_limit must be an integer", tool_name=tool_name)
    if instruction_limit <= 0 or instruction_limit > _MAX_INSTRUCTION_LIMIT:
        raise ToolError(
            f"{tool_name}: instruction_limit must be in 1..{_MAX_INSTRUCTION_LIMIT}",
            tool_name=tool_name,
        )
    collect = bool(collect_strings)

    arg_values = _normalize_arguments(arguments, tool_name=tool_name)
    if arg_values and execution_mode != _MODE_FUNCTION:
        raise ToolError(
            f"{tool_name}: arguments are only supported in function mode; set "
            "execution_mode='function' and pass an explicit calling_convention",
            tool_name=tool_name,
        )
    if execution_mode == _MODE_FUNCTION and not calling_convention:
        raise ToolError(
            f"{tool_name}: execution_mode='function' requires an explicit calling_convention "
            "(x86: cdecl/stdcall/fastcall, x64: win64/sysv64)",
            tool_name=tool_name,
        )

    extras = _normalize_memory_ranges(memory_ranges, tool_name=tool_name)
    code = _normalize_code_ranges(code_ranges, tool_name=tool_name)
    buffers = _normalize_buffers(memory_buffers, tool_name=tool_name)
    captures_specs = list(capture_specs)
    if implicit_capture:
        captures_specs.insert(0, {"label": "output", **implicit_capture})

    # Architecture is an IDA query: run it on the host thread, before any
    # memory snapshot work.
    unicorn = _load_unicorn()
    arch = run_on_host_thread(_resolve_arch, unicorn)
    resolved_registers = _normalize_registers(registers, arch, tool_name=tool_name)
    if execution_mode == _MODE_RANGE and not resolved_registers:
        # Range mode replays code the caller must describe: an empty map
        # would silently invent a zeroed machine state. Function mode may
        # pass {} because the arguments carry the state.
        raise ToolError(
            f"{tool_name}: registers must name at least one initial register value in range mode",
            tool_name=tool_name,
        )
    if execution_mode == _MODE_RANGE and calling_convention:
        # Applying an ABI in range mode would realign a supplied SP and
        # plant a return address the caller never asked for.
        raise ToolError(
            f"{tool_name}: calling_convention only applies to execution_mode='function'",
            tool_name=tool_name,
        )

    # The ABI layout is resolved *before* the captures so a stack-relative
    # capture uses the SP the callee will actually see, and *before* the
    # snapshot so a declared code range is mapped: the entry range is the
    # only one the memory layer backs implicitly.
    abi = _build_abi(
        arch,
        tool_name=tool_name,
        convention=calling_convention,
        arguments=arg_values,
        registers=resolved_registers,
        return_address=stop if execution_mode == _MODE_FUNCTION else None,
    )
    entry_registers = dict(abi.registers)
    entry_registers[arch.sp_reg] = abi.stack_pointer

    captures: list[CaptureRequest] = []
    for spec in captures_specs:
        if "address" in spec:
            address = int(spec["address"])
        else:
            offset = int(spec["stack_offset"])
            address = abi.stack_pointer + offset
            if not (arch.stack_base <= address and address + spec["size"] <= arch.stack_base + _STACK_SIZE):
                raise ToolError(
                    f"{tool_name}: stack-relative capture {offset:+d} resolves to 0x{address:x}, "
                    "outside the 1 MiB synthetic stack",
                    tool_name=tool_name,
                )
        captures.append(CaptureRequest(address=address, size=int(spec["size"]), label=str(spec["label"])))

    try:
        snapshot = run_on_host_thread(
            lambda: snapshot_memory(
                arch=arch,
                start_address=start,
                stop_address=stop,
                extra_ranges=extras,
                captures=captures,
                memory_buffers=buffers,
                code_ranges=code,
                deadline=deadline,
                cancel_event=cancel,
            )
        )
    except SnapshotAborted as aborted:
        # The snapshot never completed, so there is no memory state to
        # report: invent nothing.
        return EmulationResult(
            status=aborted.status,
            reason=str(aborted),
            entry_pc=start,
            stop_pc=start,
            architecture=arch.label,
        )

    # The snapshot is complete here, so a late cancel/timeout can report
    # the real mapping and the prepared register state instead of pretending
    # a run happened.
    if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
        was_cancelled = cancel is not None and cancel.is_set()
        return EmulationResult(
            status="cancelled" if was_cancelled else "timeout",
            reason=(
                "cancelled before emulation started"
                if was_cancelled
                else "wall-clock deadline reached while preparing memory"
            ),
            entry_pc=start,
            stop_pc=start,
            architecture=arch.label,
            mapped_ranges=[(r.address, r.address + r.size, r.permissions) for r in snapshot.regions if not r.synthetic],
            initial_registers=_expand_registers(entry_registers, arch),
            final_registers=dict(_expand_registers(entry_registers, arch)),
        )

    return run_emulation(
        arch=arch,
        snapshot=snapshot,
        entry_pc=start,
        stop_pc=stop,
        execution_mode=execution_mode,
        registers=entry_registers,
        stack_writes=abi.stack_writes,
        captures=captures,
        code_ranges=code,
        max_instructions=instruction_limit,
        collect_strings=collect,
        deadline=deadline,
        cancel_event=cancel,
        tool_name=tool_name,
    )


# ---------------------------------------------------------------------------
# Public tools. Both opt out of the registry's outer host dispatch and
# dispatch their own IDA sections.
# ---------------------------------------------------------------------------


@tool(category="emulation", timeout=30.0, main_thread=False)
def emulate_code(
    start_address: Annotated[str, "First instruction to execute (inclusive hex address)"],
    stop_address: Annotated[
        str,
        "Exclusive emulation end. In function mode this is the function's end "
        "address and doubles as the return sentinel a RET must land on.",
    ],
    registers: Annotated[
        dict,
        "Explicit initial CPU register state; at least one value is required "
        "in range mode, while function mode may pass {} because the arguments "
        "carry the state. Every register not named starts at 0. Keys are "
        "x86/x64 register names (eax/ebx/rax/r8/eflags/etc.), values are "
        "integers or '0x'-style hex strings. "
        "Aliases of the same register (eax + rax) must agree. "
        "eip/rip are taken from start_address and cannot be overridden.",
    ],
    memory_ranges: Annotated[
        list[dict] | None,
        "Optional extra IDB address ranges to map (encrypted input, key, "
        "lookup tables). Each entry is {address, size} where address is an "
        "int or '0x' hex string and size is a positive integer.",
    ] = None,
    capture_ranges: Annotated[
        list[dict] | None,
        "Optional output buffers to read back at the end of emulation. Each "
        "entry is {address, size, label} or {stack_offset, size, label} — "
        "exactly one of address / signed stack_offset, at most 4096 bytes and "
        "16 entries. stack_offset is relative to the entry SP after ABI setup.",
    ] = None,
    instruction_limit: Annotated[
        int,
        "Upper bound on instructions executed (default 100000, hard cap 1000000).",
    ] = _DEFAULT_INSTRUCTION_LIMIT,
    memory_buffers: Annotated[
        list[dict] | None,
        "Optional scratch/input buffers written into the emulated address "
        "space: {address, size, data_hex, permissions}. permissions is 'r' or "
        "'rw' (default 'rw'); scratch memory is never executable and must not "
        "overlap IDB pages.",
    ] = None,
    execution_mode: Annotated[
        str,
        "'range' (default) for a straight instruction range, 'function' to run "
        "a function through an explicit calling convention.",
    ] = _MODE_RANGE,
    calling_convention: Annotated[
        str,
        "Required in function mode. x86: cdecl / stdcall / fastcall. x64: win64 / sysv64.",
    ] = "",
    arguments: Annotated[
        list | None,
        "Function-mode argument values (integers or hex strings), passed in "
        "declaration order through the calling convention. Rejected in range mode.",
    ] = None,
    code_ranges: Annotated[
        list[dict] | None,
        "Optional explicit executable IDB ranges ({address, size}). Execution "
        "outside the main range and these allowlist entries stops with range_exit.",
    ] = None,
    timeout_seconds: Annotated[
        float,
        "Wall-clock budget for the run (default 5.0, hard cap 20.0). A timeout "
        "returns partial state with status 'timeout'.",
    ] = _DEFAULT_TIMEOUT_SECONDS,
    collect_strings: Annotated[
        bool,
        "Scan memory the run modified and report the printable strings it produced (useful for stack strings).",
    ] = False,
) -> str:
    """Run a bounded, read-only Unicorn emulation of IDA code.

    Returns partial state — registers, instruction count, mapped ranges, write
    events, captured bytes and (optionally) discovered strings — plus a precise
    stop reason. Never modifies the IDB, follows no external calls, and adds no
    API or syscall stubs. ``registers`` must name at least one value in range
    mode; function mode may pass ``{}`` because the arguments carry the state.

    Status values: ``completed`` (reached the stop sentinel, including a
    function's ``ret``), ``range_exit``, ``instruction_limit``,
    ``unmapped_memory``, ``permission_error``,
    ``unsupported_instruction``, ``timeout``, ``cancelled``, ``emulator_error``.
    """

    start = _coerce_addr(start_address, ctx="emulate_code: start_address")
    stop = _coerce_addr(stop_address, ctx="emulate_code: stop_address")
    result = _run_tool(
        tool_name="emulate_code",
        start=start,
        stop=stop,
        registers=registers,
        memory_ranges=memory_ranges or (),
        code_ranges=code_ranges or (),
        memory_buffers=memory_buffers or (),
        capture_specs=_normalize_capture_specs(capture_ranges or (), tool_name="emulate_code", default_label="output"),
        implicit_capture=None,
        execution_mode=execution_mode,
        calling_convention=calling_convention,
        arguments=arguments or (),
        instruction_limit=instruction_limit,
        timeout_seconds=timeout_seconds,
        collect_strings=collect_strings,
    )
    return format_result(result)


@tool(category="emulation", timeout=30.0, main_thread=False)
def resolve_emulated_string(
    start_address: Annotated[str, "First instruction to execute (inclusive hex address)"],
    stop_address: Annotated[
        str,
        "Exclusive emulation end. In function mode this is the function's end "
        "address and doubles as the return sentinel a RET must land on.",
    ],
    registers: Annotated[
        dict,
        "Explicit initial CPU register state (see emulate_code); at least one "
        "value is required in range mode, and function mode may pass {} because "
        "the arguments carry the state. eip/rip are always taken from start_address.",
    ],
    output_address: Annotated[
        str,
        "Address of the decoded-string output buffer (int or '0x' hex string). "
        "Supply exactly one of output_address / output_stack_offset.",
    ] = "",
    max_output_size: Annotated[
        int,
        "Maximum bytes to scan for NUL terminators (default 4096, hard cap 4096).",
    ] = _MAX_OUTPUT_BYTES,
    memory_ranges: Annotated[
        list[dict] | None,
        "Optional extra IDB address ranges to map (encrypted input, key, lookup).",
    ] = None,
    instruction_limit: Annotated[
        int,
        "Upper bound on instructions executed (default 100000, hard cap 1000000).",
    ] = _DEFAULT_INSTRUCTION_LIMIT,
    memory_buffers: Annotated[
        list[dict] | None,
        "Optional scratch/input buffers: {address, size, data_hex, permissions}.",
    ] = None,
    execution_mode: Annotated[
        str,
        "'range' (default) or 'function'.",
    ] = _MODE_RANGE,
    calling_convention: Annotated[
        str,
        "Required in function mode. x86: cdecl / stdcall / fastcall. x64: win64 / sysv64.",
    ] = "",
    arguments: Annotated[
        list | None,
        "Function-mode argument values (integers or hex strings).",
    ] = None,
    code_ranges: Annotated[
        list[dict] | None,
        "Optional explicit executable IDB ranges ({address, size}).",
    ] = None,
    output_stack_offset: Annotated[
        int | None,
        "Signed offset from the entry SP to capture instead of output_address "
        "(e.g. -16 for a local buffer). Supply exactly one of the two.",
    ] = None,
    timeout_seconds: Annotated[
        float,
        "Wall-clock budget for the run (default 5.0, hard cap 20.0).",
    ] = _DEFAULT_TIMEOUT_SECONDS,
    collect_strings: Annotated[
        bool,
        "Also report printable strings found in memory the run modified.",
    ] = False,
) -> str:
    """Convenience wrapper around :func:`emulate_code` for decoded-string extraction.

    Runs the same bounded execution with an explicit output buffer and returns
    the raw bytes plus ASCII / UTF-8 / UTF-16LE candidate strings, plus the
    same status block ``emulate_code`` would report.
    """

    start = _coerce_addr(start_address, ctx="resolve_emulated_string: start_address")
    stop = _coerce_addr(stop_address, ctx="resolve_emulated_string: stop_address")
    has_address = output_address not in (None, "")
    has_offset = output_stack_offset is not None
    if has_address == has_offset:
        raise ToolError(
            "resolve_emulated_string: supply exactly one of output_address or output_stack_offset",
            tool_name="resolve_emulated_string",
        )
    if (
        isinstance(max_output_size, bool)
        or not isinstance(max_output_size, int)
        or not (1 <= max_output_size <= _MAX_OUTPUT_BYTES)
    ):
        raise ToolError(
            f"resolve_emulated_string: max_output_size must be in 1..{_MAX_OUTPUT_BYTES}",
            tool_name="resolve_emulated_string",
        )

    implicit_capture: dict[str, Any] = {"size": int(max_output_size)}
    if has_address:
        implicit_capture["address"] = _coerce_addr(
            output_address,
            ctx="resolve_emulated_string: output_address",
            tool_name="resolve_emulated_string",
        )
    else:
        implicit_capture["stack_offset"] = _coerce_signed_int(
            output_stack_offset,
            ctx="resolve_emulated_string: output_stack_offset",
            tool_name="resolve_emulated_string",
        )

    result = _run_tool(
        tool_name="resolve_emulated_string",
        start=start,
        stop=stop,
        registers=registers,
        memory_ranges=memory_ranges or (),
        code_ranges=code_ranges or (),
        memory_buffers=memory_buffers or (),
        capture_specs=_normalize_capture_specs((), tool_name="resolve_emulated_string", default_label="output"),
        implicit_capture=implicit_capture,
        execution_mode=execution_mode,
        calling_convention=calling_convention,
        arguments=arguments or (),
        instruction_limit=instruction_limit,
        timeout_seconds=timeout_seconds,
        collect_strings=collect_strings,
    )
    return format_result(result)
