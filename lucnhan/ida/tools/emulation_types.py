"""Shared value types for the bounded Unicorn emulation tool surface.

These dataclasses are the contract between the four emulation modules:

* :mod:`lucnhan.ida.tools.emulation_memory` — IDA-side memory snapshot
  (``MemoryBuffer`` / ``MemoryRegion`` / ``MemorySnapshot``).
* :mod:`lucnhan.ida.tools.emulation` — CPU runner, ABI setup, tool handlers
  (``ArchMode`` / ``CaptureRequest`` / ``EmulationResult``).
* :mod:`lucnhan.ida.tools.emulation_output` — string decoding, discovery and
  result formatting (``StringCandidate`` and friends).

Kept dependency-free on purpose: the memory and output workers import this
module without pulling in Unicorn, IDA, or the tool registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ArchMode",
    "CaptureRequest",
    "EmulationResult",
    "MemoryBuffer",
    "MemoryRegion",
    "MemorySnapshot",
    "StringCandidate",
]


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


@dataclass(frozen=True)
class CaptureRequest:
    """An output range the runner reads into ``captures`` at the end."""

    address: int
    size: int
    label: str  # human-readable key in the result dict


@dataclass(frozen=True)
class MemoryBuffer:
    """A caller-supplied scratch/input buffer written into the engine."""

    address: int
    size: int
    data: bytes
    permissions: int  # translated R/W mask: 1 = R, 2 = RW (never X)


@dataclass(frozen=True)
class MemoryRegion:
    """One mapped region with its exact payload as it exists at run start."""

    address: int
    size: int
    permissions: int  # translated R/W/X mask
    data: bytes
    synthetic: bool = False


@dataclass(frozen=True)
class MemorySnapshot:
    """Everything the CPU phase needs — no IDA handles survive the snapshot."""

    regions: list[MemoryRegion]
    valid_ranges: list[tuple[int, int, int]]  # start, exclusive end, R/W/X mask
    stack_base: int
    stack_top: int
    total_bytes: int


@dataclass(frozen=True)
class StringCandidate:
    """A printable string candidate found in emulated memory."""

    address: int
    encoding: str
    text: str
    terminated: bool


@dataclass
class EmulationResult:
    """Structured return value of the CPU runner (rendered by ``format_result``).

    ``status`` is one of: ``completed`` (the stop sentinel was reached, which
    in function mode means the callee returned), ``range_exit``,
    ``instruction_limit``, ``unmapped_memory``, ``permission_error``,
    ``unsupported_instruction``, ``timeout``, ``cancelled``,
    ``emulator_error``.
    """

    status: str = "emulator_error"
    reason: str = "(not executed)"
    entry_pc: int = 0
    stop_pc: int = 0
    instruction_count: int = 0
    architecture: str = "x86"
    mapped_ranges: list[tuple[int, int, int]] = field(default_factory=list)
    initial_registers: dict[str, int] = field(default_factory=dict)
    final_registers: dict[str, int] = field(default_factory=dict)
    writes: list[dict[str, Any]] = field(default_factory=list)
    write_event_count: int = 0
    captures: dict[str, bytes] = field(default_factory=dict)
    captured_strings: dict[str, dict[str, Any]] = field(default_factory=dict)
    discovered_strings: list[StringCandidate] = field(default_factory=list)
    discovery_truncated: bool = False
