# Background Restore for Large Chat Sessions

**Date**: 2026-06-07
**Status**: Approved (pending user review of written spec)
**Author**: Brainstorming session with user

## Problem Statement

Luc Nhan persists chat sessions as JSON. When a user opens a large session
(50–200+ messages) or switches to an existing tab, `ChatView.restore_from_messages`
runs synchronously on the main thread. Profiling shows:

| Messages | Restore time (no paint) | With paint + event loop |
|---:|---:|---:|
| 30  | ~96 ms   | ~400 ms   |
| 50  | ~160 ms  | ~900 ms   |
| 100 | ~350 ms  | ~1,800 ms |
| 200 | ~706 ms  | ~3,500+ ms |

The dominant costs (in profiling) are:

1. **Widget construction** (~1–2 ms per `AssistantMessageWidget`,
   `ToolCallWidget`, etc.) — every widget needs a `QVBoxLayout`,
   `QLabel` for the header, and other children.
2. **Deferred markdown render** triggered by `showEvent` — `md_to_html`
   runs once per `AssistantMessageWidget` on first paint (correctly
   deferred, but still ~3–4 ms per message for typical content).
3. **Resize cascade** — every `insertWidget` fires `resizeEvent` on the
   `QScrollArea`. Already mitigated by `_in_restore` flag, but layout
   passes still happen.
4. **ProcessEvents in benches** — paint events cost 50%+ in synthetic
   tests. In production, the same paint work happens via Qt's normal
   event loop, just with a single visible freeze.

**Symptom**: switching to a tab with a 100+ message session visibly hangs
the UI for half a second to several seconds. The chat window does not
respond to mouse, scrolling, or keystrokes during the restore.

## Goals

1. **Tab switch perceived latency < 50 ms** for sessions of any size.
   User clicks the tab → chat viewport is responsive → content "fills
   in" without blocking input.
2. **Total restore time unchanged or improved**. Background work may
   take the same wall-clock as the current synchronous path, but it
   must not block the UI thread.
3. **No regressions in scroll, find, or accessibility** for restored
   sessions.
4. **Bounded memory** — the restore queue must not allow unbounded
   growth if the user closes tabs or scrolls away rapidly.

## Non-Goals

- Replacing `QScrollArea` with a true virtualizing list widget.
  (Considered but too high-risk for v1; lazy render inside the existing
  scroll area gives 90% of the benefit with 10% of the code.)
- Persisting widget trees across app restarts. Sessions are
  reloaded from JSON on each open.
- Optimizing the in-progress streaming path (turn-by-turn message
  arrival). That path is already incremental and not the bottleneck.

## Design

### Overview

`restore_from_messages` is split into two phases:

1. **Background phase (worker thread)**: a `QThread` worker walks the
   `list[Message]`, builds lightweight frozen `MessageSpec` dataclasses,
   pre-renders markdown to HTML via `md_to_html` (which is pure Python
   and Qt-free), and computes an estimated pixel height per message.
   Specs are emitted to the main thread in chunks of 20 via signals.
2. **Main-thread phase (incremental)**: for each `MessageSpec` the
   main thread inserts a lightweight `MessagePlaceholder` widget with
   the estimated height. A viewport-aware pass replaces placeholders
   inside the scroll viewport (and a small buffer above/below) with
   real `AssistantMessageWidget` / `ToolCallWidget` instances.

The key property: **all heavy work that does not require Qt runs off
the UI thread** (markdown render, JSON serialization, height
estimation), and **all Qt widget operations stay on the UI thread**
(construction, layout, paint). The thread boundary is a one-way
stream of immutable `MessageSpec` objects, which is safe to share
without locks because frozen dataclasses are effectively immutable.

### Components

#### 1. `MessageSpec` (frozen dataclass, in `lucnhan/ui/chat_view.py`)

```python
@dataclass(frozen=True)
class ToolSpec:
    id: str
    name: str
    arguments_json: str  # pre-serialized via json.dumps
    # Estimated pixel height of the rendered ToolCallWidget when collapsed.
    # Pre-computed in the worker so MessageSpec.estimated_height can sum
    # child heights without re-iterating the spec.
    estimated_height: int

@dataclass(frozen=True)
class ToolResultSpec:
    tool_call_id: str
    name: str
    content: str
    is_error: bool

@dataclass(frozen=True)
class MessageSpec:
    msg_id: str
    role: Role
    content: str  # raw markdown
    rendered_html: str | None  # pre-rendered, or None if no content
    tool_calls: tuple[ToolSpec, ...]
    tool_results: tuple[ToolResultSpec, ...]
    estimated_height: int  # pixels, used for placeholder
    # True for messages whose metadata marks them as internal (not for LLM).
    # UI must skip rendering for these (defensive — restored sessions
    # should not normally contain them).
    hidden: bool
```

`MessageSpec` is **frozen** and contains only primitive types or
frozen nested dataclasses, so it can be safely passed across thread
boundaries. (The codebase already uses `Signal(object)` for `ThemeTokens`
in `theme/manager.py:246`, confirming PySide6's queued-connection
serialization handles frozen dataclasses without metatype registration.)

#### 2. `ChatRestoreWorker(QThread)` (new file `lucnhan/ui/chat_restore_worker.py`)

```python
class ChatRestoreWorker(QThread):
    # A batch of MessageSpec tuples. The receiver installs all
    # placeholders in one layout pass, then triggers one viewport
    # render. This avoids the per-spec insertion + repaint storm that
    # would happen if we emitted one signal per spec.
    chunk_ready = Signal(tuple)  # tuple[MessageSpec, ...]
    restore_finished = Signal()
    restore_error = Signal(str)
    progress = Signal(int, int)  # (done, total) — for optional UI

    def __init__(self, messages: list[Message], chunk_size: int = 20):
        super().__init__()
        # Copy the list. Worker only reads `messages`; main thread
        # may free the original.
        self._messages = list(messages)
        self._chunk_size = chunk_size
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            specs: list[MessageSpec] = []
            total = len(self._messages)
            done = 0
            for msg in self._messages:
                if self._cancelled:
                    return
                specs.append(_build_spec(msg))
                done += 1
                if len(specs) >= self._chunk_size:
                    self.chunk_ready.emit(tuple(specs))
                    self.progress.emit(done, total)
                    specs.clear()
            if specs:
                self.chunk_ready.emit(tuple(specs))
                self.progress.emit(total, total)
            self.restore_finished.emit()
        except Exception as exc:
            self.restore_error.emit(str(exc))
```

`_build_spec` is a pure function: it calls `md_to_html`, `json.dumps`,
and the height estimator. It is exported alongside the worker so it
can be unit-tested without spinning up a thread.

#### 3. `MessagePlaceholder` (new widget, in `lucnhan/ui/chat_view.py`)

A lightweight `QFrame` that:

- Renders a subtle background (`theme.alt_base`) sized to
  `spec.estimated_height`.
- Shows a small "Loading..." text in the top-right corner
  (`theme.mid` color, 9px) — only visible while its message is
  not yet rendered, and hidden via `setVisible(False)` once swapped.
- Has no children that would trigger expensive paint or layout.

This is the placeholder inserted into the layout for every message
in a large session, regardless of whether the user will ever scroll
to it. Because it is so cheap, the cost of inserting 200 of them is
small (~50–100 ms total) and runs in small batches with main-thread
yields between them.

#### 4. Height estimator (function, in `lucnhan/ui/chat_view.py`)

```python
_CHARS_PER_LINE = 80
_LINE_HEIGHT_PX = 18
_CODE_LINE_HEIGHT_PX = 16
_MESSAGE_HEADER_PX = 32
_TOOL_WIDGET_PX = 90

def _estimate_tool_height(tool: ToolSpec) -> int:
    """Estimated pixel height of one ToolCallWidget when collapsed."""
    # args_json length drives the height of a collapsed tool widget
    args_lines = max(2, tool.arguments_json.count("\n") + 2)
    return min(_TOOL_WIDGET_PX, _TOOL_WIDGET_PX + args_lines * 4)

def _estimate_message_height(spec: MessageSpec) -> int:
    if not spec.content and not spec.tool_calls:
        return _MESSAGE_HEADER_PX
    height = _MESSAGE_HEADER_PX
    if spec.content:
        # Approximate: chars / chars-per-line, minimum 1 line
        text_lines = max(1, (len(spec.content) + _CHARS_PER_LINE - 1)
                         // _CHARS_PER_LINE)
        # Count ```-fenced code blocks; they render ~40% taller
        code_blocks = spec.content.count("```") // 2
        height += text_lines * _LINE_HEIGHT_PX
        height += code_blocks * 5 * _CODE_LINE_HEIGHT_PX
    for tc in spec.tool_calls:
        height += tc.estimated_height
    return int(height)
```

`_build_spec` calls `_estimate_tool_height` per tool call, then sums
them via `_estimate_message_height`. The per-tool value is stored on
`ToolSpec` so the height is computed exactly once per tool, and
`MessageSpec.estimated_height` is the sum.

The estimate is **approximate** — empirical tests show ±20% error on
realistic content, which is acceptable. A future improvement can
fine-tune constants from real session data.

#### 5. `ChatView` changes (in `lucnhan/ui/chat_view.py`)

New attributes:

```python
self._worker: ChatRestoreWorker | None = None
self._worker_generation: int = 0  # bumped on every new restore, so
                                 # late signals from an old worker
                                 # are detected and dropped
self._placeholders: dict[str, MessagePlaceholder] = {}  # msg_id → widget
self._specs: dict[str, MessageSpec] = {}  # msg_id → spec
self._rendered: set[str] = set()  # msg_ids with real widgets
self._restore_in_progress: bool = False
self._scroll_listener_installed: bool = False
```

New public method:

```python
def restore_from_messages_async(self, messages: list[Message]) -> None:
    """Start a background restore. Replaces restore_from_messages for
    large sessions; the old method remains as a synchronous fallback
    used by tests and for small message counts."""
```

Internal flow:

```
restore_from_messages_async(messages)
  ├─ self.clear_chat()
  ├─ self._restore_in_progress = True
  ├─ self._worker_generation += 1  # invalidate any in-flight signals
  ├─ self._my_generation = self._worker_generation  # captured by slots
  ├─ self._worker = ChatRestoreWorker(messages)
  ├─ self._worker.chunk_ready.connect(self._on_chunk_ready)
  ├─ self._worker.restore_finished.connect(self._on_restore_finished)
  ├─ self._worker.restore_error.connect(self._on_restore_error)
  ├─ self._worker.finished.connect(self._on_worker_finished)
  ├─ self._worker.start()
  └─ return immediately  # tab switch is non-blocking

_on_chunk_ready(specs)
  ├─ If self._my_generation != self._worker_generation: return  # stale
  ├─ For each spec in specs:
  │   ├─ self._specs[spec.msg_id] = spec
  │   ├─ placeholder = MessagePlaceholder(spec)
  │   ├─ self._placeholders[spec.msg_id] = placeholder
  │   └─ self._insert_widget(placeholder)  # existing helper, respects _in_restore
  ├─ self._messages_layout.invalidate()  # batch one layout pass for all
  ├─ self._messages_layout.activate()    # placeholders
  ├─ self._widget.verticalScrollBar().setMaximum(self._total_estimated_height())
  ├─ processEvents once so paint can flush
  └─ Trigger _ensure_viewport_rendered()

_on_restore_finished()
  ├─ If self._my_generation != self._worker_generation: return  # stale
  ├─ _ensure_viewport_rendered()  # final pass
  ├─ _scroll_to_bottom()
  ├─ self._restore_in_progress = False
  └─ _maybe_cleanup_worker()

_on_worker_finished()
  └─ self._worker = None  # free QThread resources

_ensure_viewport_rendered()
  ├─ If a viewport pass was run in the last 50ms, return (debounce)
  ├─ Force the viewport's layout to compute current geometry: call
  │   self._messages_layout.activate() and read the scrollbar value
  │   (a placeholder's geometry from the previous pass may be stale
  │   because Qt defers layout until the next event-loop iteration).
  ├─ For each placeholder in the layout, if its msg_id not in
  │   self._rendered AND its position is within viewport (with
  │   ±2-screen buffer above/below), replace it with the real widget
  └─ Mark the msg_ids as rendered
```

**Scroll-triggered rendering**: a one-shot `QScrollBar.valueChanged`
connection (installed lazily on first restore) calls
`_ensure_viewport_rendered()` after a 16 ms debounce, so scrolling
near the edges of the rendered region triggers the next batch.

**Synchronous fallback**: `restore_from_messages(messages)` continues
to exist and behaves exactly as today. `restore_from_messages_async`
is the new entry point. Call sites that currently call
`restore_from_messages` are updated to call
`restore_from_messages_async` **only when** the message list is long
(>20 messages). Short sessions are kept synchronous — the overhead of
spinning up a worker thread isn't worth it for 5-message sessions.

**From-scratch semantics**: `restore_from_messages_async` always
clears the existing tab before starting the new restore. It does
**not** merge with the current message list. Callers that want to
append must collect the current messages, append, and re-restore.
This matches `restore_from_messages` today.

#### 6. Scrollbar accuracy

The scrollbar needs an accurate `maximum()` so the user can scroll
smoothly. We use the **estimated heights** for the placeholder
positions, so the scrollbar is correct from the moment all placeholders
are inserted. When a placeholder is replaced by the real widget, the
real widget's actual height may differ from the estimate. We handle
this with `QWidget.setMinimumHeight(actual)` and `setMaximumHeight(actual)`
on the new widget **after** the first `showEvent`, which lets the
layout settle to the true height. In practice, the layout only
re-computes below the changed widget, so this causes a one-time
small jump as a single message renders. To minimize visible jumps:

- The viewport is rendered first, so the user is already looking at
  the affected area.
- The estimator is tuned to overestimate slightly, so the
  post-render adjustment is usually a shrink (less jarring than a
  grow).

If post-render jumps prove noticeable in practice, a follow-up can
add a `QTimer.singleShot(0, ...)` measurement pass to pre-size the
real widget to match the placeholder's height.

### Data Flow

```
┌─────────────────────────┐         ┌─────────────────────────┐
│   Worker Thread         │         │   Main Thread           │
│                         │         │                         │
│  _build_spec(msg) × N   │ chunk   │  _on_chunk_ready(specs) │
│   ├─ md_to_html         │ ──────→ │   ├─ N × insertWidget   │
│   ├─ json.dumps         │  tuple  │   ├─ layout.activate()  │
│   └─ _estimate_height   │         │   ├─ setMaximum(scroll) │
│                         │         │   └─ ensure viewport    │
│                         │ finish  │                         │
│                         │ ──────→ │  _on_restore_finished   │
│                         │         │   └─ final pass         │
└─────────────────────────┘         └─────────────────────────┘
```

### Thread Safety

- `MessageSpec` is `@dataclass(frozen=True)` and contains only
  primitives and tuples of frozen dataclasses. It is safe to share
  across threads.
- `MessagePlaceholder` is **only constructed and mutated on the
  main thread**. The worker does not touch Qt objects.
- The worker receives a `list[Message]` copy in its constructor.
  The caller (main thread) is free to drop the original after
  `start()` returns.
- Signals are the only communication channel. Qt's queued
  connection semantics ensure `_on_chunk_ready` runs on the main
  thread even if the worker emits from its own thread.
- If a new restore starts before the previous worker finished (rapid
  tab switching), the previous worker's signals are dropped by the
  generation check (`self._my_generation != self._worker_generation`).
  The previous worker is cancelled via `cancel()` and `wait(2000)`.

### Error Handling

| Failure | Detection | Behavior |
|---|---|---|
| Worker thread exception | `try/except` in `run()` | Emit `restore_error`; main thread shows error toast in status bar; placeholders remain (user can scroll an empty session). |
| `md_to_html` raises for one message | `try/except` in `_build_spec` | Spec gets `rendered_html=None`; main thread falls back to plain-text render via `QLabel.setText`. |
| Tab closed during restore | `tabRemoved` signal from `QTabWidget` | Calls `self._worker.cancel()` and `self._worker.wait(2000)`. Worker checks `_cancelled` between specs and exits cleanly. |
| App quit during restore | `QApplication.aboutToQuit` | Same as tab closed; `QThread.quitOnDelete = True` is set as a backup. |
| Widget creation fails (extremely rare) | `try/except` in `_replace_placeholder` | The placeholder remains; logged via `log_error`. User can see the message exists. |
| `restore_from_messages_async` called while a previous restore is in progress | First call still running | Cancel the previous worker (with 1 s grace), then start the new one. |

### Backward Compatibility

`restore_from_messages` is preserved as a synchronous method. Existing
callers are migrated incrementally:

- `panel_core.py:776` (fork tab) — calls `restore_from_messages` with
  messages copied from another tab. **Keep synchronous** (typically
  small).
- `panel_core.py:943` (deferred restore on tab show) — uses
  `_restore_messages_if_needed`. **Migrate to async** for any session
  > 20 messages.
- `panel_core.py:1316` (legacy single-session restore on startup) —
  **Migrate to async** (startup is exactly when perceived latency
  matters most).

The threshold (20 messages) is a constant in `panel_core.py` that can
be tuned later. Below 20 messages, the synchronous path is fast enough
that the worker overhead would be a net loss.

## Testing

### Unit tests (in `tests/ui/test_chat_restore.py`)

- `MessageSpec` is frozen (frozen=True prevents setattr).
- `_build_spec` produces a spec with correct `role`, `content`,
  `rendered_html` (verify HTML is non-empty for markdown content),
  `tool_calls` count, and `estimated_height` > 0.
- `_estimate_message_height` returns reasonable values for:
  - empty message (just header)
  - one-line text
  - multi-line text with code block
  - 5 tool calls
- `_build_spec` recovers from `md_to_html` raising (returns spec
  with `rendered_html=None`).
- `ChatRestoreWorker.cancel()` causes the worker to exit early
  when called between chunks (verified by counting emitted specs).
- Signal ordering: `chunk_ready` × N (with the right tuple size),
  then `restore_finished` only after the final batch.
- Viewport accuracy: with a 200-message session and a 5-message
  viewport, after `restore_finished` exactly the 5 in-viewport
  messages are real widgets and the rest are still placeholders.
  This catches the "stale layout" bug where `_ensure_viewport_rendered`
  reads the placeholder geometry from before the layout pass
  (fix is in the helper: call `layout.activate()` and
  `processEvents()` first).
- Scrollbar accuracy: after `restore_finished`, the scrollbar's
  `maximum()` is within 5% of the sum of estimated heights.

### Bench tests (extend `bench_restore_chatview.py`)

- `tab_switch_ms`: time from `restore_from_messages_async` call
  to return. **Target: < 50 ms** (currently 700–1800 ms for 200
  messages).
- `bg_restore_ms`: time from worker `start()` to
  `restore_finished`. **Target: ≤ synchronous restore time**
  (we shouldn't make total restore slower).
- `viewport_rendered_ms`: time from first `restore_finished` to
  the viewport region being fully rendered. **Target: < 100 ms**
  (because the chunked rendering already overlaps with the
  background work, the main-thread cost should be small).
- `scroll_smoothness`: simulate scroll events, measure time
  between frames when entering an unrendered region. **Target:
  no frame > 50 ms**.

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Height estimate off by >30% causes visible scrollbar jumps | Medium | Low (annoying) | Overestimate slightly; tune constants; first viewport pass happens before user scrolls. |
| Worker thread leaked if app crashes | Low | Medium | `quitOnDelete = True`; `finished` signal cleans up reference; tab close explicitly cancels. |
| Markdown render on worker thread breaks if `md_to_html` is ever changed to use Qt (e.g. for font metrics) | Low | High | Keep `_build_spec` pure; add a comment / assertion that `md_to_html` is Qt-free; unit test that builds specs in a thread. |
| Existing callers of `restore_from_messages` break | Low | High | Keep the synchronous method working; migrate call sites one at a time; add a runtime warning if `restore_from_messages` is called with > 50 messages. |
| Placeholder inserted for messages that scroll off-screen never get rendered (memory) | Medium | Low | `MessagePlaceholder` is a small QFrame (~few hundred bytes). 200 of them is < 100 KB. |
| Spec queue grows unboundedly if main thread is starved | Low | Medium | Worker emits in chunks; main thread is the bottleneck; chunks are sized (20) to drain quickly. |
| Race: user types in input area while restore is in progress | Low | None | Input area is a separate widget outside `ChatView`. Restore only modifies the chat scroll area. |

## Open Questions

1. Should we expose a progress bar in the tab label during restore
   (e.g. "Chat (loading…)" or "Chat (50/200)")? Currently the design
   says no, but it might be nice for very large sessions.
2. Should we pre-render the *most recent* (last in the list)
   messages first, so a user opening a long session and immediately
   scrolling to the bottom gets content fastest? Currently chunks
   arrive in order.
3. Should we also defer `ToolCallWidget.set_arguments` JSON parse
   until the widget is rendered? The args JSON is small enough
   that this is probably unnecessary.

## Implementation Order

1. Add `MessageSpec`, `ToolSpec`, `ToolResultSpec`, `_build_spec`,
   `_estimate_message_height`, `MessagePlaceholder` (no thread yet).
   Unit tests pass.
2. Add `ChatRestoreWorker` with signals. Unit tests pass.
3. Add `restore_from_messages_async` to `ChatView`; wire signals;
   verify synchronous portion returns quickly. Integration tests
   pass for the viewport-render case.
4. Add scroll-triggered rendering. Integration tests pass.
5. Migrate call sites in `panel_core.py`. Manual smoke test in
   IDA with a real large session.
6. Add bench tests; verify targets met on real hardware.
7. Add progress reporting (optional, based on user feedback from
   step 5).

## Files Touched

- `lucnhan/ui/chat_view.py` — add `MessageSpec`, `MessagePlaceholder`,
  `_build_spec`, `_estimate_message_height`, `restore_from_messages_async`,
  `_on_spec_ready`, `_on_chunk_finished`, `_on_restore_finished`,
  `_on_restore_error`, `_on_worker_finished`,
  `_ensure_viewport_rendered`, scroll listener.
- `lucnhan/ui/chat_restore_worker.py` — new file: `ChatRestoreWorker`.
- `lucnhan/ui/panel_core.py` — migrate 2 of 3 call sites
  (`_restore_messages_if_needed` and the legacy restore).
  Add the threshold constant and the migration logic.
- `tests/ui/test_chat_restore.py` — new file: unit tests.
- `tests/ui/test_chat_restore_integration.py` — new file: integration
  tests.
- `bench_restore_chatview.py` — extend with new measurements.
