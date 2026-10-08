# Document Standardization — Design Spec

**Date**: 2026-06-16
**Status**: Approved
**Scope**: Option 2 — sửa stale facts + dọn dẹp `docs/`
**Language policy**: giữ nguyên (EN = technical docs quốc tế, VI = project log nội bộ)

---

## Problem

Documentation đã drift so với code hiện tại sau một chuỗi port/refactor. Audit
phát hiện các vấn đề sau (đều là stale facts hoặc cấu trúc sai, không phải lỗi code):

1. **Số liệu sai**: "10 built-in skills" (thực 12), "~56 tools" / "56+ tools"
   (thực 73 `@tool` defs), "Known Broken IDA Tools" (4 tool đều đã fix).
2. **Cấu trúc thư mục sai trong AGENTS.md**: liệt kê `lucnhan/tools/navigation.py`,
   `strings.py`, `database.py`, `decompiler.py`... nhưng các file này nằm ở
   `lucnhan/ida/tools/`. Developer đọc sẽ tìm sai chỗ.
3. **`docs/` chứa 2 plan mâu thuẫn**: `PROJECT_MODIFICATION_PLAN.md` tự ghi
   "đã lạc hậu", bị `FORK_MIGRATION_ASSESSMENT.md` supersede.
4. **`lucnhan/plans/` chứa 2 design spec chưa implement** (web_researcher),
   không có status marker → dễ nhầm là đã xong hoặc đã bỏ.

---

## Ground Truth (đã verify 2026-06-16)

| Claim | Thực tế | Nguồn |
|-------|---------|-------|
| Built-in skills | **12** | `ls lucnhan/skills/builtins/` (12 dir + `__init__.py`) |
| `@tool` defs | **73** | `grep -rh "@tool(" lucnhan/tools/ lucnhan/ida/tools/` |
| `create_struct` broken? | **Không** — đã migrate `ida_typeinf` | `ida/tools/types_tools.py:373,381` |
| `import_c_header` broken? | **Không** — dùng `idc.parse_decls()`, `idc` đã import | `types_tools.py:915,922` + `:30-34` import loop |
| `set_function_prototype` broken? | **Không** — dùng `idc.SetType()` | `types_tools.py:899,908` |
| `apply_type_to_variable` broken? | **Không** — guard đúng (`ida_typeinf is None`) | `types_tools.py:827,835` |
| Tool impls ở đâu? | IDA impls ở `lucnhan/ida/tools/`, framework ở `lucnhan/tools/` | `ls` cả 2 dir |
| `lucnhan/plans/web_researcher_*` | **Chưa implement** — chỉ là design spec | `grep "web_researcher" lucnhan/ --include="*.py"` → 0 hit |

### Cấu trúc `lucnhan/tools/` thực tế (framework + shared helpers)

```
__init__.py  base.py  cache.py  coercion.py  formatting.py
pagination.py  registry.py  script_guard.py  value_format.py
web.py  web_fetch.py  xrefs.py
```

### Cấu trúc `lucnhan/ida/tools/` thực tế (IDA tool implementations)

```
__init__.py  annotations.py  database.py  decompiler.py  disassembly.py
functions.py  microcode.py  microcode_format.py  microcode_optim.py
navigation.py  registry.py  scripting.py  strings.py  types_tools.py  xrefs.py
```

---

## Design — theo phần

### Section A: Sửa stale facts (3 file)

**A1. Skill count 10 → 12**

| File | Line | Hiện tại | Sửa thành |
|------|------|----------|-----------|
| `AGENTS.md` | 95 | `# 10 built-in skills` | `# 12 built-in skills` |
| `llms.txt` | 10 | `10 built-in skills.` | `12 built-in skills.` |
| `llms.txt` | 37 | `# Skill discovery, loading, 10 built-in skills` | `# Skill discovery, loading, 12 built-in skills` |

Thêm 2 skill còn thiếu vào bảng skills `llms.txt:57-71` (sau `/ida-scripting`):
```
| `/ida-docs` | Search and browse official IDA Pro documentation |
| `/ida-pro-mcp` | IDAPython scripting reference |
```

**A2. Tool count 56 → 60+**

| File | Line | Hiện tại | Sửa thành |
|------|------|----------|-----------|
| `ARCHITECTURE.md` | 262 | `Each host provides ~56 tools organized by category:` | `Each host provides 60+ tools organized by category:` |
| `llms.txt` | 9 | `56+ tools for IDA.` | `60+ tools for IDA.` |

> Dùng "60+" thay vì "73" vì số tool dao động theo host capability (headless
> loại cursor/jump tools). 60+ là bound an toàn.

**A3. Xóa bảng "Known Broken IDA Tools" trong `llms.txt:100-107`**

Toàn bộ 4 tool đều đã fix (xem Ground Truth). Xóa block:
```
## Known Broken IDA Tools

| Tool | Error |
...
| `apply_type_to_variable` | Decompiler guard fires incorrectly |
```
+ xóa `## Adding Tools` heading bị cô lập nếu còn.

---

### Section B: Sửa cấu trúc thư mục AGENTS.md

`AGENTS.md:45-60` liệt kê `lucnhan/tools/` chứa cả IDA tool implementations
(`navigation.py`, `strings.py`, `database.py`, `decompiler.py`, `annotations.py`,
`types_tools.py`, `microcode*.py`, `scripting.py`) — nhưng các file này nằm ở
`lucnhan/ida/tools/`.

**Sửa**: viết lại 2 block tree theo ground truth:

- `lucnhan/tools/` → chỉ framework + shared helpers (`base.py`, `registry.py`,
  `coercion.py`, `cache.py`, `formatting.py`, `pagination.py`, `value_format.py`,
  `script_guard.py`, `web.py`, `web_fetch.py`, `xrefs.py`) + comment đúng vai trò.
- `lucnhan/ida/tools/` → block riêng chứa IDA implementations (13 file) +
  `registry.py` (IDA-specific `create_default_registry()`).

Đồng bộ comment header `## tools/`: đổi từ "IDA tool implementations" thành
"Shared tool framework (`@tool` decorator, registry, coercion, helpers)".

---

### Section C: Dọn dẹp `docs/`

**C1. Xóa `docs/PROJECT_MODIFICATION_PLAN.md`**
- Lý do: tự ghi "đã lạc滞后" (line 5), bị `FORK_MIGRATION_ASSESSMENT.md` supersede.
- Git xóa: `git rm docs/PROJECT_MODIFICATION_PLAN.md`.
- Kiểm tra cross-refs: `docs/FORK_MIGRATION_ASSESSMENT.md:5` reference tới
  `PROJECT_MODIFICATION_PLAN.md` — sửa câu "Superseds ..." thành note ngắn gọn
  (file cũ đã xóa).

**C2. Giữ + rà `docs/FORK_MIGRATION_ASSESSMENT.md`**
- Plan hiện hành, giữ nguyên. Chỉ sửa reference C1.

**C3. Giữ + rà `docs/EVALUATION_WORKFLOW.md`**
- Workflow tái sử dụng được (language-agnostic). Giữ nguyên phần method.
- §9 "Worked Example (Luc Nhan)" có sample data có thể stale (git state "23
  commits ahead", LOC "~45,000"). Đánh dấu rõ đây là **worked example snapshot
  2026-06**, không phải state hiện hành — thêm 1 dòng disclaimer ở đầu §9.

**C4. Giữ `docs/superpowers/specs/2026-06-07-background-restore-design.md`**
- Approved spec, còn giá trị. Không động.

---

### Section D: `lucnhan/plans/` status markers

2 file design spec `web_researcher` chưa implement. Thêm frontmatter/ header
note vào đầu mỗi file:

```markdown
> **Status**: Design spec — NOT YET IMPLEMENTED (2026-06-16).
> Đây là tài liệu thiết kế cho tính năng `web_researcher` sub-agent.
> Chưa có code. Tham khảo khi triển khai.
```

Files: `lucnhan/plans/web_researcher_design.md`,
`lucnhan/plans/web_researcher_tools_design.md`.

---

## Out of Scope (Option 3, không làm)

- Dịch ngôn ngữ docs (giữ EN technical / VI log).
- Tách design doc (AGENTS.md 1268 dòng → split).
- Sync `webpage/*.html` (hand-maintained landing page).
- Sửa sample data chi tiết trong EVALUATION_WORKFLOW §9 (chỉ thêm disclaimer).

---

## Files Touched

| Action | File |
|--------|------|
| Edit (A1+A2) | `AGENTS.md` |
| Edit (A1+A2+A3) | `llms.txt` |
| Edit (A2) | `ARCHITECTURE.md` |
| Edit (B) | `AGENTS.md` (tools tree block) |
| Delete (C1) | `docs/PROJECT_MODIFICATION_PLAN.md` |
| Edit (C1 ref) | `docs/FORK_MIGRATION_ASSESSMENT.md` |
| Edit (C3 disclaimer) | `docs/EVALUATION_WORKFLOW.md` |
| Edit (D) | `lucnhan/plans/web_researcher_design.md` |
| Edit (D) | `lucnhan/plans/web_researcher_tools_design.md` |

**Tổng: 7 file edit + 1 file delete. Không chạm code.**

---

## Verification

- [ ] `grep "10 built-in" AGENTS.md llms.txt` → 0 hit
- [ ] `grep "~56 tools\|56+ tools" ARCHITECTURE.md llms.txt` → 0 hit
- [ ] `grep "Known Broken IDA Tools" llms.txt` → 0 hit
- [ ] `AGENTS.md` tree block `lucnhan/tools/` KHÔNG còn list `navigation.py`, `strings.py`, `database.py`, `decompiler.py`, `annotations.py`, `types_tools.py`, `microcode.py`, `scripting.py`
- [ ] `AGENTS.md` có block `lucnhan/ida/tools/` riêng
- [ ] `docs/PROJECT_MODIFICATION_PLAN.md` không còn tồn tại
- [ ] 2 file `lucnhan/plans/web_researcher_*.md` có status note "NOT YET IMPLEMENTED"
- [ ] `./ci-local.sh` không ảnh hưởng (không chạm code) — skip nếu tin cậy

---

## References

- [FORK_MIGRATION_ASSESSMENT.md](../../FORK_MIGRATION_ASSESSMENT.md) — plan hiện hành
- [EVALUATION_WORKFLOW.md](../../EVALUATION_WORKFLOW.md) — workflow tái sử dụng
- [AGENTS.md](../../../AGENTS.md) — developer guide
