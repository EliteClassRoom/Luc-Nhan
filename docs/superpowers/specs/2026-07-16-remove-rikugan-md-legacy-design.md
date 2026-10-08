# Remove RIKUGAN.md Legacy — Cutover to Central MEMORY.md

**Ngày:** 2026-07-16
**Trạng thái:** Approved (brainstormed 2026-07-16)
**Spec authority cho:** implementation plan kế tiếp

## Mục tiêu

Chuyển hoàn toàn (cutover) persistent memory runtime từ legacy `RIKUGAN.md` (markdown file cạnh IDB) sang central memory subsystem (`BinaryMemoryService` — SQLite structured facts + `MEMORY.md` managed region). Xóa toàn bộ legacy read/write code runtime, xóa importer `legacy.py`, xóa 3 dark-scaffolding config flags. Không còn dual-path, không còn fallback.

**Scope:** Final cleanup của cutover đã được dark-scaffold. Hệ thống central memory (Tasks 1-6 trong `2026-07-14-central-memory-cutover.md`) đã hoàn thiện — phần còn lại chỉ là flip default + dọn legacy.

## Bối cảnh

Luc Nhan có 2 lớp memory:
- **Legacy (xóa):** `RIKUGAN.md` file cạnh IDB, ghi bởi `save_memory` tool, đọc bởi `_load_persistent_memory()`, append bởi `append_to_memory_file()`.
- **Central (giữ, làm default):** `lucnhan/memory/` package — `BinaryMemoryService` façade, SQLite repository, `MemoryProjector` ghi managed region vào `MEMORY.md`, `MemoryWriteAuthority` (non-serializable, main-agent-only).

Codebase đã có dispatch song song (central vs legacy) tại 3 điểm:
- `_handle_save_memory_tool` (`loop.py:1625`)
- `_handle_memory_command` (`loop_commands.py:106`)
- `build_system_prompt` (`system_prompt.py:128`)

Central path kích hoạt qua `memory_workspaces_enabled` flag, hiện `False`.

## Quyết định thiết kế

1. **Central memory luôn-on.** Xóa flag `memory_workspaces_enabled` — không còn cách disable. `case_memory_enabled` và `peer_retrieval_enabled` cũng xóa (chúng phụ thuộc central memory).
2. **Không giữ importer.** Xóa `lucnhan/memory/legacy.py` hoàn toàn (user chọn clean break — ai đang có `RIKUGAN.md` cũ phải copy thủ công).
3. **Giữ `sanitize_memory()`.** Hàm này vẫn dùng trong central path để wrap `manual_memory_notes` từ `MEMORY.md` unmanaged region (`system_prompt.py:132`). Đổi docstring mention `RIKUGAN.md` → `MEMORY.md`.
4. **Không backward-compat runtime.** Legacy branch bị xóa, không giữ "deprecated but functional".
5. **Identity failure path = silent.** Khi `manager.bind()` trả `ephemeral` (không resolve được workspace — filesystem không hỗ trợ inode, read-only, ...), `_wire_central_memory()` return sớm (dòng 530-531), `loop.memory_service` vẫn `None`. Behavior: memory đơn giản không có — không warning, không log cho user (giữ behavior hiện tại). `save_memory`/`/memory` báo "not available" qua message error đã define ở Phase 2. Đây là edge case hiếm, không cần surfacing.
6. **Legacy data không migrate.** CHANGELOG chỉ ghi "legacy RIKUGAN.md data is not migrated — the old file is ignored". Không thêm hướng dẫn copy thủ công. User tự chịu trách nhiệm nếu muốn giữ dữ liệu cũ.

## Thay đổi chi tiết

### Phase 1 — Config layer (flip flags)

**`lucnhan/core/config.py`:**
- Xóa 3 dataclass field: `memory_workspaces_enabled` (dòng 156), `case_memory_enabled` (dòng 160), `peer_retrieval_enabled` (dòng 165) và comment block dòng 149-165.
- Xóa 3 entry khỏi load-key tuple (dòng 362-364).
- Xóa 3 entry khỏi `_BOOLEAN_FIELDS` set (dòng 402-404).
- Trong `load()`: các key cũ nếu xuất hiện trong config file user sẽ tự bị bỏ qua (không trong load-key list → không `setattr`). Không cần cleanup riêng — behavior tự nhiên.

**Hệ quả (không cần sửa — đã always-on):**
- `MemoryWorkspaceManager.__init__` luôn `self._registry.initialize()` (guard `if config.memory_workspaces_enabled` dòng 52 → xóa guard, gọi `initialize()` unconditionally).
- `MemoryWorkspaceManager.bind()` xóa dark-mode branch (dòng 65-74) — luôn đi path `self._resolver.resolve(...)`.
- `MemoryWorkspaceManager.set_active_case()`: xóa **chỉ guard flag** (dòng 112-113). **GIỮ** guard binding-state (dòng 114-115 `if self._binding is None or state not in {active, provisional} → raise PersistenceDisabled`) — vẫn valid cho identity-failure path.
- `require_persistent_paths()` (dòng 141-148): **GIỮ nguyên** — guard check binding-state, không liên quan flag. Vẫn raise `PersistenceDisabled` khi chưa bind.
- `session_controller_base.py:491` xóa `if getattr(self.config, "memory_workspaces_enabled", False)` → luôn gọi `_wire_central_memory()`.

### Phase 2 — Runtime legacy code cleanup

**`lucnhan/agent/system_prompt.py`:**
- Xóa: `_MAX_MEMORY_LINES` (dòng 21), `_MEMORY_CACHE` (dòng 40), `_MEMORY_MISSING_SENTINEL` (dòng 41), toàn bộ `_load_persistent_memory()` (dòng 44-96), comment block dòng 20-39.
- `build_system_prompt()`: xóa param `idb_dir` (dòng 115). Xóa legacy branch `else` (dòng 133-137). Memory chỉ đến từ `structured_memory` / `manual_memory_notes`. Refactor: nếu cả hai rỗng → không thêm section memory nào.
- Xóa import `os` (dòng 5) nếu không còn dùng. Giữ import `sanitize_memory` (dòng 10).
- **2 caller cần cập nhật:**
  - `loop.py:543` `_build_system_prompt()` — xóa `idb_dir=idb_dir` arg (dòng 552), dọn biến `idb_dir` dòng 516-518 nếu không dùng mục đích khác.
  - `agent/orchestra/main_agent.py:145` — xóa `idb_dir=idb_dir` arg (dòng 152), dọn biến dòng 141-143.

**`lucnhan/agent/loop.py`:**
- Xóa `_MEMORY_HEADER` (dòng 84) + `append_to_memory_file()` (dòng 251-...).
- `_handle_save_memory_tool()` (dòng 1601): xóa legacy `else` branch (dòng 1643-1668) bao gồm `make_store`/`ingest_save_memory` auto-ingest import. Logic mới: nếu `memory_service is None or _memory_authority is None` → trả error "Central memory is not available in this context." (không ghi file).
- Cập nhật docstring/comment mention `RIKUGAN.md` (dòng 269, 285, 1609, 1617) → `MEMORY.md` hoặc "MEMORY.md managed region".
- `loop.memory_service` / `_memory_authority` / `_memory_manager` giữ nguyên (vẫn injection point từ controller).

**`lucnhan/agent/modes/plan.py`:**
- `persist_plan()` (dòng 109): thay body. Nếu `loop.memory_service is not None and loop._memory_authority is not None` → `loop.memory_service.save_plan(loop._memory_authority, goal=user_goal, steps=steps)`, log success. Không thì no-op + `log_debug`. Xóa import `append_to_memory_file`, xóa `os.path.join(idb_dir, "RIKUGAN.md")`, xóa `time.strftime` formatting (service tự format).
- **Caller `exploration.py:249`** giữ signature `persist_plan(loop, user_goal, steps)` — không thay đổi.

**`lucnhan/agent/loop_commands.py`:**
- `_handle_memory_command()` (dòng 98): xóa legacy branch (dòng 124-147). Nếu `memory_service is None` → `TurnEvent.text_done("Central memory is not available...")` + return.

**`lucnhan/agent/loop.py` `_handle_case_command` (dòng 459-461):**
- Message lỗi `"Central memory is not enabled. Set memory_workspaces_enabled=true in config."` — flag không còn, sửa thành `"Central memory is not available for this binary."` (không reference flag). Context: message này chạy khi `memory_service is None`, tức identity resolve thất bại.

**`lucnhan/agent/modes/research.py`:**
- Docstring/prompt mention `RIKUGAN.md` (dòng 146, 162) → `MEMORY.md`.

### Phase 3 — Module removal + docstring cleanup

- **Xóa file `lucnhan/memory/legacy.py`** hoàn toàn.
- `lucnhan/memory/__init__.py`: docstring dòng 7-17 mention `config.memory_workspaces_enabled` + dark mode → cập nhật/rút gọn (xóa đoạn deprecation). Note: module này export `KnowledgeRawStore` cho knowledge subsystem (khác central memory) — **không** xóa, chỉ sửa docstring.
- `lucnhan/memory/paths.py`: docstring dòng 11 mention `RIKUGAN.md` → `MEMORY.md` hoặc bỏ câu. Giữ entity-ID helpers + `knowledge_paths()`.
- `lucnhan/memory/manager.py`: docstring mention dark mode (dòng 7-9) → cập nhật. Xóa guards như Phase 1.
- `lucnhan/core/sanitize.py`: docstring dòng 5 "(skills, RIKUGAN.md)" → "(skills, MEMORY.md)".

### Phase 4 — Tests

**Xóa file hoàn toàn (test feature flag / legacy — vô nghĩa sau cutover):**
- `tests/memory/test_legacy.py` (importer đã xóa)
- `tests/memory/test_activation_gate.py` (toàn bộ 5 test đều test `memory_workspaces_enabled` flag: default-disabled, round-trip, invalid-type, manager-init, full-flow — flag không còn)
- `tests/memory/test_foundation_gate.py` — **sửa có chọn lọc, không xóa file**. File có 3 class: (a) `TestDarkModeGate` (3 test, dòng 19-38) — tất cả test flag/legacy → **xóa cả class**; (b) `TestStableTypes` (6 test, dòng 41-102) — export checks runtime, **giữ nguyên**; (c) `TestEndToEndDarkFlow` — xóa `test_disabled_bind_returns_ephemeral_and_no_paths` (dòng 108-130, test dark-mode), giữ `test_enabled_full_flow` (dòng 132-164, bỏ dòng 139 `config.memory_workspaces_enabled = True`).

**Sửa (xóa flag references, không xóa file):**
- `tests/memory/test_config.py`: dòng 17 `assert ... is False`, dòng 23-25 test typed-load của flag → xóa các assertion liên quan flag (giữ phần test config khác nếu có).
- `tests/memory/test_manager.py`:
  - **Xóa cả class `TestDarkBinding`** (dòng 34-61, 3 test): `test_disabled_config_returns_ephemeral_binding` (sau cutover bind luôn resolve → không còn EPHEMERAL), `test_disabled_config_does_not_create_registry` (manager luôn init registry), `test_require_persistent_paths_fails_in_disabled_mode` (tên sai ngữ nghĩa — guard binding-state vẫn raise khi chưa bind, nhưng "disabled mode" không còn).
  - Xóa **7 sites** set `config.memory_workspaces_enabled = True` (dòng 68, 85, 98, 115, 133, 145, 165) trong `TestEnabledBinding`+ — manager luôn-on.
  - Docstring module dòng 1 "dark binding, generation, disabled mode" → cập nhật bỏ "dark/disabled".
- `tests/memory/test_first_open_regression.py`: 3 sites set flag (dòng 28, 69, 100) → xóa.
- `tests/memory/test_case_binding.py`: xóa set flag tại dòng 20 (`= True`). **Xóa hẳn test** `test_disabled_config_rejects_case_operations` (dòng 91-97) — nó verify dark-mode raise `PersistenceDisabled`, sau cutover `set_active_case` không còn guard đó nên test vô nghĩa. Giữ các test case-binding khác.
- `tests/memory/test_case_e2e.py` (dòng 32), `test_case_commands.py` (dòng 53): xóa set flag.
- `tests/agent/test_memory_cutover.py`: xóa `test_legacy_path_still_works_without_service` (dòng 74-88) + dòng 38 set flag.
- `tests/agent/test_prompt_cutover.py` (dòng 37): xóa set flag, update cho `build_system_prompt` không còn param `idb_dir`.

**Kiểm tra + sửa nếu cần:**
- `tests/agent/test_memory_write_ownership.py`, `tests/agent/test_system_prompt.py` — remove RIKUGAN.md references, update `build_system_prompt` signature.

**Giữ nguyên:**
- `tests/core/test_sanitize.py`, `lucnhan/tests/test_session_restore_sanitization.py` (sanitize_memory vẫn dùng)
- Các test central memory service/repo/workspace/case (chỉ bỏ set-flag, không đổi logic test)

### Phase 5 — Docs

- `CLAUDE.md`: dòng 209 bảng sanitize (`RIKUGAN.md` → `MEMORY.md`), mục "Persistent memory" mention `RIKUGAN.md` (tìm + replace).
- `AGENTS.md`: dòng 579 + grep toàn bộ `RIKUGAN.md` mention.
- `ARCHITECTURE.md`, `README.md`, `llms.txt`, `webpage/llms.txt`, `webpage/index.html`, `webpage/docs.html`, `webpage/ARCHITECTURE.html`: cập nhật mention.
- `CHANGELOG.md`: thêm entry `feat(memory): cut over to central MEMORY.md, remove legacy RIKUGAN.md` + note rõ "legacy `RIKUGAN.md` data is not migrated — the old file is ignored" (không hướng dẫn copy thủ công).
- **Không sửa** `docs/superpowers/plans/2026-07-14-central-memory-cutover.md` và spec liên quan — đây là historical record của việc build hệ thống mới.

## Rủi ro và mitigations

| Rủi ro | Mitigation |
|--------|------------|
| User đang có config file chứa 3 flags cũ → load fail | Flags không trong load-key list → tự bỏ qua. Test `test_config.py` verify. |
| User đang có `RIKUGAN.md` cũ → mất dữ liệu | Clean break (user chọn b). Note trong CHANGELOG rằng dữ liệu legacy không auto-migrate. |
| `memory_service` None trong headless/a2a/identity-failure context → `save_memory` báo error thay vì ghi | Behavior đúng — central memory cần controller wire service + identity resolve thành công. Identity failure silent (quyết định #5). Headless phải wire hoặc disable. Verify headless path. |
| `idb_dir` param xóa khỏi `build_system_prompt` → caller khác miss | Chỉ 2 caller (đã map: `loop.py` + `orchestra/main_agent.py`). Grep verify sau khi sửa. |
| Test `test_legacy.py` / `test_activation_gate.py` xóa nhưng có import vào conftest | Kiểm tra `tests/memory/__init__.py`, `tests/conftest.py`, `tests/memory/conftest.py` trước khi xóa. |
| ~20+ test sites set `memory_workspaces_enabled` → NameError sau xóa field | Phase 4 đã map đầy đủ (test_manager 7x, test_first_open 3x, test_case_binding 4x, test_case_e2e/commands, test_config, test_cutover, test_prompt_cutover). Grep `"memory_workspaces_enabled\|case_memory_enabled\|peer_retrieval_enabled" -- tests/` = empty sau khi sửa. |

## Out of scope

- Build mới central memory subsystem (đã xong — Tasks 1-6 cutover plan).
- Knowledge subsystem (`notes/`, `.lucnhan-kb/`, `KnowledgeRawStore`) — đây là JSONL knowledge store riêng, **không** phải persistent memory. Giữ nguyên.
- Headless memory wiring chi tiết (verify-only, không build mới).
- Version bump (task riêng theo release flow).

## Thứ tự thực thi đề xuất

1. Phase 1 (config) — thấp rủi ro, enable feature
2. Phase 2 (runtime code) — core thay đổi
3. Phase 3 (module removal) — sau khi Phase 2 xóa hết caller
4. Phase 4 (tests) — song song với Phase 2-3 (TDD-style khi sửa logic)
5. Phase 5 (docs) — cuối cùng
6. Verify: `./ci-local.sh` + grep "RIKUGAN.md" trong `lucnhan/` không còn runtime reference
