# Chat History Deletion

**Date:** 2026-07-18  
**Status:** Approved design  
**Scope:** Delete one persisted chat from the current IDB's History panel

## 1. Problem Statement

Chat History On-Demand lets users list, search, and reopen saved chats for the current IDB, but it intentionally excluded deletion from its first version. Luc Nhan already has a low-level `SessionHistory.delete_session()` helper, but exposing that helper directly to Qt would be unsafe:

- a queued autosave could run after deletion and recreate the chat;
- a stale row from another IDB could target the wrong session;
- deleting a chat still attached to an open tab would let later autosave resurrect it;
- persistence exceptions could escape through a Qt slot;
- the current helper leaves fork-summary sidecars behind.

The feature must provide an explicit, safe hard-delete operation without weakening the on-demand History architecture or its thread-safety invariants.

## 2. Validated Product Decisions

The following product decisions were approved:

1. Delete one chat at a time from History for the current IDB only.
2. Deletion is permanent and requires confirmation.
3. The confirmation names the chat, states that deletion cannot be undone, focuses `Cancel` by default, and uses a destructive `Delete` button.
4. If the chat is open in any tab, deletion is blocked. Luc Nhan focuses that tab and asks the user to close it first.
5. Each History row has a delete button that becomes visually prominent on hover or keyboard focus.
6. After successful deletion, the row disappears immediately, the search query and scroll position are preserved, and a background refresh reconciles the panel with storage.
7. On failure, the row remains and the panel offers Retry using a fixed user-safe error message.
8. No success toast is needed.

## 3. Goals and Non-Goals

### 3.1 Goals

- Make single-chat deletion discoverable without cluttering the History panel.
- Preserve the passive-widget pattern: widgets emit intent and render state only.
- Keep all persistence work off the Qt main thread.
- Return a typed terminal result for every submitted delete request.
- Revalidate current-IDB ownership immediately before mutation.
- Make repeated delete attempts idempotent.
- Prevent save/delete ordering from resurrecting a deleted chat.
- Preserve list/load generation and shutdown protections.
- Treat primary-session deletion as the user-visible success boundary while cleaning secondary artifacts best-effort.

### 3.2 Non-Goals

- Bulk delete or multi-select.
- Trash, archive, soft-delete, or undo.
- Deleting an open chat automatically.
- Cancelling an active agent as part of deletion.
- Cross-IDB browsing or deletion.
- Headless/control API, MCP, or LLM-tool exposure.
- Startup/session auto-restore changes.
- Config-schema or session-schema migration.

## 4. Existing Architecture and Boundary Conditions

The implementation extends the established History path:

```text
HistoryRowWidget signal
  -> HistoryPanel signal
  -> LucNhanPanelCore main-thread preflight + confirmation
  -> dedicated _history_executor worker
  -> SessionControllerBase Qt-free API
  -> SessionHistory ordered command on _SAVE_EXECUTOR
  -> typed result queue
  -> dedicated History QTimer
  -> LucNhanPanelCore main-thread apply
  -> passive HistoryPanel update
```

Important existing invariants remain unchanged:

- `_history_executor` remains distinct from `_SAVE_EXECUTOR`.
- One `_history_pending` slot serializes list, load, and delete requests.
- A captured `HistoryScope` contains `idb_path`, `db_instance_id`, and generation.
- Workers receive the captured `threading.Event`; `_invalidate_history()` sets the old event and replaces it rather than reusing it.
- Results apply only if their generation still equals `_history_generation`.
- Workers never touch Qt widgets.
- `HistoryPanel` never imports persistence, config, threads, or executors.
- `SessionHistory._session_path()` remains the filesystem-containment boundary.
- `SessionHistory._manifest_lock` remains the manifest read-modify-write boundary.

## 5. Selected Design

### 5.1 Options considered

#### Option A — Delete synchronously from the row click

Rejected because filesystem work would block the Qt main thread, exceptions could escape through a slot, and a queued autosave could recreate the session.

#### Option B — Confirm in the widget, mutate in PanelCore

Rejected because it makes `HistoryPanel` stateful and couples presentation to persistence and lifecycle behavior.

#### Option C — Passive widget + PanelCore coordinator + ordered persistence command

**Selected:** This follows the established list/load architecture, keeps the UI responsive, preserves stale-generation protection, and provides a testable error contract.

## 6. Component Design

### 6.1 Cross-thread result contracts

Add a delete-specific status enum and immutable result DTO:

```python
class HistoryDeleteStatus(str, Enum):
    DELETED = "deleted"
    NOT_FOUND = "not_found"
    WRONG_IDB = "wrong_idb"
    FAILED = "failed"


@dataclass(frozen=True)
class HistoryDeleteResult:
    status: HistoryDeleteStatus
    scope: HistoryScope
    session_id: str
    error: str = ""
```

Delete statuses remain separate from `HistoryRequestStatus`, which currently models list/load outcomes. `error` is diagnostic-only and must not be rendered in the widget.

### 6.2 `HistoryRowWidget` and `HistoryPanel`

Each row gains a compact delete button at its right edge.

Behavior:

- The button is visually subdued at rest and prominent when the row is hovered.
- Keyboard focus also makes the button prominent.
- The button remains in tab order when visually subdued.
- Tooltip: `Delete chat`.
- Accessible name includes the sanitized title.
- Clicking Delete must not propagate to the row's open-chat behavior.
- The title column reserves horizontal space for the button.
- Rows retain their plain-text title and 320 px width constraints.

Signals:

```python
HistoryRowWidget.session_delete_requested = Signal(str, str)
HistoryPanel.session_delete_requested = Signal(str, str)
```

The values are persisted `session_id` and sanitized display `title`. The panel forwards row intent only; it does not confirm, delete, or create threads.

Add panel methods with presentation-only responsibilities:

```python
remove_entry(session_id: str) -> None
set_operation_pending(session_id: str | None) -> None
show_notice(
    message: str,
    *,
    retry_visible: bool = False,
    dismiss_visible: bool = True,
) -> None
clear_notice() -> None
```

`remove_entry()` updates the cached immutable-entry list and rerenders while preserving the current search query. It captures the vertical scroll value before rerendering and restores it, clamped to the new range, after the row layout has been rebuilt.

`set_operation_pending(session_id)` disables open and delete controls on every row, including the targeted row, because list/load/delete share one single-flight slot. Search remains enabled because it is a local operation over cached entries; closing History also remains enabled. Passing `None` clears the pending state. Programmatic intents received while pending are rejected with `History is busy. Try again shortly.` rather than silently ignored.

`show_notice()` provides non-modal feedback in a dedicated notice row above the list, so successful cached rows remain rendered and interactive unless `set_operation_pending()` disables them. A notice clears on explicit dismissal, Retry, the next terminal success, panel `clear()`, or IDB invalidation. The panel emits its existing Retry signal with PanelCore-owned retry routing; dismissing a notice only clears presentation state.

### 6.3 `LucNhanPanelCore`

PanelCore owns confirmation, open-tab checks, delete submission, worker lifetime, retry routing, and UI updates.

On delete intent:

1. Reject if shutdown is active.
2. Reject while `_history_pending` is true and show `History is busy. Try again shortly.`
3. Call `find_tab_for_session(session_id)` before confirmation.
4. If found, focus the tab and show `Close this chat before deleting it from History.` No confirmation or worker starts.
5. Show a confirmation dialog with `Cancel` as default/escape and `Delete` as destructive action.
6. Recheck `find_tab_for_session(session_id)` after confirmation to close the click-to-confirm race.
7. Capture a fresh `HistoryScope`, increment generation, mark the operation pending, record the retry target, and submit to the existing `_history_executor`.

The History worker calls the controller API and enqueues `HistoryDeleteResult`. The controller handles `FileNotFoundError` as `NOT_FOUND`, then catches expected `(OSError, ValueError, KeyError, json.JSONDecodeError)` failures and returns `FAILED`. The outer worker catches unexpected `Exception` only to protect the executor boundary and converts it to `FAILED`. It must re-raise `concurrent.futures.CancelledError`; `KeyboardInterrupt` and `SystemExit` remain uncaught as `BaseException` subclasses. It never touches widgets.

PanelCore starts a one-shot 30-second watchdog when it submits deletion. If the request is still the current generation when the watchdog fires, the panel shows `Deleting this chat is taking longer than expected.` without changing the pending state. The watchdog is stopped on every terminal result, submission failure, IDB invalidation, and shutdown. This avoids the ambiguous state where a timed-out future might later delete the chat after Retry has been enabled.

`_drain_history_results()` extends its queue union to include `HistoryDeleteResult` and retains generation rejection. The delete branch clears `_history_pending` before calling the apply method so the success path can submit a reconciliation list request.

Apply behavior:

- `DELETED` or `NOT_FOUND`: remove the row from the panel cache, clear delete retry state, and start a background list refresh.
- `WRONG_IDB`: clear retry state and keep the cached row until the current-IDB refresh authoritatively replaces the list; this avoids removing a potentially unrelated current row from stale UI data.
- `FAILED`: keep the row, re-enable controls, record `_history_retry_delete_session_id`, and show `Could not delete this chat.` with Retry and Dismiss.
- Stale generation: discard the UI result. Persistence may already have completed.

The existing Retry button routes by operation type. A delete retry does not ask for confirmation again, because the same session was already confirmed. It must recheck that the chat has not been opened, capture a fresh scope/generation, and clear itself if the IDB changed.

### 6.4 `SessionControllerBase`

Add a Qt-free worker API:

```python
def delete_history_session(
    self,
    session_id: str,
    scope: HistoryScope,
) -> HistoryDeleteResult:
    ...
```

Responsibilities:

- Validate that the scope still represents the controller's current IDB.
- Submit one ordered delete command to `SessionHistory`; the persistence command is the single point that reads and validates persisted `id`, `idb_path`, and `db_instance_id` immediately before mutation.
- Wait for the ordered save-executor future until it reaches a terminal outcome. A Qt-main-thread watchdog displays a non-terminal slow-operation notice after `HISTORY_DELETE_SLOW_NOTICE_SECONDS` (30 seconds), but it does not clear `_history_pending`, release the deletion intent, or enable Retry while the persistence outcome is unknown.
- Map the internal persistence outcome to `HistoryDeleteResult`.
- Handle `FileNotFoundError` before its `OSError` superclass so absence maps to `NOT_FOUND`. Convert remaining expected `(OSError, ValueError, KeyError, json.JSONDecodeError)` failures into `FAILED`; do not let them escape to a Qt slot.

The controller does not perform a redundant pre-load, avoiding a double read and a stale validation-to-mutation window. Open-tab state remains a PanelCore main-thread responsibility; §8.3 prevents an in-flight or already-queued load from attaching from the moment delete confirmation begins.

### 6.5 `SessionHistory`

The existing synchronous `delete_session()` is not sufficient because it can race `_SAVE_EXECUTOR` and does not clean companion files. Introduce an ordered asynchronous deletion API, implemented using the same single-worker save executor as autosaves:

```python
class SessionDeleteStatus(str, Enum):
    DELETED = "deleted"
    NOT_FOUND = "not_found"
    WRONG_IDB = "wrong_idb"
    FAILED = "failed"


@dataclass(frozen=True)
class SessionDeleteOutcome:
    status: SessionDeleteStatus
    error: str = ""


def delete_session_async(
    self,
    session_id: str,
    *,
    expected_idb_path: str,
    expected_db_instance_id: str,
) -> Future[SessionDeleteOutcome]:
    ...
```

The private save-executor worker performs one ordered command:

1. Validate the session ID through the existing conservative rule and `_session_path()` containment check.
2. Acquire `_manifest_lock` for the full preflight-and-mutation critical section.
3. Read the target session JSON exactly once.
4. Validate:
   - payload `id == session_id`;
   - every populated identity field matches: normalized `idb_path` must equal the expected normalized path, and canonical `db_instance_id` must equal the expected canonical instance ID. This is intentionally stricter than the list/load `_matches_current_idb()` predicate because delete is destructive; instance identity must not override a contradictory populated path.
5. Delete the primary `{session_id}.json`.
6. Best-effort delete `{session_id}.summary.json`.
7. Best-effort remove the manifest entry using the existing atomic temp-file + `os.replace()` path.
8. Return a terminal `SessionDeleteOutcome`.

The existing synchronous public `delete_session()` must not remain as an unsafe alternative. Replace it with the ordered API; if temporary compatibility is required during implementation, make the synchronous method a private save-executor worker that cannot be called independently.

## 7. Persistence Semantics

### 7.1 Ordering relative to autosaves

The required ordering is:

```text
_save_executor queue:
  save(session X)
  save(session X)
  delete(session X)
```

Since `_SAVE_EXECUTOR` has one worker, every save already queued before the delete completes first, and the delete runs last. This removes the flush-then-delete gap where another save could be queued between a sentinel and direct deletion.

No in-memory open tab may still reference the deleted session because PanelCore blocks deletion while the session is open.

### 7.2 Source of truth

The primary session JSON is the authoritative user-visible artifact. Sidecar and manifest are secondary indexes/caches.

Success boundary:

- if the primary file is deleted: `DELETED`;
- if the primary file is already absent: `NOT_FOUND`;
- if the primary cannot be removed due to I/O failure: `FAILED`.

### 7.3 Idempotency

Missing primary file is a terminal idempotent result, not an error:

- return `NOT_FOUND`;
- remove a stale manifest entry if possible;
- remove an orphan summary sidecar if possible.

PanelCore treats `NOT_FOUND` like successful deletion and removes the cached row before reconciliation.

### 7.4 Partial cleanup

Once the primary JSON is gone, the user-visible deletion has succeeded. A sidecar or manifest cleanup failure is logged but does not restore the row or return `FAILED`. The next list/rebuild removes stale manifest state. Manifest rebuild should also ignore and may clean orphan summary files.

### 7.5 Identity and containment

- Invalid IDs do not perform filesystem I/O and result in `NOT_FOUND` at the persistence boundary.
- `_session_path()` enforces containment below `checkpoints/sessions`.
- Expected IDB values come from immutable `HistoryScope`.
- Persisted payload identity is revalidated immediately before deletion; every populated path and instance field must agree.
- A filename/payload-ID mismatch or wrong IDB returns `WRONG_IDB` without mutation.

## 8. Concurrency and Lifecycle

### 8.1 Single-flight operations

List, load, and delete share `_history_pending`. At most one History request is queued or running. This avoids ambiguity in cached rows, retry routing, and worker shutdown.

### 8.2 Save-executor serialization

Filesystem deletion is submitted to `_SAVE_EXECUTOR`, not executed directly by `_history_executor`. This removes the flush-then-delete race and creates a deterministic order with all previously queued saves.

### 8.3 Delete versus history load

The open-tab preflight alone is not enough. A LOAD worker may already be in flight when the user starts DELETE:

```text
LOAD starts for session X
DELETE confirmation accepted for session X
LOAD result reaches Qt before DELETE result
```

If PanelCore blindly attaches the load, the session becomes open after the preflight and before persistence deletion. The later in-memory tab can autosave and resurrect the deleted file.

PanelCore therefore keeps a main-thread-owned set of deletion intents:

```python
_history_delete_intents: set[str]
```

An intent is added as soon as the user initiates deletion for a closed row, before confirmation opens. It prevents a queued or in-flight load result for that session from attaching while the modal dialog is active. Cancel, dialog close, or failed post-confirm preflight removes the provisional intent; confirmation keeps the same entry active for the submitted delete until terminal result or invalidation.

Rules:

1. After the initial open-tab check succeeds, add the session ID to the intent set before opening confirmation.
2. `_apply_history_loaded()` checks this set before `attach_history_session()`. If the loaded session ID has a delete intent, it does not attach or reuse a tab; it discards the loaded payload.
3. `_on_history_open_requested()` refuses to start a new load for a session with a delete intent.
4. Cancel, dialog close, or a failed post-confirm open-tab check removes the intent immediately.
5. On confirmation, keep the intent while the ordered delete is submitted; remove it on `DELETED`, `NOT_FOUND`, `WRONG_IDB`, `FAILED`, request-submission failure, IDB invalidation, or shutdown.
6. Intents are mutated only on the Qt main thread. Workers receive immutable request data and never access the set.

### 8.4 IDB switch and shutdown

Every delete carries a captured `HistoryScope` and generation. IDB switch and shutdown invalidate UI application of old results.

- If the History worker has not submitted the ordered persistence command, executor cancellation may prevent it.
- Once the ordered persistence command has been submitted, the History worker waits for its terminal outcome and the save executor runs it in FIFO order; the operation is not interrupted halfway.
- A completed stale result is discarded by PanelCore and reflected when History is next refreshed.
- The slow-operation watchdog is stopped during invalidation and shutdown.
- Shutdown does not start retries.

Closing History alone does not cancel a confirmed deletion. The worker may finish and the result may be drained without reopening the panel.

## 9. UX State Machine

### 9.1 Idle row

- Row click opens/focuses chat.
- Delete button is subdued but keyboard reachable.

### 9.2 Hover/focus

- Delete affordance becomes prominent.
- No title-derived stylesheet interpolation.

### 9.3 Confirmation

Exact copy:

```text
Delete chat?

“{title}” will be permanently deleted from History.
This action cannot be undone.
```

Buttons:

- `Cancel` — default and escape action.
- `Delete` — destructive action.

### 9.4 Open chat refusal

```text
Close this chat before deleting it from History.
```

The matching tab is focused. The History panel remains visible.

### 9.5 Pending

- All History row open/delete controls are disabled.
- Search and panel close remain enabled.
- The targeted row remains visible; no optimistic removal occurs before terminal success.

### 9.6 Failure

```text
Could not delete this chat.
```

- The row remains.
- Retry and Dismiss are visible.
- Retry does not ask for confirmation again.
- Dismiss clears the notice and delete-retry target.

### 9.7 Success / idempotent absence

- Remove the row immediately from cache.
- Preserve search query and scroll position.
- No success toast.
- Trigger a background list refresh.

## 10. Security and Privacy

- Session IDs remain untrusted input and must pass `_session_path()` validation.
- Persisted session identity is revalidated against immutable expected scope immediately before mutation; destructive delete does not let instance identity override a contradictory populated path.
- User/LLM-derived titles are plain text only.
- Titles are never interpolated into QSS.
- Confirmation uses the sanitized cached title.
- Widget error copy is fixed and never shows raw exception, path, manifest content, or transcript.
- Logs identify session ID plus exception type; paths and transcript content are not needed for successful deletion logging.
- Symlink containment continues to rely on the existing `_session_path()` policy; deletion must not introduce an alternate path-construction route.

## 11. Test Strategy

### 11.1 Persistence tests

Extend `tests/state/test_history_on_demand.py`:

- valid deletion removes primary JSON, summary sidecar, and manifest entry;
- delete is ordered after previously queued saves;
- delete cannot be followed by a stale queued save that recreates the file;
- missing primary file returns `NOT_FOUND` and cleans stale index/sidecar best-effort;
- wrong IDB path or instance ID returns `WRONG_IDB` without mutation;
- filename/payload-ID mismatch returns `WRONG_IDB` without mutation;
- invalid/traversal IDs perform no I/O;
- primary delete failure returns `FAILED` and keeps manifest row;
- sidecar failure after primary success remains `DELETED`;
- manifest cleanup failure after primary success remains `DELETED` and list/rebuild removes the stale row;
- `flush_saves()` plus list after deletion does not resurrect the session.

Update or remove legacy tests that call the old synchronous `delete_session()` API.

### 11.2 Controller tests

Extend `tests/agent/test_session_controller.py`:

- live scope maps persistence `DELETED` to UI `DELETED`;
- stale controller scope returns `WRONG_IDB` without deletion;
- `NOT_FOUND` and `WRONG_IDB` map exactly;
- expected persistence exception maps to `FAILED`;
- session payload is read/validated only by the ordered persistence command, not twice in controller + persistence;
- no Qt or IDA dependency is introduced.

### 11.3 Widget tests

Extend `tests/ui/test_history_panel.py`:

- delete signal carries persisted ID and sanitized title;
- delete click does not emit open signal;
- hover and keyboard focus expose the destructive affordance;
- tooltip and accessible name are present;
- title width still fits beside the button;
- pending state disables row open/delete controls but leaves search enabled;
- `remove_entry()` preserves query and clamps/restores scroll;
- removing the last match shows correct search-empty/IDB-empty copy;
- notice/retry/dismiss behavior does not replace cached rows;
- passive-widget forbidden-import tests continue to pass.

### 11.4 PanelCore tests

Extend `tests/tools/test_panel_core.py`:

- open chat focuses existing tab; no confirmation or worker;
- post-confirm recheck catches a tab opened during the modal dialog;
- accepted confirmation captures fresh scope, increments generation, and submits delete;
- canceled confirmation does nothing;
- worker emits exactly one terminal result;
- worker drops result after invalidation;
- worker converts expected exception to `FAILED` and re-raises `CancelledError`;
- `DELETED`/`NOT_FOUND` remove cached row and start refresh;
- `WRONG_IDB` keeps row until refresh;
- `FAILED` keeps row and enables Retry/Dismiss;
- Retry rechecks open-tab state and does not reconfirm;
- list/load/delete share `_history_pending`;
- a queued or running load for the same session cannot attach once delete confirmation begins;
- starting load then delete for the same session produces no open tab, and a later agent/save path cannot resurrect the deleted primary file;
- delete-intent state clears on cancel, every terminal result, submission failure, IDB invalidation, and shutdown;
- stale generation results do not touch widgets;
- shutdown clears retry state and never dispatches new work.

### 11.5 Integration test

Extend `tests/integration/test_history_on_demand.py` with a deterministic flow:

1. Persist two sessions for IDB A and one for IDB B.
2. Open History for IDB A.
3. Delete one closed IDB-A chat and verify only that row/file/index disappears.
4. Reopen History and verify the chat does not return.
5. Open the remaining chat, attempt deletion, and verify focus/refusal without mutation.
6. Force a primary-file failure and verify row + Retry remain.
7. Verify IDB B remains untouched.
8. Switch IDBs during a pending delete and verify stale UI application is discarded.

## 12. Documentation and Release Notes

Add an `Unreleased` changelog entry describing:

- single-chat permanent deletion from History;
- confirmation and open-tab protection;
- Retry on failure.

No config, session schema, plugin version, headless route, MCP, or tool documentation changes are required.

## 13. Acceptance Criteria

The feature is complete when all of the following are true:

- A keyboard user can discover and activate Delete for a saved History row.
- Cancel is the safe default and closes without mutation.
- Delete of a closed current-IDB chat removes its primary JSON and cached row.
- Search query and list scroll position survive successful deletion.
- A chat open in any tab cannot be deleted and its tab is focused.
- A save queued before delete cannot recreate the chat afterward.
- A stale load cannot attach the session after confirmed deletion.
- Missing files are idempotent success (`NOT_FOUND` at the typed boundary).
- Wrong-IDB or malformed identity is never deleted.
- Primary delete failure keeps the row and exposes Retry.
- Sidecar/manifest cleanup failure after primary deletion does not restore the row.
- Workers do not touch Qt and widget code does not perform I/O.
- IDB switch and shutdown cannot apply stale results to the wrong panel.
- Existing History list/search/open behavior remains green.
- Targeted suites, full tests, local CI, and required code reviews pass.
