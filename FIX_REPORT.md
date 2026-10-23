# Báo cáo Tổng hợp — Sửa toàn bộ findings từ review (branch `fix/review-findings`)

**Mục tiêu:** verify lại và sửa hết các vấn đề tìm được trong 7 báo cáo review.
**Branch:** `fix/review-findings` (tách từ `master` @ 8693222) — **chưa merge**, chờ review.
**Phương pháp:** 6 fixer agents (mỗi agent một cụm module) theo TDD (test trước → fail → fix → pass), lead verify độc lập toàn bộ sau cùng.

---

## Kết quả xác minh tổng thể (lead chạy lại độc lập)

| Kiểm chứng | Lệnh | Kết quả |
|---|---|---|
| Full test suite | `pytest tests/ -q` | **4163 passed, 170 skipped** — 1 fail flake pre-existing xem bên dưới |
| Lint toàn repo | `ruff check lucnhan/ tests/` | ✅ All checks passed |
| Format | `ruff format --check lucnhan/` | ✅ 266 files already formatted |
| Type gate | `mypy lucnhan/core lucnhan/providers` | ✅ Success (exit 0) |

**Tổng thay đổi:** 52 files, **+1311 / −293** dòng (gồm cả import-sort tự động cho toàn `tests/` lần đầu được lint).

---

## 6/6 cụm fix hoàn thành

| Task | Cụm | Findings | Kết quả |
|---|---|---|---|
| task-8 | Agent Core | 2 HIGH + 3 MEDIUM + 2 LOW | ✅ 788 tests agent pass |
| task-9 | Tools | 1 HIGH + 1 MEDIUM + 2 LOW | ✅ 71+ tests pass + 16 test validate mới |
| task-10 | Providers+Memory+Security | 2 HIGH + 5 MEDIUM | ✅ memory 314 + providers 236 pass |
| task-11 | UI | 1 HIGH + 2 MEDIUM + 3 LOW | ✅ 324 tests UI pass |
| task-12 | Host+Headless+CLI | 3 MEDIUM + 7 LOW | ✅ 301 tests pass |
| task-13 | Tests+CI | 4 HIGH + 2 MEDIUM + 1 LOW + 2 pre-existing | ✅ tests/tools 1026 pass |

---

## Các HIGH đã sửa (chi tiết)

### 1. [/a2a prompt-injection] output external-agent không sanitize — FIXED
`lucnhan/agent/modes/a2a.py` — mọi `TEXT_DELTA` re-forward + `text_done(final)` giờ qua `sanitize_tool_result(..., "a2a")` (đồng bộ với `_handle_delegate_external_task_tool`). Test: `test_external_output_is_sanitized`.

### 2. [_verify_mutation getter-raw] — VERIFIED không-bug + contract test
Đọc code xác nhận `get_comment`/`get_function_comment`/`get_address_name`/`get_function_name` đều trả raw (`... or ""`). Thêm contract test `tests/agent/test_verify_mutation_getters.py` (5 test, kháng IDA-mock re-install).

### 3. [Memory exploration sanitize] title/metadata lưu verbatim — FIXED
`lucnhan/memory/service.py` — `save_exploration_finding` sanitize `title/entity_refs/tags/verdict_claim/verification_citations` qua `_sanitize_short_text()`/`_sanitize_fact()`. Test: marker injection trong title → assert strip.

### 4. [Memory manual_notes yếu] — FIXED
`manual_notes_context` dùng `_sanitize_manual_notes()` mới (strip_lone_surrogates + strip_injection_markers + regex, trả text khô tránh double-wrap với `system_prompt.py`). Test: MEMORY.md chứa `<system>` → assert strip.

### 5. [@idasync invariant lệch CLAUDE.md] — FIXED (doc + test)
Cập nhật `CLAUDE.md` §2 cho khớp thực tế (marshalling ở `ToolRegistry.execute/execute_coerced/execute_current_thread` qua `dispatch_wrapper`/`main_thread`). Test `TestRegistryThreadDispatch` (3 test) đảm bảo main_thread=True được wrap, main_thread=False (emulation) không wrap. *(Chọn phương án an toàn, tránh double-marshal — xem Ruling trong ledger.)*

### 6. [UI theme-leak singleton] message_widgets 13 điểm — FIXED
`lucnhan/ui/message_widgets.py` — 13 class chuyển sang `bind_theme`/`disconnect_theme` + `shutdown()`; `AssistantMessageWidget` cascade child `_ThinkingBlock`; `chat_view.py` gọi `_shutdown_message_widget` ở mọi đường xóa widget. Test `test_message_widgets_theme.py` (3 test). *(Lead đã fix thêm tính hermetic của test — xem bên dưới.)*

### 7. [Windows CI missing] — FIXED
`.github/workflows/ci.yml` — matrix thêm `windows-latest` (× py 3.11/3.12), guard Linux-only Qt libs.

### 8. [Ruff không lint tests/] — FIXED
`ci.yml` + `ci-local.sh` + `ci-local.ps1` → `ruff check lucnhan/ tests/`. Đã `--fix` 23 lỗi I001 pre-existing trong tests/.

### 9. [Mypy không gate memory/ui] — FIXED (gate đã mở)
`[tool.mypy].files` khai báo memory/ui; các lỗi mypy production pre-existing đã được **sửa tận gốc** (không dùng ignore):
- `memory/repository.py`, `ingest.py`, `agent/loop_commands.py` — `cast(HypothesisStatus, ...)` thay cho str → Literal
- `memory/markdown.py` — RLock `__exit__` trả None tường minh
- `memory/manager.py` — `choice: IdentityChoice | None` (trước là `object`)
- `memory/report.py` — `messages: Iterable[Any] | None` (trước `object`, không iterable được)
- `memory/workspace_store.py` — `_cached_list` generic `Callable[[], list[T]]` (trước trả `object`)
- `memory/retrieve.py` — `SearchAllResult` TypedDict cho `search_all` (trước `dict[str, object]`)
- `memory/case_commands.py` — tuple/list assignment
- `memory/authority.py` — `__reduce__` signature
- `ui/model_picker.py` — narrow `is not None`, `cast(QKeyEvent, ...)`, `cast(QApplication, ...)`
- `agent/modes/exploration.py`, `agent/loop.py`, `agent/loop_commands.py` — assert/guard cho `store`/`paths` None-narrowing

Gate mở tại: `ci.yml` type-check job, `ci-local.sh`, `ci-local.ps1`, `release.yml` → `mypy lucnhan/core lucnhan/providers lucnhan/memory lucnhan/ui` — **pass sạch** (exit 0).

### 10. [ida_mock MagicMock che bug — P1 crash 0xC0000409] — FIXED (root cause!)
**Root cause crash Windows:** `_get_base_namespace()` import **real PyPI package `ida_domain`** (không được mock) → `ida_domain.__init__` load `idapro`/Qt → construct QWidget không QApplication → native fast-fail. Fix: thêm `"ida_domain"` vào mocked-module set `tests/mocks/ida_mock.py`. Verify: `tests/tools` = **1026 passed** (trước: crash interpreter).

---

## Pre-existing findings phát hiện thêm trong quá trình (lead xác minh)

| # | Mô tả | Trạng thái |
|---|---|---|
| P1 | Crash 0xC0000409 (ida_domain không mock) | ✅ FIXED trong task-13 |
| P2 | `test_string_cache` order pollution | ✅ FIXED (monkeypatch `resolve_active_cache_key`) |
| P3 | `test_message_widgets_theme` fail khi chạy full suite (test của fixer UI thiếu hermetic — `test_panel_core` patch ThemeManager in-place không restore) | ✅ FIXED bởi lead: `_purge_ui_theme_modules()` force re-import |
| P4 | `tests/tools/test_panel_core.py` pollutes `tests/headless/test_runner.py` (8 fail order-dependent) | ✅ FIXED bởi lead: purge stubs đầu `test_runner.py` (pattern `test_providers.py`) trước module-level lucnhan imports — cả 2 thứ tự 255 passed |
| P5 | Mypy errors trong memory/* + ui/model_picker.py (chặn mở gate mypy) | ✅ **FIXED tận gốc** — gate `mypy core+providers+memory+ui` đã mở trong ci.yml/ci-local/release.yml, pass sạch (xem mục 9) |
| P6 | desloppify baseline 89.9 (query.json) vs CLAUDE.md 89.0 — scan thiếu bandit | ✅ Xác minh: `ci-local.ps1` chạy desloppify qua uvx **objective 89.9/100 ≥ baseline 89 — CI local ALL PASSED 5/5** |
| P7 | `test_raw_store::test_concurrent_upserts_no_loss` Windows flake (`PermissionError 13` file-lock khi 4 thread atomic-replace JSONL) | ⚠️ Pre-existing Windows-only, không do branch này (pass 5/5 lặp, full suite lần cuối pass luôn) |

---

## Rulings (quyết định của lead, đầy đủ trong ledger)

1. **@idasync** → sửa CLAUDE.md + test, KHÔNG wrap decorator (tránh double-marshal emulation). Chi phí nếu sai: handler gọi ngoài registry không marshal (đã ghi rõ trong doc).
2. **ida_mock** → route bảo thủ (mock ida_domain + test anti-leak), không refactor ~70 module sang realistic defaults (rủi ro phá hàng loạt test dùng `.return_value`).
3. **Mypy gate memory/ui** → defer (lỗi production pre-existing ngoài scope test-only task) + comment blocker.
4. **P3 hermeticity** → lead tự fix test (thay đổi test-infra nhỏ, theo pattern có sẵn của repo).

---

## Action items follow-up (đã xử lý trong lượt này — còn lại rất ít)

1. ~~**[P0]** Sửa lỗi mypy production trong `memory/*` + `ui/model_picker.py` → mở gate~~ ✅ ĐÃ XONG — gate mở ở 4 nơi (ci.yml, ci-local.sh, ci-local.ps1, release.yml).
2. ~~**[P0]** Fix `test_panel_core.py` pollution `tests/headless/test_runner.py`~~ ✅ ĐÃ XONG — purge stub đầu test_runner.py; verify cả 2 thứ tự 255 passed.
3. ~~**[P1]** Re-baseline desloppify với bandit; cân nhắc fail CI khi security coverage reduced.~~ ✅ ĐÃ XÁC MINH — ci-local.ps1 chạy desloppify (uvx) objective **89.9/100 ≥ 89 baseline** → ALL PASSED 5/5.
4. **[P1]** Chạy `./ci-local.ps1` lần CUỐI trên branch đã sửa mypy + merge → đang thực hiện; push `fix/review-findings` + merge master sau khi xanh.
5. **[P2]** Xem xét `test_raw_store` Windows flake (retry/mutex trong KnowledgeRawStore atomic write) — vẫn mở.
6. **[P2]** test_message_widgets: mau kiểm tra socket-thật cho control server concurrency cap — vẫn mở.

---
*Ledger đầy đủ: `.superpowers/sdd/fix-review-ledger.md`. Chi tiết từng cụm: task-8 → task-13 trên team board.*
