---
name: Naming Convention
description: Comprehensive naming standard for IDA — functions, variables, globals, structs, enums, types. Covers edge cases (wrappers, mangling, Go/Rust, vtable) and confidence-based placeholders. Load before bulk rename or complex retyping.
tags: [naming, convention, annotations, reverse-engineering]
author: Luc Nhan
version: 1.0
triggers:
  - naming convention
  - naming standard
  - rename function
  - rename variable
  - how to name
  - naming
  - wrapper name
  - thunk name
  - unknown function
  - uncertain rename
  - c++ mangling
  - go function name
  - rust function name
  - vtable method
  - enum naming
  - struct field name
allowed_tools:
  - rename_function
  - rename_variable
  - rename_address
  - set_comment
  - set_function_comment
  - set_function_prototype
  - decompile_function
  - get_decompiler_variables
  - xrefs_to
  - xrefs_from
  - function_xrefs
  - search_strings
  - search_imports
  - imports_by_module
  - search_functions
  - save_memory
  - suggest_struct_from_accesses
  - create_struct
  - create_enum
---

## Pre-Rename Checklist

Before renaming or retyping ANYTHING, form a complete hypothesis:

1. **Decompile** the function and read the full body.
2. **Check xrefs** — `xrefs_to` (who calls it?) and `xrefs_from` / `function_xrefs` (what does it call?).
3. **Check strings** — `search_strings` near the function for error messages, log strings, format strings.
4. **Form a verb-noun hypothesis** — what does it DO? (`Parse`, `Send`, `Decrypt`, `Alloc`...)
5. If you cannot state the verb confidently, you do NOT have enough evidence — see the Escalation Ladder.

Do not rename without evidence. A wrong name is worse than `sub_XXXX` — it misleads every future analyst.

## Naming Conventions by Object Type

### Functions — PascalCase verb-noun

- **Verb-noun required**: `ParseHttpRequest`, NOT `HttpRequestParser`, NOT `http_request_parser`.
- **Subsystem prefix (optional, default: do NOT use)**: only add when (a) the binary has a clearly identifiable module (vtable / dispatch table) AND (b) the function's own behavior does not already leak the subsystem. Example where prefix is justified: `CryptoAesDecrypt` (binary has a separate crypto module). Default: keep it simple (`AesDecrypt`).
- **Avoid filler pronouns**: no `My`, `This`, `The`. Use `DecryptConfig`, not `DecryptMyConfig`.

### Variables — snake_case

- `snake_case`, lowercase, underscore separator (`buffer_offset`, `bytes_read`).
- **Parameters**: same rule. Do NOT add `p_`/`a_` prefixes — IDA auto-generates `a1`, `v2`; when you rename, drop the auto prefix.
- **No Hungarian notation** (`bEnabled`, `dwSize`). Names describe PURPOSE, not TYPE. Reason: local variables have decompiler scope — type is visible from the declaration, so Hungarian is redundant.
- **Booleans**: `is_`/`has_`/`should_` prefix (`is_initialized`, `has_pending_data`).

### Globals — `g_` prefix + camelCase

- `g_` prefix + camelCase body: `g_C2ServerUrl`, `g_pConfigStart`, `g_bEnabled`.
- **Keep light Hungarian for globals** (`g_p` = pointer, `g_b` = bool, `g_dw` = dword). Reason: globals are hard to type-infer from context (no local decompiler scope), so Hungarian provides a useful signal. This is a deliberate difference from local variables (which drop Hungarian).
- **vtable pointer / section base**: `g_vtable_<ClassName>`.

### Structs & UDT — PascalCase name, snake_case field

- **Struct name**: PascalCase noun (`DnsConfig`, `TcpConnectionState`).
- **Struct field**: snake_case (`connection_timeout`, `buffer_size`) — matches C convention.
- **C++ class**: keep the `C` prefix if the binary uses MFC/ATL (`CFooMgr`); otherwise drop it.
- **Union**: `union_<Purpose>` or PascalCase + a comment.
- **Nested/anonymous**: add a comment `// anonymous struct for ...`.

### Enums — PascalCase type + UPPER_SNAKE members

- **Enum type name**: PascalCase (`SocketState`, `MessageType`).
- **Enum member**: `UPPER_SNAKE_CASE`, prefixed with the enum's abbreviation: `SOCK_CONNECTED`, `MSG_TYPE_HANDSHAKE`.
- **Flag/bitmask**: `FLAG_` prefix (`FLAG_READ`, `FLAG_WRITE`).
- **IDA 9.x**: create enums via `ida_typeinf` with `BTF_ENUM`. Do NOT use `ida_enum` (removed in IDA 9.x).

### Type / Typedef — PascalCase

- **Function pointer typedef**: PascalCase + `Cb` suffix (callback) or `Fn` suffix (function): `TimerCallback`, `AllocFn`.
- **Standard typedef**: PascalCase noun, NO `_t` suffix (avoids clashing with C stdint `uint32_t`): `SocketHandle`, `ConnectionId`.
- **Function prototype**: use `set_function_prototype` with standard C syntax.

## Edge Cases

| Situation | Rule | Example |
|-----------|------|---------|
| **Jump-thunk** (jump-only, no stack frame) | `j_<Orig>` prefix | `j_malloc` |
| **Call-thunk** (small stack frame, setup then `call`) | `thunk_<Orig>` prefix | `thunk_CreateFile` |
| **Logic-wrapper** (adds logic before/after: logging, mutex, error check, arg transform) | `<Orig>Wrapper` suffix | `MallocWrapper` |
| **C++ name mangling** (binary has symbols) | Demangle, keep full signature if it's a public method | `std::vector<int>::push_back` → keep as-is |
| **C++ but stripped** (no RTTI/symbols) | Treat as a plain C function; do NOT apply C++ rules | — |
| **Go binary** | `go_<pkg>_<FuncName>`: package + function, drop the receiver | `go_main_ConnectC2`, `go_net_ResolveDomain` |
| **Rust binary** | Keep original snake_case if unmangled; if mangled, demangle Rust-style | `rust_std_panicking_panic` |
| **vtable method** | `<Class>__<Method>`: double underscore | `CHttpClient__SendRequest` |
| **Known callback** | `<Event>Callback` | `WindowProc`, `TimerCallback` |
| **Entry point** | Keep `main`/`wWinMain`; rename the wrapper to `Entry_*` | `Entry_RealMain` |
| **Thunk import** | Keep `__imp_` (IDA auto-generated); do NOT rename | — |

### Wrapper/Thunk decision table

| Code characteristic | Prefix/suffix | Example |
|---------------------|---------------|---------|
| Only `jmp` to target, no stack frame | `j_<Orig>` | `j_malloc` |
| Small stack frame, setup then `call` | `thunk_<Orig>` | `thunk_CreateFile` |
| Adds logic (logging, mutex, error check, arg transform) | `<Orig>Wrapper` | `MallocWrapper` |

## Confidence-Based Decision Matrix

| Confidence | Action | Name |
|------------|--------|------|
| **>90%** | Rename immediately | `ParseHttpRequest` |
| **70-90%** | Rename + add a repeatable comment with the evidence | `DecryptConfig // ev: calls RC4, keysched at 0x4013` |
| **50-70%** | Do NOT rename — use the placeholder | `Unknown_<Hint>_<addr>` (see below) |
| **<50%** | Leave as `sub_XXXX`; log a hypothesis via `save_memory` | (keep `sub_XXXX`) |

## Escalation Ladder (when confidence < 70%)

Do NOT rename in a hurry. Climb the evidence ladder in order (cheap → expensive):

**Level 0 — Re-read carefully (almost free)**
- Decompile fully + read the disassembly chunk too.
- Drop a **repeatable comment** with the hypothesis first: `// hypothesis: RC4 key schedule (evidence: loops 256, byte swap)`. Comments are easier to fix than a wrong name.
- Ask: is there a clear verb? If not, you don't have enough evidence yet.

**Level 1 — Call-graph context (cheap)**
- `xrefs_to`: who calls this? If the caller is `main`/`init_*`, it's likely an init routine.
- `xrefs_from` + `function_xrefs` (depth 2): what does it call? Heavy `socket`/`send`/`recv` → networking.
- Topology: hub (many callers) = utility; leaf (calls nothing) = primitive; big switch = dispatcher/handler.
- **Heuristic**: function purpose usually leaks from CALLERS more than from the body.

**Level 2 — Semantic leak (medium)**
- `search_strings` + `xrefs_to` the string's address near the function: error messages, log strings, format strings leak purpose (`"Failed to decrypt"`, `"http://%s:%d"`).
- `imports_by_module` + `search_imports`: if the function only calls `CreateFileW`/`WriteFile`/`CloseHandle` → file I/O subsystem.
- Pattern-set matching: characteristic import groups pin down a subsystem (crypto: `CryptAcquireContext`+`BCrypt*`; registry: `RegOpenKey`+`RegQueryValue`).

**Level 3 — Constant / magic matching (strong)**
- Magic constants identify an algorithm with near-certainty:
  - `0xEDB88320` / `0x04C11DB7` → CRC32 polynomial
  - AES S-box bytes (`0x63, 0x7c, 0x77, ...`) → AES
  - SHA-256 init `0x6a09e667` / MD5 `0x67452301` → hash
  - RC4 KSA pattern: `for i in 256 { swap }`
- `search_strings` for hex constants, or read raw bytes from `.rdata`.
- **Why Level 3 is more trustworthy than Level 0**: decompiler output can be distorted by obfuscation (MBA, CFF). But magic constants live in `.rdata` — they're DATA, not CODE, and obfuscation rarely hides them.

**Level 4 — Struct reconstruction (strong, expensive)**
- `suggest_struct_from_accesses`: reconstruct the data layout from pointer-access patterns → field names leak purpose.
- `get_decompiler_variables`: retyping a local var can make the decompiler clearer → then the verb becomes inferrable.

**Level 5 — Deep analysis (most expensive)**
- Spawn a subagent (`/explore` mode, or the deep bulk_renamer) with 8 turns, full tool access, chasing xrefs 2-3 levels.
- External comparison: Diaphora/BinDiff match against an already-analyzed binary or an open-source build → borrow verified names.

## Placeholder Convention

For the 50-70% confidence band, use `Unknown_<Hint>_<addr>`:

- **Format**: `Unknown_` + PascalCase hint + `_` + hex lowercase address with NO `0x` prefix.
  Examples: `Unknown_HashFunc_4012a0`, `Unknown_StringOp_4012a0`.
- **Why a prefix instead of comment-only**: IDA's function-list panel shows names but not comments. The `Unknown_` prefix turns the function list into a natural kanban board — sort by name, all `Unknown_*` cluster together, easy to review.
- **Compatibility with `_AUTO_NAME_PATTERNS`**: the regex set in `bulk_renamer.py` only skips `sub_`/`FUN_`/`func_`/`unnamed_`/`loc_`. It does NOT match `Unknown_`, so bulk_renamer treats it as human-assigned and will NOT overwrite it. This is intentional — when you gain new evidence, rename an `Unknown_` entry manually.
- **Progressive renaming**: after renaming a function, re-check its callers to see if the hypothesis still holds. If renaming `ParseConfig` makes caller `sub_402000` suddenly readable → positive confirmation. If the caller still reads as nonsense → the name may be wrong; revert.

## Cross-References

- `/malware-analysis` — Windows PE malware patterns (uses the same naming conventions).
- `/generic-re` — General binary analysis workflow.
- `/ida-scripting` — IDAPython API for `execute_python` (enum/struct creation APIs).