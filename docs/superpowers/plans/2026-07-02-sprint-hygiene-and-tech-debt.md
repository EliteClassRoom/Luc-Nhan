# Sprint: Hygiene & Tech-Debt Reduction (2026-07-02 → 2026-07-16)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Đóng issue #3 (test rewrite), vá CHANGELOG gap cho v1.6.0, hoàn tất nốt dây cuối cùng của C.4 (pseudo tool schemas) + port 3 fork helpers vào `ida/tools/functions.py`, hoàn tất 2 quality refactors còn lại (Q-007, Q-009), và re-review desloppify subjective items để đẩy strict score từ 84.2 → 85.0+.

**Architecture:** Mỗi phase là một work-stream độc lập, ship được riêng. Phase 0-1 là hygiene/setup, Phase 2-3 là cleanup nhỏ, Phase 4 là refactor có rủi ro, Phase 5 là subjective re-review. Mọi phase đều merge vào `master` qua PR riêng sau khi `./ci-local.sh` pass.

**Tech Stack:** Python 3.10-3.12, pytest, ruff, mypy, desloppify, GitHub Actions.

## Global Constraints

- **IDAPython imports**: Mọi `import ida_*` PHẢI đi qua `importlib.import_module()` trong `try/except ImportError` (Shiboken UAF guard).
- **Thread safety**: Mọi IDA API call chạy trên main thread; tool mutating phải có `@idasync`.
- **immutable patterns**: KHÔNG mutate dataclass instances; dùng `dataclasses.replace()`.
- **Version sync**: Bất kỳ thay đổi nào về behavior phải cập nhật đồng thời `pyproject.toml` + `ida-plugin.json` + `lucnhan/constants.py` (nếu bump version).
- **Local CI required**: Chạy `./ci-local.sh` trước mỗi commit; push lên `master` không trigger CI upstream (workflow chỉ chạy `[main, dev]`).
- **Test coverage**: Không giảm coverage dưới mức hiện tại (theo desloppify: Test health 100.0% / strict 67.4%).
- **Commit style**: `<type>(scope): description` — types: feat, fix, refactor, docs, test, chore, perf, ci.
- **Branch prefix**: `feat/`, `fix/`, `refactor/`, `chore/`, `docs/`.

---

## File Structure (sprint touchpoints)

### Modify
- `CHANGELOG.md` — thêm v1.6.0 entry
- `.github/workflows/ci.yml` — xóa `--ignore=tests/ui/test_a2a_widget.py`
- `tests/ui/test_a2a_widget.py` — rewrite theo new threading model
- `lucnhan/agent/loop.py` — import + remove inline `DELEGATE_EXTERNAL_TASK_SCHEMA`
- `lucnhan/ida/tools/functions.py` — wire 3 fork helpers
- `lucnhan/agent/bulk_renamer.py` — Q-007: tách `_run_quick` còn lại
- `lucnhan/ui/tool_widgets.py` — Q-009: verify/fill dict-of-handlers
- `pyproject.toml`, `ida-plugin.json`, `lucnhan/constants.py` — bump version v1.6.1 (nếu release)

### Reference (đọc, không sửa trừ khi cần)
- `lucnhan/agent/pseudo_tool_schemas.py` — schema definitions
- `lucnhan/ui/a2a_widget.py` — new threading model API
- `lucnhan/tools/formatting.py`, `lucnhan/tools/pagination.py`, `lucnhan/tools/value_format.py` — fork helpers đã port
- `docs/FORK_MIGRATION_ASSESSMENT.md` — assessment lịch sử (đã stale một phần)

### Create
- (Không có file mới trong sprint này; toàn bộ là wire-up + refactor)

---

## Phase 0: Hygiene (Day 0 — ~2 giờ)

> Mục tiêu: dọn nhanh các gap tích lũy, unblock mọi công việc khác.

### Task 0.1: Update CHANGELOG v1.6.0

**Files:**
- Modify: `CHANGELOG.md:7-8` (chèn ngay sau `# Changelog` header intro, trước `## [1.5.0]`)

**Context:** v1.6.0 đã được tag (`v1.6.0` tại commit `722d326 chore(release): bump version to 1.6.0`) nhưng CHANGELOG chỉ document đến 1.5.0. Đây là gap vi phạm Keep a Changelog convention.

**Diff to apply:**
```markdown
## [1.6.0] — 2026-07-02

### Added
- `set_runtime_config` wiring in `lucnhan/web/__init__.py` (fixes silent `getattr` no-op; security-constant-real-bug step 2).
- `EXECUTE_PYTHON_TOOL_NAME` constant in `lucnhan/constants.py` (security-constant-real-bug step 1).

### Fixed
- CI: master trigger + push hook + concurrency + Python 3.12 matrix (`f191722`).
- CI: base_ref diff guard for push events + runtime deps install in test job (`d2545d0`).
- CI: pin Python 3.11 via uv for reproducible desloppify scores (`d518cb6`).
- Release: sync version check across 3 sources (`4270f69`).
- Subprocess injection guard in a2a (port from fork `57caf5e`).
- `a2a_widget` threading model refactored from `QThread`/`_A2AWorker(QObject)` to `threading.Thread` + `queue.Queue` + `QTimer` polling.

### Refactor / Quality
- Pseudo tool schemas extracted to `lucnhan/agent/pseudo_tool_schemas.py` (6 of 7 schemas imported into `loop.py`; `DELEGATE_EXTERNAL_TASK_SCHEMA` import pending — see Phase 2).
- Purged IDA 8.x `ida_struct` paths from `types_tools.py` (step 8 of dead-code-purge).
- Removed duplicate `completed_tool_call_ids.add()` at `loop.py:775` (step 7).
- Removed 58 empty legacy `{dark:'', light:''}` dict constants (step 6).
- Applied ruff format to 9 files + removed invalid `noqa` (step 5).
- Extended `sanitize.py` with `strip_lone_surrogates` + `sanitize_messages_for_provider`.

### Tooling
- Added Dependabot weekly config with grouped updates.
- Added `CODEOWNERS` for security and CI paths.
```

**Steps:**
- [ ] **Step 1:** Mở `CHANGELOG.md`, xác nhận v1.5.0 entry nằm ở dòng 8
- [ ] **Step 2:** Chèn block trên ngay trước `## [1.5.0] — 2026-06-29` (giữa dòng 6 và 8)
- [ ] **Step 3:** Verify format đúng (Markdown table heading levels, bullets consistent)
- [ ] **Step 4:** Commit: `git add CHANGELOG.md && git commit -m "docs(changelog): add v1.6.0 release notes"`
- [ ] **Step 5:** Push: `git push origin master`

---

### Task 0.2: Merge 2 Dependabot PRs

**Files:**
- PR #1: `dependabot/github_actions/actions-f144c02174` → `master` (5 GitHub Actions updates)
- PR #2: `dependabot/pip/html2text-gte-2025.4.15` → `master` (html2text bump)

**Steps:**
- [ ] **Step 1:** Mở https://github.com/EliteClassRoom/Luc-Nhan/pulls?q=is%3Apr+is%3Aopen+author%3Aapp%2Fdependabot
- [ ] **Step 2:** Cho PR #1: review changelog, click "Approve" → "Merge pull request" (button click)
- [ ] **Step 3:** Cho PR #2: review changelog, click "Approve" → "Merge pull request" (button click)
- [ ] **Step 4:** `git pull origin master` local
- [ ] **Step 5:** Chạy `./ci-local.sh` local để verify (cảm ơn fork master không trigger CI upstream)

**Acceptance:** Cả 2 PR đã merge, `git log --oneline -5` hiện Dependabot commits trên master, `./ci-local.sh` pass.

---

### Task 0.3: Commit 13 resolved desloppify issues

**Files:**
- Có thể đã có 13 uncommitted changes trong working tree hoặc staged từ lần scan trước.

**Steps:**
- [ ] **Step 1:** Chạy `git status` để xem có uncommitted changes không
- [ ] **Step 2:** Nếu có: `desloppify plan commit-log` để xem diff tóm tắt 13 fixes
- [ ] **Step 3:** Review từng fix ngắn gọn (desloppify auto-fix an toàn cho naming/imports)
- [ ] **Step 4:** `git add . && git commit -m "chore(quality): apply 13 desloppify auto-fixes"`
- [ ] **Step 5:** `git push origin master`

**Acceptance:** `desloppify status` không còn warning về "13 resolved issues uncommitted".

---

## Phase 1: Test Health — Issue #3 Rewrite (Day 1-3 — ~2 ngày)

> Mục tiêu: đóng issue #3, restore full test coverage cho `a2a_widget`, xóa `--ignore` flag khỏi CI.

### Task 1.1: Map new a2a_widget threading model

**Files:**
- Read: `lucnhan/ui/a2a_widget.py:88-250` (full `_A2ATaskRunner` + event types)
- Read: `lucnhan/ui/a2a_widget.py:302-907` (`A2ABridgeWidget` + methods)
- Read: `tests/ui/test_a2a_widget.py:1-50` (current skip mark + helper setup)

**Context:** Cần hiểu API mới trước khi viết tests. Threading model mới:
- `_A2ATaskRunner` (no Qt base) chạy `A2ADispatcher.run_task` trong `threading.Thread`
- Runner expose `queue: queue.Queue[_A2ATaskEvent]` (background thread writes, widget polls)
- Widget dùng `QTimer` poll queue, route events đến UI handlers
- Cancellation: `runner.cancel()` sets `threading.Event` (idempotent, thread-safe)
- Shutdown: `runner.is_alive()` + `runner.join(timeout)` (daemon threads)

**Steps:**
- [ ] **Step 1:** Đọc `lucnhan/ui/a2a_widget.py:88-250`, list ra:
  - `_A2ARunnerEventType` enum values
  - `_A2ATaskEvent` dataclass fields
  - `_A2ATaskRunner.__init__` signature + public attrs (`queue`, `_cancel_event`)
  - `_A2ATaskRunner.start/cancel/is_alive/join` signatures
- [ ] **Step 2:** Đọc `lucnhan/ui/a2a_widget.py:302-907`, list ra:
  - `A2ABridgeWidget.__init__` params + public signal/slot names
  - Method `_on_send_clicked` (tạo runner + start)
  - Method `_poll_queue` (nếu có) — drains queue, route to UI
  - Method `_shutdown` — joins threads
  - Public signal: `task_dispatched(task_id, agent_name, task)`
  - Public method: `refresh_agents()` dùng dispatcher
- [ ] **Step 3:** Đọc `tests/ui/test_a2a_widget.py:1-50`, list:
  - `pytestmark = pytest.mark.skip(...)` — cần xóa
  - `_build_widget_with_mocks` helper signature
  - `_FakeAgent` class shape
  - Mocks/patches hiện tại
- [ ] **Step 4:** Viết notes vào comment trong test file (hoặc scratch note local) mapping: cũ `QThread`/`_A2AWorker` → mới `_A2ATaskRunner`/queue-poll

**Output:** Notes rõ ràng về new API surface, sẵn sàng cho Task 1.2.

---

### Task 1.2: Rewrite 12 failing tests

**Files:**
- Modify: `tests/ui/test_a2a_widget.py:50` (xóa `pytestmark`)
- Modify: `tests/ui/test_a2a_widget.py:184-260` (rewrite `TestSendClick` + `TestWorkerSignalHandlers`)
- Modify: `tests/ui/test_a2a_widget.py:299-340` (rewrite `TestCancelHandler` + `TestShutdown`)
- Modify: `tests/ui/test_a2a_widget.py:359-410` (rewrite `TestWorker`)

**Context:** Issue #3 list 12 failing tests. Mapping cũ → mới theo API mới:
- `test_signal_emitted_on_send` (TestSendClick) → verify `task_dispatched` signal emit vẫn work (vì signal vẫn còn trong widget, chỉ internal worker thay đổi)
- 6× `TestWorkerSignalHandlers` → đổi tên thành `TestPollEventHandlers` (vì giờ widget polls queue, không nhận Qt signal từ worker)
  - `test_started_sets_running`: enqueue STARTED event, gọi `_poll_queue`, verify row status
  - `test_output_appends`: enqueue 2× OUTPUT event, gọi `_poll_queue`, verify `result_text` append
  - `test_completed_marks_status`: enqueue COMPLETED, verify status
  - `test_failed_marks_error`: enqueue FAILED, verify error state
  - `test_cancelled_marks_status`: enqueue CANCELLED, verify status
- `test_cancel_sets_event` (TestCancelHandler) → verify `runner.cancel()` set `_cancel_event` (vẫn work vì `_A2ATaskRunner.cancel()` wraps `self._cancel_event.set()`)
- `test_shutdown_cancels_inflight` (TestShutdown) → verify `_shutdown` join threads + clear `_inflight`
- 3× `TestWorker` → đổi tên `TestTaskRunner`:
  - `test_runner_stores_arguments`: verify `__init__` stores fields
  - `test_runner_run_emits_started_output_completed`: chạy `_run()` thật với mock dispatcher, drain queue, verify event sequence
  - `test_runner_run_emits_failed_on_exception`: mock dispatcher raises, verify FAILED event

**Approach (TDD):**
- Mỗi test mới: viết test, chạy (`pytest tests/ui/test_a2a_widget.py::TestX -v`), confirm FAIL, viết minimal helper nếu cần, confirm PASS
- KHÔNG sửa production code trong task này — chỉ align tests với new API

**Steps:**
- [ ] **Step 1:** Xóa `pytestmark = pytest.mark.skip(...)` ở `tests/ui/test_a2a_widget.py:50`
- [ ] **Step 2:** Chạy `python3 -m pytest tests/ui/test_a2a_widget.py -v 2>&1 | head -30` — confirm 12 tests fail
- [ ] **Step 3:** Rewrite `TestSendClick.test_signal_emitted_on_send` để dùng `_on_send_clicked` mới (xóa `patch("...QThread")`)
- [ ] **Step 4:** Chạy test đó riêng: `python3 -m pytest tests/ui/test_a2a_widget.py::TestSendClick::test_signal_emitted_on_send -v` — confirm PASS
- [ ] **Step 5:** Rewrite 6 tests trong `TestWorkerSignalHandlers` → `TestPollEventHandlers` — enqueue events trực tiếp vào `runner.queue`, gọi widget's poll method
- [ ] **Step 6:** Chạy: `python3 -m pytest tests/ui/test_a2a_widget.py::TestPollEventHandlers -v` — confirm 6 PASS
- [ ] **Step 7:** Rewrite `TestCancelHandler` (không cần patch QThread, chỉ verify `runner.cancel()` effect)
- [ ] **Step 8:** Chạy: `python3 -m pytest tests/ui/test_a2a_widget.py::TestCancelHandler -v` — confirm PASS
- [ ] **Step 9:** Rewrite `TestShutdown` (verify `_shutdown` clears `_inflight` map)
- [ ] **Step 10:** Chạy: `python3 -m pytest tests/ui/test_a2a_widget.py::TestShutdown -v` — confirm PASS
- [ ] **Step 11:** Rewrite `TestWorker` → `TestTaskRunner` (instantiate `_A2ATaskRunner` directly, mock `A2ADispatcher`)
- [ ] **Step 12:** Chạy: `python3 -m pytest tests/ui/test_a2a_widget.py::TestTaskRunner -v` — confirm 3 PASS
- [ ] **Step 13:** Chạy full file: `python3 -m pytest tests/ui/test_a2a_widget.py -v` — confirm 0 fail, 0 skip
- [ ] **Step 14:** Commit: `git add tests/ui/test_a2a_widget.py && git commit -m "test(a2a_widget): rewrite 12 tests for threading.Thread + queue-poll model (fixes #3)"`
- [ ] **Step 15:** Push: `git push origin master`

**Acceptance:** `pytest tests/ui/test_a2a_widget.py` 0 fail, 0 skip; issue #3 comment update "ready for review".

---

### Task 1.3: Restore CI test coverage

**Files:**
- Modify: `.github/workflows/ci.yml:103` (xóa `--ignore=tests/ui/test_a2a_widget.py`)

**Context:** Sau Task 1.2, file đã pass nhưng CI upstream vẫn skip do `--ignore` flag. Cần xóa flag để CI thực sự chạy file này.

**Steps:**
- [ ] **Step 1:** Đọc `.github/workflows/ci.yml:100-110`, xác nhận dòng ignore
- [ ] **Step 2:** Edit dòng đó, xóa `--ignore=tests/ui/test_a2a_widget.py` (giữ nguyên các flag khác)
- [ ] **Step 3:** Commit: `git add .github/workflows/ci.yml && git commit -m "ci(test): drop --ignore for tests/ui/test_a2a_widget.py (closes #3)"`
- [ ] **Step 4:** Push: `git push origin master`
- [ ] **Step 5:** Manual verify local: `python3 -m pytest tests/ --tb=short -q` (KHÔNG có `--ignore`) — confirm pass

**Acceptance:** `ci.yml` không còn `--ignore`; full test suite chạy local pass.

**⚠️ Note:** Vì CI upstream không trigger trên fork master, hành động này chỉ effective khi upstream merge ngược. Issue #3 chính thức close khi maintainer accept PR.

---

## Phase 2: C.4 — Wire remaining pseudo tool schema (Day 3-4 — ~4 giờ)

> Mục tiêu: hoàn tất việc wire `DELEGATE_EXTERNAL_TASK_SCHEMA` vào `loop.py`, xóa 2 inline schema còn lại.

### Task 2.1: Verify DELEGATE_EXTERNAL_TASK_SCHEMA exists

**Files:**
- Read: `lucnhan/agent/pseudo_tool_schemas.py` (full file, ~13KB)

**Context:** `loop.py:66-73` import 6 schemas (ASK_USER, EXPLORATION_REPORT, PHASE_TRANSITION, RESEARCH_NOTE, SAVE_MEMORY, SPAWN_SUBAGENT). Assessment cho rằng có 32 inline schemas — verify thực tế chỉ còn 2 (`grep -cE '^\s+"description":'` returned 2, một cho delegate_external_task, một có thể là pseudo khác). Cần xác nhận schema tồn tại trong `pseudo_tool_schemas.py`.

**Steps:**
- [ ] **Step 1:** `grep -n "DELEGATE_EXTERNAL_TASK" lucnhan/agent/pseudo_tool_schemas.py` — confirm defined
- [ ] **Step 2:** Nếu KHÔNG có, cần define thêm trong file (xem file hiện tại để bám sát style)
- [ ] **Step 3:** Verify các schema tương tự (delegation, A2A, external task) đã có trong `loop.py` chưa

---

### Task 2.2: Add DELEGATE_EXTERNAL_TASK_SCHEMA to loop.py import

**Files:**
- Modify: `lucnhan/agent/loop.py:66-73` (thêm 1 dòng vào import block)

**Steps:**
- [ ] **Step 1:** Edit import block, thêm `DELEGATE_EXTERNAL_TASK_SCHEMA,` (giữ alphabetical order)
- [ ] **Step 2:** Chạy `python3 -c "from lucnhan.agent.loop import DELEGATE_EXTERNAL_TASK_SCHEMA"` — confirm import OK
- [ ] **Step 3:** Commit: `git add lucnhan/agent/loop.py && git commit -m "refactor(agent): import DELEGATE_EXTERNAL_TASK_SCHEMA from pseudo_tool_schemas"`
- [ ] **Step 4:** Push: `git push origin master`

---

### Task 2.3: Remove inline DELEGATE_EXTERNAL_TASK schema from loop.py

**Files:**
- Modify: `lucnhan/agent/loop.py` (tìm inline schema literal cho delegate_external_task, thay bằng reference đến imported constant)

**Context:** Có 1 inline schema literal cho delegate_external_task tool. Sau Task 2.2, constant đã available — chỉ cần thay tham chiếu.

**Steps:**
- [ ] **Step 1:** `grep -nE 'delegate_external_task' lucnhan/agent/loop.py` — tìm vị trí inline schema
- [ ] **Step 2:** Đọc context (50 dòng quanh), xác nhận schema shape
- [ ] **Step 3:** Thay inline dict literal bằng reference: `DELEGATE_EXTERNAL_TASK_SCHEMA` (giữ tên biến consistent với inline literal cũ)
- [ ] **Step 4:** Chạy: `python3 -m pytest tests/agent/test_agent_loop.py -v` — confirm pass
- [ ] **Step 5:** Chạy: `python3 -m mypy lucnhan/core lucnhan/providers lucnhan/agent` — confirm no error
- [ ] **Step 6:** Commit: `git add lucnhan/agent/loop.py && git commit -m "refactor(agent): replace inline delegate_external_task schema with imported constant (C.4 final step)"`
- [ ] **Step 7:** Push: `git push origin master`

**Acceptance:** `grep -cE '^\s+"description":' lucnhan/agent/loop.py` returns 0 (hoặc giảm 1).

---

## Phase 3: Wire 3 fork helpers into ida/tools/functions.py (Day 4-5 — ~6 giờ)

> Mục tiêu: tận dụng 245 LOC helpers đã port sẵn (`formatting.py:58`, `pagination.py:37`, `value_format.py:150`) để DRY `ida/tools/functions.py` (186 LOC).

### Task 3.1: Identify inline duplicates in functions.py

**Files:**
- Read: `lucnhan/ida/tools/functions.py` (full file, 186 lines)
- Read: `lucnhan/tools/formatting.py` (`format_function_summary`, `format_callers_callees`)
- Read: `lucnhan/tools/pagination.py` (`normalize_page`, `format_page`)
- Read: `lucnhan/tools/value_format.py` (`format_global_value`, `normalize_type_hint`, `bytes_needed_for_type`)

**Steps:**
- [ ] **Step 1:** Đọc `functions.py` end-to-end, note từng function dùng inline formatting
- [ ] **Step 2:** Cross-check với 3 helpers file:
  - Có function nào trong `functions.py` reimplement `format_function_summary`?
  - Có chỗ nào hardcode `format_global_value` logic thay vì import?
  - Có pagination logic nào inline (vd `start = page * limit; end = start + limit`)?
- [ ] **Step 3:** Lập list cụ thể: function name + line range + target helper

**Output:** Bảng ánh xạ `functions.py:LINE-RANGE` → `helpers.HELPER_NAME`.

---

### Task 3.2: Wire format_function_summary

**Files:**
- Modify: `lucnhan/ida/tools/functions.py` (thay inline formatting bằng `from lucnhan.tools.formatting import format_function_summary`)

**Context:** Chỉ wire nếu Task 3.1 xác nhận có duplicate. Nếu không có, skip task này.

**Steps:**
- [ ] **Step 1:** Thêm import: `from lucnhan.tools.formatting import format_function_summary`
- [ ] **Step 2:** Replace inline code → `format_function_summary(...)` call
- [ ] **Step 3:** Chạy: `python3 -m pytest tests/tools/ -k "functions" -v` — confirm pass
- [ ] **Step 4:** Chạy: `python3 -m mypy lucnhan/ida/tools/functions.py` — confirm no error
- [ ] **Step 5:** Commit: `git add lucnhan/ida/tools/functions.py && git commit -m "refactor(tools): use format_function_summary from lucnhan.tools.formatting"`

---

### Task 3.3: Wire format_global_value + normalize_type_hint

**Files:**
- Modify: `lucnhan/ida/tools/functions.py` (tương tự Task 3.2 với value helpers)

**Steps:**
- [ ] **Step 1:** Thêm imports: `from lucnhan.tools.value_format import format_global_value, normalize_type_hint`
- [ ] **Step 2:** Replace inline value formatting
- [ ] **Step 3:** Chạy: `python3 -m pytest tests/tools/ -k "functions or value" -v` — confirm pass
- [ ] **Step 4:** Commit: `git add lucnhan/ida/tools/functions.py && git commit -m "refactor(tools): use format_global_value from lucnhan.tools.value_format"`

---

### Task 3.4: Wire pagination helpers

**Files:**
- Modify: `lucnhan/ida/tools/functions.py` (nếu có pagination logic inline)

**Steps:**
- [ ] **Step 1:** Thêm imports: `from lucnhan.tools.pagination import normalize_page, format_page`
- [ ] **Step 2:** Replace inline pagination arithmetic
- [ ] **Step 3:** Chạy: `python3 -m pytest tests/tools/ -v` — confirm pass
- [ ] **Step 4:** Commit: `git add lucnhan/ida/tools/functions.py && git commit -m "refactor(tools): use normalize_page/format_page from lucnhan.tools.pagination"`

---

### Task 3.5: Verify LOC reduction + run full CI

**Steps:**
- [ ] **Step 1:** `wc -l lucnhan/ida/tools/functions.py` — verify LOC giảm
- [ ] **Step 2:** Chạy `./ci-local.sh` — confirm pass sạch
- [ ] **Step 3:** `desloppify scan` — verify score cải thiện (mục tiêu: +0.3 strict)
- [ ] **Step 4:** Nếu cần, push commits tích lũy: `git push origin master`

**Acceptance:** `functions.py` giảm ≥ 20 LOC, full test pass, desloppify score không giảm.

---

## Phase 4: Quality Refactors Q-007 + Q-009 (Day 5-7 — ~1.5 ngày)

> Mục tiêu: hoàn tất 2 quality refactors còn lại từ FORK_MIGRATION_ASSESSMENT (Q-007 đã partial refactor, Q-009 đã dùng dict-of-handlers).

### Task 4.1: Q-007 — Complete _run_quick refactor in bulk_renamer.py

**Files:**
- Read: `lucnhan/agent/bulk_renamer.py:405-600` (current `_run_quick` + helpers)
- Modify: `lucnhan/agent/bulk_renamer.py:407-?` (split into `_quick_decompile_jobs` + `_quick_split_batches` + `_quick_run_batch`)

**Context:** Assessment gốc nói `_run_quick` 197 dòng. Hiện tại đã partial refactor: có `_quick_decompile_jobs` + `_quick_split_batches` helpers (thấy ở signature). Cần verify xem còn phần "Phase 3" (parallel sub-batches) inline hay đã extract.

**Steps:**
- [ ] **Step 1:** Đọc `_run_quick` end-to-end, list các phần còn inline
- [ ] **Step 2:** Nếu Phase 3 (parallel run) còn > 30 dòng, extract thành `_quick_run_batch(sub_batch)` method
- [ ] **Step 3:** Đảm bảo `_run_quick` chỉ còn orchestration logic (< 50 dòng)
- [ ] **Step 4:** Chạy: `python3 -m pytest tests/agent/ -k "bulk" -v` — confirm pass
- [ ] **Step 5:** Commit: `git add lucnhan/agent/bulk_renamer.py && git commit -m "refactor(bulk_renamer): extract _quick_run_batch from _run_quick (Q-007)"`
- [ ] **Step 6:** Push: `git push origin master`

**Acceptance:** `_run_quick` method < 50 dòng, tất cả sub-steps đã extract.

---

### Task 4.2: Q-009 — Verify/fill _format_tool_summary in tool_widgets.py

**Files:**
- Read: `lucnhan/ui/tool_widgets.py:342-?` (current implementation)
- Modify: `lucnhan/ui/tool_widgets.py:344-?` (nếu còn if/elif chain)

**Context:** Code hiện đã dùng `_TOOL_SUMMARY_FORMATTERS.get(short_name)` pattern (dict-of-handlers). Cần verify:
- (a) Dict đã cover hết tools chưa (không còn fallback if/elif)
- (b) Mỗi handler ngắn gọn (< 10 dòng)

**Steps:**
- [ ] **Step 1:** Đọc `_format_tool_summary` end-to-end + `_TOOL_SUMMARY_FORMATTERS` dict
- [ ] **Step 2:** Nếu có tools phổ biến (vd `decompile_function`, `rename_function`, `list_functions`) chưa có handler, thêm vào dict
- [ ] **Step 3:** Nếu có fallback logic `else: return str(args)` → giữ nguyên (acceptable)
- [ ] **Step 4:** Chạy: `python3 -m pytest tests/ui/test_tool_widgets.py -v` — confirm pass (nếu test file tồn tại)
- [ ] **Step 5:** Commit (nếu có thay đổi): `git add lucnhan/ui/tool_widgets.py && git commit -m "refactor(tool_widgets): fill missing _TOOL_SUMMARY_FORMATTERS entries (Q-009)"`
- [ ] **Step 6:** Push: `git push origin master`

**Acceptance:** `_format_tool_summary` < 50 dòng hoặc đã fully dict-driven.

---

### Task 4.3: Run desloppify scan, verify score

**Steps:**
- [ ] **Step 1:** Chạy `desloppify scan` (sau khi cài uv nếu cần)
- [ ] **Step 2:** Verify overall ≥ 86.0, strict ≥ 84.5 (mục tiêu ban đầu là 85.0)
- [ ] **Step 3:** Nếu chưa đạt, xem `desloppify issues` work queue, chọn 2-3 items dễ nhất fix
- [ ] **Step 4:** Commit fixes nếu có

---

## Phase 5: Desloppify Subjective Re-Review (Day 7-8 — ~6 giờ)

> Mục tiêu: refresh 6 stale subjective items, đẩy strict score từ 84.2 → 85.0+.

### Task 5.1: Prepare subjective re-review batches

**Files:**
- (Không sửa code trong task này)

**Steps:**
- [ ] **Step 1:** Chạy `desloppify review --prepare --dimensions incomplete_migration` (theo output gợi ý của `desloppify next`)
- [ ] **Step 2:** Verify output dir chứa N prompt files (1 per dimension)
- [ ] **Step 3:** Đọc qua 1-2 prompt files để hiểu format câu hỏi
- [ ] **Step 4:** Note dimensions cần review: `Stale migration` (78.0%), 8 dimensions khác stale

---

### Task 5.2: Dispatch parallel review subagents

**Files:**
- (Tạo review output trong `--run-dir`)

**Context:** Theo hướng dẫn desloppify: "Launch one subagent per prompt, all in parallel."

**Steps:**
- [ ] **Step 1:** Với mỗi prompt file trong output dir, dispatch 1 subagent (model Sonnet) để chạy review
- [ ] **Step 2:** Parallel dispatch để tăng tốc (cap concurrency theo CPU cores - 2)
- [ ] **Step 3:** Đợi tất cả subagents hoàn tất, thu thập output JSON
- [ ] **Step 4:** Verify mỗi output có `findings: []` hoặc issue list

**Output:** Một run-dir chứa N JSON outputs, mỗi file cho 1 dimension.

---

### Task 5.3: Import review results + scan

**Steps:**
- [ ] **Step 1:** Chạy: `desloppify review --import-run <run-dir> --scan-after-import`
- [ ] **Step 2:** Verify scan complete, không có error
- [ ] **Step 3:** Chạy: `desloppify status` — check strict score
- [ ] **Step 4:** Nếu strict ≥ 85.0: celebration 🎉
- [ ] **Step 5:** Nếu strict < 85.0: xem `desloppify issues` mới surfaced, chọn 2-3 quick wins

---

### Task 5.4: Apply quick wins from re-review

**Steps:**
- [ ] **Step 1:** Đọc output `desloppify issues` — focus subjective items giờ un-stale
- [ ] **Step 2:** Với mỗi quick win (effort S):
  - Viết fix
  - Chạy `./ci-local.sh`
  - Commit: `chore(quality): <desloppify-finding-id>`
- [ ] **Step 3:** Chạy: `desloppify scan` — verify score cải thiện
- [ ] **Step 4:** Push tất cả commits: `git push origin master`

**Acceptance:** Strict score ≥ 85.0, no new HIGH findings introduced.

---

## Sprint Wrap-up (Day 8)

### Task W.1: Final verification

**Steps:**
- [ ] **Step 1:** `./ci-local.sh` pass sạch
- [ ] **Step 2:** `desloppify status` — strict ≥ 85.0, overall ≥ 87.0
- [ ] **Step 3:** `git log --oneline master -20` — review tất cả commits sprint này
- [ ] **Step 4:** `pytest tests/ -v` — full test suite pass (1772+ tests)
- [ ] **Step 5:** Nếu có behavior changes đáng kể → bump version v1.6.1 (sync 3 nguồn: `pyproject.toml`, `ida-plugin.json`, `lucnhan/constants.py`)
- [ ] **Step 6:** Nếu bump version: commit `chore(release): bump version to 1.6.1`, push, tag

---

## Definition of Done (Sprint-level)

- [ ] Issue #3 closed, `--ignore` flag removed from `ci.yml`
- [ ] CHANGELOG có entry v1.6.0 với sections Added/Changed/Fixed/Refactor
- [ ] 2 Dependabot PRs merged
- [ ] 13 resolved desloppify issues committed
- [ ] C.4 hoàn tất: `DELEGATE_EXTERNAL_TASK_SCHEMA` imported + 0 inline schemas trong `loop.py`
- [ ] 3 fork helpers wired vào `ida/tools/functions.py` (giảm ≥ 20 LOC)
- [ ] Q-007 + Q-009 refactors merged
- [ ] Desloppify strict ≥ 85.0
- [ ] `./ci-local.sh` pass sạch
- [ ] All branches pushed to `origin/master`
- [ ] (Optional) v1.6.1 tagged nếu có behavior changes

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Test rewrite tests pass locally nhưng fail trên CI do threading timing | Med | High | Dùng `threading.Event` + `queue.join()` để sync thay vì `time.sleep()` |
| Wire helpers breaks existing IDA tool callers | Med | High | Chạy `./ci-local.sh` + manual smoke test trong IDA trước khi merge |
| Q-007 refactor breaks bulk rename flow | Low | High | TDD: viết test cho `_quick_run_batch` extraction trước khi refactor |
| Desloppify subjective re-review tốn nhiều token / timeout | Med | Med | Parallel subagents, dry-run trước, có thể skip re-review và dùng lại 84.2 baseline |
| Phase 1.2 test rewrite quá phức tạp, kéo dài > 2 ngày | Med | Med | Tách sub-task: rewrite theo class, mỗi class 1 commit riêng |
| CI upstream không trigger → không verify được merge end-to-end | High | Low | Đã known limitation; rely on `./ci-local.sh` local |

## Out of Scope (defer to next sprint)

- Tier 2 items: Q-008 `_analyze_one` dedup, Q-005 `do_POST` dict-of-handlers, Q-014 message_widgets (đã fix)
- Tier 3 items: Split `loop.py`/`chat_view.py`/`panel_core.py`/`settings_dialog.py` (mỗi file > 1300 dòng)
- Tier 3 item: `styles.py` refactor (~2758 dòng → template + tokens)
- D.3: 6 test isolation bugs (xfail) — chưa có capacity trong sprint này
- E: Doc sync (llms.txt, webpage)
- B.5: Refactor `openai_provider._format_messages` (giữ logic dedup)
- A2A/headless feature additions

---

**Plan complete and saved to `docs/superpowers/plans/2026-07-02-sprint-hygiene-and-tech-debt.md`.**

Two execution options:

1. **Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration. Best cho: Phase 1.2 (test rewrite cần fresh context), Phase 5 (parallel re-review).

2. **Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints. Best cho: Phase 0 (quick wins), Phase 2 (small wire-up).

**Which approach?**
