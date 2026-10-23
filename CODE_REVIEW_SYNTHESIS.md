# Báo cáo tổng hợp đánh giá codebase Luc Nhan (Luc Nhân) — IDA Pro plugin

**Người tổng hợp:** lead (Team Lead)
**Phương pháp:** 7 reviewer agents chuyên biệt, mỗi agent đánh giá độc lập một cụm module, kiểm chứng trực tiếp trên mã nguồn và đối chiếu với `CLAUDE.md`/`ARCHITECTURE.md`. Mục tiêu gốc: bảo mật + thread-safety + tuân thủ invariant thiết kế + chất lượng code + độ phủ test.

**Chạy tuần tự** do giới hạn rate-limit model (30 req/phút chia sẻ): 7/7 review đã hoàn thành, toàn bộ báo cáo chi tiết lưu trong task board (task-1 → task-7).

---

## Tổng quan

Đây là một **codebase rất lớn (≈74K LOC nguồn + ≈71K LOC test, 236 file .py + 259 file test)** với chất lượng tổng thể **cao.** Toàn bộ reviewer xác nhận đa số invariant của `CLAUDE.md` được tuân thủ nghiêm túc — đặc biệt ở 3 trụ cột kỹ thuật:

1. **Bảo mật dữ liệu bất tín (untrusted)** — stack sanitize + script-guard rất mạnh.
2. **Thread-safety IDA/Shiboken** — mọi import qua `importlib` + try/except, mọi giao tiếp cross-thread qua `queue.Queue`+`QTimer`.
3. **Test chất lượng** — các vùng security-critical được test adversarial cực kỹ.

Không có lỗ hổng **[CRITICAL]** được tìm thấy ở bất kỳ nhánh nào. Các phát hiện tập trung ở ranh giới (boundary), đường hiếm gặp, và **cấu hình CI/coverage-gate** (không phải chất lượng test).

---

## Ma trận kết quả theo module

| # | Task | Phạm vi | CRITICAL | HIGH | MEDIUM | Nội dung nổi bật |
|---|------|---------|:---:|:---:|:---:|------------------|
| 6 | Core + Security | core/, mcp/, script_guard, mutation | 0 | 0 | 2 | Sanitize choke-point tập trung, script-guard đa tầng, không auto-approve |
| 1 | Agent Core | agent/ (loop, turn, subagent, modes, orchestra, a2a) | 0 | 2 | 5 | `/a2a` thiếu sanitize; `_verify_mutation` phụ thuộc getter-raw |
| 2 | Tools | tools/ + ida/tools | 0 | 1 | 1 | `@tool` không tự áp `@idasync` (lệch CLAUDE.md) |
| 3 | Providers + Memory | providers/, memory/ | 0 | 2 | 1 | Persistence memory bỏ sót sanitize title/metadata |
| 4 | UI | ui/ + ida/ui | 0 | 1 | 3 | Memory-leak singleton ThemeManager trong message_widgets |
| 5 | Host + Headless | ida/, headless/, control/, cli/, state/ | 0 | 0 | 3 | Cả 6 tiêu chí headless-security đều đạt; hardening biên |
| 7 | Tests + CI | tests/, .github/, pyproject, ci-local | 0 | 4 | 6 | Test mạnh nhưng CI bỏ phủ Windows/tests/memory/ui |

**Tổng:** **0 CRITICAL · 10 HIGH · 21 MEDIUM** (+ nhiều LOW). Các HIGH phân bố đều và không tập trung thành một lỗ hổng chí mạng nào.

---

## Các phát hiện HIGH (ưu tiên sửa)

### A. Bảo mật / sanitization

**A1. [HIGH] `/a2a` không sanitize output từ external agent → prompt-injection**
- `lucnhan/agent/modes/a2a.py:119-120` — emit `TEXT_DONE` với `final = collected_text.strip()` trực tiếp, KHÔNG qua `sanitize_tool_result()`/`strip_injection_markers()`.
- Bất nhất với pseudo-tool `delegate_external_task` (`loop.py:2748`) có sanitize. Output từ external process (claude/codex CLI — nguồn không uy tín) đi thẳng vào chat + lịch sử.
- Sửa: áp tương tự `_handle_delegate_external_task_tool` (vd `sanitize_tool_result(final, "a2a")`) trước khi `text_done` và trước khi re-forward `TEXT_DELTA` (`a2a.py:95`).

**A2. [HIGH] `_verify_mutation` phụ thuộc contract "getter returns raw data"**
- `lucnhan/agent/loop.py:2789-2855` — compare `str(actual) == str(new_name)`/comment. Nếu getter (vd `get_function_name`) trả string đã format ⇒ mutation hợp lệ bị đánh fail ⇒ **entry undo bị mất sai lầm**.
- Sửa: test contract getter-raw; normalize trước khi compare; đảm bảo `capture_pre_state` dùng getter raw như CLAUDE.md.

**A3. [HIGH] Persistent memory bỏ sót sanitize ở nhánh exploration**
- `lucnhan/memory/service.py:251-270` — `save_exploration_finding` lưu `title/entity_refs/tags/verdict_claim/citations` **verbatim**, chỉ sanitize `content`. Đường đọc lại dùng `_sanitize_prompt_text` (regex mật độ thấp) yếu hơn `strip_injection_markers`. Title (thường= dòng đầu summary binary/MCP) có thể lọt vào `MEMORY.md` và mọi prompt sau.
- Sửa: sanitize toàn bộ metadata nhánh exploration; dùng `strip_injection_markers` cho mọi trường.

**A4. [HIGH] `manual_notes_context` đọc MEMORY.md bằng hàm sanitize yếu**
- `lucnhan/memory/service.py:145` — dùng `_sanitize_prompt_text` thay vì `sanitize_memory()` mà CLAUDE.md chỉ định. Sửa: chuyển sang `sanitize_memory()`.

### B. Kiến trúc / thread-safety

**B1. [HIGH] `@tool` KHÔNG tự áp `@idasync` — marshalling nằm ở registry**
- `lucnhan/tools/base.py:214-318` (`@tool`) vs `ida/tools/registry.py:114-115`/`tools/registry.py:367-368`. CLAUDE.md ghi "applied automatically by @tool" nhưng thực tế là `ToolRegistry.execute()` gói `dispatch_wrapper`. Handler gọi bypass registry sẽ **không** được marshal sang main-thread.
- Sửa: wrap `idasync` trong decorator, HOẶC cập nhật CLAUDE.md + thêm test mọi tool `main_thread=True` được wrap.

**B2. [HIGH] Memory-leak singleton: message widgets không disconnect ThemeManager**
- `lucnhan/ui/message_widgets.py:326,428,…,1720` — 13+ class widget connect `ThemeManager.instance().themeChanged.connect(self._apply_styles)` mà **không** có `disconnect_theme`/`shutdown`/`closeEvent`. Singleton sống cả process giữ bound-method ⇒ C++ QObject không được thu hồi dù `deleteLater()`. Mỗi `new-chat`/`switch IDB`/`close tab` tích lũy widget bị "pin".
- `theme/applicator.py` (`bind_theme`/`disconnect_theme`) đã được viết để giải quyết đúng vấn đề này nhưng **chưa migrate message_widgets** (các module khác đã dùng đúng).
- Sửa: chuyển sang `bind_theme`/`disconnect_theme`; disconnect ở `ChatView.shutdown()`.

### C. CI / Coverage-gate

**C1. [HIGH] CI không chạy Windows** dù plugin chạy chính trong IDA Pro trên Windows (`ci.yml:69` chỉ ubuntu; `ci-local.ps1` không được CI dùng).

**C2. [HIGH] Ruff không lint `tests/`** — `ci.yml:48` dùng `ruff check lucnhan/`, path tường minh bỏ qua `extend-include` của `pyproject.toml:68` (chỉ tác dụng với `ruff check .`).

**C3. [HIGH] Mypy không gate `memory`/`ui`** — CI pin path cụ thể `core providers`, bỏ qua `[tool.mypy].files` (`pyproject.toml:114-117`); `memory` là module an toàn dữ liệu.

**C4. [HIGH] `ida_mock.py` dùng `MagicMock()` cho đa số module IDA** — sentinel truthy/hashable che bug thật (`tests/mocks/ida_mock.py:120-139`); semantic thật của IDA là `None/False/BADADDR`.

---

## Phát hiện MEDIUM (nhóm chính)

**Thread-safety / đúng logic:**
- Orchestra `_handle_delegate_task` block `queue.get(timeout=300)` không poll cancellation khi chờ approval; `self._cancelled` không nối parent (`orchestra/main_agent.py:325-332`, hiện đang `_ORCHESTRA_ENABLED=False`).
- `subagent_manager.info.activity`/state ghi từ worker, UI đọc không lock — slice-delete không atomic (`subagent_manager.py:56-57,476-480`, `update_external:294-330`).
- Busy-wait `time.sleep(0.1/0.05)` trong spawn/join subagent không có hard cap tổng (`loop.py:2509-2511, 2561-2563`).
- `reload_mcp` fire-and-forget daemon thread không serialize (`ui/session_controller_base.py:1371`).
- `_oauth_thread` warm-up trong `__init__` cần xác minh Qt-free (Shiboken UAF) (`panel_core.py:483-485`).
- Headless hardening: `control/server.py:362` không socket-read timeout → slow-client thread exhaustion; `ida/dispatch.py:205` worker `event.wait()` không timeout → block shutdown.
- `state/session.py:353-374` `_trim_to_budget` có thể drop `messages[0]` (system prompt) + tạo orphan tool_result.

**Bảo mật biên:**
- `scripting._get_base_namespace` pre-import IDA module không bọc `SafeModule` → deny-set không đồng nhất (`scripting.py:36`); `_cached_namespace` dùng chung object có thể rò state.
- Validate LLM argument không đồng đều: `new_name`/`comment`/`type_string` không check empty; `json.loads` không bắt `JSONDecodeError` (`ida/tools/annotations.py:22-38,107-118,71-84`, `types_tools.py`).

**CI/process:**
- Desloppify baseline (89.9, CLAUDE ghi 89.0) sinh bằng scan thiếu bandit → mismatch + Security score phồng; CI chỉ so score không so coverage-status.
- pytest-timeout bị tắt; security-gate chỉ chạy PR + parse không check exit code.

---

## Điểm mạnh được xác nhận (bằng đọc mã)

1. **Cancellation nhất quán** — `_check_cancelled()` ở mọi retry loop, backoff, tool call, stream chunk (`agent/loop.py`). Mô hình `_wait_for_queue` poll-0.5s loại hẳn race threading.Event.
2. **TurnEvent stream chất lượng** — `BackgroundAgentRunner` coalescing, bounded put không drop lifecycle/approval events, sentinel timeout gọn.
3. **Sanitize chokepoint điển hình** — mọi tool result đều qua `sanitize_tool_result` (`loop.py:2213`), traceback chỉ đầy đủ cho `execute_python`.
4. **Script-guard đa tầng mạnh** — AST blocklist trước approval, `SafeModule` chặn transitive `uuid.os`/`re.sys`, namespace restricted, headless **auto-deny**, không auto-approve ở bất kỳ mode nào.
5. **100% tuân thủ Shiboken** — grep toàn repo không còn module-level `import ida_*`; `EXECUTE_PYTHON_TOOL_NAME` chỉ hardcode 1 chỗ trong constants.
6. **Mutation tracking drift-proof** — `_INTENTIONALLY_NON_REVERSIBLE` có lý do bắt buộc; `_drop_record` identity-based.
7. **Headless security đạt cả 6 tiêu chí** — loopback-only 2 lớp, Bearer token constant-time, `/health` không leak, bootstrap qua env JSON.
8. **Thread-safety UI đúng chuẩn** — queue.Queue+QTimer khắp nơi, `qt_compat` seam 100% (ida/ không import PySide6 trực tiếp), shutdown rất kỹ, approval gates button-only đúng (Stop là escape door).
9. **Test security-critical xuất sắc** — script_guard adversarial (963 LOC), control server side-effect-guards (1400+ LOC), sanitize obfuscation (995 LOC), approval-gate multi-face, provider/memory tham số hóa SQL toàn bộ.
10. **Memory subsystem defense-in-depth** — authority token non-serializable, WAL bắt buộc, atomic write, optimistic revision, cross-process lock.

---

## ACTION ITEMS ưu tiên (P0 — sửa ngay)

| Ưu tiên | Hành động | Gốc |
|---|---|---|
| P0 | Sanitize output `/a2a` + bổ sung test | task-1, task-7 |
| P0 | Sanitize title/metadata nhánh exploration + `manual_notes_context` qua `sanitize_memory()` | task-3 |
| P0 | Migrate `message_widgets.py` sang `bind_theme`/`disconnect_theme` | task-4 |
| P0 | Đồng bộ invariant `@idasync` (decorator hoặc CLAUDE.md + test) | task-2 |
| P0 | Thêm CI job Windows + `ruff check lucnhan/ tests/` + mypy gate memory/ui | task-7 |
| P0 | Test `_verify_mutation` getter-raw end-to-end | task-1, task-7 |
| P1 | Cập nhật `ida_mock.py` default thực tế (None/0/BADADDR) thay MagicMock sentinel | task-7 |
| P1 | Orchestra: poll cancellation + nối `_cancelled` với parent (trước re-enable) | task-1 |
| P1 | Thread-safe state SubagentInfo + serialize `reload_mcp` | task-1, task-4 |
| P1 | Desloppify re-baseline có bandit + CI fail nếu coverage reduced | task-7 |
| P2 | Bật pytest-timeout, fix flake `test_height_cached_label` | task-7 |
| P2 | Validate non-empty LLM args + bắt JSONDecodeError ở tools | task-2 |
| P2 | Gom magic numbers thành hằng số/config | nhiều task |

---

## Ghi chú phạm vi

Một số module chưa được đọc sâu (các reviewer đã liệt kê): `orchestra/tools/*`, `orchestra/prompts/*`, các `agents/*` reader/reviewer internals, `report_*.py`, `hypothesis_verification.py`, `modes/research.py`, `context_window.py`, `system_prompt.py`, `glm_guard.py`, `prompts/base.py`, `ida/ui/tools_form.py`. Nên review bổ sung ở phase sau nếu muốn phủ 100% `lucnhan/`.

---

*Tài liệu tổng hợp từ 7 báo cáo chi tiết: task-1 (Agent Core), task-2 (Tools), task-3 (Providers+Memory), task-4 (UI), task-5 (Host+Headless), task-6 (Core+Security), task-7 (Tests+CI).*
