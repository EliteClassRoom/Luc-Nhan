# Chat History On-Demand Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mở Luc Nhan hoặc đổi IDB luôn tạo đúng một `New Chat` trống, đồng thời cung cấp panel History bên phải để tìm và mở lại chat của IDB hiện tại theo yêu cầu.

**Architecture:** Giữ nguyên per-session JSON và manifest index. `SessionHistory` sở hữu persistence/hardening, `SessionControllerBase` cung cấp các API list/load/attach không phụ thuộc Qt, `HistoryPanel` chỉ trình bày metadata, còn `LucNhanPanelCore` điều phối một executor riêng, queue, generation counter và QTimer main-thread để tránh I/O hoặc Qt xuyên thread.

**Tech Stack:** Python 3.11, dataclasses/Enum, `concurrent.futures.ThreadPoolExecutor`, `queue.Queue`, PySide6 qua `lucnhan.ui.qt_compat`, pytest/unittest, ruff, mypy.

## Global Constraints

- IDA Pro ≥ 9.0; Qt binding chỉ dùng PySide6 qua `lucnhan/ui/qt_compat.py`.
- Không import `ida_*` hoặc PySide6 mới ngoài import seam; không phát Qt signal từ worker thread.
- Mọi mutation widget/tab chạy trên Qt main thread; worker chỉ làm Python file I/O và tạo immutable DTO.
- `_history_executor` là single-worker executor riêng, không bao giờ là `_SAVE_EXECUTOR`.
- Startup và IDB change không đọc manifest/session payload và luôn kết thúc bằng đúng một empty `New Chat`.
- History v1 chỉ Current IDB, mở + tìm kiếm; không delete, rename, archive, pin, pagination hoặc cross-IDB.
- Session JSON hiện có không bị rewrite trong manifest migration.
- Session ID hợp lệ khớp `^[A-Za-z0-9_-]{1,32}$` và phải qua path-containment check trước mọi I/O.
- `MANIFEST_SCHEMA_VERSION = 2`; `HISTORY_TITLE_MAX_CHARS = 80`.
- Test theo RED → GREEN → IMPROVE; không thêm dependency test mới.
- Không commit trong quá trình thực thi plan này trừ khi người dùng yêu cầu riêng.

---

## File Structure

### Tạo mới

- `lucnhan/state/history_types.py` — immutable DTO/status contracts dùng chung, không import Qt.
- `lucnhan/ui/history_panel.py` — widget History thụ động: states, metadata rows, search và main-thread signals.
- `tests/state/test_history_on_demand.py` — validation, title, manifest v2, filter và storage safety.
- `tests/core/test_config_history.py` — tương thích config cũ và loại bỏ auto-restore key.
- `tests/ui/test_history_panel.py` — states/search/signals/plain-text/theme của widget.
- `tests/integration/test_history_on_demand.py` — luồng startup → list → open → dedupe → continue → IDB switch → restart.

### Sửa

- `lucnhan/constants.py` — thêm `HISTORY_TITLE_MAX_CHARS = 80`; không thêm sentinel không cần thiết.
- `lucnhan/state/history.py` — ID validation/path containment, title derivation, manifest v2, `updated_at`, predicate Current-IDB dùng chung.
- `lucnhan/core/config.py` — xóa `startup_restore_sessions` và các validation/load branches.
- `lucnhan/ui/session_controller_base.py` — scope/list/load/attach/dedupe APIs; async save khi đổi IDB; shared tab-title helper; bỏ legacy restore APIs sau audit.
- `lucnhan/ui/panel_core.py` — bỏ auto-restore; thêm History button/panel, right-panel coordinator, worker/queue/timer/generation, IDB/shutdown cleanup và open-session flow.
- `lucnhan/ui/theme/widgets_common.py` — style getters dành riêng cho History panel/rows.
- `lucnhan/ui/styles.py` — re-export style getters mới.
- `tests/agent/test_state.py` — test manifest backfill/non-destructive migration nếu fixture hiện có phù hợp.
- `tests/agent/test_session_controller.py` — rewrite legacy restore tests và thêm APIs/races mới.
- `tests/tools/test_panel_core.py` — lifecycle/concurrency/right-panel/startup regression.
- `tests/ui/test_chat_view_restore.py` — regression tab-close/IDB-switch giữa async restore nếu test seam hiện tại phù hợp.
- `CHANGELOG.md`, `AGENTS.md`, `CLAUDE.md`, `DEVELOPMENT.md` — cập nhật hành vi và invariants.

---

### Task 1: Shared History Contracts

**Files:**
- Create: `lucnhan/state/history_types.py`
- Modify: `lucnhan/constants.py`
- Create: `tests/state/test_history_on_demand.py`

**Interfaces:**
- Produces: `SessionHistoryEntry`, `HistoryScope`, `HistoryRequestStatus`, `HistoryLoadResult`, `HistoryAttachStatus`, `HistoryAttachResult`, `HISTORY_TITLE_MAX_CHARS`.
- Consumed by: Tasks 2, 3, 5, 6, 8–10.

- [ ] **Step 1: Write failing frozen-contract tests**

```python
# tests/state/test_history_on_demand.py
from dataclasses import FrozenInstanceError

import pytest

from lucnhan.state.history_types import (
    HistoryAttachResult,
    HistoryAttachStatus,
    HistoryLoadResult,
    HistoryRequestStatus,
    HistoryScope,
    SessionHistoryEntry,
)


def test_history_entry_and_scope_are_frozen() -> None:
    entry = SessionHistoryEntry("abc123", "Title", 1.0, 2.0, "anthropic", "claude", 3)
    scope = HistoryScope("C:/sample.i64", "deadbeef", 7)

    with pytest.raises(FrozenInstanceError):
        entry.title = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        scope.generation = 8  # type: ignore[misc]


def test_history_result_status_contracts() -> None:
    scope = HistoryScope("C:/sample.i64", "deadbeef", 1)
    loaded = HistoryLoadResult(status=HistoryRequestStatus.NOT_FOUND, scope=scope)
    attached = HistoryAttachResult(status=HistoryAttachStatus.STALE_SCOPE)

    assert loaded.session is None
    assert loaded.error == ""
    assert HistoryRequestStatus.SAVE_FLUSH_TIMEOUT.value == "save_flush_timeout"
    assert attached.tab_id == ""
```

- [ ] **Step 2: Run RED test**

Run:

```bash
python -m pytest tests/state/test_history_on_demand.py -v
```

Expected: FAIL during import because `lucnhan.state.history_types` does not exist.

- [ ] **Step 3: Add the minimal type module and constant**

```python
# lucnhan/state/history_types.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .session import SessionState


@dataclass(frozen=True)
class SessionHistoryEntry:
    session_id: str
    title: str
    created_at: float
    updated_at: float
    provider: str
    model: str
    message_count: int


@dataclass(frozen=True)
class HistoryScope:
    idb_path: str
    db_instance_id: str
    generation: int


class HistoryRequestStatus(str, Enum):
    LISTED = "listed"
    LOADED = "loaded"
    NOT_FOUND = "not_found"
    WRONG_IDB = "wrong_idb"
    EMPTY = "empty"
    SAVE_FLUSH_TIMEOUT = "save_flush_timeout"
    FAILED = "failed"


class HistoryAttachStatus(str, Enum):
    OPENED = "opened"
    ALREADY_OPEN = "already_open"
    STALE_SCOPE = "stale_scope"


@dataclass(frozen=True)
class HistoryLoadResult:
    status: HistoryRequestStatus
    scope: HistoryScope
    session: SessionState | None = None
    error: str = ""


@dataclass(frozen=True)
class HistoryAttachResult:
    status: HistoryAttachStatus
    tab_id: str = ""
    session: SessionState | None = None
```

Add to `lucnhan/constants.py`:

```python
HISTORY_TITLE_MAX_CHARS = 80
```

- [ ] **Step 4: Run GREEN test and lint the new module**

Run:

```bash
python -m pytest tests/state/test_history_on_demand.py -v
python -m ruff check lucnhan/state/history_types.py tests/state/test_history_on_demand.py
```

Expected: PASS; ruff exits 0.

---

### Task 2: Session ID Validation and Path Containment

**Files:**
- Modify: `lucnhan/state/history.py:34-41, 204-277, 362-444, 581-587`
- Extend: `tests/state/test_history_on_demand.py`

**Interfaces:**
- Consumes: no upper-layer interfaces.
- Produces: `_validate_session_id(session_id: object) -> bool`, `_session_path(session_id: str) -> str | None`; hardened `load_session`, `delete_session`, manifest validation/rebuild/list.

- [ ] **Step 1: Add failing boundary tests**

```python
# append to tests/state/test_history_on_demand.py
import json
from pathlib import Path

from lucnhan.core.config import LucNhanConfig
from lucnhan.state.history import SessionHistory


def _history(tmp_path: Path) -> SessionHistory:
    config = LucNhanConfig()
    config._config_dir = str(tmp_path)
    return SessionHistory(config)


@pytest.mark.parametrize(
    "session_id",
    ["", ".", "..", "../escape", "..\\escape", "/absolute", "a" * 33, "x.json", "x\x00y"],
)
def test_invalid_session_ids_do_not_touch_storage(tmp_path: Path, session_id: str) -> None:
    history = _history(tmp_path)
    before = sorted(Path(history._dir).iterdir())

    assert history.load_session(session_id) is None
    assert history.delete_session(session_id) is False
    assert sorted(Path(history._dir).iterdir()) == before


def test_rebuild_and_list_skip_tampered_ids(tmp_path: Path) -> None:
    history = _history(tmp_path)
    bad_path = Path(history._dir) / "safe-file.json"
    bad_path.write_text(
        json.dumps({"id": "../../escape", "messages": [], "created_at": 1.0}),
        encoding="utf-8",
    )

    assert history.list_sessions(idb_path="C:/sample.i64") == []
```

- [ ] **Step 2: Run RED tests**

Run:

```bash
python -m pytest tests/state/test_history_on_demand.py -k "invalid_session or tampered" -v
```

Expected: at least one invalid ID reaches the filesystem or tampered entry survives rebuild.

- [ ] **Step 3: Implement one validation/path helper and call it at every boundary**

```python
# lucnhan/state/history.py
import re

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _validate_session_id(session_id: object) -> bool:
    return isinstance(session_id, str) and session_id not in {".", ".."} and _SESSION_ID_RE.fullmatch(session_id) is not None


class SessionHistory:
    def _session_path(self, session_id: str) -> str | None:
        if not _validate_session_id(session_id):
            return None
        root = os.path.realpath(self._dir)
        candidate = os.path.realpath(os.path.join(root, f"{session_id}.json"))
        try:
            if os.path.commonpath((root, candidate)) != root:
                return None
        except ValueError:
            return None
        return candidate
```

Apply the helper before path/stat/open/remove in:

```python
# _validate_manifest_entry
file_path = self._session_path(session_id)
if file_path is None:
    log_warning(f"Skipping invalid session id in manifest: {session_id!r}")
    return False

# _rebuild_manifest
sid = data.get("id", fname[:-5])
if not _validate_session_id(sid):
    log_warning(f"Skipping session file with invalid id: {fname}")
    continue

# load_session
path = self._session_path(session_id)
if path is None:
    return None

# delete_session
path = self._session_path(session_id)
if path is None:
    return False
```

Filter invalid manifest keys in both loops inside `list_sessions()` before calling `_validate_manifest_entry`.

- [ ] **Step 4: Run focused and existing persistence tests**

Run:

```bash
python -m pytest tests/state/test_history_on_demand.py -k "invalid_session or tampered" -v
python -m pytest tests/agent/test_state.py tests/state/test_history_async.py -v
```

Expected: PASS; existing save/load/delete tests remain green.

---

### Task 3: Shared Title Derivation and Manifest v2

**Files:**
- Modify: `lucnhan/state/history.py:34-35, 140-188, 221-331, 446-571`
- Modify: `lucnhan/constants.py`
- Extend: `tests/state/test_history_on_demand.py`
- Extend: `tests/agent/test_state.py`

**Interfaces:**
- Produces: `derive_history_title(messages: Sequence[Message], max_chars: int = HISTORY_TITLE_MAX_CHARS) -> str`, `_matches_current_idb(*, entry_idb_path: object, entry_db_instance_id: object, target_idb_path: str, target_db_instance_id: str) -> bool`.
- Consumed by: Tasks 4, 6, 7.

- [ ] **Step 1: Write failing pure-title and manifest tests**

```python
# tests/state/test_history_on_demand.py
from lucnhan.core.types import Message, Role
from lucnhan.state.history import derive_history_title
from lucnhan.state.session import SessionState


def test_title_uses_first_sanitized_user_message() -> None:
    messages = [
        Message(role=Role.ASSISTANT, content="skip"),
        Message(role=Role.USER, content="  Analyze\n\n [SYSTEM] this parser  "),
    ]
    title = derive_history_title(messages, max_chars=80)
    assert "[SYSTEM]" not in title
    assert title == "Analyze this parser"


def test_title_fallback_and_truncation() -> None:
    assert derive_history_title([], max_chars=80) == "Untitled chat"
    assert len(derive_history_title([Message(role=Role.USER, content="x" * 100)], max_chars=20)) == 20


def test_manifest_v1_rebuilds_to_v2_without_rewriting_session(tmp_path: Path) -> None:
    history = _history(tmp_path)
    session = SessionState(id="abc123def456", idb_path="C:/sample.i64", db_instance_id="deadbeef")
    session.add_message(Message(role=Role.USER, content="Analyze parser"))
    session_path = Path(history.save_session(session))
    before = session_path.read_bytes()
    manifest_path = Path(history._manifest_path())
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    rows = history.list_sessions(idb_path=session.idb_path, db_instance_id=session.db_instance_id)

    rebuilt = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert rebuilt["version"] == 2
    assert rows[0]["description"] == "Analyze parser"
    assert isinstance(rows[0]["updated_at"], int)
    assert session_path.read_bytes() == before
```

- [ ] **Step 2: Run RED tests**

Run:

```bash
python -m pytest tests/state/test_history_on_demand.py -k "title or manifest_v1" -v
```

Expected: import failure for `derive_history_title`, manifest remains version 1/no `updated_at`.

- [ ] **Step 3: Implement the shared helper and schema fields**

```python
# lucnhan/state/history.py
from collections.abc import Sequence

from ..constants import HISTORY_TITLE_MAX_CHARS
from ..core.types import Message, Role, _safe_persisted_identifier

MANIFEST_SCHEMA_VERSION = 2


def derive_history_title(
    messages: Sequence[Message],
    max_chars: int = HISTORY_TITLE_MAX_CHARS,
) -> str:
    for message in messages:
        if message.role is not Role.USER:
            continue
        text = _safe_persisted_identifier(message.content)
        text = " ".join(text.split())
        if text:
            return text[:max_chars]
    return "Untitled chat"
```

In `save_session()` and `_build_manifest_entry()`:

```python
safe_description = _safe_persisted_identifier(description)
title = safe_description or derive_history_title(session.messages)
# persist title as description in session JSON and manifest
```

In `_rebuild_manifest()`, hydrate only valid message dicts with `Message.from_dict`, derive the same title when legacy `description` is empty, and include:

```python
"updated_at": int(st.st_mtime),
"file_mtime_ns": file_mtime,
```

In `_build_manifest_entry()`, include the same `updated_at` based on the just-written session file.

- [ ] **Step 4: Extract one current-IDB predicate and exclude empty rows**

```python
def _canonical_instance_id(value: object) -> str:
    text = _safe_persisted_identifier(value).strip().lower()
    return text if len(text) == 32 and all(ch in "0123456789abcdef" for ch in text) else ""


def _matches_current_idb(
    *,
    entry_idb_path: object,
    entry_db_instance_id: object,
    target_idb_path: str,
    target_db_instance_id: str,
) -> bool:
    entry_instance = _canonical_instance_id(entry_db_instance_id)
    target_instance = _canonical_instance_id(target_db_instance_id)
    if entry_instance:
        return bool(target_instance) and entry_instance == target_instance
    entry_path = _normalize_db_path(str(entry_idb_path or ""))
    return bool(entry_path) and entry_path == _normalize_db_path(target_idb_path)
```

Use this exact predicate in both pre-rebuild and post-rebuild `list_sessions()` loops. Skip rows whose `messages` count is zero. Sanitize `description` again before returning it.

- [ ] **Step 5: Run storage suite**

Run:

```bash
python -m pytest tests/state/test_history_on_demand.py tests/agent/test_state.py tests/state/test_history_async.py -v
python -m ruff check lucnhan/state/history.py tests/state/test_history_on_demand.py
```

Expected: PASS, including non-destructive v1→v2 rebuild and legacy filter fallback.

---

### Task 4: Remove Legacy Startup Restore Configuration

**Files:**
- Modify: `lucnhan/core/config.py:149-153, 210-211, 236-238, 315-356`
- Create: `tests/core/test_config_history.py`

**Interfaces:**
- Produces: `LucNhanConfig` without `startup_restore_sessions`; legacy JSON key ignored.
- Consumed by: Task 8 startup cleanup.

- [ ] **Step 1: Write failing compatibility tests**

```python
# tests/core/test_config_history.py
import json
from pathlib import Path

from lucnhan.core.config import LucNhanConfig


def test_legacy_startup_restore_key_is_ignored_and_not_resaved(tmp_path: Path) -> None:
    config = LucNhanConfig()
    config._config_dir = str(tmp_path)
    Path(config.config_path).write_text(
        json.dumps({"startup_restore_sessions": "all"}),
        encoding="utf-8",
    )

    config.load()
    config.save()

    saved = json.loads(Path(config.config_path).read_text(encoding="utf-8"))
    assert not hasattr(config, "startup_restore_sessions")
    assert "startup_restore_sessions" not in saved
    assert config.validate() == []
```

- [ ] **Step 2: Run RED test**

Run:

```bash
python -m pytest tests/core/test_config_history.py -v
```

Expected: FAIL because the dataclass still has `startup_restore_sessions` and saves it.

- [ ] **Step 3: Remove all five production references**

Delete from `lucnhan/core/config.py`:

```python
startup_restore_sessions: str = "all"
```

Delete validation and normalization branches, the `_apply_loaded_config` allow-list entry, and load-time special case. Do not add a replacement field; unknown JSON keys remain ignored by the explicit allow-list.

- [ ] **Step 4: Run tests and source audit**

Run:

```bash
python -m pytest tests/core/test_config_history.py tests/core -v
python -c "from pathlib import Path; hits=[p for p in Path('lucnhan').rglob('*.py') if 'startup_restore_sessions' in p.read_text(encoding='utf-8')]; assert not hits, hits"
```

Expected: PASS; source audit prints nothing.

---

### Task 5: Controller History Scope, List, Load, Attach, and Async IDB Save

**Files:**
- Modify: `lucnhan/ui/session_controller_base.py:27-41, 212-298, 597-727`
- Modify: `tests/agent/test_session_controller.py:22-169`

**Interfaces:**
- Consumes: Task 1 result DTOs; Task 3 title/filter helpers.
- Produces:
  - `capture_history_scope(generation: int) -> HistoryScope`
  - `list_history_sessions(scope: HistoryScope) -> list[SessionHistoryEntry]`
  - `load_history_session(session_id: str, scope: HistoryScope) -> HistoryLoadResult`
  - `attach_history_session(result: HistoryLoadResult) -> HistoryAttachResult`
  - `find_tab_for_session(persisted_session_id: str) -> str | None`
- Consumed by: Tasks 8–10.

- [ ] **Step 1: Rewrite legacy restore tests first**

Replace the existing `test_restore_session`, `test_restore_preserves_token_usage`, and `test_restore_preserves_tool_calls` with tests that persist a session, then call:

```python
scope = self.ctrl.capture_history_scope(generation=1)
loaded = self.ctrl.load_history_session(session.id, scope)
assert loaded.status is HistoryRequestStatus.LOADED
before = len(self.ctrl.tab_ids)
attached = self.ctrl.attach_history_session(loaded)
assert attached.status is HistoryAttachStatus.OPENED
assert len(self.ctrl.tab_ids) == before + 1
assert self.ctrl._sessions[attached.tab_id].id == session.id
# Keep the previous token_usage/tool_calls assertions on attached.session.
```

Add focused cases:

```python
def _save_history_session(self, instance_id: str) -> str:
    session = SessionState(
        id="saved-history",
        idb_path=self.ctrl._idb_path,
        db_instance_id=instance_id,
    )
    session.add_message(Message(role=Role.USER, content="Analyze parser"))
    SessionHistory(self.cfg).save_session(session)
    return session.id


def _loaded_result_for_current_idb(self, generation: int) -> HistoryLoadResult:
    saved_id = self._save_history_session(self.ctrl._db_instance_id)
    scope = self.ctrl.capture_history_scope(generation)
    return self.ctrl.load_history_session(saved_id, scope)


def _load_result_for_instance(self, *, saved_instance: str, live_instance: str) -> HistoryLoadResult:
    self.ctrl._db_instance_id = live_instance
    saved_id = self._save_history_session(saved_instance)
    scope = self.ctrl.capture_history_scope(generation=1)
    return self.ctrl.load_history_session(saved_id, scope)


def test_load_does_not_mutate_sessions(self) -> None:
    saved_id = self._save_history_session(self.ctrl._db_instance_id)
    scope = self.ctrl.capture_history_scope(generation=3)
    before = dict(self.ctrl._sessions)

    result = self.ctrl.load_history_session(saved_id, scope)

    self.assertIs(result.status, HistoryRequestStatus.LOADED)
    self.assertEqual(self.ctrl._sessions, before)


def test_attach_returns_already_open_for_persisted_id(self) -> None:
    saved_id = self._save_history_session("current-idb")
    scope = self.ctrl.capture_history_scope(generation=3)
    loaded = self.ctrl.load_history_session(saved_id, scope)
    first = self.ctrl.attach_history_session(loaded)

    second = self.ctrl.attach_history_session(loaded)

    self.assertIs(first.status, HistoryAttachStatus.OPENED)
    self.assertIs(second.status, HistoryAttachStatus.ALREADY_OPEN)
    self.assertEqual(second.tab_id, first.tab_id)


def test_attach_rejects_stale_live_scope(self) -> None:
    loaded = self._loaded_result_for_current_idb(generation=4)
    self.ctrl._db_instance_id = "b" * 32

    result = self.ctrl.attach_history_session(loaded)

    self.assertIs(result.status, HistoryAttachStatus.STALE_SCOPE)


def test_wrong_idb_payload_is_not_loaded(self) -> None:
    result = self._load_result_for_instance(saved_instance="a" * 32, live_instance="b" * 32)
    self.assertIs(result.status, HistoryRequestStatus.WRONG_IDB)
    self.assertIsNone(result.session)


def test_find_tab_matches_session_id_not_tab_id(self) -> None:
    tab_id = self.ctrl.active_tab_id
    persisted_id = self.ctrl.session.id
    self.assertEqual(self.ctrl.find_tab_for_session(persisted_id), tab_id)
    self.assertIsNone(self.ctrl.find_tab_for_session(tab_id))
```

- [ ] **Step 2: Run RED controller tests**

Run:

```bash
python -m pytest tests/agent/test_session_controller.py -k "history or restore or stale or wrong_idb" -v
```

Expected: FAIL because the new APIs do not exist.

- [ ] **Step 3: Add runtime-light controller APIs**

Import DTOs normally because `history_types.py` has no heavy chain. Preserve the existing lazy imports for `SessionHistory`/`SessionState`.

```python
def capture_history_scope(self, generation: int) -> HistoryScope:
    return HistoryScope(self._idb_path, self._db_instance_id, generation)


def find_tab_for_session(self, persisted_session_id: str) -> str | None:
    for tab_id, session in self._sessions.items():
        if session.id == persisted_session_id:
            return tab_id
    return None
```

`list_history_sessions()` calls only `SessionHistory.list_sessions(...)` and converts manifest summaries directly to `SessionHistoryEntry`; it must not call `load_session()` per row.

`load_history_session()` loads one payload and uses the same `_matches_current_idb` helper on `SessionState` fields. `attach_history_session()` compares `result.scope` with the current live path/instance, performs post-load dedupe, then inserts the loaded object under a new `uuid4().hex[:8]` tab key.

- [ ] **Step 4: Make IDB reset saves non-blocking**

Replace the blocking loop in `reset_for_new_file()`:

```python
for tab_id, session in list(self._sessions.items()):
    if not session.messages:
        continue
    try:
        future = SessionHistory(self.config).save_session_async(session)
        future.add_done_callback(self._on_async_save_done)
    except (OSError, ValueError) as exc:
        log_error(f"Failed to enqueue session {tab_id} on file change: {exc}")
```

Then clear/reset sessions as before. Futures retain detached old sessions; no code mutates them after reset. Keep shutdown's existing synchronous durability behavior unchanged.

- [ ] **Step 5: Use shared title helper for tab labels**

```python
def tab_label(self, tab_id: str) -> str:
    session = self._sessions.get(tab_id)
    if session is None or not session.messages:
        return "New Chat"
    return derive_history_title(session.messages, max_chars=20)
```

- [ ] **Step 6: Run controller/storage tests**

Run:

```bash
python -m pytest tests/agent/test_session_controller.py tests/state/test_history_on_demand.py -v
python -m mypy lucnhan/core lucnhan/providers
```

Expected: PASS. Mypy remains green for configured scopes.

---

### Task 6: Isolated HistoryPanel Widget

**Files:**
- Create: `lucnhan/ui/history_panel.py`
- Modify: `lucnhan/ui/theme/widgets_common.py:229-249`
- Modify: `lucnhan/ui/styles.py:108-130`
- Create: `tests/ui/test_history_panel.py`

**Interfaces:**
- Consumes: `SessionHistoryEntry`.
- Produces: `HistoryPanel` with signals `session_open_requested(str)`, `close_requested()`, `retry_requested()` and methods `set_loading()`, `set_entries(entries)`, `set_error(message, retry_visible=True)`, `clear()`, `shutdown()`.
- Consumed by: Tasks 8–10.

- [ ] **Step 1: Write failing UI state/search/signal tests**

Use `tests.qt_stubs.ensure_pyside6_stubs()` before importing the widget. Pin observable behavior, not pixel screenshots:

```python
def test_empty_and_search_empty_copy() -> None:
    panel = HistoryPanel()
    panel.set_entries([])
    assert panel._status_label.text() == "No saved chats for this IDB yet."

    panel.set_entries([_entry(title="Parser")])
    panel._search.setText("missing")
    assert panel._status_label.text() == "No chats match your search."


def test_search_is_case_insensitive_title_only() -> None:
    panel = HistoryPanel()
    panel.set_entries([_entry("a", "Analyze Parser"), _entry("b", "Map imports")])
    panel._search.setText(" parser ")
    assert panel.visible_session_ids() == ["a"]


def test_row_title_is_plain_text() -> None:
    panel = HistoryPanel()
    panel.set_entries([_entry(title="<b>not rich</b>")])
    row = panel._row_widgets[0]
    assert row._title.textFormat() == Qt.TextFormat.PlainText
```

Also test Retry appears only after `set_error(..., retry_visible=True)`, close/retry/open signals carry the expected values, and `clear()` resets cached entries/query.

- [ ] **Step 2: Run RED test**

Run:

```bash
python -m pytest tests/ui/test_history_panel.py -v
```

Expected: FAIL because `history_panel.py` does not exist.

- [ ] **Step 3: Implement a passive widget only**

```python
class HistoryPanel(QFrame):
    session_open_requested = Signal(str)
    close_requested = Signal()
    retry_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: tuple[SessionHistoryEntry, ...] = ()
        self._row_widgets: list[HistoryRowWidget] = []
        # Header: "Chat History", "Current IDB", close button.
        # Search QLineEdit: "Search conversations…".
        # Stacked states: loading/status/list/error with Retry.
        # No SessionHistory/config/executor imports.

    def set_entries(self, entries: list[SessionHistoryEntry]) -> None:
        self._entries = tuple(entries)
        self._apply_search()

    def _apply_search(self) -> None:
        query = self._search.text().strip().casefold()
        visible = [entry for entry in self._entries if query in entry.title.casefold()]
        self._render_rows(visible)
```

Use `bind_theme`/`disconnect_theme` like `MutationLogPanel`; force title labels to `Qt.TextFormat.PlainText`. Never interpolate title text into QSS.

- [ ] **Step 4: Add dedicated token-driven style getters**

Add `get_history_panel_style`, `get_history_row_style`, `get_history_title_style`, `get_history_meta_style` in `widgets_common.py`, re-export from `styles.py`, and apply through `maybe_host_stylesheet()` so IDA-native mode inherits host palette.

- [ ] **Step 5: Run widget tests and style lint**

Run:

```bash
python -m pytest tests/ui/test_history_panel.py -v
python -m ruff check lucnhan/ui/history_panel.py lucnhan/ui/theme/widgets_common.py tests/ui/test_history_panel.py
```

Expected: PASS.

---

### Task 7: Fresh-by-Default Startup and Legacy Restore Removal

**Files:**
- Modify: `lucnhan/ui/panel_core.py:465-573, 1269-1317, 1584-1631`
- Modify: `lucnhan/ui/session_controller_base.py:654-711`
- Modify: `lucnhan/state/history.py:573-579`
- Extend: `tests/tools/test_panel_core.py`
- Extend: `tests/agent/test_session_controller.py`

**Interfaces:**
- Consumes: Task 4 config removal.
- Produces: startup/IDB change with no auto-restore; no legacy bulk restore APIs after caller audit.
- Required before: Task 8 History coordination.

- [ ] **Step 1: Write failing no-I/O startup tests**

Add a source/behavior regression around `_build_ui()`:

```python
def test_build_ui_does_not_restore_or_read_history(monkeypatch) -> None:
    panel = _make_panel()
    restore = MagicMock()
    panel._ctrl.restore_sessions = restore
    # Build with existing Qt stubs/fixtures.
    LucNhanPanelCore._build_ui(panel)
    restore.assert_not_called()
    assert panel._pending_restore_messages == {}
```

Add `on_database_changed` test asserting no call to restore and exactly one recreated tab.

- [ ] **Step 2: Run RED tests**

Run:

```bash
python -m pytest tests/tools/test_panel_core.py -k "restore or database_changed or startup" -v
```

Expected: FAIL because `_build_ui` and `on_database_changed` still invoke `_try_restore_session()`.

- [ ] **Step 3: Remove automatic restore call sites and method**

Delete `_try_restore_session()` calls from `_build_ui()` and `on_database_changed()`, then delete `_try_restore_session()` itself.

Audit:

```bash
python -c "from pathlib import Path; hits=[(p,n+1,l) for p in Path('lucnhan').rglob('*.py') for n,l in enumerate(p.read_text(encoding='utf-8').splitlines()) if '_try_restore_session' in l]; assert not hits, hits"
```

- [ ] **Step 4: Remove dead legacy restore APIs after grep**

Run:

```bash
python -c "from pathlib import Path; print([(str(p),n+1,l) for p in Path('lucnhan').rglob('*.py') for n,l in enumerate(p.read_text(encoding='utf-8').splitlines()) if 'restore_session' in l])"
```

If only `SessionControllerBase.restore_sessions`, `restore_session`, and `SessionHistory.get_latest_session` remain, remove all three. Keep the rewritten data-integrity tests from Task 5; do not delete them.

- [ ] **Step 5: Run regression suite**

Run:

```bash
python -m pytest tests/tools/test_panel_core.py tests/agent/test_session_controller.py tests/agent/test_state.py -v
```

Expected: PASS; startup has one empty tab and no persistence call.

---

### Task 8: PanelCore Right-Panel Coordinator and History Listing Worker

**Files:**
- Modify: `lucnhan/ui/panel_core.py:184-241, 591-768, 1156-1317, 1652-1659`
- Extend: `tests/tools/test_panel_core.py`

**Interfaces:**
- Consumes: `HistoryPanel`, `HistoryScope`, `SessionHistoryEntry`, controller list API.
- Produces: `_show_right_panel`, `_start_history_list_request`, `_drain_history_results`, `_stop_history_poll_timer`, `_invalidate_history`.
- Consumed by: Tasks 9–10.

- [ ] **Step 1: Extend `_make_panel()` and write failing right-panel tests**

Initialize in the helper:

```python
panel._history_panel = MagicMock()
panel._history_btn = MagicMock()
panel._history_generation = 0
panel._history_executor = None
panel._history_result_queue = queue.Queue(maxsize=2)
panel._history_poll_timer = None
panel._history_pending = False
panel._history_closing = threading.Event()
```

Test transitions:

```python
@pytest.mark.parametrize(
    ("name", "history_visible", "mutation_visible"),
    [("history", True, False), ("mutation", False, True), (None, False, False)],
)
def test_show_right_panel_is_mutually_exclusive(name, history_visible, mutation_visible) -> None:
    panel = _make_history_panel()
    panel._show_right_panel(name)
    panel._history_panel.setVisible.assert_called_with(history_visible)
    panel._mutation_panel.setVisible.assert_called_with(mutation_visible)
    panel._history_btn.setChecked.assert_called_with(history_visible)
    panel._mutations_btn.setChecked.assert_called_with(mutation_visible)
```

Test that opening History while `_ctrl.is_agent_running is False` still creates/starts the separate history QTimer.

- [ ] **Step 2: Run RED panel tests**

Run:

```bash
python -m pytest tests/tools/test_panel_core.py -k "right_panel or history_poll or history_list" -v
```

Expected: FAIL because new fields/helpers do not exist.

- [ ] **Step 3: Wire the History widget and button**

In `_build_main_splitter()`, add `HistoryPanel` as the third hidden widget. In `_build_action_buttons()`, add an always-visible checkable `History` button. Implement:

```python
def _show_right_panel(self, name: Literal["history", "mutation"] | None) -> None:
    self._mutation_panel.setVisible(False)
    self._mutations_btn.setChecked(False)
    self._history_panel.setVisible(False)
    self._history_btn.setChecked(False)
    if name == "history":
        self._history_panel.setVisible(True)
        self._history_btn.setChecked(True)
        self._start_history_list_request()
    elif name == "mutation":
        self._mutation_panel.setVisible(True)
        self._mutations_btn.setChecked(True)
```

Connect close/retry/open signals, and route `_on_toggle_mutation_log()` through this coordinator.

- [ ] **Step 4: Add a dedicated executor and typed result queue**

Create `_history_executor` lazily as:

```python
ThreadPoolExecutor(max_workers=1, thread_name_prefix="lucnhan-history")
```

It must be distinct from `lucnhan.state.history._SAVE_EXECUTOR`. Permit one pending request at a time. List worker behavior:

```python
def _history_list_worker(self, scope: HistoryScope) -> None:
    result: HistoryListResult
    try:
        SessionHistory(self._ctrl.config).flush_saves(timeout=10.0)
        entries = self._ctrl.list_history_sessions(scope)
        result = HistoryListResult(HistoryRequestStatus.LISTED, scope, tuple(entries))
    except TimeoutError:
        result = HistoryListResult(HistoryRequestStatus.SAVE_FLUSH_TIMEOUT, scope)
    except Exception as exc:
        result = HistoryListResult(HistoryRequestStatus.FAILED, scope, error=f"{type(exc).__name__}: {exc}")
    if not self._history_closing.is_set():
        self._history_result_queue.put(result)
```

Add `HistoryListResult` to `history_types.py` before using this skeleton:

```python
@dataclass(frozen=True)
class HistoryListResult:
    status: HistoryRequestStatus
    scope: HistoryScope
    entries: tuple[SessionHistoryEntry, ...] = ()
    error: str = ""
```

- [ ] **Step 5: Implement separate timer lifecycle and drain**

Mirror existing `_stop_poll_timer()`:

```python
def _stop_history_poll_timer(self) -> None:
    timer = self._history_poll_timer
    if timer is None:
        return
    timer.stop()
    try:
        timer.timeout.disconnect(self._drain_history_results)
    except (RuntimeError, TypeError):
        pass
    timer.deleteLater()
    self._history_poll_timer = None
```

The drain is the only method calling `HistoryPanel.set_entries/set_error`; discard any result whose scope generation differs from live `_history_generation`.

- [ ] **Step 6: Run panel tests including deadlock guard**

```bash
python -m pytest tests/tools/test_panel_core.py -k "right_panel or history" -v
```

Expected: PASS. Include a deterministic test asserting `_history_executor is not _SAVE_EXECUTOR` and a queued save + list completes without self-deadlock.

---

### Task 9: Open Historical Sessions, Dedupe, and Deferred Async Restore

**Files:**
- Modify: `lucnhan/ui/panel_core.py:776-878, 987-1023`
- Extend: `tests/tools/test_panel_core.py`
- Extend: `tests/ui/test_chat_view_restore.py`

**Interfaces:**
- Consumes: controller find/load/attach APIs and Task 8 result drain.
- Produces: `_on_history_open_requested`, load worker, `_apply_history_loaded`, tab focus helper, pending restore cleanup.

- [ ] **Step 1: Write failing pre/post-dedupe and open tests**

```python
def test_history_open_focuses_existing_tab_without_worker() -> None:
    panel = _make_history_panel()
    panel._ctrl.find_tab_for_session.return_value = "tab-a"
    panel._on_history_open_requested("persisted-a")
    panel._ctrl.load_history_session.assert_not_called()
    panel._focus_tab.assert_called_once_with("tab-a")


def test_loaded_session_opens_one_tab_and_uses_async_restore() -> None:
    panel = _make_history_panel()
    # Feed a LOADED result through main-thread drain/apply.
    panel._apply_history_loaded(load_result)
    panel._create_tab.assert_called_once()
    panel._restore_messages_if_needed.assert_called_once_with("tab-new")
```

Add stale-scope, NOT_FOUND refresh, WRONG_IDB no-attach, and post-load `ALREADY_OPEN` tests.

- [ ] **Step 2: Run RED tests**

```bash
python -m pytest tests/tools/test_panel_core.py -k "history_open or history_loaded or pending_restore" -v
```

Expected: FAIL because the handlers do not exist.

- [ ] **Step 3: Submit load requests through the same dedicated executor**

Pre-load dedupe on the main thread. Worker calls only `self._ctrl.load_history_session(session_id, scope)` and queues `HistoryLoadResult`; no Qt/signal/tab mutation.

- [ ] **Step 4: Attach and restore only in the main-thread drain**

For `LOADED`, call `attach_history_session`:

- `STALE_SCOPE`: silently drop.
- `ALREADY_OPEN`: focus existing tab.
- `OPENED`: write `_pending_restore_messages[tab_id]`, create/focus tab, call `_restore_messages_if_needed(tab_id)` so `ChatView.restore_from_messages_async()` remains the rendering path.

For `NOT_FOUND`, show `This chat is no longer available.` then start one list refresh. For `WRONG_IDB` and `EMPTY`, show exact non-retryable copy from the spec.

- [ ] **Step 5: Clean pending payload on tab close**

At the start of `_on_close_tab()` after resolving `tab_id`:

```python
self._pending_restore_messages.pop(tab_id, None)
```

Then call `ChatView.shutdown()` as today; its generation counter cancels/drops late restore signals.

- [ ] **Step 6: Run open/restore regressions**

```bash
python -m pytest tests/tools/test_panel_core.py -k "history or close_tab or restore" -v
python -m pytest tests/ui/test_chat_view_restore.py -v
```

Expected: PASS, including close/IDB-switch mid-render safety.

---

### Task 10: IDB Change and Shutdown Invalidation

**Files:**
- Modify: `lucnhan/ui/panel_core.py:1156-1317`
- Extend: `tests/tools/test_panel_core.py`

**Interfaces:**
- Consumes: Task 8 executor/queue/timer/generation.
- Produces: `_invalidate_history(clear_panel: bool) -> None`; safe IDB-change and shutdown ordering.

- [ ] **Step 1: Write failing stale-result and teardown tests**

Test that `on_database_changed()`:

1. increments generation before reset,
2. stops/deletes timer,
3. calls executor `shutdown(wait=False, cancel_futures=True)`,
4. drains queue,
5. clears History and pending restore payloads,
6. recreates one `New Chat`,
7. never restores history.

Test that worker completion after shutdown sees `_history_closing` and does not `queue.put` or touch widgets.

- [ ] **Step 2: Run RED tests**

```bash
python -m pytest tests/tools/test_panel_core.py -k "database_changed or history_shutdown or stale" -v
```

Expected: FAIL because invalidation ordering is not implemented.

- [ ] **Step 3: Implement one idempotent invalidation helper**

```python
def _invalidate_history(self, *, clear_panel: bool) -> None:
    self._history_generation += 1
    self._history_closing.set()
    self._stop_history_poll_timer()
    executor = self._history_executor
    self._history_executor = None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)
    while True:
        try:
            self._history_result_queue.get_nowait()
        except queue.Empty:
            break
    self._history_pending = False
    if clear_panel:
        self._history_panel.clear()
```

Create a fresh closing `Event` and executor lazily on the next History request.

- [ ] **Step 4: Place invalidation at lifecycle boundaries**

- `on_database_changed()`: invalidate before controller identity changes; clear `_pending_restore_messages`; reset/rebuild tabs; no auto-restore.
- `shutdown()`: set `_is_shutdown`, invalidate, disconnect History panel theme/signals via `shutdown()`, then continue existing teardown.
- Hiding History alone does not cancel a load; keep the timer alive until its terminal result is drained/discarded, then stop it.

- [ ] **Step 5: Run lifecycle and full panel tests**

```bash
python -m pytest tests/tools/test_panel_core.py -v
```

Expected: PASS, including existing shutdown idempotency and tab behavior.

---

### Task 11: End-to-End Regression and Documentation

**Files:**
- Create: `tests/integration/test_history_on_demand.py`
- Modify: `CHANGELOG.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: `DEVELOPMENT.md` only where grep finds session restore behavior

**Interfaces:**
- Consumes: all prior tasks.
- Produces: one behavioral regression scenario and updated developer/user guidance.

- [ ] **Step 1: Write the end-to-end regression before final cleanup**

The test uses a temp config/session directory and host/Qt stubs. Persist two IDB-A sessions and one IDB-B session, then assert:

```python
# Behavioral outline
panel_a = make_panel(idb_a)
assert panel_a.tab_count() == 1
assert panel_a.active_session.messages == []

panel_a.open_history()
assert panel_a.visible_history_ids() == {a1.id, a2.id}

panel_a.open_history_session(a1.id)
assert panel_a.tab_count() == 2
panel_a.open_history_session(a1.id)
assert panel_a.tab_count() == 2  # focus, no duplicate

panel_a.on_database_changed(idb_b)
assert panel_a.tab_count() == 1
assert panel_a.active_session.messages == []
panel_a.open_history()
assert panel_a.visible_history_ids() == {b1.id}

panel_restart = make_panel(idb_b)
assert panel_restart.tab_count() == 1
assert panel_restart.active_session.messages == []
```

Use deterministic queue drains/direct slot calls rather than sleeps.

- [ ] **Step 2: Run RED/then GREEN integration test**

Run during final wiring:

```bash
python -m pytest tests/integration/test_history_on_demand.py -v
```

Expected before final fixes: failures pinpoint missing fixture/wiring; after fixes: PASS.

- [ ] **Step 3: Update documentation with exact behavior**

`CHANGELOG.md` Unreleased:

```markdown
### Changed
- Chat history is now on demand. Opening Luc Nhan or switching IDBs starts with one empty `New Chat`; use History to reopen chats for the current IDB.

### Removed
- Removed `startup_restore_sessions`. Older config files may retain the key, but Luc Nhan ignores it and omits it on the next save.
```

Update `AGENTS.md` and `CLAUDE.md` with these invariants:

```text
HistoryPanel owns presentation only. LucNhanPanelCore owns the dedicated
history executor, bounded queue, main-thread poll timer, and generation.
History never auto-restores and never uses _SAVE_EXECUTOR.
```

Search `DEVELOPMENT.md` for `restore_session`, `restore sessions`, and startup tabs; update only matching sections.

- [ ] **Step 4: Run targeted full suite and static checks**

```bash
python -m ruff format lucnhan/ tests/
python -m ruff check lucnhan/ tests/
python -m mypy lucnhan/core lucnhan/providers
python -m pytest tests/state/test_history_on_demand.py tests/core/test_config_history.py tests/agent/test_session_controller.py tests/ui/test_history_panel.py tests/tools/test_panel_core.py tests/integration/test_history_on_demand.py -v
```

Expected: all PASS.

- [ ] **Step 5: Run repository audits**

```bash
python -c "from pathlib import Path; hits=[p for p in Path('lucnhan').rglob('*.py') if 'startup_restore_sessions' in p.read_text(encoding='utf-8')]; assert not hits, hits"
python -c "from pathlib import Path; hits=[(p,n+1,l) for p in Path('lucnhan').rglob('*.py') for n,l in enumerate(p.read_text(encoding='utf-8').splitlines()) if '_try_restore_session' in l]; assert not hits, hits"
```

Expected: no production hits.

- [ ] **Step 6: Run local CI and manual IDA smoke test**

```bash
./ci-local.sh
```

Expected: format, lint, mypy, pytest and desloppify gates pass with no baseline regression.

Manual IDA checklist:

1. Open an IDB with prior sessions → exactly one `New Chat`.
2. Open History while agent is idle → only current-IDB metadata appears.
3. Search by mixed-case title → list filters immediately.
4. Open a 200+ message chat → UI stays responsive and session can continue.
5. Open the same session again → existing tab focuses.
6. Switch IDB during history list/load/restore → no stale rows/messages; exactly one new chat.
7. Close/reopen Luc Nhan → again exactly one new chat; history remains on disk.

---

## Final Self-Review

### Spec coverage

- Fresh startup/IDB switch: Tasks 4, 7, 10, 11.
- Current-IDB metadata-only history: Tasks 3, 5, 6, 8.
- Lazy full payload/open/dedupe/continue: Tasks 5, 9, 11.
- Qt/thread/executor/timer invariants: Tasks 8–10.
- Path traversal/title sanitization/manifest backfill: Tasks 2–3.
- Empty drafts/config migration: Tasks 4–5.
- Error/retry/search/theme/narrow layout: Tasks 6, 8, 10.
- Documentation and verification: Task 11.

No spec requirement lacks an implementation or verification task.

### Placeholder scan

The plan contains no unresolved placeholder or deferred implementation step. Compact behavioral examples are paired with exact interfaces, assertions, commands, and expected outcomes in the same task.

### Type consistency

- Persistent identity: `SessionState.id` / `SessionHistoryEntry.session_id`.
- Ephemeral identity: controller dict key / `HistoryAttachResult.tab_id`.
- Worker load: `HistoryLoadResult`.
- Main-thread attach: `HistoryAttachResult`.
- List worker: `HistoryListResult` added in Task 8 to the Task 1 module.
- All cross-thread result DTOs are frozen and carry `HistoryScope`; list payloads are immutable tuples, while `HistoryLoadResult.session` is a detached `SessionState` that remains worker-owned until main-thread attach and is never mutated concurrently.

No code is committed by this plan unless the user separately authorizes commits.
