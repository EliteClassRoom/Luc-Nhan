# Document Standardization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Chuẩn hóa documentation Luc Nhan: sửa stale facts (skills/tools count, broken tools table), sửa cấu trúc thư mục sai trong AGENTS.md, dọn dẹp docs/ mâu thuẫn, đánh dấu status cho design spec chưa implement.

**Architecture:** Pure-doc task — 7 file edit + 1 file delete, không chạm code. Mỗi task là một file/cluster độc lập, commit riêng. Verification bằng `grep` (không cần chạy test vì không đổi code).

**Tech Stack:** Markdown (`AGENTS.md`, `ARCHITECTURE.md`, `llms.txt`, `docs/*.md`, `lucnhan/plans/*.md`). Git.

**Reference spec:** `docs/superpowers/specs/2026-06-16-document-standardization-design.md`

---

## Ground Truth (đã verify 2026-06-16 — không thay đổi trong lúc làm)

| Fact | Value |
|------|-------|
| Built-in skills | **12** (12 dir trong `lucnhan/skills/builtins/` + `__init__.py`) |
| `@tool` defs | **73** |
| IDA tool impls location | `lucnhan/ida/tools/` (13 file) |
| Framework + helpers location | `lucnhan/tools/` (11 file: base, registry, coercion, cache, formatting, pagination, value_format, script_guard, web, web_fetch, xrefs) |
| 4 "broken tools" | **Tất cả đã fix** (migrate `ida_typeinf`) |
| `lucnhan/plans/web_researcher_*` | **Chưa implement** (0 hit `grep` trong `*.py`) |

---

## Task 1: Sửa skill count 10 → 12 trong AGENTS.md

**Files:**
- Modify: `AGENTS.md:95`

- [ ] **Step 1: Sửa comment trong tree**

Edit `AGENTS.md`, thay:

```
│   └── builtins/             # 10 built-in skills
```

bằng:

```
│   └── builtins/             # 12 built-in skills
```

- [ ] **Step 2: Verify**

Run: `grep -n "10 built-in" AGENTS.md`
Expected: no output (0 match)

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "docs: fix built-in skill count 10 → 12 in AGENTS.md"
```

---

## Task 2: Sửa llms.txt (skills 10→12, tools 56→60+, xóa broken-tools table)

**Files:**
- Modify: `llms.txt` (lines 9, 10, 37, 57-71, 100-107)

- [ ] **Step 2.1: Sửa tool count (line 9)**

Edit `llms.txt`, thay:

```
- **Tools**: 56+ tools for IDA. Defined with the `@tool` decorator in `lucnhan/ida/tools/`. Categories: navigation, functions, strings, database, disassembly, decompiler, xrefs, annotations, types, scripting, microcode.
```

bằng:

```
- **Tools**: 60+ tools for IDA. Defined with the `@tool` decorator in `lucnhan/ida/tools/`. Categories: navigation, functions, strings, database, disassembly, decompiler, xrefs, annotations, types, scripting, microcode.
```

- [ ] **Step 2.2: Sửa skill count (line 10)**

Edit `llms.txt`, thay:

```
- **Skills**: Markdown files with YAML frontmatter in `lucnhan/skills/builtins/`. Activated with `/<slug>`. 10 built-in skills.
```

bằng:

```
- **Skills**: Markdown files with YAML frontmatter in `lucnhan/skills/builtins/`. Activated with `/<slug>`. 12 built-in skills.
```

- [ ] **Step 2.3: Sửa skill count trong File Layout (line 37)**

Edit `llms.txt`, thay:

```
├── skills/         # Skill discovery, loading, 10 built-in skills
```

bằng:

```
├── skills/         # Skill discovery, loading, 12 built-in skills
```

- [ ] **Step 2.4: Thêm 2 skill còn thiếu vào bảng Built-in Skills**

Edit `llms.txt`, thay block (sau `/ida-scripting`, trước `/modify`):

```
| `/ida-scripting` | IDAPython API reference |
| `/modify` | Exploration mode skill for binary modification |
| `/smart-patch-ida` | IDA-specific binary patching workflow |
```

bằng:

```
| `/ida-scripting` | IDAPython API reference |
| `/ida-docs` | Search and browse official IDA Pro documentation |
| `/ida-pro-mcp` | IDAPython scripting reference — disassembly, decompilation, types, xrefs |
| `/modify` | Exploration mode skill for binary modification |
| `/smart-patch-ida` | IDA-specific binary patching workflow |
```

- [ ] **Step 2.5: Xóa bảng "Known Broken IDA Tools"**

Edit `llms.txt`, xóa toàn bộ block cuối file (từ heading đến hết):

```
## Known Broken IDA Tools

| Tool | Error |
|------|-------|
| `create_struct` | `ida_struct` removed in IDA 9.x — needs `ida_typeinf` migration |
| `import_c_header` | `idc` not imported in handler |
| `set_function_prototype` | `idc` not imported in handler |
| `apply_type_to_variable` | Decompiler guard fires incorrectly |
```

(4 tool này đều đã fix — `create_struct` migrated sang `ida_typeinf`, 3 tool kia dùng `idc` đã được import qua `importlib` loop.)

- [ ] **Step 2.6: Verify llms.txt**

Run: `grep -nE "10 built-in|56\+ tools|~56 tools|Known Broken" llms.txt`
Expected: no output (0 match)

Run: `grep "ida-docs\|ida-pro-mcp" llms.txt`
Expected: đúng 2 dòng (2 skill mới đã thêm vào bảng).

- [ ] **Step 2.7: Commit**

```bash
git add llms.txt
git commit -m "docs: fix llms.txt skill/tool counts and remove stale broken-tools table"
```

---

## Task 3: Sửa tool count trong ARCHITECTURE.md

**Files:**
- Modify: `ARCHITECTURE.md:262`

- [ ] **Step 3.1: Sửa tool count**

Edit `ARCHITECTURE.md`, thay:

```
Each host provides ~56 tools organized by category:
```

bằng:

```
Each host provides 60+ tools organized by category:
```

- [ ] **Step 3.2: Verify**

Run: `grep -n "~56 tools" ARCHITECTURE.md`
Expected: no output (0 match)

- [ ] **Step 3.3: Commit**

```bash
git add ARCHITECTURE.md
git commit -m "docs: update tool count to 60+ in ARCHITECTURE.md"
```

---

## Task 4: Sửa cấu trúc thư mục `lucnhan/tools/` trong AGENTS.md

Đây là fix quan trọng nhất — AGENTS.md liệt kê IDA tool impls ở sai chỗ
(nói `lucnhan/tools/navigation.py` nhưng thực ra nằm ở `lucnhan/ida/tools/`).

**Files:**
- Modify: `AGENTS.md:38-60` (2 block: `ida/tools/` và `tools/`)

- [ ] **Step 4.1: Mở rộng block `ida/tools/` để liệt kê IDA impls**

Edit `AGENTS.md`, thay:

```
│   ├── tools/
│   │   └── registry.py       # IDA create_default_registry() — imports lucnhan.tools.*
│   └── ui/
```

bằng:

```
│   ├── tools/                     # IDA tool implementations (host-specific)
│   │   ├── registry.py            # IDA create_default_registry() — imports lucnhan.tools.* lazily
│   │   ├── navigation.py          # IDA navigation tools (cursor, jump, name-at)
│   │   ├── functions.py           # IDA function tools (list, search, info)
│   │   ├── strings.py             # IDA string tools
│   │   ├── database.py            # IDA database tools (segments, imports, exports)
│   │   ├── disassembly.py         # IDA disassembly tools
│   │   ├── decompiler.py          # IDA decompiler tools (Hex-Rays)
│   │   ├── xrefs.py               # IDA xref tools
│   │   ├── annotations.py         # IDA annotation tools (rename, comment, set type)
│   │   ├── types_tools.py         # IDA type tools (structs, enums, typedefs, TILs)
│   │   ├── microcode.py           # IDA Hex-Rays microcode tools
│   │   ├── microcode_format.py    # Microcode formatting helpers
│   │   ├── microcode_optim.py     # Microcode optimizer framework
│   │   └── scripting.py           # IDA execute_python tool
│   └── ui/
```

- [ ] **Step 4.2: Sửa block `tools/` (line 45-60) — chuyển thành framework + shared helpers**

Edit `AGENTS.md`, thay toàn bộ block:

```
├── tools/                    # IDA tool implementations
│   ├── base.py               # @tool decorator, ToolDefinition, JSON schema generation
│   ├── registry.py           # Shared ToolRegistry class
│   ├── navigation.py         # IDA navigation tools
│   ├── functions.py          # IDA function tools
│   ├── strings.py            # IDA string tools
│   ├── database.py           # IDA database tools (segments, imports, exports)
│   ├── disassembly.py        # IDA disassembly tools
│   ├── decompiler.py         # IDA decompiler tools (Hex-Rays)
│   ├── xrefs.py              # IDA xref tools
│   ├── annotations.py        # IDA annotation tools (rename, comment, set type)
│   ├── types_tools.py        # IDA type tools (structs, enums, typedefs, TILs)
│   ├── microcode.py          # IDA Hex-Rays microcode tools
│   ├── microcode_format.py   # Microcode formatting helpers
│   ├── microcode_optim.py    # Microcode optimizer framework
│   └── scripting.py          # IDA execute_python tool
```

bằng:

```
├── tools/                    # Shared tool framework (host-agnostic)
│   ├── base.py               # @tool decorator, ToolDefinition, JSON schema generation
│   ├── registry.py           # Shared ToolRegistry class (registration, dispatch, timeout)
│   ├── coercion.py           # Argument coercion (hex→int, "true"→bool)
│   ├── cache.py              # Tool-level result caching
│   ├── formatting.py         # Shared formatting helpers (function summaries, callers/callees)
│   ├── pagination.py         # Result pagination (page/limit normalization)
│   ├── value_format.py       # Global value + type hint formatting
│   ├── script_guard.py       # execute_python blocklist + sandboxed exec()
│   ├── web.py                # Web tools (web search via MCP)
│   ├── web_fetch.py          # Web fetch tools
│   └── xrefs.py              # Shared cross-reference helpers
```

- [ ] **Step 4.3: Verify không còn IDA impls nào bị liệt kê sai ở `tools/`**

Run: `sed -n '45,62p' AGENTS.md`
Expected: block `tools/` chỉ còn các file framework/helper, KHÔNG có `navigation.py`, `strings.py`, `database.py`, `decompiler.py`, `annotations.py`, `types_tools.py`, `microcode.py`, `microcode_format.py`, `microcode_optim.py`, `scripting.py`.

Run: `grep -n "navigation.py" AGENTS.md`
Expected: duy nhất 1 match nằm trong block `ida/tools/` (không trong block `tools/`).

- [ ] **Step 4.4: Commit**

```bash
git add AGENTS.md
git commit -m "docs: fix AGENTS.md tool directory tree — split IDA impls (ida/tools/) from framework (tools/)"
```

---

## Task 5: Xóa `docs/PROJECT_MODIFICATION_PLAN.md` + sửa cross-ref

**Files:**
- Delete: `docs/PROJECT_MODIFICATION_PLAN.md`
- Modify: `docs/FORK_MIGRATION_ASSESSMENT.md:5`

- [ ] **Step 5.1: Xóa file plan cũ (đã lạc hậu, bị supersede)**

Run:
```bash
git rm docs/PROJECT_MODIFICATION_PLAN.md
```

- [ ] **Step 5.2: Sửa cross-ref trong FORK_MIGRATION_ASSESSMENT.md**

File `docs/FORK_MIGRATION_ASSESSMENT.md:5` đang reference tới file vừa xóa. Edit, thay:

```
> Superseds phần status cũ của [PROJECT_MODIFICATION_PLAN.md](PROJECT_MODIFICATION_PLAN.md) (cập nhật 2026-06-13, nay đã lạc hậu so với git history).
```

bằng:

```
> Supersedes plan cũ (PROJECT_MODIFICATION_PLAN.md, 2026-06-13) — file đó đã xóa vì lạc hậu so với git history.
```

- [ ] **Step 5.3: Verify**

Run: `test -f docs/PROJECT_MODIFICATION_PLAN.md && echo "STILL EXISTS" || echo "DELETED OK"`
Expected: `DELETED OK`

Run: `grep -n "PROJECT_MODIFICATION_PLAN" docs/FORK_MIGRATION_ASSESSMENT.md`
Expected: 1 match (dòng ref mới, không còn link Markdown `[...]()`).

- [ ] **Step 5.4: Commit**

```bash
git add docs/FORK_MIGRATION_ASSESSMENT.md
git commit -m "docs: remove stale PROJECT_MODIFICATION_PLAN, fix cross-ref in FORK_MIGRATION_ASSESSMENT"
```

---

## Task 6: Thêm disclaimer cho EVALUATION_WORKFLOW.md §9 sample data

**Files:**
- Modify: `docs/EVALUATION_WORKFLOW.md:683` (sau heading §9)

- [ ] **Step 6.1: Thêm disclaimer snapshot**

Edit `docs/EVALUATION_WORKFLOW.md`, thay:

```
## 9. Worked Example (Luc Nhan)

### 9.1 Project Context
```

bằng:

```
## 9. Worked Example (Luc Nhan)

> **Note**: §9 là **snapshot minh họa** từ đợt đánh giá 2026-06, KHÔNG phải state hiện hành
> của project (LOC, số commit ahead, git state có thể đã đổi). Workflow (§1-8) vẫn chính xác
> và tái sử dụng được.

### 9.1 Project Context
```

- [ ] **Step 6.2: Verify**

Run: `sed -n '683,690p' docs/EVALUATION_WORKFLOW.md`
Expected: thấy block note sau heading §9.

- [ ] **Step 6.3: Commit**

```bash
git add docs/EVALUATION_WORKFLOW.md
git commit -m "docs: mark EVALUATION_WORKFLOW §9 as snapshot, not current state"
```

---

## Task 7: Thêm status note cho 2 design spec chưa implement

**Files:**
- Modify: `lucnhan/plans/web_researcher_design.md:1-2`
- Modify: `lucnhan/plans/web_researcher_tools_design.md:1-2`

- [ ] **Step 7.1: Thêm status note vào web_researcher_design.md**

Edit `lucnhan/plans/web_researcher_design.md`, thay:

```
# Web Researcher Sub-Agent Architecture Design

## 1. Overview
```

bằng:

```
# Web Researcher Sub-Agent Architecture Design

> **Status**: Design spec — NOT YET IMPLEMENTED (reviewed 2026-06-16).
> Đây là tài liệu thiết kế cho `web_researcher` sub-agent. Chưa có code
> (`grep web_researcher lucnhan/ --include="*.py"` → 0 hit). Tham khảo khi triển khai.

## 1. Overview
```

- [ ] **Step 7.2: Thêm status note vào web_researcher_tools_design.md**

Edit `lucnhan/plans/web_researcher_tools_design.md`, thay:

```
# Web Researcher Tools Architecture Design

## 1. Overview
```

bằng:

```
# Web Researcher Tools Architecture Design

> **Status**: Design spec — NOT YET IMPLEMENTED (reviewed 2026-06-16).
> Đây là tài liệu thiết kế cho `web_search` / `understand_image` tools. Chưa có code
> triển khai theo spec này. Tham khảo khi triển khai.

## 1. Overview
```

- [ ] **Step 7.3: Verify**

Run: `grep -l "NOT YET IMPLEMENTED" lucnhan/plans/web_researcher_*.md`
Expected: 2 file listed.

- [ ] **Step 7.4: Commit**

```bash
git add lucnhan/plans/web_researcher_design.md lucnhan/plans/web_researcher_tools_design.md
git commit -m "docs: mark web_researcher design specs as not-yet-implemented"
```

---

## Task 8: Final verification (toàn bộ plan)

- [ ] **Step 8.1: Chạy tất cả verification grep**

Run (mỗi command phải trả empty/đúng):

```bash
echo "=== A1: no '10 built-in' anywhere ==="
grep -rn "10 built-in" AGENTS.md llms.txt
# Expected: empty

echo "=== A2: no '~56 tools' or '56+ tools' ==="
grep -rn "~56 tools\|56+ tools" ARCHITECTURE.md llms.txt
# Expected: empty

echo "=== A3: no 'Known Broken IDA Tools' ==="
grep -rn "Known Broken IDA Tools" llms.txt
# Expected: empty

echo "=== B: AGENTS.md tools/ tree correct ==="
grep -n "navigation.py" AGENTS.md
# Expected: exactly 1 match (in ida/tools/ block)

echo "=== C1: PROJECT_MODIFICATION_PLAN deleted ==="
test -f docs/PROJECT_MODIFICATION_PLAN.md && echo "FAIL: still exists" || echo "OK: deleted"

echo "=== C2: FORK_MIGRATION_ASSESSMENT no broken link ==="
grep -c "PROJECT_MODIFICATION_PLAN.md](PROJECT_MODIFICATION_PLAN.md)" docs/FORK_MIGRATION_ASSESSMENT.md
# Expected: 0

echo "=== D: plans have status marker ==="
grep -l "NOT YET IMPLEMENTED" lucnhan/plans/web_researcher_design.md lucnhan/plans/web_researcher_tools_design.md
# Expected: both files
```

- [ ] **Step 8.2: Sanity-check không có code nào bị động**

Run:
```bash
git diff --stat master -- lucnhan/ | tail -5
```
Expected: empty hoặc chỉ file `lucnhan/plans/*.md` (docs only, không `.py`).

> Nếu thấy file `.py` nào bị đổi → STOP, đó là lỗi (plan này không chạm code).

- [ ] **Step 8.3: (Optional) Push**

Plan này không kích CI (CI trigger trên `main`/`dev`, không phải `master`). Push an toàn:

```bash
git push origin master
```

> Chỉ push khi user yêu cầu. Plan mặc định commit cục bộ.

---

## Spec Coverage Map

| Spec section | Task |
|--------------|------|
| A1 (skill count) | Task 1 (AGENTS.md), Task 2.2/2.3/2.4 (llms.txt) |
| A2 (tool count) | Task 2.1 (llms.txt), Task 3 (ARCHITECTURE.md) |
| A3 (broken tools table) | Task 2.5 |
| B (directory tree) | Task 4 |
| C1 (delete plan) | Task 5.1 |
| C1 ref fix | Task 5.2 |
| C3 (EVALUATION disclaimer) | Task 6 |
| D (plans status) | Task 7 |
| Verification | Task 8 |

---

## Commit Summary (dự kiến)

| # | Commit | Task |
|---|--------|------|
| 1 | `docs: fix built-in skill count 10 → 12 in AGENTS.md` | 1 |
| 2 | `docs: fix llms.txt skill/tool counts and remove stale broken-tools table` | 2 |
| 3 | `docs: update tool count to 60+ in ARCHITECTURE.md` | 3 |
| 4 | `docs: fix AGENTS.md tool directory tree` | 4 |
| 5 | `docs: remove stale PROJECT_MODIFICATION_PLAN, fix cross-ref` | 5 |
| 6 | `docs: mark EVALUATION_WORKFLOW §9 as snapshot` | 6 |
| 7 | `docs: mark web_researcher design specs as not-yet-implemented` | 7 |
