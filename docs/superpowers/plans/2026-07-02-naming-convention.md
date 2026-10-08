# Naming Convention Standard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify naming conventions across Luc Nhan by defining a comprehensive PascalCase/snake_case standard, fixing the PascalCase-vs-snake_case inconsistency between the system prompt and bulk_renamer, and shipping a new `naming-convention` skill for edge cases.

**Architecture:** Hybrid 3-tier — (1) expand the baseline `RENAMING_SECTION` in `lucnhan/agent/prompts/base.py` from 3 naming rules to 6, (2) create a new `naming-convention` skill with the full standard + escalation ladder, (3) fix `bulk_renamer.py` prompts from snake_case to PascalCase. The skill is the single source of truth; the baseline and bulk_renamer hold summaries.

**Tech Stack:** Python 3.11+, ruff, mypy, pytest, IDA Pro 9.x API, Luc Nhan skill loader (custom YAML frontmatter parser — no PyYAML).

**Spec:** `docs/superpowers/specs/2026-07-02-naming-convention-design.md`

## Global Constraints

- Functions: PascalCase verb-noun (`InitializeGlobals`, `ParseHttpRequest`). NEVER snake_case.
- Variables: snake_case (`buffer_offset`). No Hungarian. No `p_`/`a_` prefixes.
- Globals: `g_` prefix + camelCase (`g_bEnabled`, `g_pConfigStart`). Keep light Hungarian (`g_p`/`g_b`/`g_dw`).
- Structs: PascalCase name + snake_case fields (`BrowserConfig`, `connection_timeout`).
- Enums: PascalCase type + UPPER_SNAKE members (`MessageType`, `MSG_TYPE_HANDSHAKE`).
- Typedefs: PascalCase (`SocketHandle`, `TimerCallback`). No `_t` suffix.
- Uncertain names (50-70% confidence): `Unknown_<Hint>_<addr>` where `<addr>` is hex lowercase, no `0x`.
- `rename_multi_variables` does NOT exist in the toolset — do not reference it. Use `rename_variable` per-variable.
- All Python modules start with `from __future__ import annotations`.
- Host API imports use `importlib.import_module()` inside `try/except ImportError`.
- Commit format: `type(scope): description`. Attribution disabled globally.
- Run `./ci-local.sh` before push — must pass ruff format, ruff check, mypy, pytest, desloppify (score must not drop >0.5).

---

## File Structure

| File | Responsibility |
|------|---------------|
| `lucnhan/skills/builtins/naming-convention/SKILL.md` | **NEW** — Full naming standard (7 sections) + frontmatter with triggers |
| `lucnhan/skills/builtins/naming-convention/references/naming-examples.md` | **NEW** — Before/after examples by scenario (lazy-loaded) |
| `lucnhan/agent/prompts/base.py` | **MODIFY** — Expand `RENAMING_SECTION` (lines 49-63) from 3 to 6 naming rules |
| `lucnhan/agent/bulk_renamer.py` | **MODIFY** — `QUICK_ANALYSIS_PROMPT` (line 24) + `DEEP_ANALYSIS_PROMPT` (line 49): snake_case → PascalCase |
| `lucnhan/skills/builtins/malware-analysis/SKILL.md` | **MODIFY** — Naming Conventions section (lines 88-92): expand to 6 rules |
| `lucnhan/skills/builtins/generic-re/SKILL.md` | **MODIFY** — Renaming Strategy naming line (line 52): expand to 6 rules |
| `tests/agent/test_system_prompt.py` | **MODIFY** — Add 2 tests for expanded `RENAMING_SECTION` |
| `tests/tools/test_skills.py` | **MODIFY** — Add test for `naming-convention` skill discovery + trigger isolation |
| `tests/agent/test_bulk_renamer_prompts.py` | **NEW** — Regression test for PascalCase prompts |
| `CHANGELOG.md` | **MODIFY** — Add entry under `[1.6.0]` (or next unreleased) |

---

## Task 1: Create the `naming-convention` skill (Tầng 2)

**Files:**
- Create: `lucnhan/skills/builtins/naming-convention/SKILL.md`
- Create: `lucnhan/skills/builtins/naming-convention/references/naming-examples.md`
- Test: `tests/tools/test_skills.py`

**Interfaces:**
- Produces: skill slug `naming-convention`, discoverable via `SkillRegistry.discover()`, with triggers including `naming convention`, `rename function`, `wrapper name`, `thunk name`, `unknown function`, `c++ mangling`, `go function name`, `vtable method`, `enum naming`.
- Consumes: nothing (standalone markdown).

- [ ] **Step 1: Write the failing test**

Add to `tests/tools/test_skills.py`, inside `class TestBuiltinTriggerMatching` (after `test_vuln_audit_still_wins_own_queries`, before the `if __name__` block):

```python
    def test_naming_convention_skill_discovered(self):
        """The naming-convention skill must be discoverable with its triggers."""
        skill = self.reg.get("naming-convention")
        self.assertIsNotNone(skill, "naming-convention skill not discovered")
        self.assertGreater(
            len(skill.triggers), 10, "naming-convention must carry its triggers"
        )

    def test_naming_convention_wins_rename_queries(self):
        """Rename/wrapper/thunk queries must route to naming-convention."""
        for query in (
            "what naming convention should I use for this function",
            "how should I name this wrapper function",
            "is this a thunk I should prefix with j_",
            "how to name an unknown function I'm not sure about",
            "naming standard for enum members",
        ):
            with self.subTest(query=query):
                skill = self.reg.match_triggers(query)
                self.assertIsNotNone(skill, f"no match for: {query}")
                self.assertEqual(
                    skill.slug,
                    "naming-convention",
                    f"expected naming-convention for: {query}",
                )

    def test_naming_convention_does_not_steal_analysis_queries(self):
        """naming-convention must not steal general analysis queries that
        belong to generic-re or malware-analysis."""
        for query in (
            "analyze this binary for malware behavior",
            "what does this binary do overall",
            "find all crypto imports in this sample",
        ):
            with self.subTest(query=query):
                # These should NOT resolve to naming-convention.
                skill = self.reg.match_triggers(query)
                if skill is not None:
                    self.assertNotEqual(
                        skill.slug,
                        "naming-convention",
                        f"naming-convention stole a general query: {query}",
                    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/tools/test_skills.py::TestBuiltinTriggerMatching::test_naming_convention_skill_discovered -v`
Expected: FAIL with `naming-convention skill not discovered` (skill does not exist yet).

- [ ] **Step 3: Create the skill directory and SKILL.md**

Create `lucnhan/skills/builtins/naming-convention/SKILL.md`:

````markdown
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
````

- [ ] **Step 4: Create the references file**

Create `lucnhan/skills/builtins/naming-convention/references/naming-examples.md`:

````markdown
# Naming Examples — Before / After

Concrete before/after examples for each common scenario. Load this reference when you want worked examples of the naming standard.

## 1. Single function rename (PE malware)

**Before** (`sub_4012A0`, calls `socket` → `connect` → `send`):
```c
int sub_4012A0(char *host, int port, char *data) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    connect(s, ...);
    send(s, data, ...);
    return s;
}
```
**After**: `ConnectAndSend` (confidence >90%).
If only 60% sure it's connect-and-send (no clear `connect`): `Unknown_NetSend_4012a0`.

## 2. Bulk rename batch (stripped Go binary)

Go functions recover as `sub_XXXX` because symbols are stripped, but `pclntab` leaks original names.
**Before**: `sub_4812F0`
**Recovered from pclntab**: `main.ConnectC2`
**After**: `go_main_ConnectC2`

## 3. Struct reconstruction + field naming

**Before**: a global pointer `dword_5A1000` accessed with offsets `+0`, `+8`, `+10h`.
**Reconstructed struct**:
```c
struct ConnectionConfig {
    char *server_url;        // +0x00
    int port;                // +0x08
    int timeout_ms;          // +0x10
};
```
**After**: struct `ConnectionConfig` (PascalCase), fields `server_url`/`port`/`timeout_ms` (snake_case).

## 4. Wrapper/thunk chain (IAT resolution)

**Call graph**: `sub_402000` → `__imp_CreateFileW`
- `sub_402000` body: just `jmp __imp_CreateFileW` (no frame) → `j_CreateFileW`
- If it had `push ebp; mov ebp,esp; call __imp_CreateFileW; pop ebp` → `thunk_CreateFileW`
- If it wrapped with a mutex lock around the call → `CreateFileWWrapper`

## 5. Crypto identification via magic constant

**Before**: `sub_4030B0` contains a 256-entry loop with byte swaps and a constant table starting `0x63, 0x7c, 0x77, 0x6b...`
**Evidence**: AES S-box bytes → near-certain AES.
**After**: `AesDecrypt` (or `AesEncrypt` depending on direction; the S-box alone doesn't tell direction — check for `InvMixColumns`/inverse S-box `0x52, 0x09, 0x6a...` to distinguish).

If direction is ambiguous (50-70%): `Unknown_AesOp_4030b0`.
````

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/tools/test_skills.py::TestBuiltinTriggerMatching -v`
Expected: All 3 new tests PASS. The existing `test_vuln_audit_still_wins_own_queries` and `test_unrelated_query_no_match` must still PASS.

- [ ] **Step 6: Commit**

```bash
git add lucnhan/skills/builtins/naming-convention/SKILL.md lucnhan/skills/builtins/naming-convention/references/naming-examples.md tests/tools/test_skills.py
git commit -m "feat(skills): add naming-convention skill with full standard + escalation ladder"
```

---

## Task 2: Expand the baseline `RENAMING_SECTION` (Tầng 1)

**Files:**
- Modify: `lucnhan/agent/prompts/base.py:49-63` (the `RENAMING_SECTION`)
- Test: `tests/agent/test_system_prompt.py`

**Interfaces:**
- Produces: an expanded `RENAMING_SECTION` string covering all 6 object types, referencing the `naming-convention` skill for edge cases.
- Consumes: the `naming-convention` skill (Task 1) for edge-case lookups.

- [ ] **Step 1: Write the failing tests**

Add to `tests/agent/test_system_prompt.py`, inside `class TestBasePromptContent` (after `test_has_analysis_section`):

```python
    def test_renaming_section_covers_all_object_types(self):
        """Baseline RENAMING_SECTION must cover all 6 IDA object types."""
        from lucnhan.agent.prompts.base import RENAMING_SECTION
        self.assertIn("PascalCase", RENAMING_SECTION)   # functions
        self.assertIn("snake_case", RENAMING_SECTION)   # variables
        self.assertIn("g_", RENAMING_SECTION)           # globals
        self.assertIn("Enum", RENAMING_SECTION)         # enums
        self.assertIn("Typedef", RENAMING_SECTION)      # typedefs

    def test_renaming_section_references_naming_convention_skill(self):
        """Baseline must point to the naming-convention skill for edge cases."""
        from lucnhan.agent.prompts.base import RENAMING_SECTION
        self.assertIn("naming-convention", RENAMING_SECTION)
        self.assertIn("Unknown_<Hint>", RENAMING_SECTION)

    def test_renaming_section_does_not_reference_ghost_tool(self):
        """Regression: rename_multi_variables is a ghost tool — must NOT be
        referenced as if it exists. See spec self-review round 2."""
        from lucnhan.agent.prompts.base import RENAMING_SECTION
        # The phrase 'Use rename_multi_variables when available' must be gone.
        self.assertNotIn("Use rename_multi_variables", RENAMING_SECTION)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/agent/test_system_prompt.py::TestBasePromptContent::test_renaming_section_covers_all_object_types tests/agent/test_system_prompt.py::TestBasePromptContent::test_renaming_section_references_naming_convention_skill tests/agent/test_system_prompt.py::TestBasePromptContent::test_renaming_section_does_not_reference_ghost_tool -v`
Expected: All 3 FAIL — `RENAMING_SECTION` currently only mentions PascalCase/g_/structs, not Enum/Typedef/naming-convention, and still says "Use rename_multi_variables".

- [ ] **Step 3: Replace `RENAMING_SECTION`**

In `lucnhan/agent/prompts/base.py`, replace the entire `RENAMING_SECTION` block (currently lines 49-63):

```python
RENAMING_SECTION = """\
## Renaming & Retyping
- Before renaming or retyping anything, form a complete hypothesis about
  the function's purpose. Evidence = decompiled code + xrefs + string refs.
- Do not rename without evidence.
- Rename in semantic batches: all network vars together, all crypto vars
  together, etc. Use `rename_variable` per-variable (batch manually —
  `rename_multi_variables` does NOT exist in the current toolset).
- After renaming a batch: re-decompile once to verify the renamed code
  reads correctly.
- Naming conventions:
  - Functions: PascalCase verb-noun (InitializeGlobals, ParseHttpRequest)
  - Variables: snake_case (buffer_offset, bytes_read); no Hungarian
  - Globals: g_ prefix + camelCase (g_bEnabled, g_pConfigStart)
  - Structs: PascalCase name, snake_case fields (BrowserConfig.connection_timeout)
  - Enums: PascalCase type, UPPER_SNAKE members (MessageType.MSG_TYPE_HANDSHAKE)
  - Typedefs: PascalCase (SocketHandle, TimerCallback)
- For edge cases (wrappers, C++ mangling, Go/Rust, vtable) or confidence
  <70%, activate_skill("naming-convention") for the full standard +
  escalation ladder. Uncertain names use Unknown_<Hint>_<addr> placeholder.
"""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/agent/test_system_prompt.py -v`
Expected: ALL tests PASS, including the 3 new ones and the existing `test_has_renaming_section`.

- [ ] **Step 5: Verify no regression in the assembled prompt**

Run: `python -m pytest tests/agent/test_system_prompt.py::TestBuildSystemPrompt -v`
Expected: All PASS — `assemble_system_prompt` still includes the renaming section.

- [ ] **Step 6: Commit**

```bash
git add lucnhan/agent/prompts/base.py tests/agent/test_system_prompt.py
git commit -m "feat(prompt): expand RENAMING_SECTION to 6 object types + skill reference"
```

---

## Task 3: Fix `bulk_renamer.py` prompts — snake_case → PascalCase (Tầng 3)

**Files:**
- Modify: `lucnhan/agent/bulk_renamer.py:24-47` (`QUICK_ANALYSIS_PROMPT`)
- Modify: `lucnhan/agent/bulk_renamer.py:49-66` (`DEEP_ANALYSIS_PROMPT`)
- Create: `tests/agent/test_bulk_renamer_prompts.py`

**Interfaces:**
- Produces: `QUICK_ANALYSIS_PROMPT` and `DEEP_ANALYSIS_PROMPT` strings that enforce PascalCase.
- Consumes: nothing. Output format (`0x<addr> <name>` / `RENAME:`) is UNCHANGED — the parsers at `_quick_llm_call` (line 632) and `_run_deep_common` (line 742) must keep working.

- [ ] **Step 1: Write the failing test**

Create `tests/agent/test_bulk_renamer_prompts.py`:

```python
"""Regression tests for bulk_renamer prompt naming conventions.

The bulk_renamer prompts historically demanded snake_case, contradicting the
system prompt's PascalCase. These tests lock in the PascalCase fix so the
inconsistency cannot silently return.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.agent.bulk_renamer import DEEP_ANALYSIS_PROMPT, QUICK_ANALYSIS_PROMPT


class TestBulkRenamerPromptsUsePascalCase(unittest.TestCase):
    def test_quick_prompt_does_not_demand_snake_case(self):
        """The original 'Use snake_case naming convention' directive must be gone."""
        self.assertNotIn(
            "Use snake_case naming convention",
            QUICK_ANALYSIS_PROMPT,
            "QUICK_ANALYSIS_PROMPT still demands snake_case",
        )

    def test_deep_prompt_does_not_demand_snake_case(self):
        """The original 'using snake_case convention' directive must be gone."""
        self.assertNotIn(
            "using snake_case convention",
            DEEP_ANALYSIS_PROMPT,
            "DEEP_ANALYSIS_PROMPT still demands snake_case",
        )

    def test_quick_prompt_enforces_pascalcase(self):
        self.assertIn("PascalCase", QUICK_ANALYSIS_PROMPT)
        self.assertIn("NEVER snake_case", QUICK_ANALYSIS_PROMPT)

    def test_deep_prompt_enforces_pascalcase(self):
        self.assertIn("PascalCase", DEEP_ANALYSIS_PROMPT)
        self.assertIn("NEVER snake_case", DEEP_ANALYSIS_PROMPT)

    def test_quick_prompt_mentions_uncertain_placeholder(self):
        """Quick prompt must teach the Unknown_ placeholder for <70% confidence."""
        self.assertIn("Unknown_<Hint>", QUICK_ANALYSIS_PROMPT)

    def test_deep_prompt_mentions_uncertain_placeholder(self):
        self.assertIn("Unknown_<Hint>", DEEP_ANALYSIS_PROMPT)

    def test_output_format_unchanged(self):
        """Output format must stay '0x<addr> <name>' / 'RENAME:' so parsers work."""
        self.assertIn("0x<address> <new_name>", QUICK_ANALYSIS_PROMPT)
        self.assertIn("RENAME: 0x<address> <new_name>", DEEP_ANALYSIS_PROMPT)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/agent/test_bulk_renamer_prompts.py -v`
Expected: FAIL — `test_quick_prompt_enforces_pascalcase` and friends fail because the current prompts say snake_case.

- [ ] **Step 3: Replace `QUICK_ANALYSIS_PROMPT`**

In `lucnhan/agent/bulk_renamer.py`, replace the `QUICK_ANALYSIS_PROMPT` string (currently lines 24-47):

```python
QUICK_ANALYSIS_PROMPT = """\
You are a reverse engineering assistant specializing in function naming.

Below are decompiled functions from a binary, each accompanied by its
disassembly listing when available. For each function, suggest a descriptive
name based on its behavior.

Naming convention (CRITICAL):
- Functions: PascalCase verb-noun (InitializeGlobals, ParseHttpRequest,
  DecryptConfig). NEVER snake_case.
- Use verb prefixes: Init/Parse/Send/Recv/Encrypt/Decrypt/Alloc/Free/
  Check/Validate/Handle/Dispatch.
- If a function is a wrapper/thunk, prefix: j_<Orig>, thunk_<Orig>,
  or <Orig>Wrapper.
- If a function's purpose is unclear (<70% confident), output:
  Unknown_<Hint>_<hexaddr>   (e.g. Unknown_HashFunc_4012a0)
  Do NOT guess a confident name when uncertain.

Rules:
- Analyze what each function does based on decompiled code + disassembly
- If a function is a wrapper, name it after what it wraps (e.g. MallocWrapper)
- Use both decompiled code AND disassembly to understand the function

Output format: one line per function, exactly:
0x<address> <new_name>

Do NOT include any other text, explanations, or markdown formatting.
Only output the address-name pairs.

Functions to analyze:
"""
```

- [ ] **Step 4: Replace `DEEP_ANALYSIS_PROMPT`**

In `lucnhan/agent/bulk_renamer.py`, replace the `DEEP_ANALYSIS_PROMPT` string (currently lines 49-66):

```python
DEEP_ANALYSIS_PROMPT = """\
You are a reverse engineering expert. Analyze this function in depth.

Examine:
1. All callers and callees (decompile them if needed)
2. String references
3. API imports used
4. Data structures accessed
5. Control flow patterns
6. Magic constants (CRC32=0xEDB88320, AES S-box, SHA256 init=0x6a09e667)

Based on your thorough analysis, determine the function's purpose and
suggest a single descriptive name using PascalCase verb-noun convention
(InitializeGlobals, DecryptConfig, ParseHttpRequest). NEVER snake_case.

If confidence <70%, output:
Unknown_<Hint>_<hexaddr>   (e.g. Unknown_HashFunc_4012a0)

Your final line of output MUST be exactly:
RENAME: 0x<address> <new_name>

Function to analyze:
"""
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/agent/test_bulk_renamer_prompts.py -v`
Expected: ALL 7 tests PASS.

- [ ] **Step 6: Verify no snake_case directive remains in the file**

Run: `python -m pytest tests/agent/ -v -k "bulk_renamer or system_prompt"`
Expected: All PASS.

Run: `grep -in "snake_case naming\|using snake_case" lucnhan/agent/bulk_renamer.py`
Expected: No output (empty).

- [ ] **Step 7: Commit**

```bash
git add lucnhan/agent/bulk_renamer.py tests/agent/test_bulk_renamer_prompts.py
git commit -m "fix(bulk_renamer): switch Quick/Deep prompts from snake_case to PascalCase"
```

---

## Task 4: Sync the naming sections in `malware-analysis` and `generic-re` skills (DRY cleanup)

**Files:**
- Modify: `lucnhan/skills/builtins/malware-analysis/SKILL.md:88-92` (Naming Conventions section)
- Modify: `lucnhan/skills/builtins/generic-re/SKILL.md:52` (single naming line)
- Test: `tests/tools/test_skills.py`

**Interfaces:**
- Produces: both skills now show the same 6-rule summary and cross-reference `/naming-convention`.
- Consumes: the `naming-convention` skill (Task 1) as the detailed reference.

- [ ] **Step 1: Write the failing test**

Add to `tests/tools/test_skills.py`, inside `class TestBuiltinTriggerMatching` (after the naming-convention tests from Task 1):

```python
    def test_malware_analysis_skill_naming_section_expanded(self):
        """malware-analysis must carry the full 6-rule naming summary, not just 3."""
        skill = self.reg.get("malware-analysis")
        self.assertIsNotNone(skill)
        body = skill.body
        self.assertIn("Variables: snake_case", body)
        self.assertIn("Enums", body)
        self.assertIn("/naming-convention", body)

    def test_generic_re_skill_naming_section_expanded(self):
        """generic-re must carry the full 6-rule naming summary, not just 1 line."""
        skill = self.reg.get("generic-re")
        self.assertIsNotNone(skill)
        body = skill.body
        self.assertIn("Variables: snake_case", body)
        self.assertIn("Enums", body)
        self.assertIn("/naming-convention", body)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/tools/test_skills.py::TestBuiltinTriggerMatching::test_malware_analysis_skill_naming_section_expanded tests/tools/test_skills.py::TestBuiltinTriggerMatching::test_generic_re_skill_naming_section_expanded -v`
Expected: Both FAIL — current sections only cover 3 rules / 1 line.

- [ ] **Step 3: Update `malware-analysis/SKILL.md`**

In `lucnhan/skills/builtins/malware-analysis/SKILL.md`, replace the `## Naming Conventions` block (currently lines 88-92):

```markdown
## Naming Conventions

- Functions: PascalCase verb-noun (InitializeGlobals, StealDiscordTokens)
- Variables: snake_case (buffer_offset, bytes_read)
- Globals: g_ prefix + camelCase (g_bEnabled, g_pConfigStart, g_C2ServerUrl)
- Structs: PascalCase; fields snake_case (BrowserConfig, connection_timeout)
- Enums: PascalCase type + UPPER_SNAKE members (MessageType, MSG_TYPE_HANDSHAKE)
- For edge cases / uncertain names, see the /naming-convention skill.
```

- [ ] **Step 4: Update `generic-re/SKILL.md`**

In `lucnhan/skills/builtins/generic-re/SKILL.md`, replace the single naming line (currently line 52, `- Naming conventions: PascalCase for functions, g_ prefix for globals, PascalCase for structs`) with:

```markdown
- Naming conventions:
  - Functions: PascalCase verb-noun (InitializeGlobals, ParseHttpRequest)
  - Variables: snake_case (buffer_offset, bytes_read)
  - Globals: g_ prefix + camelCase (g_bEnabled, g_pConfigStart, g_C2ServerUrl)
  - Structs: PascalCase; fields snake_case (BrowserConfig, connection_timeout)
  - Enums: PascalCase type + UPPER_SNAKE members (MessageType, MSG_TYPE_HANDSHAKE)
  - For edge cases / uncertain names, see the /naming-convention skill.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/tools/test_skills.py::TestBuiltinTriggerMatching -v`
Expected: ALL tests PASS (new + existing).

- [ ] **Step 6: Commit**

```bash
git add lucnhan/skills/builtins/malware-analysis/SKILL.md lucnhan/skills/builtins/generic-re/SKILL.md tests/tools/test_skills.py
git commit -m "docs(skills): sync malware-analysis + generic-re naming sections to 6-rule standard"
```

---

## Task 5: CHANGELOG entry + full CI verification

**Files:**
- Modify: `CHANGELOG.md`
- Test: full `./ci-local.sh`

**Interfaces:**
- Produces: a documented breaking-change note for the bulk_renamer PascalCase switch.
- Consumes: all prior tasks.

- [ ] **Step 1: Add the CHANGELOG entry**

In `CHANGELOG.md`, under the topmost `## [1.6.0] — 2026-07-02` section (or the next unreleased section if the version has moved), add inside `### Added`:

```markdown
- `naming-convention` skill (`lucnhan/skills/builtins/naming-convention/`) — comprehensive naming standard covering functions, variables, globals, structs, enums, and typedefs, plus edge cases (wrappers/thunks, C++ mangling, Go/Rust, vtable) and a confidence-based escalation ladder with `Unknown_<Hint>_<addr>` placeholders.
```

And add a new `### Changed` subsection (create it if absent, after `### Added`) under the same version:

```markdown
### Changed
- **BREAKING (behavior):** `bulk_renamer` Quick and Deep prompts now generate PascalCase function names (`InitializeGlobals`) instead of snake_case (`initialize_globals`). This unifies bulk-rename output with the system prompt and the new `naming-convention` skill. Existing IDBs are NOT migrated — old snake_case names persist; only new renames follow the standard. If you relied on snake_case output from Bulk Rename, regenerate names for affected functions.
- `RENAMING_SECTION` in the system prompt (`lucnhan/agent/prompts/base.py`) expanded from 3 naming rules to 6 (now covers variables, enums, typedefs) and references the `/naming-convention` skill for edge cases. Also removes the ghost-tool reference to `rename_multi_variables` (which never existed).
- `malware-analysis` and `generic-re` skills: naming sections expanded from 1-3 rules to the full 6-rule summary, cross-referencing `/naming-convention`.
```

- [ ] **Step 2: Run the full local CI**

Run: `./ci-local.sh`
Expected: All stages pass — ruff format, ruff check, mypy, pytest, desloppify (score within 0.5 of baseline 89.0).

If desloppify score drops >0.5: review the new files for any slop patterns (unused code, dead branches, vague comments) and fix before committing. The skill markdown is documentation and should not affect the objective score; if it does, the issue is likely in the Python test files.

- [ ] **Step 3: Run the targeted test suites once more for certainty**

Run: `python -m pytest tests/agent/test_system_prompt.py tests/agent/test_bulk_renamer_prompts.py tests/tools/test_skills.py -v`
Expected: ALL tests PASS.

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): note naming-convention unification + bulk_renamer PascalCase switch"
```

- [ ] **Step 5: Final verification — grep for the old inconsistency**

Run these three checks; all must return no matches:

```bash
grep -rn "Use snake_case naming convention" lucnhan/
grep -rn "using snake_case convention" lucnhan/
grep -rn "Use rename_multi_variables when available" lucnhan/
```

Expected: all three return nothing. If any returns a match, a prompt or skill was missed — fix it before considering the plan done.

---

## Self-Review (run after writing this plan — already done, issues fixed inline)

**1. Spec coverage:** Every spec section maps to a task:
- Skill (Tầng 2) → Task 1
- Baseline RENAMING_SECTION (Tầng 1) → Task 2
- bulk_renamer (Tầng 3) → Task 3
- Sync 2 existing skills → Task 4
- CHANGELOG + CI → Task 5
- rename_multi_variables ghost tool → handled in Task 2 (baseline) + Task 2 test
- Test reliability fix → Task 3 uses exact phrases + positive checks

**2. Placeholder scan:** No TBD/TODO. Every code block is complete and copy-pasteable.

**3. Type consistency:** All referenced tools (`rename_function`, `rename_variable`, `rename_address`, `decompile_function`, `xrefs_to`, `search_strings`, `search_imports`, `imports_by_module`, `function_xrefs`, `suggest_struct_from_accesses`, `get_decompiler_variables`, `create_struct`, `create_enum`, `set_comment`, `set_function_comment`, `save_memory`) verified to exist in `lucnhan/ida/tools/`. `rename_multi_variables` confirmed NOT to exist — handled as a removal.

**4. Trigger isolation:** Task 1 includes `test_naming_convention_does_not_steal_analysis_queries` to prevent the new skill's triggers from hijacking generic-re / malware-analysis queries (the same regression class that `test_vuln_audit_still_wins_own_queries` guards).
