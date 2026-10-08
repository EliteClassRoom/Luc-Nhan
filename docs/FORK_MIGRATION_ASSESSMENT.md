# FORK_MIGRATION_ASSESSMENT.md — Luc Nhan MAIN vs FORK

> Đánh giá toàn diện (2026-06-14) giữa **MAIN** (`D:/re_dev_projects/vibe-clone/lucnhan`, v1.2) và **FORK** (`D:/re_dev_projects/Luc Nhan`, v1.3.1).
> Mục tiêu: xác định "tốt nhất của FORK" để port sang MAIN, đồng thời sửa các vấn đề tồn đọng của MAIN.
> Supersedes plan cũ (PROJECT_MODIFICATION_PLAN.md, 2026-06-13) — file đó đã xóa vì lạc hậu so với git history.

---

## TL;DR

- **MAIN = v1.2 nhưng nhiều tính năng hơn FORK** (+17k LOC): A2A bridge, orchestra, headless mode + control server, 4 IDA reader agents, session manifest cache, OpenAI `tool_call_id` dedup, IDA theme/font integration.
- **FORK = v1.3.1, gọn hơn nhưng NHỎ HƠN chủ yếu vì ít tính năng**, không phải vì "code sạch hơn". FORK đã clean Binary Ninja, hoàn tất theme token-driven refactor, extract IDA tools formatting helpers.
- **Cái bẫy lớn nhất**: nhiều file MAIN lớn hơn FORK là do **MAIN có tính năng/fix bảo vệ mà FORK không có** (openai_provider dedup, history manifest, ida/ui/panel theme integration). Port mù quáng file fork sẽ **mất fix/tính năng**.
- **Chỉ 3 fork improvements thực sự đáng port** (an toàn, rõ win): theme QSS token-driven refactor, IDA tools formatting dedup, `tools/pagination.py` + `value_format.py` helpers.
- **12 file MAIN vượt 800 dòng** cần tách (tech debt nội bộ MAIN, không liên quan fork).
- **Workflow đa-agent đánh giá thất bại hoàn toàn** (16/16 agent stall) — đánh giá thủ công có cấu trúc được dùng thay thế, verify trực tiếp tất cả HIGH findings.

---

## Trạng thái thực tế vs Plan cũ

`PROJECT_MODIFICATION_PLAN.md` (2026-06-13) đã lạc hậu. Git history cho thấy nhiều việc đã làm **sau** khi plan viết:

| Mục plan | Trạng thái plan cũ | Trạng thái THỰC TẾ (2026-06-14) | Bằng chứng |
|----------|--------------------|--------------------------------|------------|
| **C.1** Port theme/watcher.py | ⏳ Pending | ✅ **ĐÃ XONG** | commit `9eb6486`, file `ui/theme/watcher.py` đã có (4375 bytes), wired vào `ida/ui/panel.py:429 _maybe_start_theme_watcher` |
| **D.2** Subprocess injection a2a | ⏳ Pending | ✅ **ĐÃ XONG** | commit `57caf5e`, `_build_command()` có `_validate_task()` + `--` separator |
| **B.1-B.4** Provider porting | ✅ Done | ✅ Confirmed | codex_provider, pseudo_tool_schemas đã có (auth_compat đã xóa — port chết, zero callers) |
| **D.1** Path traversal research | ✅ Done | ✅ Confirmed | `_safe_note_path` trong research.py |
| **A.1-A.5** Quick wins | ✅ Done | ✅ Confirmed | 3 archives xóa, .gitignore mở rộng, README skills, debug_test.py xóa |
| **C.4** Extract loop.py schemas | ⏳ Pending | ⏳ **VẪN PENDING** | `pseudo_tool_schemas.py` đã có nhưng **không import ở đâu**; loop.py vẫn 32 inline schemas (FORK chỉ 2) |
| **C.2/C.3/C.5/C.6** File splits | ⏳ Pending | ⏳ **VẪN PENDING** | styles 2758, loop 2153, panel_core 2026, chat_view 2003, settings_dialog 1297 |
| **D.3** 6 test isolation bugs | ⏳ Pending | ⏳ Vẫn pending | marked xfail trong `a8e8060` |
| **E** Doc sync | ⏳ Pending | ⏳ Vẫn pending | llms.txt, webpage |

**Kết luận**: Plan cũ phản ánh snapshot cũ. Cần plan mới dựa trên thực tế.

---

## Đánh giá theo cụm module

### 1. `ui/styles.py` (MAIN 2758 vs FORK 266) — **PORT (high value, high effort)**

**Fork approach**: styles.py chỉ là thin wrapper 266 dòng, delegate sang `ui/theme/` package. FORK hoàn tất refactor token-driven:
- `ThemeTokens` dataclass (palette_dark/light/ida) chứa colors
- `_QSS_TEMPLATE` + `format_template()` generate QSS động từ tokens
- `DARK_THEME`/`IDA_NATIVE_THEME` trở thành dict alias cho backward compat
- Mọi `get_*_style()` getter trả QSS built từ tokens

**MAIN status**: **dở dang**. MAIN đã có `format_template` helper trong `theme/manager.py` (line 13), đã có `build_settings_dialog_stylesheet`/`build_input_area_stylesheet` token-driven ở cuối file. NHƯNG đoạn 71-2280 (~2200 dòng) vẫn là **QSS string cứng theo theme** (`LIGHT_THEME = """..."""`, `DARK_THEME`, `IDA_*`).

**Verdict**: **PORT (partial — port approach, không copy file)**. MAIN không thể copy styles.py của fork vì:
- MAIN có nhiều getter hơn (orchestra_panel, a2a_widget, bulk_renamer styles...)
- MAIN có `set_current_theme`/`is_host_theme`/`build_theme_stylesheet` API mà nhiều module đã import
- Nên port **cách tiếp cận**: chuyển từng mega-block `LIGHT_THEME`/`DARK_THEME` → template + `format_template(tokens)`, giữ public API getter.

**Effort**: XL. **Risk**: high (đụng tới mọi widget). **Giảm**: 2758 → ~800 dòng.

### 2. `agent/loop.py` (MAIN 2153 vs FORK 1472) — **C.4 dễ port, phần còn lại là feature**

- **MAIN lớn hơn 681 dòng** vì thêm orchestra/a2a/pseudo-tool routing (FORK không có các mode này). **Không thể copy file.**
- **C.4 (extract inline schemas)**: MAIN loop.py có **32 inline `"description":` schema literals**, FORK chỉ **2**. File `pseudo_tool_schemas.py` đã được thêm (B.3) nhưng **không import ở đâu**. Đây là port dễ nhất, giảm ~700 dòng, effort L, risk medium.

**Verdict**: **PORT C.4 (high value)** — wire `pseudo_tool_schemas.ALL_PSEUDO_TOOL_SCHEMAS` vào loop.py, xóa inline.

### 3. `providers/openai_provider.py` (MAIN 620 vs FORK 307) — **SKIP PORT (MAIN có fix bảo vệ)**

**CẢNH BÁO**: MAIN lớn hơn 313 dòng KHÔNG phải vì bloat. `_format_messages` của MAIN (121 dòng) fix bug **duplicate `tool_calls[].id`** mà OpenAI reject (`invalid params, duplicate tool_call id`). FORK (34 dòng) **không có fix này**.

Port fork version sẽ **tái引入 bug** trong restored sessions. Đây là cái bẫy điển hình khi đánh giá fork theo LOC.

**Verdict**: **SKIP port file**. Có thể refactor nội bộ MAIN (`_format_messages` 121 dòng → extract helpers) nhưng **giữ logic dedup**. Đây là việc refactor nội bộ, không phải port.

### 4. `control/server.py` (1062 dòng, MAIN-only) — **FIX quality findings (MAIN-only debt)**

FORK không có headless/control server. Đây là feature MAIN mới → 5 bare-except quality findings (Q-001..Q-005) là debt nội bộ MAIN.

| Finding | Severity | Trạng thái | Fix |
|---------|----------|-----------|-----|
| Q-001 bare except `EventBroker.stop()` line 109 | HIGH | ✅ confirmed | `logger.exception()` |
| Q-002 bare except long-poll line 875 | HIGH | ✅ confirmed | log + drop/re-queue |
| Q-003 3x except→make_error_json no log (578,653,729) | MEDIUM | ✅ confirmed | thêm `logger.exception()` |
| Q-004 magic `range(200)` drain line 212 | MEDIUM | ✅ confirmed | named constant `_DRAIN_MAX` |
| Q-005 `do_POST` nesting depth 7 line 399 | MEDIUM | ✅ confirmed | dict-of-handlers refactor |

**Verdict**: **FIX nội bộ MAIN** (effort S-M, risk low). Không liên quan fork.

### 5. `agent/bulk_renamer.py` (MAIN 1004 vs FORK 761) — **FIX shared debt**

- FORK cũng có `_run_quick` lớn + bare except → **Q-006/Q-007/Q-008 là shared tech debt**, không phải fork fix.
- MAIN lớn hơn 243 dòng vì có `_run_deep_preloaded` (variant) + thêm error paths.

| Finding | Severity | Fix |
|---------|----------|-----|
| Q-006 bare except decompile line 409 | HIGH | catch cụ thể + flag missing disasm |
| Q-007 `_run_quick` 197 dòng line 359 | HIGH | tách `_decompile_jobs`/`_invoke_llm`/`_apply_renames` |
| Q-008 `_analyze_one` closure 154 dòng line 650,842 | MEDIUM | promote method + dedup deep vs deep_preloaded |

**Verdict**: **FIX nội bộ MAIN** (effort L, risk medium).

### 6. `ui/tool_widgets.py` `_format_tool_summary` (Q-009) — **FIX shared debt**

- **Cả MAIN lẫn FORK** đều có `_format_tool_summary` là if/elif chain dài (chỉ khác tên tool: MAIN `rename_variable`, FORK `rename_single_variable`).
- → Đây là shared tech debt, không phải fork fix. Refactor dict-of-handlers áp dụng cho cả 2.

**Verdict**: **FIX nội bộ MAIN** (effort M, risk low). Q-010/Q-011/Q-012/Q-013/Q-014 (panel_core magic widths, settings_dialog sizing, message_widgets bare except) tương tự — debt nội bộ MAIN.

### 7. `tools/` framework — **PORT (clean, low risk)**

FORK đã tách shared helpers ra:
- `tools/formatting.py` (54 dòng): `format_function_summary`, `format_callers_callees`
- `tools/pagination.py` (37 dòng): `normalize_page`, `format_page` với `DEFAULT_PAGE_LIMIT=80`, `MAX_PAGE_LIMIT=200`
- `tools/value_format.py` (150 dòng): `format_global_value`, `normalize_type_hint`, `bytes_needed_for_type`

MAIN hiện inline logic này (vd `ida/tools/functions.py` 214 dòng inline formatting vs FORK 104 dùng helper).

**Verdict**: **PORT 3 file helpers** + refactor `ida/tools/functions.py` dùng chúng (effort M, risk low, giảm ~110 dòng + DRY).

### 8. `ida/tools/` dedup — **PORT (commit e6ab8e9 của fork)**

FORK commit `e6ab8e9: "refactor(tools): extract shared formatting helpers, drop duplicate IDA tools"` đã gọn hóa. MAIN `ida/tools/registry.py` (183) vs FORK (44), `functions.py` (214 vs 104). Cần đối chiếu từng file — một phần đã xử lý ở cluster 7.

**Verdict**: **PORT selective** — port formatting helpers (cluster 7), KHÔNG port registry (MAIN registry lớn hơn vì có _TOOL_MODULES mapping cho nhiều tool hơn).

### 9. `state/history.py` (MAIN 620 vs FORK 307) & `ida/ui/panel.py` (539 vs 85) — **SKIP (MAIN có feature)**

- **MAIN `history.py`** thêm **manifest cache system** (`_read_manifest`/`_write_manifest`/`_rebuild_manifest`) để `list_sessions` nhanh. FORK quét DB trực tiếp. → MAIN feature, skip.
- **MAIN `ida/ui/panel.py`** thêm `_apply_ida_theme`, `_maybe_start_theme_watcher`, `_apply_font_override`. FORK chỉ thin wrapper 85 dòng. → MAIN đã port theme watcher vào đây rồi (C.1 done). Skip.

---

## 38 Quality Findings — Trạng thái xác minh

Đọc `.scratch/quality_findings.json` (38 findings: 11 HIGH, 24 MEDIUM, 3 LOW). Tất cả HIGH findings đã verify trực tiếp trong code hiện tại — **tất cả vẫn còn đúng**:

| Category | Count | Ví dụ | Loại fix |
|----------|-------|------|----------|
| `error_handling` | 9 | bare `except Exception: pass` (control/server, bulk_renamer, message_widgets, settings_dialog) | internal MAIN |
| `function_size` | 11 | `_run_quick` 197 dòng, `_format_tool_summary` 146, `_on_accept` 94, `run()` 111 | internal MAIN |
| `magic_number` | 7 | `range(200)`, 7x `setFixedWidth(64)`, resize ratios | internal MAIN |
| `deep_nesting` | 5 | `do_POST` depth 7, `_execute_tool_calls` depth 8, `_format_tool_summary` depth 16 | internal MAIN |
| `file_size` | 2 | styles 2758, (loop) | internal MAIN |
| `type_safety` | 2 | `raw_parts: Any` leak (Q-017, Q-027) trong loop.py streaming chain | internal MAIN |
| `complexity` | 2 | `_modify_struct_ida9` 91 dòng/8 params (Q-019), `_get()` 195 dòng (Q-029) | internal MAIN |

**Tất cả đều là debt nội bộ MAIN**, không phải fork sẽ giải quyết.

### Bằng chứng fork chia sẻ tech debt

Q-017..Q-038 có **13 findings mô tả debt "in the fork"** (Q-024, Q-025, Q-027, Q-028, Q-029, Q-030, Q-032, Q-033, Q-034, Q-035, Q-036, Q-037, Q-038). Đây là bằng chứng trực tiếp: **fork mắc cùng loại tech debt** (file >800, bare except, magic number, deep nesting). Fork không phải "bản sạch" — nó chỉ nhỏ hơn vì ít feature.

Vài findings đáng thêm vào Tier 2 (verify trong code hiện tại trước khi fix):
- **Q-019 [HIGH]**: `ida/tools/types_tools.py:174` `_modify_struct_ida9` 91 dòng/8 params/if-elif 8 branches — refactor dict-of-actions.
- **Q-031 [MEDIUM]**: `core/sanitize.py:332` magic 64/40/32 (SHA-256/1/MD5 hash lengths) — named constants trong security code.
- **Q-018 [MEDIUM]**: `loop.py:1392` magic `20` max subagent spawn → named constant.
- **Q-022/Q-023**: `chat_view.handle_event` depth 9, `panel_core._poll_tools_events` 103 dòng duplicate blocks.

---

## Migration Plan cập nhật (theo impact/effort/risk)

### Tier 1 — Win cao, effort thấp, risk thấp (làm ngay)

| # | Action | Effort | Risk | Impact | Loại |
|---|--------|--------|------|--------|------|
| 1 | **Fix Q-001/Q-002**: 2 bare-except HIGH trong `control/server.py` (109, 875) → `logger.exception()` | S | low | HIGH | internal |
| 2 | **Fix Q-003**: 3 except→make_error_json thêm log (578,653,729) | S | low | MED | internal |
| 3 | **Fix Q-004**: `range(200)` → `_DRAIN_MAX` constant + comment | S | low | MED | internal |
| 4 | **Fix Q-014**: `message_widgets.py:952` bare except + import logger | S | low | HIGH | internal |
| 5 | **Fix Q-010**: 7x `setFixedWidth(64)` → `_ACTION_BUTTON_WIDTH` | S | low | MED | internal |
| 6 | **Fix Q-011**: `settings_dialog:685` except→return thêm log + fallback | S | low | HIGH | internal |
| 7 | **PORT C.4**: wire `pseudo_tool_schemas.ALL_PSEUDO_TOOL_SCHEMAS` vào `loop.py`, xóa 32 inline schemas | L | med | HIGH (giảm ~700 dòng) | port |
| 8 | **PORT `tools/pagination.py` + `value_format.py` + `formatting.py`** (3 helpers file từ fork) | M | low | MED (DRY) | port |

### Tier 2 — Win cao, effort trung, risk trung (lên kế hoạch)

| # | Action | Effort | Risk | Impact | Loại |
|---|--------|--------|------|--------|------|
| 9 | **PORT IDA tools dedup**: refactor `ida/tools/functions.py` (214→~100) dùng `formatting.py` | M | low | MED | port |
| 10 | **Fix Q-007**: tách `_run_quick` (197→<50) thành 3 helper trong `bulk_renamer.py` | L | med | HIGH | internal |
| 11 | **Fix Q-008**: promote `_analyze_one` thành method, dedup deep vs deep_preloaded | L | med | MED | internal |
| 12 | **Fix Q-009**: refactor `_format_tool_summary` (if/elif → dict-of-handlers) | M | low | HIGH | internal |
| 13 | **Fix Q-005**: `control/server.py do_POST` → dict-of-handlers | M | med | MED | internal |

### Tier 3 — Win lớn nhất, effort XL, risk cao (cần kế hoạch kỹ)

| # | Action | Effort | Risk | Impact | Loại |
|---|--------|--------|------|--------|------|
| 14 | **PORT styles.py refactor** (2758→~800): chuyển mega QSS blocks → `_QSS_TEMPLATE` + `format_template(tokens)`. Giữ public getter API. | XL | high | HUGE | port (approach) |
| 15 | **Split `loop.py`** (2153): tách streaming/tool-dispatch/finalization helpers (Q-015/Q-016) | XL | high | HIGH | internal |
| 16 | **Split `panel_core.py`** (2026), `chat_view.py` (2003) | XL | high | MED | internal |

### Tier 4 — Defer / bỏ qua

- ❌ **B.5 port openai_provider**: MAIN có `tool_call_id` dedup fix quan trọng — KHÔNG port file fork. Refactor nội bộ OK nhưng giữ logic.
- ❌ **Port `history.py`/`ida/ui/panel.py`**: MAIN có manifest cache + theme/font integration mà fork thiếu.
- ⏳ **D.3** 6 test isolation bugs (xfail): effort M, risk low, làm khi rảnh.
- ⏳ **E** doc sync (llms.txt, webpage): sau khi code ổn định.

---

## Nguyên tắc cốt lõi khi port fork

1. **Đối chiếu từng khối code, không chỉ LOC.** MAIN thường lớn hơn vì có fix/feature bảo vệ (openai dedup, history manifest, panel theme). Copy mù = mất fix.
2. **Port "approach", không nhất thiết port "file".** Styles refactor: áp dụng template pattern, giữ MAIN's getter API.
3. **MAIN là source of truth cho feature set.** A2A/orchestra/headless/4 IDA readers là tài sản MAIN — không bao giờ xóa để "đuổi theo" fork.
4. **Shared tech debt (Q-006/7/8/9) fix ở MAIN, không cần fork.** Fork cũng mắc.
5. **Verify từng finding trong code hiện tại** trước khi fix — plan cũ đã cũ, git đã drift.

---

## Quy trình đánh giá (ghi chú meta)

Lần này, **Workflow đa-agent thất bại hoàn toàn** (16/16 agent stall sau 6 retry × 180s = ~50 phút, 0 kết quả). Nguyên nhân khả thi: schema lồng sâu + đường dẫn Windows tuyệt đối dài + subagent đọc file lớn song song. **Đánh giá thủ công có cấu trúc** (Grep/Read/Bash trực tiếp, verify từng finding) lại hiệu quả và nhanh hơn rõ rệt — verify toàn bộ 11 HIGH findings trong vài phút. Bài học: workflow không phải luôn là công cụ đúng; với task đánh giá cần đọc chéo 2 cây code, thu thập thủ công định hướng tốt hơn.

---

## Bước tiếp theo đề xuất

1. **Chạy Tier 1 (8 mục)** — 1-2 ngày, thu gọn lỗi trầm cảm và hoàn tất C.4 + port helpers. Chạy `./ci-local.sh` sau mỗi mục.
2. **Cập nhật `PROJECT_MODIFICATION_PLAN.md`** với trạng thái thực tế (đánh dấu C.1, D.2 done) — hoặc thay bằng file này.
3. **Lên kế hoạch Tier 3 #14 (styles)** riêng vì risk cao — branch riêng, incremental extraction, test visual sau mỗi bước.
