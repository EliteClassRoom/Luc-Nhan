# Chat History On-Demand

**Date**: 2026-07-16  
**Status**: Approved design (pending user review of written spec)  
**Author**: Brainstorming session with user

## 1. Problem Statement

Luc Nhan persists chat sessions per IDB and currently restores them automatically when the panel opens. The current startup path is:

1. `SessionControllerBase` creates one empty `SessionState`.
2. `LucNhanPanelCore` creates a visible `New Chat` tab for that session.
3. `LucNhanPanelCore._try_restore_session()` reads `startup_restore_sessions`.
4. The default value, `"all"`, loads every saved session and replaces the empty tab with many historical tabs.

The relevant code is currently located at:

- `lucnhan/ui/session_controller_base.py:127-130` — creates the initial empty session.
- `lucnhan/ui/panel_core.py:524` — creates the initial `New Chat` tab.
- `lucnhan/ui/panel_core.py:566-569` — invokes automatic restore during panel construction.
- `lucnhan/ui/panel_core.py:1584-1631` — restores all/latest sessions.
- `lucnhan/core/config.py:149-153` — defaults `startup_restore_sessions` to `"all"`.
- `lucnhan/ui/panel_core.py:1269-1317` — repeats automatic restore when the current IDB changes.

This produces an undesirable default experience: opening Luc Nhan floods the tab bar with previous work instead of presenting a fresh workspace.

The required behavior is:

- Opening Luc Nhan always starts with exactly one completely new, empty chat.
- Switching to another IDB/binary always resets the panel to exactly one new, empty chat for that IDB.
- Historical chats remain persisted but are not loaded or rendered automatically.
- Historical chats become visible only after the user explicitly opens History.
- Selecting a historical chat opens it as a normal tab that can be continued.

## 2. Validated Product Decisions

The following decisions were explicitly approved during brainstorming.

| Decision | Approved behavior |
|---|---|
| Startup | Always show exactly one empty `New Chat`; never auto-restore history |
| IDB change | Save chats belonging to the old IDB, then show exactly one empty `New Chat` for the new IDB |
| Existing config | Enforce the new behavior for every user; legacy `all`/`latest` values no longer control startup |
| History entry point | A right-side slide-out panel, following Luc Nhan's existing side-panel pattern |
| History scope | Current IDB only |
| Open semantics | Open the selected session as a normal, continuable tab |
| Duplicate open | Focus the existing tab if the same persisted session is already open |
| v1 actions | Open and search only |
| Out of scope for v1 | Delete, rename, archive, pin/favorite, bulk management, cross-IDB history |
| Code organization | Extend existing `SessionHistory` and `SessionControllerBase` seams; do not add a speculative service layer |

## 3. Goals

1. **Fresh-by-default startup**
   - Panel construction completes with exactly one empty session and one `New Chat` tab.
   - No historical session payload is read at startup.

2. **Explicit history access**
   - History metadata is fetched only when the user opens the History panel.
   - A full session JSON file is loaded only when the user selects that session.

3. **Current-IDB isolation**
   - The History panel lists only sessions belonging to the currently open IDB.
   - A stale or tampered selection cannot open a session belonging to another IDB.

4. **Responsive UI**
   - Opening History does not block IDA's main thread while pending saves drain, a manifest rebuild occurs, or session files are scanned.
   - Opening a large session reuses the existing asynchronous restore path.

5. **No data loss**
   - Existing session JSON files stay valid and remain on disk.
   - Existing save, atomic-write, manifest-rebuild, and corruption-tolerance behavior stays intact.

6. **Minimal architectural change**
   - Keep per-session JSON files.
   - Keep `_session_manifest.json` as the metadata index.
   - Keep `SessionHistory` as the only persistence layer.
   - Keep `SessionControllerBase` as the owner of in-memory sessions and tabs.

## 4. Non-Goals

The first version will not include:

- Automatic startup restore in any form.
- A setting to restore latest/all sessions at startup.
- Cross-IDB or all-binaries history.
- Read-only preview mode.
- Fork-on-open behavior.
- Session delete, archive, pin, favorite, or rename actions.
- Bulk selection or bulk cleanup.
- Full-text search through message payloads.
- Pagination or cursor-based history loading.
- SQLite migration for chat history.
- JSONL/event-log storage.
- A new `HistoryBrowserService` abstraction.
- Headless-mode changes.

These features can be reconsidered after the on-demand flow is used in real IDA sessions.

## 5. Research Findings

### 5.1 Luc Nhan already has the correct storage shape

`SessionHistory` already provides:

- One JSON file per session.
- A compact manifest containing metadata for fast listing.
- Atomic temp-file + `os.replace()` writes for both sessions and the manifest.
- A process-local manifest lock.
- A single-worker executor that serializes asynchronous saves.
- Manifest validation through file mtime and size.
- Full manifest rebuild when the index is missing, stale, corrupt, or version-mismatched.
- Per-message corruption tolerance in `load_session()`.

Therefore, the problem is a lifecycle/UI problem, not a storage-format problem.

### 5.2 External implementations confirm the same boundary

Verified open-source references included:

- Continue (`continuedev/continue`): session metadata index plus one payload file per session.
- Cline (`cline/cline`): lightweight task-history metadata plus per-task payload files and atomic file writes.
- Open WebUI (`open-webui/open-webui`): metadata projection for lists and full chat payload loaded by ID.
- LibreChat (`danny-avila/LibreChat`): lightweight projected conversation lists and full conversation retrieval separately.

The shared pattern is: **list cheap metadata first; load the complete conversation only after selection**.

Luc Nhan already implements the persistence half of that pattern. Replacing JSON with SQLite, splitting payloads into JSONL, or introducing a new indexing service would add migration risk without solving the startup-tab problem.

## 6. Proposed Architecture

### 6.1 Component boundaries

```text
LucNhanPanelCore
├── QTabWidget
│   └── ChatView × N
├── MutationLogPanel        (right-side panel, existing)
└── HistoryPanel            (right-side panel, new)
    ├── Search field
    ├── Loading / empty / error state
    └── Metadata-only session list

HistoryPanel
    │ emits close / retry / selected session ID on the main thread
    ▼
LucNhanPanelCore history coordinator
├── captures immutable HistoryScope
├── starts Python worker for list/load I/O
├── receives typed results through queue.Queue
└── installs results from a main-thread QTimer
    │
    ▼
SessionControllerBase
├── capture_history_scope()
├── list_history_sessions(scope)
├── load_history_session(session_id, scope)   # no state mutation
├── attach_history_session(load_result)       # main thread
└── find_tab_for_session(session_id)
    │
    ▼
SessionHistory
├── list_sessions(...)
├── load_session(session_id)
├── save_session[_async](...)
└── manifest rebuild / validation
```

Responsibilities remain separated:

- **`HistoryPanel`** owns presentation, search, loading state, and main-thread selection/refresh events. It never starts threads and never performs file I/O.
- **`LucNhanPanelCore`** owns Qt composition, side-panel visibility, the history worker/queue/timer lifecycle, tab widget creation, and asynchronous message rendering. It uses a dedicated `_history_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lucnhan-history")`; it must never submit history jobs to the process-wide `_SAVE_EXECUTOR` because history listing calls `flush_saves()` and would deadlock waiting on a sentinel queued behind itself.
- **`SessionControllerBase`** owns immutable scope capture, current-IDB filtering, duplicate detection, security validation, and main-thread insertion of loaded `SessionState` objects into `_sessions`. Its worker-callable list/load methods are pure with respect to controller state.
- **`SessionHistory`** remains the only component that reads or writes session files.

### 6.2 New immutable history DTO

Add a frozen dataclass in `lucnhan/state/history_types.py`:

```python
@dataclass(frozen=True)
class SessionHistoryEntry:
    session_id: str
    title: str
    created_at: float
    updated_at: float
    provider: str
    model: str
    message_count: int
```

The UI must not receive loose manifest dictionaries. The DTO gives the panel a stable, typed, immutable contract without exposing paths, memory IDs, or storage internals.

`updated_at` is derived from the already-recorded session file mtime. No new timestamp needs to be stored in the full session payload. `message_count` is the raw message-array length recorded in the manifest at the last save/rebuild; it is display metadata and may exceed the number rendered if `load_session()` later skips corrupt individual messages. V1 accepts that recovery discrepancy and does not open payloads merely to recompute a count.

### 6.3 `HistoryPanel`

Create `lucnhan/ui/history_panel.py`.

The panel contains:

- Title: `Chat History`.
- Scope label: `Current IDB` (informational; not a selectable scope in v1).
- Search field with placeholder `Search conversations…`.
- Metadata list sorted newest-first; v1 does not add date-group headers.
- Loading state.
- Empty state: `No saved chats for this IDB yet.`
- Search-empty state: `No chats match your search.`
- Error state with `Retry`.
- Close button.

Each row displays:

- Title.
- Last-updated date/time.
- Provider/model when available.
- Message count.

The panel exposes main-thread Qt events only:

- `session_open_requested(session_id: str)`.
- `close_requested()`.
- `retry_requested()`.

The Retry button is rendered only in the error state; clicking it always emits `retry_requested()`. No full message content enters the list model. The panel is intentionally passive: PanelCore owns the worker, result queue, generation counter, and polling timer so one coordinator controls shutdown and IDB-change invalidation. These signals are emitted only by direct main-thread widget interactions or by PanelCore's main-thread queue drain; worker callbacks never call `Signal.emit()`. The History button is always visible, including when the list is empty. Closing and reopening History preserves the last successful rows and search query, but reopening always starts a background refresh so newly saved turns can reorder the list.

### 6.4 Side-panel integration

`LucNhanPanelCore._build_main_splitter()` currently adds the tab widget and `MutationLogPanel`. It will also add `HistoryPanel` as a hidden right-side widget.

The action-button stack gets a checkable `History` button near `Mutations`/`Tools`.

Only one right-side auxiliary panel is visible at a time:

- Add `_show_right_panel(name: Literal["history", "mutation"] | None)` as the single coordinator: hide both widgets, uncheck both buttons, then show/check only the requested panel.
- Opening History calls `_show_right_panel("history")`; opening Mutation Log calls `_show_right_panel("mutation")`.
- The `HistoryPanel` header close button and toggling off the History button call `_show_right_panel(None)`.
- Closing History returns the full splitter width to chat; it deliberately does not remember or reopen Mutation Log.
- Mutual exclusion is based on panel visibility, not merely button checked state, because the `Mutations` button remains hidden until the first mutation is recorded.
- Splitter widget order is chat, Mutation Log, History. Mutation and History start hidden; only the visible auxiliary widget receives the existing 3:1 chat-to-side stretch ratio, so the hidden third widget consumes zero width.

This keeps narrow IDA layouts usable and matches the approved mockup's single-side-panel model.

## 7. Startup and IDB Lifecycle

### 7.1 Startup

The startup flow becomes:

```text
SessionControllerBase.__init__
  → create one empty SessionState
LucNhanPanelCore._build_ui
  → create one New Chat tab
  → finish; do not inspect SessionHistory
```

Required changes:

- Remove the `_try_restore_session()` call from `_build_ui()`.
- Remove `_try_restore_session()` after all call sites are eliminated.
- Remove controller bulk startup methods `restore_sessions()` and `restore_session()` once the implementation-time call-site grep confirms the repository has no remaining callers. If an external compatibility obligation is discovered, retain them as deprecated non-startup APIs instead of calling them automatically.
- Remove `SessionHistory.get_latest_session()` if its only remaining caller was the deleted legacy restore path; otherwise retain it only with a verified caller and focused test.
- Rewrite the existing controller tests for `restore_session()`, token-usage restoration, and tool-call restoration to exercise the new `load_history_session()` → `attach_history_session()` path; do not merely delete their data-integrity assertions.
- Do not load the manifest at startup.
- Do not create `_pending_restore_messages` entries at startup.

### 7.2 IDB change

The IDB-change flow becomes:

```text
on_database_changed(new_path)
  → call controller.cancel() before persistence/reset
  → snapshot non-empty old-IDB sessions and enqueue async saves
  → invalidate History generation/executor/queue for old IDB
  → reset controller identity to new IDB without blocking on JSON writes
  → tear down old ChatViews and pending restores
  → clear HistoryPanel results
  → create exactly one New Chat tab
  → do not auto-restore
```

`reset_for_new_file()` must no longer call blocking `save_session()` in a Qt-main-thread loop. After `cancel()`, it submits each non-empty old-IDB `SessionState` to `save_session_async()` in deterministic tab order, attaches the existing error callback, then detaches those session objects by resetting controller state. The queued futures retain the old sessions, and no code may mutate them after detachment. The history list worker later calls `flush_saves(timeout=10.0)` on the **separate** history executor before listing, so saved conversations become visible without freezing the IDB switch. Shutdown keeps its existing durability responsibility and is not redesigned by this feature.

The existing `_try_restore_session()` call at the end of `on_database_changed()` is removed.

IDB switch invalidates History in this order on the Qt main thread:

1. Increment `_history_generation` before changing controller identity.
2. Stop and destroy `_history_poll_timer` using the lifecycle in §7.4.
3. Shut down the old dedicated history executor with `wait=False, cancel_futures=True`; create a fresh executor lazily the next time History opens.
4. Drain-and-discard the history result queue.
5. Clear HistoryPanel rows and cached search results.
6. Clear `_pending_restore_messages`, then shut down old `ChatView` objects; `ChatView.shutdown()` already calls `_cancel_restore()` and bumps its restore generation, so late restore signals are dropped.

A result produced for the old IDB must never be applied after an IDB switch.

### 7.4 History poll timer lifecycle

History uses a timer separate from the existing agent `_poll_timer`, because History normally opens while the agent is idle.

- Create `_history_poll_timer = QTimer(self)` only when History opens and a request is submitted.
- Connect `timeout` to the main-thread `_drain_history_results()` slot; the timer is active while History is visible or a history request remains in flight.
- Stop the timer when History is hidden and no request remains; if a request is still running, keep polling only until its typed result is drained and discarded/applied.
- `on_database_changed()` and `shutdown()` always stop, disconnect, call `deleteLater()`, and set `_history_poll_timer = None`, catching `(RuntimeError, TypeError)` during disconnect like the existing poll-timer helpers.
- `_drain_history_results()` returns immediately when `_is_shutdown` is true and never emits a signal or touches widgets for a stale generation.

### 7.3 Empty draft behavior

An empty `New Chat` is an in-memory draft, not a historical session.

Existing save call sites already guard on `session.messages`. The design preserves and tests this invariant:

- Opening and closing Luc Nhan without sending a message creates no history entry.
- Creating multiple empty tabs creates no history entries.
- Switching IDBs with an empty chat creates no history entry.
- History filters out any legacy zero-message session that may already exist on disk.

## 8. History Listing Data Flow

### 8.1 User opens History

Add an immutable scope snapshot:

```python
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
```

PanelCore captures `HistoryScope` on the Qt main thread before starting I/O. The worker receives only that immutable scope; it does not read live controller fields while an IDB switch may be mutating them. PanelCore owns the dedicated single-worker `_history_executor` for both list and load requests so manifest rebuilds, flushes, and full-session loads are serialized. This executor is distinct from `_SAVE_EXECUTOR`. It is shut down without waiting on the Qt main thread; late results are discarded by generation/closing checks.

```text
User clicks History
  → PanelCore shows HistoryPanel and increments request generation
  → PanelCore captures immutable HistoryScope on the main thread
  → background Python worker calls controller.list_history_sessions(scope)
       → SessionHistory.flush_saves(timeout=10.0) off the Qt thread
       → SessionHistory.list_sessions(scope.idb_path, scope.db_instance_id)
       → convert summaries to frozen SessionHistoryEntry DTOs
       → sort updated_at descending
  → worker puts typed result + scope into queue
  → PanelCore QTimer polls queue on Qt main thread
  → PanelCore verifies generation + current IDB identity
  → HistoryPanel receives entries only when scope still matches
```

The worker communicates through `queue.Queue`; it must not mutate Qt widgets, emit Qt signals, read mutable `_sessions`, or attach sessions to the controller.

### 8.2 Why listing is asynchronous

The normal path reads only the compact manifest, but `list_sessions()` may rebuild it by scanning every session JSON when the manifest is missing or stale. That fallback must not freeze IDA.

Waiting for `save_session_async()` to finish is also safe only off the main thread. Draining pending saves before listing ensures a chat saved moments earlier appears in History without blocking the UI.

### 8.3 Current-IDB matching

Current IDB means:

1. If a persisted entry contains `db_instance_id`, it must equal the controller's current `_db_instance_id`; path is display/compatibility metadata only in this case.
2. A legacy entry without `db_instance_id` matches only when its non-empty normalized `idb_path` equals the current normalized path.
3. A legacy entry with neither a reliable instance ID nor a non-empty path is excluded.
4. `binary_memory_id` is not used for History v1 because that would broaden the scope to a workspace spanning moved/copied databases.

The matching predicate is one pure helper over a normalized target record (`idb_path`, `db_instance_id`). Manifest entries and loaded `SessionState` objects are adapted to that same record before matching, so list-time and post-load authorization cannot drift. It is reused before and after a manifest rebuild. In particular, the post-rebuild path retains the legacy-path fallback instead of accidentally applying a broader or different filter. The controller adapter unpacks `HistoryScope` into the existing `SessionHistory.list_sessions(idb_path=..., db_instance_id=...)` API; the persistence API does not receive or depend on the UI-oriented scope object. Current database instance IDs are generated as canonical lowercase UUID hex; persisted IDs are sanitized and compared as canonical lowercase hex after trimming surrounding whitespace, with malformed values treated as absent and therefore eligible only for the legacy non-empty path fallback.

### 8.4 Search

Search is local and metadata-only:

- Case-insensitive substring match on `SessionHistoryEntry.title`.
- Leading/trailing whitespace is ignored.
- Search runs on the Qt main thread against the last cached list result only.
- Search never opens session payload files, starts a worker, or queries `SessionHistory` again.
- Empty query restores the full cached current-IDB result set.
- Search updates synchronously on `textChanged`; typical history volume does not justify a debounce timer or search index in v1.

## 9. Title and Manifest Evolution

### 9.1 Title derivation

The manifest already contains `description`, but existing save call sites do not populate it.

For this feature, `description` becomes the session title:

1. Find the first message whose role is `Role.USER` and whose content remains non-empty after sanitization and whitespace trimming.
2. Strip injection-marker patterns using the existing sanitization seam in `lucnhan/core/sanitize.py`.
3. Collapse whitespace and line breaks to single spaces, then trim again.
4. Truncate to the named constant `HISTORY_TITLE_MAX_CHARS = 80`.
5. Fall back to `Untitled chat` only when no usable user message exists.

One shared pure helper, `derive_history_title(messages, max_chars)`, implements this pipeline. `SessionHistory.save_session()` calls it when no explicit description is supplied; `_rebuild_manifest()` calls the same helper for legacy JSON whose `description` is absent. Any explicit description is sanitized through `_safe_persisted_identifier` at the storage boundary before being written. `SessionHistory.list_sessions()` sanitizes legacy manifest descriptions again when constructing `SessionHistoryEntry`, falling back to `Untitled chat` if sanitization empties the value.

`SessionControllerBase.tab_label()` calls the same helper with `max_chars=20`, so tab text and History title differ only by length—not by sanitization or source-message selection. History rows render titles through a widget forced to `Qt.TextFormat.PlainText`; titles are never interpolated into QSS or sent to the LLM.

### 9.2 Existing session backfill

Set `MANIFEST_SCHEMA_VERSION = 2` for the title-aware manifest shape. The new manifest entry stores both `updated_at` and the existing validation timestamp:

- `updated_at`: `int(os.stat(session_path).st_mtime)` as whole seconds since the epoch, used only for display and newest-first sorting; nanosecond precision is not exposed in the DTO.
- `file_mtime_ns`: nanosecond mtime used with file size for stale-entry validation, unchanged from the current contract.

On first History access after upgrade:

- The version mismatch triggers the existing manifest rebuild.
- The rebuild reads existing session JSON files once.
- It validates each JSON/filename session ID before admitting it to the manifest; invalid IDs are skipped with a warning.
- It derives titles from their first user messages through the shared helper whenever a trusted explicit description is absent.
- It records `updated_at` from `st_mtime` and `file_mtime_ns` from `st_mtime_ns`.
- It does not rewrite or migrate the full session files.
- If writing the rebuilt manifest fails, listing may still return the in-memory rebuilt entries for that request, logs a warning, and leaves `last_full_scan` effectively unset so the next request retries instead of treating a failed rebuild as durable.

This provides useful titles and stable timestamps for old sessions without a destructive migration.

### 9.3 Storage invariants retained

- Session JSON remains the authoritative payload.
- Manifest remains disposable/rebuildable metadata.
- Session and manifest writes remain atomic.
- Save ordering remains serialized by `_SAVE_EXECUTOR`.
- A manifest-write failure remains non-fatal after a successful session write.

## 10. Opening a Historical Session

### 10.1 Split load from attach

Opening a session can require full JSON I/O, so loading must happen off the Qt main thread. Attaching the loaded object to `_sessions` must happen on the main thread. These are separate controller operations.

Add these typed load/attach results:

```python
@dataclass(frozen=True)
class HistoryLoadResult:
    status: HistoryRequestStatus
    scope: HistoryScope
    session: SessionState | None = None
    error: str = ""

class HistoryAttachStatus(str, Enum):
    OPENED = "opened"
    ALREADY_OPEN = "already_open"
    STALE_SCOPE = "stale_scope"

@dataclass(frozen=True)
class HistoryAttachResult:
    status: HistoryAttachStatus
    tab_id: str = ""
    session: SessionState | None = None
```

Add controller methods:

```python
def capture_history_scope(self, generation: int) -> HistoryScope: ...
def list_history_sessions(self, scope: HistoryScope) -> list[SessionHistoryEntry]: ...
def load_history_session(self, session_id: str, scope: HistoryScope) -> HistoryLoadResult: ...
def attach_history_session(self, result: HistoryLoadResult) -> HistoryAttachResult: ...
def find_tab_for_session(self, session_id: str) -> str | None: ...
```

`list_history_sessions()` and `load_history_session()` may run in the dedicated history worker and must not mutate controller state. `capture_history_scope()`, `attach_history_session()`, and tab focus/rendering run on the Qt main thread. The worker wraps successful list results and every failure status in frozen result DTOs carrying the captured scope; no exception is used as cross-thread control flow.

### 10.2 Duplicate detection

The parameter is always the persisted `SessionState.id` stored in the manifest and session filename, never the ephemeral `_sessions` dictionary key. `find_tab_for_session(persisted_session_id)` iterates `(tab_id, session)` pairs and returns the `tab_id` whose `session.id == persisted_session_id`. A fresh empty tab's `tab_id` and its not-yet-persisted `SessionState.id` remain independent identifiers.

Duplicate detection occurs twice to handle races safely:

1. On the main thread before starting a load, `find_tab_for_session()` focuses an already-open session immediately.
2. After the worker returns, `attach_history_session()` checks again in case another request opened the same persisted session while this load was in flight.

If found at either point:

- Return `ALREADY_OPEN` with the existing `tab_id`.
- Do not insert a second `SessionState`.
- Do not create a second tab.
- PanelCore focuses the existing tab.

### 10.3 New open

The flow for a session that is not already open is:

1. PanelCore captures the current immutable `HistoryScope` on the main thread.
2. The worker validates the selected session ID before constructing a path.
3. The worker loads through `SessionHistory.load_session()`.
4. The worker rejects missing, corrupt, or empty sessions.
5. The worker verifies the payload belongs to the captured scope using the same current-IDB predicate as listing.
6. The worker queues `HistoryLoadResult` without mutating `_sessions` or Qt.
7. PanelCore's main-thread drain compares `result.scope.generation` with live `_history_generation`; a mismatch is dropped silently before any controller/UI mutation.
8. `attach_history_session()` then re-reads the controller's live normalized IDB path and instance ID. If either no longer matches `result.scope`, it returns `STALE_SCOPE` without creating a tab; otherwise it rechecks duplicate state, creates an ephemeral `tab_id`, and inserts the loaded session into `_sessions`.

PanelCore then:

1. Stores messages in `_pending_restore_messages[tab_id]` before creating the tab.
2. Creates the `ChatView` and label.
3. Switches/focuses the new tab.
4. Calls the existing `_restore_messages_if_needed()` path.
5. `ChatView.restore_from_messages_async()` renders the conversation without blocking the panel.
6. `_on_close_tab()` always removes `_pending_restore_messages.pop(tab_id, None)` before shutting down the `ChatView`, so closing a tab before lazy restore cannot retain a full message list.

The History panel remains open until the user closes it or toggles the History button, allowing several different historical sessions to be opened deliberately.

### 10.4 Continuing the chat

A restored session is a normal active `SessionState`, not a read-only snapshot.

When the user sends a new message:

- The existing `start_agent()` path uses that restored message history.
- `_wire_central_memory()` binds the current IDB workspace before the run.
- End-of-turn auto-save updates the same persisted session ID.
- The manifest mtime changes, so the session sorts to the top the next time History refreshes.

## 11. Security and Data Integrity

### 11.1 Session ID validation

`load_session(session_id)` currently constructs a path from the ID. The new user-triggered open path must validate IDs at the storage boundary.

Accepted IDs must match the concrete rule `^[A-Za-z0-9_-]{1,32}$`. In addition:

- `.` and `..` are rejected explicitly.
- The constructed path is resolved with `os.path.realpath()`.
- `os.path.commonpath()` must prove that the resolved path remains inside `os.path.realpath(self._dir)` before any existence check or open.

One `_validate_session_id()` helper enforces the rule. It runs before path construction in every `SessionHistory` method that accepts a session ID (`load_session()` and `delete_session()`), before `_validate_manifest_entry()` constructs a path, before `_rebuild_manifest()` admits a JSON-derived ID, and before `list_sessions()` emits an existing manifest key. Invalid rebuild/list entries are skipped with `log_warning`; invalid direct calls return the method's not-found/no-op result without I/O. The current generator produces 12 lowercase hex characters, so all existing valid Luc Nhan IDs satisfy the new conservative superset. This prevents a corrupt/tampered manifest or future caller from turning a session ID into path traversal.

### 11.2 Current-IDB revalidation

Filtering the list is not sufficient. The loaded payload is revalidated against the current IDB before it is inserted into `_sessions`. This handles stale UI results, IDB switches, and disk tampering.

### 11.3 Untrusted titles

Titles originate from user/LLM conversation content and must be treated as untrusted display text:

- Derive through existing sanitization helpers.
- Render as plain text, never rich HTML.
- Do not interpolate titles into stylesheets.
- Do not send title text back to the LLM as an instruction.

### 11.4 Thread safety

- Background history loading performs only Python file I/O and immutable DTO construction.
- Worker threads never access Qt widgets or call `Signal.emit()`.
- Worker results enter a bounded `queue.Queue` sized for the single in-flight operation plus its terminal result; PanelCore permits at most one submitted list/load future at a time.
- A dedicated main-thread `QTimer` applies results even when the agent itself is idle.
- Every worker callable catches `Exception` at its outer boundary and enqueues exactly one typed terminal success/failure result in `finally`; the UI cannot remain stuck in Loading because a future raised silently.
- Request generations discard late results after refresh, close, shutdown, or IDB switch.
- History list/load jobs run through one serialized `_history_executor` distinct from `_SAVE_EXECUTOR`, avoiding both concurrent manifest rebuild/load races and flush-sentinel deadlock.
- Shutdown increments generation, stops/disconnects/deletes the polling timer, requests non-blocking executor shutdown, drains and discards queued results, and clears the queue reference. A worker completion after shutdown checks a thread-safe closing flag before attempting `put`; it drops the result instead of retaining a full `SessionState` in an unpolled queue.
- Opening tabs and restoring `ChatView` widgets stays on the Qt main thread.

## 12. Configuration Migration

The legacy `startup_restore_sessions` option no longer represents supported behavior.

Implementation direction:

- Remove it from the `LucNhanConfig` dataclass.
- Remove its validation and normalization branches.
- Remove it from the load field list.
- Ignore the key if it remains in an older config file.
- The next normal config save naturally omits the obsolete key.
- Do not expose a replacement setting in `SettingsDialog`.

This deliberately enforces the fresh-start behavior for both new and existing users, as approved.

No session data is deleted or migrated.

## 13. Error Handling

| Failure | Detection | User-visible behavior | Internal behavior |
|---|---|---|---|
| No history for current IDB | Empty metadata result | `No saved chats for this IDB yet.` | Normal state, no warning |
| Search has no matches | Filtered result empty | `No chats match your search.` | Preserve original entries for query reset |
| Missing/corrupt manifest | Existing manifest validation | Loading state remains until rebuild completes | Rebuild in worker |
| Corrupt session JSON in listing | Existing rebuild logic skips file | Entry omitted | Log diagnostic |
| Session deleted/corrupted after listing | `load_session()` returns `None` | `This chat is no longer available.` then refresh list | Do not create tab |
| Session belongs to another IDB | Post-load ID check | `This chat belongs to a different IDB.` | Return `WRONG_IDB` |
| Session contains no messages | Message-count/load validation | `This chat is empty and cannot be opened.` | Omit from future list |
| One corrupt message | Existing `Message.from_dict` tolerance | Open remaining valid messages | Log skipped message |
| Pending save takes too long | `SessionHistory.flush_saves(timeout=10.0)` raises `concurrent.futures.TimeoutError` in the dedicated history worker | Show error state: `Recent chats are still being saved.` with Retry | Convert to typed `SAVE_FLUSH_TIMEOUT`; do not return a potentially incomplete list |
| Background list/load exception | Outer worker boundary catches `Exception` | Error state with Retry | Enqueue exactly one typed failure containing safe exception type/message; never leave Loading indefinitely |
| IDB changes during metadata load | Generation/scope mismatch | No stale content appears | Drop late worker result |
| Tab closes during async restore | Existing restore cancellation/generation behavior | Tab closes normally | Cancel/drop stale restore |
| Panel shuts down during load | Shutdown invalidates generation and stops timer | No late UI access | Worker result is ignored |

Broad exceptions may be used only at the outer UI/background-worker boundary to prevent teardown crashes; storage and controller code should catch explicit exception types.

## 14. Testing Strategy

Development follows TDD: write failing behavior tests before implementation.

### 14.1 State/persistence tests

Extend `tests/agent/test_state.py` and focused history tests:

- Saving a session derives `description` from the first user message.
- Derived title collapses whitespace, strips unsafe markers, and truncates at the named limit.
- An explicit non-empty `description` supplied to the existing save API takes precedence over automatic derivation.
- A manifest with a version other than the new title-aware version triggers rebuild; the rebuilt manifest contains derived titles, and session JSON files remain byte-identical.
- Rebuild derives `updated_at` from file mtime.
- Empty sessions are excluded from History results.
- Current-IDB matching accepts equal `db_instance_id`.
- Current-IDB matching rejects a different `db_instance_id` even when paths are similar.
- Legacy entries without instance ID can match the normalized current path.
- The same filter predicate is applied after a stale-manifest rebuild.
- Direct storage calls reject `"../escape"`, `"..\\escape"`, absolute paths, `"."`, `".."`, empty IDs, IDs longer than 32 characters, embedded separators, NUL/control characters, and extension-bearing IDs before any filesystem operation; `load_session()` returns `None` and `delete_session()` is a no-op.
- A tampered session JSON whose internal `id` is invalid is omitted during manifest rebuild; an invalid pre-existing manifest key is omitted during listing.
- Missing/corrupt payloads return no session without escaping the history directory.
- Existing fork `.summary.json` files remain excluded.

### 14.2 Controller tests

Extend `tests/agent/test_session_controller.py`:

- Controller initialization produces exactly one empty session.
- `list_history_sessions(scope)` returns only current-IDB sessions and entries sorted by `updated_at` descending.
- `load_history_session(saved_id, scope)` returns `LOADED` with `result.session.id == saved_id` but does not mutate `_sessions`.
- `attach_history_session(result)` runs on the test main thread, inserts the loaded state under a new ephemeral `tab_id`, and preserves token usage and tool calls covered by the legacy restore tests.
- Pre-load dedup focuses an already-open persisted `SessionState.id` without submitting a worker.
- Post-load dedup rechecks after a concurrent winner attached the same persisted ID and returns `ALREADY_OPEN` with that winner's `tab_id`.
- A result captured for IDB A returns `STALE_SCOPE` and leaves `_sessions` unchanged after the controller switches to IDB B.
- A payload tampered after manifest listing to contain a different `db_instance_id` returns `WRONG_IDB` and does not mutate `_sessions`.
- Missing, corrupt, and empty sessions do not mutate `_sessions`.
- A restored session remains usable by the normal `start_agent()` path.
- `reset_for_new_file()` enqueues non-empty old sessions without blocking on disk I/O and leaves exactly one empty new session; flushing `_SAVE_EXECUTOR` later persists them.
- Empty drafts are never saved on close, reset, or shutdown.
- The existing tests for `restore_session()`, token-usage restoration, and tool-call restoration are rewritten against `load_history_session()` → `attach_history_session()` rather than deleted.

### 14.3 Configuration tests

Create or extend a focused core config test module:

- Loading an older config that contains `startup_restore_sessions="all"` succeeds.
- `hasattr(config, "startup_restore_sessions")` is false; the obsolete key cannot change startup behavior.
- Saving the loaded config omits `startup_restore_sessions` from serialized JSON.
- Validation succeeds without the removed field.
- A repository-source grep after implementation finds zero `startup_restore_sessions` references under `lucnhan/`; historical plan/spec text is excluded from this audit.

### 14.4 Panel/UI tests

Extend `tests/tools/test_panel_core.py` and add focused `HistoryPanel` tests:

- `_build_ui()` never calls restore APIs; spies on `SessionHistory.list_sessions`, `_read_manifest`, and session-directory scans record zero calls before the user opens History.
- Startup has exactly one `New Chat` tab even when saved sessions exist.
- `on_database_changed()` increments `_history_generation`, clears panel rows and `_pending_restore_messages`, discards queued old-scope results, and ends with exactly one `New Chat` tab.
- History button is always visible; its button and header-close paths call the shared right-panel coordinator and keep checked state synchronized.
- History and Mutation panels are mutually exclusive across History→Mutation, Mutation→History, and History→closed transitions.
- Clicking History returns before an injected blocking worker completes; `HistoryPanel.set_entries()` is called only by the main-thread queue drain, never by the worker.
- Opening History while the agent is idle still starts `_history_poll_timer` and applies a completed metadata result.
- Loading, empty, search-empty, populated, save-timeout, and generic error states expose the exact copy/actions from §6.3 and §13.
- Search is an immediate, case-insensitive, title-only filter over cached entries and performs no storage call.
- A stale list/load generation is ignored without mutating panel rows, `_sessions`, `_chat_views`, or `_pending_restore_messages`.
- A new session selection creates a tab and calls the async restore path.
- Pre-load and post-load duplicate selections focus the existing tab without adding a tab.
- A failed open displays a non-blocking error; NOT_FOUND refreshes the visible list, while WRONG_IDB does not attach.
- An injected worker exception always leaves Loading and shows Retry; clicking Retry increments generation and submits exactly one replacement request.
- Opening a large session and then switching IDB or closing its tab mid-render does not crash or render stale messages; existing `ChatView.shutdown()` generation cancellation is exercised.
- Closing a tab before lazy restore removes its `_pending_restore_messages` entry.
- Theme changes refresh the History panel in both IDA-native and Luc Nhan-owned themes.
- `shutdown()` sets `_history_poll_timer` to `None`, non-blockingly closes `_history_executor`, drains result references, and prevents late widget/signal access.

### 14.5 Integration regression

A host-stub integration scenario should cover:

1. Persist two non-empty sessions for IDB A and one for IDB B.
2. Construct the panel for IDB A.
3. Assert exactly one empty `New Chat` tab and zero payload loads.
4. Open History and assert only the two IDB A entries appear.
5. Select one; assert one additional tab and async restore.
6. Select it again; assert no duplicate tab.
7. Continue the session and save it.
8. Refresh History; assert it sorts first by updated time.
9. Switch to IDB B; assert exactly one empty `New Chat` tab.
10. Open History; assert only IDB B's entry appears.
11. Restart the panel; assert it again starts with one empty tab.

### 14.6 Performance checks

- Panel startup performs no session-directory scan and no session JSON load, verified with storage spies rather than wall-clock timing.
- A History click handler returns before a fake blocked worker is released and makes no synchronous storage call; avoid flaky absolute millisecond assertions in unit CI.
- The dedicated history executor and `_SAVE_EXECUTOR` are distinct objects. A test queues a save, opens History, and proves `flush_saves()` completes rather than self-deadlocking.
- Opening a 200-message session uses `restore_from_messages_async()` and preserves the existing background-restore responsiveness contract.
- Search over hundreds of metadata entries stays local; storage spies record no payload or manifest reads while the query changes.
- At a 320-pixel panel width, History and Mutation are never visible together, and showing History leaves a usable chat pane rather than allocating width to both hidden/visible side panels.

## 15. Implementation Touch Points

Expected files:

### New

- `lucnhan/ui/history_panel.py`
  - `HistoryPanel` widget.
  - Presentation/search/loading/error states only; PanelCore owns queue-polled request state.

- `lucnhan/state/history_types.py`
  - Frozen `SessionHistoryEntry`, `HistoryScope`, list/load/attach status enums, and typed result dataclasses shared by persistence/controller/UI without importing Qt.
  - Search/filter and row rendering.

- Focused test file for `HistoryPanel` if existing Qt test modules would become too large.

### Modified

- `lucnhan/state/history.py`
  - Safe session-ID validation at load/delete/validate/rebuild/list boundaries.
  - Title derivation/backfill.
  - `updated_at` projection.
  - Current-IDB filter predicate reuse.
  - Manifest version increment.

- `lucnhan/ui/session_controller_base.py`
  - List/open/deduplicate APIs for History.
  - Remove bulk startup-restore methods after call-site verification, or retain them only as deprecated compatibility APIs if an external obligation is found.

- `lucnhan/ui/panel_core.py`
  - Remove automatic restore from startup and IDB change.
  - Add History button/panel and right-panel coordinator.
  - Add dedicated history executor, bounded result queue, generation counter, and separate poll-timer lifecycle.
  - Open/focus selected session.
  - Reuse deferred async restore and clean pending payloads on tab close.
  - Invalidate History work during IDB change/shutdown.

- `lucnhan/core/config.py`
  - Remove obsolete `startup_restore_sessions` dataclass field, validation block, normalization branches, load-field entry, and load-time compatibility normalization; unknown legacy keys remain ignored by the normal loader.

- `lucnhan/ui/styles.py` or panel-local style helpers
  - History panel styles consistent with current theme architecture.

- Tests under `tests/agent/`, `tests/state/`, `tests/tools/`, or `tests/ui/` as appropriate.

- `CHANGELOG.md`, `AGENTS.md`, and user-facing development documentation where behavior is described.

## 16. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---:|---:|---|
| Old sessions show poor/empty titles | Medium | Medium | Manifest version bump and one-time title backfill |
| Manifest rebuild freezes IDA | Medium | High | Always list in a background Python worker; queue-polled main-thread application |
| Late history results from old IDB appear | Medium | High | Generation + current-scope check before applying results |
| Same session opens multiple times | Medium | Medium | Controller-level `session.id → tab_id` duplicate detection |
| Wrong-IDB session opened through stale/tampered row | Low | High | Post-load current-IDB revalidation |
| Path traversal through session ID | Low | High | Validate ID and enforce resolved path containment in `SessionHistory` |
| Side panels make chat too narrow | Medium | Low | Mutual exclusion between History and Mutation panels |
| Existing users expect auto-restore | Medium | Medium | Intentional breaking UX change; preserve all data and make History prominent |
| Removing restore code breaks hidden caller | Low | High | Grep all callers first; retain a deprecated method only if an external/internal contract is found |
| Search grows slow at very large scale | Low | Low | Metadata-only in-memory filtering; add model virtualization/pagination only after measured need |

## 17. Acceptance Criteria

The feature is complete only when all of the following are true:

1. Opening Luc Nhan with any amount of saved history shows exactly one empty `New Chat` tab.
2. No historical session payload is loaded during startup.
3. Switching IDBs leaves exactly one empty `New Chat` for the new IDB.
4. Chats for the previous IDB remain persisted.
5. History appears only after an explicit click on the History button.
6. The panel lists only sessions for the current IDB.
7. The list loads metadata without blocking the Qt main thread.
8. Search filters titles without reading full session payloads.
9. Selecting a session opens it as a normal continuable tab.
10. Selecting an already-open session focuses its tab and creates no duplicate.
11. Large sessions use the existing asynchronous restore path.
12. Missing/corrupt/wrong-IDB sessions do not create tabs and produce a clear non-blocking message.
13. Existing session JSON files require no destructive migration.
14. Legacy `startup_restore_sessions` values cannot re-enable automatic restore.
15. Empty drafts never appear in History.
16. All relevant automated tests pass and startup/history behavior is manually smoke-tested inside IDA Pro.

## 18. Deferred Follow-Ups

Potential later features, explicitly excluded from v1:

- Rename chat title.
- Delete with confirmation.
- Archive/restore.
- Pin/favorite.
- Cross-IDB or workspace-wide history.
- Read-only preview pane.
- Date/provider/model filters.
- Pagination/virtualized model for thousands of sessions.
- Keyboard shortcut and IDA menu action for History.

Each follow-up should be driven by observed usage rather than included speculatively in the first implementation.
