# Emulation P0 + P1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development. Work only in the isolated worktree. The primary agent owns integration and verification; workers skip build/lint/tests/formatters while edits are in flight.

**Goal:** Make bounded x86/x64 emulation trustworthy and support explicit runtime inputs, ABI-aware function execution, dirty-memory string extraction, compact output, and real deadlines/cancellation.

**Architecture:** Retain Unicorn. Split the existing large emulation module only along real independent boundaries: IDA memory snapshot, pure result formatting, and the CPU runner/tool handlers. Tool execution context carries host dispatch and cancellation into the worker; only architecture/memory snapshot touches IDA APIs on the host thread.

**Tech Stack:** Python >=3.11, Unicorn >=2.1.0,<3 (locked 2.1.4), stdlib contextvars/threading, existing pytest/subprocess harness.

**Spec:** User-approved P0 + P1 research recommendations in this conversation; this document defines exact implementation contracts. P2 (trace/batch/API models/ARM) is excluded by the user's scope selection.

## Global Constraints

- Workspace: `D:/Program Files/IDAdata/IDAUSR/plugins/rikugan/.superpowers/worktrees/emulation-p0p1`, branch `fix/emulation-p0p1`.
- Do not commit, merge, push, change global config, install dependencies, or edit the original checkout.
- No new dependency, arbitrary callbacks, script execution, syscall/API simulation, or IDB mutations.
- Every IDA API call uses the existing host-thread dispatcher. Unicorn execution stays on the registry worker in normal UI execution.
- Keep existing tool names and old range-mode calls valid; no obsolete helper aliases/re-exports. Migrate tests/imports to the owning modules.
- Stack: 1 MiB; aggregate mapped memory including stack: 16 MiB. Maximum 1,000,000 instructions, default 100,000. Each capture <=4096 bytes; <=16 captures. Synthetic buffers share aggregate cap and cannot overlap IDB pages. Stack initialization is permitted within the existing synthetic stack, without widening permissions.
- Scratch permissions: `r` or `rw`, default `rw`; no executable scratch. IDB permissions never widened. Conflicting permissions on a shared page fail explicitly rather than OR-ing into RWX.
- Initial IDB bytes must be read exactly, by segment intersection, not from an entire segment or a page-relative segment offset. Missing bytes fail explicitly unless IDA identifies genuine BSS zero-fill.
- Runtime timeout parameter: `timeout_seconds`, default 5.0, finite and >0, hard cap 20.0; native Unicorn timeout uses microseconds. Engine deadline also accounts for snapshot/setup and registry context deadline. Return partial state with `timeout` or `cancelled` rather than claiming completion.
- Output budget <=7500 characters, preserving status/PC/capture summaries before verbose details; all omissions and string/write truncation explicit.
- Permanent tests assert consumer-visible outcomes, boundary validation, isolation, and stop conditions, not source text, forwarding, or incidental wording.

## Shared Interfaces

Task 2 owns `rikugan/ida/tools/emulation_types.py`. Create these definitions early and inform the other workers when available. Use absolute imports.

```python
@dataclass(frozen=True)
class ArchMode:
    label: str
    arch_const: int
    mode_const: int
    ptr_size: int
    ip_reg: str
    sp_reg: str
    flags_reg: str
    stack_base: int

@dataclass(frozen=True)
class CaptureRequest:
    address: int
    size: int
    label: str

@dataclass(frozen=True)
class MemoryBuffer:
    address: int
    size: int
    data: bytes
    permissions: int

@dataclass(frozen=True)
class MemoryRegion:
    address: int
    size: int
    permissions: int
    data: bytes
    synthetic: bool = False

@dataclass(frozen=True)
class MemorySnapshot:
    regions: list[MemoryRegion]
    valid_ranges: list[tuple[int, int, int]]  # start, exclusive end, R/W/X mask
    stack_base: int
    stack_top: int
    total_bytes: int

@dataclass(frozen=True)
class StringCandidate:
    address: int
    encoding: str
    text: str
    terminated: bool

@dataclass
class EmulationResult:
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
```

Task 1 provides:
```python
def snapshot_memory(*, arch: ArchMode, start_address: int, stop_address: int,
                    extra_ranges: Sequence[tuple[int, int]],
                    captures: Sequence[CaptureRequest],
                    memory_buffers: Sequence[MemoryBuffer] = (),
                    code_ranges: Sequence[tuple[int, int]] = ()) -> MemorySnapshot: ...
```
Ranges use `(address, size)` except `valid_ranges`. Snapshot maps required pages and reads available IDB segment intersections on those pages. Synthetic stack is mapped once. Stack top remains `stack_base + 1 MiB - 0x100`; ABI runner derives aligned entry SP from it.

Task 3 provides:
```python
def decode_string_candidates(data: bytes) -> dict[str, Any]: ...
def extract_strings(address: int, data: bytes, *, min_length: int = 4,
                    max_candidates: int = 64) -> list[StringCandidate]: ...
def format_result(result: EmulationResult) -> str: ...
```
Candidate metadata keeps `ascii`, `utf8`, `utf16le`, `raw_length`, `has_nul_terminator` and adds `ascii_terminated`, `utf8_terminated`, `utf16le_terminated`. Wide NUL must start at an even byte offset. Do not manufacture `?`-replacement strings as discoveries; ASCII/UTF-8/wide discoveries must be valid printable candidates. Nonterminated wide input is decoded from its complete even-length bytes, not an ASCII prefix.

Task 4 provides in `rikugan/tools/execution.py`:
```python
@dataclass(frozen=True)
class ToolExecutionContext:
    dispatch_wrapper: Callable | None = None
    cancel_event: threading.Event | None = None
    deadline: float | None = None

def get_execution_context() -> ToolExecutionContext: ...
def run_on_host_thread(func: Callable, *args: Any, **kwargs: Any) -> Any: ...
```
A ContextVar/context manager (worker-owned name) installs/restores this context around handler execution. `run_on_host_thread` calls the supplied dispatcher; when already on the main thread it calls directly to avoid dispatch deadlocks. With no registry dispatcher, use the existing `idasync` seam for real UI IDA. Headless callers must use their registered dispatcher. Do not leak context between concurrent calls.

ToolDefinition and `@tool` gain `main_thread: bool = True`. Registry skips outer host dispatch only for `main_thread=False`; emulation tools opt out and explicitly dispatch their snapshot. Registry execution methods gain keyword-only `cancel_event: threading.Event | None = None`. AgentLoop passes `self._cancelled` to its actual tool execution call. Preserve default behavior for all other tools.

## Task 1: Memory fidelity and bounded snapshot

**Owner/files:** Create `rikugan/ida/tools/emulation_memory.py` and `tests/ida/test_emulation_memory.py`. Do not edit emulation.py/types/output/framework files.

- [ ] Write focused behavioral regressions before implementation: read extra buffer `78 56 34 12` at 0x501000; entry at 0x402000 inside segment beginning 0x401000; non-page-aligned segment; adjacent RX/RW pages; stack capture; synthetic input bytes; synthetic overlap and missing IDB bytes.
- [ ] Implement `snapshot_memory` and its bounded address/page helpers. Lazy safe IDA imports, architecture-width address validation, positive strict sizes (reject bool), page-level permissions kept separate. Read requested page intersections using `ida_bytes.get_bytes(intersection_start, intersection_size)`.
- [ ] A capture in the synthetic stack must reuse the stack mapping. Buffers within stack initialize bytes without a second map; validate RW stack permissions. Synthetic regions can share a page only with compatible nonoverlapping valid bytes and identical permissions; reject overlaps and inconsistent page permissions. Reject overlap with IDB pages.
- [ ] Explicitly reject conflicting IDB permissions on one page, huge inputs before allocating/reading, holes in requested code/input/capture ranges, short/missing data, negative/overflow addresses, duplicate ambiguous buffers. Do not silently zero-fill a loader failure.
- [ ] Return exact valid segment/synthetic byte intervals so the runner can reject accesses to page padding. Region payload data uses the mapped region base, with explicit zero padding only outside valid bytes or synthetic zero-filled buffers.
- [ ] Self-review and report interfaces, changes, concerns. Skip check commands; primary verifies after the edit wave.

**Acceptance:** Requested bytes preserve VA and are identical to IDB bytes, independent of page/segment offsets. Adding RW data cannot grant write permission to RX code. Stack captures do not double-map. Total allocated pages including stack are capped.

## Task 2: CPU correctness, inputs, ABI and partial results

**Owner/files:** Modify `rikugan/ida/tools/emulation.py`, `tests/ida/test_emulation.py`, `tests/test_emulation_subprocess.py`; create `rikugan/ida/tools/emulation_types.py` and `tests/ida/test_emulation_execution.py`. Do not edit memory/output/framework files. Sole integration owner for the emulation tool surface.

- [ ] Create behavior regressions first. Cover x64 `eax=41; add eax,1 ->42`, carry `adc`, exact one/two-instruction budgets, stop PC at exclusive end, mid-range syscall, permission-vs-unmapped status, `ret` function completion, x86 stack args, win64/sysv64 ABI args, stack-relative capture, timeout/cancel partial state, and >64 writes producing a discovered string.
- [ ] Create shared dataclasses early, notify memory/output workers. Remove old definitions and unused helper implementations from emulation.py; migrate imports/tests, no compatibility re-exports. Remove incidental wording/implementation tests rather than re-pin them.
- [ ] Extend BOTH tools with `memory_buffers`, `execution_mode="range"`, `calling_convention=""`, `arguments`, `code_ranges`, `timeout_seconds=5.0`, `collect_strings=False`. All optional lists use immutable empty defaults. Buffer shape `{address, size, data_hex, permissions}`; permissions defaults `rw`. Strictly validate hex length<=size and all caps before decoding/allocation. `code_ranges` shape `{address,size}` is explicit executable IDB allowlist.
- [ ] `capture_ranges` accepts exactly one of address or signed `stack_offset`, plus size/label. Resolve stack offsets relative to initial SP after ABI setup. Unique labels, <=16 captures. `resolve_emulated_string` allows exactly one of `output_address` (default empty string) or optional `output_stack_offset` to be supplied.
- [ ] Calling convention is required explicitly in function mode: x86 cdecl/stdcall/fastcall, x64 win64/sysv64. ABI arguments accept integer/hex string values. Reject mismatched architecture/convention, range-mode args, ambiguous conflicting argument-register values, stack bounds overflow. x86 fastcall uses ecx/edx first; remaining args start at SP+ptr. win64 uses rcx/rdx/r8/r9, initial SP%16=8, 32-byte shadow space, extra args at SP+40. sysv64 uses rdi/rsi/rdx/rcx/r8/r9, SP%16=8, extra args at SP+8. Return sentinel `stop_address` only in function mode. Do not automatically skip calls.
- [ ] Obtain context, validate arguments, then run architecture+memory snapshot through `run_on_host_thread`. Execute Unicorn on caller worker; no IDA references or segment objects retained in CPU phase. Capture initial normalized registers. Initialize native mode registers once; supplied aliases must not be overwritten by defaults. Reject conflicting alias values rather than depend on dict order; preserve explicit flags, reject 64-only registers on x86. Include PC in final state.
- [ ] Correct count budget before the next instruction, actual final PC, reached-stop precedence, enum membership classification, out-of-range fetch classification, and reject unsupported syscall/sysenter/int instructions anywhere including prefixed instructions before they execute. Bound each instruction's bytes to allowed executable ranges. Valid-range hooks deny reads/writes into padding, never recover by mapping zero pages.
- [ ] Real native deadline in microseconds plus monotonic/cancel checks during setup and code hooks. Clamp to earliest context/local deadline. Inspect native timeout completion; a native early return is not `completed`. Return registers/captures/discoveries for timeout/cancel. Keep registry timeout headroom.
- [ ] Track dirty page addresses independently of truncated write log. For collect_strings compare initial/final bytes of dirty pages, extract candidates from modified neighborhoods (include preceding/following unchanged string bytes, but do not report unchanged strings). Cap discovery to 64 candidates and report truncation. Do not allocate a full memory snapshot every instruction or trust only the first 64 writes.
- [ ] Upgrade subprocess worker with explicit bitness and per-segment permissions; expose CPU result values via consumer-readable output or structured runner scenario. Existing IDB mutation guards remain. Test missing Unicorn path and schema still advertising tools.
- [ ] Integrate memory/output/framework interfaces without editing their files. Report concerns; skip build/lint/tests/formatters mid-flight.

**Acceptance examples:**
```python
# x64 aliases
assert result.final_registers["eax"] == 42
# range completion and exact budget
assert (result.status, result.stop_pc, result.instruction_count) == ("completed", 0x401006, 2)
# no syscall execution
assert result.status == "unsupported_instruction"
# dirty-memory extraction beyond write-event preview cap
assert any(s.text == "HELLO" for s in result.discovered_strings)
```

## Task 3: Encoding and compact useful output

**Owner/files:** Create `rikugan/ida/tools/emulation_output.py`, `tests/ida/test_emulation_output.py`. Do not edit existing engine/tests/framework files.

- [ ] Write regressions for aligned/unaligned UTF-16 NULs, valid UTF-8, malformed payloads, and capture/discovery summaries surviving the real registry result cap for a 4096-byte capture.
- [ ] Implement the three shared functions. Decode terminators independently by encoding; report raw length; wide data without aligned terminator must not be truncated by interior single-byte NUL.
- [ ] Discover printable ASCII, UTF-8, UTF-16LE strings with addresses, encoding and termination metadata; min_length measured in characters. Honor max_candidates; deduplicate identical candidates.
- [ ] Format status/reason/entry/stop/count then discovered/captured summaries, changed registers, mapping/write summaries and bounded hex previews. Ensure <=7500 chars for many captures/strings; every omission is visible. Keep summaries before details, text escaped so embedded newlines/control characters do not impersonate result fields. Hex preview <=64 bytes/capture by default; still report total raw size. Show write_event_count and omitted events accurately.
- [ ] No global registry/UI cap increase; no persistence mechanism or extra raw-output tool. Report changes/concerns and skip check commands until primary verification.

**Acceptance:** `ABCD` extracted from a default-sized buffer reaches the agent instead of being lost behind the hex dump. UTF-16 `41 00 00 42 43 00 44 00` has no aligned terminator and must not report only `A` as the wide candidate.

## Task 4: Worker execution context and cancellation routing

**Owner/files:** Create `rikugan/tools/execution.py`, `tests/tools/test_tool_execution_context.py`; modify `rikugan/tools/base.py`, `rikugan/tools/registry.py`, `rikugan/agent/loop.py`. Do not edit emulation modules/tests/docs.

- [ ] Write deterministic tests first for actual host/worker separation using a queued dispatcher, context isolation between overlapping registry executions, cancellation reaching an executing worker, context cleanup on exception, and unchanged dispatch of ordinary tools. Use real threading/events/registry, not assertions that mocks echoed arguments.
- [ ] Implement shared context and `main_thread` metadata; new field appended with default True to avoid positional callers changing meaning. Propagate decorator metadata correctly.
- [ ] Add keyword-only cancel_event to registry execute/execute_coerced/execute_current_thread. Install context INSIDE the executor worker (ContextVars do not automatically cross thread boundaries), and reset in finally. Record registry timeout deadline before submission. Do not bypass existing mutation serialization/cache/error handling.
- [ ] Only main_thread=False handlers skip outer dispatcher and explicitly dispatch their host sections. `run_on_host_thread` must avoid re-dispatch when on main thread, preserve UI/headless dispatch semantics, and never leak a cancelled context to the next call. Current-thread calls retain their documented direct behavior and provide context without submitting work that waits on a blocked host thread.
- [ ] AgentLoop passes its existing per-run `_cancelled` event at actual tool execution. Do not add Event parameters to LLM tool schemas or auto-approve scripts.
- [ ] Update all affected exported references; LSP references failed in primary (`Unknown request`), so use exact symbol searches as fallback. Do not broad-refactor other registry behaviors.
- [ ] Self-review, report tests added/contract changes/concerns; skip build/lint/tests/formatters mid-flight.

**Acceptance:** A main_thread=False handler snapshots on the host queue, continues CPU work on the worker, notices cancellation, and leaves the next invocation unaffected. Ordinary tools still run under the previous host dispatcher.

## Primary Integration and Verification

- [ ] Preflight shared contracts and exclusive ownership. Memory/output consume only types; execution context is independent. Task 2 is the integration owner, so no concurrent edits to its files.
- [ ] Baseline: original emulation + registry suite observed 38 passed in the isolated checkout before edits. Confirm existing failing-before CPU probes against baseline; exercise new optional parameters to prove absence before implementation.
- [ ] Await worker reports and inspect critical source/interfaces, then review package to a reviewer (spec + quality). No commits; review is the uncommitted diff and new source files.
- [ ] Run project interpreter from original `.venv` with worktree cwd. Format changed Python files once; run scoped ruff and pytest for emulation, execution context, registry and agent approval/cancellation paths. Run the broader suite if those pass; investigate failures and distinguish unrelated baseline issues without silently skipping tests.
- [ ] Smoke actual public tools with real Unicorn in fresh subprocesses: cross-segment XOR decoder -> plaintext, scratch input -> known hash/value, x86 and x64 function ABI -> expected return, stack strings without known output, mid-range syscall rejection, read-only write rejection, exact budgets, timeout/cancellation -> partial state, registry truncation preservation.
- [ ] Exercise live IDA surface if tools can access an existing database without changing it; otherwise report that GUI interaction is unverified. No database mutations or arbitrary script approval bypass.
- [ ] After smoke proof, update existing emulator/deobfuscation docs and Unreleased changelog to match exact shipped APIs. Do not change unrelated release notes or versions.
- [ ] Final worktree path, plan path, checks actually run and exact limitations. Leave changes uncommitted in worktree; no merge/push.
