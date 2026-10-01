"""Tests for the bounded Unicorn emulation tools.

Three layers of coverage:

1. Pure helpers (address coercion, structured-argument unwrapping, opcode
   scanning) — no IDA / Unicorn runtime.
2. Architecture resolution and bitness detection through the IDA mock.
3. One real end-to-end Unicorn scenario in a subprocess worker, so the
   decoder-shaped use case keeps a real end-to-end guard.

CPU outcomes, calling conventions, deadline handling and argument
validation live in ``tests/ida/test_emulation_execution.py``; the memory
snapshot and the renderer have their own modules' test files.
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

HAVE_UNICORN = importlib.util.find_spec("unicorn") is not None


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


def _make_ida_mock(*, procname: str = "metapc", app_bitness: int | None = 64) -> MagicMock:
    """Build a stand-in ``ida_ida`` for the IDA >= 7.6 contract.

    ``app_bitness=None`` removes the attribute so ``_ida_bitness`` surfaces
    its ``IDA bitness query failed`` ToolError.
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
    def test_coerce_addr_accepts_int_hex_dec_and_integral_float(self) -> None:
        # Regression: LLMs emit addresses as floats and as "0x"-strings.
        self.assertEqual(emu._coerce_addr(0x401000, ctx="t"), 0x401000)
        self.assertEqual(emu._coerce_addr("0x401000", ctx="t"), 0x401000)
        self.assertEqual(emu._coerce_addr("4198400", ctx="t"), 4198400)
        self.assertEqual(emu._coerce_addr(4198400.0, ctx="t"), 4198400)
        self.assertEqual(emu._coerce_addr("0x00401000", ctx="t"), 0x401000)

    def test_coerce_addr_rejects_garbage_and_negatives(self) -> None:
        for bad in (True, None, "", "xyz", 4198400.5, [1], -0x10):
            with self.assertRaises(ToolError):
                emu._coerce_addr(bad, ctx="t")

    def test_structured_arguments_unwrap_text_wrapped_json(self) -> None:
        # Some providers serialize nested structured args as {"$text": "<json>"}.
        out = emu._normalize_memory_ranges(
            [{"$text": '{"address": 268734464, "size": 45056}'}], tool_name="emulate_code"
        )
        self.assertEqual(out, [(268734464, 45056)])
        out = emu._normalize_buffers(
            [{"$text": '{"address": 4198400, "size": 4, "data_hex": "78563412"}'}],
            tool_name="emulate_code",
        )
        self.assertEqual(out[0].data, bytes.fromhex("78563412"))
        out = emu._normalize_capture_specs(
            [{"$text": '{"address": 4198400, "size": 8, "label": "out"}'}],
            tool_name="emulate_code",
            default_label="output",
        )
        self.assertEqual(out[0]["label"], "out")

    def test_range_normalization_rejects_bad_shapes(self) -> None:
        for bad in (
            [{"size": 4}],
            [{"address": 0x401000}],
            [{"address": 0x401000, "size": 0}],
            [{"address": 0x401000, "size": True}],
            ["nope"],
        ):
            with self.assertRaises(ToolError):
                emu._normalize_memory_ranges(bad, tool_name="emulate_code")

    def test_opcode_scan_flags_far_control_anywhere(self) -> None:
        self.assertIsNone(emu._scan_forbidden(bytes.fromhex("83c00190")))
        self.assertIsNotNone(emu._scan_forbidden(bytes.fromhex("0f0590")))
        self.assertIsNotNone(emu._scan_forbidden(bytes.fromhex("0f3490")))
        self.assertIsNotNone(emu._scan_forbidden(bytes.fromhex("cd80")))
        self.assertIsNotNone(emu._scan_forbidden(bytes.fromhex("66cd2e")))
        # 0x66 is a real operand-size prefix; 0x42 is a REX byte and never
        # precedes `syscall` in a real encoding, so the prefix chain here is
        # 0x66/0x67/0xf0/0xf2/0xf3 only.
        self.assertIsNotNone(emu._scan_forbidden(bytes.fromhex("660f05")))
        self.assertIsNotNone(emu._scan_forbidden(bytes.fromhex("f30f34")))
        self.assertIsNotNone(emu._scan_forbidden(bytes.fromhex("cd21")))
        self.assertIsNotNone(emu._scan_forbidden(bytes.fromhex("cc90")))  # int3
        self.assertIsNotNone(emu._scan_forbidden(bytes.fromhex("f190")))  # into
        self.assertIsNotNone(emu._scan_forbidden(bytes.fromhex("0f0b90")))  # ud2

    def test_range_membership_helpers(self) -> None:
        # ranges are (address, size); adjacent pages collapse into one span
        merged = emu._merge_ranges([(0x1000, 0x1000), (0x2000, 0x1000), (0x9000, 0x100)])
        self.assertEqual(merged, [(0x1000, 0x3000), (0x9000, 0x9100)])
        self.assertTrue(emu._within(merged, 0x1000, 4))
        self.assertFalse(emu._within(merged, 0x2FFE, 4))


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
        emu.ida_ida.inf_get_app_bitness.return_value = 64

    def test_32bit_db(self) -> None:
        # Regression for the user's reported error on a 32-bit IDB in IDA 9.4.
        emu.ida_ida.inf_get_app_bitness.return_value = 32
        self.assertEqual(emu._ida_bitness(), 32)

    def test_64bit_db(self) -> None:
        emu.ida_ida.inf_get_app_bitness.return_value = 64
        self.assertEqual(emu._ida_bitness(), 64)

    def test_16bit_db_is_reported_then_rejected_as_unsupported(self) -> None:
        emu.ida_ida.inf_get_app_bitness.return_value = 16
        self.assertEqual(emu._ida_bitness(), 16)
        emu.ida_ida.inf_get_procname.return_value = "metapc"
        try:
            with self.assertRaises(ToolError) as ctx:
                emu._resolve_arch(unittest.mock.MagicMock(UC_ARCH_X86=4, UC_MODE_32=1, UC_MODE_64=2))
        finally:
            _set_x64()
        self.assertIn("16-bit", str(ctx.exception))


class TestIdaBitnessArchResolution(unittest.TestCase):
    """Arch resolution via ``ida_ida.inf_get_app_bitness()`` (IDA >= 7.6)."""

    def setUp(self) -> None:
        self._saved = sys.modules["ida_ida"]

    def tearDown(self) -> None:
        sys.modules["ida_ida"] = self._saved
        _set_x64()

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
        self.assertEqual(arch.sp_reg, "esp")

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
        self.assertEqual(arch.sp_reg, "rsp")

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
# Real Unicorn integration — the decoder-shaped use case runs in a fresh
# subprocess so per-engine state cannot leak between tests.
# ---------------------------------------------------------------------------


@unittest.skipUnless(HAVE_UNICORN, "unicorn not installed in this environment")
class TestRealUnicornIntegration(unittest.TestCase):
    def test_x86_xor_decoder_completes_via_exclusive_stop(self) -> None:
        decoder = (
            b"\xb9\x05\x00\x00\x00"  # mov ecx, 5
            b"\xbf\x00\x30\x40\x00"  # mov edi, 0x403000
            b"\x80\x37\x47"  # xor byte [edi], bl
            b"\x47"  # inc edi
            b"\xe2\xfa"  # loop -6 (back to the xor)
        )
        start = 0x401000
        encrypted = bytes(b ^ 0x47 for b in b"hello")
        code = decoder + b"\x90" * (0x1000 - len(decoder))
        data = encrypted
        plan = {
            "tool": "emulate_code",
            "payload": {
                "start_address": hex(start),
                "stop_address": hex(start + len(decoder)),
                "registers": {"eax": 0, "ebx": 0x47},
                "memory_ranges": [{"address": "0x403000", "size": 16}],
                "capture_ranges": [{"address": "0x403000", "size": 16, "label": "plain"}],
            },
            "bits": 32,
            "segments": [
                {"start": start, "end": start + 0x1000, "perm": 7, "data_hex": code.hex()},
                {"start": 0x403000, "end": 0x403010, "perm": 6, "sclass": "DATA", "data_hex": data.hex()},
            ],
            "scenario": "structured",
        }
        out = run_in_subprocess("tests.test_emulation_subprocess", json.dumps(plan), timeout=60.0)
        self.assertEqual(out["status"], "completed")
        self.assertEqual(bytes.fromhex(out["captures"]["plain"])[:5], b"hello")
        self.assertEqual(out["final_registers"]["edi"], 0x403005)
        self.assertEqual(out["final_registers"]["ecx"], 0)

    def test_rendered_report_keeps_the_summary_when_truncated(self) -> None:
        # The registry truncates long tool output; the status/PC block must
        # survive so a truncated result is still readable.
        decoder = b"\xeb\xfe" + b"\x90" * 0x20
        plan = {
            "tool": "emulate_code",
            "payload": {
                "start_address": "0x401000",
                "stop_address": "0x401010",
                "registers": {"eax": 1},
                "instruction_limit": 4,
            },
            "bits": 32,
            "segments": [
                {"start": 0x401000, "end": 0x402000, "perm": 7, "data_hex": (decoder + b"\x90" * 0x400).hex()},
            ],
            "scenario": "",
        }
        out = run_in_subprocess("tests.test_emulation_subprocess", json.dumps(plan), timeout=60.0)
        text = out["text"]
        self.assertIn("Status: instruction_limit", text)
        self.assertIn("Stop PC:", text)
        self.assertIn("Instructions executed: 4", text)
