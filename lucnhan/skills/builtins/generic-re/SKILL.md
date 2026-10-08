---
name: General Reverse Engineering
description: General-purpose binary analysis — understand functionality, architecture, and behavior
tags: [analysis, reverse-engineering, general]
---
Task: General Reverse Engineering. You are analyzing a binary to understand its functionality, architecture, or behavior. No assumption about maliciousness.

## Approach

Build a mental map of the binary's structure. Start at the entry point or user-specified function. Name functions as you understand them — each rename makes the next function easier to read. Focus on what the user is interested in, not exhaustive coverage.

## Workflow

1. `get_binary_info` — format, architecture, size, function count
2. `list_imports` + `list_exports` — understand the binary's interface (batch these)
3. Start at the function of interest (or entry if exploring)
4. `decompile_function` → understand → `rename_function` / `rename_variable` → follow call chains
5. Use `xrefs_to` and `xrefs_from` to trace data and code references
6. Build up a picture of the binary's modules, data structures, and control flow

## Call Graph Strategy

Use xref tools BEFORE decompiling for exploration — they're cheaper:
1. `function_xrefs` on entry → map top-level subsystems without decompiling everything
2. `xrefs_to` on interesting imports → find which functions use specific APIs
3. Decompile only the nodes you actually need to understand
4. After understanding a function's purpose, check its callers to propagate context upward

Depth guidance:
- Immediate callers/callees: quick orientation
- 2 levels: neighborhood — usually sufficient
- 3+ levels: subsystem mapping — only for deep dives

## Domain-Specific Tips

**Libraries/frameworks:** Focus on exported functions and their calling conventions. Use `list_exports` to map the public API.

**Drivers/kernel modules:** Identify dispatch routines, IOCTL handlers, initialization. Consider using `/driver-analysis` for Windows drivers.

**Proprietary formats:** Trace the parsing code. Use `create_struct` and `suggest_struct_from_accesses` to reconstruct data structures. Apply with `apply_struct_to_address`.

**Firmware/embedded:** Check for known library signatures in function prologues. Map memory-mapped I/O regions via `list_segments`.

**Statically linked (Go/Rust):** No imports — look for runtime strings (runtime., go.itab, panicked at). Function count will be high; focus on entry and user code.

## Renaming Strategy

- Before renaming, form a hypothesis from: decompiled code + xrefs + string references
- Rename in semantic batches: all network functions together, all crypto together
- After renaming a batch: re-decompile to verify the renamed code reads correctly
- Use `set_comment` and `set_function_comment` to document non-obvious logic
- Naming conventions:
  - Functions: PascalCase verb-noun (InitializeGlobals, ParseHttpRequest)
  - Variables: snake_case (buffer_offset, bytes_read)
  - Globals: g_ prefix + camelCase (g_bEnabled, g_pConfigStart, g_C2ServerUrl)
  - Structs: PascalCase; fields snake_case (BrowserConfig, connection_timeout)
  - Enums: PascalCase type + UPPER_SNAKE members (MessageType, MSG_TYPE_HANDSHAKE)
  - For edge cases / uncertain names, see the /naming-convention skill.

## Output

Deliver what the user asks for:
- Function summaries with addresses
- Architectural overview
- Data structure definitions (C-style)
- Specific answers about behavior
