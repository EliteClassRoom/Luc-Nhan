"""Byte-fidelity and bounding regressions for ``emulation_memory``.

Every test drives :func:`snapshot_memory` against a fake IDB (``ida_segment``
/ ``ida_bytes``) whose segment table and byte store are real dictionaries, so
the assertions are about the bytes the runner would load, not about the
implementation's internals.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()
importlib.invalidate_caches()

for _stale in ("lucnhan.ida.tools.emulation_memory",):
    sys.modules.pop(_stale, None)
emu_mem = importlib.import_module("lucnhan.ida.tools.emulation_memory")

from lucnhan.core.errors import ToolError
from lucnhan.ida.tools.emulation_types import (
    ArchMode,
    CaptureRequest,
    MemoryBuffer,
)

# IDA ``segreg_t.perm`` bits.
PERM_R, PERM_W, PERM_X = 4, 2, 1

#: The executable page every test enters at.
CODE_BASE = 0x401000

STACK64 = 0x7FFE_0000_0000
STACK32 = 0x7FFE_0000


def _arch(ptr_size: int = 8) -> ArchMode:
    return ArchMode(
        label="x64" if ptr_size == 8 else "x86",
        arch_const=4,  # unicorn.UC_ARCH_X86 — never touched here
        mode_const=2 if ptr_size == 4 else 8,
        ptr_size=ptr_size,
        ip_reg="rip" if ptr_size == 8 else "eip",
        sp_reg="rsp" if ptr_size == 8 else "esp",
        flags_reg="rflags" if ptr_size == 8 else "eflags",
        stack_base=STACK64 if ptr_size == 8 else STACK32,
    )


class FakeIdb:
    """Minimal but honest ``ida_segment`` / ``ida_bytes`` stand-in."""

    def __init__(self) -> None:
        # (start, end, perm, sclass)
        self.segments: list[tuple[int, int, int, str]] = []
        self.image: dict[int, int] = {}
        self.reads: list[tuple[int, int]] = []
        self.unmapped: list[int] = []
        self.fail_reads: set[int] = set()
        self.short_reads: set[int] = set()
        # Segments whose bytes IDA genuinely has no data for (loader zeroes).
        self.no_bytes: set[int] = set()
        # Individual addresses the mask must report as undefined.
        self.undefined: set[int] = set()
        # start address -> an exact (payload, packed mask) response, so a test
        # can reproduce the byte-for-byte shape a live database returns.
        self.mask_override: dict[int, tuple[bytes, bytes]] = {}
        # Optional hook invoked after every successful read.
        self.on_read = None
        self.SEG_BSS = 8
        self._install()

    # -- fixture construction -------------------------------------------
    def add_segment(
        self, start: int, end: int, perm: int = PERM_R | PERM_X, sclass: str = "CODE", bss: bool = False
    ) -> None:
        self.segments.append((start, end, perm, sclass, bss))
        self.segments.sort()

    def put(self, address: int, payload: bytes) -> None:
        for offset, byte in enumerate(payload):
            self.image[address + offset] = byte

    def _image_at(self, ea: int) -> int:
        # A real IDB has bytes everywhere inside a segment. Bytes written with
        # ``put`` win; otherwise a deterministic filler stands in for the file
        # image, unless a payload was written nearby — then the filler changes
        # byte so a mis-placed payload is visible in a diff.
        if ea in self.image:
            return self.image[ea]
        seed = 0x11 if not self.image else (min(self.image) & 0xFF)
        return ((ea * 7) + seed) & 0xFF

    # -- ida_segment surface --------------------------------------------
    def get_segm_qty(self) -> int:
        return len(self.segments)

    def getnseg(self, index: int) -> SimpleNamespace:
        start, end, perm, sclass, bss = self.segments[index]
        return SimpleNamespace(
            start_ea=start,
            end_ea=end,
            perm=perm,
            sclass=sclass,
            type=self.SEG_BSS if bss else 1,
        )

    def get_segm_class(self, seg: SimpleNamespace) -> str:
        return seg.sclass

    def getseg(self, ea: int) -> SimpleNamespace | None:
        for index in range(len(self.segments)):
            if self.segments[index][0] <= ea < self.segments[index][1]:
                return self.getnseg(index)
        return None

    # -- ida_bytes surface ----------------------------------------------
    def _is_defined(self, ea: int) -> bool:
        segment = self._segment_of(ea)
        if segment is None:
            self.unmapped.append(ea)
            return False
        if ea in self.undefined:
            return False
        if segment[0] in self.no_bytes:
            return False
        return True

    def get_bytes(self, address: int, size: int) -> bytes | None:
        # READALL semantics: the full length comes back even when part of the
        # range is uninitialised, so length alone proves nothing.
        if address in self.fail_reads:
            return None
        return bytes(self._image_at(address + i) for i in range(size))

    def get_bytes_and_mask(self, ea: int, size: int) -> tuple[bytes, bytes] | None:
        # Authoritative definedness as IDA reports it: the payload is `size`
        # bytes, the mask is ceil(size / 8) bytes of *packed* bits, LSB first
        # within each mask byte, one bit per payload byte.
        self.reads.append((ea, size))
        if ea in self.fail_reads:
            return None
        if ea in self.mask_override:
            payload, mask = self.mask_override[ea]
            return payload, mask
        if ea in self.short_reads:
            size = max(size - 1, 0)
        if size == 0:
            return b"", b""
        payload = bytearray()
        mask = bytearray((size + 7) // 8)
        for offset in range(size):
            address = ea + offset
            payload.append(self._image_at(address))
            if self._is_defined(address):
                mask[offset >> 3] |= 1 << (offset & 7)
        if self.on_read is not None:
            self.on_read()
        return bytes(payload), bytes(mask)

    def _segment_of(self, ea: int) -> tuple[int, int, int, str, bool] | None:
        for segment in self.segments:
            if segment[0] <= ea < segment[1]:
                return segment
        return None

    def _install(self) -> None:
        self._saved = (emu_mem.ida_segment, emu_mem.ida_bytes)
        emu_mem.ida_segment = self
        emu_mem.ida_bytes = self

    def restore(self) -> None:
        emu_mem.ida_segment, emu_mem.ida_bytes = self._saved


class MemoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.idb = FakeIdb()
        self.addCleanup(self.idb.restore)
        self.arch = _arch(8)
        # The entry range is code, so every test needs a real executable page
        # to enter at. Tests that care about data add their own segments and
        # reach them through ``extra_ranges`` rather than the entry.
        self.idb.add_segment(CODE_BASE, CODE_BASE + 0x1000, PERM_R | PERM_X)

    def snap(self, **kwargs):
        params = {
            "arch": self.arch,
            "start_address": CODE_BASE,
            "stop_address": CODE_BASE + 0x10,
            "extra_ranges": [],
            "captures": [],
        }
        params.update(kwargs)
        return emu_mem.snapshot_memory(**params)

    def region_at(self, snapshot, address: int):
        for region in snapshot.regions:
            if region.address <= address < region.address + region.size:
                return region
        return None

    def bytes_at(self, snapshot, address: int, size: int) -> bytes:
        out = bytearray()
        remaining = size
        cursor = address
        while remaining:
            region = self.region_at(snapshot, cursor)
            self.assertIsNotNone(region, f"no region at 0x{cursor:x}")
            offset = cursor - region.address
            take = min(remaining, region.size - offset)
            out += region.data[offset : offset + take]
            cursor += take
            remaining -= take
        return bytes(out)

    def valid(self, snapshot, address: int, size: int) -> bool:
        return any(start <= address and address + size <= end for start, end, _p in snapshot.valid_ranges)


class TestExactIdbBytes(MemoryTestCase):
    def test_entry_inside_segment_reads_its_own_bytes(self) -> None:
        # Entry 0x402000 is 0x1000 into a segment that starts at 0x401000:
        # a page-relative re-offset of the segment payload would be wrong.
        self.idb.add_segment(0x401000, 0x403000, PERM_R | PERM_X)
        self.idb.put(0x402000, b"\x48\x89\xe5")
        snapshot = self.snap(start_address=0x402000, stop_address=0x402003)
        self.assertEqual(self.bytes_at(snapshot, 0x402000, 3), b"\x48\x89\xe5")
        # The bytes must come from page/segment intersection reads only: one
        # page read starting at the page base, and no whole-segment read.
        self.assertEqual(self.idb.reads, [(0x402000, 0x1000)])

    def test_non_page_aligned_segment_boundary_is_respected(self) -> None:
        # A segment that starts mid-page and ends mid-page, on a page the entry
        # range never touches, so its partial coverage is the only thing there.
        base = CODE_BASE + 0x1000
        self.idb.add_segment(base + 0x234, base + 0x300, PERM_R | PERM_X)
        self.idb.put(base + 0x234, b"\x90\x90\x90\x90")
        snapshot = self.snap(start_address=base + 0x240, stop_address=base + 0x248)
        self.assertEqual(
            self.bytes_at(snapshot, base + 0x240, 8),
            self.idb.get_bytes(base + 0x240, 8),
        )
        self.assertTrue(self.valid(snapshot, base + 0x240, 8))
        # Bytes before the segment start are page padding, not IDB bytes.
        self.assertFalse(self.valid(snapshot, base, 0x240))

    def test_cross_segment_input_keeps_both_venas(self) -> None:
        self.idb.add_segment(0x402000, 0x402100, PERM_R | PERM_W)
        self.idb.add_segment(0x403000, 0x403100, PERM_R | PERM_W)
        self.idb.put(0x402000, b"\xaa" * 0x100)
        self.idb.put(0x403000, b"\xbb" * 0x100)
        snapshot = self.snap(
            extra_ranges=[(0x402080, 0x10), (0x403080, 0x10)],
        )
        self.assertEqual(self.bytes_at(snapshot, 0x402080, 0x10), b"\xaa" * 0x10)
        self.assertEqual(self.bytes_at(snapshot, 0x403080, 0x10), b"\xbb" * 0x10)
        self.assertTrue(self.valid(snapshot, 0x402080, 0x10))
        self.assertTrue(self.valid(snapshot, 0x403080, 0x10))

    def test_extra_buffer_bytes_are_returned_at_their_va(self) -> None:
        self.idb.add_segment(0x501000, 0x501100, PERM_R | PERM_W)
        self.idb.put(0x501000, bytes.fromhex("78563412") + b"\x44" * 0xFC)
        snapshot = self.snap(extra_ranges=[(0x501000, 0x10)])
        self.assertEqual(self.bytes_at(snapshot, 0x501000, 4), bytes.fromhex("78563412"))

    def test_short_mask_response_is_rejected_instead_of_guessed(self) -> None:
        self.idb.short_reads.add(CODE_BASE)
        with self.assertRaises(ToolError) as ctx:
            self.snap()
        self.assertIn("refusing to guess", str(ctx.exception))

    def test_loader_failure_is_rejected_instead_of_zero_filled(self) -> None:
        self.idb.fail_reads.add(CODE_BASE)
        with self.assertRaises(ToolError) as ctx:
            self.snap()
        self.assertIn("failed read", str(ctx.exception))

    def test_bss_segment_with_stored_bytes_keeps_them(self) -> None:
        # A BSS page that IDA has bytes for (initialised or patched) must keep
        # them — the class alone must not discard real data.
        self.idb.add_segment(0x404000, 0x405000, PERM_R | PERM_W, sclass="BSS", bss=True)
        self.idb.put(0x404010, b"KEEP")
        snapshot = self.snap(extra_ranges=[(0x404010, 0x10)])
        self.assertEqual(self.bytes_at(snapshot, 0x404010, 4), b"KEEP")
        self.assertTrue(self.idb.reads)

    def test_genuine_bss_with_no_stored_bytes_zero_fills(self) -> None:
        self.idb.add_segment(0x404000, 0x405000, PERM_R | PERM_W, sclass="BSS", bss=True)
        self.idb.no_bytes.add(0x404000)
        snapshot = self.snap(extra_ranges=[(0x404010, 0x10)])
        self.assertEqual(self.bytes_at(snapshot, 0x404010, 0x10), b"\x00" * 0x10)
        self.assertTrue(self.valid(snapshot, 0x404010, 0x10))


class TestPermissions(MemoryTestCase):
    def test_adjacent_rx_and_rw_pages_stay_separate_mappings(self) -> None:
        self.idb.add_segment(0x402000, 0x403000, PERM_R | PERM_W)
        snapshot = self.snap(
            start_address=CODE_BASE,
            stop_address=CODE_BASE + 0x10,
            extra_ranges=[(0x402000, 0x10)],
        )
        code_region = self.region_at(snapshot, CODE_BASE)
        data_region = self.region_at(snapshot, 0x402000)
        self.assertEqual(code_region.permissions, 5)  # R|X
        self.assertEqual(data_region.permissions, 3)  # R|W
        self.assertIsNot(code_region, data_region)
        self.assertFalse(code_region.permissions & 2)  # code stays non-writable
        self.assertFalse(data_region.permissions & 4)  # data stays non-executable

    def test_rw_scratch_next_to_rx_code_cannot_widen_code(self) -> None:
        snapshot = self.snap(memory_buffers=[MemoryBuffer(0x501000, 0x10, b"\x00" * 4, 3)])
        self.assertEqual(self.region_at(snapshot, CODE_BASE).permissions, 5)
        self.assertEqual(self.region_at(snapshot, 0x501000).permissions, 3)
        self.assertFalse(self.region_at(snapshot, CODE_BASE).permissions & 2)

    def test_conflicting_segment_permissions_on_one_page_fail(self) -> None:
        self.idb.add_segment(CODE_BASE, CODE_BASE + 0x800, PERM_R | PERM_X)
        self.idb.add_segment(CODE_BASE + 0x800, CODE_BASE + 0x1000, PERM_R | PERM_W)
        with self.assertRaises(ToolError) as ctx:
            self.snap()
        self.assertIn("conflicting permissions", str(ctx.exception))

    def test_scratch_never_executable(self) -> None:
        with self.assertRaises(ToolError):
            self.snap(memory_buffers=[MemoryBuffer(0x501000, 0x10, b"", 7)])

    def test_scratch_pages_with_different_permissions_fail(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.snap(
                memory_buffers=[
                    MemoryBuffer(0x501000, 0x10, b"", 3),
                    MemoryBuffer(0x501010, 0x10, b"", 1),
                ]
            )
        self.assertIn("permissions", str(ctx.exception))


class TestSyntheticBuffers(MemoryTestCase):
    def test_buffer_bytes_land_at_their_va(self) -> None:
        snapshot = self.snap(memory_buffers=[MemoryBuffer(0x501000, 8, bytes.fromhex("78563412"), 3)])
        self.assertEqual(self.bytes_at(snapshot, 0x501000, 4), bytes.fromhex("78563412"))
        self.assertTrue(self.valid(snapshot, 0x501000, 8))
        # Neighbouring scratch bytes outside the buffer stay invalid.
        self.assertFalse(self.valid(snapshot, 0x501010, 1))

    def test_short_buffer_data_zero_fills_its_own_tail(self) -> None:
        snapshot = self.snap(memory_buffers=[MemoryBuffer(0x501000, 8, bytes.fromhex("7856"), 3)])
        self.assertEqual(self.bytes_at(snapshot, 0x501000, 8), b"\x78\x56" + b"\x00" * 6)

    def test_buffer_may_share_a_page_with_another_buffer(self) -> None:
        snapshot = self.snap(
            memory_buffers=[
                MemoryBuffer(0x501000, 0x10, b"\xaa" * 0x10, 3),
                MemoryBuffer(0x501800, 0x10, b"\xbb" * 0x10, 3),
            ]
        )
        self.assertEqual(self.bytes_at(snapshot, 0x501000, 0x10), b"\xaa" * 0x10)
        self.assertEqual(self.bytes_at(snapshot, 0x501800, 0x10), b"\xbb" * 0x10)

    def test_buffer_overlapping_another_buffer_is_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.snap(
                memory_buffers=[
                    MemoryBuffer(0x501000, 0x20, b"", 3),
                    MemoryBuffer(0x501010, 0x20, b"", 3),
                ]
            )
        self.assertIn("overlaps another memory buffer", str(ctx.exception))

    def test_buffer_overlapping_idb_bytes_is_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.snap(memory_buffers=[MemoryBuffer(0x401800, 0x10, b"", 3)])
        self.assertIn("overlaps IDB-backed bytes", str(ctx.exception))

    def test_data_longer_than_declared_size_is_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.snap(memory_buffers=[MemoryBuffer(0x501000, 2, b"\xaa\xbb\xcc", 3)])

    def test_buffer_in_stack_reuses_the_stack_mapping(self) -> None:
        snapshot = self.snap(
            captures=[CaptureRequest(self.arch.stack_base + 0x100, 4, "out")],
            memory_buffers=[
                MemoryBuffer(self.arch.stack_base + 0x100, 4, b"SEED", 3),
            ],
        )
        stack_regions = [r for r in snapshot.regions if r.synthetic]
        self.assertEqual(len(stack_regions), 1)
        stack = stack_regions[0]
        self.assertEqual(stack.address, self.arch.stack_base)
        self.assertEqual(stack.size, emu_mem.STACK_SIZE)
        self.assertEqual(stack.permissions, 3)
        self.assertEqual(stack.data[0x100:0x104], b"SEED")
        self.assertTrue(self.valid(snapshot, self.arch.stack_base + 0x100, 4))

    def test_read_only_buffer_in_stack_is_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.snap(memory_buffers=[MemoryBuffer(self.arch.stack_base + 0x100, 4, b"SEED", 1)])

    def test_buffer_straddling_the_stack_is_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.snap(
                memory_buffers=[
                    MemoryBuffer(self.arch.stack_base - 0x10, 0x20, b"", 3),
                ]
            )


class TestCaptures(MemoryTestCase):
    def test_stack_capture_does_not_add_a_second_stack_mapping(self) -> None:
        snapshot = self.snap(captures=[CaptureRequest(self.arch.stack_base + 0x40, 0x20, "sp_out")])
        mapped_at_stack = [r for r in snapshot.regions if r.address == self.arch.stack_base]
        self.assertEqual(len(mapped_at_stack), 1)
        self.assertTrue(mapped_at_stack[0].synthetic)
        self.assertTrue(self.valid(snapshot, self.arch.stack_base + 0x40, 0x20))

    def test_idb_capture_is_backed_by_idb_bytes(self) -> None:
        self.idb.put(0x401800, b"OUT!")
        snapshot = self.snap(captures=[CaptureRequest(0x401800, 4, "out")])
        self.assertEqual(self.bytes_at(snapshot, 0x401800, 4), b"OUT!")
        self.assertTrue(self.valid(snapshot, 0x401800, 4))

    def test_capture_beyond_mapped_bytes_is_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.snap(captures=[CaptureRequest(0x501000, 4, "out")])
        self.assertIn("no backing bytes", str(ctx.exception))

    def test_capture_size_cap(self) -> None:
        self.idb.add_segment(0x501000, 0x502000, PERM_R | PERM_W)
        with self.assertRaises(ToolError):
            self.snap(captures=[CaptureRequest(0x501000, 4097, "out")])

    def test_capture_count_cap(self) -> None:
        self.idb.add_segment(0x501000, 0x502000, PERM_R | PERM_W)
        caps = [CaptureRequest(0x501000 + i * 0x10, 0x10, f"c{i}") for i in range(17)]
        with self.assertRaises(ToolError) as ctx:
            self.snap(captures=caps)
        self.assertIn("17", str(ctx.exception))


class TestBounds(MemoryTestCase):
    def test_hole_between_segments_is_rejected(self) -> None:
        self.idb.add_segment(0x403000, 0x404000, PERM_R | PERM_W)
        with self.assertRaises(ToolError) as ctx:
            self.snap(extra_ranges=[(0x401FF0, 0x20)])
        self.assertIn("no backing bytes", str(ctx.exception))

    def test_read_outside_any_segment_is_rejected(self) -> None:
        self.idb.add_segment(0x501000, 0x501010, PERM_R | PERM_W)
        self.idb.undefined.update(range(0x501000, 0x501010))
        with self.assertRaises(ToolError) as ctx:
            self.snap(extra_ranges=[(0x501000, 0x10)])
        self.assertIn("undefined", str(ctx.exception))

    def test_address_within_a_page_but_outside_every_segment_is_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.snap(extra_ranges=[(0x501000, 0x10)])
        self.assertIn("no backing bytes", str(ctx.exception))
        self.assertEqual([read for read in self.idb.reads if read[0] >= 0x500000], [])

    def test_start_after_stop_is_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.snap(start_address=CODE_BASE + 0x10, stop_address=CODE_BASE)

    def test_negative_and_overflowing_addresses_are_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.snap(start_address=-1, stop_address=CODE_BASE)
        with self.assertRaises(ToolError):
            self.snap(extra_ranges=[(0xFFFF_FFFF_FFFF_FFF0, 0x20)])

    def test_non_positive_and_bool_sizes_are_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.snap(extra_ranges=[(0x501000, 0)])
        with self.assertRaises(ToolError):
            self.snap(extra_ranges=[(0x501000, -4)])
        with self.assertRaises(ToolError):
            self.snap(extra_ranges=[(0x501000, True)])

    def test_aggregate_cap_includes_the_stack(self) -> None:
        # 16 MiB is 4096 pages. 16 code pages + 4095 input pages + the stack
        # page crosses the cap.
        self.idb.add_segment(CODE_BASE, CODE_BASE + 0x10000, PERM_R | PERM_X)
        self.idb.add_segment(0x600000, 0x15F0000, PERM_R | PERM_W)
        with self.assertRaises(ToolError) as ctx:
            self.snap(
                start_address=CODE_BASE,
                stop_address=CODE_BASE + 0x10000,
                extra_ranges=[(0x600000, 0xFF0000)],
            )
        self.assertIn("cap", str(ctx.exception))

    def test_no_bytes_are_read_when_the_cap_is_exceeded(self) -> None:
        self.idb.add_segment(CODE_BASE, CODE_BASE + 0x10000, PERM_R | PERM_X)
        self.idb.add_segment(0x600000, 0x15F0000, PERM_R | PERM_W)
        self.idb.reads.clear()
        with self.assertRaises(ToolError):
            self.snap(
                start_address=CODE_BASE,
                stop_address=CODE_BASE + 0x10000,
                extra_ranges=[(0x600000, 0xFF0000)],
            )
        self.assertEqual(self.idb.reads, [])

    def test_x86_address_space_rejects_64_bit_addresses(self) -> None:
        self.arch = _arch(4)
        with self.assertRaises(ToolError):
            self.snap(extra_ranges=[(0x1_0000_0000, 0x10)])

    def test_stack_top_sits_one_reservation_below_the_stack_end(self) -> None:
        snapshot = self.snap()
        self.assertEqual(snapshot.stack_base, self.arch.stack_base)
        self.assertEqual(snapshot.stack_top, self.arch.stack_base + emu_mem.STACK_SIZE - 0x100)
        self.assertEqual(snapshot.stack_top + 0x100, self.arch.stack_base + emu_mem.STACK_SIZE)


class TestCodeRanges(MemoryTestCase):
    def test_rw_code_range_gains_executable(self) -> None:
        # "Exec what was asked": an explicitly requested code range runs even
        # when the segment is R|W (packed binaries). X is granted on top of
        # the real permissions; W is never added anywhere.
        self.idb.add_segment(0x404000, 0x405000, PERM_R | PERM_W)
        snapshot = self.snap(code_ranges=[(0x404000, 0x10)])
        perms = next(p for s, e, p in snapshot.valid_ranges if s <= 0x404000 < e)
        self.assertEqual(perms, PERM_R | PERM_W | PERM_X)

    def test_executable_code_range_is_accepted(self) -> None:
        snapshot = self.snap(code_ranges=[(0x401000, 0x10)])
        self.assertTrue(self.valid(snapshot, 0x401000, 0x10))

    def test_code_range_hole_is_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.snap(code_ranges=[(0x501000, 0x10)])


class TestPageHelpers(unittest.TestCase):
    def test_alignment(self) -> None:
        self.assertEqual(emu_mem.page_align_down(0x401001), 0x401000)
        self.assertEqual(emu_mem.page_align_up(0x401001), 0x402000)
        self.assertEqual(emu_mem.page_align_up(0x401000), 0x401000)


class TestScratchAndStackMapping(MemoryTestCase):
    """How scratch buffers and the synthetic stack are placed and mapped."""

    def test_capture_inside_a_scratch_buffer_reuses_that_mapping(self) -> None:
        # The output buffer is an unwritten scratch page, not IDB bytes.
        snapshot = self.snap(
            captures=[CaptureRequest(0x501010, 8, "out")],
            memory_buffers=[MemoryBuffer(0x501000, 0x20, b"\x00" * 0x20, 3)],
        )
        self.assertEqual(self.bytes_at(snapshot, 0x501010, 8), b"\x00" * 8)
        self.assertTrue(self.valid(snapshot, 0x501010, 8))
        self.assertEqual([r for r in self.idb.reads if r[0] >= 0x500000], [])

    def test_multi_page_buffer_keeps_offsets_and_valid_size(self) -> None:
        # 0x501FF0..0x502010 crosses a page boundary; the tail of page 1 and
        # the head of page 2 must land at the right offsets, and only the
        # declared bytes stay valid.
        payload = bytes(range(0x20))
        snapshot = self.snap(
            memory_buffers=[MemoryBuffer(0x501FF0, 0x20, payload, 3)],
            captures=[CaptureRequest(0x501FF0, 0x20, "out")],
        )
        self.assertEqual(self.bytes_at(snapshot, 0x501FF0, 0x20), payload)
        self.assertTrue(self.valid(snapshot, 0x501FF0, 0x20))
        self.assertFalse(self.valid(snapshot, 0x501F00, 0xF0))
        self.assertFalse(self.valid(snapshot, 0x502010, 0x10))
        for region in snapshot.regions:
            self.assertEqual(len(region.data), region.size)
            self.assertEqual(region.address % 0x1000, 0)

    def test_scratch_above_the_stack_is_not_treated_as_in_stack(self) -> None:
        above = self.arch.stack_base + emu_mem.STACK_SIZE
        snapshot = self.snap(memory_buffers=[MemoryBuffer(above, 0x20, b"\xab" * 0x20, 3)])
        scratch = self.region_at(snapshot, above)
        self.assertIsNotNone(scratch)
        self.assertTrue(scratch.synthetic)
        self.assertEqual(scratch.size, 0x1000)
        self.assertEqual(scratch.permissions, 3)
        self.assertEqual(self.bytes_at(snapshot, above, 0x20), b"\xab" * 0x20)
        stack_region = next(r for r in snapshot.regions if r.address == self.arch.stack_base)
        self.assertEqual(stack_region.size, emu_mem.STACK_SIZE)

    def test_scratch_and_idb_regions_stay_distinct(self) -> None:
        snapshot = self.snap(memory_buffers=[MemoryBuffer(0x501000, 0x20, b"\x01", 3)])
        scratch = self.region_at(snapshot, 0x501000)
        code = self.region_at(snapshot, 0x401000)
        self.assertTrue(scratch.synthetic)
        self.assertFalse(code.synthetic)

    def test_entire_stack_is_valid_memory(self) -> None:
        # push/pop and ABI shadow-space spills run long before any capture.
        snapshot = self.snap()
        stack = self.arch.stack_base
        self.assertTrue(self.valid(snapshot, stack, emu_mem.STACK_SIZE))
        self.assertTrue(self.valid(snapshot, snapshot.stack_top, 0x100))
        self.assertTrue(self.valid(snapshot, stack + 0x28, 0x20))
        self.assertTrue(self.valid(snapshot, stack, 8))
        self.assertFalse(self.valid(snapshot, stack + emu_mem.STACK_SIZE, 1))

    def test_stack_costs_a_full_megabyte_in_the_cap(self) -> None:
        # 4095 IDB pages + the 1 MiB stack is exactly the cap; one more page
        # must fail — and fail before any page set or byte read is built.
        self.idb.add_segment(0x600000, 0x15F0000, PERM_R | PERM_W)
        self.idb.reads.clear()
        with self.assertRaises(ToolError) as ctx:
            self.snap(
                start_address=0x600000,
                stop_address=0x600000 + 0xFFF000,
                extra_ranges=[(0x600000, 0xFFF000)],
            )
        self.assertIn("cap", str(ctx.exception))
        self.assertEqual(self.idb.reads, [])

    def test_stack_size_is_not_counted_as_a_single_page(self) -> None:
        # The whole 1 MiB stack is charged: a run that maps just under the
        # cap for IDB pages alone still fails once the stack is added.
        self.idb.add_segment(0x600000, 0x15F0000, PERM_R | PERM_W)
        with self.assertRaises(ToolError):
            self.snap(
                start_address=0x600000,
                stop_address=0x600000 + 0xFFF000,
            )

    def test_float_and_bool_range_values_are_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.snap(extra_ranges=[(4198400.0, 0x10)])
        with self.assertRaises(ToolError):
            self.snap(extra_ranges=[(0x501000, 16.0)])
        with self.assertRaises(ToolError):
            self.snap(code_ranges=[(True, 0x10)])

    def test_bss_class_with_missing_bytes_still_zero_fills(self) -> None:
        self.idb.add_segment(0x404000, 0x405000, PERM_R | PERM_W, sclass="BSS", bss=True)
        self.idb.no_bytes.add(0x404000)
        snapshot = self.snap(extra_ranges=[(0x404000, 0x10)])
        self.assertEqual(self.bytes_at(snapshot, 0x404000, 0x10), b"\x00" * 0x10)


class TestSetupBudget(MemoryTestCase):
    """``deadline`` / ``cancel_event`` bound the work setup may do."""

    def setUp(self) -> None:
        super().setUp()
        self.idb.add_segment(0x600000, 0x601000, PERM_R | PERM_W)

    def test_expired_deadline_discards_the_snapshot(self) -> None:
        with self.assertRaises(emu_mem.SnapshotAborted) as ctx:
            self.snap(extra_ranges=[(0x600000, 0x10)], deadline=time.monotonic() - 1.0)
        self.assertEqual(ctx.exception.status, "timeout")

    def test_set_event_reports_cancellation(self) -> None:
        event = threading.Event()
        event.set()
        with self.assertRaises(emu_mem.SnapshotAborted) as ctx:
            self.snap(extra_ranges=[(0x600000, 0x10)], cancel_event=event)
        self.assertEqual(ctx.exception.status, "cancelled")

    def test_aborted_snapshot_is_a_tool_error(self) -> None:
        # Callers that only catch ToolError still see the abort.
        with self.assertRaises(ToolError):
            self.snap(extra_ranges=[(0x600000, 0x10)], cancel_event=_set_event())

    def test_open_signals_return_a_complete_snapshot(self) -> None:
        snapshot = self.snap(
            extra_ranges=[(0x600000, 0x10)],
            deadline=time.monotonic() + 300.0,
            cancel_event=threading.Event(),
        )
        # One code page, one data page, and the 1 MiB stack.
        self.assertEqual(snapshot.total_bytes, 2 * 0x1000 + emu_mem.STACK_SIZE)
        self.assertTrue(self.valid(snapshot, CODE_BASE, 0x10))
        self.assertTrue(self.valid(snapshot, 0x600000, 0x10))
        self.assertTrue(self.valid(snapshot, self.arch.stack_base, emu_mem.STACK_SIZE))

    def test_no_signals_means_no_abort(self) -> None:
        snapshot = self.snap(extra_ranges=[(0x600000, 0x10)])
        self.assertEqual(snapshot.total_bytes, 2 * 0x1000 + emu_mem.STACK_SIZE)
        self.assertTrue(self.valid(snapshot, 0x600000, 0x10))

    def test_expiry_after_the_last_read_is_caught(self) -> None:
        # A single page that only expires once its bytes have been read: the
        # pre-return gate has to notice, since no further page will be polled.
        clock = _FakeClock()
        self.idb.on_read = clock.expire_after_first_read
        with _patched_monotonic(clock):
            with self.assertRaises(emu_mem.SnapshotAborted) as ctx:
                self.snap(extra_ranges=[(0x600000, 0x10)], deadline=clock.now + 10.0)
        self.assertEqual(ctx.exception.status, "timeout")
        self.assertTrue(self.idb.reads)

    def test_deadline_is_checked_before_any_idb_read(self) -> None:
        self.idb.reads.clear()
        with self.assertRaises(emu_mem.SnapshotAborted):
            self.snap(extra_ranges=[(0x600000, 0x10)], deadline=time.monotonic() - 1.0)
        self.assertEqual(self.idb.reads, [])

    def test_cancellation_during_reads_is_caught(self) -> None:
        event = _set_event()
        with self.assertRaises(emu_mem.SnapshotAborted):
            self.snap(extra_ranges=[(0x600000, 0x10)], cancel_event=event)


class _FakeClock:
    """Monotonic clock the fake IDB can push past a deadline."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.expired = False

    def expire_after_first_read(self) -> None:
        """Called by the fake IDB once it has served a page."""
        self.expired = True

    def __call__(self) -> float:
        return self.now + (10.0 if self.expired else 0.0)


class _patched_monotonic:
    """Swap ``time.monotonic`` for a fake clock for the duration of a test."""

    def __init__(self, clock: _FakeClock) -> None:
        self._clock = clock
        self._original = time.monotonic

    def __enter__(self) -> _FakeClock:
        time.monotonic = self._clock
        return self._clock

    def __exit__(self, *exc: object) -> None:
        time.monotonic = self._original


def _set_event() -> threading.Event:
    event = threading.Event()
    event.set()
    return event


class TestStackBoundaryMapping(MemoryTestCase):
    """Placement of ranges at the synthetic stack boundary."""

    def test_capture_straddling_the_stack_uses_its_idb_side(self) -> None:
        # The capture starts below the stack, so its outside part must come
        # from IDB bytes while the part inside the stack is stack-backed. The
        # segment has to end at the stack base for those bytes to be IDB-backed.
        base = self.arch.stack_base
        self.idb.add_segment(base - 0x1000, base, PERM_R | PERM_W)
        self.idb.put(base - 8, b"\x7f" * 8)
        snapshot = self.snap(captures=[CaptureRequest(base - 8, 0x10, "split")])
        self.assertEqual(self.bytes_at(snapshot, base - 8, 0x10), b"\x7f" * 8 + b"\x00" * 8)
        self.assertTrue(self.valid(snapshot, base - 8, 0x10))
        self.assertTrue(self.valid(snapshot, base, 8))

    def test_capture_straddling_the_stack_without_idb_bytes_fails(self) -> None:
        with self.assertRaises(ToolError):
            self.snap(captures=[CaptureRequest(self.arch.stack_base - 8, 0x10, "split")])

    def test_many_disjoint_requests_in_one_page_are_charged_once(self) -> None:
        # 16 MiB is 4096 pages. Charge code + scratch in disjoint chunks of a
        # single page and stay under the cap; naive per-span counting would
        # reject this.
        self.idb.add_segment(0x600000, 0x600100, PERM_R | PERM_W)
        tiny = [(0x700000 + i * 0x10, 4) for i in range(60)]
        snapshot = self.snap(memory_buffers=[MemoryBuffer(a, s, b"", 3) for a, s in tiny])
        # 1 code page + 1 scratch page + 1 MiB stack.
        self.assertEqual(snapshot.total_bytes, 2 * 0x1000 + emu_mem.STACK_SIZE)

    def test_extra_range_inside_the_stack_is_not_double_counted(self) -> None:
        # A 512 KiB range inside the stack adds nothing: the stack is already
        # charged once at its full 1 MiB, alongside the code page.
        base = self.arch.stack_base
        snapshot = self.snap(extra_ranges=[(base + 0x1000, 0x80000)])
        self.assertEqual(snapshot.total_bytes, 0x1000 + emu_mem.STACK_SIZE)
        self.assertEqual(len(self.idb.reads), 1)

    def test_write_only_scratch_is_rejected_not_promoted(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.snap(memory_buffers=[MemoryBuffer(0x501000, 0x10, b"", 2)])
        self.assertIn("permissions", str(ctx.exception))

    def test_rw_entry_range_gains_executable(self) -> None:
        # The entry is explicitly requested code: it executes even when the
        # segment is R|W, gaining X without ever gaining W.
        self.idb.add_segment(0x404000, 0x405000, PERM_R | PERM_W)
        snapshot = self.snap(start_address=0x404010, stop_address=0x404020)
        perms = next(p for s, e, p in snapshot.valid_ranges if s <= 0x404010 < e)
        self.assertEqual(perms, PERM_R | PERM_W | PERM_X)

    def test_entry_in_the_synthetic_stack_is_rejected(self) -> None:
        # The entry is code; the stack is data, so it cannot host it.
        with self.assertRaises(ToolError) as ctx:
            self.snap(
                start_address=self.arch.stack_base + 0x100,
                stop_address=self.arch.stack_base + 0x110,
            )
        self.assertIn("executable entry range", str(ctx.exception))

    def test_extra_range_inside_the_stack_is_backed_by_the_stack(self) -> None:
        base = self.arch.stack_base
        snapshot = self.snap(extra_ranges=[(base + 0x1000, 0x40)])
        # The code page plus the stack; the range itself lives in the stack.
        self.assertEqual(snapshot.total_bytes, 0x1000 + emu_mem.STACK_SIZE)
        self.assertTrue(self.valid(snapshot, base + 0x1000, 0x40))

    def test_scratch_over_an_unselected_readonly_segment_is_rejected(self) -> None:
        # The segment is never selected by a requested range, but mapping RW
        # scratch over it would still grant a permission the IDB withholds.
        self.idb.add_segment(0x500000, 0x501000, PERM_R)
        with self.assertRaises(ToolError) as ctx:
            self.snap(memory_buffers=[MemoryBuffer(0x500800, 0x20, b"\x00" * 4, 3)])
        self.assertIn("overlaps IDB segment", str(ctx.exception))

    def test_scratch_byte_disjoint_from_a_segment_on_the_same_page_is_rejected(self) -> None:
        # The buffer's own bytes are disjoint from the segment, but they share
        # the page, so the page would be mapped twice with different rights.
        self.idb.add_segment(0x500000, 0x500800, PERM_R)
        with self.assertRaises(ToolError) as ctx:
            self.snap(memory_buffers=[MemoryBuffer(0x500800, 0x20, b"", 3)])
        self.assertIn("overlaps IDB segment", str(ctx.exception))

    def test_scratch_on_a_free_page_is_still_accepted(self) -> None:
        self.idb.add_segment(0x500000, 0x500800, PERM_R)
        snapshot = self.snap(memory_buffers=[MemoryBuffer(0x501000, 0x20, b"\x11" * 0x20, 3)])
        self.assertEqual(self.bytes_at(snapshot, 0x501000, 4), b"\x11" * 4)
        self.assertTrue(self.region_at(snapshot, 0x501000).synthetic)

    def test_packed_mask_three_defined_bytes_is_accepted(self) -> None:
        # Live shape: 3 payload bytes, mask 07 — one mask byte, not three. The
        # segment is exactly 3 bytes so the page intersection requests 3 bytes.
        self.idb.add_segment(0x404000, 0x404003, PERM_R | PERM_X)
        self.idb.mask_override[0x404000] = (b"\x48\x89\xe5", b"\x07")
        snapshot = self.snap(start_address=0x404000, stop_address=0x404003)
        self.assertEqual(self.bytes_at(snapshot, 0x404000, 3), b"\x48\x89\xe5")

    def test_packed_mask_nine_bytes_last_undefined_is_rejected(self) -> None:
        # 9 payload bytes -> 2 mask bytes; ff 00 leaves only bit 8 (0x404008)
        # undefined, so the reported address must be that byte.
        self.idb.add_segment(0x404000, 0x404009, PERM_R | PERM_X)
        self.idb.mask_override[0x404000] = (b"\x90" * 9, b"\xff\x00")
        with self.assertRaises(ToolError) as ctx:
            self.snap(start_address=0x404000, stop_address=0x404009)
        self.assertIn("0x404008", str(ctx.exception))

    def test_packed_mask_nine_bytes_all_defined_is_accepted(self) -> None:
        self.idb.add_segment(0x404000, 0x404009, PERM_R | PERM_X)
        self.idb.mask_override[0x404000] = (b"\x90" * 9, b"\xff\x01")
        snapshot = self.snap(start_address=0x404000, stop_address=0x404009)
        self.assertEqual(self.bytes_at(snapshot, 0x404000, 9), b"\x90" * 9)

    def test_wrong_mask_length_is_rejected(self) -> None:
        # A per-byte mask of the full length is not the packed shape.
        self.idb.add_segment(0x404000, 0x404003, PERM_R | PERM_X)
        self.idb.mask_override[0x404000] = (b"\x90" * 3, b"\x01\x01\x01")
        with self.assertRaises(ToolError) as ctx:
            self.snap(start_address=0x404000, stop_address=0x404003)
        self.assertIn("refusing to guess", str(ctx.exception))

    def test_packed_mask_zeroes_only_undefined_bss_bytes(self) -> None:
        # Interleaved definedness inside a single mask byte: 0x15 = bits 0, 2, 4.
        self.idb.add_segment(0x404000, 0x404008, PERM_R | PERM_W, sclass="BSS", bss=True)
        self.idb.mask_override[0x404000] = (b"\xaa" * 8, b"\x15")
        snapshot = self.snap(extra_ranges=[(0x404000, 8)])
        payload = self.bytes_at(snapshot, 0x404000, 8)
        self.assertEqual(payload, b"\xaa\x00\xaa\x00\xaa\x00\x00\x00")

    def test_interleaved_patched_bss_zeroes_only_masked_positions(self) -> None:
        self.idb.add_segment(0x404000, 0x405000, PERM_R | PERM_W, sclass="BSS", bss=True)
        self.idb.put(0x404000, b"\xaa" * 4)
        # Patched, defined bytes interleaved with uninitialised ones.
        self.idb.undefined.update({0x404004, 0x404006})
        snapshot = self.snap(extra_ranges=[(0x404000, 0x10)])
        payload = self.bytes_at(snapshot, 0x404000, 0x10)
        self.assertEqual(payload[:4], b"\xaa" * 4)
        self.assertEqual(payload[4], 0)
        self.assertEqual(payload[6], 0)
        self.assertTrue(self.valid(snapshot, 0x404000, 0x10))

    def test_mixed_defined_undefined_non_bss_is_rejected(self) -> None:
        self.idb.add_segment(0x404000, 0x405000, PERM_R | PERM_W)
        self.idb.put(0x404000, b"\xaa" * 4)
        self.idb.undefined.add(0x404008)
        with self.assertRaises(ToolError) as ctx:
            self.snap(extra_ranges=[(0x404000, 0x10)])
        self.assertIn("undefined", str(ctx.exception))

    def test_mask_only_bss_zeroes_every_undefined_byte(self) -> None:
        # get_bytes would return the full length here; only the mask says the
        # bytes are uninitialised, and a genuine BSS turns that into zeros.
        self.idb.add_segment(0x404000, 0x405000, PERM_R | PERM_W, sclass="BSS", bss=True)
        self.idb.undefined.update(range(0x404000, 0x404010))
        snapshot = self.snap(extra_ranges=[(0x404000, 0x10)])
        self.assertEqual(self.bytes_at(snapshot, 0x404000, 0x10), b"\x00" * 0x10)

    def test_read_failure_is_an_error_even_for_bss(self) -> None:
        self.idb.add_segment(0x404000, 0x405000, PERM_R | PERM_W, sclass="BSS", bss=True)
        self.idb.fail_reads.add(0x404000)
        with self.assertRaises(ToolError) as ctx:
            self.snap(extra_ranges=[(0x404000, 0x10)])
        self.assertIn("failed read", str(ctx.exception))

    def test_idb_segment_covering_the_stack_is_rejected(self) -> None:
        # The synthetic stack must never shadow real bytes.
        base = self.arch.stack_base
        self.idb.add_segment(base - 0x1000, base + 0x1000, PERM_R | PERM_W)
        with self.assertRaises(ToolError) as ctx:
            self.snap()
        self.assertIn("overlaps the synthetic stack", str(ctx.exception))

    def test_segment_ending_exactly_at_the_stack_is_allowed(self) -> None:
        base = self.arch.stack_base
        self.idb.add_segment(base - 0x2000, base, PERM_R | PERM_W)
        snapshot = self.snap(extra_ranges=[(base - 0x2000, 0x10)])
        self.assertEqual(snapshot.total_bytes, 2 * 0x1000 + emu_mem.STACK_SIZE)
        self.assertTrue(self.valid(snapshot, base - 0x2000, 0x10))

    def test_segment_starting_exactly_at_the_stack_end_is_allowed(self) -> None:
        base = self.arch.stack_base
        end = base + emu_mem.STACK_SIZE
        self.idb.add_segment(end, end + 0x1000, PERM_R | PERM_W)
        snapshot = self.snap(extra_ranges=[(end, 0x10)])
        # The code page and the page above the stack, plus the stack itself.
        self.assertEqual(snapshot.total_bytes, 2 * 0x1000 + emu_mem.STACK_SIZE)
        self.assertTrue(self.valid(snapshot, end, 0x10))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
