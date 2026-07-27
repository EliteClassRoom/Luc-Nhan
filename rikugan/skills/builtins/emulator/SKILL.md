---
name: Emulator
description: Bounded CPU emulation for self-contained IDA code ranges — decode stubs, custom crypto, opaque predicates. Wraps emulate_code and resolve_emulated_string with the right register, memory-range, and stop-address discipline.
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

The engine is a per-call Unicorn instance. Memory is mapped from real IDA
segments; source virtual addresses are preserved. The only always-writable
region is the synthetic stack — IDB read-only pages stay read-only.

## When to Use This Skill

Activate this skill when the user asks to:

- **Decode a string** whose decoder stub is self-contained (no API calls,
  no syscalls, no branches leaving the stub).
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
- Issues syscalls (`syscall`, `sysenter`, `int 0x2e`, `int 0x80`) — these
  are detected up front and reported as `unsupported_instruction`.
- Branches outside the proposed range mid-decode — execution stops at the
  range boundary with status `range_exit` and a partial result.
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
- `stop_address` (**exclusive** hex) — execution stops BEFORE this address.
  A successful run reaches `stop_address` and reports `status=completed`.
- `registers` (non-empty object) — explicit initial CPU state. Keys are
  x86/x64 register names (`eax`, `ebx`, `rax`, `r8`, `eflags`, etc.).
  `eip`/`rip` are always taken from `start_address` and cannot be set here.
- `memory_ranges` — extra IDB address ranges to map beyond the code range.
  Use this for encrypted input buffers, key material, lookup tables that
  live in other segments. Each entry is `{"address": "0x...", "size": N}`.
- `capture_ranges` — output buffers to read back at the end. Each entry is
  `{"address": "0x...", "size": N}` (size capped at 4096).
- `instruction_limit` — default 100_000, hard cap 1_000_000.

### `resolve_emulated_string` — String-Extraction Shortcut

Same engine, optimised for the common "decode one string" case. Use when
you know the output buffer address and just want the decoded bytes.

Key parameters:
- `start_address`, `stop_address`, `registers`, `memory_ranges`,
  `instruction_limit` — same semantics as `emulate_code`.
- `output_address` — address of the decoded-string output buffer.
- `max_output_size` — bytes to scan for NUL terminators (default 4096,
  hard cap 4096).

Returns raw bytes plus ASCII / UTF-8 / UTF-16LE candidate strings with a
`terminated=` flag.

## Workflow

### Step 1 — Recon the Target

Before invoking either tool, gather context:

1. `decompile_function` the target — confirm it is self-contained
   (no calls to imports, no syscalls, no branches to addresses outside
   the function body).
2. `read_function_disassembly` to identify the exact `start_address` and
   the address immediately AFTER the last instruction to execute. That
   next address is your **exclusive** `stop_address`.
3. `list_segments` to confirm which segments hold the code, encrypted
   input, key material, and output buffer — you will need their address
   ranges for `memory_ranges` and `capture_ranges`.
4. `xrefs_to` the routine if you need to recover call-site arguments
   (encrypted data pointer, key pointer, output pointer).

### Step 2 — Identify Inputs

For each input the routine reads, determine:

- **Address** in the IDB (encrypted blob, key bytes, lookup table).
- **Size** in bytes (from the routine's read pattern or segment layout).
- Whether it lives inside the code range (auto-mapped) or in another
  segment (must be added to `memory_ranges`).

If an input is not yet present in the IDB (e.g., a runtime-allocated
buffer), emulation cannot proceed — tell the user and propose
`execute_python` instead.

### Step 3 — Build the Register State

From the decompiled signature and calling convention, set:

- Argument registers (`rcx`/`rdx`/`r8`/`r9` on x64 MSVC; `ecx`/`edx` on
  x86 cdecl/fastcall) to the input addresses you identified.
- `rsp`/`esp` is auto-set to a synthetic stack top — you do not need to
  pass it unless the routine reads a specific stack offset.
- `eip`/`rip` MUST be omitted — it is taken from `start_address`.

The `registers` object must be non-empty; the tool rejects an empty
object with a `ToolError`.

### Step 4 — Run the Emulation

Call `emulate_code` (general case) or `resolve_emulated_string` (string
case). Inspect the `status` field in the result:

| Status | Meaning | Next action |
|---|---|---|
| `completed` | Reached `stop_address` | Read captures — done |
| `range_exit` | PC left `[start, stop)` before reaching `stop` | Widen the range or fall back to `execute_python` |
| `instruction_limit` | Exceeded `instruction_limit` | Raise the limit (up to 1_000_000) or check for an infinite loop |
| `unmapped_memory` | Read/write/fetch to an unmapped address | Add the missing range to `memory_ranges` |
| `permission_error` | Wrote to a read-only IDB page | Map the buffer in `memory_ranges` with explicit write intent — but note the tools refuse to silently remap IDB pages, so this usually means a wrong output address |
| `unsupported_instruction` | Hit `syscall`/`sysenter`/`int 0x2e`/`int 0x80` or an instruction Unicorn cannot decode | Fall back to `execute_python`; the routine is not self-contained |
| `emulator_error` | Unicorn raised an unexpected error | Report the `reason` field; this is a bug or a corner case |

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
  stop_address="0x401080",      # address of the ret + 1
  registers={
    "rcx": "0x402000",
    "rdx": "0x402040",
    "r8":  "0x403000",
  },
  memory_ranges=[
    {"address": "0x402000", "size": 32},   # encrypted input
    {"address": "0x402040", "size": 4},    # key
    {"address": "0x403000", "size": 32},   # output buffer
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
  registers={"rcx": "0x402000", "rdx": "0x402040", "r8": "0x403000"},
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

- **`stop_address` is exclusive.** A common mistake is to pass the address
  of the last instruction; the run will report `range_exit` because
  execution falls off the end before "reaching" that address.
- **`registers` must be non-empty and must not contain `eip`/`rip`.** The
  tool raises `ToolError` otherwise.
- **Map every input region.** Any address the routine reads that is not
  in the code range and not in `memory_ranges` will trigger
  `unmapped_memory`.
- **Read-only IDB pages stay read-only.** The tools never silently remap
  a segment as writable — if the routine writes to a read-only page,
  you'll see `permission_error`. This is intentional (defensive default).
- **Aggregate mapped bytes are capped at 16 MiB.** Plan `memory_ranges`
  accordingly; map only the bytes the routine actually touches.
- **Always redecompile/verify after analysis.** Emulation gives you a
  snapshot; cross-check the result against the static decompilation
  before annotating the IDB.
- **`status != completed` is still useful.** Partial runs report final
  registers, instruction count, and write events — these often reveal
  where the routine diverged from your model.
