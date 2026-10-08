# Chat History Deletion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Thêm hard delete có xác nhận cho từng chat đã lưu trong History của IDB hiện tại, đồng thời ngăn autosave hoặc history load làm chat bị xóa xuất hiện lại.

**Architecture:** `HistoryPanel` chỉ phát intent và render trạng thái; `LucNhanPanelCore` sở hữu confirmation, deletion intents, single-flight executor, watchdog, queue và Qt-main-thread apply; `SessionControllerBase` ánh xạ persistence outcome sang typed UI result; `SessionHistory` serialize delete với `_SAVE_EXECUTOR` hiện có. Primary session JSON là source of truth; sidecar và manifest là cleanup/recoverable metadata.

**Tech Stack:** Python 3.11 target (tương thích IDA Pro ≥ 9.0; Python 3.10 là runtime IDA an toàn nhất), PySide6 qua `lucnhan.ui.qt_compat`, frozen dataclasses/Enum, `concurrent.futures.ThreadPoolExecutor`, `queue.Queue`, `threading.Event`, pytest/unittest, ruff, mypy.

## Global Constraints

- Đọc `AGENTS.md` trước khi sửa code; mọi module Python mới/sửa vẫn bắt đầu bằng `from __future__ import annotations`.
- Không import `ida_*` hoặc `PySide6` trực tiếp; UI chỉ import Qt qua `lucnhan.ui.qt_compat`.
- `HistoryPanel`/`HistoryRowWidget` tuyệt đối không import persistence, config, threading, executor hoặc thực hiện I/O.
- Worker không chạm Qt; mọi mutation widget/tab/intent set chạy trên Qt main thread qua result queue + `QTimer`.
- `_history_executor` và `_SAVE_EXECUTOR` phải là hai executor khác nhau; delete filesystem được enqueue vào `_SAVE_EXECUTOR` để có FIFO với autosave.
- Không dùng `flush_saves() -> delete`; khoảng trống giữa hai lệnh có thể cho save mới chen vào.
- Session ID hợp lệ khớp `^[A-Za-z0-9_-]{1,32}$` và phải qua `_session_path()` trước I/O.
- Delete chỉ Current IDB; validate persisted `id`, normalized `idb_path`, và `db_instance_id` ngay trước mutation.
- Chat đang mở ở bất kỳ tab nào không được xóa; focus tab và yêu cầu đóng trước.
- Không bulk delete, trash/undo/archive, cross-IDB UI, headless/control API, MCP hay LLM tool.
- Title/error render plain text; raw path, transcript và OS exception chỉ vào log.
- Test theo RED → GREEN → IMPROVE; không thêm dependency mới.
- Chỉ commit khi người dùng đã cho phép; các bước commit dưới đây là checkpoint đề xuất, không phải quyền tự động commit.

## File Structure

### Production

- `lucnhan/constants.py` — slow-delete watchdog constant.
- `lucnhan/state/history_types.py` — frozen UI-facing delete result/status contracts.
- `lucnhan/state/history.py` — internal persistence outcome, ordered async delete, sidecar/manifest cleanup; loại public synchronous delete unsafe.
- `lucnhan/ui/session_controller_base.py` — Qt-free delete API mapping persistence result to UI result.
- `lucnhan/ui/history_panel.py` — passive delete affordance, notices, pending state, cached-row removal.
- `lucnhan/ui/theme/widgets_common.py` — token-driven History delete button style.
- `lucnhan/ui/styles.py` — re-export style getter.
- `lucnhan/ui/panel_core.py` — confirmation, intent race gate, worker/drain/apply/retry/watchdog/lifecycle.
- `CHANGELOG.md` — Unreleased feature note.

### Tests

- `tests/state/test_history_on_demand.py` — DTO and persistence hard-delete matrix.
- `tests/agent/test_state.py` — remove legacy synchronous-delete expectations.
- `tests/agent/test_session_controller.py` — scope validation and persistence-to-UI result mapping.
- `tests/ui/test_history_panel.py` — passive widget behavior, signals, notices, pending state, cache/search pintent.
- `tests/tools/test_panel_core.py` — coordinator, confirmation, intents, worker/drain/watchdog/retry/lifecycle.
- `tests/integration/test_history_on_demand.py` — real persistence/controller/coordinator flow across two IDBs.
- `tests/qt_stubs.py` — only extend if the new deterministic widget tests need stored tooltip/accessibility/enabled/focus values unavailable in the current stubs.

---

### Task 1: Freeze Delete Contracts and Watchdog Constant

**Files:**
- Modify: `lucnhan/constants.py:51-57`
- Modify: `lucnhan/state/history_types.py:60-129`
- Test: `tests/state/test_history_on_demand.py:40-54`

**Interfaces:**
- Consumes: existing `HistoryScope`.
- Produces: `HISTORY_DELETE_SLOW_NOTICE_SECONDS: float`, `HistoryDeleteStatus`, `HistoryDeleteResult`.

- [ ] **Step 1: Write failing contract tests**

Add imports and focused tests to `tests/state/test_history_on_demand.py`:

```python
from lucnhan.constants import HISTORY_DELETE_SLOW_NOTICE_SECONDS
from lucnhan.state.history_types import (
    HistoryDeleteResult,
    HistoryDeleteStatus,
)


def test_history_delete_contract_is_frozen_and_stable() -> None:
    scope = HistoryScope("C:/sample.i64", "a" * 32, 7)
    result = HistoryDeleteResult(
        status=HistoryDeleteStatus.DELETED,
        scope=scope,
        session_id="abc123",
    )

    assert HISTORY_DELETE_SLOW_NOTICE_SECONDS == 30.0
    assert [status.value for status in HistoryDeleteStatus] == [
        "deleted",
        "not_found",
        "wrong_idb",
        "failed",
    ]
    assert result.error == ""
    with pytest.raises(FrozenInstanceError):
        result.session_id = "changed"  # type: ignore[misc]
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
python -m pytest tests/state/test_history_on_demand.py::test_history_delete_contract_is_frozen_and_stable -v
```

Expected: collection fails because the constant/types do not exist.

- [ ] **Step 3: Add the constant and frozen contracts**

In `lucnhan/constants.py`:

```python
HISTORY_TITLE_MAX_CHARS = 80
HISTORY_DELETE_SLOW_NOTICE_SECONDS = 30.0
```

In `lucnhan/state/history_types.py`, after `HistoryAttachStatus`:

```python
class HistoryDeleteStatus(str, Enum):
    """Terminal outcome of a persisted-history deletion request."""

    DELETED = "deleted"
    NOT_FOUND = "not_found"
    WRONG_IDB = "wrong_idb"
    FAILED = "failed"


@dataclass(frozen=True)
class HistoryDeleteResult:
    """Typed delete result crossing the history worker -> Qt queue boundary."""

    status: HistoryDeleteStatus
    scope: HistoryScope
    session_id: str
    error: str = ""
```

- [ ] **Step 4: Run focused contracts**

```bash
python -m pytest tests/state/test_history_on_demand.py -v -k "history_delete_contract or history_result_status_contracts"
```

Expected: PASS.

- [ ] **Step 5: Run formatting and checkpoint**

```bash
python -m ruff format lucnhan/constants.py lucnhan/state/history_types.py tests/state/test_history_on_demand.py
python -m ruff check lucnhan/constants.py lucnhan/state/history_types.py tests/state/test_history_on_demand.py
```

If commits are authorized:

```bash
git add lucnhan/constants.py lucnhan/state/history_types.py tests/state/test_history_on_demand.py
git commit -m "feat(history): define chat deletion contracts"
```

---

### Task 2: Implement Ordered Persistence Deletion

**Files:**
- Modify: `lucnhan/state/history.py:50-82, 235-357, 545-572, 819-827`
- Modify: `tests/state/test_history_on_demand.py:103-380`
- Modify: `tests/agent/test_state.py:251-320`

**Interfaces:**
- Consumes: `_SAVE_EXECUTOR`, `_manifest_lock`, `_session_path()`, `_matches_current_idb()`, `_SUMMARY_SUFFIX`.
- Produces: `SessionDeleteStatus`, frozen `SessionDeleteOutcome`, `SessionHistory.delete_session_async(session_id, expected_idb_path, expected_db_instance_id) -> Future[SessionDeleteOutcome]`.

- [ ] **Step 1: Audit current callers and FIFO invariant before removing unsafe sync API**

Run:

```bash
rg -n "\bdelete_session\(" lucnhan tests
rg -n "_SAVE_EXECUTOR = ThreadPoolExecutor\(max_workers=1" lucnhan/state/history.py
```

Expected baseline: production definition in `lucnhan/state/history.py`, legacy happy/nonexistent tests in `tests/agent/test_state.py`, and the invalid-ID assertion in `tests/state/test_history_on_demand.py`; no other production caller. The second command must find exactly the single-worker executor declaration because FIFO save→delete correctness depends on `max_workers=1`. Migrate all three test call sites in this task before removing the public sync method.

- [ ] **Step 2: Write failing happy-path, sidecar, and idempotency tests**

Add helpers/tests to `tests/state/test_history_on_demand.py`:

```python
from lucnhan.state.history import (
    MANIFEST_FILE,
    SessionDeleteStatus,
)


def _wait_delete(
    history: SessionHistory,
    session_id: str,
    idb_path: str,
    db_instance_id: str,
):
    return history.delete_session_async(
        session_id,
        expected_idb_path=idb_path,
        expected_db_instance_id=db_instance_id,
    ).result(timeout=5)


def test_ordered_delete_removes_primary_sidecar_and_manifest(tmp_path: Path) -> None:
    history = _history(tmp_path)
    session = SessionState(
        id="delete-me",
        idb_path=str(tmp_path / "sample.i64"),
        db_instance_id="a" * 32,
    )
    session.add_message(Message(role=Role.USER, content="Delete me"))
    primary = Path(history.save_session(session))
    sidecar = primary.with_name(f"{session.id}.summary.json")
    sidecar.write_text('{"messages":1}', encoding="utf-8")

    outcome = _wait_delete(history, session.id, session.idb_path, session.db_instance_id)

    assert outcome.status is SessionDeleteStatus.DELETED
    assert not primary.exists()
    assert not sidecar.exists()
    manifest = json.loads((primary.parent / MANIFEST_FILE).read_text(encoding="utf-8"))
    assert session.id not in manifest["entries"]


def test_ordered_delete_missing_primary_cleans_stale_metadata(tmp_path: Path) -> None:
    history = _history(tmp_path)
    session = SessionState(
        id="already-gone",
        idb_path=str(tmp_path / "sample.i64"),
        db_instance_id="b" * 32,
    )
    session.add_message(Message(role=Role.USER, content="Delete me"))
    primary = Path(history.save_session(session))
    sidecar = primary.with_name(f"{session.id}.summary.json")
    sidecar.write_text('{"messages":1}', encoding="utf-8")
    primary.unlink()

    outcome = _wait_delete(history, session.id, session.idb_path, session.db_instance_id)

    assert outcome.status is SessionDeleteStatus.NOT_FOUND
    assert not sidecar.exists()
    manifest = json.loads((primary.parent / MANIFEST_FILE).read_text(encoding="utf-8"))
    assert session.id not in manifest["entries"]
```

- [ ] **Step 3: Run RED persistence tests**

```bash
python -m pytest tests/state/test_history_on_demand.py -v -k "ordered_delete"
```

Expected: import/attribute failures for new persistence contracts/API.

- [ ] **Step 4: Add internal persistence outcome and ordered public API**

In `lucnhan/state/history.py`:

```python
from dataclasses import dataclass
from enum import Enum


class SessionDeleteStatus(str, Enum):
    DELETED = "deleted"
    NOT_FOUND = "not_found"
    WRONG_IDB = "wrong_idb"
    FAILED = "failed"


@dataclass(frozen=True)
class SessionDeleteOutcome:
    status: SessionDeleteStatus
    error: str = ""
```

Replace the public synchronous `delete_session()` with:

```python
def delete_session_async(
    self,
    session_id: str,
    *,
    expected_idb_path: str,
    expected_db_instance_id: str,
) -> Future[SessionDeleteOutcome]:
    """Queue one hard delete behind all earlier autosaves."""
    return _SAVE_EXECUTOR.submit(
        self._delete_session_ordered,
        session_id,
        expected_idb_path,
        expected_db_instance_id,
    )
```

Use a private worker, never call it directly outside tests that specifically verify internals:

```python
def _delete_session_ordered(
    self,
    session_id: str,
    expected_idb_path: str,
    expected_db_instance_id: str,
) -> SessionDeleteOutcome:
    path = self._session_path(session_id)
    if path is None:
        return SessionDeleteOutcome(SessionDeleteStatus.NOT_FOUND)

    sidecar_path = os.path.join(self._dir, f"{session_id}{_SUMMARY_SUFFIX}")
    with self._manifest_lock:
        data = self._read_manifest()
        entries = dict(data.get("entries", {}))
        last_full_scan = float(data.get("last_full_scan", 0.0) or 0.0)

        if not os.path.exists(path):
            self._remove_file_if_present(sidecar_path, session_id=session_id)
            entries.pop(session_id, None)
            try:
                self._write_manifest(entries, last_full_scan=last_full_scan)
            except OSError as exc:
                log_warning(
                    f"Session {session_id} was already absent, but manifest cleanup failed: {exc}"
                )
            return SessionDeleteOutcome(SessionDeleteStatus.NOT_FOUND)

        with open(path, encoding="utf-8") as handle:
            persisted = json.load(handle)
        if persisted.get("id") != session_id or not _matches_current_idb(
            entry_idb_path=persisted.get("idb_path", ""),
            entry_db_instance_id=persisted.get("db_instance_id", ""),
            target_idb_path=expected_idb_path,
            target_db_instance_id=expected_db_instance_id,
        ):
            return SessionDeleteOutcome(SessionDeleteStatus.WRONG_IDB)

        try:
            os.remove(path)
        except FileNotFoundError:
            primary_status = SessionDeleteStatus.NOT_FOUND
        except OSError as exc:
            return SessionDeleteOutcome(SessionDeleteStatus.FAILED, str(exc))
        else:
            primary_status = SessionDeleteStatus.DELETED

        self._remove_file_if_present(sidecar_path, session_id=session_id)
        entries.pop(session_id, None)
        try:
            self._write_manifest(entries, last_full_scan=last_full_scan)
        except OSError as exc:
            log_warning(f"Deleted session {session_id}, but manifest cleanup failed: {exc}")
        return SessionDeleteOutcome(primary_status)
```

Add a small best-effort helper:

```python
@staticmethod
def _remove_file_if_present(path: str, *, session_id: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        log_warning(
            f"Failed to clean up companion for session {session_id}: "
            f"{type(exc).__name__}"
        )
```

**Implementation correction:** `_write_manifest()` logs and re-raises, so the missing-primary path must also wrap manifest write and still return `NOT_FOUND`; do not let cleanup convert desired absence into a false primary failure.

- [ ] **Step 5: Add wrong-IDB, identity, and primary failure tests**

```python
@pytest.mark.parametrize(
    ("expected_path", "expected_instance"),
    [
        ("C:/other.i64", "a" * 32),
        ("C:/sample.i64", "b" * 32),
    ],
)
def test_ordered_delete_rejects_wrong_idb_without_mutation(
    tmp_path: Path,
    expected_path: str,
    expected_instance: str,
) -> None:
    history = _history(tmp_path)
    session = SessionState(
        id="keep-me",
        idb_path="C:/sample.i64",
        db_instance_id="a" * 32,
    )
    session.add_message(Message(role=Role.USER, content="Keep me"))
    primary = Path(history.save_session(session))

    outcome = _wait_delete(history, session.id, expected_path, expected_instance)

    assert outcome.status is SessionDeleteStatus.WRONG_IDB
    assert primary.exists()


def test_ordered_delete_rejects_filename_payload_id_mismatch(tmp_path: Path) -> None:
    history = _history(tmp_path)
    session = SessionState(
        id="filename-id",
        idb_path="C:/sample.i64",
        db_instance_id="a" * 32,
    )
    session.add_message(Message(role=Role.USER, content="Keep me"))
    primary = Path(history.save_session(session))
    payload = json.loads(primary.read_text(encoding="utf-8"))
    payload["id"] = "different-id"
    primary.write_text(json.dumps(payload), encoding="utf-8")

    outcome = _wait_delete(history, session.id, session.idb_path, session.db_instance_id)

    assert outcome.status is SessionDeleteStatus.WRONG_IDB
    assert primary.exists()


def test_ordered_delete_primary_remove_failure_keeps_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = _history(tmp_path)
    session = SessionState(
        id="locked",
        idb_path="C:/sample.i64",
        db_instance_id="a" * 32,
    )
    session.add_message(Message(role=Role.USER, content="Keep me"))
    primary = Path(history.save_session(session))
    original_remove = os.remove

    def fail_primary(path: str) -> None:
        if os.path.normcase(path) == os.path.normcase(str(primary)):
            raise PermissionError("locked")
        original_remove(path)

    monkeypatch.setattr(os, "remove", fail_primary)
    outcome = _wait_delete(history, session.id, session.idb_path, session.db_instance_id)

    assert outcome.status is SessionDeleteStatus.FAILED
    assert primary.exists()
    manifest = json.loads((primary.parent / MANIFEST_FILE).read_text(encoding="utf-8"))
    assert session.id in manifest["entries"]
```

- [ ] **Step 6: Add FIFO resurrection regression**

Use executor FIFO and bounded `Future.result()` calls instead of sleeps:

```python
def test_delete_runs_after_queued_save_and_wins_final_state(tmp_path: Path) -> None:
    history = _history(tmp_path)
    session = SessionState(
        id="ordered",
        idb_path="C:/sample.i64",
        db_instance_id="a" * 32,
    )
    session.add_message(Message(role=Role.USER, content="Original"))

    save_future = history.save_session_async(session)
    delete_future = history.delete_session_async(
        session.id,
        expected_idb_path=session.idb_path,
        expected_db_instance_id=session.db_instance_id,
    )

    assert Path(save_future.result(timeout=5)).name == f"{session.id}.json"
    assert delete_future.result(timeout=5).status is SessionDeleteStatus.DELETED
    SessionHistory.flush_saves(timeout=5)
    assert history.load_session(session.id) is None
    assert all(row["id"] != session.id for row in history.list_sessions())
```

- [ ] **Step 7: Cover recoverable cleanup failures**

Add tests that pin the "primary file is truth" contract:

```python
def test_manifest_cleanup_failure_after_primary_delete_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = _history(tmp_path)
    session = SessionState(
        id="manifest-stale",
        idb_path="C:/sample.i64",
        db_instance_id="a" * 32,
    )
    session.add_message(Message(role=Role.USER, content="Delete me"))
    primary = Path(history.save_session(session))
    original_write = history._write_manifest
    calls = 0

    def fail_once(entries, last_full_scan=0.0):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("manifest locked")
        return original_write(entries, last_full_scan)

    monkeypatch.setattr(history, "_write_manifest", fail_once)
    outcome = _wait_delete(history, session.id, session.idb_path, session.db_instance_id)

    assert outcome.status is SessionDeleteStatus.DELETED
    assert not primary.exists()
    rows = history.list_sessions(
        idb_path=session.idb_path,
        db_instance_id=session.db_instance_id,
    )
    assert all(row["id"] != session.id for row in rows)
```

Also patch sidecar `os.remove` only and assert primary deletion still returns `DELETED`; the warning must include session ID + exception type, not raw path. These tests prove list/rebuild reconciliation removes phantom manifest rows after successful primary deletion.

- [ ] **Step 8: Replace every legacy sync-delete test**

In `tests/agent/test_state.py`, remove `test_delete_session`/`test_delete_nonexistent` calls to the deleted public sync API. Keep one compatibility guard:

```python
def test_history_has_no_public_synchronous_delete(self):
    history = SessionHistory(self.config)
    self.assertFalse(hasattr(history, "delete_session"))
```

In `tests/state/test_history_on_demand.py::test_invalid_session_ids_do_not_touch_storage`, replace the sync assertion with the new async boundary and a valid expected scope:

```python
outcome = history.delete_session_async(
    session_id,
    expected_idb_path="C:/sample.i64",
    expected_db_instance_id="a" * 32,
).result(timeout=5)
assert outcome.status is SessionDeleteStatus.NOT_FOUND
assert list(sessions_dir.iterdir()) == before
```

This preserves the original "invalid ID causes no I/O" assertion while exercising the production API.

- [ ] **Step 9: Run persistence suites and audit callers**

```bash
python -m pytest tests/state/test_history_on_demand.py tests/agent/test_state.py -v
rg -n "\bdelete_session\(" lucnhan tests
```

Expected: tests PASS; grep returns no public synchronous call.

- [ ] **Step 10: Format, lint, and checkpoint**

```bash
python -m ruff format lucnhan/state/history.py tests/state/test_history_on_demand.py tests/agent/test_state.py
python -m ruff check lucnhan/state/history.py tests/state/test_history_on_demand.py tests/agent/test_state.py
```

If authorized:

```bash
git add lucnhan/state/history.py tests/state/test_history_on_demand.py tests/agent/test_state.py
git commit -m "feat(history): serialize persisted chat deletion"
```

---

### Task 3: Add the Qt-Free Controller Delete Boundary

**Files:**
- Modify: `lucnhan/ui/session_controller_base.py:15-33, 669-813`
- Test: `tests/agent/test_session_controller.py:216-329`

**Interfaces:**
- Consumes: `SessionHistory.delete_session_async(...)`, `SessionDeleteStatus`, `HistoryDeleteStatus`, `HistoryDeleteResult`.
- Produces: `SessionControllerBase.delete_history_session(session_id: str, scope: HistoryScope) -> HistoryDeleteResult`.

- [ ] **Step 1: Write failing controller mapping tests**

Add to `tests/agent/test_session_controller.py`:

```python
def test_delete_history_session_maps_deleted_outcome(self) -> None:
    saved_id = self._save_history_session(self.ctrl._db_instance_id)
    scope = self.ctrl.capture_history_scope(generation=9)

    result = self.ctrl.delete_history_session(saved_id, scope)

    self.assertIs(result.status, HistoryDeleteStatus.DELETED)
    self.assertEqual(result.scope, scope)
    self.assertEqual(result.session_id, saved_id)


def test_delete_history_session_rejects_stale_live_scope(self) -> None:
    scope = self.ctrl.capture_history_scope(generation=3)
    self.ctrl._db_instance_id = "f" * 32

    with patch("lucnhan.state.history.SessionHistory.delete_session_async") as delete_async:
        result = self.ctrl.delete_history_session("saved-history", scope)

    self.assertIs(result.status, HistoryDeleteStatus.WRONG_IDB)
    delete_async.assert_not_called()
```

- [ ] **Step 2: Run RED controller tests**

```bash
python -m pytest tests/agent/test_session_controller.py -v -k "delete_history_session"
```

Expected: method/import failures.

- [ ] **Step 3: Implement scope validation and outcome mapping**

Use lazy imports to preserve panel construction performance:

```python
def delete_history_session(
    self,
    session_id: str,
    scope: HistoryScope,
) -> HistoryDeleteResult:
    """Delete one persisted current-IDB chat on the history worker thread."""
    if scope.idb_path != self._idb_path or scope.db_instance_id != self._db_instance_id:
        return HistoryDeleteResult(
            HistoryDeleteStatus.WRONG_IDB,
            scope,
            session_id,
        )

    from ..state.history import SessionDeleteStatus

    history = SessionHistory(self.config)
    try:
        outcome = history.delete_session_async(
            session_id,
            expected_idb_path=scope.idb_path,
            expected_db_instance_id=scope.db_instance_id,
        ).result()
    except FileNotFoundError:
        status = HistoryDeleteStatus.NOT_FOUND
        error = ""
    except (OSError, ValueError, KeyError) as exc:
        status = HistoryDeleteStatus.FAILED
        error = f"{type(exc).__name__}: {exc}"
    else:
        status = {
            SessionDeleteStatus.DELETED: HistoryDeleteStatus.DELETED,
            SessionDeleteStatus.NOT_FOUND: HistoryDeleteStatus.NOT_FOUND,
            SessionDeleteStatus.WRONG_IDB: HistoryDeleteStatus.WRONG_IDB,
            SessionDeleteStatus.FAILED: HistoryDeleteStatus.FAILED,
        }[outcome.status]
        error = outcome.error
    return HistoryDeleteResult(status, scope, session_id, error=error)
```

`json.JSONDecodeError` is already a `ValueError`; keep the catch set minimal and explicit in the docstring/comment. Do not catch `CancelledError`, `KeyboardInterrupt`, or `SystemExit` here.

- [ ] **Step 4: Test expected failure translation**

```python
def test_delete_history_session_translates_persistence_failure(self) -> None:
    scope = self.ctrl.capture_history_scope(generation=4)
    failed = Future()
    failed.set_exception(PermissionError("locked path"))

    with patch(
        "lucnhan.state.history.SessionHistory.delete_session_async",
        return_value=failed,
    ):
        result = self.ctrl.delete_history_session("saved-history", scope)

    self.assertIs(result.status, HistoryDeleteStatus.FAILED)
    self.assertIn("PermissionError", result.error)
```

Also add a `CancelledError` regression:

```python
def test_delete_history_session_does_not_swallow_cancelled_error(self) -> None:
    scope = self.ctrl.capture_history_scope(generation=4)
    cancelled = Future()
    cancelled.cancel()

    with patch(
        "lucnhan.state.history.SessionHistory.delete_session_async",
        return_value=cancelled,
    ):
        with self.assertRaises(CancelledError):
            self.ctrl.delete_history_session("saved-history", scope)
```

- [ ] **Step 5: Run controller and nearby history tests**

```bash
python -m pytest tests/agent/test_session_controller.py -v
python -m pytest tests/state/test_history_on_demand.py -v
```

Expected: PASS.

- [ ] **Step 6: Format, lint, and checkpoint**

```bash
python -m ruff format lucnhan/ui/session_controller_base.py tests/agent/test_session_controller.py
python -m ruff check lucnhan/ui/session_controller_base.py tests/agent/test_session_controller.py
```

If authorized:

```bash
git add lucnhan/ui/session_controller_base.py tests/agent/test_session_controller.py
git commit -m "feat(history): add chat deletion controller boundary"
```

---

### Task 4: Add the Passive Delete Affordance and Notice API

**Files:**
- Modify: `lucnhan/ui/history_panel.py:54-67, 126-201, 216-542`
- Modify: `lucnhan/ui/theme/widgets_common.py:251-304, 422-454`
- Modify: `lucnhan/ui/styles.py:108-138`
- Modify: `tests/qt_stubs.py:19-415`
- Test: `tests/ui/test_history_panel.py:55-500`

**Interfaces:**
- Consumes: `SessionHistoryEntry`, ThemeTokens.
- Produces: `session_delete_requested(str, str)`, `notice_dismissed()`, `remove_entry()`, `set_operation_pending()`, `show_notice()`, `clear_notice()`, `get_history_delete_btn_style()`.

- [ ] **Step 1: Write failing row intent/accessibility tests**

Add `HistoryRowWidget` to test imports and create focused tests:

```python
from lucnhan.ui.history_panel import HistoryRowWidget


class TestDeleteAffordance(unittest.TestCase):
    def test_delete_emits_id_and_title_without_opening_row(self) -> None:
        row = HistoryRowWidget(_entry("abc123", "Analyze parser"))
        deleted: list[tuple[str, str]] = []
        opened: list[str] = []
        row.session_delete_requested.connect(
            lambda session_id, title: deleted.append((session_id, title))
        )
        row.session_open_requested.connect(opened.append)

        row._delete_btn.clicked.emit()

        self.assertEqual(deleted, [("abc123", "Analyze parser")])
        self.assertEqual(opened, [])

    def test_delete_button_has_accessible_copy(self) -> None:
        row = HistoryRowWidget(_entry("abc123", "Analyze parser"))
        self.assertEqual(row._delete_btn.toolTip(), "Delete chat")
        self.assertEqual(
            row._delete_btn.accessibleName(),
            "Delete chat: Analyze parser",
        )
```

The current Qt stub discards tooltip/accessibility/enabled state and does not expose a stateful scroll bar. Extend `_qt_class()` in `tests/qt_stubs.py` with deterministic setters/getters rather than weakening the tests:

```python
"setToolTip": lambda self, value: setattr(self, "_tooltip", value),
"toolTip": lambda self: getattr(self, "_tooltip", ""),
"setAccessibleName": lambda self, value: setattr(self, "_accessible_name", value),
"accessibleName": lambda self: getattr(self, "_accessible_name", ""),
"setEnabled": lambda self, value: setattr(self, "_enabled", bool(value)),
"isEnabled": lambda self: getattr(self, "_enabled", True),
```

Add a minimal `_ScrollBar` stub with `value()`, `setValue()`, `maximum()`, and `setMaximum()`; make `QScrollArea.verticalScrollBar()` return one per instance. This lets `remove_entry()` tests set a non-zero value and verify it is restored/clamped after rerender.

- [ ] **Step 2: Run RED widget intent tests**

```bash
python -m pytest tests/ui/test_history_panel.py -v -k "DeleteAffordance"
```

Expected: missing class signal/button/style API.

- [ ] **Step 3: Add token-driven delete button style**

In `lucnhan/ui/theme/widgets_common.py`:

```python
def _history_delete_btn_style() -> str:
    t = _tokens()
    return (
        "QPushButton#history_delete_btn {"
        f"color: {t.muted_text}; background: transparent; border: 1px solid transparent;"
        "padding: 2px 4px; border-radius: 3px;"
        "}"
        "QPushButton#history_delete_btn:hover {"
        f"color: {t.error}; background: {t.alt_base}; border-color: {t.error};"
        "}"
        "QPushButton#history_delete_btn:focus {"
        f"color: {t.error}; border-color: {t.accent};"
        "}"
        "QPushButton#history_delete_btn:disabled {"
        f"color: {t.muted_text}; background: transparent; border-color: transparent;"
        "}"
    )


def get_history_delete_btn_style() -> str:
    return _history_delete_btn_style()
```

Re-export it from `lucnhan/ui/styles.py`.

- [ ] **Step 4: Add row signal/button without event bubbling**

In `HistoryRowWidget`:

```python
session_delete_requested = Signal(str, str)

# after text_col is added:
self._delete_btn = QPushButton("×")
self._delete_btn.setObjectName("history_delete_btn")
self._delete_btn.setToolTip("Delete chat")
self._delete_btn.setAccessibleName(f"Delete chat: {entry.title}")
self._delete_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
self._delete_btn.setStyleSheet(get_history_delete_btn_style())
self._delete_btn.clicked.connect(
    lambda: self.session_delete_requested.emit(entry.session_id, entry.title)
)
layout.addWidget(self._delete_btn)
```

A child button consumes its own mouse release; row `mouseReleaseEvent()` therefore must not be called by the button signal. Keep row open behavior unchanged for clicks on the row body. Add `set_operation_enabled(enabled: bool)` to control both body-open and delete behavior:

```python
def set_operation_enabled(self, enabled: bool) -> None:
    self._operations_enabled = enabled
    self._delete_btn.setEnabled(enabled)


def mouseReleaseEvent(self, event) -> None:
    if not self._operations_enabled:
        return
    self.session_open_requested.emit(self._entry.session_id)
```

Initialize `_operations_enabled = True`.

- [ ] **Step 5: Add failing panel cache/pending/notice tests**

```python
class TestDeletePanelState(unittest.TestCase):
    def test_panel_forwards_delete_intent(self) -> None:
        panel = HistoryPanel()
        panel.set_entries([_entry("abc123", "Analyze parser")])
        deleted: list[tuple[str, str]] = []
        panel.session_delete_requested.connect(
            lambda session_id, title: deleted.append((session_id, title))
        )

        panel._row_widgets[0]._delete_btn.clicked.emit()

        self.assertEqual(deleted, [("abc123", "Analyze parser")])

    def test_remove_entry_preserves_query_and_remaining_rows(self) -> None:
        panel = HistoryPanel()
        panel.set_entries([
            _entry("a", "Analyze parser"),
            _entry("b", "Analyze TLS"),
        ])
        panel._search.setText("analyze")

        panel.remove_entry("a")

        self.assertEqual(panel._search.text(), "analyze")
        self.assertEqual(panel.visible_session_ids(), ["b"])

    def test_pending_disables_row_operations_but_not_search(self) -> None:
        panel = HistoryPanel()
        panel.set_entries([_entry("a", "Analyze parser")])

        panel.set_operation_pending("a")

        self.assertFalse(panel._row_widgets[0]._delete_btn.isEnabled())
        self.assertTrue(panel._search.isEnabled())
        panel.set_operation_pending(None)
        self.assertTrue(panel._row_widgets[0]._delete_btn.isEnabled())

    def test_notice_preserves_cached_rows_and_can_be_dismissed(self) -> None:
        panel = HistoryPanel()
        panel.set_entries([_entry("a", "Analyze parser")])
        dismissed: list[bool] = []
        panel.notice_dismissed.connect(lambda: dismissed.append(True))

        panel.show_notice("Could not delete this chat.", retry_visible=True)

        self.assertEqual(panel.visible_session_ids(), ["a"])
        self.assertEqual(panel._notice_label.text(), "Could not delete this chat.")
        self.assertTrue(panel._notice_frame.isVisible())
        self.assertTrue(panel._notice_retry_btn.isVisible())
        panel._notice_dismiss_btn.clicked.emit()
        self.assertEqual(dismissed, [True])
        self.assertEqual(panel.visible_session_ids(), ["a"])
        self.assertFalse(panel._notice_frame.isVisible())
```

- [ ] **Step 6: Implement passive panel methods**

Track rows and pending state:

```python
session_delete_requested = Signal(str, str)
notice_dismissed = Signal()

self._row_widgets: list[HistoryRowWidget] = []
self._pending_session_id: str | None = None
```

In `_render_rows()`:

```python
self._row_widgets = []
for entry in entries:
    row = HistoryRowWidget(entry)
    row.session_open_requested.connect(self.session_open_requested.emit)
    row.session_delete_requested.connect(self.session_delete_requested.emit)
    row.set_operation_enabled(self._pending_session_id is None)
    self._row_widgets.append(row)
    self._list_layout.insertWidget(self._list_layout.count() - 1, row)
```

Add methods:

```python
def remove_entry(self, session_id: str) -> None:
    scrollbar = self._list_scroll.verticalScrollBar()
    old_scroll = scrollbar.value()
    self._entries = tuple(
        entry for entry in self._entries if entry.session_id != session_id
    )
    self._apply_search()
    scrollbar.setValue(min(old_scroll, scrollbar.maximum()))


def set_operation_pending(self, session_id: str | None) -> None:
    self._pending_session_id = session_id
    for row in self._row_widgets:
        row.set_operation_enabled(session_id is None)


def show_notice(
    self,
    message: str,
    *,
    retry_visible: bool = False,
    dismiss_visible: bool = True,
) -> None:
    self._notice_label.setText(message)
    self._notice_retry_btn.setVisible(retry_visible)
    self._notice_dismiss_btn.setVisible(dismiss_visible)
    self._notice_frame.setVisible(True)


def clear_notice(self) -> None:
    self._notice_frame.setVisible(False)
    self._notice_retry_btn.setVisible(False)
    self._notice_dismiss_btn.setVisible(False)
```

Create `_notice_frame` as a compact row between Search and `_stack`; it contains a plain-text word-wrapped `_notice_label`, `_notice_retry_btn`, and `_notice_dismiss_btn`. Connect `_notice_retry_btn` to `retry_requested`. Connect `_notice_dismiss_btn` to a private slot that runs `clear_notice()` and then emits `notice_dismissed`. Ensure `set_entries()`, `clear()`, and terminal-success paths clear stale notices without emitting dismissal. Do not reuse `_status_frame`: it would hide cached rows when `_stack` switches away from the list. Do not add an overlay or file I/O.

- [ ] **Step 7: Run full widget suite and passive-import guard**

```bash
python -m pytest tests/ui/test_history_panel.py -v
```

Expected: all existing search/plain-text/overflow/theme/passivity tests and new deletion tests PASS.

- [ ] **Step 8: Format, lint, and checkpoint**

```bash
python -m ruff format lucnhan/ui/history_panel.py lucnhan/ui/theme/widgets_common.py lucnhan/ui/styles.py tests/ui/test_history_panel.py tests/qt_stubs.py
python -m ruff check lucnhan/ui/history_panel.py lucnhan/ui/theme/widgets_common.py lucnhan/ui/styles.py tests/ui/test_history_panel.py tests/qt_stubs.py
```

If authorized:

```bash
git add lucnhan/ui/history_panel.py lucnhan/ui/theme/widgets_common.py lucnhan/ui/styles.py tests/ui/test_history_panel.py tests/qt_stubs.py
git commit -m "feat(history): add passive chat delete controls"
```

---

### Task 5: Implement PanelCore Confirmation, Intents, Worker, Retry, and Watchdog

**Files:**
- Modify: `lucnhan/ui/panel_core.py:5-27, 259-290, 665-673, 1803-2453`
- Modify: `tests/tools/test_panel_core.py:1335-1382, 1647-1927, 2170-3140`

**Interfaces:**
- Consumes: `HistoryDeleteResult`, `HistoryDeleteStatus`, `HISTORY_DELETE_SLOW_NOTICE_SECONDS`, widget APIs from Task 4, controller API from Task 3.
- Produces: `_on_history_delete_requested()`, `_start_history_delete()`, `_history_delete_worker()`, `_apply_history_deleted()`, deletion intent and watchdog lifecycle.

**Atomicity note:** This task must land as one coherent change. Do not split worker enqueue, drain routing, intent gate, and lifecycle cleanup across separate commits; intermediate combinations can attach or strand a session being deleted.

- [ ] **Step 1: Extend the bare panel fixture and write RED preflight tests**

In `_make_history_panel()` add:

```python
panel._history_retry_delete_session_id = None
panel._history_last_delete_session_id = None
panel._history_delete_intents = set()
panel._history_delete_watchdog = None
```

Add tests:

```python
class TestHistoryDeletePreflight(unittest.TestCase):
    def test_open_chat_is_focused_without_confirmation_or_submit(self) -> None:
        panel = _make_history_panel()
        panel._ctrl.find_tab_for_session.return_value = "tab-a"
        panel._focus_tab = MagicMock()
        panel._confirm_history_delete = MagicMock()

        panel._on_history_delete_requested("persisted-a", "Analyze parser")

        panel._focus_tab.assert_called_once_with("tab-a")
        panel._confirm_history_delete.assert_not_called()
        panel._history_panel.show_notice.assert_called_once_with(
            "Close this chat before deleting it from History.",
            retry_visible=False,
            dismiss_visible=True,
        )

    def test_pending_history_operation_shows_busy_notice(self) -> None:
        panel = _make_history_panel()
        panel._history_pending = True

        panel._on_history_delete_requested("persisted-a", "Analyze parser")

        panel._history_panel.show_notice.assert_called_once_with(
            "History is busy. Try again shortly.",
            retry_visible=False,
            dismiss_visible=True,
        )
```

- [ ] **Step 2: Run RED preflight tests**

```bash
python -m pytest tests/tools/test_panel_core.py -v -k "HistoryDeletePreflight"
```

Expected: missing delete slot/helper/state.

- [ ] **Step 3: Add imports, fields, signal wiring, and confirmation helper**

Update imports:

```python
from ..constants import HISTORY_DELETE_SLOW_NOTICE_SECONDS
from ..state.history_types import (
    HistoryDeleteResult,
    HistoryDeleteStatus,
    HistoryListResult,
    HistoryLoadResult,
    ...,
)
```

Change queue annotation:

```python
self._history_result_queue: queue.Queue[
    HistoryListResult | HistoryLoadResult | HistoryDeleteResult
] = queue.Queue(maxsize=_HISTORY_RESULT_QUEUE_MAXSIZE)
```

Initialize:

```python
self._history_retry_delete_session_id: str | None = None
self._history_last_delete_session_id: str | None = None
self._history_delete_intents: set[str] = set()
self._history_delete_watchdog: QTimer | None = None
```

Wire:

```python
self._history_panel.session_delete_requested.connect(
    self._on_history_delete_requested
)
self._history_panel.notice_dismissed.connect(
    self._on_history_notice_dismissed
)
```

Extract a testable confirmation helper:

```python
def _confirm_history_delete(self, title: str) -> bool:
    dialog = QMessageBox(self)
    dialog.setWindowTitle("Delete chat?")
    dialog.setTextFormat(Qt.TextFormat.PlainText)
    dialog.setText(
        f'“{title}” will be permanently deleted from History.\n'
        "This action cannot be undone."
    )
    delete_btn = dialog.addButton("Delete", QMessageBox.ButtonRole.DestructiveRole)
    cancel_btn = dialog.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    dialog.setDefaultButton(cancel_btn)
    dialog.setEscapeButton(cancel_btn)
    dialog.exec()
    return dialog.clickedButton() is delete_btn
```

Use fixed literal copy only; do not interpolate title into QSS.

- [ ] **Step 4: Implement preflight and submission**

```python
def _on_history_delete_requested(self, session_id: str, title: str) -> None:
    if self._is_shutdown:
        return
    if self._history_pending:
        if self._history_panel is not None:
            self._history_panel.show_notice(
                "History is busy. Try again shortly.",
                retry_visible=False,
                dismiss_visible=True,
            )
        return
    existing_tab = self._ctrl.find_tab_for_session(session_id)
    if existing_tab is not None:
        self._focus_tab(existing_tab)
        if self._history_panel is not None:
            self._history_panel.show_notice(
                "Close this chat before deleting it from History.",
                retry_visible=False,
                dismiss_visible=True,
            )
        return
    self._history_delete_intents = {
        *self._history_delete_intents,
        session_id,
    }
    if not self._confirm_history_delete(title):
        self._clear_history_delete_intent(session_id)
        return
    self._start_history_delete(session_id)


def _start_history_delete(self, session_id: str) -> None:
    if self._is_shutdown or self._history_pending:
        self._clear_history_delete_intent(session_id)
        return
    existing_tab = self._ctrl.find_tab_for_session(session_id)
    if existing_tab is not None:
        self._clear_history_delete_intent(session_id)
        self._focus_tab(existing_tab)
        if self._history_panel is not None:
            self._history_panel.show_notice(
                "Close this chat before deleting it from History.",
                retry_visible=False,
                dismiss_visible=True,
            )
        return

    self._history_delete_intents = {
        *self._history_delete_intents,
        session_id,
    }
    self._history_generation += 1
    scope = self._ctrl.capture_history_scope(self._history_generation)
    self._history_pending = True
    self._history_last_delete_session_id = session_id
    self._history_retry_delete_session_id = None
    if self._history_panel is not None:
        self._history_panel.clear_notice()
        self._history_panel.set_operation_pending(session_id)
    # Create/reuse the dedicated History executor; never use _SAVE_EXECUTOR.
    if self._history_executor is None:
        self._history_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=_HISTORY_EXECUTOR_PREFIX,
        )
    executor = self._history_executor
    closing_event = self._history_closing
    try:
        executor.submit(
            self._history_delete_worker,
            session_id,
            scope,
            closing_event,
        )
    except RuntimeError:
        self._clear_history_delete_intent(session_id)
        self._history_pending = False
        if self._history_panel is not None:
            self._history_panel.set_operation_pending(None)
        raise
    self._start_history_delete_watchdog(scope)
    self._ensure_history_poll_timer()
```

Keep the existing inline lazy-executor pattern used by list/load; do not introduce an unrelated executor refactor in this feature.

- [ ] **Step 5: Write RED worker/drain/apply tests**

```python
class TestHistoryDeleteWorkerAndApply(unittest.TestCase):
    def test_worker_enqueues_typed_delete_result(self) -> None:
        panel = _make_history_panel()
        scope = MagicMock(generation=4)
        result = HistoryDeleteResult(
            HistoryDeleteStatus.DELETED,
            scope,
            "persisted-a",
        )
        panel._ctrl.delete_history_session.return_value = result

        panel._history_delete_worker(
            "persisted-a",
            scope,
            panel._history_closing,
        )

        self.assertIs(panel._history_result_queue.get_nowait(), result)

    def test_deleted_result_removes_row_and_starts_refresh(self) -> None:
        panel = _make_history_panel()
        scope = panel._ctrl.capture_history_scope.return_value
        scope.generation = 5
        panel._history_generation = 5
        panel._history_delete_intents = {"persisted-a"}
        panel._start_history_list_request = MagicMock()
        result = HistoryDeleteResult(
            HistoryDeleteStatus.DELETED,
            scope,
            "persisted-a",
        )

        panel._apply_history_deleted(result)

        panel._history_panel.remove_entry.assert_called_once_with("persisted-a")
        panel._start_history_list_request.assert_called_once()
        self.assertNotIn("persisted-a", panel._history_delete_intents)

    def test_failed_result_keeps_row_and_enables_retry(self) -> None:
        panel = _make_history_panel()
        result = HistoryDeleteResult(
            HistoryDeleteStatus.FAILED,
            MagicMock(),
            "persisted-a",
            error="PermissionError: locked",
        )

        panel._apply_history_deleted(result)

        panel._history_panel.remove_entry.assert_not_called()
        panel._history_panel.set_operation_pending.assert_called_with(None)
        panel._history_panel.show_notice.assert_called_with(
            "Could not delete this chat.",
            retry_visible=True,
            dismiss_visible=True,
        )
        self.assertEqual(panel._history_retry_delete_session_id, "persisted-a")
```

- [ ] **Step 6: Implement worker, drain union routing, and apply**

Worker:

```python
def _history_delete_worker(
    self,
    session_id: str,
    scope: HistoryScope,
    closing_event: threading.Event,
) -> None:
    try:
        result = self._ctrl.delete_history_session(session_id, scope)
    except CancelledError:
        raise
    except Exception as exc:
        log_error(f"history delete worker failed: {type(exc).__name__}: {exc}")
        result = HistoryDeleteResult(
            HistoryDeleteStatus.FAILED,
            scope,
            session_id,
            error=f"{type(exc).__name__}: {exc}",
        )
    if not closing_event.is_set():
        self._history_result_queue.put(result)
```

Import `CancelledError` from `concurrent.futures`; the explicit branch above preserves cancellation before the general worker-boundary catch.

In `_drain_history_results()`:

```python
if isinstance(result, HistoryLoadResult):
    self._apply_history_loaded(result)
elif isinstance(result, HistoryDeleteResult):
    self._apply_history_deleted(result)
else:
    self._apply_history_list_result(result)
```

Keep the current generation check and clear `_history_pending` before apply. Terminal delete apply must stop the watchdog first.

Apply:

```python
def _apply_history_deleted(self, result: HistoryDeleteResult) -> None:
    self._stop_history_delete_watchdog()
    self._clear_history_delete_intent(result.session_id)
    if self._history_panel is not None:
        self._history_panel.set_operation_pending(None)

    if result.status in {
        HistoryDeleteStatus.DELETED,
        HistoryDeleteStatus.NOT_FOUND,
    }:
        self._history_retry_delete_session_id = None
        if self._history_panel is not None:
            self._history_panel.remove_entry(result.session_id)
            self._history_panel.clear_notice()
        self._start_history_list_request()
        return

    if result.status is HistoryDeleteStatus.WRONG_IDB:
        self._history_retry_delete_session_id = None
        if self._history_panel is not None:
            self._history_panel.clear_notice()
        self._start_history_list_request()
        return

    self._history_retry_delete_session_id = result.session_id
    if self._history_panel is not None:
        self._history_panel.show_notice(
            "Could not delete this chat.",
            retry_visible=True,
            dismiss_visible=True,
        )
```

- [ ] **Step 7: Add and verify LOAD→DELETE intent regression**

Write the critical race tests:

```python
class TestHistoryDeleteIntents(unittest.TestCase):
    def test_delete_intent_exists_while_confirmation_is_open(self) -> None:
        panel = _make_history_panel()

        def confirm(_title: str) -> bool:
            self.assertIn("persisted-a", panel._history_delete_intents)
            return False

        panel._confirm_history_delete = confirm

        panel._on_history_delete_requested("persisted-a", "Analyze parser")

        self.assertNotIn("persisted-a", panel._history_delete_intents)

    def test_intent_blocks_queued_load_result_before_confirmation_returns(self) -> None:
        panel = _make_history_panel()
        session = MagicMock(id="persisted-a", messages=[])
        result = _load_result(
            status=HistoryRequestStatus.LOADED,
            scope=MagicMock(),
            session=session,
        )
        panel._history_delete_intents = {"persisted-a"}

        panel._apply_history_loaded(result)

        panel._ctrl.attach_history_session.assert_not_called()

    def test_intent_blocks_another_load(self) -> None:
        panel = _make_history_panel()
        panel._history_delete_intents = {"persisted-a"}
        panel._start_history_load = MagicMock()

        panel._on_history_open_requested("persisted-a")

        panel._start_history_load.assert_not_called()
        panel._ctrl.find_tab_for_session.assert_not_called()
```

Implement at the top of `_apply_history_loaded()` after validating `result.session` exists but before attach:

```python
if result.session.id in self._history_delete_intents:
    return
```

Implement at the top of `_on_history_open_requested()`:

```python
if session_id in self._history_delete_intents:
    return
```

Use immutable replacement when adding/removing intent entries:

```python
def _clear_history_delete_intent(self, session_id: str) -> None:
    self._history_delete_intents = {
        intent_id
        for intent_id in self._history_delete_intents
        if intent_id != session_id
    }
```

- [ ] **Step 8: Add retry routing and dismissal behavior**

Extend `_on_history_retry()` priority: delete retry, then load retry, then list retry.

```python
retry_delete_id = self._history_retry_delete_session_id
if retry_delete_id is not None:
    self._history_retry_delete_session_id = None
    self._start_history_delete(retry_delete_id)
    return
```

A delete retry calls `_start_history_delete()` directly (no confirmation), which rechecks open-tab, captures fresh generation/scope, and re-adds intent. Use the `notice_dismissed = Signal()` added in Task 4: PanelCore connects it to:

```python
def _on_history_notice_dismissed(self) -> None:
    self._history_retry_delete_session_id = None
```

This clears retry presentation state without changing persistence state. Do not let widget code mutate PanelCore state.

Add these concrete tests beside the retry-routing tests:

```python
def test_delete_retry_skips_confirmation_and_captures_fresh_scope(self) -> None:
    panel = _make_history_panel()
    panel._history_retry_delete_session_id = "persisted-a"
    panel._confirm_history_delete = MagicMock()
    panel._start_history_delete = MagicMock()

    panel._on_history_retry()

    panel._confirm_history_delete.assert_not_called()
    panel._start_history_delete.assert_called_once_with("persisted-a")


def test_delete_retry_focuses_chat_opened_after_failure(self) -> None:
    panel = _make_history_panel()
    panel._history_retry_delete_session_id = "persisted-a"
    panel._ctrl.find_tab_for_session.return_value = "tab-a"
    panel._focus_tab = MagicMock()
    panel._history_executor = MagicMock()

    panel._on_history_retry()

    panel._focus_tab.assert_called_once_with("tab-a")
    panel._history_executor.submit.assert_not_called()
    panel._ctrl.delete_history_session.assert_not_called()
```

- [ ] **Step 9: Add non-terminal slow watchdog**

Tests:

```python
def test_delete_watchdog_notice_does_not_clear_pending_or_intent(self) -> None:
    panel = _make_history_panel()
    panel._history_pending = True
    panel._history_generation = 8
    panel._history_last_delete_session_id = "persisted-a"
    panel._history_delete_intents = {"persisted-a"}

    panel._on_history_delete_slow(8, "persisted-a")

    self.assertTrue(panel._history_pending)
    self.assertIn("persisted-a", panel._history_delete_intents)
    panel._history_panel.show_notice.assert_called_once_with(
        "Deleting this chat is taking longer than expected.",
        retry_visible=False,
        dismiss_visible=True,
    )
```

Implementation:

```python
def _start_history_delete_watchdog(self, scope: HistoryScope) -> None:
    self._stop_history_delete_watchdog()
    timer = QTimer(self)
    timer.setSingleShot(True)
    timer.timeout.connect(
        lambda: self._on_history_delete_slow(scope.generation, self._history_last_delete_session_id)
    )
    timer.start(int(HISTORY_DELETE_SLOW_NOTICE_SECONDS * 1000))
    self._history_delete_watchdog = timer
```

`_on_history_delete_slow()` only shows notice if generation/session still match and `_history_pending` is true. It never clears pending/intent or enables Retry.

- [ ] **Step 10: Extend invalidation/shutdown cleanup**

In `_invalidate_history()`:

```python
self._stop_history_delete_watchdog()
self._history_delete_intents = set()
self._history_retry_delete_session_id = None
self._history_last_delete_session_id = None
if self._history_panel is not None:
    self._history_panel.set_operation_pending(None)
```

Add the regression:

```python
def test_invalidate_clears_delete_state_and_watchdog(self) -> None:
    panel = _make_history_panel()
    panel._history_delete_intents = {"persisted-a"}
    panel._history_retry_delete_session_id = "persisted-a"
    panel._history_last_delete_session_id = "persisted-a"
    panel._stop_history_delete_watchdog = MagicMock()

    panel._invalidate_history(clear_panel=False)

    self.assertEqual(panel._history_delete_intents, set())
    self.assertIsNone(panel._history_retry_delete_session_id)
    self.assertIsNone(panel._history_last_delete_session_id)
    panel._stop_history_delete_watchdog.assert_called_once()
    panel._history_panel.set_operation_pending.assert_called_with(None)
```

Keep the existing fresh-Event replacement as the final invalidation step.

- [ ] **Step 11: Run focused and full PanelCore suites**

```bash
python -m pytest tests/tools/test_panel_core.py -v -k "delete or intent or watchdog"
python -m pytest tests/tools/test_panel_core.py -v
```

Expected: PASS with no regressions in list/load pending lifecycle.

- [ ] **Step 12: Format, lint, and checkpoint**

```bash
python -m ruff format lucnhan/ui/panel_core.py tests/tools/test_panel_core.py
python -m ruff check lucnhan/ui/panel_core.py tests/tools/test_panel_core.py
```

If authorized:

```bash
git add lucnhan/ui/panel_core.py tests/tools/test_panel_core.py
git commit -m "feat(history): coordinate confirmed chat deletion"
```

---

### Task 6: Add the End-to-End Deletion Regression

**Files:**
- Modify: `tests/integration/test_history_on_demand.py:415-587, 625-790`

**Interfaces:**
- Consumes: real persistence/controller/coordinator APIs from Tasks 2–5.
- Produces: integration proof for current-IDB deletion, disk cleanup, open-tab refusal, failure/retry state, and no resurrection.

- [ ] **Step 1: Extend the recording panel and builder state**

Add methods/state to `_RecordingHistoryPanel`:

```python
self.notice_calls: list[tuple[str, bool, bool]] = []
self.pending_session_id: str | None = None


def remove_entry(self, session_id):
    self.entries = [entry for entry in self.entries if entry.session_id != session_id]


def set_operation_pending(self, session_id):
    self.pending_session_id = session_id


def show_notice(self, message, *, retry_visible=False, dismiss_visible=True):
    self.notice_calls.append((message, retry_visible, dismiss_visible))


def clear_notice(self):
    pass
```

Seed in `_build_panel()`:

```python
panel._history_retry_delete_session_id = None
panel._history_last_delete_session_id = None
panel._history_delete_intents = set()
panel._history_delete_watchdog = None
```

- [ ] **Step 2: Add deterministic facade delete method**

```python
def delete_history_session(self, session_id: str) -> None:
    self._panel._confirm_history_delete = MagicMock(return_value=True)
    entry = next(
        entry
        for entry in self._panel._history_panel.entries
        if entry.session_id == session_id
    )
    self._panel._on_history_delete_requested(session_id, entry.title)
    if not self._panel._history_pending:
        # Open-tab/busy/cancel path: no worker was submitted.
        return

    executor = self._panel._history_executor
    assert executor is not None
    executor.shutdown(wait=True)
    self._panel._history_executor = None
    self._panel._drain_history_results()

    # DELETED/NOT_FOUND queues one reconciliation list request.
    if self._panel._history_pending:
        executor = self._panel._history_executor
        assert executor is not None
        executor.shutdown(wait=True)
        self._panel._history_executor = None
        self._panel._drain_history_results()
```

This mirrors the existing facade's deterministic shutdown/recreate behavior: setting `_history_executor = None` lets the next production request lazily create a fresh executor.

- [ ] **Step 3: Write the full current-IDB delete scenario**

```python
def test_delete_closed_chat_is_permanent_and_scoped(self):
    self.facade.open_history()
    primary = Path(self.cfg.checkpoints_dir) / "sessions" / f"{self.a1_id}.json"
    sidecar = primary.with_name(f"{self.a1_id}.summary.json")
    sidecar.write_text('{"messages":1}', encoding="utf-8")

    self.facade.delete_history_session(self.a1_id)

    self.assertNotIn(self.a1_id, self.facade.visible_history_ids())
    self.assertFalse(primary.exists())
    self.assertFalse(sidecar.exists())
    manifest = json.loads(
        (primary.parent / "_session_manifest.json").read_text(encoding="utf-8")
    )
    self.assertNotIn(self.a1_id, manifest["entries"])
    self.assertTrue(
        (primary.parent / f"{self.b1_id}.json").exists(),
        "IDB-B history must remain untouched",
    )

    self.facade.open_history()
    self.assertNotIn(self.a1_id, self.facade.visible_history_ids())
```

- [ ] **Step 4: Add open-tab refusal**

```python
def test_delete_open_chat_focuses_tab_without_disk_mutation(self):
    self.facade.open_history()
    self.facade.open_history_session(self.a1_id)
    primary = Path(self.cfg.checkpoints_dir) / "sessions" / f"{self.a1_id}.json"

    self.facade.delete_history_session(self.a1_id)

    self.assertTrue(primary.exists())
    self.assertEqual(
        self.panel._history_panel.notice_calls[-1][0],
        "Close this chat before deleting it from History.",
    )
```

Make facade delete branch on `panel._history_pending`: only wait/shutdown/drain when the slot actually submitted a worker. For an open-tab rejection, return immediately after `_on_history_delete_requested()` and assert no executor was created.

- [ ] **Step 5: Add LOAD→DELETE no-attach/no-resurrection regression**

Drive the production apply seam deterministically:

```python
def test_confirmed_delete_intent_blocks_loaded_session_attach(self):
    self.facade.open_history()
    scope = self.panel._ctrl.capture_history_scope(generation=1)
    loaded = self.panel._ctrl.load_history_session(self.a1_id, scope)
    self.panel._history_delete_intents = {self.a1_id}

    self.panel._apply_history_loaded(loaded)

    self.assertIsNone(self.panel._ctrl.find_tab_for_session(self.a1_id))
    self.facade.delete_history_session(self.a1_id)
    SessionHistory.flush_saves(timeout=5)
    self.assertIsNone(SessionHistory(self.cfg).load_session(self.a1_id))
```

This test must fail if `_apply_history_loaded()` attaches before checking intent.

- [ ] **Step 6: Add primary failure keeps row + Retry state**

Patch the persistence removal boundary for the target primary file only:

```python
def test_primary_delete_failure_keeps_row_and_retry_succeeds(self):
    self.facade.open_history()
    primary = Path(self.cfg.checkpoints_dir) / "sessions" / f"{self.a1_id}.json"
    original_remove = os.remove

    def fail_target(path):
        if os.path.normcase(str(path)) == os.path.normcase(str(primary)):
            raise PermissionError("locked")
        original_remove(path)

    with patch("lucnhan.state.history.os.remove", side_effect=fail_target):
        self.facade.delete_history_session(self.a1_id)

    self.assertIn(self.a1_id, self.facade.visible_history_ids())
    self.assertEqual(self.panel._history_retry_delete_session_id, self.a1_id)
    self.assertEqual(
        self.panel._history_panel.notice_calls[-1],
        ("Could not delete this chat.", True, True),
    )

    self.panel._confirm_history_delete = MagicMock()
    self.panel._on_history_retry()
    retry_executor = self.panel._history_executor
    assert retry_executor is not None
    retry_executor.shutdown(wait=True)
    self.panel._history_executor = None
    self.panel._drain_history_results()
    if self.panel._history_pending:
        refresh_executor = self.panel._history_executor
        assert refresh_executor is not None
        refresh_executor.shutdown(wait=True)
        self.panel._history_executor = None
        self.panel._drain_history_results()
    self.panel._confirm_history_delete.assert_not_called()
    self.assertFalse(primary.exists())
```

- [ ] **Step 7: Run the integration suite**

```bash
python -m pytest tests/integration/test_history_on_demand.py -v
```

Expected: PASS for old on-demand behavior and all new deletion scenarios.

- [ ] **Step 8: Run the full affected test matrix**

```bash
python -m pytest \
  tests/state/test_history_on_demand.py \
  tests/agent/test_state.py \
  tests/agent/test_session_controller.py \
  tests/ui/test_history_panel.py \
  tests/tools/test_panel_core.py \
  tests/integration/test_history_on_demand.py \
  -v
```

Expected: PASS.

- [ ] **Step 9: Format and checkpoint**

```bash
python -m ruff format tests/integration/test_history_on_demand.py
python -m ruff check tests/integration/test_history_on_demand.py
```

If authorized:

```bash
git add tests/integration/test_history_on_demand.py
git commit -m "test(history): cover permanent chat deletion flow"
```

---

### Task 7: Document, Review, and Run Final Quality Gates

**Files:**
- Modify: `CHANGELOG.md:8-10`
- Review: all production/test files changed in Tasks 1–6

**Interfaces:**
- Consumes: completed feature and tests.
- Produces: release note, caller/security audit, code reviews, local-CI evidence.

- [ ] **Step 1: Add Unreleased changelog entry**

```markdown
## [Unreleased]

### Added
- **Delete individual saved chats from History** — hover or focus a row's
  delete control, confirm permanent removal, and keep the current search
  context. Open chats are protected and focused instead; failed deletion
  remains visible with Retry.
```

Do not bump `PLUGIN_VERSION`, `pyproject.toml`, or `ida-plugin.json`; version bump is a separate release task.

- [ ] **Step 2: Audit forbidden/legacy surfaces**

```bash
rg -n "\bdelete_session\(" lucnhan tests
rg -n "SessionHistory|ThreadPoolExecutor|threading|open\(|os\." lucnhan/ui/history_panel.py
rg -n "from PySide6|import ida_|from ida_" \
  lucnhan/ui/history_panel.py \
  lucnhan/ui/panel_core.py \
  lucnhan/ui/session_controller_base.py
```

Expected:

- no public synchronous `SessionHistory.delete_session()` call;
- HistoryPanel contains no persistence/thread/file I/O import;
- no new direct PySide6/IDA import.

- [ ] **Step 3: Run targeted format/lint**

```bash
python -m ruff format \
  lucnhan/constants.py \
  lucnhan/state/history_types.py \
  lucnhan/state/history.py \
  lucnhan/ui/session_controller_base.py \
  lucnhan/ui/history_panel.py \
  lucnhan/ui/theme/widgets_common.py \
  lucnhan/ui/styles.py \
  lucnhan/ui/panel_core.py \
  tests/state/test_history_on_demand.py \
  tests/agent/test_state.py \
  tests/agent/test_session_controller.py \
  tests/ui/test_history_panel.py \
  tests/tools/test_panel_core.py \
  tests/integration/test_history_on_demand.py

python -m ruff check \
  lucnhan/constants.py \
  lucnhan/state/history_types.py \
  lucnhan/state/history.py \
  lucnhan/ui/session_controller_base.py \
  lucnhan/ui/history_panel.py \
  lucnhan/ui/theme/widgets_common.py \
  lucnhan/ui/styles.py \
  lucnhan/ui/panel_core.py \
  tests/state/test_history_on_demand.py \
  tests/agent/test_state.py \
  tests/agent/test_session_controller.py \
  tests/ui/test_history_panel.py \
  tests/tools/test_panel_core.py \
  tests/integration/test_history_on_demand.py
```

Expected: no lint errors.

- [ ] **Step 4: Run mypy scope used by project CI**

```bash
python -m mypy lucnhan/core lucnhan/providers
```

Expected: PASS. Note that current CI mypy scope does not cover UI/state; rely on tests, ruff, and reviewers for those files.

- [ ] **Step 5: Run complete test suite**

```bash
python -m pytest tests/ -v
```

Expected: PASS.

- [ ] **Step 6: Run repository local CI**

```bash
./ci-local.sh
```

Expected: format, lint, mypy, pytest, and desloppify score gates PASS with no score regression beyond the configured allowance.

- [ ] **Step 7: Run mandatory reviews**

Invoke in parallel after tests are green:

1. `code-reviewer` — general correctness/maintainability.
2. `python-reviewer` — Python typing/concurrency/error handling.
3. `security-reviewer` — path containment, scope checks, deletion/logging, symlink/Windows semantics.

Required review questions:

- Can any save occur after ordered deletion for the same open session ID?
- Can a queued or running LOAD result attach once delete confirmation begins?
- Can any stale-IDB event delete a different IDB's session?
- Does any failure leak raw path/transcript to UI?
- Does invalidation clear every intent/retry/watchdog state?
- Is primary-delete success preserved if sidecar/manifest cleanup fails?

Address every CRITICAL/HIGH finding and rerun affected tests. Fix MEDIUM findings when feasible.

- [ ] **Step 8: Run runtime verification where available**

If an IDA Pro test environment is available, exercise:

1. Open History for an IDB with at least two saved chats.
2. Keyboard-tab to the subdued delete control and verify focus visibility.
3. Cancel confirmation; verify no row/disk change.
4. Confirm deletion; verify row removal, search pintent, and no reappearance after closing/reopening History.
5. Open another saved chat; verify delete focuses the tab and refuses mutation.
6. Simulate a locked session file on Windows; verify fixed error copy and Retry.

If IDA is unavailable, explicitly record that runtime UI verification was skipped; do not claim E2E UI verification from stub tests alone.

- [ ] **Step 9: Final diff/status audit and checkpoint**

```bash
git diff --check
git diff --stat
git status --short
```

Verify no `.superpowers/brainstorm/`, `__pycache__/`, `MagicMock/`, temp manifest/session files, or unrelated changes are staged.

If authorized:

```bash
git add \
  CHANGELOG.md \
  lucnhan/constants.py \
  lucnhan/state/history_types.py \
  lucnhan/state/history.py \
  lucnhan/ui/session_controller_base.py \
  lucnhan/ui/history_panel.py \
  lucnhan/ui/theme/widgets_common.py \
  lucnhan/ui/styles.py \
  lucnhan/ui/panel_core.py \
  tests/state/test_history_on_demand.py \
  tests/agent/test_state.py \
  tests/agent/test_session_controller.py \
  tests/ui/test_history_panel.py \
  tests/tools/test_panel_core.py \
  tests/integration/test_history_on_demand.py

git commit -m "feat(history): add safe chat deletion"
```

## Plan Self-Review

### Spec coverage

- Single-chat, Current-IDB-only hard delete: Tasks 2, 3, 5, 6.
- Confirmation with named title and safe defaults: Task 5.
- Open-tab focus/refusal: Tasks 5 and 6.
- Hover/focus/keyboard-accessible passive affordance: Task 4.
- Preserve query/scroll and background reconciliation: Tasks 4, 5, 6.
- Failure keeps row with Retry/Dismiss: Tasks 4–6.
- `_SAVE_EXECUTOR` FIFO serialization: Task 2.
- Primary → sidecar → manifest recovery semantics: Task 2.
- ID/path/instance validation and no raw error leakage: Tasks 2, 3, 5, 7.
- LOAD→DELETE intent race: Tasks 5 and 6.
- Generation/IDB/shutdown lifecycle: Task 5.
- Non-terminal slow-operation UX without ambiguous Retry: Task 5.
- Full affected matrix/local CI/review/runtime verification: Task 7.

### Type consistency

- Persistence: `SessionDeleteOutcome.status: SessionDeleteStatus`.
- Controller/UI queue: `HistoryDeleteResult.status: HistoryDeleteStatus`.
- Queue union: `HistoryListResult | HistoryLoadResult | HistoryDeleteResult`.
- Widget signal: `session_delete_requested(str, str)` = `(session_id, title)`.
- Retry state: `_history_retry_delete_session_id: str | None`.
- Delete-intent state: `_history_delete_intents: set[str]`, Qt-main-thread owned.
- Watchdog: `_history_delete_watchdog: QTimer | None`, non-terminal only.

### Known execution caveats

- Task 5 is intentionally larger because its worker/drain/intent/lifecycle changes are one correctness unit; splitting it would create unsafe intermediate states.
- The integration facade must not assume an executor exists after open-tab refusal.
- The plan corrected the earlier timeout ambiguity: delete waits for a terminal persistence outcome; a 30-second watchdog informs the user but never enables Retry while outcome is unknown.
- Real IDA UI verification is environment-dependent and must be reported accurately.
