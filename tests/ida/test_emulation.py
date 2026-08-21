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

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

importlib.invalidate_caches()

# Re-import after the mock install so module-level IDA refs land on the mocks.
if "rikugan.ida.tools.emulation" in sys.modules:
    del sys.modules["rikugan.ida.tools.emulation"]
emu = importlib.import_module("rikugan.ida.tools.emulation")

from rikugan.core.errors import ToolError  # noqa: E402
from tests.subprocess_test_worker import run_in_subprocess  # noqa: E402

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

    def test_is_hex_or_int(self) -> None:
        self.assertTrue(emu.is_hex_or_int(0x401000))
        self.assertTrue(emu.is_hex_or_int("0x401000"))
        self.assertTrue(emu.is_hex_or_int("401000"))
        self.assertTrue(emu.is_hex_or_int(0))
        self.assertFalse(emu.is_hex_or_int(True))
        self.assertFalse(emu.is_hex_or_int(None))
        self.assertFalse(emu.is_hex_or_int(""))
        self.assertFalse(emu.is_hex_or_int("xyz"))

    def test_coerce_addr_accepts_int_hex_dec_and_integral_float(self) -> None:
        # Regression: LLMs emit addresses as floats in JSON; is_hex_or_int
        # rejects them but _coerce_addr must accept the integral form.
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


# Each worker serializes the simulated IDB + tool arguments and returns a
# serialised ``EmulationResult``-shaped dict (or raises).


def _run_in_subprocess(tool: str, payload: dict, *, setup_pages: list[tuple[int, int, bytes]]):

    plan = {
        "tool": tool,
        "payload": payload,
        "setup_pages": [[s, e, list(d)] for s, e, d in setup_pages],
    }
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
            b"\xbf\x20\x10\x40\x00"  # mov edi, 0x401020
            b"\x80\x37\x47"  # xor byte [edi], bl
            b"\x47"  # inc edi
            b"\xe2\xfb"  # loop -5
        )
        start = 0x401000
        encrypted = bytes(b ^ 0x47 for b in b"hello")
        raw = bytearray(b"\x90" * 0x2000)
        raw[: len(decoder)] = decoder
        raw[0x401020 - start : 0x401020 - start + len(encrypted)] = encrypted

        out = _run_in_subprocess(
            "emulate_code",
            {
                "start_address": hex(start),
                "stop_address": hex(start + len(decoder)),
                "registers": {"eax": 0, "ebx": 0x47},
            },
            setup_pages=[(start, start + len(raw), bytes(raw))],
        )
        self.assertEqual(out["status"], "completed")
        self.assertIn("Instructions executed:", out["text"])

    def test_branch_loop_hits_instruction_limit(self) -> None:
        start = 0x401000
        raw = b"\xeb\xfe" + b"\x90" * (0x1000 - 2)
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
        _set_unsupported_arch()  # ARM architecture in the mock

        # Run worker manually rather than paying subprocess cost: the
        # validation path runs before Unicorn is loaded.
        with self.assertRaises(ToolError) as ctx:
            emu.emulate_code(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"eax": 0},
            )
        self.assertIn("Unsupported architecture", str(ctx.exception))
