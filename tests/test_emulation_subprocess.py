"""Subprocess-friendly emulation test that returns JSON-serialised results.

Launched via ``tests.subprocess_test_worker.run_in_subprocess``. Each
subprocess boots a fresh interpreter so Unicorn's ctypes state cannot
leak between scenarios. The payload is forwarded via stdin (Windows
rejects argv-based JSON once the plan exceeds ~8 KB).

Payload shape::

    {
        "tool": "emulate_code" | "resolve_emulated_string",
        "payload": {...},                       # forwarded to the tool
        "bits": 32 | 64,                        # explicit IDB bitness
        "segments": [{"start": 4198400, "end": 4198600, "perm": 5, "data_hex": "..."}],
        "scenario": "structured"                # return the EmulationResult fields
    }

``segments`` are explicit per-segment permissions, so a read-only page
can be exercised for real instead of relying on a blanket RWX mapping.
``scenario: "structured"`` returns the CPU result values (status, stop
PC, instruction count, registers, captures, discovered strings) instead
of parsing the rendered text, which is what the behaviour regressions
assert on.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass


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
    bits = int(plan.get("bits", 32))
    sys.modules["ida_ida"].inf_get_app_bitness.return_value = bits
    sys.modules["ida_ida"].inf_is_64bit.return_value = bits == 64
    sys.modules["ida_ida"].inf_is_32bit.return_value = bits == 32

    @dataclass
    class _Seg:
        start_ea: int
        end_ea: int
        perm: int
        sclass: str = "CODE"

    segments: list[tuple[_Seg, bytes]] = []
    for spec in plan.get("segments", []):
        start = int(spec["start"])
        end = int(spec["end"]) if "end" in spec else start + int(spec["size"])
        payload = bytes.fromhex(spec.get("data_hex", ""))
        if len(payload) < end - start:
            payload = payload + b"\x90" * ((end - start) - len(payload))
        segments.append((_Seg(start, end, int(spec.get("perm", 5)), spec.get("sclass", "CODE")), payload))
    segments.sort(key=lambda item: item[0].start_ea)

    def _getseg(ea: int):
        return next((seg for seg, _ in segments if seg.start_ea <= ea < seg.end_ea), None)

    def _getnseg(index: int):
        return segments[index][0] if 0 <= index < len(segments) else None

    def _get_segm_class(seg) -> str:
        return getattr(seg, "sclass", "CODE")

    def _get_bytes_and_mask(ea: int, size: int):
        # Packed LSB-first definedness: ceil(size / 8) mask bytes, one bit
        # per payload byte. A byte no segment covers stays undefined.
        payload = bytearray()
        mask = bytearray((size + 7) // 8)
        for offset in range(size):
            address = ea + offset
            byte = None
            for seg, data in segments:
                if seg.start_ea <= address < seg.end_ea:
                    byte = data[address - seg.start_ea]
                    break
            payload.append(byte or 0)
            if byte is not None:
                mask[offset >> 3] |= 1 << (offset & 7)
        # An all-zero mask means "nothing here is defined" (a real BSS
        # read); only a genuine read failure returns None.
        return bytes(payload), bytes(mask)

    sys.modules["ida_segment"].getseg.side_effect = _getseg
    sys.modules["ida_segment"].get_segm_qty.return_value = len(segments)
    sys.modules["ida_segment"].getnseg.side_effect = _getnseg
    sys.modules["ida_segment"].get_segm_class.side_effect = _get_segm_class
    sys.modules["ida_bytes"].get_bytes_and_mask.side_effect = _get_bytes_and_mask

    # The emulation tools are read-only: any IDB mutation is a hard failure.
    def _deny_mutation(*args, **kwargs):
        raise AssertionError("emulation test attempted an IDB mutation")

    for name in ("patch_byte", "patch_bytes", "put_byte", "put_bytes"):
        setattr(sys.modules["ida_bytes"], name, _deny_mutation)
    idc = sys.modules["idc"]
    idc.set_cmt = _deny_mutation
    idc.set_name = _deny_mutation
    idc.create_strlit = _deny_mutation

    from rikugan.ida.tools import emulation as emu

    tool_name = plan["tool"]
    payload = dict(plan["payload"])

    if plan.get("scenario") == "dispatch":
        # The two IDA sections must run on the dispatcher thread while the
        # CPU phase stays on the worker that called them.
        import threading

        from rikugan.tools.execution import ToolExecutionContext, tool_execution_context

        marker = "dispatched"
        worker_name: list[str] = []
        snapshot_thread: list[str] = []
        dispatcher_calls: list[str] = []
        cpu_thread: list[str] = []

        def _dispatcher(func):
            # Stands in for the host queue: the work is handed to a fresh
            # thread, exactly like a real host dispatcher would.
            def _inner(*args, **kwargs):
                dispatcher_calls.append(threading.current_thread().name)
                box: dict = {}

                def _host():
                    box["thread"] = threading.current_thread().name
                    box["value"] = func(*args, **kwargs)

                host = threading.Thread(target=_host, name="host-thread")
                host.start()
                host.join()
                return box["value"]

            return _inner

        real_dispatch = emu.run_on_host_thread

        def _forced_dispatch(func, *args, **kwargs):
            return _dispatcher(func)(*args, **kwargs)

        real_run_emulation = emu.run_emulation

        def _record_cpu(**kwargs):
            cpu_thread.append(threading.current_thread().name)
            return real_run_emulation(**kwargs)

        result_box: dict = {}

        def _record_result(value):
            result_box["value"] = value
            return value

        def _worker():
            worker_name.append(threading.current_thread().name)
            emu.run_emulation = _record_cpu
            with tool_execution_context(ToolExecutionContext(dispatch_wrapper=_dispatcher)):
                result = emu._run_tool(
                    tool_name=tool_name,
                    start=emu._coerce_addr(payload["start_address"], ctx="start_address"),
                    stop=emu._coerce_addr(payload["stop_address"], ctx="stop_address"),
                    registers=payload.get("registers", {}),
                    memory_ranges=(),
                    code_ranges=(),
                    memory_buffers=(),
                    capture_specs=[],
                    implicit_capture=None,
                    execution_mode="range",
                    calling_convention="",
                    arguments=(),
                    instruction_limit=1000,
                    timeout_seconds=5.0,
                    collect_strings=False,
                )
                _record_result(result)
            emu.run_emulation = real_run_emulation

        real_snapshot = emu.snapshot_memory

        def _spy(**kwargs):
            on_host = threading.current_thread() is not threading.main_thread()
            snapshot_thread.append("dispatched" if on_host else "main")
            return real_snapshot(**kwargs)

        emu.snapshot_memory = _spy
        emu.run_on_host_thread = _forced_dispatch
        try:
            worker = threading.Thread(target=_worker, name="registry-worker")
            worker.start()
            worker.join()
        finally:
            emu.snapshot_memory = real_snapshot
            emu.run_on_host_thread = real_dispatch
        print(
            json.dumps(
                {
                    "snapshot_thread": snapshot_thread[0] if snapshot_thread else marker,
                    "worker_thread": worker_name[0] if worker_name else "",
                    "cpu_thread": cpu_thread[0] if cpu_thread else "",
                    "dispatcher_calls": dispatcher_calls,
                    **_result_payload(result_box["value"]),
                }
            )
        )
        return 0

    if plan.get("scenario") == "structured":
        # Run the same front-end the tools use, but keep the structured
        # result so regressions assert on values, not on rendered prose.
        try:
            result = emu._run_tool(
                tool_name=tool_name,
                start=emu._coerce_addr(payload["start_address"], ctx=f"{tool_name}: start_address"),
                stop=emu._coerce_addr(payload["stop_address"], ctx=f"{tool_name}: stop_address"),
                registers=payload.get("registers", {}),
                memory_ranges=payload.get("memory_ranges", ()),
                code_ranges=payload.get("code_ranges", ()),
                memory_buffers=payload.get("memory_buffers", ()),
                capture_specs=emu._normalize_capture_specs(
                    payload.get("capture_ranges", ()), tool_name=tool_name, default_label="output"
                ),
                implicit_capture=_implicit_capture(tool_name, payload),
                execution_mode=payload.get("execution_mode", "range"),
                calling_convention=payload.get("calling_convention", ""),
                arguments=payload.get("arguments", ()),
                instruction_limit=payload.get("instruction_limit", 100_000),
                timeout_seconds=payload.get("timeout_seconds", 5.0),
                collect_strings=payload.get("collect_strings", False),
            )
        except Exception as e:
            print(json.dumps({"error": str(e), "exception": type(e).__name__}))
            return 0
        print(json.dumps(_result_payload(result)))
        return 0

    if plan.get("registry"):
        import threading

        from rikugan.tools.registry import ToolRegistry

        handler = emu.emulate_code if tool_name == "emulate_code" else emu.resolve_emulated_string
        registry = ToolRegistry()
        registry.register(handler._tool_definition)
        cancel = threading.Event()
        if plan.get("cancel_before"):
            cancel.set()
        timer = None
        if plan.get("cancel_after"):
            timer = threading.Timer(plan["cancel_after"], cancel.set)
            timer.start()
        try:
            text = registry.execute(tool_name, payload, cancel_event=cancel)
        finally:
            if timer is not None:
                timer.cancel()
    else:
        try:
            if tool_name == "emulate_code":
                text = emu.emulate_code(**payload)
            elif tool_name == "resolve_emulated_string":
                text = emu.resolve_emulated_string(**payload)
            else:
                print(json.dumps({"error": f"unknown tool {tool_name!r}"}))
                return 0
        except Exception as e:
            print(json.dumps({"error": str(e), "exception": type(e).__name__}))
            return 0

    out: dict[str, object] = {"text": text}
    for line in text.splitlines():
        if line.startswith("Status:") and "status" not in out:
            out["status"] = line.split(":", 1)[1].strip()
        elif line.startswith("Stop PC:") and "stop_pc" not in out:
            out["stop_pc"] = int(line.split("0x", 1)[1].strip(), 16)
        elif line.startswith("Instructions executed:") and "instruction_count" not in out:
            out["instruction_count"] = int(line.split(":", 1)[1].strip())
    print(json.dumps(out))
    return 0


def _implicit_capture(tool_name: str, payload: dict) -> dict[str, object] | None:
    """The implicit ``output`` capture resolve_emulated_string adds."""

    if tool_name != "resolve_emulated_string":
        return None
    size = int(payload.get("max_output_size", 4096))
    if payload.get("output_stack_offset") is not None:
        return {"size": size, "stack_offset": int(payload["output_stack_offset"])}
    address = payload.get("output_address") or ""
    if not address:
        return None
    value = int(address, 0) if isinstance(address, str) else int(address)
    return {"size": size, "address": value}


def _result_payload(result) -> dict[str, object]:
    return {
        "status": result.status,
        "reason": result.reason,
        "entry_pc": result.entry_pc,
        "stop_pc": result.stop_pc,
        "instruction_count": result.instruction_count,
        "architecture": result.architecture,
        "initial_registers": {k: v for k, v in result.initial_registers.items()},
        "final_registers": {k: v for k, v in result.final_registers.items()},
        "write_event_count": result.write_event_count,
        "writes": result.writes,
        "captures": {label: data.hex() for label, data in result.captures.items()},
        "captured_strings": result.captured_strings,
        "discovered_strings": [
            {
                "address": s.address,
                "encoding": s.encoding,
                "text": s.text,
                "terminated": s.terminated,
            }
            for s in result.discovered_strings
        ],
        "discovery_truncated": result.discovery_truncated,
    }


if __name__ == "__main__":
    raise SystemExit(_main())
