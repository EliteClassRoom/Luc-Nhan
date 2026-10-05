"""Behaviour regressions for the bounded Unicorn emulation tools.

Every test asserts a consumer-visible outcome — a status, a stop PC, an
instruction count, a register value, a captured buffer, a discovered
string — through the real tool surface, or through the same front-end
the tools call when the check is about host-thread routing.

Real Unicorn runs happen in a fresh subprocess per scenario via
``tests.subprocess_test_worker`` so per-engine ctypes state cannot leak
between tests. Register/ABI/argument handling runs in-process against
the IDA mock because those paths must fail before an engine exists.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import unittest
import unittest.mock
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

importlib.invalidate_caches()

if "rikugan.ida.tools.emulation" in sys.modules:
    del sys.modules["rikugan.ida.tools.emulation"]
emu = importlib.import_module("rikugan.ida.tools.emulation")

from rikugan.core.errors import ToolError
from tests.subprocess_test_worker import run_in_subprocess

HAVE_UNICORN = importlib.util.find_spec("unicorn") is not None

# IDA segment permission bits (SEGPERM_EXEC=1, WRITE=2, READ=4).
RX = 5
RWX = 7
RO = 4
_PAGE = 0x1000


def _set_bits(bits: int) -> None:
    sys.modules["ida_ida"].inf_get_procname.return_value = "metapc"
    sys.modules["ida_ida"].inf_get_app_bitness.return_value = bits
    sys.modules["ida_ida"].inf_is_64bit.return_value = bits == 64
    sys.modules["ida_ida"].inf_is_32bit.return_value = bits == 32


def _segment(start: int, end: int, data: bytes, *, perm: int = RX, sclass: str = "CODE") -> dict:
    body = bytes(data) + b"\x00" * max(0, (end - start) - len(data))
    return {
        "start": start,
        "end": end,
        "perm": perm,
        "sclass": sclass,
        "data_hex": body.hex(),
    }


def _code_page(code: bytes, *, perm: int = RWX, start: int = 0x401000) -> dict:
    return _segment(start, start + _PAGE, code + b"\x90" * (_PAGE - len(code)), perm=perm)


def _rip_disp(target: int, next_address: int) -> bytes:
    """disp32 for a RIP-relative operand ending at *next_address*."""

    return (target - next_address).to_bytes(4, "little", signed=True)


def _stores(base: int, payload: bytes, *, at: int = 0x401000) -> bytes:
    """``mov byte [rip+d], imm8`` per byte.

    x64 mode decodes a 32-bit absolute operand (``moffs``) as
    address-size-relative, so a RIP-relative form is the encoding that
    actually reaches a scratch page in this Unicorn build.
    """

    out = bytearray()
    cursor = at
    for offset, byte in enumerate(payload):
        out += b"\xc6\x05" + _rip_disp(base + offset, cursor + 7) + bytes([byte])
        cursor += 7
    return bytes(out)


def _store_rip(target: int, at: int) -> bytes:
    """``mov byte [rip+d], al``."""

    return b"\x88\x05" + _rip_disp(target, at + 6)


def _load_rip32(target: int, at: int) -> bytes:
    """``mov eax, dword [rip+d]``."""

    return b"\x8b\x05" + _rip_disp(target, at + 6)


def _jump_to(from_address: int, to_address: int) -> bytes:
    return b"\xe9" + (to_address - from_address - 5).to_bytes(4, "little", signed=True)


def _run(
    *,
    tool: str,
    payload: dict,
    segments: list[dict],
    bits: int = 32,
    structured: bool = True,
    timeout: float = 90.0,
    **extra,
) -> dict:
    plan = {
        "tool": tool,
        "payload": payload,
        "bits": bits,
        "segments": segments,
        "scenario": "structured" if structured else "",
    }
    plan.update(extra)
    return run_in_subprocess("tests.test_emulation_subprocess", json.dumps(plan), timeout=timeout)


# ---------------------------------------------------------------------------
# CPU outcomes — the P0 bugs.
# ---------------------------------------------------------------------------


@unittest.skipUnless(HAVE_UNICORN, "unicorn not installed in this environment")
class TestCpuOutcomes(unittest.TestCase):
    def test_x64_add_updates_both_eax_and_rax(self) -> None:
        # P0: on x64 the 64-bit default register state was written after the
        # caller's 32-bit values, so the CPU ran with eax = 0.
        code = bytes.fromhex("83c001")  # add eax, 1
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401003",
                "registers": {"eax": 41},
            },
            segments=[_code_page(code)],
            bits=64,
        )
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["initial_registers"]["eax"], 41)
        self.assertEqual(out["final_registers"]["eax"], 42)
        self.assertEqual(out["final_registers"]["rax"], 42)
        self.assertEqual(out["final_registers"]["rip"], 0x401003)

    def test_requested_code_range_executes_despite_rw_segment(self) -> None:
        # Packed binaries mark .text R|W; the explicitly requested code range
        # still executes ("exec what was asked") while data pages keep their
        # faithful permissions.
        code = bytes.fromhex("b839050000")  # mov eax,0x539
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401005",
                "registers": {"eax": 0},
            },
            segments=[_code_page(code, perm=3)],  # R|W, no X
            bits=32,
        )
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["final_registers"]["eax"], 0x539)

    def test_explicit_flags_survive_register_defaults(self) -> None:
        code = bytes.fromhex("83c001")  # add eax, 1
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401003",
                "registers": {"eax": 0, "eflags": "0x202"},
            },
            segments=[_code_page(code)],
            bits=64,
        )
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["initial_registers"]["eflags"], 0x202)

    def test_carry_from_add_feeds_adc(self) -> None:
        # add al,-1 sets CF; adc eax,1 adds the carry on top of al = 0xff.
        code = bytes.fromhex("80c0ff83d001")  # add al,0xff ; adc eax,1
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401006",
                "registers": {"eax": 0},
            },
            segments=[_code_page(code)],
            bits=64,
        )
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["instruction_count"], 2)
        self.assertEqual(out["final_registers"]["eax"], 0x100)

    def test_stop_pc_and_count_are_exact_at_the_sentinel(self) -> None:
        code = bytes.fromhex("83c00183c001")  # add eax,1 ; add eax,1
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401006",
                "registers": {"eax": 0},
            },
            segments=[_code_page(code)],
            bits=64,
        )
        self.assertEqual(
            (out["status"], out["stop_pc"], out["instruction_count"]),
            ("completed", 0x401006, 2),
        )

    def test_one_instruction_budget_reports_the_real_stop_pc(self) -> None:
        # P0: the budget was charged before the first instruction, so
        # limit=1 suppressed the instruction the caller asked to run.
        code = bytes.fromhex("83c00183c001")  # add eax,1 ; add eax,1
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401006",
                "registers": {"eax": 0},
                "instruction_limit": 1,
            },
            segments=[_code_page(code)],
            bits=64,
        )
        self.assertEqual(out["status"], "instruction_limit")
        self.assertEqual(out["instruction_count"], 1)
        self.assertEqual(out["stop_pc"], 0x401003)
        self.assertEqual(out["final_registers"]["eax"], 1)

    def test_budget_stop_is_reported_for_a_backward_jump(self) -> None:
        # `jmp $` loops on one address; the engine's own counter would stop
        # it, so the hook must decide first and count exactly the budget.
        code = bytes.fromhex("ebfe")  # jmp $
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401010",
                "registers": {"eax": 7},
                "instruction_limit": 3,
            },
            segments=[_code_page(code)],
            bits=64,
        )
        self.assertEqual(out["status"], "instruction_limit")
        self.assertEqual(out["instruction_count"], 3)
        self.assertEqual(out["stop_pc"], 0x401000)

    def test_two_instruction_budget_completes(self) -> None:
        code = bytes.fromhex("83c00183c001")
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401006",
                "registers": {"eax": 0},
                "instruction_limit": 2,
            },
            segments=[_code_page(code)],
            bits=64,
        )
        self.assertEqual(
            (out["status"], out["stop_pc"], out["instruction_count"]),
            ("completed", 0x401006, 2),
        )

    def test_int3_mid_range_is_rejected_and_not_counted(self) -> None:
        # P0: a trap faulted inside Unicorn and surfaced as emulator_error.
        code = bytes.fromhex("b801000000cc") + bytes.fromhex("83c001")  # mov eax,1 ; int3 ; add eax,1
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401008",
                "registers": {"eax": 0},
            },
            segments=[_code_page(code)],
            bits=64,
        )
        self.assertEqual(out["status"], "unsupported_instruction")
        self.assertEqual(out["final_registers"]["eax"], 1)
        self.assertEqual(out["instruction_count"], 1)

    def test_any_software_interrupt_is_rejected(self) -> None:
        for imm, name in ((0x21, "int 0x21"), (0x80, "int 0x80"), (0x2E, "int 0x2e")):
            with self.subTest(name=name):
                code = bytes.fromhex("b801000000cd") + bytes([imm]) + b"\x90"
                out = _run(
                    tool="emulate_code",
                    payload={
                        "start_address": "0x401000",
                        "stop_address": "0x401007",
                        "registers": {"eax": 0},
                    },
                    segments=[_code_page(code)],
                    bits=64,
                )
                self.assertEqual(out["status"], "unsupported_instruction")
                self.assertEqual(out["final_registers"]["eax"], 1)

    def test_prefixed_syscall_inside_range_is_rejected(self) -> None:
        code = bytes.fromhex("900f0590")  # nop ; syscall ; nop
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401004",
                "registers": {"eax": 7},
            },
            segments=[_code_page(code)],
            bits=64,
        )
        self.assertEqual(out["status"], "unsupported_instruction")
        self.assertEqual(out["final_registers"]["eax"], 7)

    def test_rex_prefixed_traps_are_rejected_in_x64(self) -> None:
        # 0x40-0x4f are REX in long mode, so a REX-prefixed syscall/sysenter
        # must still be gated before it reaches the engine.
        for code, name in ((bytes.fromhex("400f0590"), "REX syscall"), (bytes.fromhex("480f3490"), "REX sysenter")):
            with self.subTest(instruction=name):
                out = _run(
                    tool="emulate_code",
                    payload={
                        "start_address": "0x401000",
                        "stop_address": hex(0x401000 + len(code)),
                        "registers": {"eax": 7},
                    },
                    segments=[_code_page(code)],
                    bits=64,
                )
                self.assertEqual(out["status"], "unsupported_instruction")
                self.assertEqual(out["final_registers"]["eax"], 7)

    def test_inc_dec_bytes_run_normally_in_x86(self) -> None:
        # The same bytes are `inc eax` / `dec eax` in 32-bit mode, so the
        # prefix walk must not swallow them.
        code = bytes.fromhex("40") + bytes.fromhex("48")
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": hex(0x401000 + len(code)),
                "registers": {"eax": 5},
            },
            segments=[_code_page(code)],
            bits=32,
        )
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["final_registers"]["eax"], 5)

    def test_into_and_int1_are_rejected(self) -> None:
        for opcode, names in ((0xCE, ("into",)), (0xF1, ("int1", "undecodable")), (0xCC, ("int3",))):
            # OF clear and set: `into` only traps when OF is 1, so the gate
            # must not depend on the flag.
            for eflags in (0x202, 0x203):
                # 0xf1 is a one-byte instruction; pad so the decoder never
                # reports a size outside the hook's 1..15 window.
                code = bytes([opcode]) + b"\x90\x90"
                with self.subTest(instruction=opcode, eflags=hex(eflags)):
                    out = _run(
                        tool="emulate_code",
                        payload={
                            "start_address": "0x401000",
                            "stop_address": hex(0x401000 + len(code)),
                            "registers": {"eax": 1, "eflags": eflags},
                        },
                        segments=[_code_page(code)],
                        bits=32,
                    )
                    self.assertEqual(out["status"], "unsupported_instruction")
                    self.assertTrue(
                        any(name in out["reason"] for name in names),
                        f"{out['reason']!r} mentions none of {names}",
                    )

    def test_ud2_is_reported_and_not_counted(self) -> None:
        code = bytes.fromhex("b8010000000f0b")  # mov eax,1 ; ud2
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401007",
                "registers": {"eax": 0},
            },
            segments=[_code_page(code)],
            bits=64,
        )
        self.assertEqual(out["status"], "unsupported_instruction")
        self.assertEqual(out["final_registers"]["eax"], 1)
        self.assertEqual(out["instruction_count"], 1)

    def test_sysenter_at_entry_is_rejected(self) -> None:
        code = bytes.fromhex("0f3490")  # sysenter ; nop
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401003",
                "registers": {"eax": 0},
            },
            segments=[_code_page(code)],
            bits=32,
        )
        self.assertEqual(out["status"], "unsupported_instruction")

    def test_write_to_read_only_data_is_a_permission_error(self) -> None:
        # P0: every fault was reported as unmapped, hiding permission bugs.
        code = _store_rip(0x404000, 0x401000) + b"\x90"
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": hex(0x401000 + len(code)),
                "registers": {"rax": 0x41},
                "memory_ranges": [{"address": "0x404000", "size": 16}],
            },
            segments=[
                _code_page(code),
                _segment(0x404000, 0x405000, b"\x41" * _PAGE, perm=RO, sclass="DATA"),
            ],
            bits=64,
        )
        self.assertEqual(out["status"], "permission_error")
        self.assertIn("read-only", out["reason"])

    def test_write_only_idb_allows_stores_but_denies_loads(self) -> None:
        for write, expected in ((True, "completed"), (False, "permission_error")):
            with self.subTest(write=write):
                code = (_store_rip if write else _load_rip32)(0x404000, 0x401000) + b"\x90"
                out = _run(
                    tool="emulate_code",
                    payload={
                        "start_address": "0x401000",
                        "stop_address": hex(0x401000 + len(code)),
                        "registers": {"rax": 0x41},
                        "memory_ranges": [{"address": "0x404000", "size": 16}],
                        "capture_ranges": [{"address": "0x404000", "size": 1, "label": "out"}],
                    },
                    segments=[
                        _code_page(code),
                        _segment(0x404000, 0x404010, b"\x00" * 16, perm=2, sclass="DATA"),
                    ],
                    bits=64,
                )
                self.assertEqual(out["status"], expected)
                if write:
                    self.assertEqual(out["captures"]["out"], "41")
                else:
                    self.assertIn("unreadable", out["reason"])
                    self.assertEqual(out["final_registers"]["rax"], 0x41)

    def test_write_beyond_declared_buffer_bytes_is_unmapped(self) -> None:
        # A 16-byte scratch buffer is backed; the rest of its page is a hole.
        code = _store_rip(0x403010, 0x401000) + b"\x90"
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": hex(0x401000 + len(code)),
                "registers": {"rax": 0x41},
                "memory_buffers": [
                    {"address": "0x403000", "size": 16, "data_hex": "", "permissions": "rw"},
                ],
            },
            segments=[_code_page(code)],
            bits=64,
        )
        self.assertEqual(out["status"], "unmapped_memory")


# ---------------------------------------------------------------------------
# Calling conventions and function mode.
# ---------------------------------------------------------------------------


@unittest.skipUnless(HAVE_UNICORN, "unicorn not installed in this environment")
class TestFunctionMode(unittest.TestCase):
    def _x86(self, code: bytes, stack: int = 0x20) -> list[dict]:
        return [
            _code_page(code, perm=RWX),
            _segment(0x402000, 0x402000 + stack, b"\x00" * stack, perm=RWX, sclass="BSS"),
        ]

    def _x64(self, code: bytes) -> list[dict]:
        return [_code_page(code, perm=RWX)]

    def test_ret_lands_on_the_return_sentinel(self) -> None:
        code = bytes.fromhex("b801000000c3")  # mov eax,1 ; ret
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401010",
                "registers": {},
                "execution_mode": "function",
                "calling_convention": "cdecl",
            },
            segments=self._x86(code),
            bits=32,
        )
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["stop_pc"], 0x401010)
        self.assertEqual(out["final_registers"]["eax"], 1)

    def test_x86_cdecl_arguments_ascend_from_the_return_address(self) -> None:
        # cdecl pushes arguments right-to-left: the sentinel is at [esp] and
        # the first argument is at [esp+4].
        code = bytes.fromhex("8b442404c3")  # mov eax,[esp+4] ; ret
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401010",
                "registers": {},
                "execution_mode": "function",
                "calling_convention": "cdecl",
                "arguments": ["0x41", 2],
                "capture_ranges": [{"stack_offset": 0, "size": 16, "label": "frame"}],
            },
            segments=self._x86(code),
            bits=32,
        )
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["final_registers"]["eax"], 0x41)
        frame = bytes.fromhex(out["captures"]["frame"])
        self.assertEqual(int.from_bytes(frame[0:4], "little"), 0x401010)  # return sentinel
        self.assertEqual(int.from_bytes(frame[4:8], "little"), 0x41)  # first argument
        self.assertEqual(int.from_bytes(frame[8:12], "little"), 2)  # second argument

    def test_x86_fastcall_uses_ecx_edx(self) -> None:
        # The first fastcall argument is the pointer the callee reads.
        code = b"\x8b\x01" + b"\xc3"  # mov eax,[ecx] ; ret
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401010",
                "registers": {},
                "execution_mode": "function",
                "calling_convention": "fastcall",
                "arguments": ["0x402000", 6],
                "memory_ranges": [{"address": "0x402000", "size": 0x20}],
            },
            segments=[
                _code_page(code, perm=RWX),
                _segment(0x402000, 0x402020, b"\x55" * 0x20, perm=RWX, sclass="DATA"),
            ],
            bits=32,
        )
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["initial_registers"]["ecx"], 0x402000)
        self.assertEqual(out["initial_registers"]["edx"], 6)
        self.assertEqual(out["final_registers"]["eax"], 0x55555555)

    def test_win64_arguments_use_the_microsoft_register_order(self) -> None:
        # mov eax,[rsp+40] : the 5th win64 argument, above the shadow space.
        code = bytes.fromhex("8b442428c3")  # mov eax,[rsp+40] ; ret
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401010",
                "registers": {},
                "execution_mode": "function",
                "calling_convention": "win64",
                "arguments": [1, 2, 3, 4, 0x2A],
            },
            segments=self._x64(code),
            bits=64,
        )
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["initial_registers"]["rcx"], 1)
        self.assertEqual(out["initial_registers"]["rdx"], 2)
        self.assertEqual(out["initial_registers"]["r8"], 3)
        self.assertEqual(out["initial_registers"]["r9"], 4)
        self.assertEqual(out["initial_registers"]["rsp"] % 16, 8)
        self.assertEqual(out["final_registers"]["eax"], 0x2A)

    def test_win64_preserves_initialized_shadow_space(self) -> None:
        code = bytes.fromhex("8b442408c3")  # mov eax,[rsp+8] ; ret
        stack_pointer = 0x7FFE000FFF08
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401010",
                "registers": {"rsp": stack_pointer},
                "execution_mode": "function",
                "calling_convention": "win64",
                "memory_buffers": [
                    {"address": hex(stack_pointer + 8), "size": 4, "data_hex": "78563412"},
                ],
            },
            segments=self._x64(code),
            bits=64,
        )
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["final_registers"]["eax"], 0x12345678)

    def test_sysv64_arguments_use_the_system_v_register_order(self) -> None:
        # mov eax,[rsp+8] : the first stack argument of a 7-argument call.
        code = bytes.fromhex("8b442408c3")  # mov eax,[rsp+8] ; ret
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401010",
                "registers": {},
                "execution_mode": "function",
                "calling_convention": "sysv64",
                "arguments": [1, 2, 3, 4, 5, 6, 7],
            },
            segments=self._x64(code),
            bits=64,
        )
        self.assertEqual(out["status"], "completed")
        for name, value in (("rdi", 1), ("rsi", 2), ("rdx", 3), ("rcx", 4), ("r8", 5), ("r9", 6)):
            self.assertEqual(out["initial_registers"][name], value, name)
        self.assertEqual(out["initial_registers"]["rsp"] % 16, 8)
        self.assertEqual(out["final_registers"]["eax"], 7)

    def test_stack_relative_capture_reads_a_local_buffer(self) -> None:
        # sub rsp,0x20 ; "ab!" at [rsp] ; add rsp,0x20 — the buffer sits 0x20
        # below the entry SP the ABI handed the callee. The letters avoid
        # 0x48/0x4c, which a decoder would read as a REX prefix.
        code = (
            bytes.fromhex("4883ec20")
            + bytes.fromhex("c60424")
            + b"a"
            + bytes.fromhex("c6842401000000")
            + b"b"
            + bytes.fromhex("c6842402000000")
            + b"!"
            + bytes.fromhex("c6842403000000")
            + b"\x00"
            + bytes.fromhex("4883c420")
            + b"\xc3"
        )
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": hex(0x401000 + len(code)),
                "registers": {},
                "execution_mode": "function",
                "calling_convention": "sysv64",
                "capture_ranges": [{"stack_offset": -32, "size": 8, "label": "local"}],
            },
            segments=self._x64(code),
            bits=64,
        )
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["stop_pc"], 0x401000 + len(code))
        self.assertEqual(bytes.fromhex(out["captures"]["local"])[:4], b"ab!\x00")

    def test_range_mode_keeps_an_explicit_stack_pointer(self) -> None:
        # Range mode never rewrites SP and needs no calling convention.
        # mov dword [rsp],0x5a5a5a5a ; add rsp,0x20 ; nop
        code = b"\xc7\x04\x24" + (0x5A5A5A5A).to_bytes(4, "little") + b"\x90"
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": hex(0x401000 + len(code)),
                "registers": {"rsp": "0x7FFE000F0000"},
                "capture_ranges": [{"stack_offset": 0, "size": 8, "label": "frame"}],
            },
            segments=[_code_page(code)],
            bits=64,
        )
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["initial_registers"]["rsp"], 0x7FFE000F0000)
        self.assertEqual(bytes.fromhex(out["captures"]["frame"])[0:4], b"ZZZZ")

    def test_ordinary_capture_address_still_works(self) -> None:
        # 32-bit absolute form: in x64 Unicorn sign-extends a moffs operand,
        # in x86 mode it addresses the page directly.
        code = b"\xa1" + (0x401100).to_bytes(4, "little") + b"\x90"
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": hex(0x401000 + len(code)),
                "registers": {},
                "execution_mode": "function",
                "calling_convention": "cdecl",
                "capture_ranges": [{"address": "0x401100", "size": 4, "label": "out"}],
            },
            segments=[
                _code_page(code, perm=RWX),
                _segment(0x401100, 0x401200, b"\x00" * 0x100, perm=RWX, sclass="DATA"),
            ],
            bits=32,
        )
        self.assertEqual(out["status"], "completed")
        self.assertEqual(bytes.fromhex(out["captures"]["out"])[0:4], b"\x90\x90\x90\x90")


# ---------------------------------------------------------------------------
# Runtime inputs: scratch buffers, code ranges, discovery, deadlines.
# ---------------------------------------------------------------------------


@unittest.skipUnless(HAVE_UNICORN, "unicorn not installed in this environment")
class TestInputsAndDeadlines(unittest.TestCase):
    def test_scratch_buffer_feeds_the_decoder(self) -> None:
        # movzx eax, byte [abs] — an absolute read of the scratch buffer.
        code = _load_rip32(0x500000, 0x401000) + b"\x90"
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": hex(0x401000 + len(code)),
                "registers": {"eax": 0},
                "memory_buffers": [
                    {"address": "0x500000", "size": 16, "data_hex": "78", "permissions": "rw"},
                ],
            },
            segments=[_code_page(code)],
            bits=64,
        )
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["final_registers"]["eax"], 0x78)

    def test_read_only_scratch_buffer_is_not_writable(self) -> None:
        code = _store_rip(0x500000, 0x401000) + b"\x90"
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": hex(0x401000 + len(code)),
                "registers": {"rax": 0x41},
                "memory_buffers": [
                    {"address": "0x500000", "size": 16, "data_hex": "41", "permissions": "r"},
                ],
            },
            segments=[_code_page(code)],
            bits=64,
        )
        self.assertEqual(out["status"], "permission_error")

    def test_declared_code_range_allows_the_hop(self) -> None:
        # `jmp helper` ; the helper is a `ret` reached through the declared
        # code range, so the sentinel at the range end ends the run.
        segments = [
            _code_page(_jump_to(0x401000, 0x402000) + b"\x90" * 3),
            _code_page(bytes.fromhex("b809000000c3") + b"\x90" * 8, start=0x402000),
        ]
        base = {
            "start_address": "0x401000",
            "stop_address": "0x401010",
            "registers": {},
            "execution_mode": "function",
            "calling_convention": "sysv64",
            "code_ranges": [{"address": "0x402000", "size": 16}],
        }
        out = _run(tool="emulate_code", payload=base, segments=segments, bits=64)
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["final_registers"]["eax"], 9)

        undeclared = {k: v for k, v in base.items() if k != "code_ranges"}
        blocked = _run(tool="emulate_code", payload=undeclared, segments=segments, bits=64)
        self.assertEqual(blocked["status"], "range_exit")
        self.assertEqual(blocked["instruction_count"], 1)

    def test_timeout_returns_partial_state(self) -> None:
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401010",
                "registers": {"eax": 0x7B},
                "timeout_seconds": 0.3,
            },
            segments=[_code_page(b"\xeb\xfe")],  # jmp $
            bits=64,
        )
        self.assertEqual(out["status"], "timeout")
        self.assertEqual(out["final_registers"]["eax"], 0x7B)
        self.assertGreater(out["instruction_count"], 0)

    def test_collect_strings_finds_a_string_beyond_the_write_event_cap(self) -> None:
        code = _stores(0x500000, b"HELLO\x00", at=0x401000) + b"\x90"
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": hex(0x401000 + len(code)),
                "registers": {"eax": 0},
                "collect_strings": True,
                "memory_buffers": [
                    {"address": "0x500000", "size": 256, "data_hex": "", "permissions": "rw"},
                ],
            },
            segments=[_code_page(code)],
            bits=64,
        )
        self.assertIn("HELLO", [s["text"] for s in out["discovered_strings"]])
        self.assertEqual(out["write_event_count"], 6)

    def test_string_written_after_a_hundred_other_writes_is_still_found(self) -> None:
        # The 64-result cap must not be spent on the noise writes.
        noise = _stores(0x500000, b"\x90" * 100, at=0x401000)
        tail = _stores(0x500100, b"MIDDLESTRING\x00", at=0x401000 + len(noise))
        code = noise + tail + b"\x90"
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": hex(0x401000 + len(code)),
                "registers": {"eax": 0},
                "collect_strings": True,
                "memory_buffers": [
                    {"address": "0x500000", "size": 512, "data_hex": "", "permissions": "rw"},
                ],
            },
            segments=[_code_page(code)],
            bits=64,
        )
        texts = [s["text"] for s in out["discovered_strings"]]
        self.assertIn("MIDDLESTRING", texts)
        self.assertGreater(out["write_event_count"], 100)

    def test_discovery_uses_real_addresses_for_a_mid_page_buffer(self) -> None:
        # The buffer starts mid-page, so the dirty page and the valid range
        # do not share an origin: addresses must still be exact VAs.
        base = 0x500234
        code = _stores(base, b"MIDPAGE\x00", at=0x401000) + b"\x90"
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": hex(0x401000 + len(code)),
                "registers": {"eax": 0},
                "collect_strings": True,
                "memory_buffers": [
                    {"address": hex(base), "size": 64, "data_hex": "", "permissions": "rw"},
                ],
            },
            segments=[_code_page(code)],
            bits=64,
        )
        self.assertEqual(out["status"], "completed")
        found = [(s["address"], s["text"]) for s in out["discovered_strings"]]
        self.assertIn((base, "MIDPAGE"), found)

    def test_discovery_uses_real_addresses_for_a_mid_page_segment(self) -> None:
        base = 0x500234
        code = _stores(base, b"SEGSTR\x00", at=0x401000) + b"\x90"
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": hex(0x401000 + len(code)),
                "registers": {"eax": 0},
                "collect_strings": True,
                "memory_ranges": [{"address": hex(base), "size": 0x40}],
            },
            segments=[
                _code_page(code),
                _segment(base, base + 0x40, b"\x00" * 0x40, perm=6, sclass="DATA"),
            ],
            bits=64,
        )
        self.assertEqual(out["status"], "completed")
        found = [(s["address"], s["text"]) for s in out["discovered_strings"]]
        self.assertIn((base, "SEGSTR"), found)

    def test_unchanged_strings_are_not_discovered(self) -> None:
        code = bytes.fromhex("b801000000c3")
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401006",
                "registers": {"eax": 0},
                "collect_strings": True,
                "memory_buffers": [
                    {
                        "address": "0x500000",
                        "size": 32,
                        "data_hex": b"STATICSTRING\x00".hex(),
                        "permissions": "rw",
                    },
                ],
            },
            segments=[_code_page(code)],
            bits=64,
        )
        self.assertNotIn("STATICSTRING", [s["text"] for s in out["discovered_strings"]])

    def test_resolve_emulated_string_captures_a_scratch_output(self) -> None:
        code = _stores(0x401100, b"ABC\x00", at=0x401000) + b"\x90"
        out = _run(
            tool="resolve_emulated_string",
            payload={
                "start_address": "0x401000",
                "stop_address": hex(0x401000 + len(code)),
                "registers": {"eax": 0},
                "output_address": "0x401100",
                "max_output_size": 8,
            },
            segments=[_code_page(code)],
            bits=64,
        )
        self.assertEqual(out["status"], "completed")
        self.assertEqual(bytes.fromhex(out["captures"]["output"])[:4], b"ABC\x00")
        self.assertEqual(out["captured_strings"]["output"]["ascii"], "ABC")

    def test_resolve_emulated_string_captures_a_stack_local(self) -> None:
        # The public resolver must take the same signed stack offset as
        # emulate_code's capture_ranges.
        code = (
            bytes.fromhex("4883ec20")
            + bytes.fromhex("c6")
            + b"\x04\x24"
            + b"a"
            + bytes.fromhex("c6")
            + b"\x84\x24\x01\x00\x00\x00"
            + b"b"
            + bytes.fromhex("c6")
            + b"\x84\x24\x02\x00\x00\x00"
            + b"!"
            + bytes.fromhex("c6")
            + b"\x84\x24\x03\x00\x00\x00"
            + b"\x00"
            + bytes.fromhex("4883c420")
            + b"\x90\x90"
        )
        out = _run(
            tool="resolve_emulated_string",
            payload={
                "start_address": "0x401000",
                "stop_address": hex(0x401000 + len(code)),
                "registers": {},
                "execution_mode": "function",
                "calling_convention": "sysv64",
                "output_stack_offset": -32,
                "max_output_size": 8,
            },
            segments=[_code_page(code)],
            bits=64,
        )
        self.assertEqual(out["status"], "completed")
        self.assertEqual(bytes.fromhex(out["captures"]["output"])[:4], b"ab!\x00")
        self.assertEqual(out["captured_strings"]["output"]["ascii"], "ab!")

    def test_cancellation_returns_partial_state(self) -> None:
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401010",
                "registers": {"eax": 0x7B},
            },
            segments=[_code_page(b"\xeb\xfe")],  # jmp $
            bits=64,
            structured=False,
            registry=True,
            cancel_after=0.3,
        )
        self.assertIn("cancelled", out["text"])
        self.assertIn("Stop PC:", out["text"])

    def test_cancel_before_start_does_not_execute(self) -> None:
        out = _run(
            tool="emulate_code",
            payload={
                "start_address": "0x401000",
                "stop_address": "0x401006",
                "registers": {"eax": 41},
            },
            segments=[_code_page(bytes.fromhex("83c00183c001"))],
            bits=64,
            structured=False,
            registry=True,
            cancel_before=True,
        )
        self.assertIn("cancelled", out["text"])
        self.assertIn("Instructions executed: 0", out["text"])


# ---------------------------------------------------------------------------
# Register handling (no engine needed).
# ---------------------------------------------------------------------------


def _arch(label: str) -> emu.ArchMode:
    if HAVE_UNICORN:
        import unicorn

        arch_const = unicorn.UC_ARCH_X86
        mode_const = unicorn.UC_MODE_64 if label == "x64" else unicorn.UC_MODE_32
    else:  # pragma: no cover - constants only
        arch_const, mode_const = 4, (2 if label == "x64" else 1)
    return emu.ArchMode(
        label=label,
        arch_const=arch_const,
        mode_const=mode_const,
        ptr_size=8 if label == "x64" else 4,
        ip_reg="rip" if label == "x64" else "eip",
        sp_reg="rsp" if label == "x64" else "esp",
        flags_reg="rflags" if label == "x64" else "eflags",
        stack_base=0x7FFE00000000 if label == "x64" else 0x7FFE0000,
    )


class TestRegisterNormalization(unittest.TestCase):
    def test_single_alias_maps_onto_the_native_register(self) -> None:
        self.assertEqual(
            emu._normalize_registers({"eax": 0x41}, _arch("x64"), tool_name="t"),
            {"rax": 0x41},
        )
        self.assertEqual(
            emu._normalize_registers({"rax": 0x1_41}, _arch("x64"), tool_name="t"),
            {"rax": 0x1_41},
        )

    def test_conflicting_aliases_are_rejected(self) -> None:
        # Any bit both aliases name must agree: a differing value, a zero
        # against a non-zero, and a value stated outside the narrower alias
        # are all contradictions, never an OR.
        for reg in (
            {"rax": 0x1, "eax": 0x2},
            {"rax": 0x141, "eax": 0x41},
            {"eax": 0x41, "ax": 0x42},
            {"eax": 0x100, "al": 0x42},
            {"ax": 0x42, "ah": 0x1},
        ):
            with self.subTest(registers=reg):
                with self.assertRaises(ToolError):
                    emu._normalize_registers(reg, _arch("x64"), tool_name="t")

    def test_narrow_alias_alone(self) -> None:
        self.assertEqual(
            emu._normalize_registers({"al": 0x42}, _arch("x64"), tool_name="t"),
            {"rax": 0x42},
        )
        self.assertEqual(
            emu._normalize_registers({"ah": 0x42}, _arch("x64"), tool_name="t"),
            {"rax": 0x4200},
        )

    def test_64_only_registers_rejected_on_x86(self) -> None:
        with self.assertRaises(ToolError):
            emu._normalize_registers({"r8": 1}, _arch("x86"), tool_name="t")
        with self.assertRaises(ToolError):
            emu._normalize_registers({"rax": 1}, _arch("x86"), tool_name="t")

    def test_byte_registers_are_mode_specific(self) -> None:
        # sil/dil/bpl/spl are 64-bit-mode names; there the same encodings
        # are inc/dec, so a 32-bit database must refuse the names outright.
        for name, native in (("sil", "rsi"), ("dil", "rdi"), ("bpl", "rbp"), ("spl", "rsp")):
            with self.subTest(register=name, mode="x86"):
                with self.assertRaises(ToolError):
                    emu._normalize_registers({name: 1}, _arch("x86"), tool_name="t")
            with self.subTest(register=name, mode="x64"):
                self.assertEqual(
                    emu._normalize_registers({name: 1}, _arch("x64"), tool_name="t"),
                    {native: 1},
                )

    def test_unknown_register_and_ip_override_rejected(self) -> None:
        with self.assertRaises(ToolError):
            emu._normalize_registers({"nope": 1}, _arch("x64"), tool_name="t")
        with self.assertRaises(ToolError):
            emu._normalize_registers({"rip": 0x500000}, _arch("x64"), tool_name="t")

    def test_hex_strings_and_empty_input(self) -> None:
        self.assertEqual(
            emu._normalize_registers({"rbx": "0x1234"}, _arch("x64"), tool_name="t"),
            {"rbx": 0x1234},
        )
        self.assertEqual(emu._normalize_registers({}, _arch("x64"), tool_name="t"), {})


class TestAbiLayout(unittest.TestCase):
    def test_x86_cdecl_arguments_ascend_from_the_return_address(self) -> None:
        abi = emu._build_abi(
            _arch("x86"),
            tool_name="t",
            convention="cdecl",
            arguments=[0x11, 0x22],
            registers={},
            return_address=0x401FFF,
        )
        by_address = dict(abi.stack_writes)
        sp = abi.stack_pointer
        self.assertEqual(by_address[sp], (0x401FFF).to_bytes(4, "little"))
        self.assertEqual(by_address[sp + 4], (0x11).to_bytes(4, "little"))
        self.assertEqual(by_address[sp + 8], (0x22).to_bytes(4, "little"))

    def test_x86_fastcall_spills_from_sp_plus_ptr(self) -> None:
        abi = emu._build_abi(
            _arch("x86"),
            tool_name="t",
            convention="fastcall",
            arguments=[1, 2, 3],
            registers={},
            return_address=0x401FFF,
        )
        by_address = dict(abi.stack_writes)
        self.assertEqual(abi.registers["ecx"], 1)
        self.assertEqual(abi.registers["edx"], 2)
        self.assertEqual(by_address[abi.stack_pointer + 4], (3).to_bytes(4, "little"))

    def test_win64_reserves_shadow_space(self) -> None:
        abi = emu._build_abi(
            _arch("x64"),
            tool_name="t",
            convention="win64",
            arguments=[1, 2, 3, 4, 5],
            registers={},
            return_address=0x401FFF,
        )
        self.assertEqual(abi.registers["rcx"], 1)
        self.assertEqual(abi.registers["r9"], 4)
        by_address = dict(abi.stack_writes)
        self.assertEqual(by_address[abi.stack_pointer + 40], (5).to_bytes(8, "little"))
        self.assertEqual(abi.stack_pointer % 16, 8)

    def test_sysv64_extra_args_start_at_sp_plus_eight(self) -> None:
        abi = emu._build_abi(
            _arch("x64"),
            tool_name="t",
            convention="sysv64",
            arguments=[1, 2, 3, 4, 5, 6, 7],
            registers={},
            return_address=0x401FFF,
        )
        by_address = dict(abi.stack_writes)
        self.assertEqual(by_address[abi.stack_pointer + 8], (7).to_bytes(8, "little"))

    def test_range_mode_needs_no_convention_and_keeps_supplied_sp(self) -> None:
        abi = emu._build_abi(
            _arch("x64"),
            tool_name="t",
            convention="",
            arguments=[],
            registers={"rsp": 0x7FFE000F0000},
            return_address=None,
        )
        self.assertEqual(abi.stack_pointer, 0x7FFE000F0000)
        self.assertEqual(abi.stack_writes, ())

    def test_supplied_sp_outside_the_stack_is_rejected(self) -> None:
        with self.assertRaises(ToolError):
            emu._build_abi(
                _arch("x64"),
                tool_name="t",
                convention="",
                arguments=[],
                registers={"rsp": 0x1000},
                return_address=None,
            )

    def test_supplied_stack_pointer_must_be_inside_the_stack(self) -> None:
        for reg, value in (("rsp", 0x1000), ("rsp", 0), ("esp", 0x1000), ("esp", 0)):
            with self.subTest(register=reg, value=hex(value)):
                with self.assertRaises(ToolError):
                    emu._build_abi(
                        _arch("x64" if reg == "rsp" else "x86"),
                        tool_name="t",
                        convention="sysv64" if reg == "rsp" else "cdecl",
                        arguments=[],
                        registers={reg: value},
                        return_address=0x401FFF,
                    )

    def test_rejected_sp_reports_a_usable_stack_range(self) -> None:
        # The exclusive top of the stack mapping is the value a caller most
        # plausibly reaches for; the error has to name the real range and a
        # concrete default, otherwise the retry repeats the same SP.
        for label, reg in (("x86", "esp"), ("x64", "rsp")):
            with self.subTest(arch=label):
                arch = _arch(label)
                with self.assertRaises(ToolError) as ctx:
                    emu._build_abi(
                        arch,
                        tool_name="emulate_code",
                        convention="",
                        arguments=[],
                        registers={reg: arch.stack_base + 0x100000},
                        return_address=None,
                    )
                message = str(ctx.exception)
                self.assertIn(f"the synthetic stack is 0x{arch.stack_base:x}", message)
                # The advertised default must itself pass the same check.
                top = arch.stack_base + 0x100000 - 0x100
                self.assertIn(f"default 0x{top:x}", message)
                abi = emu._build_abi(
                    arch,
                    tool_name="emulate_code",
                    convention="",
                    arguments=[],
                    registers={},
                    return_address=None,
                )
                self.assertEqual(abi.stack_pointer, top)

    def test_win64_shadow_space_must_fit(self) -> None:
        arch = _arch("x64")
        top = arch.stack_base + 0x100000 - 0x100
        abi = emu._build_abi(
            arch,
            tool_name="t",
            convention="win64",
            arguments=[],
            registers={"rsp": top},
            return_address=0x401FFF,
        )
        self.assertLessEqual(abi.stack_pointer + 40, arch.stack_base + 0x100000)
        # An entry SP a little lower pushes the aligned frame (and its
        # 32-byte shadow space) past the top of the synthetic stack.
        with self.assertRaises(ToolError) as ctx:
            emu._build_abi(
                arch,
                tool_name="t",
                convention="win64",
                arguments=[],
                registers={"rsp": arch.stack_base + 0x100000 - 0x20},
                return_address=0x401FFF,
            )
        self.assertIn("shadow space", str(ctx.exception))

    def test_range_mode_needs_no_convention(self) -> None:
        _set_bits(64)
        sys.modules["ida_ida"].inf_get_procname.return_value = "metapc"
        emu.ida_ida.inf_get_procname.return_value = "metapc"
        with self.assertRaises(ToolError) as ctx:
            emu.emulate_code(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"eax": 1},
                calling_convention="sysv64",
            )
        self.assertIn("execution_mode='function'", str(ctx.exception))

    def test_conflicting_argument_register_rejected(self) -> None:
        with self.assertRaises(ToolError):
            emu._build_abi(
                _arch("x64"),
                tool_name="t",
                convention="sysv64",
                arguments=[0x11],
                registers={"rdi": 0x22},
                return_address=0x401FFF,
            )

    def test_mismatched_convention_rejected(self) -> None:
        with self.assertRaises(ToolError):
            emu._build_abi(
                _arch("x86"),
                tool_name="t",
                convention="sysv64",
                arguments=[],
                registers={},
                return_address=0,
            )
        with self.assertRaises(ToolError):
            emu._build_abi(
                _arch("x64"),
                tool_name="t",
                convention="cdecl",
                arguments=[],
                registers={},
                return_address=0,
            )

    def test_argument_count_overflow_rejected(self) -> None:
        with self.assertRaises(ToolError):
            emu._build_abi(
                _arch("x64"),
                tool_name="t",
                convention="sysv64",
                arguments=list(range(200_000)),
                registers={},
                return_address=0x401FFF,
            )


class TestCaptureSpecValidation(unittest.TestCase):
    def _norm(self, items, tool_name="t"):
        return emu._normalize_capture_specs(items, tool_name=tool_name, default_label="output")

    def test_exactly_one_of_address_or_stack_offset(self) -> None:
        self.assertEqual(self._norm([{"address": 0x401000, "size": 8}])[0]["address"], 0x401000)
        self.assertEqual(self._norm([{"stack_offset": -16, "size": 8}])[0]["stack_offset"], -16)
        with self.assertRaises(ToolError):
            self._norm([{"address": 0x401000, "stack_offset": 4, "size": 8}])
        with self.assertRaises(ToolError):
            self._norm([{"size": 8}])

    def test_negative_hex_stack_offset(self) -> None:
        self.assertEqual(self._norm([{"stack_offset": "-0x20", "size": 8}])[0]["stack_offset"], -32)
        with self.assertRaises(ToolError):
            self._norm([{"stack_offset": True, "size": 8}])
        with self.assertRaises(ToolError):
            self._norm([{"stack_offset": 1.5, "size": 8}])

    def test_duplicate_labels_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self._norm(
                [
                    {"address": 0x401000, "size": 8, "label": "a"},
                    {"address": 0x401010, "size": 8, "label": "a"},
                ]
            )

    def test_size_bounds(self) -> None:
        with self.assertRaises(ToolError):
            self._norm([{"address": 0x401000, "size": 4097}])
        with self.assertRaises(ToolError):
            self._norm([{"address": 0x401000, "size": 0}])

    def test_capture_count_cap(self) -> None:
        with self.assertRaises(ToolError):
            self._norm([{"address": 0x401000 + i * 8, "size": 8} for i in range(17)])


class TestBufferValidation(unittest.TestCase):
    def _norm(self, items, tool_name="t"):
        return emu._normalize_buffers(items, tool_name=tool_name)

    def test_buffer_defaults_to_rw_and_decodes_hex(self) -> None:
        (buf,) = self._norm([{"address": 0x500000, "size": 4, "data_hex": "78563412"}])
        self.assertEqual(buf.data, bytes.fromhex("78563412"))
        self.assertEqual(buf.permissions, 3)

    def test_executable_scratch_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self._norm([{"address": 0x500000, "size": 4, "data_hex": "00", "permissions": "rwx"}])

    def test_hex_longer_than_size_rejected_before_decode(self) -> None:
        with self.assertRaises(ToolError):
            self._norm([{"address": 0x500000, "size": 2, "data_hex": "00112233"}])

    def test_odd_hex_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self._norm([{"address": 0x500000, "size": 4, "data_hex": "abc"}])

    def test_bad_hex_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self._norm([{"address": 0x500000, "size": 4, "data_hex": "zzzz"}])

    def test_duplicate_address_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self._norm(
                [
                    {"address": 0x500000, "size": 4, "data_hex": "00"},
                    {"address": 0x500000, "size": 8, "data_hex": "00"},
                ]
            )

    def test_aggregate_cap_rejected_before_decoding(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self._norm([{"address": 0x500000 + i * 0x400000, "size": 0x400000, "data_hex": "00"} for i in range(6)])
        self.assertIn("aggregate mapping cap", str(ctx.exception))


class TestArgumentValidation(unittest.TestCase):
    def setUp(self) -> None:
        _set_bits(64)
        sys.modules["ida_ida"].inf_get_procname.return_value = "metapc"

    def tearDown(self) -> None:
        _set_bits(64)
        sys.modules["ida_ida"].inf_get_procname.return_value = "metapc"

    def test_range_mode_rejects_arguments(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            emu.emulate_code(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"eax": 0},
                arguments=[1],
            )
        self.assertIn("function mode", str(ctx.exception))

    def test_function_mode_requires_convention(self) -> None:
        with self.assertRaises(ToolError):
            emu.emulate_code(
                start_address="0x401000",
                stop_address="0x401010",
                registers={},
                execution_mode="function",
            )

    def test_unknown_execution_mode_rejected(self) -> None:
        with self.assertRaises(ToolError):
            emu.emulate_code(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"eax": 0},
                execution_mode="loop",
            )

    def test_timeout_bounds(self) -> None:
        for bad in (0, -1, float("inf"), float("nan"), "5"):
            with self.assertRaises(ToolError):
                emu._validate_timeout(bad, tool_name="emulate_code")
        self.assertEqual(emu._validate_timeout(100.0, tool_name="t"), 20.0)
        self.assertEqual(emu._validate_timeout(3, tool_name="t"), 3.0)

    def test_instruction_limit_bounds(self) -> None:
        with self.assertRaises(ToolError):
            emu.emulate_code(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"eax": 0},
                instruction_limit=0,
            )
        with self.assertRaises(ToolError):
            emu.emulate_code(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"eax": 0},
                instruction_limit=2_000_000,
            )

    def test_start_after_stop_rejected(self) -> None:
        with self.assertRaises(ToolError):
            emu.emulate_code(
                start_address="0x401010",
                stop_address="0x401000",
                registers={"eax": 0},
            )

    def test_resolve_requires_exactly_one_output_location(self) -> None:
        with self.assertRaises(ToolError):
            emu.resolve_emulated_string(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"eax": 0},
            )
        with self.assertRaises(ToolError):
            emu.resolve_emulated_string(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"eax": 0},
                output_address="0x401100",
                output_stack_offset=0,
            )

    def test_resolve_max_output_size_bounded(self) -> None:
        with self.assertRaises(ToolError):
            emu.resolve_emulated_string(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"eax": 0},
                output_address="0x401100",
                max_output_size=8192,
            )

    def test_empty_registers_rejected_in_range_mode(self) -> None:
        # Range mode replays code the caller describes; an empty map would
        # silently invent a zeroed machine state.
        sys.modules["ida_ida"].inf_get_procname.return_value = "metapc"
        emu.ida_ida.inf_get_procname.return_value = "metapc"
        for call in (
            lambda: emu.emulate_code(
                start_address="0x401000",
                stop_address="0x401010",
                registers={},
            ),
            lambda: emu.resolve_emulated_string(
                start_address="0x401000",
                stop_address="0x401010",
                registers={},
                output_address="0x401100",
            ),
        ):
            with self.subTest(tool=call):
                with self.assertRaises(ToolError) as ctx:
                    call()
                self.assertIn("at least one initial register", str(ctx.exception))


class TestArchitectureValidation(unittest.TestCase):
    """Kept separate: the unsupported-architecture case rewrites the mock."""

    def setUp(self) -> None:
        _set_bits(64)
        sys.modules["ida_ida"].inf_get_procname.return_value = "metapc"

    def test_unsupported_arch_rejected(self) -> None:
        # `emulation.ida_ida` is bound at import time, so the handler's own
        # view is what has to say ARM.
        sys.modules["ida_ida"].inf_get_procname.return_value = "ARM"
        self.addCleanup(setattr, sys.modules["ida_ida"].inf_get_procname, "return_value", "metapc")
        emu.ida_ida.inf_get_procname.return_value = "ARM"
        with self.assertRaises(ToolError) as ctx:
            emu.emulate_code(
                start_address="0x401000",
                stop_address="0x401010",
                registers={"eax": 0},
            )
        self.assertIn("Unsupported architecture", str(ctx.exception))


# ---------------------------------------------------------------------------
# Host-thread dispatch and the missing-Unicorn path.
# ---------------------------------------------------------------------------


@dataclass
class _Dispatched:
    thread: str


def _install_segments(segments: list[dict]) -> None:
    import ida_bytes
    import ida_segment

    entries = []
    for spec in segments:
        seg = unittest.mock.MagicMock()
        seg.start_ea = spec["start"]
        seg.end_ea = spec["end"]
        seg.perm = spec.get("perm", RX)
        seg.sclass = spec.get("sclass", "CODE")
        entries.append((seg, bytes.fromhex(spec["data_hex"])))

    _set_bits(64)
    sys.modules["ida_ida"].inf_get_procname.return_value = "metapc"
    ida_segment.get_segm_qty.return_value = len(entries)
    ida_segment.getnseg.side_effect = lambda index: entries[index][0]
    ida_segment.get_segm_class.side_effect = lambda seg: getattr(seg, "sclass", "CODE")
    ida_segment.getseg.side_effect = lambda ea: next((s for s, _ in entries if s.start_ea <= ea < s.end_ea), None)

    def _get_bytes_and_mask(ea: int, size: int):
        # Packed LSB-first definedness: ceil(size / 8) mask bytes, one bit
        # per payload byte. A byte no segment covers stays undefined.
        payload = bytearray()
        mask = bytearray((size + 7) // 8)
        for offset in range(size):
            address = ea + offset
            byte = None
            for seg, data in entries:
                if seg.start_ea <= address < seg.end_ea:
                    byte = data[address - seg.start_ea]
                    break
            payload.append(byte or 0)
            if byte is not None:
                mask[offset >> 3] |= 1 << (offset & 7)
        # An all-zero mask means "nothing here is defined" (a real BSS
        # read); only a genuine read failure returns None.
        return bytes(payload), bytes(mask)

    ida_bytes.get_bytes_and_mask.side_effect = _get_bytes_and_mask


class TestHostDispatch(unittest.TestCase):
    """The IDA sections are dispatched; the CPU phase is not.

    Runs in a subprocess worker because Unicorn's ctypes state is not safe
    to create in a pytest process that also serves the other suites.
    """

    @unittest.skipUnless(HAVE_UNICORN, "unicorn not installed in this environment")
    def test_snapshot_runs_on_the_dispatcher_thread(self) -> None:
        plan = {
            "tool": "emulate_code",
            "payload": {
                "start_address": "0x401000",
                "stop_address": "0x401003",
                "registers": {"eax": 41},
            },
            "bits": 64,
            "segments": [_code_page(bytes.fromhex("83c001"), perm=RWX)],
            "scenario": "dispatch",
        }
        out = run_in_subprocess("tests.test_emulation_subprocess", json.dumps(plan), timeout=60.0)
        self.assertEqual(out["snapshot_thread"], "dispatched")
        self.assertEqual(out["worker_thread"], "registry-worker")
        # Two host dispatches: the architecture probe and the memory snapshot.
        self.assertEqual(len(out["dispatcher_calls"]), 2)
        self.assertEqual(out["cpu_thread"], "registry-worker")
        self.assertEqual(out["final_registers"]["eax"], 42)


class TestRegistryRejection(unittest.TestCase):
    """Argument validation must also hold through the registry entry point."""

    @unittest.skipUnless(HAVE_UNICORN, "unicorn not installed in this environment")
    def test_empty_registers_rejected_through_the_registry(self) -> None:
        _set_bits(64)
        sys.modules["ida_ida"].inf_get_procname.return_value = "metapc"
        _install_segments([_code_page(bytes.fromhex("83c001"), perm=RWX)])
        from rikugan.tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register(emu.emulate_code._tool_definition)
        # `emulation.ida_ida` is bound at import time; the advanced registry
        # may have replaced the module since, so pin the handler's own view.
        self.addCleanup(setattr, emu, "ida_ida", sys.modules["ida_ida"])
        sys.modules["ida_ida"].inf_get_procname.return_value = "metapc"
        emu.ida_ida.inf_get_procname.return_value = "metapc"
        emu.ida_ida.inf_get_app_bitness.return_value = 64
        with self.assertRaises(ToolError) as ctx:
            registry.execute(
                "emulate_code",
                {"start_address": "0x401000", "stop_address": "0x401003", "registers": {}},
            )
        self.assertIn("at least one initial register", str(ctx.exception))


class TestMissingUnicorn(unittest.TestCase):
    def test_missing_sdk_raises_an_actionable_error(self) -> None:
        original = sys.modules.pop("unicorn", None)
        sys.modules["unicorn"] = None  # force ImportError
        try:
            with self.assertRaises(ToolError) as ctx:
                emu._load_unicorn()
            self.assertIn("Unicorn", str(ctx.exception))
        finally:
            if original is not None:
                sys.modules["unicorn"] = original
            else:
                sys.modules.pop("unicorn", None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
