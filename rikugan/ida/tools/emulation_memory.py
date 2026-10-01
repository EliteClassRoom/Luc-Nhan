"""Bounded, byte-faithful memory snapshots for the Unicorn emulation tools.

``snapshot_memory`` turns the requested virtual-address ranges of a run into a
``MemorySnapshot``: page-aligned mappings whose payloads hold the *exact* IDB
bytes at their source virtual addresses, plus synthetic regions for the
caller-supplied scratch buffers and the 1 MiB stack.

Fidelity rules (P0 memory fidelity):

* Initial bytes come from segment/page *intersections* read with
  ``ida_bytes.get_bytes(intersection_start, intersection_size)`` — never a
  whole-segment read, never a page-relative re-offset of segment bytes.
* Page permissions come from the IDA segment that owns those bytes and are
  never widened. Two segments with different permissions sharing one page
  fail explicitly instead of being OR-ed into RWX; pages with different
  permissions stay separate mappings even when adjacent.
* Synthetic scratch buffers may share a page only with other scratch, never
  with IDB-backed bytes, and only with identical permissions. Scratch is
  never executable. A buffer inside the stack initializes bytes in the
  existing stack mapping instead of mapping it twice.
* A missing byte is an explicit error, never a silent zero fill — the only
  exception is a segment IDA itself classifies as ``BSS``.
* Everything is bounded: a 1 MiB stack, <=16 captures of <=4096 bytes, and a
  16 MiB aggregate mapping cap that is checked before a byte is read.

The module imports no IDA symbol eagerly (``importlib`` only) and no
Unicorn. Callers must already be on the IDA host thread; the snapshot
performs no dispatch of its own.
"""

from __future__ import annotations

import importlib
import threading
import time
from collections.abc import Sequence
from typing import Any

from rikugan.core.errors import ToolError
from rikugan.core.logging import log_debug
from rikugan.ida.tools.emulation_types import (
    ArchMode,
    CaptureRequest,
    MemoryBuffer,
    MemoryRegion,
    MemorySnapshot,
)

__all__ = [
    "MAX_CAPTURES",
    "MAX_CAPTURE_BYTES",
    "MAX_TOTAL_BYTES",
    "PAGE_SIZE",
    "STACK_SIZE",
    "STACK_TOP_BIAS",
    "SnapshotAborted",
    "page_align_down",
    "page_align_up",
    "snapshot_memory",
]

_TOOL = "emulate_code"


class SnapshotAborted(ToolError):
    """Setup was cut short: the snapshot was discarded, not truncated.

    ``status`` is ``"timeout"`` or ``"cancelled"`` so the CPU runner can
    report an honest setup-aborted result with no captures instead of
    fabricating a partial mapping. Subclasses ``ToolError`` so existing
    ``except ToolError`` handlers keep working.
    """

    def __init__(self, message: str, status: str) -> None:
        super().__init__(message, tool_name=_TOOL)
        self.status = status


PAGE_SIZE = 0x1000
STACK_SIZE = 1 * 1024 * 1024
#: SP starts this far below the top of the stack mapping.
STACK_TOP_BIAS = 0x100
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_CAPTURE_BYTES = 4096
MAX_CAPTURES = 16

# Translated permission mask: 1 = read, 2 = write, 4 = execute.
_R = 1
_W = 2
_X = 4
_RW = _R | _W

# IDA ``segreg_t.perm`` bits: SEGPERM_EXEC=1, SEGPERM_WRITE=2, SEGPERM_READ=4.
_SEGPERM_EXEC = 1
_SEGPERM_WRITE = 2
_SEGPERM_READ = 4

# ---------------------------------------------------------------------------
# IDA imports — lazy ``importlib`` so the module loads without IDA Pro. The
# caller runs the snapshot on the IDA host thread; this module only reads.
# ---------------------------------------------------------------------------

ida_segment = ida_bytes = None
try:
    ida_segment = importlib.import_module("ida_segment")
    ida_bytes = importlib.import_module("ida_bytes")
except ImportError as e:  # pragma: no cover - exercised in non-IDA tests
    log_debug(f"IDA modules not available for emulation memory: {e}")


# ---------------------------------------------------------------------------
# Bounded address/page helpers (pure — no IDA dependency).
# ---------------------------------------------------------------------------


def page_align_down(address: int) -> int:
    return address & ~(PAGE_SIZE - 1)


def page_align_up(address: int) -> int:
    if address <= 0:
        return PAGE_SIZE
    return ((address + PAGE_SIZE - 1) // PAGE_SIZE) * PAGE_SIZE


def _merge(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping/adjacent half-open ``(start, end)`` ranges."""

    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def _page_count(span: tuple[int, int]) -> int:
    """Number of pages *span* touches — no set, so a huge span is cheap."""

    return (page_align_up(span[1]) - page_align_down(span[0])) // PAGE_SIZE


def _addr(value: Any, ctx: str, max_addr: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{ctx} must be an integer address, got {value!r}", tool_name=_TOOL)
    if value < 0 or value > max_addr:
        raise ToolError(
            f"{ctx} 0x{value:x} is outside the {max_addr.bit_length()}-bit address space",
            tool_name=_TOOL,
        )
    return value


def _size(value: Any, ctx: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{ctx} must be an integer byte count, got {value!r}", tool_name=_TOOL)
    if value <= 0:
        raise ToolError(f"{ctx} must be positive, got {value!r}", tool_name=_TOOL)
    return value


def _span(address: int, size: int, ctx: str, max_addr: int) -> tuple[int, int]:
    """Return the validated half-open ``[address, address + size)`` span."""

    if address + size > max_addr:
        raise ToolError(
            f"{ctx} 0x{address:x}+0x{size:x} overflows the {max_addr.bit_length()}-bit address space",
            tool_name=_TOOL,
        )
    return address, address + size


def _pair(item: Any, ctx: str) -> tuple[Any, Any]:
    """Unpack an ``(address, size)`` request item.

    Values are returned untouched so ``_addr``/``_size`` see the raw
    caller-supplied objects and reject ``bool``/``float`` strictly.
    """

    try:
        address, size = item
    except (TypeError, ValueError) as e:
        raise ToolError(f"{ctx} must be an (address, size) pair, got {item!r}", tool_name=_TOOL) from e
    return address, size


def _in_stack(span: tuple[int, int], stack: tuple[int, int]) -> bool:
    """True only when *span* lies entirely inside the stack mapping."""

    return span[0] >= stack[0] and span[1] <= stack[1]


def _touches_stack(span: tuple[int, int], stack: tuple[int, int]) -> bool:
    return span[0] < stack[1] and stack[0] < span[1]


def _split_spans(spans: Sequence[tuple[int, int]], span: tuple[int, int]) -> list[tuple[int, int]]:
    """Return *span* minus every part already covered by *spans*."""

    remaining = [span]
    for start, end in _merge(spans):
        nxt: list[tuple[int, int]] = []
        for part_start, part_end in remaining:
            if end <= part_start or start >= part_end:
                nxt.append((part_start, part_end))
                continue
            if part_start < start:
                nxt.append((part_start, start))
            if end < part_end:
                nxt.append((end, part_end))
        remaining = nxt
    return remaining


def _scratch_perms(value: Any, ctx: str) -> int:
    """Validate a caller-supplied scratch permission.

    Only ``1`` (read-only) and ``3`` (read/write) are accepted — the R/W/X
    bitmask the runner maps directly. Values are used exactly as given: a
    write-only ``2`` is rejected rather than silently promoted to ``3``,
    because quietly granting the read the caller did not ask for is the same
    class of surprise as widening an IDB page.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{ctx}.permissions must be an integer mask, got {value!r}", tool_name=_TOOL)
    if value not in (_R, _RW):
        raise ToolError(
            f"{ctx}.permissions {value} is not a supported scratch mask; "
            f"use {_R} for read-only or {_RW} for read/write",
            tool_name=_TOOL,
        )
    return value


def _require_covered(span: tuple[int, int], covered: Sequence[tuple[int, int]], what: str) -> None:
    """Raise unless every byte of *span* falls inside a *covered* range."""

    start, end = span
    cursor = start
    for cover_start, cover_end in covered:
        if cover_end <= cursor:
            continue
        if cover_start > cursor:
            break
        cursor = max(cursor, cover_end)
        if cursor >= end:
            return
    if cursor < end:
        raise ToolError(
            f"no backing bytes at 0x{cursor:x} for {what} 0x{start:x}..0x{end:x} "
            "(missing IDB bytes, a gap between segments, or a loader read failure)",
            tool_name=_TOOL,
        )


def _split_stack(span: tuple[int, int], stack: tuple[int, int]) -> list[tuple[int, int]]:
    """Split *span* around the synthetic stack mapping."""

    start, end = span
    if end <= stack[0] or start >= stack[1]:
        return [span]
    out = []
    if start < stack[0]:
        out.append((start, stack[0]))
    if end > stack[1]:
        out.append((stack[1], end))
    return out


# ---------------------------------------------------------------------------
# IDA-side readers.
# ---------------------------------------------------------------------------


def _require_ida() -> None:
    if ida_segment is None or ida_bytes is None:
        raise ToolError(
            "IDA segment/bytes modules are unavailable — emulation memory needs a live IDB",
            tool_name=_TOOL,
        )


def _check_setup(
    deadline: float | None,
    cancel_event: threading.Event | None,
    where: str,
) -> None:
    """Abort setup when the run is cancelled or its deadline has passed.

    ``deadline`` is an absolute ``time.monotonic()`` value; ``cancel_event``
    is a ``threading.Event``. Both are optional, and both are checked before
    allocations, before the IDB page loop, and once more before returning so
    a slow read of fewer than 16 pages cannot slip past the poll.
    """

    if cancel_event is not None and cancel_event.is_set():
        raise SnapshotAborted(f"emulation setup cancelled while {where}", "cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise SnapshotAborted(
            f"emulation setup exceeded its deadline while {where}; "
            "narrow the requested ranges or raise timeout_seconds",
            "timeout",
        )


def _seg_bounds(seg: Any) -> tuple[int, int]:
    """Return ``(start_ea, end_ea)`` for *seg*; raises when unusable."""
    try:
        return int(seg.start_ea), int(seg.end_ea)
    except (AttributeError, TypeError, ValueError) as e:
        raise ToolError(
            "IDA returned an unusable segment (no readable start_ea/end_ea) — "
            "emulation memory needs real segment bounds",
            tool_name=_TOOL,
        ) from e


def _seg_perms(seg: Any) -> int:
    """Translate IDA segment permissions into the R/W/X mask."""

    try:
        perm = int(seg.perm) & 0x7
    except (AttributeError, TypeError, ValueError):
        return _R  # unreadable perms: fall back to read-only, never widen
    out = 0
    if perm & _SEGPERM_READ:
        out |= _R
    if perm & _SEGPERM_WRITE:
        out |= _W
    if perm & _SEGPERM_EXEC:
        out |= _X
    return out or _R


def _is_bss(seg: Any) -> bool:
    """True when IDA itself classifies *seg* as zero-filled BSS.

    ``seg.type == ida_segment.SEG_BSS`` is the authoritative signal; the
    segment *class* string is the fallback. Either way the bytes are still
    read when IDA has them — a patched or initialised BSS page keeps its real
    contents, and only genuinely missing bytes are zero-filled.
    """

    seg_type = getattr(seg, "type", None)
    if isinstance(seg_type, int):
        marker = getattr(ida_segment, "SEG_BSS", None)
        if isinstance(marker, int) and seg_type == marker:
            return True
    getter = getattr(ida_segment, "get_segm_class", None)
    if callable(getter):
        try:
            return str(getter(seg) or "").strip().upper() == "BSS"
        except Exception:  # pragma: no cover - defensive
            pass
    return str(getattr(seg, "sclass", "") or "").strip().upper() == "BSS"


def _segments() -> list[tuple[int, int, int, bool]]:
    """Read the IDB segment table once: ``(start, end, perms, is_bss)``."""

    _require_ida()
    assert ida_segment is not None  # narrowed for the type checker; _require_ida raised otherwise
    try:
        count = int(ida_segment.get_segm_qty())
    except Exception as e:
        raise ToolError(f"IDA segment table unavailable: {e}", tool_name=_TOOL) from e
    out = []
    for index in range(max(count, 0)):
        seg = ida_segment.getnseg(index)
        start, end = _seg_bounds(seg)
        if end > start:
            out.append((start, end, _seg_perms(seg), _is_bss(seg)))
    out.sort()
    return out


def _read_page_bytes(address: int, size: int, is_bss: bool) -> bytes:
    """Read page bytes using IDA's authoritative defined/undefined mask.

    ``get_bytes`` cannot answer this: with the default ``GMB_READALL`` it
    returns ``size`` bytes for a range that is partly uninitialised, and a
    zero return value proves nothing about definedness.

    ``get_bytes_and_mask`` returns ``(bytes, mask)`` where the mask is
    **packed bits**, not one flag per byte: it is ``ceil(size / 8)`` bytes
    long, and byte *i* of the payload is defined iff
    ``mask[i // 8] & (1 << (i % 8))``. So three defined bytes give mask
    ``07`` (one byte), and sixteen undefined bytes give ``00 00`` (two
    bytes) — a length-16 mask would mean something entirely different.

    Consequently:

    * every requested byte must be defined for a non-BSS segment, otherwise the
      first undefined address is reported and nothing is zero-filled;
    * a genuine BSS segment keeps every defined byte and gets a zero exactly
      where the mask says the byte is undefined, interleaved positions
      included;
    * ``None`` (read failure) is always an error, even for BSS — it is not
      evidence that the bytes are missing.

    The payload must be exactly ``size`` bytes and the mask exactly
    ``ceil(size / 8)``; anything else is a loader failure, never a licence to
    invent zeros.
    """

    _require_ida()
    assert ida_bytes is not None  # narrowed for the type checker; _require_ida raised otherwise
    try:
        result = ida_bytes.get_bytes_and_mask(address, size)
    except Exception as e:
        raise ToolError(f"IDA byte read failed at 0x{address:x} ({size} bytes): {e}", tool_name=_TOOL) from e
    if result is None:
        raise ToolError(
            f"IDA returned no data for 0x{address:x}..0x{address + size:x} — a failed read "
            "is never treated as missing BSS bytes",
            tool_name=_TOOL,
        )
    try:
        data, mask = result
    except (TypeError, ValueError) as e:
        raise ToolError(
            f"IDA returned an unusable (bytes, mask) pair for 0x{address:x}..0x{address + size:x}",
            tool_name=_TOOL,
        ) from e
    data = bytes(data or b"")
    mask = bytes(mask or b"")
    expected_mask = (size + 7) // 8
    if len(data) != size or len(mask) != expected_mask:
        raise ToolError(
            f"IDA returned {len(data)} payload bytes and a {len(mask)}-byte mask for the "
            f"{size} bytes requested at 0x{address:x}..0x{address + size:x} "
            f"(expected {size} and {expected_mask}); refusing to guess the rest",
            tool_name=_TOOL,
        )
    undefined = next(
        (address + index for index in range(size) if not mask[index >> 3] & (1 << (index & 7))),
        None,
    )
    if is_bss:
        # Keep every defined byte; zero only where the mask says undefined.
        return bytes(byte if mask[index >> 3] & (1 << (index & 7)) else 0 for index, byte in enumerate(data))
    if undefined is not None:
        raise ToolError(
            f"IDA reports the byte at 0x{undefined:x} as undefined in "
            f"0x{address:x}..0x{address + size:x} — refusing to zero-fill a non-BSS segment",
            tool_name=_TOOL,
        )
    return data


# ---------------------------------------------------------------------------
# Snapshot.
# ---------------------------------------------------------------------------


def snapshot_memory(
    *,
    arch: ArchMode,
    start_address: int,
    stop_address: int,
    extra_ranges: Sequence[tuple[int, int]],
    captures: Sequence[CaptureRequest],
    memory_buffers: Sequence[MemoryBuffer] = (),
    code_ranges: Sequence[tuple[int, int]] = (),
    deadline: float | None = None,
    cancel_event: threading.Event | None = None,
) -> MemorySnapshot:
    """Map every byte the run can reach, holding the exact bytes IDA holds.

    *start_address*/*stop_address* is the executable code range;
    *extra_ranges* are additional IDB ranges; *code_ranges* is an explicit
    executable allowlist that must be backed by executable IDB bytes;
    *captures* are output ranges (IDB-backed, scratch-backed, or inside the
    synthetic stack); *memory_buffers* are caller-supplied scratch buffers.

    *deadline* is an absolute ``time.monotonic()`` value and *cancel_event* a
    ``threading.Event``. They bound setup only: they are checked before the
    allocations, before the IDB page loop, and once more before returning, and
    on expiry raise ``SnapshotAborted`` carrying ``status`` ``"timeout"`` or
    ``"cancelled"``. The snapshot is then discarded, never truncated — a
    partial mapping would silently drop requested bytes.

    Raises ``ToolError`` — never a partial or silently zero-filled snapshot —
    for invalid addresses/sizes, oversized inputs, missing IDB bytes, loader
    read failures, permission conflicts on a shared page, and buffers that
    overlap IDB pages or each other.
    """

    _check_setup(deadline, cancel_event, "validating the request")
    _require_ida()
    if arch.ptr_size not in (4, 8):
        raise ToolError(f"unsupported pointer size {arch.ptr_size!r}", tool_name=_TOOL)
    max_addr = (1 << (arch.ptr_size * 8)) - 1

    start = _addr(start_address, "start_address", max_addr)
    stop = _addr(stop_address, "stop_address", max_addr)
    if start >= stop:
        raise ToolError(f"start_address (0x{start:x}) must be < stop_address (0x{stop:x})", tool_name=_TOOL)
    # The entry range is code and is held to a stricter standard than the data
    # ranges around it; keeping the two lists separate makes that explicit.
    entry_span = (start, stop)
    extra_spans = _request_spans(extra_ranges, "memory_ranges", max_addr)
    code_spans = _request_spans(code_ranges, "code_ranges", max_addr)
    capture_list = list(captures)
    if len(capture_list) > MAX_CAPTURES:
        raise ToolError(
            f"{len(capture_list)} capture ranges exceed the {MAX_CAPTURES}-capture limit",
            tool_name=_TOOL,
        )
    capture_spans = []
    for cap in capture_list:
        label = getattr(cap, "label", "")
        size = _size(cap.size, f"capture '{label}' size")
        if size > MAX_CAPTURE_BYTES:
            raise ToolError(
                f"capture range '{label}' size {size} exceeds {MAX_CAPTURE_BYTES} bytes",
                tool_name=_TOOL,
            )
        capture_spans.append(
            _span(
                _addr(cap.address, f"capture '{label}' address", max_addr),
                size,
                f"capture '{label}'",
                max_addr,
            )
        )

    stack_start = _addr(arch.stack_base, "stack_base", max_addr)
    stack = (stack_start, stack_start + STACK_SIZE)
    buffers = _plan_buffers(memory_buffers, stack, max_addr)
    scratch_spans = [(address, end) for address, end, _d, _p in buffers if not _in_stack((address, end), stack)]

    # -- Cap first, on spans only.  Nothing is allocated and no byte is read
    #    before the aggregate is known to fit, so an absurd range costs
    #    arithmetic rather than memory.
    #
    #    Every requested span has the stack removed (the stack is charged once,
    #    at its real 1 MiB, below), then the survivors are page-aligned and
    #    merged so that disjoint requests sharing a page are charged once —
    #    counting them per span would double-charge a common case.
    outside_stack: list[tuple[int, int]] = []
    for span in [entry_span, *code_spans, *extra_spans, *capture_spans, *scratch_spans]:
        outside_stack.extend(_split_stack(span, stack))
    aligned = [(page_align_down(start), page_align_up(end)) for start, end in outside_stack]
    total_bytes = sum(end - start for start, end in _merge(aligned)) + STACK_SIZE
    if total_bytes > MAX_TOTAL_BYTES:
        raise ToolError(
            f"aggregate mapped memory {total_bytes:#x} (including the {STACK_SIZE:#x} stack) "
            f"exceeds the {MAX_TOTAL_BYTES:#x} cap",
            tool_name=_TOOL,
        )

    # -- IDB-backed bytes.  The entry range and the code allowlist always need
    #    real IDB bytes.  Extra input ranges and captures are data: they may sit
    #    on IDB bytes, on a declared scratch buffer, or inside the synthetic
    #    stack, so only their uncovered part drags in IDB pages.  A capture
    #    that straddles the stack keeps only its outside part, exactly as the
    #    cap accounted for it.
    _check_setup(deadline, cancel_event, "resolving required pages")
    idb_spans = [part for part in _split_stack(entry_span, stack)]
    idb_spans += [part for span in code_spans for part in _split_stack(span, stack)]
    for span in extra_spans:
        for part in _split_stack(span, stack):
            idb_spans.extend(_split_spans(scratch_spans, part))
    for span in capture_spans:
        for part in _split_stack(span, stack):
            idb_spans.extend(_split_spans(scratch_spans, part))

    idb_pages: set[int] = set()
    for span in idb_spans:
        for page in range(page_align_down(span[0]), page_align_up(span[1]), PAGE_SIZE):
            idb_pages.add(page)

    buffer_pages: set[int] = set()
    for address, end in scratch_spans:
        for page in range(page_align_down(address), page_align_up(end), PAGE_SIZE):
            buffer_pages.add(page)
    clashing = sorted(buffer_pages & idb_pages)
    if clashing:
        raise ToolError(
            f"synthetic buffer overlaps IDB-backed bytes on page 0x{clashing[0]:x}; "
            "place scratch outside the mapped code/data pages",
            tool_name=_TOOL,
        )

    # -- IDB pages: read only the page/segment intersections, never a whole
    #    segment and never a page-relative re-offset of one.
    segments = _segments()
    # Scratch may never sit on a page the IDB describes, even where the requested
    # ranges never touch those bytes: mapping RW scratch over a read-only IDB
    # page would hand the run a permission the database does not grant, and the
    # IDB bytes on that page would silently become scratch. So the check is
    # against the whole segment table, not just the pages we selected.
    for page in sorted(buffer_pages):
        page_end = page + PAGE_SIZE
        for seg_start, seg_end, _perms, _is_bss in segments:
            if seg_start < page_end and page < seg_end:
                raise ToolError(
                    f"synthetic buffer on page 0x{page:x} overlaps IDB segment "
                    f"0x{seg_start:x}..0x{seg_end:x}; scratch may not shadow IDB bytes",
                    tool_name=_TOOL,
                )
    # The stack must never stand in for real IDB bytes: if a segment actually
    # covers the stack, the synthetic zero page would silently replace it.
    # Touching the boundary is fine (the stack is page-aligned); overlapping a
    # single byte is not. Relocating the stack instead would break the ABI and
    # capture contract, so refuse explicitly.
    for seg_start, seg_end, _perms, _is_bss in segments:
        if seg_start < stack[1] and stack[0] < seg_end:
            raise ToolError(
                f"IDB segment 0x{seg_start:x}..0x{seg_end:x} overlaps the synthetic stack "
                f"0x{stack[0]:x}..0x{stack[1]:x}; the emulator will not shadow real bytes "
                "with the synthetic stack",
                tool_name=_TOOL,
            )
    page_perms: dict[int, int] = {}
    page_chunks: dict[int, list[tuple[int, bytes]]] = {}
    valid_idb: list[tuple[int, int, int]] = []
    for page_index, page in enumerate(sorted(idb_pages)):
        _check_setup(deadline, cancel_event, f"reading IDB page {page_index + 1}")
        page_end = page + PAGE_SIZE
        perms_seen: set[int] = set()
        for seg_start, seg_end, perms, is_bss in segments:
            lo = max(seg_start, page)
            hi = min(seg_end, page_end)
            if lo >= hi:
                continue
            perms_seen.add(perms)
            page_chunks.setdefault(page, []).append((lo - page, _read_page_bytes(lo, hi - lo, is_bss)))
            valid_idb.append((lo, hi, perms))
        if len(perms_seen) > 1:
            raise ToolError(
                f"page 0x{page:x} is covered by IDA segments with conflicting permissions "
                f"(R/W/X masks {sorted(perms_seen)}) — refusing to OR them into one mapping",
                tool_name=_TOOL,
            )
        if perms_seen:
            page_perms[page] = perms_seen.pop()

    # -- Scratch buffers.  A page already backed by IDB bytes was rejected
    #    above, so any shared page here is scratch-only and must agree on
    #    permissions.  Buffers are sliced per page: a multi-page buffer writes
    #    only the bytes of each page intersection.
    valid_buffers: list[tuple[int, int, int]] = []
    for address, end, data, perms in buffers:
        valid_buffers.append((address, end, perms))
        if _in_stack((address, end), stack):
            continue
        for page in range(page_align_down(address), page_align_up(end), PAGE_SIZE):
            existing = page_perms.get(page)
            if existing is None:
                page_perms[page] = perms
            elif existing != perms:
                raise ToolError(
                    f"synthetic buffer at 0x{address:x} needs permissions {perms} but page "
                    f"0x{page:x} is already mapped as {existing} — scratch never widens or "
                    "narrows an existing mapping",
                    tool_name=_TOOL,
                )
            lo = max(address, page)
            hi = min(end, page + PAGE_SIZE)
            page_chunks.setdefault(page, []).append((lo - page, data[lo - address : hi - address]))

    # -- Coverage.  The entry range is *code*: it must be IDB-backed and
    #    executable, which is a stricter bar than the data ranges around it.
    #    Extra input ranges and captures are data: they may sit on IDB bytes,
    #    on a declared scratch buffer, or anywhere inside the synthetic stack,
    #    which the runner already emits and marks valid.
    idb_covered = _merge([(s, e) for s, e, _p in valid_idb] + [(a, e) for a, e, _p in valid_buffers])
    # Everything a data request may legally read from.
    data_backed = _merge([*idb_covered, stack])
    executable = _merge([(s, e) for s, e, p in valid_idb if p & _X])
    _require_covered(entry_span, executable, "executable entry range")
    for span in code_spans:
        _require_covered(span, executable, "executable code range")
    for span in extra_spans:
        _require_covered(span, data_backed, "memory range")
    for span in capture_spans:
        _require_covered(span, data_backed, "capture range")

    # -- Assemble page-aligned regions.  Pages merge only when they are
    #    contiguous, share permissions, and share provenance, so a scratch
    #    region is never silently folded into IDB-backed memory.
    regions: list[MemoryRegion] = []
    payload = bytearray()
    group_start = 0
    group_perms = 0
    group_synthetic = False
    for page in sorted(page_perms):
        perms = page_perms[page]
        synthetic = page in buffer_pages
        page_data = bytearray(PAGE_SIZE)
        for offset, chunk in page_chunks.get(page, ()):
            page_data[offset : offset + len(chunk)] = chunk
        if payload and group_start + len(payload) == page and perms == group_perms and synthetic == group_synthetic:
            payload += page_data
            continue
        if payload:
            regions.append(
                MemoryRegion(
                    address=group_start,
                    size=len(payload),
                    permissions=group_perms,
                    data=bytes(payload),
                    synthetic=group_synthetic,
                )
            )
        payload = bytearray(page_data)
        group_start = page
        group_perms = perms
        group_synthetic = synthetic
    if payload:
        regions.append(
            MemoryRegion(
                address=group_start,
                size=len(payload),
                permissions=group_perms,
                data=bytes(payload),
                synthetic=group_synthetic,
            )
        )

    # -- Stack: mapped once, with any in-stack buffer bytes baked in.
    stack_data = bytearray(STACK_SIZE)
    for address, _end, data, _perms in buffers:
        if _in_stack((address, _end), stack):
            stack_data[address - stack[0] : address - stack[0] + len(data)] = data
    regions.append(
        MemoryRegion(
            address=stack[0],
            size=STACK_SIZE,
            permissions=_RW,
            data=bytes(stack_data),
            synthetic=True,
        )
    )

    # The whole stack is usable: push/pop, ABI shadow space, and stack
    # argument spills all run before any capture is taken.
    valid: list[tuple[int, int, int]] = list(valid_idb) + list(valid_buffers)
    valid.append((stack[0], stack[1], _RW))
    # Last gate before handing the mapping over: a run that expired during the
    # final assembly must not proceed to execute.
    _check_setup(deadline, cancel_event, "assembling the mapped regions")
    return MemorySnapshot(
        regions=regions,
        valid_ranges=_merge_valid(valid),
        stack_base=stack[0],
        stack_top=stack[1] - STACK_TOP_BIAS,
        total_bytes=sum(region.size for region in regions),
    )


def _request_spans(items: Sequence[Any], name: str, max_addr: int) -> list[tuple[int, int]]:
    """Validate a sequence of ``(address, size)`` requests into spans."""

    spans = []
    for index, item in enumerate(items):
        address, size = _pair(item, f"{name}[{index}]")
        spans.append(
            _span(
                _addr(address, f"{name}[{index}].address", max_addr),
                _size(size, f"{name}[{index}].size"),
                f"{name}[{index}]",
                max_addr,
            )
        )
    return spans


def _plan_buffers(
    memory_buffers: Sequence[MemoryBuffer],
    stack: tuple[int, int],
    max_addr: int,
) -> list[tuple[int, int, bytes, int]]:
    """Validate scratch buffers into ``(start, end, data, perms)`` tuples."""

    out: list[tuple[int, int, bytes, int]] = []
    for index, buf in enumerate(memory_buffers):
        ctx = f"memory_buffers[{index}]"
        size = _size(buf.size, f"{ctx}.size")
        address = _addr(buf.address, f"{ctx}.address", max_addr)
        span = _span(address, size, ctx, max_addr)
        data = bytes(buf.data or b"")
        if len(data) > size:
            raise ToolError(
                f"{ctx} carries {len(data)} bytes of data but declares size {size}",
                tool_name=_TOOL,
            )
        perms = _scratch_perms(buf.permissions, ctx)
        for other_start, other_end, _d, _p in out:
            if span[0] < other_end and other_start < span[1]:
                raise ToolError(
                    f"{ctx} 0x{span[0]:x}..0x{span[1]:x} overlaps another memory buffer "
                    f"0x{other_start:x}..0x{other_end:x}",
                    tool_name=_TOOL,
                )
        if _in_stack(span, stack):
            if perms != _RW:
                raise ToolError(
                    f"{ctx} requests read-only scratch inside the synthetic stack, which is "
                    "always R|W; initialize the buffer with permissions rw",
                    tool_name=_TOOL,
                )
        elif _touches_stack(span, stack):
            raise ToolError(
                f"{ctx} 0x{span[0]:x}..0x{span[1]:x} straddles the synthetic stack "
                f"0x{stack[0]:x}..0x{stack[1]:x}; place it fully inside or fully outside",
                tool_name=_TOOL,
            )
        out.append((span[0], span[1], data, perms))
    return out


def _merge_valid(ranges: Sequence[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """Sort and coalesce adjacent valid ranges that share permissions."""

    out: list[tuple[int, int, int]] = []
    for start, end, perms in sorted(ranges):
        if out and out[-1][1] == start and out[-1][2] == perms:
            out[-1] = (out[-1][0], end, perms)
        else:
            out.append((start, end, perms))
    return out
