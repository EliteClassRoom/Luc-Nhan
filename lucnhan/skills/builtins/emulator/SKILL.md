---
name: Emulator
description: Use when a self-contained x86/x64 decoder, crypto helper, opaque predicate, or stack-string routine needs bounded execution from the IDB.
tags:
  - emulation
  - unicorn
  - deobfuscation
  - string-decryption
  - analysis
allowed_tools:
  - emulate_code
  - resolve_emulated_string
  - decompile_function
  - read_disassembly
  - read_function_disassembly
  - get_function_by_address
  - xrefs_to
  - list_segments
  - set_comment
  - rename_function
triggers:
  - emulate
  - emulator
  - unicorn
  - decode stub
  - trace execution
  - resolve string
---

# Emulator Mode

Bounded, read-only CPU emulation for self-contained IDA code ranges.
**Never modifies the IDB. Never runs the target binary. Never spawns processes.**

The engine is a per-call Unicorn instance. IDB pages retain their source
addresses, bytes and permissions. The 1 MiB synthetic stack and explicitly
declared `rw` scratch buffers are writable; read-only IDB pages stay read-only.

## When to Use This Skill

Activate this skill when the user asks to:

- **Decode a string** whose decoder is self-contained (no external APIs or
  syscalls; internal helpers can be declared in `code_ranges`).
- **Trace a custom crypto routine** to recover a key, IV, or output buffer.
- **Reconstruct a control-flow-flattened path** by emulating a single
  dispatcher iteration with known state.
- **Resolve an opaque predicate** by running it with concrete inputs.
- **Execute any small code range** where static analysis is ambiguous but
  dynamic execution within a strict boundary would be conclusive.

## When NOT to Use This Skill

Skip emulation (and tell the user why) when the target routine:

- Calls external APIs (Win32, libc, custom imports) — no API stubs are
  provided; execution will stop with `range_exit` or `unmapped_memory`.
- Issues syscalls or software interrupts — each instruction is checked before
  execution and reports `unsupported_instruction`, even mid-range.
- Branches outside the main range and explicit `code_ranges` — execution stops
  with `range_exit` and partial state.
- Depends on captured/unmodeled state (heap pointers, TLS, global
  mutexes) that cannot be reconstructed from the IDB alone.

In those cases, fall back to `execute_python` reimplementation or the
broader `/deobfuscation` skill (which covers optimizer-based approaches).

## The Two Tools

### `emulate_code` — General-Purpose Bounded Execution

Use for arbitrary instruction ranges: decoder loops, custom crypto stubs,
control-flow-flattening reconstruction, opaque-predicate resolution.

Key parameters:
- `start_address` (inclusive hex) — first instruction to execute.
- `stop_address` (**exclusive** hex) — bounds the main code range. In range mode,
  execution stops before it. In function mode it also serves as the synthetic
  return address, so a `ret` reaching it reports `completed`.
- `registers` — explicit initial CPU state. Required and non-empty in range
  mode; `{}` is allowed in function mode. Register aliases must agree.
  `eip`/`rip` always come from `start_address`; `eflags`/`rflags` may be supplied.
- `memory_ranges` — extra IDB input/key/table ranges:
  `{"address": "0x...", "size": N}`. No permissions override is available.
- `memory_buffers` — scratch/input bytes not present in the IDB:
  `{"address": "0x...", "size": N, "data_hex": "...", "permissions": "rw"}`.
  Permissions are `r` or `rw` (default `rw`), never executable. Buffers cannot
  overlap IDB pages or each other; undeclared padding is not valid input.
- `capture_ranges` — up to 16 labeled output windows, each 1..4096 bytes:
  `{"address": "0x...", "size": N, "label": "decoded"}` or
  `{"stack_offset": -32, "size": N, "label": "local"}`. Supply exactly one
  address form; stack offsets are relative to the initial, ABI-adjusted SP.
- `execution_mode` — `range` by default; `function` sets up a real call frame
  with a return sentinel. Function mode requires `calling_convention`:
  x86 `cdecl`/`stdcall`/`fastcall`, x64 `win64`/`sysv64`.
- `arguments` — function-mode argument word values in declaration order.
  Use integers or hex bit patterns; do not infer the calling convention.
  Win64 reserves shadow space; both x64 ABIs align entry RSP to 8 modulo 16.
- `code_ranges` — explicit executable IDB helper ranges (`address`, `size`).
  Mapping data via `memory_ranges` does not authorize executing it.
- `instruction_limit` — default 100_000, hard cap 1_000_000.
- `timeout_seconds` — finite positive wall-clock budget, default 5 seconds,
  clamped to 20; includes snapshot/setup. Cancellation and timeout stop the run.
- `collect_strings=True` — discover printable ASCII/UTF-8/UTF-16LE candidates
  overlapping changed bytes, including stack strings with unknown offsets.
  Results are bounded and omissions are reported; candidates are not proof of
  the intended encoding or meaning.

### `resolve_emulated_string` — String-Extraction Shortcut

Same engine, optimised for the common "decode one string" case. Use when
you know the output buffer address and just want the decoded bytes.

Shared execution, buffer, ABI, allowlist and budget parameters have the same
semantics as `emulate_code`.
- Supply exactly one of `output_address` or signed `output_stack_offset`.
- `max_output_size` — capture size, 1..4096 bytes (default 4096). The whole
  requested window must be backed by IDB, declared scratch, or the stack.

Returns bounded capture summaries with independent per-encoding termination
flags and a short hex preview. Captures are limited to 4096 bytes each;
the text response is capped at 7500 characters and does not dump full buffers.

## Workflow

### Step 1 — Recon the Target

Before invoking either tool, gather context:

1. `decompile_function` the target — confirm no external API/syscall dependency;
   identify internal helpers that need `code_ranges`.
2. `read_function_disassembly` to identify entry and exclusive end. A range
   slice normally stops before `ret`; a whole-function call uses `function`
   mode and includes `ret` inside the bounds.
3. `list_segments` to locate code, IDB inputs and output. Use `memory_ranges`
   for IDB bytes and `memory_buffers` for known runtime/scratch bytes.
4. `xrefs_to` the routine if you need to recover call-site arguments
   (encrypted data pointer, key pointer, output pointer).

### Step 2 — Identify Inputs

For each input the routine reads, determine:

- **Address** in the IDB (encrypted blob, key bytes, lookup table).
- **Size** in bytes (from the routine's read pattern or segment layout).
- Whether it lives inside the code range (auto-mapped) or in another
  segment (must be added to `memory_ranges`).

If runtime bytes are known but absent from the IDB, provide `memory_buffers`;
do not patch the IDB or pretend an unmodeled API/TLS/heap dependency is known.

### Step 3 — Build the Entry State

- Range mode: provide the registers initialized outside the slice. Unnamed
  registers start at zero; a synthetic SP is supplied if omitted.
- Whole function: choose the verified calling convention and pass `arguments`
  in declaration order. Use `registers={}` unless extra initial state is needed.
- Never override `eip`/`rip`; avoid conflicting aliases or argument registers.
- Capture known locals by signed `stack_offset`; use `collect_strings=True`
  when their output location is unknown.

### Step 4 — Run the Emulation

Call `emulate_code` (general case) or `resolve_emulated_string` (string
case). Inspect the `status` field in the result:

| Status | Meaning | Next action |
|---|---|---|
| `completed` | Reached the stop/return sentinel | Read captures and check the result |
| `range_exit` | PC left the executable allowlist | Identify the missing internal helper or unmodeled call; do not widen blindly |
| `instruction_limit` | Consumed the instruction budget | Check loop inputs before raising the bounded limit |
| `unmapped_memory` | Accessed absent bytes or page padding | Supply the actual missing IDB/input bytes |
| `permission_error` | Access violated mapped permissions | Correct the output address or use separate `rw` scratch; `memory_ranges` never widens permissions |
| `unsupported_instruction` | Syscall/software interrupt/unsupported opcode | Use an approved reimplementation or a different analysis technique |
| `timeout` / `cancelled` | Deadline or cancellation stopped setup/execution | CPU-started runs retain partial state; aborted snapshots have no fabricated captures |
| `emulator_error` | Unexpected native engine error | Inspect `reason` and the static code |

### Step 5 — Capture and Annotate

On `completed` (or any partial result with useful captures):

1. Read the captured bytes from the result block.
2. If it is a string, pick the encoding whose candidate is non-empty and
   printable (ASCII is the safest default; UTF-16LE for Windows wide-chars).
3. `set_comment` at the call site with the decoded value — this persists
   the result in the IDB without mutating code.
4. Optionally `rename_function` the routine (e.g., `decrypt_string_xor`,
   `decode_base64_table`) if its purpose is now clear.

## Worked Example: XOR String Decoder (x64)

A function at `0x401000` takes `rcx = encrypted_ptr`, `rdx = key_ptr`,
`r8 = output_ptr`, decodes 32 bytes via XOR, and `ret`s. The encrypted
blob is at `0x402000` (32 bytes), the key at `0x402040` (4 bytes), and
the output buffer is at `0x403000` (32 bytes).

```
emulate_code(
  start_address="0x401000",
  stop_address="0x401080",      # function end; return sentinel
  registers={},
  execution_mode="function",
  calling_convention="win64",
  arguments=["0x402000", "0x402040", "0x403000"],
  memory_ranges=[
    {"address": "0x402000", "size": 32},   # encrypted input
    {"address": "0x402040", "size": 4},    # key
    {"address": "0x403000", "size": 32},   # writable IDB output
  ],
  capture_ranges=[
    {"address": "0x403000", "size": 32, "label": "decoded"},
  ],
  instruction_limit=100000,
)
```

Or, equivalently, with the string-extraction shortcut:

```
resolve_emulated_string(
  start_address="0x401000",
  stop_address="0x401080",
  registers={},
  execution_mode="function",
  calling_convention="win64",
  arguments=["0x402000", "0x402040", "0x403000"],
  output_address="0x403000",
  max_output_size=32,
  memory_ranges=[
    {"address": "0x402000", "size": 32},
    {"address": "0x402040", "size": 4},
  ],
)
```

Expected result: `status=completed`, captured output contains the
decoded ASCII string.

## Critical Rules

- **`stop_address` is exclusive.** Passing the last instruction's address
  skips that instruction. Range mode does not plant a return address; use
  function mode when the routine must execute its `ret`.
- **Require explicit entry state.** Non-empty registers in range mode;
  an explicit verified calling convention in function mode.
- **Map actual inputs.** IDB ranges preserve their real bytes; scratch bytes
  must be declared. Gaps and page padding never become valid zero-filled input.
- **Read-only IDB pages stay read-only.** Permission conflicts sharing a page
  fail explicitly. Do not try to change them through `memory_ranges`.
- **Aggregate mapped memory is capped at 16 MiB including the 1 MiB stack.**
  Map only needed pages. A collision with the fixed synthetic stack is rejected.
- **Never guess `esp`/`rsp`.** The synthetic stack is a fixed 1 MiB mapping:
  `0x7ffe0000..0x800dff00` on 32-bit, `0x7ffe00000000..0x7ffe000fff00` on
  64-bit. The top is exclusive for the mapping, so an SP at or above
  `base + 0x100000` is rejected. Omit the register and let the emulator pick
  the default. A real address from the binary is a different address space and
  will be rejected.
- **Always redecompile/verify after analysis.** Emulation gives you a
  snapshot; cross-check the result against the static decompilation
  before annotating the IDB.
- **`status != completed` is still useful.** Partial runs report final
  registers, instruction count, and write events — these often reveal
  where the routine diverged from your model.
