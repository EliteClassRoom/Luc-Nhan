# Remove RIKUGAN.md Legacy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cắt đứt hoàn toàn runtime persistent memory từ legacy `RIKUGAN.md` sang central `BinaryMemoryService` (SQLite + `MEMORY.md` managed region), xóa 3 dark-scaffolding config flags, xóa module `legacy.py`, dọn sạch toàn bộ test/docs reference.

**Architecture:** Central memory subsystem đã hoàn thiện (dark scaffolding). Plan này là cutover cuối: (1) xóa flags để manager luôn-on, (2) dọn legacy read/write code runtime, (3) xóa importer, (4) dọn tests, (5) dọn docs. Mỗi task kết thúc bằng `./ci-local.sh` pass hoặc targeted tests pass.

**Tech Stack:** Python 3.11–3.12, SQLite WAL, portalocker, PySide6, pytest, ruff, mypy. Không cần IDA Pro để chạy test (stubs).

## Global Constraints

- Spec authority: `docs/superpowers/specs/2026-07-16-remove-lucnhan-md-legacy-design.md`.
- Không runtime read/write `RIKUGAN.md` sau khi plan xong.
- Không dual-path, không fallback. Guard binding-state trong `manager.py` (`set_active_case`, `require_persistent_paths`) GIỮ — chỉ xóa guard flag.
- `sanitize_memory()` GIỮ (vẫn wrap manual MEMORY.md notes).
- Knowledge subsystem (`notes/`, `.lucnhan-kb/`, `KnowledgeRawStore`) KHÔNG đụng — JSONL store riêng.
- Identity-failure path silent: bind ephemeral → memory_service None → không warning.
- Commit theo Conventional Commits: `feat(memory):`, `refactor(memory):`, `test(memory):`, `docs(memory):`.
- Host API imports dùng `importlib.import_module()` trong try/except (không liên quan plan này, nhưng giữ convention).
- Stage commit theo filename cụ thể, không `git add -A`.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `lucnhan/core/config.py` | Modify | Xóa 3 flag fields + load entries |
| `lucnhan/memory/manager.py` | Modify | Xóa dark-mode branches/guards flag, giữ binding-state guards |
| `lucnhan/ui/session_controller_base.py` | Modify | Luôn wire central memory |
| `lucnhan/agent/system_prompt.py` | Modify | Xóa `_load_persistent_memory` + param `idb_dir` |
| `lucnhan/agent/loop.py` | Modify | Xóa `append_to_memory_file`, `_MEMORY_HEADER`, legacy save_memory branch, case message |
| `lucnhan/agent/modes/plan.py` | Modify | `persist_plan` dùng service.save_plan |
| `lucnhan/agent/loop_commands.py` | Modify | `_handle_memory_command` xóa legacy branch |
| `lucnhan/agent/modes/research.py` | Modify | Docstring mention RIKUGAN.md → MEMORY.md |
| `lucnhan/agent/orchestra/main_agent.py` | Modify | Xóa `idb_dir` param |
| `lucnhan/memory/legacy.py` | Delete | Importer không còn |
| `lucnhan/memory/__init__.py` | Modify | Docstring dọn dark-mode mention |
| `lucnhan/memory/paths.py` | Modify | Docstring mention |
| `lucnhan/core/sanitize.py` | Modify | Docstring mention |
| `tests/memory/test_legacy.py` | Delete | Test importer đã xóa |
| `tests/memory/test_activation_gate.py` | Delete | Toàn test flag |
| `tests/memory/test_manager.py` | Modify | Xóa class TestDarkBinding + flag sets |
| `tests/memory/test_foundation_gate.py` | Modify | Xóa class TestDarkModeGate + 1 test |
| `tests/memory/test_case_binding.py` | Modify | Xóa 1 test dark-mode + flag set |
| `tests/memory/test_config.py` | Modify | Xóa flag assertions |
| `tests/memory/test_first_open_regression.py` | Modify | Xóa flag sets |
| `tests/memory/test_case_e2e.py` | Modify | Xóa flag set |
| `tests/memory/test_case_commands.py` | Modify | Xóa flag set |
| `tests/agent/test_memory_cutover.py` | Modify | Xóa legacy test + flag set |
| `tests/agent/test_prompt_cutover.py` | Modify | Xóa legacy fallback test + flag set |
| `tests/agent/test_memory_write_ownership.py` | Modify | Xóa RIKUGAN.md refs (nếu có) |
| `tests/agent/test_system_prompt.py` | Modify | Update build_system_prompt signature |
| `CLAUDE.md`, `AGENTS.md`, `ARCHITECTURE.md`, `README.md`, `llms.txt`, `webpage/*` | Modify | Mention RIKUGAN.md → MEMORY.md |
| `CHANGELOG.md` | Modify | Thêm cutover entry |

---

### Task 1: Xóa dark-scaffolding config flags

**Files:**
- Modify: `lucnhan/core/config.py:149-165, 355-365, 394-405`
- Test: `tests/memory/test_config.py`

**Interfaces:**
- Consumes: `LucNhanConfig` dataclass, `_apply_loaded_config()` typed-load
- Produces: `LucNhanConfig` không còn `memory_workspaces_enabled` / `case_memory_enabled` / `peer_retrieval_enabled` fields

- [ ] **Step 1: Sửa test_config.py — xóa flag assertions**

Mở `tests/memory/test_config.py`. Đọc toàn bộ file, xóa mọi dòng reference 3 flags. Cụ thể xóa các assertion dạng `assert config.memory_workspaces_enabled is False`, block test typed-load `config._apply_loaded_config({"memory_workspaces_enabled": "true"})`. Giữ lại các test config khác (provider, knowledge_enabled, v.v.).

Run: `python -m pytest tests/memory/test_config.py -v`
Expected: FAIL (fields còn tồn tại, chưa xóa — nhưng nếu test đã xóa reference thì PASS vì không chạm field). Nếu test PASS ngay → OK, đi Step 2. Nếu test còn assert field tồn tại → sửa tiếp.

- [ ] **Step 2: Xóa 3 dataclass fields trong config.py**

Sửa `lucnhan/core/config.py`. Xóa block dòng 149-165 (comment header "Central memory workspaces" + 3 field definitions). Cụ thể xóa:

```python
    # ------------------------------------------------------------------
    # Central memory workspaces (see lucnhan.memory.*)
    # ------------------------------------------------------------------
    # Dark-scaffolding switch: False until the atomic cutover plan
    # activates it.  When False, no central workspace directories are
    # created and all runtime memory continues to use the legacy
    # folder-scoped RIKUGAN.md / .lucnhan-kb layout.
    memory_workspaces_enabled: bool = False

    # Analysis case subsystem: when True and central memory is enabled,
    # case context (cross-binary facts) is included in the prompt.
    case_memory_enabled: bool = False

    # Controlled peer retrieval: when True and a case is active, facts
    # from related peer binaries (relation confidence >= 0.7) are included
    # in the prompt. Explicit /case search always works regardless.
    peer_retrieval_enabled: bool = False
```

- [ ] **Step 3: Xóa 3 entries khỏi load-key tuple**

Trong `_apply_loaded_config()`, xóa 3 dòng trong tuple load-key list:

```python
            "memory_workspaces_enabled",
            "case_memory_enabled",
            "peer_retrieval_enabled",
```

- [ ] **Step 4: Xóa 3 entries khỏi _BOOLEAN_FIELDS set**

Trong cùng `_apply_loaded_config()`, xóa 3 dòng trong `_BOOLEAN_FIELDS` set:

```python
                    "memory_workspaces_enabled",
                    "case_memory_enabled",
                    "peer_retrieval_enabled",
```

- [ ] **Step 5: Run config tests**

Run: `python -m pytest tests/memory/test_config.py tests/core/test_profile.py -v`
Expected: PASS. Grep verify không còn reference flag trong config.py:

```bash
git grep -n "memory_workspaces_enabled\|case_memory_enabled\|peer_retrieval_enabled" -- lucnhan/core/config.py
```
Expected: no matches.

- [ ] **Step 6: Commit**

```bash
git add lucnhan/core/config.py tests/memory/test_config.py
git commit -m "refactor(memory): remove dark-scaffolding config flags"
```

---

### Task 2: Xóa dark-mode branches trong MemoryWorkspaceManager

**Files:**
- Modify: `lucnhan/memory/manager.py:1-10, 52-53, 55-81, 106-115`
- Test: `tests/memory/test_manager.py`, `tests/memory/test_foundation_gate.py`

**Interfaces:**
- Consumes: `LucNhanConfig` (không còn `memory_workspaces_enabled`)
- Produces: `MemoryWorkspaceManager` luôn init registry + luôn resolve bind. Guard binding-state (`set_active_case` line 114-115, `require_persistent_paths` line 147-148) GIỮ nguyên.

- [ ] **Step 1: Sửa test_manager.py — xóa class TestDarkBinding + flag sets**

Mở `tests/memory/test_manager.py`:
1. Xóa toàn bộ class `TestDarkBinding` (dòng 34-61, 3 test).
2. Xóa tất cả dòng `config.memory_workspaces_enabled = True` (7 sites: dòng 68, 85, 98, 115, 133, 145, 165).
3. Sửa docstring module dòng 1 từ `"Tests for MemoryWorkspaceManager: dark binding, generation, disabled mode."` thành `"Tests for MemoryWorkspaceManager: binding, generation, persistence."`.
4. Kiểm tra import `PersistenceDisabled` — vẫn cần (test `require_persistent_paths` chưa bind). Giữ import dòng 13.

Run: `python -m pytest tests/memory/test_manager.py -v`
Expected: FAIL (manager vẫn còn dark-mode branch, bind giờ luôn resolve nhưng config không set flag → manager init không initialize registry → resolve fail).

- [ ] **Step 2: Xóa guard flag trong __init__**

Sửa `lucnhan/memory/manager.py` dòng 52-53. Từ:

```python
        if config.memory_workspaces_enabled:
            self._registry.initialize()
```

Thành:

```python
        self._registry.initialize()
```

- [ ] **Step 3: Xóa dark-mode branch trong bind()**

Sửa `manager.py` dòng 55-81. Từ:

```python
    def bind(
        self,
        request: IdentityRequest,
        choice: object | None = None,
    ) -> IdentityResolution:
        """Bind identity evidence to a workspace and return the resolution.

        In dark mode (feature disabled), returns a disabled binding without
        touching the registry.
        """
        if not self._config.memory_workspaces_enabled:
            self._binding = WorkspaceBinding(
                memory_id="",
                state="disabled",
                display_name=request.display_name,
            )
            return IdentityResolution(
                status=ResolutionStatus.EPHEMERAL,
                binding=self._binding,
            )

        resolution = self._resolver.resolve(request, choice)
```

Thành:

```python
    def bind(
        self,
        request: IdentityRequest,
        choice: object | None = None,
    ) -> IdentityResolution:
        """Bind identity evidence to a workspace and return the resolution."""
        resolution = self._resolver.resolve(request, choice)
```

- [ ] **Step 4: Xóa guard flag trong set_active_case()**

Sửa `manager.py` dòng 106-115. Xóa dòng 112-113:

```python
        if not self._config.memory_workspaces_enabled:
            raise PersistenceDisabled("central memory persistence is unavailable")
```

GIỮ nguyên dòng 114-115 (guard binding-state):

```python
        if self._binding is None or self._binding.state not in {"active", "provisional"}:
            raise PersistenceDisabled("no active binary binding")
```

- [ ] **Step 5: Cập nhật module docstring**

Sửa `manager.py` dòng 1-10. Xóa/condense đoạn mention dark mode:

```python
"""MemoryWorkspaceManager: facade for the central memory subsystem.

This manager wraps the registry, identity resolver, and locator into a single
controller-owned object. It binds identity evidence to a workspace, tracks
process-local generations, and produces frozen run contexts.
"""
```

- [ ] **Step 6: Run manager tests**

Run: `python -m pytest tests/memory/test_manager.py tests/memory/test_first_open_regression.py tests/memory/test_case_binding.py tests/memory/test_case_e2e.py tests/memory/test_case_commands.py -v`
Expected: các test_flag đã xóa → PASS. Nếu test_first_open/test_case_* còn set flag (chưa sửa trong task này) → chúng FAIL với AttributeError. Đi Task 3 sửa tiếp (những file đó). Nhưng test_manager phải PASS.

- [ ] **Step 7: Commit**

```bash
git add lucnhan/memory/manager.py tests/memory/test_manager.py
git commit -m "refactor(memory): remove dark-mode branches from MemoryWorkspaceManager"
```

---

### Task 3: Dọn flag sets trong các test memory còn lại

**Files:**
- Modify: `tests/memory/test_first_open_regression.py:28,69,100`
- Modify: `tests/memory/test_case_binding.py:20, 91-97`
- Modify: `tests/memory/test_case_e2e.py:32`
- Modify: `tests/memory/test_case_commands.py:53`
- Modify: `tests/memory/test_foundation_gate.py:19-38, 105-131`

**Interfaces:**
- Consumes: MemoryWorkspaceManager (Task 2)
- Produces: test suite memory không còn flag references

- [ ] **Step 1: Sửa test_first_open_regression.py**

Xóa 3 dòng `config.memory_workspaces_enabled = True` (dòng 28, 69, 100). Các test đều valid (test first-open DB creation), không cần thay đổi logic khác.

- [ ] **Step 2: Sửa test_case_binding.py**

1. Xóa dòng 20 `config.memory_workspaces_enabled = True` (trong `_bind_workspace` helper).
2. Xóa toàn bộ test `test_disabled_config_rejects_case_operations` (dòng 91-97) — test verify dark-mode raise PersistenceDisabled, vô nghĩa sau cutover.
3. Kiểm tra import `PersistenceDisabled` dòng 11 — nếu không còn test nào dùng (sau khi xóa test dòng 91) → xóa khỏi import. Grep `PersistenceDisabled` trong file: nếu 0 match ngoài import → xóa import.

- [ ] **Step 3: Sửa test_case_e2e.py + test_case_commands.py**

Xóa `config.memory_workspaces_enabled = True` tại dòng 32 (e2e) và dòng 53 (commands).

- [ ] **Step 4: Sửa test_foundation_gate.py**

1. Xóa toàn bộ class `TestDarkModeGate` (dòng 19-38, 3 test).
2. Trong `TestEndToEndDarkFlow`: xóa test `test_disabled_bind_returns_ephemeral_and_no_paths` (dòng 108-130). GIỮ `test_enabled_full_flow` (dòng 132-164) nhưng xóa dòng 139 `config.memory_workspaces_enabled = True`.
3. Giữ nguyên `TestStableTypes` (dòng 41-102) — export checks vẫn valid. Đặc biệt dòng 101 check export `PersistenceDisabled` — GIỮ (class vẫn tồn tại).
4. Sửa docstring module dòng 1 từ `"Foundation integration gate: dark mode, no cutover, stable types."` thành `"Foundation integration gate: stable types and enabled flow."`.
5. Xóa import `pytest` dòng 8 NẾU không còn dùng (sau khi xóa test dùng `pytest.raises`). Grep `pytest` trong file: nếu 0 match → xóa import.

- [ ] **Step 5: Run toàn bộ memory tests**

Run: `python -m pytest tests/memory/ -v`
Expected: PASS (trừ `test_activation_gate.py` và `test_legacy.py` — 2 file này xóa ở Task 6, có thể import error nếu còn). Nếu test_activation_gate/test_legacy gây collection error → tạm skip: `python -m pytest tests/memory/ -v --ignore=tests/memory/test_activation_gate.py --ignore=tests/memory/test_legacy.py`.

- [ ] **Step 6: Commit**

```bash
git add tests/memory/test_first_open_regression.py tests/memory/test_case_binding.py tests/memory/test_case_e2e.py tests/memory/test_case_commands.py tests/memory/test_foundation_gate.py
git commit -m "test(memory): remove dark-mode tests and flag references"
```

---

### Task 4: Controller luôn wire central memory

**Files:**
- Modify: `lucnhan/ui/session_controller_base.py:488-499`

**Interfaces:**
- Consumes: `LucNhanConfig` (không còn flag), `MemoryWorkspaceManager`
- Produces: `_wire_central_memory(loop)` luôn được gọi cho mỗi agent run

- [ ] **Step 1: Xóa guard flag trong start_agent**

Sửa `lucnhan/ui/session_controller_base.py` dòng 488-499. Từ:

```python
        # Inject central memory service when enabled.
        if getattr(self.config, "memory_workspaces_enabled", False):
            log_info("Central memory enabled — wiring service into loop")
            self._wire_central_memory(loop)
        else:
            log_debug("Central memory disabled — using legacy RIKUGAN.md path")
```

Thành:

```python
        # Inject central memory service for every agent run.
        self._wire_central_memory(loop)
```

- [ ] **Step 2: Cập nhật docstring _wire_central_memory**

Sửa dòng 501-506 docstring. Từ:

```python
    def _wire_central_memory(self, loop: AgentLoop) -> None:
        """Construct and inject BinaryMemoryService into the loop.

        Called only when ``config.memory_workspaces_enabled`` is True.
        In dark mode (default), the loop keeps its legacy memory path.
        """
```

Thành:

```python
    def _wire_central_memory(self, loop: AgentLoop) -> None:
        """Construct and inject BinaryMemoryService into the loop.

        Called for every agent run. If identity resolution fails (bind
        returns ephemeral), this method returns early without injecting
        a service, so the loop runs without central memory.
        """
```

- [ ] **Step 3: Run controller tests**

Run: `python -m pytest tests/agent/test_session_controller.py tests/ui/ -v -k "memory or controller or session" 2>&1 | head -50`
Expected: PASS hoặc no tests collected (UI tests cần Qt stubs). Verify syntax:

```bash
python -c "import ast; ast.parse(open('lucnhan/ui/session_controller_base.py').read())"
```
Expected: no output (valid syntax).

- [ ] **Step 4: Commit**

```bash
git add lucnhan/ui/session_controller_base.py
git commit -m "refactor(memory): always wire central memory in controller"
```

---

### Task 5: Xóa legacy system_prompt loading

**Files:**
- Modify: `lucnhan/agent/system_prompt.py:1-145`
- Test: `tests/agent/test_prompt_cutover.py`, `tests/agent/test_system_prompt.py`

**Interfaces:**
- Consumes: `structured_memory`, `manual_memory_notes` params (từ BinaryMemoryService)
- Produces: `build_system_prompt()` KHÔNG còn param `idb_dir`. KHÔNG còn `_load_persistent_memory`, `_MEMORY_CACHE`, `_MAX_MEMORY_LINES`, `_MEMORY_MISSING_SENTINEL`.

- [ ] **Step 1: Sửa test_prompt_cutover.py**

1. Xóa dòng 37 `config.memory_workspaces_enabled = True`.
2. Xóa test `test_build_system_prompt_falls_back_to_legacy` (dòng 128-135) — gọi `build_system_prompt(idb_dir=...)`, param bị xóa. Thay bằng test mới verify no-memory case:

```python
    def test_build_system_prompt_no_memory_section_when_empty(self, tmp_path: Path) -> None:
        """Without structured_memory or manual_memory_notes, no memory section appears."""
        from lucnhan.agent.system_prompt import build_system_prompt

        prompt = build_system_prompt()
        # No memory content, but base prompt is present
        assert "## Persistent Memory (RIKUGAN.md)" not in prompt
        assert "## Structured Memory" not in prompt
```

- [ ] **Step 2: Sửa test_system_prompt.py — xóa idb_dir references**

Run: `git grep -n "idb_dir\|_load_persistent_memory\|_MEMORY_CACHE" -- tests/agent/test_system_prompt.py`
Xóa mọi reference. Nếu test gọi `build_system_prompt(idb_dir=...)` → xóa arg đó.

- [ ] **Step 3: Xóa legacy loading code trong system_prompt.py**

Sửa `lucnhan/agent/system_prompt.py`. Xóa:
1. Dòng 5 `import os` (nếu `_load_persistent_memory` là user duy nhất — verify bằng `git grep -n "^import os\|os\." -- lucnhan/agent/system_prompt.py`; nếu 0 match ngoài import → xóa).
2. Dòng 20-96: toàn bộ `_MAX_MEMORY_LINES`, comment block cache, `_MEMORY_CACHE`, `_MEMORY_MISSING_SENTINEL`, `_load_persistent_memory()`.
3. Giữ import `sanitize_memory` dòng 10.

- [ ] **Step 4: Refactor build_system_prompt — xóa param idb_dir + legacy branch**

Sửa `build_system_prompt()` (dòng 106-137). Xóa param `idb_dir: str | None = None` (dòng 115). Thay memory section logic (dòng 125-137) từ:

```python
    # Central memory path: when structured_memory or manual_memory_notes
    # are supplied by BinaryMemoryService, use them instead of legacy
    # RIKUGAN.md. Otherwise fall back to the legacy path.
    if structured_memory or manual_memory_notes:
        if structured_memory:
            parts.append(f"\n{structured_memory}")
        if manual_memory_notes:
            parts.append(f"\n## Manual Notes\n{sanitize_memory(manual_memory_notes)}")
    else:
        # Legacy persistent memory from RIKUGAN.md.
        memory = _load_persistent_memory(idb_dir or "")
        if memory:
            parts.append(f"\n## Persistent Memory (RIKUGAN.md)\n{sanitize_memory(memory)}")
```

Thành:

```python
    # Central memory: structured facts from SQLite + manual notes from
    # MEMORY.md unmanaged region. Both supplied by BinaryMemoryService.
    if structured_memory:
        parts.append(f"\n{structured_memory}")
    if manual_memory_notes:
        parts.append(f"\n## Manual Notes\n{sanitize_memory(manual_memory_notes)}")
```

- [ ] **Step 4b: Dọn caller loop.py _build_system_prompt**

Sửa `lucnhan/agent/loop.py` `_build_system_prompt()` (dòng 494-560):
1. Xóa block derive idb_dir (dòng 515-518):

```python
        # Derive IDB directory for persistent memory loading (legacy path).
        idb_dir = ""
        if self.session.idb_path:
            idb_dir = os.path.dirname(self.session.idb_path)

```

2. Xóa comment block dòng 520-523 mention legacy path.
3. Trong `build_system_prompt(...)` call (dòng 543-560), xóa arg `idb_dir=idb_dir,` (dòng 552).
4. Cập nhật comment dòng 520-523 nếu còn — central memory comment đã có ở structured_memory/manual_memory_notes logic.

**Lưu ý:** Caller thứ 2 (`orchestra/main_agent.py`) dọn ở Task 8. Nhưng vì param đã xóa ở Task 5, nếu orchestra chưa dọn thì import test ở Step 5 sẽ fail. Do đó **Task 8 phải chạy ngay sau Task 5** trước khi commit Task 5, HOẶC gộp dọn orchestra vào Task 5. Khuyến nghị: dọn cả orchestra trong Task 5 Step 4c để 1 commit tự đứng.

- [ ] **Step 4c: Dọn caller orchestra/main_agent.py (để Task 5 tự đứng)**

Sửa `lucnhan/agent/orchestra/main_agent.py` dòng 141-154 (chi tiết ở Task 8 Step 1-2 — copy code đó vào đây). Xóa block idb_dir + xóa arg `idb_dir=idb_dir,` trong build_system_prompt call.

- [ ] **Step 5: Run prompt tests**

Run: `python -m pytest tests/agent/test_prompt_cutover.py tests/agent/test_system_prompt.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add lucnhan/agent/system_prompt.py lucnhan/agent/loop.py lucnhan/agent/orchestra/main_agent.py tests/agent/test_prompt_cutover.py tests/agent/test_system_prompt.py
git commit -m "refactor(memory): remove legacy RIKUGAN.md loading from system prompt"
```

> **Note:** Vì Task 5 Step 4c đã dọn orchestra, Task 8 chỉ còn dọn research.py docstring. Task 8 Step 1-2 (orchestra) trở thành no-op — skip hoặc gộp.

---

### Task 6: Xóa legacy save_memory + memory command + case message

**Files:**
- Modify: `lucnhan/agent/loop.py:82-88, 251-257, 459-461, 1601-1671`
- Modify: `lucnhan/agent/loop_commands.py:98-147`
- Test: `tests/agent/test_memory_cutover.py`, `tests/agent/test_memory_write_ownership.py`

**Interfaces:**
- Consumes: `BinaryMemoryService.save_fact()`, `MemoryWriteAuthority`
- Produces: `_handle_save_memory_tool()` chỉ có central path. `append_to_memory_file`, `_MEMORY_HEADER` xóa.

- [ ] **Step 1: Sửa test_memory_cutover.py**

1. Xóa dòng 38 `config.memory_workspaces_enabled = True`.
2. Xóa toàn bộ test `test_legacy_path_still_works_without_service` (dòng 74-88) — test ghi RIKUGAN.md, legacy path không còn. Thay bằng test mới verify no-service error:

```python
    def test_save_memory_without_service_returns_error(self, tmp_path: Path) -> None:
        """When memory_service is None (identity failure), save_memory reports unavailable."""
        config = LucNhanConfig()
        session = SessionState(idb_path=str(tmp_path / "test.i64"))
        provider = MagicMock()
        tools = MagicMock()
        loop = AgentLoop(provider, tools, config, session)
        # loop.memory_service stays None (no wiring)

        tc = ToolCall(id="tc1", name="save_memory", arguments={"category": "test", "fact": "X"})
        events = list(loop._handle_save_memory_tool(tc))

        legacy_path = tmp_path / "RIKUGAN.md"
        assert not legacy_path.exists()  # no legacy file written
        tr = events[-1] if events else None
        assert tr is not None
```

- [ ] **Step 2: Xóa _MEMORY_HEADER + append_to_memory_file trong loop.py**

Sửa `lucnhan/agent/loop.py`. Xóa:
1. Dòng 84-88 `_MEMORY_HEADER = (...)`.
2. Dòng 251-257 hàm `append_to_memory_file()`.

- [ ] **Step 3: Xóa legacy branch trong _handle_save_memory_tool**

Sửa `loop.py` dòng 1601-1671. Thay toàn bộ method body phần dispatch (từ dòng 1622 trở đi). Từ:

```python
        category = _sanitize_save_memory_category(tc.arguments.get("category", "general"))
        if not fact:
            content = "Error: 'fact' is required."
            is_err = True
        elif self.memory_service is not None and self._memory_authority is not None:
            # Central memory path: write through BinaryMemoryService.
            try:
                result = self.memory_service.save_fact(
                    self._memory_authority,
                    category=category,
                    fact=fact,
                    source="save_memory",
                )
                if result.projection_dirty:
                    content = f"Saved to MEMORY.md (projection pending): [{category}] {fact}"
                else:
                    content = f"Saved to MEMORY.md: [{category}] {fact}"
                is_err = False
                log_info(f"save_memory: [{category}] {fact[:80]}")
            except Exception as e:
                content = f"Error saving to central memory: {e}"
                is_err = True
        else:
            idb_dir = os.path.dirname(self.session.idb_path) if self.session.idb_path else ""
            if not idb_dir:
                content = "Error: No IDB path set; cannot determine where to save memory."
                is_err = True
            else:
                md_path = os.path.join(idb_dir, "RIKUGAN.md")
            try:
                append_to_memory_file(md_path, f"- [{category}] {fact}\n")
                content = f"Saved to RIKUGAN.md: [{category}] {fact}"
                is_err = False
                log_info(f"save_memory: [{category}] {fact[:80]}")
                # Auto-ingest into the raw knowledge store so retrieval
                # can surface this fact on future turns. Failures are
                # silent — never undo the RIKUGAN.md write above.
                try:
                    from ..memory.ingest import ingest_save_memory, make_store

                    store, paths = make_store(self.session.idb_path)
                    if store is not None:
                        ingest_save_memory(store, paths, fact=fact, category=category)
                except Exception as e:
                    log_debug(f"knowledge ingest (save_memory) failed: {e}")
            except OSError as e:
                content = f"Error writing RIKUGAN.md: {e}"
                is_err = True
```

Thành:

```python
        category = _sanitize_save_memory_category(tc.arguments.get("category", "general"))
        if not fact:
            content = "Error: 'fact' is required."
            is_err = True
        elif self.memory_service is not None and self._memory_authority is not None:
            # Central memory path: write through BinaryMemoryService.
            try:
                result = self.memory_service.save_fact(
                    self._memory_authority,
                    category=category,
                    fact=fact,
                    source="save_memory",
                )
                if result.projection_dirty:
                    content = f"Saved to MEMORY.md (projection pending): [{category}] {fact}"
                else:
                    content = f"Saved to MEMORY.md: [{category}] {fact}"
                is_err = False
                log_info(f"save_memory: [{category}] {fact[:80]}")
            except Exception as e:
                content = f"Error saving to central memory: {e}"
                is_err = True
        else:
            content = "Error: Central memory is not available in this context."
            is_err = True
```

- [ ] **Step 4: Sửa docstring/comment mention RIKUGAN.md trong loop.py**

1. Sửa docstring `_handle_save_memory_tool` (dòng 1602-1610): thay "on a future read of RIKUGAN.md" → "on a future read of MEMORY.md".
2. Sửa comment dòng 1616-1618: "when RIKUGAN.md is reloaded" → "when MEMORY.md managed region is reloaded".
3. Sửa comment `_sanitize_save_memory_category` dòng 269: "``RIKUGAN.md`` line format" → "``MEMORY.md`` managed line format".
4. Sửa comment dòng 285: "survive into RIKUGAN.md" → "survive into MEMORY.md".

- [ ] **Step 5: Sửa case command message**

Sửa `loop.py` dòng 460. Từ:

```python
            yield TurnEvent.text_done("Central memory is not enabled. Set memory_workspaces_enabled=true in config.")
```

Thành:

```python
            yield TurnEvent.text_done("Central memory is not available for this binary.")
```

- [ ] **Step 6: Xóa legacy branch trong _handle_memory_command (loop_commands.py)**

Sửa `lucnhan/agent/loop_commands.py` dòng 98-147. Thay toàn bộ method. Từ:

```python
def _handle_memory_command(loop: AgentLoop) -> Generator[TurnEvent, None, None]:
    """Show current memory contents in chat.

    When central memory is enabled, reads from BinaryMemoryService
    (SQLite structured facts + unmanaged MEMORY.md notes). Otherwise
    falls back to legacy RIKUGAN.md.
    """
    # Central memory path
    if loop.memory_service is not None:
        try:
            structured = loop.memory_service.structured_context()
            manual = loop.memory_service.manual_notes_context()
            parts = []
            if structured:
                parts.append(structured)
            if manual:
                parts.append(f"\n## Manual Notes\n{manual}")
            if not parts:
                yield TurnEvent.text_done("No memory saved yet. Use `save_memory` to persist facts.")
            else:
                yield TurnEvent.text_done("**Memory**:\n\n" + "\n".join(parts))
            return
        except Exception as e:
            yield TurnEvent.error_event(f"Failed to read central memory: {e}")
            return

    # Legacy path: RIKUGAN.md
    idb_dir = ""
    if loop.session.idb_path:
        idb_dir = os.path.dirname(loop.session.idb_path)
    if not idb_dir:
        yield TurnEvent.text_done("No IDB path set — persistent memory is not available.")
        return

    md_path = os.path.join(idb_dir, "RIKUGAN.md")
    if not os.path.isfile(md_path):
        yield TurnEvent.text_done(
            "No persistent memory file found.\n\nUse `save_memory` to save facts that persist across sessions."
        )
        return

    try:
        with open(md_path, encoding="utf-8") as f:
            content = f.read()
        if not content.strip():
            yield TurnEvent.text_done("Memory file exists but is empty.")
        else:
            yield TurnEvent.text_done(f"**Persistent Memory**:\n\n{content}")
    except OSError as e:
        yield TurnEvent.error_event(f"Failed to read memory file: {e}")
```

Thành:

```python
def _handle_memory_command(loop: AgentLoop) -> Generator[TurnEvent, None, None]:
    """Show current memory contents in chat.

    Reads from BinaryMemoryService (SQLite structured facts + unmanaged
    MEMORY.md notes). When memory_service is None (identity resolution
    failed), reports central memory unavailable.
    """
    if loop.memory_service is None:
        yield TurnEvent.text_done("Central memory is not available for this binary.")
        return

    try:
        structured = loop.memory_service.structured_context()
        manual = loop.memory_service.manual_notes_context()
        parts = []
        if structured:
            parts.append(structured)
        if manual:
            parts.append(f"\n## Manual Notes\n{manual}")
        if not parts:
            yield TurnEvent.text_done("No memory saved yet. Use `save_memory` to persist facts.")
        else:
            yield TurnEvent.text_done("**Memory**:\n\n" + "\n".join(parts))
    except Exception as e:
        yield TurnEvent.error_event(f"Failed to read central memory: {e}")
```

- [ ] **Step 7: Run cutover tests**

Run: `python -m pytest tests/agent/test_memory_cutover.py tests/agent/test_memory_write_ownership.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add lucnhan/agent/loop.py lucnhan/agent/loop_commands.py tests/agent/test_memory_cutover.py
git commit -m "refactor(memory): remove legacy RIKUGAN.md save/read paths"
```

---

### Task 7: persist_plan dùng service.save_plan

**Files:**
- Modify: `lucnhan/agent/modes/plan.py:1-12, 109-128`

**Interfaces:**
- Consumes: `loop.memory_service.save_plan()`, `loop._memory_authority`
- Produces: `persist_plan(loop, user_goal, steps)` — signature không đổi, body dùng central service

- [ ] **Step 1: Thêm import log_debug**

Sửa `lucnhan/agent/modes/plan.py` dòng 11. Từ:

```python
from ...core.logging import log_error, log_info
```

Thành:

```python
from ...core.logging import log_debug, log_error, log_info
```

- [ ] **Step 2: Refactor persist_plan body**

Sửa `plan.py` dòng 109-128. Từ:

```python
def persist_plan(loop: AgentLoop, user_goal: str, steps: list[str]) -> None:
    """Save an approved plan to RIKUGAN.md for cross-session reference."""
    from ..loop import append_to_memory_file

    idb_dir = ""
    if loop.session.idb_path:
        idb_dir = os.path.dirname(loop.session.idb_path)
    if not idb_dir:
        return

    md_path = os.path.join(idb_dir, "RIKUGAN.md")
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M")
        lines = [f"\n## Plan ({timestamp})\n", f"Goal: {user_goal[:200]}\n"]
        lines += [f"{i}. {step}\n" for i, step in enumerate(steps, 1)]
        lines.append("\n")
        append_to_memory_file(md_path, "".join(lines))
        log_info(f"Plan persisted to RIKUGAN.md ({len(steps)} steps)")
    except OSError as e:
        log_error(f"Failed to persist plan to RIKUGAN.md: {e}")
```

Thành:

```python
def persist_plan(loop: AgentLoop, user_goal: str, steps: list[str]) -> None:
    """Save an approved plan to central memory for cross-session reference.

    Writes a structured plan fact via BinaryMemoryService. When central
    memory is not wired (identity failure), this is a silent no-op.
    """
    if loop.memory_service is None or loop._memory_authority is None:
        log_debug("persist_plan skipped — central memory not available")
        return

    try:
        loop.memory_service.save_plan(
            loop._memory_authority,
            goal=user_goal,
            steps=steps,
        )
        log_info(f"Plan persisted to central memory ({len(steps)} steps)")
    except Exception as e:
        log_error(f"Failed to persist plan to central memory: {e}")
```

- [ ] **Step 3: Xóa unused imports**

Kiểm tra `plan.py` dòng 5-6: `import os` và `import time`. Sau refactor, `persist_plan` không còn dùng `os.path` hay `time.strftime`. Grep:

```bash
git grep -n "os\.\|time\." -- lucnhan/agent/modes/plan.py
```
Nếu chỉ còn match trong `persist_plan` cũ (đã xóa) → xóa `import os` và `import time`. Nếu có match khác → giữ import tương ứng.

- [ ] **Step 4: Run plan tests**

Run: `python -m pytest tests/agent/ -v -k "plan" 2>&1 | head -40`
Expected: PASS hoặc no plan-persist tests. Verify syntax:

```bash
python -c "import ast; ast.parse(open('lucnhan/agent/modes/plan.py').read())"
```

- [ ] **Step 5: Commit**

```bash
git add lucnhan/agent/modes/plan.py
git commit -m "refactor(memory): persist plans via central memory service"
```

---

### Task 8: Dọn research docstring (orchestra đã dọn ở Task 5)

**Files:**
- Modify: `lucnhan/agent/modes/research.py:146, 162`

**Interfaces:**
- Consumes: (orchestra/main_agent.py đã dọn ở Task 5 Step 4c)
- Produces: research prompt mention MEMORY.md thay RIKUGAN.md

- [ ] **Step 1: Dọn research.py docstring**

Sửa `lucnhan/agent/modes/research.py`:
1. Dòng 146: `Persist confirmed findings to RIKUGAN.md for future sessions.` → `Persist confirmed findings to central memory (MEMORY.md) for future sessions.`
2. Dòng 162: `Use \`save_memory\` to persist confirmed findings to RIKUGAN.md so future sessions` → `Use \`save_memory\` to persist confirmed findings to central memory (MEMORY.md) so future sessions`

- [ ] **Step 2: Verify build_system_prompt không còn idb_dir**

Run:

```bash
git grep -n "idb_dir" -- lucnhan/agent/
```
Expected: no matches (loop.py + orchestra đã dọn ở Task 5).

- [ ] **Step 3: Run tests + verify syntax**

Run: `python -m pytest tests/agent/test_agent_loop.py tests/agent/test_system_prompt.py -v 2>&1 | head -40`
Expected: PASS. Verify syntax:

```bash
python -c "import ast; ast.parse(open('lucnhan/agent/modes/research.py').read())"
```

- [ ] **Step 4: Commit**

```bash
git add lucnhan/agent/modes/research.py
git commit -m "docs(memory): update research mode prompt to reference MEMORY.md"
```

---

### Task 9: Xóa module legacy.py + dọn docstrings

**Files:**
- Delete: `lucnhan/memory/legacy.py`
- Modify: `lucnhan/memory/__init__.py:7-17`
- Modify: `lucnhan/memory/paths.py:11`
- Modify: `lucnhan/core/sanitize.py:5`

**Interfaces:**
- Consumes: (không — legacy.py không còn caller sau Task 6)
- Produces: package `lucnhan.memory` không còn importer

- [ ] **Step 1: Verify legacy.py không còn import**

Run:

```bash
git grep -n "from.*memory.legacy import\|from.*memory\.legacy\|memory\.legacy" -- lucnhan/ tests/
```
Expected: chỉ match trong `tests/memory/test_legacy.py` (sẽ xóa Task 10) và chính `legacy.py`. Không match trong `lucnhan/` runtime code.

- [ ] **Step 2: Xóa file legacy.py**

```bash
git rm lucnhan/memory/legacy.py
```

- [ ] **Step 3: Dọn __init__.py docstring**

Sửa `lucnhan/memory/__init__.py` dòng 7-17. Xóa đoạn deprecation/dark-mode:

```python
.. deprecated::
    This folder-scoped JSONL subsystem is superseded by the central
    SQLite workspace store (``lucnhan.memory.workspace_store``,
    ``lucnhan.memory.repository``, ``lucnhan.memory.service``).
    When ``config.memory_workspaces_enabled`` is True, all readers and
    writers should use the central service instead of this module's
    ``KnowledgeRawStore`` / ``knowledge_paths`` APIs. The legacy path
    remains active only for dark-mode backward compatibility.
```

Giữ phần docstring còn lại (Storage layout, mô tả module). Module này vẫn export `KnowledgeRawStore` cho knowledge subsystem — **không xóa**.

- [ ] **Step 4: Dọn paths.py docstring**

Sửa `lucnhan/memory/paths.py` dòng 9-11. Thay:

```python
The filesystem layout is fixed by the plan. ``<idb_dir>`` is the
parent directory of the IDB file, matching how existing code derives
``idb_dir`` for ``RIKUGAN.md`` and the ``notes/`` directory.
```

Thành:

```python
The filesystem layout is fixed by the plan. ``<idb_dir>`` is the
parent directory of the IDB file, matching how existing code derives
``idb_dir`` for the ``notes/`` directory.
```

- [ ] **Step 5: Dọn sanitize.py docstring**

Sửa `lucnhan/core/sanitize.py` dòng 5. Từ:

```
(skills, RIKUGAN.md) is considered **untrusted**.  This module provides:
```

Thành:

```
(skills, MEMORY.md) is considered **untrusted**.  This module provides:
```

- [ ] **Step 6: Run memory + sanitize tests**

Run: `python -m pytest tests/memory/ tests/core/test_sanitize.py -v --ignore=tests/memory/test_legacy.py --ignore=tests/memory/test_activation_gate.py 2>&1 | tail -20`
Expected: PASS (2 ignore là file sẽ xóa Task 10).

- [ ] **Step 7: Commit**

```bash
git add lucnhan/memory/__init__.py lucnhan/memory/paths.py lucnhan/core/sanitize.py
git rm lucnhan/memory/legacy.py 2>/dev/null; true
git commit -m "refactor(memory): remove legacy importer and clean docstrings"
```

---

### Task 10: Xóa test files obsolete

**Files:**
- Delete: `tests/memory/test_legacy.py`
- Delete: `tests/memory/test_activation_gate.py`

**Interfaces:**
- Consumes: Task 9 (legacy.py đã xóa), Task 1 (flags đã xóa)
- Produces: test suite không còn obsolete tests

- [ ] **Step 1: Verify conftest không import 2 file này**

Run:

```bash
git grep -n "test_legacy\|test_activation_gate" -- tests/memory/__init__.py tests/memory/conftest.py tests/conftest.py 2>/dev/null; echo "---done---"
```
Expected: `---done---` (no matches). Nếu có match → sửa conftest xóa reference trước khi xóa file.

- [ ] **Step 2: Xóa 2 file**

```bash
git rm tests/memory/test_legacy.py tests/memory/test_activation_gate.py
```

- [ ] **Step 3: Run toàn bộ memory tests**

Run: `python -m pytest tests/memory/ -v 2>&1 | tail -30`
Expected: PASS, không còn collection error.

- [ ] **Step 4: Commit**

```bash
git commit -m "test(memory): remove obsolete legacy and activation-gate tests"
```

---

### Task 11: Dọn docs

**Files:**
- Modify: `CLAUDE.md`, `AGENTS.md`, `ARCHITECTURE.md`, `README.md`, `llms.txt`, `webpage/llms.txt`, `webpage/index.html`, `webpage/docs.html`, `webpage/ARCHITECTURE.html`
- Modify: `CHANGELOG.md` (thêm entry)

**Interfaces:**
- Consumes: toàn bộ tasks trước (code đã cutover)
- Produces: docs nhất quán với central memory

- [ ] **Step 1: Tìm mọi RIKUGAN.md mention trong docs**

Run:

```bash
git grep -ln "LUCNHAN\.md" -- CLAUDE.md AGENTS.md ARCHITECTURE.md README.md llms.txt webpage/
```
Expected: list file cần sửa. KHÔNG include `docs/superpowers/` (historical plans/specs giữ nguyên) và `CHANGELOG.md` (xử lý riêng).

- [ ] **Step 2: Sửa CLAUDE.md**

Mở `CLAUDE.md`. Cụ thể:
1. Dòng 209 bảng sanitize: `| \`sanitize_memory()\` | nội dung RIKUGAN.md |` → `| \`sanitize_memory()\` | nội dung MEMORY.md (manual notes) |`.
2. Grep toàn file: `git grep -n "LUCNHAN\.md" -- CLAUDE.md`. Mỗi mention: nếu mô tả persistent memory file → đổi `RIKUGAN.md` → `MEMORY.md`. Nếu mô tả "file cạnh IDB" → cập nhật thành "central memory workspace".
3. Tìm mention dark scaffolding flags (`memory_workspaces_enabled`, `case_memory_enabled`, `peer_retrieval_enabled`) → xóa hoặc cập nhật "always-on".

- [ ] **Step 3: Sửa AGENTS.md**

Mở `AGENTS.md`:
1. Dòng 579: `- **\`sanitize_memory()\`** — RIKUGAN.md content loaded into the system prompt.` → `- **\`sanitize_memory()\`** — MEMORY.md manual notes loaded into the system prompt.`
2. Grep: `git grep -n "LUCNHAN\.md\|memory_workspaces_enabled\|case_memory_enabled\|peer_retrieval_enabled" -- AGENTS.md`. Sửa từng match.

- [ ] **Step 4: Sửa ARCHITECTURE.md, README.md, llms.txt, webpage/**

Cho mỗi file trong list Step 1:
1. Grep mention `RIKUGAN.md` → thay bằng `MEMORY.md` (hoặc "central memory workspace" nếu ngữ cảnh là "file cạnh IDB").
2. Grep mention 3 flags → xóa hoặc note "always-on".
3. `webpage/*.html` — đây là generated HTML, có thể cần regenerate. Nếu chỉ text content → sửa inline. Nếu là build artifact → note trong commit và skip (regenerate riêng).

- [ ] **Step 5: Thêm CHANGELOG entry**

Sửa `CHANGELOG.md`. Thêm entry ở đầu (sau header), dạng:

```markdown
## [Unreleased]

### Added
- Central memory subsystem (`BinaryMemoryService`) now the sole persistent
  memory path — SQLite structured facts + `MEMORY.md` managed region.

### Removed
- Legacy `RIKUGAN.md` runtime read/write. **Legacy `RIKUGAN.md` data is
  not migrated — the old file is ignored.**
- `lucnhan/memory/legacy.py` importer (clean break, no migration tool).
- Config flags `memory_workspaces_enabled`, `case_memory_enabled`,
  `peer_retrieval_enabled` (central memory is always-on).
```

- [ ] **Step 6: Verify không còn runtime RIKUGAN.md mention**

Run:

```bash
git grep -n "LUCNHAN\.md" -- lucnhan/
```
Expected: no matches (code runtime sạch). Historical plans/specs trong `docs/superpowers/` vẫn có mention — đó OK.

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md AGENTS.md ARCHITECTURE.md README.md llms.txt CHANGELOG.md webpage/
git commit -m "docs(memory): document central MEMORY.md cutover, remove RIKUGAN.md refs"
```

---

### Task 12: Full verification

**Files:** (không sửa, chỉ verify)

**Interfaces:**
- Consumes: toàn bộ tasks
- Produces: xác nhận cutover hoàn tất

- [ ] **Step 1: Chạy ci-local.sh**

Run: `./ci-local.sh`
Expected: PASS (format + lint + mypy + pytest + desloppify). Nếu fail → đọc output, sửa task liên quan. Desloppify score có thể giảm nhẹ do xóa code — nếu giảm > 0.5 → review.

- [ ] **Step 2: Grep verification — không còn runtime reference**

Run:

```bash
echo "=== RIKUGAN.md in lucnhan/ ==="
git grep -n "LUCNHAN\.md" -- lucnhan/ || echo "CLEAN"
echo "=== flags in lucnhan/ ==="
git grep -n "memory_workspaces_enabled\|case_memory_enabled\|peer_retrieval_enabled" -- lucnhan/ || echo "CLEAN"
echo "=== append_to_memory_file / _load_persistent_memory ==="
git grep -n "append_to_memory_file\|_load_persistent_memory\|_MEMORY_HEADER\|_MEMORY_CACHE" -- lucnhan/ || echo "CLEAN"
echo "=== idb_dir param ==="
git grep -n "idb_dir" -- lucnhan/agent/ || echo "CLEAN"
```
Expected: tất cả CLEAN (không match).

- [ ] **Step 3: Grep tests**

Run:

```bash
echo "=== flags in tests/ ==="
git grep -n "memory_workspaces_enabled\|case_memory_enabled\|peer_retrieval_enabled" -- tests/ || echo "CLEAN"
echo "=== legacy import in tests/ ==="
git grep -n "from lucnhan.memory.legacy\|memory\.legacy" -- tests/ || echo "CLEAN"
```
Expected: tất cả CLEAN.

- [ ] **Step 4: Import smoke test**

Run:

```bash
python -c "from lucnhan.core.config import LucNhanConfig; c = LucNhanConfig(); print('config OK')"
python -c "from lucnhan.memory.manager import MemoryWorkspaceManager; print('manager OK')"
python -c "from lucnhan.agent.system_prompt import build_system_prompt; print(build_system_prompt()[:50])"
python -c "from lucnhan.agent.loop import AgentLoop; print('loop OK')"
```
Expected: 4 dòng `... OK` + prompt prefix, không có ImportError/AttributeError.

- [ ] **Step 5: Final commit (nếu có thay đổi nhỏ)**

Nếu Step 1-4 yêu cầu fix nhỏ → commit:

```bash
git add -p  # stage cụ thể
git commit -m "fix(memory): final cutover cleanup"
```

Nếu tất cả PASS → không commit, cutover hoàn tất.

---

## Self-Review

**1. Spec coverage:**
- Phase 1 (config flags) → Task 1 ✅
- Phase 1 (manager guards) → Task 2 ✅
- Phase 1 (controller) → Task 4 ✅
- Phase 2 (system_prompt) → Task 5 ✅
- Phase 2 (loop save_memory + case message) → Task 6 ✅
- Phase 2 (plan persist) → Task 7 ✅
- Phase 2 (loop_commands memory) → Task 6 Step 6 ✅
- Phase 2 (research docstring) → Task 8 ✅
- Phase 2 (orchestra idb_dir) → Task 8 ✅
- Phase 3 (legacy.py xóa) → Task 9 ✅
- Phase 3 (docstrings) → Task 9 ✅
- Phase 4 (test_manager) → Task 2 ✅
- Phase 4 (test_foundation_gate) → Task 3 ✅
- Phase 4 (test_case_binding) → Task 3 ✅
- Phase 4 (test_config/first_open/case_*) → Task 1 + Task 3 ✅
- Phase 4 (test_cutover/prompt_cutover) → Task 5 + Task 6 ✅
- Phase 4 (test_activation_gate/test_legacy xóa) → Task 10 ✅
- Phase 5 (docs) → Task 11 ✅
- Phase 5 (CHANGELOG) → Task 11 ✅
- Verify → Task 12 ✅

**2. Placeholder scan:** Không có TBD/TODO. Mỗi step có code thực hoặc command thực.

**3. Type consistency:**
- `build_system_prompt()` signature: xóa `idb_dir` ở Task 5, **cả 2 caller** (loop.py + orchestra) dọn trong cùng Task 5 (Step 4b + 4c) để commit tự đứng ✅
- `persist_plan(loop, user_goal, steps)` signature không đổi (Task 7) — caller `exploration.py:249` không cần sửa ✅
- `save_fact(authority, *, category, fact, source)` — dùng ở Task 6, khớp `service.py:131` ✅
- `save_plan(authority, *, goal, steps)` — dùng ở Task 7, khớp `service.py:179` ✅
- `MemoryWorkspaceManager.bind()` — Task 2 xóa branch nhưng signature `(request, choice)` không đổi ✅

Gaps: không phát hiện.
