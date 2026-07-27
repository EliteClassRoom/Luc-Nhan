# Implementation Plan: Multi-Tab Parallel Agent

> Goal: allow multiple chat tabs to run their agents **concurrently** instead of
> the current "one agent at a time, switching tabs cancels the running agent".
>
> Status: research-confirmed feasible. The bulk-renamer deep mode already runs
> N parallel `SubagentRunner` instances sharing one `ToolRegistry`, serialized
> through IDA's `execute_sync(MFF_WRITE)`. This plan reuses that proven pattern.

---

## 0. Scope & non-goals

**In scope**

- Multiple `BackgroundAgentRunner` instances alive simultaneously (one per tab).
- Per-tab event streaming, per-tab approval state, per-tab pending-message queue.
- Tab switching does **not** cancel a running agent.
- A concurrency cap (default 3, mirroring `bulk_renamer_max_concurrent`).

**Non-goals (explicitly deferred)**

- Per-tab undo isolation — phase 2 uses a global mutating lock so the undo
  stack stays coherent across tabs. Full per-tab undo segmentation is a later,
  separate effort.
- Running agents against *different* IDBs in the same panel (one IDB per panel,
  unchanged).
- Headless mode changes — headless is single-run by design (`ask` / `serve`);
  not touched here. **Backward-compat contract (B1):** the public lifecycle
  API (`get_runner`, `is_agent_running`, `cancel`, `get_event`) keeps
  zero-arg defaults that target the active tab. Headless/control call sites
  (`headless/runner.py`, `control/server.py`) stay untouched.

---

## 1. Architecture: what changes, what doesn't

### Does NOT change (already multi-tab capable)

- `RikuganPanelCore._chat_views: dict[str, ChatView]` — one ChatView per tab. ✅
- `SessionControllerBase._sessions: dict[str, SessionState]` — one session per tab. ✅
- `BackgroundAgentRunner` — already self-contained (own thread + own bounded
  `event_queue`). Multiple instances are independent by construction. ✅
- `AgentLoop` — already created fresh per run (`start_agent` builds a new one
  each call, wires its own memory service). ✅
- Shared singletons stay shared: `ToolRegistry`, `ProviderRegistry`,
  `SkillRegistry`, `MCPManager` — same as how `SubagentRunner` shares them. ✅

### DOES change (the single-runner bottleneck)

Three files only:

| File | Change |
| ------ | -------- |
| `rikugan/ui/session_controller_base.py` | `self._runner` (single) → `self._runners: dict[str, BackgroundAgentRunner]` (per tab). All lifecycle methods become tab-aware. |
| `rikugan/ui/panel_core.py` | `_poll_events` drains **all** running tabs (not just active). Approval/pending-answer state becomes per-tab. `_set_running` reflects "any tab running". |
| `rikugan/tools/registry.py` | Add a `_mutate_lock` (phase 2) so mutating tools serialize → keeps the undo stack coherent across concurrent agents. |

The IDA host controller (`rikugan/ida/ui/session_controller.py`) overrides **none**
of the lifecycle methods — confirmed. So no host-side changes needed.

---

## 2. Data-model changes

### 2.1 `SessionControllerBase`

```python
# BEFORE (session_controller_base.py:145)
self._runner: BackgroundAgentRunner | None = None
self._pending_messages: list[str] = []          # global queue

# AFTER
self._runners: dict[str, BackgroundAgentRunner] = {}          # tab_id -> runner
self._pending_messages: dict[str, list[str]] = {}             # tab_id -> queue
self._max_concurrent_agents: int = 3                          # cap (config-driven)
```

`_pending_messages` moves from a flat list to a per-tab dict so a queued
follow-up in tab A doesn't leak into tab B. `_pending_messages.get(tab_id, [])`.

### 2.2 `RikuganPanelCore`

```python
# BEFORE (panel_core.py:249)
self._polling = False
self._pending_answer = False              # global
self._awaiting_button_approval = False    # global

# AFTER
self._pending_answer_tab: str | None = None          # which tab awaits an answer
self._awaiting_approval_tab: str | None = None       # which tab blocks free-text
# self._polling stays a single re-entrancy guard (one QTimer, drains all tabs)
```

`_set_running` keeps its current shape but is driven by
`any(... is_running)` so the Send→Queue/Cancel button reflects global activity.

---

## 3. Phase 1 — Read-only parallel (safe, ships value)

Phase 1 delivers concurrent agents for the common case (analysis, Q&A,
exploration). Mutating serialization is phase 2 but the lock is added in phase 1
so behavior is correct from day one — it just rarely contends.

### Step 1.1 — `SessionControllerBase`: runner dict + tab-scoped lifecycle

Rewrite these methods (all in `session_controller_base.py`):

| Method | New behavior |
| -------- | -------------- |
| `start_agent(user_message)` | Operate on `active_tab_id`. Create runner, store in `self._runners[tab_id]`. **Enforce cap**: if `len(self._runners) >= self._max_concurrent_agents`, queue the message (return a sentinel) instead of starting — caller shows it as queued. |
| `get_runner(tab_id=None)` | Return `self._runners.get(tab_id or self._active_tab_id)`. |
| `get_event(tab_id, timeout)` | Read from `self._runners[tab_id].get_event(timeout)`. (New signature; takes tab_id.) |
| `iter_runners()` | New generator: `for tab_id, runner in self._runners.items()`. Used by the poll loop. |
| `is_agent_running` (property) | `any(r.agent_loop.is_running for r in self._runners.values())`. Add `is_tab_running(tab_id)` for per-tab checks. |
| `cancel(tab_id=None)` | `tab_id=None` cancels active tab; cancel only that runner + clear that tab's pending queue. Add `cancel_all()` for shutdown. |
| `on_agent_finished(tab_id)` | Takes tab_id. Pops runner from dict, saves that tab's session, returns next queued message **for that tab**. |
| `switch_tab(tab_id)` | **Remove** the `if self.is_agent_running: self.cancel()` line. Just switch active. |
| `new_chat()` | Clear only active tab's pending queue. |
| `close_tab(tab_id)` | If that tab has a running agent, cancel it first, then remove session. |
| `shutdown()` | Iterate `self._runners` and cancel each. |
| `queue_message(text)` | Append to `self._pending_messages[active_tab_id]`. |

**Concurrency cap helper:**

```python
def _acquire_slot(self) -> bool:
    """True if a new agent can start now; False -> caller should queue."""
    running = sum(1 for r in self._runners.values() if r.agent_loop.is_running)
    return running < self._max_concurrent_agents
```

The panel, when a slot frees (a tab finishes), should start the oldest queued
message in any tab (FIFO across tabs) — see Step 1.4.

### Step 1.2 — `RikuganPanelCore._poll_events`: drain all tabs

Current code (panel_core.py:1669) polls only the active runner. Rewrite to
round-robin every runner and route each event to **its own tab's ChatView**:

```python
def _poll_events(self) -> None:
    if self._polling or self._is_shutdown:
        return
    self._polling = True
    try:
        for tab_id, runner in self._ctrl.iter_runners():
            cv = self._chat_views.get(tab_id)
            if cv is None:
                continue
            container = cv._container
            container.setUpdatesEnabled(False)
            try:
                for _ in range(30):
                    event = self._ctrl.get_event(tab_id, timeout=0)
                    if event is None:
                        if not runner.agent_loop.is_running:
                            self._on_agent_finished(tab_id)
                        break
                    self._on_event(event, tab_id)   # route to correct view
            finally:
                container.setUpdatesEnabled(True)
    finally:
        self._polling = False
```

**Critical:** events MUST go to `self._chat_views[tab_id]`, not
`self._active_chat_view()`. The bug to avoid: agent in tab B emits TEXT while
the user is looking at tab A — currently `_on_event` writes to the active view.
The fix is the `tab_id` parameter threaded into `_on_event`.

### Step 1.3 — Per-tab approval routing (fixes B2, C1, C2)

`_on_event(event, tab_id)` replaces `_on_event(event)`. The approval signals
carry no `tab_id` (`chat_view.py:495-496`), so bind it at connect time
(`_create_tab`/`_rebuild_history_tab`):

```python
# panel_core.py _create_tab — bind tab_id via closure (B2)
chat_view.tool_approval_submitted.connect(
    lambda tcid, d, tid=tab_id: self._on_tool_approval(tcid, d, tid)
)
chat_view.user_answer_submitted.connect(
    lambda ans, tid=tab_id: self._on_user_answer_submitted(ans, tid)
)
# orchestra path has the identical bug — bind it too
chat_view.orchestra_approval_decided.connect(
    lambda tcid, d, tid=tab_id: self._on_orchestra_approval(tcid, d, tid)
)
```

`_on_tool_approval(tcid, decision, tab_id)` / `_on_user_answer_submitted(answer, tab_id)`
route to `self._ctrl.get_runner(tab_id).agent_loop.submit_*`.

State tracking:

- `USER_QUESTION` / `SAVE_APPROVAL_REQUEST` / `PLAN_GENERATED` set
  `self._pending_answer_tab = tab_id` and `self._awaiting_approval_tab = tab_id`
  (if buttons-only).

**C1 — input area is a single global widget.** Input-disable stays **global**:
when any tab awaits a button-only approval, `_input_area.set_enabled(False)`
panel-wide (current behavior). Per-tab-ness applies only to answer *routing*
and the visual badge (§1.5), NOT to input enabling.

**C2 — free-text USER_QUESTION (allow_text).** In `_on_submit`, when
`self._pending_answer_tab` is set, route the typed answer to that tab's runner,
not the active one: `runner = self._ctrl.get_runner(self._pending_answer_tab)`.

### Step 1.4 — Cross-tab queue draining

When a tab finishes (`_on_agent_finished(tab_id)`), check if the global cap now
has a free slot AND some other tab has a queued message. Start the oldest queued
one. Simplest: a single FIFO `_drain_queue()` called after every finish.

**B3 — `_on_submit` must use per-tab running check.** The queue-vs-start
decision in `_on_submit` currently reads `self._ctrl.is_agent_running` (ANY
agent). Change it to `self._ctrl.is_tab_running(active_tab_id)` so a fresh
message in an idle tab A starts immediately even while tab B runs in the
background.

```python
def _on_agent_finished(self, tab_id: str) -> None:
    next_msg = self._ctrl.on_agent_finished(tab_id)   # pops runner, saves session
    # per-tab follow-up:
    if next_msg and self._chat_views.get(tab_id) is not None:
        self._chat_views[tab_id].pop_first_queued_message()
        self._start_agent_for_tab(tab_id, next_msg)
        return
    # else: idle; let any other tab's queued message take the freed slot
    self._maybe_drain_global_queue()
    self._refresh_running_state()
```

### Step 1.5 — Per-tab "running" indicator

`_set_running` currently flips global Send/Cancel buttons. With multi-tab,

- The **active tab's** ChatView shows its own spinner (ChatView already has a
  spinner concept — `_SharedSpinnerTimer`).
- The global Send button shows "Queue"/"Send" based on `is_tab_running(active)`
  OR `is_agent_running` (any). Cancel cancels the active tab.
- A tab that is running in the background shows a small dot/badge on its tab
  label (QTabBar) so the user knows work is happening off-screen.

Add `self._busy_tabs: set[str]` and update tab labels in `_update_tab_label`.

**C3 — token display.** `_update_token_display` is driven only by the
**active tab's** events: in `_on_event`, skip the token update when `tab_id !=
active_tab_id`. (Or keep per-tab last-usage and show the active tab's; the
former is the ponytail choice — one guard line.)

### Step 1.6 — Tests

`tests/` already stubs the IDA API. Add:

- `tests/test_multitab_runner.py`:
  - Start 2 agents on 2 tabs → both runners exist, both threads alive.
  - Events from tab A route to tab A's ChatView (use a fake ChatView recording
    `handle_event` calls).
  - `switch_tab` does NOT cancel a running agent (assert runner still in dict).
  - Concurrency cap: start 4 tabs with cap=3 → 4th is queued, starts when one
    finishes.
  - `close_tab` cancels that tab's agent.
  - `shutdown` cancels all.

---

## 4. Phase 2 — Mutating serialization (correct undo under concurrency)

### Step 2.1 — `ToolRegistry` mutate lock

```python
# registry.py
class ToolRegistry:
    def __init__(self, ...):
        ...
        self._mutate_lock = threading.Lock()   # ponytail: global lock; per-tool
                                               # locks if throughput matters

    def execute(self, name, arguments):
        ...
        is_mutating = defn.mutating
        ...
        try:
            if is_mutating:
                with self._mutate_lock:                       # serialize writes
                    future = _executor.submit(handler, **arguments)
                    result = future.result(timeout=timeout)
            else:
                future = _executor.submit(handler, **arguments)
                result = future.result(timeout=timeout)
        except ...
        # cache invalidate stays as-is (already whole-cache on mutate)
```

This guarantees:

- Two agents calling `rename_function` at once → one waits, the other commits.
- The `mutation.py` undo record for each is captured against a stable pre-state.
- `_result_cache` invalidation (already whole-cache) stays correct.

### Step 2.2 — Undo scope note (no code change, document it)

`/undo` remains global (undoes the most recent mutation across all tabs).
This is the **accepted phase-2 trade-off**: coherent, just cross-tab. A
follow-up could add `/undo --tab` later. Note this in the CHANGELOG and the
mutation panel tooltip.

---

## 5. Config additions

`rikugan/core/config.py` (`RikuganConfig` dataclass):

```python
parallel_agent_enabled: bool = True
parallel_agent_max_concurrent: int = 3
```

- Wire into `load()`/`validate()`/`save()` (follow the existing `bulk_renamer_*`
  serialization list pattern at config.py:337-338).
- When `parallel_agent_enabled=False`, fall back to current behavior (cap=1,
  switch cancels) — gives a safe escape hatch and an easy A/B for regression.

Settings UI (`settings_dialog.py _build_behavior_group`): a spinbox for
max-concurrent + a checkbox to enable/disable, mirroring the bulk-renamer UI.

---

## 6. Cancellation & threading safety checklist

- [ ] `_check_cancelled()` is per-`AgentLoop` (it already reads
      `self._cancelled`, an event set on the loop). Cancel sets that event —
      already correct, just routed by tab now.
- [ ] No Qt signals across threads — confirmed, events travel via
      `queue.Queue` → `QTimer._poll_events`. Unchanged pattern, now draining N queues.
- [ ] `_poll_timer` stays a single QTimer (50ms) draining all tabs — one timer,
      no per-tab timer proliferation.
- [ ] The shared `_executor` (ThreadPoolExecutor max_workers=2 in registry.py:23)
      handles tool timeout wrapping; with the mutate lock, concurrent mutating
      tools block there safely (worker count 2 ≥ fine; read tools don't contend).
- [ ] IDA `execute_sync(MFF_WRITE)` already serializes main-thread IDA calls —
      no new main-thread contention beyond what bulk-renamer already exercises.

---

## 7. Risks & mitigations

| Risk | Severity | Mitigation |
| ------ | ---------- | ------------ |
| Event routed to wrong ChatView (active instead of emitting tab) | 🔴 High | Step 1.2/1.3 thread `tab_id` into `_on_event`. Covered by test "events route to source tab". |
| Undo interleaving across tabs | 🟡 Med | Phase-2 global mutate lock keeps records coherent; document `/undo` as global. |
| Token/cost / rate-limit double-spend | 🟡 Med | Concurrency cap (default 3); user-configurable. |
| Approval UI from background tab blocks input globally | 🟡 Med | Per-tab approval state (1.3); only the awaiting tab disables *its* input. Background-tab approvals surface as a tab badge. |
| `_result_cache` staleness between tabs | 🟢 Low | Already whole-cache-invalidated on mutate. |
| Regression in single-tab use | 🟢 Low | `parallel_agent_enabled=False` fallback = exact current behavior. |

---

## 8. Implementation order (sliceable)

1. **Config + toggle** (config.py, settings_dialog.py) — unblocks a safe
   fallback from the start.
2. **`registry.py` mutate lock** (Step 2.1) — small, correct-on-day-1, can land
   first independently.
3. **`session_controller_base.py` runner dict + lifecycle** (Step 1.1) — the
   core refactor.
4. **`panel_core.py` multi-tab poll + per-tab approval** (Step 1.2–1.5).
5. **Tests** (Step 1.6).
6. **CHANGELOG** + mutation-panel tooltip note.

Each slice is independently testable. Slices 1 and 2 can ship before 3–4 with
zero behavior change (cap still 1).

---

## 9. Acceptance criteria

- [ ] Two tabs can each have a running agent; switching between them does not
      cancel either.
- [ ] Text streamed in tab B appears in tab B's view even while viewing tab A.
- [ ] Cap (default 3) prevents the 4th concurrent start; it queues and starts
      when a slot frees.
- [ ] `execute_python` approval, `USER_QUESTION`, and plan approval work per-tab.
- [ ] `/undo` still reverses the most recent mutation (global, coherent).
- [ ] Headless smoke test (`ask` one-shot) still works after refactor (B1).
- [ ] With `parallel_agent_enabled=False`, behavior is byte-identical to today.
- [ ] `./ci-local.sh` passes (ruff + mypy + pytest + desloppify).
- [ ] New tests in `tests/test_multitab_runner.py` pass.

---

## 10. Review findings (adversarial pass — grounded against source)

The original plan above was reviewed against the live source. Findings below
are ordered by severity. Each cites the exact code path.

### BLOCKERS (plan is wrong or incomplete — must fix before implementing)

#### B1. Headless/control layer depends on the single-runner API surface

The plan's non-goals say "Headless mode changes — not touched here." But the
refactor changes two methods the headless layer calls *unconditionally*:

- `headless/runner.py:112` → `controller.get_runner()`
- `headless/runner.py:136` → `controller.is_agent_running`
- `control/server.py:166,168,175,226,580,649,717` → `get_runner()` / `is_agent_running`

`get_runner(tab_id=None)` must default to the active tab and `is_agent_running`
(the property) must keep its zero-arg signature returning `any(...is_running)`.
Headless always has exactly one tab/runner, so this preserves it — but the
plan must state this **backward-compat contract explicitly**, and the test
suite must cover a headless run after the refactor to prove it.

**Fix:** add to §0 non-goals a line: "Public lifecycle API (`get_runner`,
`is_agent_running`, `cancel`, `get_event`) keeps zero-arg defaults that target
the active tab — headless/control call sites stay untouched." Add a headless
smoke test to acceptance criteria.

#### B2. Approval signals carry no `tab_id` — answers route to the wrong runner

`ChatView` emits signals with **no tab identity**:

- `chat_view.py:495-496` → `tool_approval_submitted = Signal(str, str)`,
  `user_answer_submitted = Signal(str)`

The panel connects every ChatView's signals to the **same** slots
(`panel_core.py:950-951, 993-994`), and those slots call `get_runner()` — i.e.
the active runner. Under multi-tab, clicking a background tab's approval
button would deliver the answer to the *active* tab's runner.

The plan mentions "captured in a closure" in a parenthetical but treats it as
minor. It is a **required** change. Also missed: the orchestra path
`_on_orchestra_approval` (`panel_core.py:1748-1750`) has the identical bug.

**Fix:** bind the tab_id at connect time in `_create_tab`/`_rebuild_history_tab`:

```python
chat_view.tool_approval_submitted.connect(
    lambda tcid, d, tid=tab_id: self._on_tool_approval(tcid, d, tid)
)
chat_view.user_answer_submitted.connect(
    lambda ans, tid=tab_id: self._on_user_answer_submitted(ans, tid)
)
```

No change to ChatView's signal signature. Elevate this from a parenthetical to
a numbered step in §1.3.

#### B3. `_on_submit` queues on global `is_agent_running` — wrongly blocks idle tabs

Current submit path (`panel_core.py:_on_submit`) decides queue-vs-start via:

```python
if self._ctrl.is_agent_running:   # ANY agent running
    self._ctrl.queue_message(text)
```

If tab B is running in the background and the user sends a fresh message in
idle tab A, this **queues A's message instead of starting A's agent** —
A then waits for B to finish. That defeats multi-tab concurrency.

**Fix:** the decision must be per-tab: `if self._ctrl.is_tab_running(active):
queue`. The plan defines `is_tab_running` but never says `_on_submit` must use
it. Add this to §1.1/§1.4 explicitly.

### CONCERNS (correctness risk — address before shipping)

#### C1. The input area is a single global widget — per-tab input disabling is impossible

The plan's §1.3 says "only the awaiting tab disables *its* input." But there
is exactly one `InputArea` (Send/Cancel/text field) shared across all tabs
(`panel_core.py:_input_area`). It cannot be enabled for tab B while disabled
for tab A. The claim is architecturally wrong.

Real behavior: input-disable stays **global**; when any tab awaits a
button-only approval, the input is disabled panel-wide. What changes is the
**answer routing** (B2) and a visible indicator of which tab is asking
(tab badge on QTabBar).

**Fix:** rewrite §1.3/§1.5 to state input-disable is global; per-tab-ness
applies only to answer *routing* and the visual badge. Drop "only the
awaiting tab disables its input."

#### C2. Free-text `USER_QUESTION` (allow_text) answer must route to the pending tab

`_on_submit` has a path (`panel_core.py:_on_submit`) where, if
`self._pending_answer`, it submits free text to `get_runner()`. With
multi-tab, `_pending_answer` must be tied to a tab: if the active tab is *not*
the pending tab, the typed answer must still reach the **pending** tab's
runner, not the active one.

**Fix:** the per-tab `_pending_answer_tab` (§2.2) must drive the submit
target: `runner = self._ctrl.get_runner(self._pending_answer_tab)`.

#### C3. Token display flickers between tabs

`_on_event` calls `_update_token_display(token_count)` on every event with
usage. Polling all tabs means the display jumps between tab A's and tab B's
token counts each tick.

**Fix:** drive the token display off the **active tab's** usage only: skip
`_update_token_display` when `event` comes from a non-active tab (or keep a
per-tab last-usage and show the active tab's). Add to §1.2/§1.5.

### VERIFIED CORRECT (claims the review confirmed against source)

- `UserQuestionWidget` **is** created inside `ChatView.handle_event`
  (`chat_view.py:1303`), parented to that view's `_container` — per-tab
  approval widgets already exist. ✅
- `ensure_advanced_tools_ready` **is** idempotent
  (`session_controller_base.py:374` — early-returns on
  `_advanced_tools_registered`), and `start_agent` runs on the main thread
  (Qt handler), so concurrent starts are serialized → no tool-registration
  race. ✅
- `self._runner` has exactly **10 references** in the base controller
  (lines 145,349,352,534,535,603,605,609,610,632,992-994) — refactor surface
  is bounded and matches §1.1. ✅
- `BackgroundAgentRunner` is self-contained (own thread + own
  `event_queue`, `loop.py:2747`) — independent instances confirmed. ✅
- The IDA host controller overrides **none** of the lifecycle methods
  (`session_controller.py`) — host-side change-free confirmed. ✅

### NET ASSESSMENT

The plan's architecture is sound and the refactor surface is accurately
scoped (3 files, bounded references). The blockers (B1-B3) are all about
**routing identity** (which tab owns a runner/answer/queue) — not about the
concurrency model, which the bulk-renamer precedent already validates. None
of the blockers require re-architecting; they require threading `tab_id`
through the existing call paths and keeping the headless API
backward-compatible. Fix B1-B3 + C1-C3, then the plan is implement-ready.
