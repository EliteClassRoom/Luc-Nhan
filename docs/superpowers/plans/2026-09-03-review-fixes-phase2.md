# Review Fixes Phase 2 — Memory, Agent & Provider Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the 10 highest-value deferred findings from the 2026-09-02 full-project review that survived Phase 1: the WorkspaceStore cross-thread race, portalocker dead-code, delegate_external_task approval gap, subagent approval deadlocks + lost mutations, provider watchdog leak, Gemini/Codex retry gaps, missing mutation tracking (13 tools), RestoreWorker thread violation, config boolean/typing holes, and a batch of small correctness fixes.

**Architecture:** No new layers. Memory gets a single shared lock at the store seam (both existing threads already funnel through `WorkspaceStore`); agent fixes reuse the existing `_wait_for_approval`/queue patterns; provider fixes mirror each other (watchdog done-event, retryable classification); mutation coverage gets builders + a drift-proof consistency test.

**Tech Stack:** Python 3.10+ (IDA host), pytest with `tests/mocks/ida_mock.py` (no IDA needed), portalocker (already a dependency), ruff/mypy per `pyproject.toml`.

**Spec:** 2026-09-02 full-project review findings (this conversation). All file:line references below are from that review, taken BEFORE Phase 1 landed — line numbers have drifted; every task starts by re-locating the target with the given grep anchor.

## Global Constraints

- `from __future__ import annotations`; type hints on all new signatures; dataclasses for structured data.
- Never hardcode `"execute_python"` — `rikugan.constants.EXECUTE_PYTHON_TOOL_NAME`.
- Host API imports only via `importlib.import_module()` in `try/except ImportError`; never at module level in `rikugan/ida/ui/`.
- No Qt signals across threads — `queue.Queue` + `QTimer` only.
- No new dependencies (portalocker already required).
- Cross-thread cancel/approval only via existing `threading.Event` / queue patterns.
- CI: `./ci-local.sh` must not regress vs master baseline (master has 28 pre-existing suite failures + 44 ruff errors — match, don't exceed, in files you touch: your changed files must be ruff-clean).
- Commit style `type(scope): description`; branch `fix/review-phase2` off `master`.

---

### Task 1: Serialize WorkspaceStore access across threads

**Files:**
- Modify: `rikugan/memory/workspace_store.py` (constructor holds one long-lived connection — grep `check_same_thread` and `WorkspaceStore.__init__`)
- Modify: `rikugan/memory/sqlite_backend.py` (grep `check_same_thread=False`)
- Test: `tests/memory/` (new `test_workspace_store_threads.py`)

**Interfaces:**
- Produces: `WorkspaceStore` gains `self._lock = threading.RLock()`; every public method that touches `self._conn` (save_fact/save_exploration_finding/search/list/get/mark_projection_dirty/close, and any begin_immediate_with_retry path) wraps its body in `with self._lock:`. The Qt main-thread knowledge-panel refresh path (panel reads via the same service) thereby serializes against agent-thread writes.
- `open_sqlite()` keeps `check_same_thread=False` (both threads are now lock-serialized — document why in a comment).

- [ ] **Step 1: Write the failing test** — two threads hammering one store concurrently:

```python
"""Concurrent access to one WorkspaceStore must serialize without
'started a transaction within a transaction' / 'cannot rollback' errors."""
import threading
from rikugan.memory.workspace_store import WorkspaceStore

def test_concurrent_writer_and_reader_no_sqlite_errors(tmp_path):
    store = WorkspaceStore.create(tmp_path)  # adapt to real factory/classmethod
    errors: list[str] = []
    def writer(tag: str) -> None:
        for i in range(50):
            try:
                store.save_fact(...)  # adapt: minimal valid fact record
            except Exception as e:  # noqa: BLE001 — collecting failures is the point
                errors.append(f"{tag}:{i}:{e}")
    def reader() -> None:
        for _ in range(50):
            try:
                store.list_facts(...)  # adapt to a real read API
            except Exception as e:  # noqa: BLE001
                errors.append(f"reader:{e}")
    threads = [threading.Thread(target=writer, args=(f"w{n}",)) for n in range(3)] + [threading.Thread(target=reader)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert not errors
```

Also a correctness test: a reader must never observe a fact that a writer rolled back (writer raises a sentinel exception mid-transaction inside the lock; assert the fact is absent after).

- [ ] **Step 2: Run — expect FAIL** (`sqlite3.OperationalError` entries in `errors`)
- [ ] **Step 3: Implement** — add `RLock`, wrap public methods; keep transactions inside the lock so BEGIN IMMEDIATE/commit/rollback are atomic against readers. Do NOT hold the lock across the projector/markdown write (that lives above the store).
- [ ] **Step 4: Run — expect PASS**; then `python3 -m pytest tests/memory -q`
- [ ] **Step 5: Commit** `fix(memory): serialize WorkspaceStore access across agent and UI threads`

---

### Task 2: Fix MemoryProjector lock contention handling (portalocker semantics)

**Files:**
- Modify: `rikugan/memory/markdown.py` (grep `_acquire_lock`)
- Test: `tests/memory/test_markdown.py` (replace the fake-Lock-constructor test, ~lines 228-252)

**Interfaces:**
- Produces: `_acquire_lock` acquires the lock itself and returns a context manager, or `project()` calls `portalocker.Lock(...)` inside the `with` and catches `Exception` (or `portalocker.LockException` when importable) at the correct site — contention must invoke `on_contention` (→ `store.mark_projection_dirty()`), and no `except None` edge remains.

- [ ] **Step 1: Write failing tests** — a fake `portalocker.Lock` whose `__enter__` raises `LockException` (constructor succeeds), asserting `mark_projection_dirty` was called and `project()` returns gracefully; plus a real two-process/two-thread contention test using actual portalocker on a temp lock file if feasible in-test (skip gracefully if the platform blocks it).
- [ ] **Step 2: Run — expect FAIL** (current code only catches at construction)
- [ ] **Step 3: Implement** the restructure; remove the dead except branch; add a brief comment quoting portalocker's actual acquire-time raise.
- [ ] **Step 4: Run — expect PASS**; `python3 -m pytest tests/memory -q`
- [ ] **Step 5: Commit** `fix(memory): handle portalocker contention at acquire time`

---

### Task 3: Approval-gate `delegate_external_task`

**Files:**
- Modify: `rikugan/agent/loop.py` (grep `delegate_external_task` / `_handle_delegate_external_task_tool`, review ref ~:2211-2295)
- Test: `tests/agent/` (extend the approval-gate tests; follow `tests/agent/test_approval_gate.py` from Phase 1)

**Interfaces:**
- Produces: before `dispatcher.run_task(...)`, the handler calls `_wait_for_approval` with the same deny/allow semantics as execute_python (approval text must show the target agent name and the full task text so the user sees exactly what will be executed externally). Deny → tool error result, no subprocess. `a2a` *mode* (explicit user command `/a2a`) stays ungated — only the pseudo-tool dispatch inside a normal agent turn is gated.

- [ ] **Step 1: Write failing tests** — allow → dispatcher invoked once with the task text; deny → no subprocess, error result returned; `/a2a` mode path unaffected (existing tests keep passing).
- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement**; reuse the Phase-1 `requires_approval`-style flow or `_wait_for_approval` directly — read both call sites and pick the one that routes the approval to the existing UI queue (headless serve-mode must get the HTTP approval decision path for free).
- [ ] **Step 4: Run — expect PASS**; focused `tests/agent -k approval -q`
- [ ] **Step 5: Commit** `fix(agent): approval-gate delegate_external_task pseudo-tool`

---

### Task 4: Close subagent approval deadlocks + propagate subagent mutations

**Files:**
- Modify: `rikugan/agent/bulk_renamer.py` (grep `_deep_analyze_job`)
- Modify: `rikugan/agent/subagent_manager.py` (grep `_run_agent`)
- Modify: `rikugan/agent/subagent.py` (grep `run_task` / `last_session`)
- Modify: `rikugan/agent/loop.py` (grep `_mutation_log` — expose a read accessor `drain_mutations()` or return the log from `run`)
- Test: `tests/agent/` (new `test_subagent_interactive_tools.py`)

**Interfaces:**
- Produces:
  - `BulkRenamerEngine._deep_analyze_job` and `SubagentManager._run_agent` no longer hang on `TOOL_APPROVAL_REQUEST`/`USER_QUESTION`: minimal fix = exclude interactive/approval-gated tools (`execute_python`, anything `requires_approval`, `ask_user`) from the child's registry/schema in these unattended contexts (there is no queue answering them). Assert via test that a child attempting such a tool gets a clear error result, not a hang.
  - `SubagentRunner.run_task` (and `run_mode`/`run_exploration`) copy the child loop's `_mutation_log` records into the parent loop's log when a `parent_loop` exists (`parent_loop.record_mutations(child_loop.drain_mutations())` or equivalent). `/undo` then reverses subagent mutations.

- [ ] **Step 1: Write failing tests** — (a) child with filtered registry calling execute_python-shaped tool → immediate error, worker completes; (b) subagent performing a rename → parent `_mutation_log` contains the record after `run_task`.
- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement** both halves; keep `run_normal_loop`'s 100-turn ceiling untouched (separate deferred item).
- [ ] **Step 4: Run — expect PASS**; focused agent tests.
- [ ] **Step 5: Commit** `fix(agent): no unattended approval deadlocks; propagate subagent mutations to undo log`

---

### Task 5: Stop provider watchdog thread leak

**Files:**
- Modify: `rikugan/providers/openai_provider.py`, `anthropic_provider.py`, `gemini_provider.py`, `codex_provider.py` (grep `cancel_event.wait()` in each `_stream_chunks`)
- Test: `tests/providers/` (new `test_watchdog_cleanup.py`)

**Interfaces:**
- Produces: each `_stream_chunks` creates `done = threading.Event()`; watchdog waits `cancel_event.wait(timeout)` in a loop AND exits when `done.is_set()`; `_stream_chunks` sets `done` in a `finally:`. Same shape in all four providers — factor a tiny helper in `rikugan/providers/base.py` (e.g. `_spawn_cancel_watchdog(cancel_event, iter) -> threading.Event`) only if it fits the existing inheritance structure cleanly; otherwise duplicate the 10-line pattern per provider (precedent: each provider already duplicates the watchdog).

- [ ] **Step 1: Write failing test** — a fake stream that completes normally; assert no thread named `*watchdog*` (or: `threading.active_count()` returns to baseline within a short join timeout) after the generator is exhausted; and that a mid-stream cancel still interrupts promptly.
- [ ] **Step 2: Run — expect FAIL** (thread count stays elevated)
- [ ] **Step 3: Implement** per provider; do not change cancel semantics (watchdog must still fire while the stream is blocked).
- [ ] **Step 4: Run — expect PASS**; `python3 -m pytest tests/providers -q`
- [ ] **Step 5: Commit** `fix(providers): watchdog threads exit after stream completion`

---

### Task 6: Gemini/Codex retry classification + Gemini empty-candidate guard

**Files:**
- Modify: `rikugan/providers/gemini_provider.py` (grep `_handle_api_error` and `_normalize_response`)
- Modify: `rikugan/providers/codex_provider.py` (grep `_handle_api_error`)
- Test: `tests/providers/` (extend gemini/codex tests; follow existing error-classification test style)

**Interfaces:**
- Produces:
  - Gemini `_handle_api_error`: `InternalServerError`/`ServiceUnavailable`/`DeadlineExceeded` (google.api_core) and transport errors (`httpx.ConnectError`/`ReadTimeout` or `TransportError`) → `ProviderError(..., retryable=True)`, mirroring `anthropic_provider._handle_api_error`.
  - Codex `_handle_api_error`: HTTP status ≥ 500 and non-HTTPError `urllib.error.URLError` → `retryable=True`.
  - Gemini `_normalize_response`: guard `if not response.candidates or not response.candidates[0].content:` → raise `ProviderError` carrying the block reason (e.g. `prompt_feedback.block_reason` when present) instead of `IndexError`/`AttributeError`.

- [ ] **Step 1: Write failing tests** — parametrized over error shapes: each transient error yields `retryable=True`; safety-blocked empty-candidate response yields a descriptive `ProviderError` (not IndexError).
- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement**; import google.api_core exception names lazily/defensively (they exist in the SDK already in requirements).
- [ ] **Step 4: Run — expect PASS**; `python3 -m pytest tests/providers -q`
- [ ] **Step 5: Commit** `fix(providers): classify Gemini/Codex transient errors retryable; guard empty candidates`

---

### Task 7: Mutation coverage for the 13 untracked mutating tools (+ drift-proof test)

**Files:**
- Modify: `rikugan/agent/mutation.py` (grep `_REVERSE_BUILDERS`, `capture_pre_state`)
- Read-only reference for tool semantics: `rikugan/ida/tools/types_tools.py` (create_struct/modify_struct/create_enum/modify_enum/create_typedef/apply_struct_to_address/import_c_header/propagate_type/import_type_from_library), `annotations.py` (set_type), `microcode.py` (nop_microcode, install/remove_microcode_optimizer — the install/remove pair can register a non-reversible record with a clear reason, same precedent as execute_python)
- Test: `tests/agent/test_mutation_coverage.py` (new)

**Interfaces:**
- Produces: every `mutating=True` tool either (a) gets a real `build_reverse_record` + `capture_pre_state` pair (capture via existing raw getter tools — the getter must return raw data, not a formatted string, per AGENTS.md), or (b) appears in an explicit `_INTENTIONALLY_NON_REVERSIBLE: frozenset[str]` with an inline reason (execute_python, install/remove_microcode_optimizer). Plus the consistency test:

```python
def test_every_mutating_tool_is_covered():
    from rikugan.tools.registry import ToolRegistry  # adapt to real registry API
    from rikugan.agent.mutation import _REVERSE_BUILDERS, _INTENTIONALLY_NON_REVERSIBLE
    registry = build_test_registry()  # existing test registry factory — reuse it
    mutating = {td.name for td in registry.definitions() if td.mutating}
    uncovered = mutating - set(_REVERSE_BUILDERS) - _INTENTIONALLY_NON_REVERSIBLE
    assert not uncovered, f"mutating tools without undo entries: {sorted(uncovered)}"
```

Priority order for real builders (highest value first): `nop_microcode` (original bytes known), `modify_struct`/`modify_enum` (pre-state capturable via iter_struct/iter_enum), `set_type`, `create_struct`/`create_enum`/`create_typedef` (reverse = delete), `apply_struct_to_address`, `propagate_type`; `import_c_header`/`import_type_from_library` may join the non-reversible set with a reason if a faithful reverse is impractical — decide per tool, document in the report.

- [ ] **Step 1: Write the failing consistency test** (it must list all 13 as uncovered → RED)
- [ ] **Step 2: Run — expect FAIL** with the 13 names
- [ ] **Step 3: Implement** builders + frozenset; TDD each real builder with a small reverse-roundtrip test using the IDA mocks (apply mutation record → build reverse → apply reverse → pre-state restored).
- [ ] **Step 4: Run — expect PASS**; `python3 -m pytest tests/agent -k mutation -q`
- [ ] **Step 5: Commit** `fix(agent): mutation tracking for all mutating tools + drift-proof consistency test`

---

### Task 8: RestoreWorker queue+QTimer + Shiboken import guards

**Files:**
- Modify: `rikugan/ui/chat_view.py` (grep `class RestoreWorker`, review ref ~:283-294 emit sites, connections ~:1984-1990)
- Modify: `rikugan/ida/ui/panel.py` (grep `importlib.import_module` at module level ~:21-22), `rikugan/ida/ui/tools_form.py` (~:11)
- Test: `tests/ui/test_chat_view_restore.py` (new or extend existing restore tests)

**Interfaces:**
- Produces: `RestoreWorker.run()` pushes `(kind, payload)` tuples to a `queue.Queue` and the main thread drains via the existing `QTimer` poll pattern (copy the shape from `panel_core`'s history executor — grep `_poll` there); chunk widget construction moves to the main-thread drain. `panel.py`/`tools_form.py` ida imports move inside functions with `try/except ImportError` following `rikugan/ida/ui/actions.py`'s `_probe_ida/_ensure_ida` pattern.

- [ ] **Step 1: Write failing test** — restore flow with a stubbed widget factory: assert chunks are delivered on the main-thread drain call (i.e., queue populated, no cross-thread widget construction); existing restore tests must stay green.
- [ ] **Step 2: Run — expect FAIL** (new test), keep old ones green.
- [ ] **Step 3: Implement**; delete the signal-based path entirely (clean cutover — no dual path).
- [ ] **Step 4: Run — expect PASS**; `python3 -m pytest tests/ui -k restore -q`
- [ ] **Step 5: Commit** `fix(ui): RestoreWorker uses queue+QTimer; guard ida imports in panel/tools_form`

---

### Task 9: Config hardening — boolean guard + numeric coercion

**Files:**
- Modify: `rikugan/core/config.py` (grep `_BOOLEAN_FIELDS`, `_apply_loaded_config`)
- Test: `tests/core/test_config.py` (extend; Phase-1 file exists)

**Interfaces:**
- Produces:
  - `_BOOLEAN_FIELDS` gains `oauth_consent_accepted`, `preserve_context`, `a2a_auto_discover` (and audit every `bool`-typed dataclass field — any missing join the frozenset; non-bool truthy values are rejected/reset to defaults per the existing field behavior).
  - `_apply_loaded_config` coerces-or-rejects numeric provider fields: `temperature`/`max_tokens`/`context_window` values failing `float()`/`int()` are skipped (left at defaults) before `validate()` can see them — `validate()` and `save()` must never raise TypeError on hand-edited configs. Add a regression test loading `{"provider": {"temperature": "0.3", ...}}`.

- [ ] **Step 1: Write failing tests** — (a) `"oauth_consent_accepted": "yes"` does not enable consent; (b) string temperature loads without raising, `validate()` returns errors (not exceptions).
- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run — expect PASS**; `python3 -m pytest tests/core -q`
- [ ] **Step 5: Commit** `fix(core): strict boolean fields and numeric coercion for hand-edited configs`

---

### Task 10: Small-fix batch (same-shape mechanical edits, one dispatch)

**Files & exact changes:**

1. `rikugan/agent/loop_commands.py` (grep `_handle_undo_command`): pop the mutation record only AFTER a successful reverse — on `ToolError`, push the record back (or don't pop until success). Test: failing reverse → record still present, second `/undo` retries it.
2. `rikugan/agent/modes/plan.py` (grep `_execute_step`): emit `plan_step_done` with a real status (`completed`/`turn_limit`/`error`) and always emit it (try/finally around the mini loop); error path no longer leaves the UI step stuck. Test both paths.
3. `rikugan/providers/openai_provider.py` (grep `_fetch_models_live`): make the model-id prefix filter a class attr `_MODEL_ID_PREFIXES`; `GLMProvider` overrides with `("glm-",)`. Test: GLM live fetch keeps `glm-5.2`-style ids.
4. `rikugan/providers/minimax_provider.py` (grep `_NativeToolCallFilter` usage in `_stream_chunks`): tag thinking-channel chunks and skip tool-call XML detection for them (text passthrough only). Test: thinking delta containing `<invoke ...>` produces text, no tool-call events.
5. `rikugan/skills/registry.py` (grep `get_summary_for_prompt`): pass `description` (and slug) through `strip_injection_markers` (import from `core/sanitize.py`). Test: description with embedded newline+"system:" is neutralized in the summary.
6. `rikugan/memory/bundle_import.py` (grep `zf.read(file_info.name)`): verify per-member sha256 against the manifest and enforce declared uncompressed size before inflating (read via streaming with a cap); mismatch → clear import error. Test: tampered member rejected; oversized member rejected before full inflation.
7. `rikugan/agent/pseudo_tool_schemas.py` (grep `Luc Nhan`): replace with "Rikugan/IDA" wording.
8. `rikugan/memory/service.py` (grep `BUG:`): delete the two post-save `get_fact` verification blocks reaching into `repository._store` (and the same pattern in `tests/memory/test_repository.py`/`test_service.py` if trivially present).

- [ ] **Step 1: Write failing tests per item** (each in the module's existing test file; batch them in one run)
- [ ] **Step 2: Run — expect all new tests RED**
- [ ] **Step 3: Implement all eight; each keeps its own focused test green**
- [ ] **Step 4: Run the batched suite** — all green; `ruff check` clean on touched files
- [ ] **Step 5: Commit** `fix: phase-2 small-fix batch (undo, plan status, GLM models, minimax thinking, skill sanitize, bundle verify)`

---

## Final verification (after Task 10)

- [ ] `./ci-local.sh` — no regression vs master baseline (pre-existing failures unchanged; desloppify ≥ 88.5)
- [ ] Full pytest: failure set ⊆ master's 28 pre-existing failures
- [ ] `git log --oneline` shows the 10 task commits on `fix/review-phase2`
- [ ] Push branch + PR to `master` (fork `EliteClassRoom/rikugan`) — or local merge per user choice

## Explicitly out of scope (Phase 3 / accepted residuals)

- Script-guard transitive module-attribute leaks (uuid.os, ET.sys, ...) — needs guarded_getattr/namespace scrubbing redesign; human approval gate remains the backstop.
- Subagent max_turns enforcement; BackgroundAgentRunner control-event drops; orchestra chat_stream cancel_event + CANCELLED labeling (path is feature-flagged off); /a2a cancel-to-error conversion; Anthropic _raw_parts deep-copy; MiniMax env-var guidance; GLM/compat no-key 401 UX; per-run WorkspaceStore close in session_controller_base; CI scope expansion (mypy memory/ui, ruff tests/); master's 44 pre-existing ruff errors.
