# Review Fixes Phase 3 — Residuals + Architectural Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining review-derived findings that survived Phases 1 & 2: the script_guard transitive-module-attribute escape class (architectural), a per-run WorkspaceStore connection handle leak, subagent `max_turns` advisory-only, BackgroundAgentRunner control-event drops, Anthropic request-side message mutation, plan-step and `/a2a` cancel labeling, and CI scope gaps. The Phase-1 / Phase-2 deferred cosmetic minors stay parked.

**Architecture:** Script-guard remediation moves from static blocklists to a single layer of namespace sanitization at guarded execution time (`RestrictedPython`-style: replaced `__getattr__` / `__getitem__` / `getattr` / `__import__` proxies on a wrapping namespace that allow only known-safe names and explicit transitively-implied modules). All other tasks are narrow surgical fixes at existing seams.

**Tech Stack:** Python 3.10+ (IDA host), pytest, ruff/mypy per `pyproject.toml`. No new deps. (`RestrictedPython` is **not** an installed dep — Task 1 implements the proxy layer in-tree using only `__class__`-subclassed sentinel; pulling in the library is intentionally out of scope per AGENTS.md "no new deps without a good reason".)

**Spec:** 2026-09-02 full-project review findings + the residuals tabled in Phases 1 & 2 (ledger snippets appended at end of this document). Line numbers from the review are stale; each task opens by re-locating targets via grep anchors.

## Global Constraints

- `from __future__ import annotations`; type hints on all new signatures.
- `EXECUTE_PYTHON_TOOL_NAME` constant; never hardcode the string.
- Single sink for `eval`/`exec`: `rikugan/tools/script_guard.py` (Phase-1 invariant; Task 1 must not regress).
- No new third-party deps.
- Queue + `QTimer` for any cross-thread UI updates.
- Cancellation only via existing `threading.Event`/queue patterns.
- CI: `./ci-local.sh` regression envelope = master baseline; pre-existing failures unchanged.
- Commit style `type(scope): description`; branch `fix/review-phase3` off `master`.

---

### Task 1: Architectural fix — namespace scrubbing for guarded scripts (closes the transitive-leak class)

**Files:**
- Modify: `rikugan/tools/script_guard.py` (`_guarded_import`, `safe_builtins`, `run_guarded_script`, `run_guarded_code` — exact names may have drifted after Phase 1/2)
- Modify: `rikugan/ida/tools/microcode_optim.py` (optimizer compile path keeps using the hardened sink)
- Test: extend `tests/tools/test_script_guard.py`

**Background from the Phase-1 ledger.**
The Phase-1 review and four fix rounds closed every direct bypass vector (`builtins`, `timeit`, `pdb`, `inspect`, attribute aliasing, frame-dunder walks, namespace subscripts reaching `__builtins__`, the guard module itself, live-builtin injection via factories omitting `__builtins__`). What remained — explicitly parked as Phase-3 architectural work — is the transitive module-attribute leak class:
- `import uuid; uuid.os.system('id')` — `uuid` re-exports the real `os`.
- `import re; re.sys.modules['os'].system('id')` — same mechanism via the `re` → `sys` import chain.
- `import xml.etree.ElementTree; ET.sys.modules['…'].system('id')`; same via `io`/`contextlib` imports.
- `import base64; base64.sys…`, `import string; string.sys…`, `import collections; collections._sys…`, `import json; json.codecs.sys…`, `import enum; enum.sys…` — any stdlib module that imports `os`/`sys`/`importlib` transitively becomes a one-attribute-read launchpad to the un-sandboxed real objects.

Static blocklists cannot close this class: most `PureModule + DotAttribute` lookups over legitimate stdlib modules arrive at `os`/`sys`/`importlib` via innocuous transitive imports. Closing it requires an in-execution namespace policy: only allow attribute access that is either (a) a key in the explicit `safe_builtins()` table, (b) a name in `_BLOCKED_CALLS`/`_REMOVED_BUILTINS` (always denied), or (c) a name in a small allow-list of approved pure-data modules and only when that module itself appears in a deny-by-default import policy. Blocklists become positive-only at attribute-access time; dynamic lookup through `getattr`/`__getattribute__`/`__getitem__` on the namespace object is the chokepoint.

**Scopes decision (controller ruling, before execution): adopt SCOPE B (Layer A only).** The Phase-1 module blocklist policy (`_BLOCKED_MODULES`, `_BLOCKED_CALLS`, etc.) is preserved as-is. This task adds **only** an attribute-access proxy on module objects returned from `_guarded_import`. The proxy denies access to a deny-set of names regardless of which module surfaces them or how they were imported (direct or via transitive re-export). It does NOT introduce a positive allow-list of pure-data modules; it does NOT change the import policy; it does NOT block legitimate `M.attr` for M=approved modules when `attr` is not in the deny set. The Phase-1 holes the proxy is expected to close are precisely the transitive-leak chains documented in the Phase-1 ledger (`uuid.os`, `re.sys`, `ET.sys`, `re.enum.sys`, `base64.sys`, `string.sys`, `collections._sys`, `json.codecs.sys`). If a focused test reveals a leak Layer A does NOT close, the implementer files a Phase-4 finding rather than expanding in-scope code.

**Interfaces (binding contract for this task — Layer A only):**
- Produces: `SafeModule` (or extend the existing helper) wrapping any module object returned from `_guarded_import`. Its `__getattribute__`, `__getitem__` (when supported), `getattr`-style indirect access, and `vars()`/`dir()` paths route through a single denial chokepoint.
- `DENY_ATTR_NAMES = frozenset({"os", "sys", "subprocess", "importlib", "builtins", "shutil", "signal"})`. Any attribute access matching any name in this set, however reached, raises `AttributeError("Blocked — access to disallowed module attribute '<attr>'")`. Recursive wrapping (every attribute that is itself a module in DENY_ATTR_NAMES gets a proxy too).
- All OTHER attribute accesses return the underlying value (constant, function, class, submodule). Non-deny submodules that the wrapped module exposes are returned as their raw value — do not auto-wrap.
- `dir(M)` excludes the deny-set names (so `dir(re)` does not leak `sys`); other dir entries preserved.
- Existing `_check_ast` and the Phase-1 module/attribute blocklists stay byte-identical. Layer A is a runtime layer; static check remains the fast first line of defense.
- `safe_builtins()` and `_guarded_import` keep their existing signatures; only their return values change (modules are now wrapped).
- No new denials beyond `DENY_ATTR_NAMES`. Anything outside the deny set behaves identically to the un-wrapped module. This is the contract: deny-only, no positive allow-list, no policy shift.
- Layer A MUST NOT start a new watchdog thread per attribute access; verify perf with a loop of 10k reads.

- [ ] **Step 1: Write failing tests (one per Phase-1 leak, plus regressions, plus positive flows)** — covered in brief above; place in tests/tools/test_script_guard.py.
- [ ] **Step 2: Implement `SafeModule`** — proxy class with `__getattribute__` chokepoint checking the deny set; `_guarded_import` returns wrapped instances for any module passed to wrapped code; `dir()` filters deny-set names; integrate without breaking existing Phase-1 tests; no new watchdog threads.
- [ ] **Step 3: GREEN** plus the existing 123+ tests/tools/test_script_guard.py + Phase-2 test suite stay green. Re-run the consumer paths (microcode_optim, execute_python widget) tests to confirm wrapping is transparent to legit analysis flows.
- [ ] **Step 4: Commit** `fix(security): SafeModule proxy closes transitive module-attribute leak class`


### Task 2: Close per-run WorkspaceStore connection leak

**Files:**
- Modify: `rikugan/ui/session_controller_base.py` (`_wire_central_memory` + the `_on_agent_finished` path — grep those names)
- Modify: `rikugan/memory/workspace_store.py` (`close()` becomes idempotent if not already)
- Test: extend `tests/memory/` (new `test_workspace_store_lifecycle.py`)

**Interfaces:**
- Produces: each WorkspaceStore instance created in `_wire_central_memory` is `close()`d when the agent run finishes (`on_agent_finished` or whatever the actual hook name is) OR when the next run's store replaces it (whichever first). The connection is also closed if `_wire_central_memory` fails past the point of assignment. No double-close: `WorkspaceStore.close()` becomes idempotent (currently it may not be — grep and check).

- [ ] **Step 1: Write the failing test** — assert that after `on_agent_finished` for run N, the previous store's connection is closed (probe via Python's garbage collector + a `close()` instrumentation hook, or via monkeypatching the store's `close` to record calls and asserting the call count). Also assert exception-safety in `_wire_central_memory`: if assignment fails, the open connection is closed before the exception bubbles up.
- [ ] **Step 2: Implement** — track the previous store on the controller; replace on each wire-up; close on swap. Idempotent `close()`. Final report adds "store never leaks across N agent runs (100 iters, GC refcount stays flat)".
- [ ] **Step 3: GREEN** plus focused tests/memory suite stays green.
- [ ] **Step 4: Commit** `fix(memory): close per-run WorkspaceStore; idempotent close()`

### Task 3: `max_turns` becomes a hard ceiling (not advisory)

**Files:**
- Modify: `rikugan/agent/subagent.py` (`SubagentRunner.run_task`/`run_mode`/`run_exploration` accept `max_turns`; `rikugan/agent/modes/normal.py::run_normal_loop` enforces it)
- Modify: `rikugan/agent/subagent_manager.py` (per-type overrides reach the runner as a hard limit; orchestrator path stays gated off)
- Test: extend existing subagent tests

**Interfaces:**
- Produces: `SubagentRunner` instances created by the top-level API stash `max_turns` on the loop metadata (read existing pattern at `loop.py` — look for an `attrs`/metadata dict). `run_normal_loop` reads the metadata and raises `TurnLimitReached` (a new exception in agent/loop.py; convert to a `CANCELLED` variant or a well-defined error result event) when the counter exceeds `max_turns`. Subagents that exceed the limit complete the current tool call, surface a clean final result, and exit — they do not silently run 100 turns.

- [ ] **Step 1: Write failing test** — subagent spawned with `max_turns=3` and a tool schema that *would* loop forever terminates cleanly at turn 3.

- [ ] **Step 2: Implement** — keep the prompt-text advisory for backwards compatibility; add the hard counter. Subagent run methods raise/return when the limit hits; converts to the right exit code (not `Exception`).

- [ ] **Step 3: GREEN** plus existing subagent tests stay green.

- [ ] **Step 4: Commit** `fix(agent): enforce max_turns as a hard ceiling`

---

### Task 4: BackgroundAgentRunner: never drop control events

**Files:**
- Modify: `rikugan/agent/loop.py` (`BackgroundAgentRunner._safe_put`)
- Test: extend existing agent loop tests

**Interfaces:**
- Produces: `_safe_put` distinguishes *control events* (`TURN_END`, `TURN_START`, `TOOL_RESULT`, `CANCELLED`, `ERROR`, sentinel) from stream deltas. Control events use a **blocking** bounded put with timeout large enough to absorb UI pause (e.g. 5–10s, then escalate to a logged warning + persistent write to a separate recovery queue); stream deltas keep the existing drop-on-pressure behavior (documented). The sentinel `None` uses blocking put with a finite timeout so the daemon thread can exit cleanly if the consumer dies — drop the sentinel only as a last resort and log it.

- [ ] **Step 1: Write failing test** — slow UI consumer + many tool results, assert every `TURN_END` and `CANCELLED` arrives (no lifecycle state divergence); assert thread exits within bounded time when consumer is closed (`q.put(None)` doesn't hang forever).

- [ ] **Step 2: Implement** — split the put helper into two: `put_control` (blocks, bounded timeout, log+warn on timeout) and `put_delta` (current drop semantics). Update all call sites of `_safe_put` in the file; the sentinel send is its own branch.

- [ ] **Step 3: GREEN** plus existing loop tests stay green.

- [ ] **Step 4: Commit** `fix(agent): never drop control events from BackgroundAgentRunner`

---

### Task 5: Anthropic — deep-copy raw blocks on every request

**Files:**
- Modify: `rikugan/providers/anthropic_provider.py` (`_format_messages` block-list copy)
- Test: extend `tests/providers/`

**Interfaces:**
- Produces: each block dict in the replayed `raw_parts` of an assistant message is deep-copied (`[dict(b) for b in raw_parts]`-equivalent — handle nested content blocks too). Mutations `_build_request_kwargs` and the MiniMax sibling apply to the copy only.

- [ ] **Step 1: Write failing test** — call provider twice with the same Message object; assert mutation in request one does not leak into request two (cache_control field round-trip), and original Message is byte-equal after both calls.

- [ ] **Step 2: Implement** the deep copy.

- [ ] **Step 3: GREEN** plus existing provider tests stay green.

- [ ] **Step 4: Commit** `fix(providers): deep-copy Anthropic _raw_parts on every request`

---

### Task 6: Cancel label — plan-step and `/a2a` mode must emit CANCELLED, not ERROR

**Files:**
- Modify: `rikugan/agent/modes/a2a.py` (run_a2a_mode catches CancellationError separately)
- Test: extend existing a2a tests; add a quick check that plan-step "turn_limit" label is observable

**Interfaces:**
- Produces: `run_a2a_mode` re-raises `CancellationError` so the outer `AgentLoop.run` converts to `CANCELLED` (matching `modes/exploration.py` already done in Phase 1 / `_stream_llm_turn_inner` pattern). Plan-step already gets status in Phase 2 (`completed`/`turn_limit`/`error`); verify the consumer (UI) renders `turn_limit` distinctly from `completed` — if not, add the renderer change here.

- [ ] **Step 1: Write failing test** — cancel during an `a2a` turn yields `CANCELLED` event in the stream; UI sees correct label.

- [ ] **Step 2: Implement** — narrow exception handler; add minimal UI label differentiation if missing.

- [ ] **Step 3: GREEN.**

- [ ] **Step 4: Commit** `fix(agent/ui): plan-step and a2a cancel emit CANCELLED, not ERROR`

---

### Task 7: CI scope — mypy memory + ui; ruff tests/

**Files:**
- Modify: `pyproject.toml` (mypy `files = [...]`, ruff `extend-exclude` or scope; pytest timeout)
- Add: `pyproject.toml`'s `[tool.pytest.ini_options]` timeout if sensible

**Interfaces:**
- Produces: `mypy` config includes `rikugan.memory` and (separately) `rikugan.ui` modules that are non-shiboken — strict enough to catch regressions but no more than the layering requires. `ruff check` includes `tests/` directory. `[tool.pytest.ini_options]` adds a default per-test timeout so the known flakes (e.g. `test_height_cached_label` mock pollution) don't hang the suite.

- [ ] **Step 1: Land the config** with the smallest scope that adds value — mypy targets explicitly chosen modules by reading `pyproject.toml [tool.mypy]` and excluding only ones that touch shiboken at import time (e.g. `rikugan.ui.chat_view` may need to stay `ignore_errors = true` if the stubs don't cover QWidget signal overloads; evaluate per-module).

- [ ] **Step 2: Run new checks; fix any pre-existing failures they surface EXCEPT master's known pre-existing failures (workspace migration v2/v3/portalocker manifest) — record the rest as new audit-worthy items separately, do not fix here.

- [ ] **Step 3: Commit** `chore(ci): expand mypy + ruff scope; add per-test timeout`

---

## Final verification (after Task 7)

- [ ] `./ci-local.sh` — no failure NEW vs master baseline for the affected check (mypy/ruff now point at more code; pre-existing per-module ignores preserved where the stub gap is real).
- [ ] Full pytest — failure set ⊆ master's 28 (Task 2 may *reduce* some by closing the WorkspaceStore lifecycle gap).
- [ ] `git log --oneline` shows the 7 task commits on `fix/review-phase3`.
- [ ] Push branch + PR to `master` (fork `EliteClassRoom/rikugan`) — or local merge per user choice.

## Explicitly out of scope (Phase 4 / accepted)

- Master's 44 ruff + format drift and the schema-version migration tests (separate cleanup PR).
- RestoreWorker `deleteLater` stub-leak when `tests/qt_stubs.py` is unloaded between tests — test infrastructure, not code.
- GLM/compat empty-key 401 UX (deferred product nudge; auth guidance already covers it).
- Phase-1/Phase-2 cosmetic minors (PEP-8 blank lines, comment headers, hoist-to-module-level refactors).
- MiniMax MINIMAX_API_KEY env guidance text matching actual behavior (settings-dialog only).
- `BackgroundAgentRunner` sentinel recovery queue persistence (log+warn is sufficient for Phase 3).

## Appendix — residual provenance (from Phase-1/Phase-2 ledgers)

| Residual | Source ledger note |
|---|---|
| Transitive module-attribute leaks (`uuid.os`, `ET.sys`, `re.enum.sys`, `collections._sys`, `json.codecs.sys`) | Phase 1 T2 fix round 4 — parked, structural |
| Per-run WorkspaceStore connection not closed | Phase 2 MemoryReview finding #5 (review-pass) |
| Subagent `max_turns` advisory only | Phase 2 review AgentsReview finding #5 |
| BackgroundAgentRunner control-event drops | Phase 2 review AgentLoop finding #10 |
| Anthropic `_raw_parts` shared mutation | Phase 2 review ProvidersReview finding #8 |
| Plan-step /a2a cancel labeling | Phase 2 review AgentsReview findings #11, #12 |
| CI mypy/ruff/pytest timeout gaps | Phase 2 review MemoryUITestsReview finding #9 |
