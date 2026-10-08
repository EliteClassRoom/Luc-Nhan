# Naming Convention Standard — Design Spec

**Date:** 2026-07-02
**Status:** Approved (Approach 3 — Hybrid ba tầng)
**Branch target:** `feat/naming-convention`
**Origin:** Khám phá mâu thuẫn naming convention giữa system prompt và bulk_renamer

---

## Context

Luc Nhan hiện có naming convention **mâu thuẫn nội bộ**:

| Nguồn | File:line | Phong cách |
|-------|-----------|------------|
| System prompt (chat) | `lucnhan/agent/prompts/base.py:60` | **PascalCase** function |
| Skill `malware-analysis` | `lucnhan/skills/builtins/malware-analysis/SKILL.md:90` | **PascalCase** function |
| Skill `generic-re` | `lucnhan/skills/builtins/generic-re/SKILL.md:52` | **PascalCase** function |
| Bulk renamer Quick | `lucnhan/agent/bulk_renamer.py:29` | **snake_case** function ⚠️ |
| Bulk renamer Deep | `lucnhan/agent/bulk_renamer.py:60` | **snake_case** function ⚠️ |

Hậu quả: khi user dùng Bulk Rename widget, agent sinh tên `snake_case`; khi chat
thường, agent sinh `PascalCase` → **cùng một IDB có 2 phong cách tên lộn xộn**,
không thể undo tự động vì IDA lưu tên vào database.

Ngoài ra, quy chuẩn hiện tại chỉ **3 quy tắc naming** (`RENAMING_SECTION`
dòng 49-63, phần convention chỉ 3 dòng 60-62), chỉ cover 3 loại đối tượng
(function/global/struct), thiếu edge case và quy tắc khi thiếu evidence.

**Lưu ý bug hiện có:** `RENAMING_SECTION` dòng 56 và `research.py:158`/
`exploration_mode.py:300` reference tool `rename_multi_variables`, nhưng tool
này **không tồn tại** trong `lucnhan/ida/tools/` (chỉ có `rename_function`,
`rename_variable`, `rename_address`). Đây là tool ma được kế thừa — baseline
mới phải **loại bỏ** reference này, không kế thừa bug.

## Decisions (đã chốt qua brainstorming)

1. **Mục đích**: Định nghĩa bộ quy chuẩn phong phú từ đầu (không chỉ sửa mâu thuẫn).
2. **Phạm vi**: Toàn diện — mọi đối tượng IDA có thể đặt tên (function, variable,
   global, struct & UDT, enum, type/typedef).
3. **Cơ chế áp dụng**: Hybrid — System Prompt giữ baseline rút gọn + Skill riêng
   `naming-convention` chứa bộ đầy đủ. Không thêm tool validation (YAGNI).
4. **Phong cách**: PascalCase verb-noun cho function (phân biệt với C runtime),
   snake_case cho variable/struct field, `g_` prefix cho global.

## Architecture: Hybrid ba tầng

Mô hình 3 tầng liên kết, theo pattern `ida-scripting` đã có trong codebase:

```
┌─────────────────────────────────────────────────────────────┐
│ Tầng 1: RENAMING_SECTION (base.py)                           │
│   Baseline luôn active trong mọi system prompt               │
│   ~12 dòng, ~200 token                                       │
│   → đủ cho 90% trường hợp rename thường                      │
└──────────────────────────┬──────────────────────────────────┘
                           │ "(xem skill naming-convention
                           │  cho edge cases)"
                           ▼
┌─────────────────────────────────────────────────────────────┐
│ Tầng 2: Skill naming-convention/SKILL.md                     │
│   Bộ quy chuẩn đầy đủ, on-demand                             │
│   ~150-200 dòng + references/naming-examples.md              │
│   → agent activate khi gặp edge case hoặc cần tra cứu        │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ Tầng 3: bulk_renamer.py (Quick + Deep prompts)               │
│   Standalone prompts (không load skill)                      │
│   → phải tự chứa quy tắc cốt lõi, tham chiếu cùng quy chuẩn │
└─────────────────────────────────────────────────────────────┘
```

**Nguyên tắc DRY:** Quy chuẩn **chính thức** nằm ở 1 nơi duy nhất — skill
`naming-convention`. Tầng 1 và tầng 3 chỉ chứa **bản tóm tắt** + reference đến
skill. Khi quy chuẩn thay đổi, chỉ cần sửa skill; bản tóm tắt giữ ổn định.

**Vì sao giữ bản tóm tắt ở 3 nơi thay vì 1:** bulk_renamer prompt là standalone
single-shot (không load skill vào context — `_quick_llm_call` dòng 632 chỉ gửi 1
message). Nếu bỏ quy tắc cốt lõi ra khỏi bulk_renamer, agent sẽ không biết
PascalCase khi bulk rename. Tương tự system prompt phải có baseline vì agent
không thể đoán trước khi nào cần rename. Trade-off giữa DRY hoàn toàn và
self-containment.

## Files thay đổi

| File | Loại | Mô tả |
|------|------|-------|
| `lucnhan/agent/prompts/base.py` | Sửa | Expand `RENAMING_SECTION` từ 3 → ~12 dòng (baseline) |
| `lucnhan/skills/builtins/naming-convention/SKILL.md` | **Mới** | Bộ quy chuẩn đầy đủ + frontmatter |
| `lucnhan/skills/builtins/naming-convention/references/naming-examples.md` | **Mới** | Before/after examples, edge cases |
| `lucnhan/agent/bulk_renamer.py` | Sửa | `QUICK_ANALYSIS_PROMPT` + `DEEP_ANALYSIS_PROMPT`: snake_case → PascalCase |
| `lucnhan/skills/builtins/malware-analysis/SKILL.md` | Sửa (nhỏ) | Đồng bộ naming section với quy chuẩn mới |
| `lucnhan/skills/builtins/generic-re/SKILL.md` | Sửa (nhỏ) | Đồng bộ naming section với quy chuẩn mới |
| `tests/agent/test_system_prompt.py` | Sửa | Thêm test xác nhận `RENAMING_SECTION` mới có keywords |

## Naming conventions by object type

### Functions — PascalCase verb-noun

- **Verb-noun bắt buộc**: `ParseHttpRequest`, không `HttpRequestParser`,
  không `http_request_parser`.
- **Subsystem prefix (optional, default KHÔNG dùng)**: chỉ thêm khi (a) binary
  có module rõ ràng (vtable/dispatch table identifiable) VÀ (b) tên gốc của hàm
  không leak subsystem. Ví dụ hợp lệ: `CryptoAesDecrypt` (khi binary có crypto
  module tách biệt). Mặc định: giữ tên đơn giản (`AesDecrypt`).
- **Đại từ**: tránh `My`, `This`, `The` — dùng ngữ cảnh: `DecryptConfig`,
  không `DecryptMyConfig`.

### Variables — snake_case

- `snake_case` lowercase, underscore separator.
- Parameter: cùng quy tắc. **KHÔNG** prefix `p_`/`a_` (IDA tự sinh `aX`, `v1` —
  khi rename, bỏ prefix auto).
- **KHÔNG Hungarian notation** (`bEnabled`, `dwSize`) — tên mô tả mục đích, không
  mô tả kiểu. **Lý do:** local variable có decompiler scope, kiểu được infer rõ
  từ khai báo → Hungarian thừa. (Khác global — xem bên dưới.)
- Boolean: `is_`/`has_`/`should_` prefix (`is_initialized`, `has_pending_data`).

### Globals — `g_` prefix + camelCase

- `g_` prefix + camelCase body: `g_C2ServerUrl`, `g_pConfigStart`, `g_bEnabled`.
- **Giữ Hungarian nhẹ** cho global (`g_p` = pointer, `g_b` = bool, `g_dw` =
  dword). **Lý do:** global khó infer kiểu từ context (không có decompiler
  scope cục bộ) → Hungarian cung cấp signal hữu ích. Đây là khác biệt có chủ đích
  so với local variable (bỏ Hungarian).
- Section name / vtable pointer: `g_vtable_<ClassName>`.

### Structs & UDT — PascalCase name, snake_case field

- Struct name: PascalCase noun (`DnsConfig`, `TcpConnectionState`).
- **Struct field**: snake_case (`connection_timeout`, `buffer_size`) — phù hợp
  C convention.
- C++ class: giữ `C` prefix nếu binary dùng MFC/ATL (`CFooMgr`); nếu không, bỏ
  prefix.
- Union: `union_<Purpose>` hoặc PascalCase + comment.
- Nested/anonymous: comment `// anonymous struct for ...`.

### Enums — PascalCase type + UPPER_SNAKE members

- Enum type name: PascalCase (`SocketState`, `MessageType`).
- Enum member: `UPPER_SNAKE_CASE`, prefix bằng enum name viết tắt:
  `SOCK_CONNECTED`, `MSG_TYPE_HANDSHAKE`.
- Flag/bitmask: `FLAG_` prefix (`FLAG_READ`, `FLAG_WRITE`).
- IDA 9.x: enum qua `ida_typeinf` BTF_ENUM — KHÔNG dùng `ida_enum` (đã remove).

### Type / Typedef — PascalCase

- Function pointer typedef: PascalCase + suffix `Cb` (callback) hoặc `Fn`
  (function): `TimerCallback`, `AllocFn`.
- Standard typedef: PascalCase noun, KHÔNG `_t` (tránh đụng C stdint `uint32_t`):
  `SocketHandle`, `ConnectionId`.
- Function prototype: dùng `set_function_prototype` với C syntax chuẩn.

## Edge cases

| Tình huống | Quy tắc | Ví dụ |
|------------|---------|-------|
| **Jump-thunk** (jump-only, không stack frame) | `j_<Orig>` prefix | `j_malloc` |
| **Call-thunk** (có stack frame nhỏ, setup rồi call) | `thunk_<Orig>` prefix | `thunk_CreateFile` |
| **Logic-wrapper** (thêm logic trước/sau: logging, mutex, error check) | `<Orig>Wrapper` suffix | `MallocWrapper` |
| **C++ name mangling** (binary có symbols) | Demangle trước, giữ nguyên signature nếu là public method | `std::vector<int>::push_back` → giữ nguyên |
| **C++ nhưng stripped** (không RTTI/symbols) | Treat như C function, không áp dụng C++ rule | — |
| **Go binary** | `go_<pkg>_<FuncName>`: package + function, bỏ receiver | `go_main_ConnectC2`, `go_net_ResolveDomain` |
| **Rust binary** | Giữ snake_case gốc nếu unmangled; nếu mangled, demangle Rust style | `rust_std_panicking_panic` |
| **vtable method** | `<Class>__<Method>`: double underscore | `CHttpClient__SendRequest` |
| **Callback đã biết** | `<Event>Callback` | `WindowProc`, `TimerCallback` |
| **Entry point** | Giữ `main`/`wWinMain`, rename wrapper thành `Entry_*` | `Entry_RealMain` |
| **Thunk import** | `__imp_` giữ nguyên (IDA auto), KHÔNG rename | — |

**Quy tắc quyết định wrapper/thunk (chốt sau self-review):**

| Đặc điểm code | Prefix/suffix | Ví dụ |
|----------------|---------------|-------|
| Chỉ `jmp` tới target, không stack frame | `j_<Orig>` | `j_malloc` |
| Có stack frame nhỏ, setup rồi `call` | `thunk_<Orig>` | `thunk_CreateFile` |
| Thêm logic (logging, mutex, error check, transform arg) | `<Orig>Wrapper` | `MallocWrapper` |

## Confidence-based naming (escalation ladder)

Khi decompile xong mà vẫn `<70% chắc chắn`, **đừng rename vội**. Leo thang
evidence theo thứ tự (rẻ → đắt):

**Level 0 — Đọc lại cẩn thận (gần như free)**
- Decompile full + đọc cả disassembly chunk.
- Đặt **repeatable comment** ghi hypothesis trước: `// hypothesis: RC4 key
  schedule (evidence: loops 256, byte swap)` — comment dễ sửa hơn tên sai.
- Đánh giá: có clear "verb" không? Nếu không rõ verb → chưa đủ evidence.

**Level 1 — Call graph context (rẻ)**
- `xrefs_to`: ai gọi nó? Nếu caller là `main`/`init_*` → khả năng cao là init
  routine.
- `xrefs_from` + `function_xrefs` depth 2: nó gọi ai? Nếu gọi nhiều
  `socket`/`send`/`recv` → network.
- Topology: hub (nhiều caller) = utility; leaf (không gọi ai) = primitive;
  dispatcher (switch lớn) = handler.
- **Heuristic:** tên function thường leak từ **callers nhiều hơn từ body**.

**Level 2 — Semantic leak (trung bình)**
- `search_strings` + `xrefs_to` string ea gần function: error messages, log
  strings, format strings leak purpose (`"Failed to decrypt"`, `"http://%s:%d"`).
- `imports_by_module` + `search_imports`: nếu function chỉ gọi
  `CreateFileW`/`WriteFile`/`CloseHandle` → file I/O subsystem.
- Pattern set matching: nhóm import đặc trưng → subsystem xác định (crypto:
  `CryptAcquireContext`+`BCrypt*`; registry: `RegOpenKey`+`RegQueryValue`).

**Level 3 — Constant / magic matching (mạnh)**
- Magic constants identifies algorithm gần như chắc chắn:
  - `0xEDB88320` / `0x04C11DB7` → CRC32 polynomial
  - AES S-box bytes (`0x63, 0x7c, 0x77, ...`) → AES
  - SHA-256 init `0x6a09e667` / MD5 `0x67452301` → hash
  - RC4 KSA pattern: `for i in 256 { swap }`
- `search_strings` cho constants hex, hoặc đọc raw bytes từ `.rdata`.
- **Vì sao Level 3 đáng tin hơn Level 0:** Decompiler output có thể bị
  obfuscation làm sai lệch (MBA, CFF). Nhưng magic constants nằm trong `.rdata`
  — chúng là data, không phải code, obfuscation hiếm khi che chúng.

**Level 4 — Struct reconstruction (mạnh, đắt)**
- `suggest_struct_from_accesses`: reconstruct data layout từ pointer access
  pattern → field names leak purpose.
- `get_decompiler_variables`: retyping local var làm decompiler rõ hơn → mới
  infer được verb.

**Level 5 — Deep analysis (đắt nhất)**
- Spawn subagent (`/explore` mode, hoặc deep bulk_renamer) với 8 turns, full
  tool access, chase xrefs 2-3 levels.
- External comparison: Diaphorum/BinDiff match với binary đã analyze hoặc
  open-source build → mượn tên đã verified.

### Confidence decision matrix

| Confidence | Hành động | Tên |
|------------|-----------|-----|
| **>90%** | Rename ngay | `ParseHttpRequest` |
| **70-90%** | Rename + repeatable comment ghi evidence | `DecryptConfig // ev: calls RC4, keysched at 0x4013` |
| **50-70%** | **KHÔNG rename** — dùng placeholder | `Unknown_<Hint>_<addr>` (xem bên dưới) |
| **<50%** | Để nguyên `sub_XXXX`, ghi `save_memory(category=hypothesis)` | (giữ `sub_XXXX`) |

### Placeholder convention (chốt: option b)

Cho confidence 50-70%, dùng prefix `Unknown_<Hint>_<addr>`:

- Format: `Unknown_` + PascalCase hint + `_` + **hex lowercase, không `0x`**.
  Ví dụ: `Unknown_HashFunc_4012a0`, `Unknown_StringOp_4012a0`.
- **Lý do chọn (b) thay vì comment-only:** IDA function list panel chỉ hiển thị
  tên, không hiển thị comment. Prefix `Unknown_` biến function list thành kanban
  board tự nhiên — sort theo tên, toàn bộ `Unknown_*` nhóm lại, dễ review.
- **Tương thích `_AUTO_NAME_PATTERNS`:** regex trong `bulk_renamer.py:72-78`
  (chỉ skip `sub_`/`FUN_`/`func_`/`unnamed_`/`loc_`) **không** match `Unknown_`
  → bulk_renamer treat là human-assigned → không tự ý rename đè. Đây là hành vi
  mong muốn (feature, không phải bug): analyst muốn rename lại `Unknown_` khi có
  evidence mới → rename thủ công.
- **Progressive renaming:** khi rename 1 function, kiểm tra lại callers xem
  hypothesis có nhất quán không. Nếu rename `ParseConfig` mà caller
  `sub_402000` bổng đọc có nghĩa → confirmation positive. Nếu caller vẫn vô
  nghĩa → có thể tên sai, revert.

## Skill structure (Tầng 2)

### Frontmatter

```yaml
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
  - snake_case or pascalcase
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
```

### Body outline (7 phần)

1. **Pre-rename checklist** — form hypothesis, gather evidence.
2. **Naming conventions by object type** — 6 bảng (function/variable/global/
   struct/enum/type) với rule + ví dụ.
3. **Edge cases table** — wrapper/thunk, C++ mangling, Go, Rust, vtable,
   callback, entry, thunk-import.
4. **Confidence-based decision matrix** — bảng 4 mức + hành động.
5. **Escalation ladder** — 6 levels (0-5) khi không chắc chắn.
6. **Placeholder convention** — quy tắc `Unknown_<Hint>_<addr>`.
7. **Cross-references** — link tới `malware-analysis`, `generic-re`,
   `ida-scripting`.

### Reference file (lazy-load)

`references/naming-examples.md` — thư viện before/after examples theo tình huống:
- Single function rename (PE malware)
- Bulk rename batch (stripped Go binary)
- Struct reconstruction + field naming
- Wrapper/thunk chain (IAT resolution)
- Crypto identification via magic constant

## bulk_renamer.py thay đổi (Tầng 3)

### `QUICK_ANALYSIS_PROMPT` (mới)

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

### `DEEP_ANALYSIS_PROMPT` (mới)

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

**Giữ nguyên output format** (`0x<addr> <name>` / `RENAME:`): regex parse tại
`_quick_llm_call` (dòng 643, `r"^0x([0-9a-fA-F]+)\s+(\S+)$"`) và
`_run_deep_common` (dòng 742) **không đổi** → không phá logic. Regex `\S+`
match `Unknown_Foo_4012a0` OK.

## Baseline RENAMING_SECTION (Tầng 1)

`lucnhan/agent/prompts/base.py` dòng 49-63 expand từ 3 → ~12 dòng:

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

## Đồng bộ 2 skills hiện có

### `malware-analysis/SKILL.md` (dòng 88-92)

```markdown
## Naming Conventions
- Functions: PascalCase verb-noun (InitializeGlobals, StealDiscordTokens)
- Variables: snake_case (buffer_offset, bytes_read)
- Globals: g_ prefix + camelCase (g_bEnabled, g_pConfigStart, g_C2ServerUrl)
- Structs: PascalCase; fields snake_case (BrowserConfig, connection_timeout)
- Enums: PascalCase type + UPPER_SNAKE members (MessageType, MSG_TYPE_HANDSHAKE)
- For edge cases / uncertain names, see the /naming-convention skill.
```

### `generic-re/SKILL.md` (dòng 52)

Cùng nội dung 6 dòng như trên, thay cho 1 dòng hiện tại.

## Tests

### `tests/agent/test_system_prompt.py` — thêm 2 test

```python
def test_renaming_section_has_full_convention(self):
    """Baseline RENAMING_SECTION covers all 6 object types."""
    from lucnhan.agent.prompts.base import RENAMING_SECTION
    assert "PascalCase" in RENAMING_SECTION      # functions
    assert "snake_case" in RENAMING_SECTION      # variables
    assert "g_" in RENAMING_SECTION              # globals
    assert "Enum" in RENAMING_SECTION            # enums

def test_renaming_section_references_skill(self):
    """Baseline points to the naming-convention skill for edge cases."""
    from lucnhan.agent.prompts.base import RENAMING_SECTION
    assert "naming-convention" in RENAMING_SECTION
```

### Skill discovery test (`tests/tools/test_skills.py`)

Xác nhận skill mới được discover với đúng slug `naming-convention`.

### bulk_renamer regression check

```python
def test_bulk_renamer_prompts_use_pascalcase(self):
    """Bulk renamer must use PascalCase, not snake_case (regression guard).

    Checks the EXACT original snake_case phrases so the test fails loudly if
    anyone reverts. Positive checks confirm the prompts actively enforce
    PascalCase. Avoids brittle substring checks on the word "snake_case"
    (which legitimately appears in "NEVER snake_case" guidance).
    """
    from lucnhan.agent.bulk_renamer import QUICK_ANALYSIS_PROMPT, DEEP_ANALYSIS_PROMPT
    # Negative: original snake_case directives must be gone (exact phrases)
    assert "Use snake_case naming convention" not in QUICK_ANALYSIS_PROMPT
    assert "using snake_case convention" not in DEEP_ANALYSIS_PROMPT
    # Positive: PascalCase is now the stated convention
    assert "PascalCase" in QUICK_ANALYSIS_PROMPT
    assert "PascalCase" in DEEP_ANALYSIS_PROMPT
    # Positive: prompts actively forbid snake_case
    assert "NEVER snake_case" in QUICK_ANALYSIS_PROMPT
    assert "NEVER snake_case" in DEEP_ANALYSIS_PROMPT
```

## Validation & verification

**Local CI (`./ci-local.sh` trước push):**
- `ruff format` + `ruff check` — format Python.
- `mypy lucnhan/core lucnhan/providers` — type check (không ảnh hưởng vì chỉ
  sửa string + markdown).
- `pytest tests/agent/test_system_prompt.py` — test mới.
- `pytest tests/tools/test_skills.py` — skill discovery.
- `desloppify scan` — objective score không giảm >0.5.

**Manual smoke test (khuyến nghị):**
1. Mở IDA với sample binary (stripped PE).
2. Chat "rename this function" → xác nhận output PascalCase.
3. Bulk Rename widget (Quick mode) → xác nhận output PascalCase.
4. `/naming-convention` hoặc trigger tự nhiên → xác nhận skill load được.
5. Decompile 1 wrapper → xác nhận agent dùng `j_`/`thunk_`/`Wrapper`.

## Risks & mitigations

| Rủi ro | Mức | Mitigation |
|--------|-----|------------|
| **IDB cũ có tên snake_case** từ bulk_renamer cũ | THẤP | Không migrate tự động (nguy hiểm). Quy chuẩn mới chỉ áp dụng cho rename mới. Ghi CHANGELOG. |
| **Agent quên activate skill** cho edge case | TRUNG BÌNH | Baseline đủ dùng cho 90% case. Skill có triggers phong phú → auto-suggest. |
| **Bulk_renamer prompt dài hơn** (~100 token) | THẤP | Acceptable — bulk rename batch vốn đã lớn (180k chars cap). |
| **LLM không tuân thủ PascalCase** dù prompt rõ | THẤP | Hành vi LLM, không phải bug code. Confidence placeholder + re-decompile verify là safety net. |
| **`Unknown_` không match `_AUTO_NAME_PATTERNS`** → bulk_renamer skip | **CÓ Ý CHỦ ĐẠO** | Feature: analyst rename `Unknown_` thủ công khi có evidence mới. Future enhancement: thêm pattern tuỳ chọn. |
| **`rename_multi_variables` là tool ma** (không implement) | **CAO** | Baseline mới **loại bỏ** reference. Không kế thừa bug. Nếu sau này cần bulk variable rename, implement tool thật rồi mới reference lại. |

## Backward compatibility

- **Không phá tool signature**: `rename_function`, `rename_variable`,
  `rename_address` — interface không đổi.
- **Không phá mutation tracking**: `mutation.py` không động vào — undo vẫn hoạt động.
- **Không phá API IDA**: chỉ đổi prompt string + thêm markdown skill.
- **System prompt test**: `_BASE_PROMPT` test hiện có phải vẫn pass.

## Rollout sequence

```
Phase 1: Tạo skill (Tầng 2) — không phá gì
  └─ lucnhan/skills/builtins/naming-convention/SKILL.md
  └─ lucnhan/skills/builtins/naming-convention/references/naming-examples.md
  └─ Test: pytest tests/tools/test_skills.py

Phase 2: Expand baseline (Tầng 1) — thay đổi system prompt
  └─ lucnhan/agent/prompts/base.py: RENAMING_SECTION (3 → ~12 dòng)
  └─ Test: pytest tests/agent/test_system_prompt.py

Phase 3: Đồng bộ bulk_renamer (Tầng 3) — sửa inconsistency
  └─ lucnhan/agent/bulk_renamer.py: QUICK + DEEP prompts
  └─ Test: grep -i snake_case bulk_renamer.py (phải empty)

Phase 4: Đồng bộ 2 skills hiện có — DRY cleanup
  └─ malware-analysis/SKILL.md, generic-re/SKILL.md
  └─ Test: manual review

Phase 5: Tests bổ sung — regression safety
  └─ tests/agent/test_system_prompt.py: 2 test mới
  └─ Test: ./ci-local.sh pass full

Phase 6: CHANGELOG entry — backward compat note
  └─ CHANGELOG.md: note về naming convention unification
```

Mỗi phase có verification riêng → có thể dừng giữa chừng nếu có vấn đề.
