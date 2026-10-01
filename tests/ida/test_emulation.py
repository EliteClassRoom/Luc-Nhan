"""Tests for the bounded Unicorn emulation tools.

Three layers of coverage:

1. Pure helpers (page alignment, range merging, register coercion, argument
   normalisation, string-decoding, formatter) — no IDA / Unicorn runtime.
2. Argument-validation paths in the public tools — exercised against the
   IDA mock so the tool's host-query layer runs.
3. Real Unicorn integration tests run in subprocess workers via
   ``subprocess_test_worker.run_in_subprocess``. Running Unicorn inside a
   fresh interpreter avoids the ctypes / Windows access-violation quirks
   that affect repeated engine construction under pytest's longer-lived
   process, while still exercising the real SDK that ships with the
   project.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import unittest
import unittest.mock
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

importlib.invalidate_caches()

# Re-import after the mock install so module-level IDA refs land on the mocks.
if "rikugan.ida.tools.emulation" in sys.modules:
    del sys.modules["rikugan.ida.tools.emulation"]
emu = importlib.import_module("rikugan.ida.tools.emulation")

from rikugan.core.errors import ToolError
from tests.subprocess_test_worker import run_in_subprocess

# ---------------------------------------------------------------------------
# IDA architecture switches via the mock.
# ---------------------------------------------------------------------------


def _set_x86() -> None:
    sys.modules["ida_ida"].inf_get_procname.return_value = "metapc"
    sys.modules["ida_ida"].inf_is_64bit.return_value = False
    sys.modules["ida_ida"].inf_is_32bit.return_value = True
    sys.modules["ida_ida"].inf_get_app_bitness.return_value = 32


def _set_x64() -> None:
    sys.modules["ida_ida"].inf_get_procname.return_value = "metapc"
    sys.modules["ida_ida"].inf_is_64bit.return_value = True
    sys.modules["ida_ida"].inf_is_32bit.return_value = False
    sys.modules["ida_ida"].inf_get_app_bitness.return_value = 64


def _set_unsupported_arch() -> None:
    sys.modules["ida_ida"].inf_get_procname.return_value = "ARM"
    sys.modules["ida_ida"].inf_is_64bit.return_value = True
    sys.modules["ida_ida"].inf_is_32bit.return_value = False
    sys.modules["ida_ida"].inf_get_app_bitness.return_value = 64


def _make_ida_mock(
    *,
    procname: str = "metapc",
    app_bitness: int | None = 64,
) -> MagicMock:
    """Build a stand-in ``ida_ida`` for the IDA >= 7.6 contract.

    ``app_bitness`` feeds ``inf_get_app_bitness()`` (16/32/64).
    ``None`` removes the attribute so ``_ida_bitness`` surfaces its
    ``IDA bitness query failed`` ToolError, simulating a build where
    the symbol is absent.
    """
    fresh = unittest.mock.MagicMock()
    fresh.inf_get_procname.return_value = procname
    if app_bitness is not None:
        fresh.inf_get_app_bitness.return_value = app_bitness
    else:
        del fresh.inf_get_app_bitness
    return fresh


# ---------------------------------------------------------------------------
# Pure helpers.
# ---------------------------------------------------------------------------


class TestPureHelpers(unittest.TestCase):
    def test_page_align_down(self) -> None:
        self.assertEqual(emu.page_align_down(0x401000), 0x401000)
        self.assertEqual(emu.page_align_down(0x401001), 0x401000)
        self.assertEqual(emu.page_align_down(0), 0)

    def test_page_align_up(self) -> None:
        self.assertEqual(emu.page_align_up(0), 0x1000)
        self.assertEqual(emu.page_align_up(0x401001), 0x402000)
        self.assertEqual(emu.page_align_up(0x402000), 0x402000)

    def test_merge_contiguous_overlap_and_adjacency(self) -> None:
        regions = [
            (0x401000, 0x401200),
            (0x401100, 0x401300),  # overlaps
            (0x401300, 0x401500),  # adjacent to first
            (0x500000, 0x500100),
        ]
        merged = emu.merge_contiguous(regions)
        self.assertEqual(merged, [(0x401000, 0x401500), (0x500000, 0x500100)])

    def test_coerce_addr_accepts_int_hex_dec_and_integral_float(self) -> None:
        # Regression: LLMs emit addresses as floats in JSON; _coerce_addr
        # must accept the integral form while still rejecting ``True``.
        self.assertEqual(emu._coerce_addr(0x401000, ctx="t"), 0x401000)
        self.assertEqual(emu._coerce_addr("0x401000", ctx="t"), 0x401000)
        self.assertEqual(emu._coerce_addr("4198400", ctx="t"), 4198400)
        self.assertEqual(emu._coerce_addr(4198400.0, ctx="t"), 4198400)  # integral float
        self.assertEqual(emu._coerce_addr("0x00401000", ctx="t"), 0x401000)  # padded hex

    def test_coerce_addr_rejects_garbage(self) -> None:
        for bad in (True, None, "", "xyz", 4198400.5, [1]):
            with self.assertRaises(ToolError):
                emu._coerce_addr(bad, ctx="t")

    def test_normalize_memory_ranges_unwraps_text_and_accepts_float(self) -> None:
        # Regression: LLM providers serialize nested range args as {"$text": "<json>"}.
        out = emu._normalize_memory_ranges(
            [{"$text": '{"address": 268734464, "size": 45056}'}], tool_name="emulate_code"
        )
        self.assertEqual(out, [(268734464, 45056)])
        # Plain dicts and integral-float addresses also work.
        self.assertEqual(
            emu._normalize_memory_ranges([{"address": 4198400.0, "size": 16}], tool_name="emulate_code"),
            [(4198400, 16)],
        )

    def test_coerce_register_value_masks(self) -> None:
        self.assertEqual(emu.coerce_register_value(0xFFFFFFFF, 4), 0xFFFFFFFF)
        self.assertEqual(emu.coerce_register_value(0xFFFFFFFFFFFFFFFF, 4), 0xFFFFFFFF)

    def test_coerce_register_value_negative_rejected(self) -> None:
        with self.assertRaises(ToolError):
            emu.coerce_register_value(-1, 4)
        with self.assertRaises(ToolError):
            emu.coerce_register_value("-1", 4)

    def test_coerce_register_value_invalid_string(self) -> None:
        with self.assertRaises(ToolError):
            emu.coerce_register_value("abc", 4)

    def test_detect_unsupported_opcodes(self) -> None:
        self.assertIsNone(emu.detect_unsupported_opcodes(b"\x90\x90\x90\x90"))
        self.assertEqual(
            emu.detect_unsupported_opcodes(b"\x0f\x05\x90\x90"),
            "unsupported opcode at entry: 0f05",
        )
        self.assertEqual(
            emu.detect_unsupported_opcodes(b"\xcd\x80\x90\x90"),
            "unsupported opcode at entry: cd80",
        )

    def test_decode_string_candidates_ascii_nul(self) -> None:
        meta = emu._decode_string_candidates(b"hello\x00trailing")
        self.assertTrue(meta["has_nul_terminator"])
        self.assertEqual(meta["ascii"], "hello")
        self.assertEqual(meta["utf8"], "hello")
        self.assertEqual(meta["raw_length"], 14)

    def test_decode_string_candidates_utf16(self) -> None:
        # The double-NUL terminator lets the UTF-16LE candidate pick up
        # only the meaningful wide bytes — not the trailing 0xff.
        meta = emu._decode_string_candidates("hello".encode("utf-16le") + b"\x00\x00\xff\xff")
        self.assertTrue(meta["has_nul_terminator"])
        self.assertEqual(meta["utf16le"], "hello")

    def test_decode_string_candidates_malformed(self) -> None:
        meta = emu._decode_string_candidates(b"\xff\xfe\xfa\x00")
        self.assertTrue(meta["has_nul_terminator"])
        self.assertEqual(meta["ascii"], "???")
        self.assertEqual(meta["utf8"], "")
        self.assertEqual(meta["utf16le"], "")

    def test_decode_string_candidates_no_terminator(self) -> None:
        meta = emu._decode_string_candidates(b"abcdef")
        self.assertFalse(meta["has_nul_terminator"])
        self.assertEqual(meta["ascii"], "abcdef")

    def test_format_result_includes_status_and_labels(self) -> None:
        result = emu.EmulationResult(
            status="completed",
            reason="reached stop",
            entry_pc=0x401000,
            stop_pc=0x401010,
            instruction_count=8,
            architecture="x86",
            mapped_ranges=[(0x401000, 0x402000, 7)],
            final_registers={"eax": 0x42},
            captures={"output": b"hi\x00"},
            captured_strings={"output": emu._decode_string_candidates(b"hi\x00")},
        )
        text = emu.format_result(result)
        self.assertIn("Status: completed", text)
        self.assertIn("Final registers:", text)
        self.assertIn("Captured output:", text)
        self.assertIn("ascii='hi'", text)
        self.assertIn("Mapped ranges:", text)

    def test_mapping_plan_deduplicates_page_aligned_ranges(self) -> None:
        import unicorn  # only the const refs are touched

        arch = emu.ArchMode(
            label="x64",
            arch_const=unicorn.UC_ARCH_X86,
            mode_const=unicorn.UC_MODE_64,
            ptr_size=8,
            ip_reg="rip",
            sp_reg="rsp",
            flags_reg="rflags",
            stack_base=emu._stack_base_for(8),
        )
        plan = emu._build_mapping_plan(
            arch=arch,
            start_address=0x401000,
            stop_address=0x401013,
            extra_ranges=[],
            captures=[emu.CaptureRequest(0x401100, 4096, "output")],
        )
        # Start range [0x401000, 0x402000) is page-aligned. The capture at
        # 0x401100..0x402100 pages up to [0x401000, 0x403000); after the
        # page-aligned merge the single code region spans the larger
        # extent (0x2000 bytes).
        non_stack = [r for r in plan.page_aligned_regions if not r[3]]
        self.assertEqual(len(non_stack), 1)
        start, size, _perms, _is_stack = non_stack[0]
        self.assertEqual(start, 0x401000)
        self.assertEqual(size, 0x2000)


# ---------------------------------------------------------------------------
# Bitness detection via ida_ida.inf_get_app_bitness() (the IDA >=7.6 API).
# IDA 9.x has no inf_is_32bit() and removed get_inf_structure();
# inf_get_app_bitness() is the single reliable source returning 16/32/64.
# ---------------------------------------------------------------------------


class TestBitnessDetection(unittest.TestCase):
    """``_ida_bitness`` reads ``ida_ida.inf_get_app_bitness()`` and returns
    the exact bitness. Configures ``emu.ida_ida`` directly so the test stays
    robust when another module re-installs the mocks."""

    def tearDown(self) -> None:
        # Restore the bound mock to its default (64-bit) state.
        emu.ida_ida.inf_get_app_bitness.return_value = 64

    def test_32bit_db(self) -> None:
        # Regression for the user's reported error on a 32-bit IDB in IDA 9.4.
        emu.ida_ida.inf_get_app_bitness.return_value = 32
        self.assertEqual(emu._ida_bitness(), 32)

    def test_64bit_db(self) -> None:
        emu.ida_ida.inf_get_app_bitness.return_value = 64
        self.assertEqual(emu._ida_bitness(), 64)

    def test_16bit_db(self) -> None:
        emu.ida_ida.inf_get_app_bitness.return_value = 16
        self.assertEqual(emu._ida_bitness(), 16)


# ---------------------------------------------------------------------------
# Argument-validation paths via the public tools.
# ---------------------------------------------------------------------------


class TestArchitectureValidation(unittest.TestCase):
    def setUp(self) -> None:
        _set_x86()

    def test_unsupported_arch_raises(self) -> None:
        _set_unsupported_arch()
        with self.assertRaises(ToolError) as ctx:
            emu.emulate_code(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"eax": 0},
            )
        self.assertIn("Unsupported architecture", str(ctx.exception))

    def test_registers_required(self) -> None:
        with self.assertRaises(ToolError):
            emu.emulate_code(
                start_address="0x401000",
                stop_address="0x401010",
                registers={},
            )

    def test_unknown_register_rejected(self) -> None:
        with self.assertRaises(ToolError):
            emu.emulate_code(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"eax": 0, "nope": 1},
            )

    def test_ip_override_rejected(self) -> None:
        with self.assertRaises(ToolError):
            emu.emulate_code(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"eax": 0, "eip": 0x500000},
            )

    def test_start_after_stop_rejected(self) -> None:
        with self.assertRaises(ToolError):
            emu.emulate_code(
                start_address="0x401010",
                stop_address="0x401000",
                registers={"eax": 0},
            )

    def test_instruction_limit_must_be_positive(self) -> None:
        with self.assertRaises(ToolError):
            emu.emulate_code(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"eax": 0},
                instruction_limit=0,
            )

    def test_max_output_size_bounded(self) -> None:
        with self.assertRaises(ToolError):
            emu.resolve_emulated_string(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"eax": 0},
                output_address="0x401020",
                max_output_size=8192,
            )


class _ArchBoundTestCase(unittest.TestCase):
    """Base that pins ``emu.ida_ida`` to a known arch for the duration of a test.

    ``emu.ida_ida`` and ``sys.modules["ida_ida"]`` can be different objects
    once another module re-installs the mocks, so the arch is swapped on the
    module's own reference and restored afterwards.
    """

    def _use_arch(self, bitness: int) -> None:
        self._saved_ida = getattr(self, "_saved_ida", emu.ida_ida)
        self.addCleanup(self._restore_arch)
        emu.ida_ida = _make_ida_mock(app_bitness=bitness)

    def _restore_arch(self) -> None:
        emu.ida_ida = self._saved_ida


class TestArgumentCoercion(_ArchBoundTestCase):
    """JSON int / integral-float / hex-string arguments are all accepted.

    The runner is stubbed out, so these cases exercise argument parsing only —
    every assertion is about the coerced values that reach ``_run``.
    """

    def setUp(self) -> None:
        self._use_arch(32)
        self.captured: dict = {}
        self._patcher = unittest.mock.patch.object(emu, "_run", side_effect=self._fake_run)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _fake_run(self, **kwargs):
        self.captured = kwargs
        return emu.EmulationResult(status="completed", architecture=kwargs["arch"].label)

    def test_addresses_accept_int_and_integral_float(self) -> None:
        emu.emulate_code(start_address=0x401000, stop_address=4198400.0, registers={"eax": 0})
        self.assertEqual(self.captured["start_address"], 0x401000)
        self.assertEqual(self.captured["stop_address"], 4198400)

    def test_output_address_accepts_float(self) -> None:
        emu.resolve_emulated_string(
            start_address="0x401000",
            stop_address="0x401010",
            registers={"eax": 0},
            output_address=4198400.0,
        )
        self.assertEqual(self.captured["captures"][0].address, 4198400)

    def test_memory_range_size_accepts_float_and_hex_string(self) -> None:
        emu.emulate_code(
            start_address="0x401000",
            stop_address="0x401010",
            registers={"eax": 0},
            memory_ranges=[
                {"address": "0x402000", "size": 45056.0},
                {"address": "0x403000", "size": "0x1000"},
            ],
        )
        self.assertEqual(self.captured["extra_ranges"], [(0x402000, 45056), (0x403000, 0x1000)])

    def test_capture_size_accepts_hex_string(self) -> None:
        emu.emulate_code(
            start_address="0x401000",
            stop_address="0x401010",
            registers={"eax": 0},
            capture_ranges=[{"address": "0x401100", "size": "0x20"}],
        )
        self.assertEqual(self.captured["captures"][0].size, 0x20)

    def test_instruction_limit_accepts_numeric_string(self) -> None:
        emu.emulate_code(
            start_address="0x401000",
            stop_address="0x401010",
            registers={"eax": 0},
            instruction_limit="64",
        )
        self.assertEqual(self.captured["max_instructions"], 64)

    def test_instruction_limit_clamped_to_hard_cap(self) -> None:
        emu.emulate_code(
            start_address="0x401000",
            stop_address="0x401010",
            registers={"eax": 0},
            instruction_limit=10_000_000,
        )
        self.assertEqual(self.captured["max_instructions"], emu._MAX_INSTRUCTION_LIMIT)

    def test_zero_and_negative_sizes_rejected(self) -> None:
        for bad in (0, -1, "0x0", 1.5, None, True, "abc"):
            with self.subTest(bad=bad), self.assertRaises(ToolError):
                emu.emulate_code(
                    start_address="0x401000",
                    stop_address="0x401010",
                    registers={"eax": 0},
                    memory_ranges=[{"address": "0x402000", "size": bad}],
                )

    def test_max_output_size_beyond_cap_rejected_as_tool_error(self) -> None:
        with self.assertRaises(ToolError):
            emu.resolve_emulated_string(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"eax": 0},
                output_address="0x401020",
                max_output_size="8192",
            )


class TestRegisterAliasRules(_ArchBoundTestCase):
    """Alias resolution, mode availability, and IP-register rejection."""

    def setUp(self) -> None:
        self._use_arch(32)
        self.captured: dict = {}
        self._patcher = unittest.mock.patch.object(emu, "_run", side_effect=self._fake_run)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _fake_run(self, **kwargs):
        self.captured = kwargs
        return emu.EmulationResult(status="completed", architecture=kwargs["arch"].label)

    def test_x86_eip_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            emu.emulate_code(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"eip": 0x500000},
            )
        self.assertIn("eip/rip", str(ctx.exception))

    def test_x64_rip_rejected(self) -> None:
        self._use_arch(64)
        with self.assertRaises(ToolError) as ctx:
            emu.emulate_code(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"rip": 0x500000},
            )
        self.assertIn("eip/rip", str(ctx.exception))

    def test_x64_eip_rejected_too(self) -> None:
        # The schema promises rejection on every arch; the old code silently
        # ignored eip on x64.
        self._use_arch(64)
        with self.assertRaises(ToolError) as ctx:
            emu.emulate_code(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"eip": 0x500000},
            )
        self.assertIn("eip/rip", str(ctx.exception))

    def test_x86_rejects_64bit_only_registers(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            emu.emulate_code(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"r8": 1},
            )
        self.assertIn("32-bit mode", str(ctx.exception))

    def test_x64_accepts_64bit_only_registers(self) -> None:
        self._use_arch(64)
        emu.emulate_code(
            start_address="0x401000",
            stop_address="0x401010",
            registers={"r8": 1},
        )
        self.assertEqual(self.captured["registers"], {"r8": 1})

    def test_conflicting_alias_values_rejected_on_x64(self) -> None:
        self._use_arch(64)
        with self.assertRaises(ToolError) as ctx:
            emu.emulate_code(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"eax": 1, "rax": 2},
            )
        self.assertIn("conflicting", str(ctx.exception))

    def test_matching_alias_values_accepted_on_x64(self) -> None:
        self._use_arch(64)
        emu.emulate_code(
            start_address="0x401000",
            stop_address="0x401010",
            registers={"eax": 7, "rax": 7},
        )
        self.assertEqual(self.captured["registers"], {"rax": 7})

    def test_x86_alias_maps_to_32bit_canonical(self) -> None:
        emu.emulate_code(
            start_address="0x401000",
            stop_address="0x401010",
            registers={"rax": "0x40"},
        )
        self.assertEqual(self.captured["registers"], {"eax": 0x40})

    def test_x64_alias_maps_to_64bit_canonical(self) -> None:
        self._use_arch(64)
        emu.emulate_code(
            start_address="0x401000",
            stop_address="0x401010",
            registers={"ecx": 5, "eflags": 0x202},
        )
        self.assertEqual(self.captured["registers"], {"rcx": 5, "rflags": 0x202})


class TestIdaBitnessArchResolution(unittest.TestCase):
    """Arch resolution via ``ida_ida.inf_get_app_bitness()`` (IDA >= 7.6)."""

    def setUp(self) -> None:
        self._saved = sys.modules["ida_ida"]

    def tearDown(self) -> None:
        sys.modules["ida_ida"] = self._saved

    def _fake_unicorn(self):
        class _FakeUnicorn:
            UC_ARCH_X86 = 0
            UC_MODE_32 = 1
            UC_MODE_64 = 2

        return _FakeUnicorn()

    def test_resolve_arch_x86(self) -> None:
        mock_ida = _make_ida_mock(app_bitness=32)
        sys.modules["ida_ida"] = mock_ida
        emu.ida_ida = mock_ida
        try:
            arch = emu._resolve_arch(self._fake_unicorn())
        finally:
            emu.ida_ida = self._saved
        self.assertEqual(arch.label, "x86")
        self.assertEqual(arch.ptr_size, 4)
        self.assertEqual(arch.ip_reg, "eip")

    def test_resolve_arch_x64(self) -> None:
        mock_ida = _make_ida_mock(app_bitness=64)
        sys.modules["ida_ida"] = mock_ida
        emu.ida_ida = mock_ida
        try:
            arch = emu._resolve_arch(self._fake_unicorn())
        finally:
            emu.ida_ida = self._saved
        self.assertEqual(arch.label, "x64")
        self.assertEqual(arch.ptr_size, 8)
        self.assertEqual(arch.ip_reg, "rip")

    def test_ida_bitness_raises_tool_error_when_getter_missing(self) -> None:
        mock_ida = _make_ida_mock(app_bitness=None)
        sys.modules["ida_ida"] = mock_ida
        emu.ida_ida = mock_ida
        try:
            with self.assertRaises(ToolError) as ctx:
                emu._ida_bitness()
        finally:
            emu.ida_ida = self._saved
        self.assertIn("IDA bitness query failed", str(ctx.exception))

    def test_resolve_arch_propagates_bitness_query_tool_error(self) -> None:
        mock_ida = _make_ida_mock(app_bitness=None)
        sys.modules["ida_ida"] = mock_ida
        emu.ida_ida = mock_ida
        try:
            with self.assertRaises(ToolError) as ctx:
                emu._resolve_arch(self._fake_unicorn())
        finally:
            emu.ida_ida = self._saved
        self.assertIn("IDA bitness query failed", str(ctx.exception))


# ---------------------------------------------------------------------------
# Real Unicorn integration — runs each scenario in a fresh subprocess via
# ``tests.subprocess_test_worker`` so per-engine state cannot leak between
# tests under Windows ctypes quirks.
# ---------------------------------------------------------------------------


def _nop_page(code: bytes, *, fill: int = 0x90, size: int = 0x1000) -> bytes:
    """One simulated IDB page: *code* at offset 0, rest filled with *fill*.

    *code* must begin at the page base address, which every caller below
    arranges by making the page base the code's entry point.
    """

    raw = bytearray([fill]) * size
    raw[: len(code)] = code
    return bytes(raw)


# Each worker serializes the simulated IDB + tool arguments and returns a
# serialised ``EmulationResult``-shaped dict (or raises).


def _run_in_subprocess(
    tool: str,
    payload: dict,
    *,
    setup_pages: list[tuple[int, int, bytes]],
    perm: int | None = None,
):
    """Run *tool* in a fresh interpreter against a simulated IDB.

    ``perm`` overrides the simulated segment permission mask (the worker
    defaults to RWX so decode tests can write); omit it to keep the default.
    """

    plan = {
        "tool": tool,
        "payload": payload,
        "setup_pages": [[s, e, list(d)] for s, e, d in setup_pages],
    }
    if perm is not None:
        plan["perm"] = perm
    return run_in_subprocess(
        "tests.test_emulation_subprocess",
        json.dumps(plan),
        timeout=30.0,
    )


@unittest.skipUnless(
    importlib.util.find_spec("unicorn") is not None,
    "unicorn not installed in this environment",
)
class TestRealUnicornIntegration(unittest.TestCase):
    """End-to-end checks against the installed Unicorn SDK."""

    def test_x86_xor_decoder_completes_via_exclusive_stop(self) -> None:
        decoder = (
            b"\xb9\x05\x00\x00\x00"  # mov ecx, 5
            b"\xbf\x60\x89\x40\x00"  # mov edi, 0x408960
            b"\x80\x37\x47"  # xor byte [edi], bl
            b"\x47"  # inc edi
            b"\xe2\xfa"  # loop back to the xor (rel8 = -6 from the next IP)
        )
        # Entry point sits deep inside a page-aligned segment, and the
        # encrypted blob lives in a capture range outside the code page —
        # the offset math must be segment-relative, not page-relative.
        start = 0x408345
        seg_start = 0x401000
        encrypted = bytes(b ^ 0x47 for b in b"hello")
        raw = bytearray(b"\x90" * 0x9000)
        raw[start - seg_start : start - seg_start + len(decoder)] = decoder
        raw[0x408960 - seg_start : 0x408960 - seg_start + len(encrypted)] = encrypted
        # Buffer is NUL-terminated after the blob so the ASCII candidate is
        # exactly the decoded string, not the decoded string plus filler.
        raw[0x408965 - seg_start : 0x408968 - seg_start] = b"\x00\x00\x00"

        out = _run_in_subprocess(
            "emulate_code",
            {
                "start_address": hex(start),
                "stop_address": hex(start + len(decoder)),
                "registers": {"eax": 0, "ebx": 0x47},
                "capture_ranges": [{"address": "0x408960", "size": 8}],
            },
            setup_pages=[(seg_start, seg_start + len(raw), bytes(raw))],
        )
        self.assertEqual(out["status"], "completed")
        self.assertIn("Instructions executed:", out["text"])
        self.assertIn("ascii='hello'", out["text"])

    def _run_mov_eax_ecx(self, registers: dict, *, perm: int | None = None) -> dict:
        """Run ``mov eax, ecx`` (89 c8) and return the worker result."""

        start = 0x401000
        raw = _nop_page(b"\x89\xc8")  # 89 c8 = mov eax, ecx
        return _run_in_subprocess(
            "emulate_code",
            {
                "start_address": hex(start),
                "stop_address": hex(start + 2),
                "registers": registers,
            },
            setup_pages=[(start, start + len(raw), raw)],
            perm=perm,
        )

    def test_x86_user_registers_not_clobbered(self) -> None:
        # Regression: the init loop used to write every unified name as 0,
        # so the trailing ``rax=0`` alias zeroed the user-supplied ``ecx``.
        out = self._run_mov_eax_ecx({"ecx": 0x1234})
        self.assertEqual(out["status"], "completed")
        self.assertIn("eax = 0x1234", out["text"])

    def test_x64_32bit_register_name_zero_extends(self) -> None:
        # ``r8`` marks the worker heuristic as 64-bit; a value supplied under
        # the 32-bit name must zero-extend into ``rax`` instead of being wiped
        # by the later ``rax=0`` alias write the old init loop performed.
        start = 0x401000
        # 66 90 = two-byte x86-64 NOP (a lone 0x43 is REX.B, which takes the
        # following bytes as ModRM and reads address 0x1).
        raw = _nop_page(b"\x66\x90")
        out = _run_in_subprocess(
            "emulate_code",
            {
                "start_address": hex(start),
                "stop_address": hex(start + 2),
                "registers": {"eax": 0x11223344, "ecx": 1, "r8": 1},
            },
            setup_pages=[(start, start + len(raw), raw)],
        )
        self.assertEqual(out["status"], "completed")
        self.assertIn("rax = 0x11223344", out["text"])

    def test_user_eflags_preserved(self) -> None:
        # ``mov eax, ecx`` does not touch flags, so a supplied value must
        # come back verbatim (the old code unconditionally wrote flags=0).
        out = self._run_mov_eax_ecx({"ecx": 1, "eflags": 0x202})
        self.assertEqual(out["status"], "completed")
        self.assertIn("eflags = 0x202", out["text"])

    def test_code_range_in_rw_segment_executes(self) -> None:
        # Packed binaries keep code in R|W segments; the requested code
        # range must be mapped executable regardless of segment perms.
        out = self._run_mov_eax_ecx({"ecx": 0x77}, perm=3)
        self.assertEqual(out["status"], "completed")
        self.assertIn("eax = 0x77", out["text"])

    def test_payload_spans_adjacent_segments(self) -> None:
        # Code in segment A reads a byte from segment B: the payload walk
        # must fill each segment from its own start, not assume one.
        # 8a 05 05 20 40 00 = mov al, byte ptr [0x402005]  (little-endian disp32)
        # 93                = xchg eax, ebx
        code = b"\x8a\x05\x05\x20\x40\x00\x93"
        seg_a = bytearray(b"\x90" * 0x1000)
        seg_a[0x401FF0 - 0x401000 : 0x401FF0 - 0x401000 + len(code)] = code
        seg_b = bytearray(b"\x00" * 0x1000)
        seg_b[5] = 0x5A
        out = _run_in_subprocess(
            "emulate_code",
            {
                "start_address": hex(0x401FF0),
                "stop_address": hex(0x401FF0 + len(code)),
                "registers": {"ebx": 0xBB, "ecx": 0},
                "memory_ranges": [{"address": "0x402000", "size": 0x1000}],
            },
            setup_pages=[
                (0x401000, 0x402000, bytes(seg_a)),
                (0x402000, 0x403000, bytes(seg_b)),
            ],
        )
        self.assertEqual(out["status"], "completed")
        # ``xchg eax, ebx`` leaves the loaded byte in ebx, eax holds the seed.
        self.assertIn("ebx = 0x5a", out["text"])

    def test_completed_stop_pc_reports_stop_address(self) -> None:
        out = self._run_mov_eax_ecx({"ecx": 1})
        self.assertEqual(out["status"], "completed")
        self.assertIn("Stop PC:  0x401002", out["text"])

    def test_branch_loop_hits_instruction_limit(self) -> None:
        start = 0x401000
        raw = b"\xeb\xfe" + b"\x90" * (0x1000 - 2)  # eb fe = jmp $-2 (spin forever)
        out = _run_in_subprocess(
            "emulate_code",
            {
                "start_address": hex(start),
                "stop_address": hex(start + 0x40),
                "registers": {"eax": 0},
                "instruction_limit": 64,
            },
            setup_pages=[(start, start + len(raw), raw)],
        )
        self.assertEqual(out["status"], "instruction_limit")

    def test_resolve_emulated_string_returns_decoded_ascii(self) -> None:
        start = 0x401000
        # c6 07 41           = mov byte ptr [edi+0], 0x41  ('A')
        # c6 47 01 42        = mov byte ptr [edi+1], 0x42  ('B')
        # c6 47 02 43        = mov byte ptr [edi+2], 0x43  ('C')
        # c6 47 03 44        = mov byte ptr [edi+3], 0x44  ('D')
        # c6 47 04 00        = mov byte ptr [edi+4], 0x00  (NUL terminator)
        stub = b"\xc6\x07\x41\xc6\x47\x01\x42\xc6\x47\x02\x43\xc6\x47\x03\x44\xc6\x47\x04\x00"
        raw = stub + b"\x90" * (0x2000 - len(stub))
        out = _run_in_subprocess(
            "resolve_emulated_string",
            {
                "start_address": hex(start),
                "stop_address": hex(start + len(stub)),
                "registers": {"eax": 0, "edi": 0x401100},
                "output_address": "0x401100",
            },
            setup_pages=[(start, start + len(raw), raw)],
        )
        self.assertEqual(out["status"], "completed")
        self.assertIn("ascii='ABCD'", out["text"])
        self.assertIn("terminated=True", out["text"])

    def test_unsupported_arch_yields_clear_error(self) -> None:
        """Ensure the unsupported-architecture path is reachable from a real worker."""
        # Bound to the module reference, not the shared ``sys.modules`` mock,
        # so the ARM setting cannot leak into other test modules.
        saved = emu.ida_ida
        emu.ida_ida = _make_ida_mock(procname="ARM", app_bitness=64)
        self.addCleanup(setattr, emu, "ida_ida", saved)

        # Run the validation path rather than paying subprocess cost: it
        # runs before Unicorn is loaded.
        with self.assertRaises(ToolError) as ctx:
            emu.emulate_code(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"eax": 0},
            )
        self.assertIn("Unsupported architecture", str(ctx.exception))


# ---------------------------------------------------------------------------
# Runner internals: the payload walk's range arithmetic and the mapped-range
# projection. Both are invisible to the integration tests, which only observe
# final register values and status strings.
# ---------------------------------------------------------------------------


class _RecordingEngine:
    """Fake Unicorn engine that records every ``mem_write`` as ``(addr, len)``."""

    def __init__(self) -> None:
        self.writes: list[tuple[int, int]] = []

    def mem_write(self, address: int, data: bytes) -> None:
        self.writes.append((int(address), len(data)))

    @property
    def addresses(self) -> list[int]:
        return [a for a, _n in self.writes]


class _FakeSegment:
    def __init__(self, start_ea: int, end_ea: int) -> None:
        self.start_ea = start_ea
        self.end_ea = end_ea
        self.perm = 5  # R|X — the walk reads ``perm`` nowhere; kept realistic


class TestWriteSegmentPayloads(unittest.TestCase):
    """The payload walk must stay inside the requested ranges.

    ``extra_ranges`` arrives as ``(address, size)`` pairs — the shape
    ``_normalize_memory_ranges`` produces and both tool entry points forward
    verbatim. ``_write_segment_payloads`` merges them into ``(start, end)``
    regions, so a size must be converted to an end address *before* the merge.
    Treating a size as an end address instead makes the walk run to
    ``address + (address + size)``, filling hundreds of KB to megabytes of
    IDB bytes nobody asked for.
    """

    def _install_fake_idb(self, seg_start: int, seg_end: int) -> None:
        # Bind to the module's own references, not ``sys.modules``: a sibling
        # test module may have replaced the shared ``ida_segment`` /
        # ``ida_bytes`` mocks wholesale, and these stubs must win regardless.
        segment = _FakeSegment(seg_start, seg_end)
        ida_segment = emu.ida_segment
        ida_bytes = emu.ida_bytes
        previous = (ida_segment.getseg.side_effect, ida_bytes.get_bytes.side_effect)
        ida_segment.getseg.side_effect = lambda ea: segment if seg_start <= ea < seg_end else None
        ida_bytes.get_bytes.side_effect = lambda ea, size: b"\x90" * size
        self.addCleanup(self._restore_fake_idb, *previous)

    def _restore_fake_idb(self, getseg_side_effect, get_bytes_side_effect) -> None:
        emu.ida_segment.getseg.side_effect = getseg_side_effect
        emu.ida_bytes.get_bytes.side_effect = get_bytes_side_effect

    def test_write_segment_payloads_respects_range_end(self) -> None:
        # One segment spanning the code range and the declared extra range.
        self._install_fake_idb(0x401000, 0x410000)
        engine = _RecordingEngine()

        emu._write_segment_payloads(engine, 0x401400, 0x401410, [(0x401400, 0x400)])

        self.assertTrue(engine.writes, "expected the walk to write at least one payload")
        for address in engine.addresses:
            self.assertLess(
                address,
                0x401800,
                f"payload walk wrote at 0x{address:x}, past the declared end 0x401800",
            )

    def test_extra_range_is_filled_through_its_declared_end(self) -> None:
        # The complement of the guard above: the declared range must actually
        # be *filled*, not truncated away, and its fill must end where the
        # size says. The walk coalesces a single-segment region into one
        # write, so assert on the covered byte span rather than per-address.
        self._install_fake_idb(0x401000, 0x410000)
        engine = _RecordingEngine()

        emu._write_segment_payloads(engine, 0x401000, 0x401010, [(0x401400, 0x400)])

        # Code range 0x401000..0x401010 plus the 0x400-byte extra range.
        self.assertEqual(engine.writes, [(0x401000, 0x10), (0x401400, 0x400)])


class TestIdbMappedRanges(unittest.TestCase):
    """``_idb_mapped_ranges`` reports IDB mappings only, never the stack."""

    def test_idb_mapped_ranges_excludes_stack(self) -> None:
        plan = emu.MappingPlan(
            page_aligned_regions=[
                (0x401000, 0x1000, 5, False),
                (0x402000, 0x1000, 3, False),
                (0x1000000, emu._STACK_SIZE, 3, True),  # synthetic stack
            ],
            total_bytes=0x3000,
            stack_base=0x1000000,
            stack_top=0x1000000 + emu._STACK_SIZE - emu._STACK_TOP_RESERVED,
        )

        self.assertEqual(
            emu._idb_mapped_ranges(plan),
            [(0x401000, 0x402000, 5), (0x402000, 0x403000, 3)],
        )
