"""Subprocess-friendly emulation test that returns JSON-serialised results.

Launched via ``tests.subprocess_test_worker.run_in_subprocess``. Each
subprocess boots a fresh interpreter so Unicorn's ctypes state cannot
leak between scenarios. The payload is forwarded via stdin (Windows
rejects argv-based JSON once the plan exceeds ~8 KB).

Payload shape::

    {
        "tool": "emulate_code" | "resolve_emulated_string",
        "payload": {...}                       # forwarded to the tool,
        "setup_pages": [(start, end, [bytes]), ...]  # simulated IDB regions
    }
"""

from __future__ import annotations

import json
import os
import sys


def _main() -> int:
    try:
        plan = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        print(f"invalid json: {e}", file=sys.stderr)
        return 2

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo_root)

    from tests.mocks.ida_mock import install_ida_mocks

    install_ida_mocks()
    sys.modules["ida_ida"].inf_get_procname.return_value = "metapc"

    registers = plan["payload"].get("registers", {})
    is_64 = any(k in registers for k in {"rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp", "r8", "r9"}) or bool(
        registers and any(str(k).startswith("r") and k != "register" for k in registers)
    )
    sys.modules["ida_ida"].inf_is_64bit.return_value = bool(is_64)
    sys.modules["ida_ida"].inf_is_32bit.return_value = not is_64
    sys.modules["ida_ida"].inf_get_app_bitness.return_value = 64 if is_64 else 32

    from dataclasses import dataclass
    from unittest.mock import MagicMock

    @dataclass
    class _Seg:
        start_ea: int
        end_ea: int
        perm: int = 5  # R|X

    pages = [(s, e, bytes(b)) for s, e, b in plan.get("setup_pages", [])]
    perm = plan.get("perm", 7)  # RWX by default so XOR-decode tests can write

    def _getseg(ea: int):
        for start, end, _payload in pages:
            if start <= ea < end:
                return _Seg(start, end, perm)
        return None

    def _get_bytes(ea: int, size: int):
        for start, end, payload in pages:
            if start <= ea < end and ea + size <= end:
                return payload[ea - start : ea - start + size]
        return None

    sys.modules["ida_segment"].getseg.side_effect = _getseg
    sys.modules["ida_bytes"].get_bytes.side_effect = _get_bytes

    for name in ("patch_byte", "patch_bytes", "put_byte", "put_bytes"):
        mock = MagicMock(name=name)
        sys.modules["ida_bytes"].__dict__[name] = mock
    idc = sys.modules["idc"]
    idc.set_cmt = MagicMock(name="set_cmt")
    idc.set_name = MagicMock(name="set_name")
    idc.create_strlit = MagicMock(name="create_strlit")

    import rikugan.ida.tools.emulation as emu

    tool_name = plan["tool"]
    payload = dict(plan["payload"])

    try:
        if tool_name == "emulate_code":
            text = emu.emulate_code(**payload)
        elif tool_name == "resolve_emulated_string":
            text = emu.resolve_emulated_string(**payload)
        else:
            print(json.dumps({"error": f"unknown tool {tool_name!r}"}))
            return 0
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 0

    status = "unknown"
    for line in text.splitlines():
        if line.startswith("Status:"):
            status = line.split(":", 1)[1].strip()
            break
    print(json.dumps({"text": text, "status": status}))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
