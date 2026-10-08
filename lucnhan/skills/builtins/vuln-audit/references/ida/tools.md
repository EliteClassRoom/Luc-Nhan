# IDA Pro Vulnerability Audit Tools

## Evidence Gathering (Read-Only)

Use these in this order for each suspected finding:

1. **`get_binary_info`** — file format, architecture, bitness, entry point, function count, file type hints.
2. **`list_segments`** — segment ranges and permissions; flag W+X segments and unusual executable data.
3. **`list_imports`** — full API surface: memory ops, network, files, process launch, crypto, IPC, kernel interfaces.
4. **`list_exports`** — externally reachable entry points.
5. **`decompile_function`** — primary evidence source. Read the pseudocode for the function containing a suspected sink.
6. **`get_pseudocode`** — raw pseudocode lines with addresses (useful for evidence snippets).
7. **`get_decompiler_variables`** — stack buffers, argument types, local sizes. Map every buffer and its capacity.
8. **`xrefs_to`** — find all call sites of a dangerous API; trace callers of a source input.
9. **`function_xrefs`** — map callers and callees to place a function in the call graph. Use before decompiling to prioritize which functions need deep analysis.
10. **`read_disassembly`** / **`read_function_disassembly`** — raw disassembly when pseudocode is ambiguous or inlined.
11. **`search_strings`** / **`list_strings`** — find format strings, command templates, path patterns, and credential references.

## Helper Analysis

- **`execute_python`** — use only for bounded analysis when built-in tools are insufficient:
  - Calculating integer overflow boundaries for attacker-controlled sizes.
  - Decoding constant arrays that appear to be encryption keys or encoded strings.
  - Checking simple constraints (e.g., "does this check actually guard that copy?").
  - NEVER use for bulk searching, renaming, or mutation — keep the audit read-only.

## Hex-Rays Specific Notes

- Pseudocode may fold or optimize away checks that exist at the IL level. If a bounds check is missing in pseudocode, use `read_disassembly` on the function to verify it also does not exist in the compiled code.
- Inlined functions may hide dangerous calls — check `read_disassembly` for `call` instructions that pseudocode obscures.
- Decompiler-inserted casts (e.g., `(unsigned __int64)` before multiplication) may mask integer truncation bugs — verify the original operand sizes.
- `get_decompiler_variables` returns variable names and sizes as they appear in pseudocode, which may differ from the actual stack layout after optimization.

## Mutation (Only If User Explicitly Requests)

- **Do not** rename, retype, comment, or patch unless the user asks.
- If annotations would clarify a finding, propose them first and wait for approval.
