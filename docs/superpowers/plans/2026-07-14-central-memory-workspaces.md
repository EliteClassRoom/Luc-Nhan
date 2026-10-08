# Central Memory Workspaces Implementation Program

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Điều phối bốn implementation plans để thay folder-scoped memory bằng central per-binary/case workspaces mà không có dual-write, lost update, cross-binary contamination hoặc unsafe migration.

**Architecture:** Chương trình triển khai theo bốn release gates: dark foundation, atomic binary cutover, analysis cases, rồi interchange/hardening. Mỗi plan có TDD tasks và commit độc lập; plan sau chỉ bắt đầu khi exit checklist của plan trước đạt đầy đủ.

**Tech Stack:** Python 3.11–3.12, SQLite WAL, portalocker 3.3, deterministic Markdown projection, JSONL ZIP interchange, IDA 9.x, PySide6, pytest/multiprocessing.

## Global Constraints

- Design authority: `docs/superpowers/specs/2026-07-14-central-memory-workspaces-design.md` at commit `a8a028e`.
- Plans execute in the exact order listed below.
- Use a worktree created through `superpowers:using-git-worktrees` before implementation.
- Use `superpowers:subagent-driven-development` or `superpowers:executing-plans`; never implement two tasks that edit the same file concurrently.
- Every task starts with a failing targeted test, ends with passing targeted tests and one focused commit.
- Do not enable central memory until the final activation task in Plan 2.
- No dual write, no `RIKUGAN.md` fallback, no live JSONL authority.
- Do not begin Plan 3 until all binary-memory consumers are cut over.
- Do not begin Plan 4 until case schema/promotion/source-drift semantics are stable.
- All newly added tests live under root `tests/`; both `tests/` and `lucnhan/tests/` run at every phase gate.
- Never commit `.cocoindex_code/`, temporary DBs, bundles, lock files or user memory artifacts.

---

## Program Plans

| Order | Plan | Tasks | Gate outcome |
|---:|---|---:|---|
| 1 | `docs/superpowers/plans/2026-07-14-central-memory-foundation.md` | 9 | Dark registry/identity/store/projection/run-binding foundation |
| 2 | `docs/superpowers/plans/2026-07-14-central-memory-cutover.md` | 10 | All binary memory atomically uses central workspace and feature activates |
| 3 | `docs/superpowers/plans/2026-07-14-analysis-case-memory.md` | 10 | Explicit cases, promotions, five relations, controlled peer retrieval |
| 4 | `docs/superpowers/plans/2026-07-14-memory-interchange-hardening.md` | 11 | Full migration, bundle interchange, recovery, stress/security/release gates |

Total: **40 reviewer-sized TDD tasks**.

---

## Dependency Graph

```text
Plan 1: Dark Foundation
  T1 Config/dependency
    └─ T2 Workspace contracts
       └─ T3 SQLite registry/backend
          └─ T4 Identity resolver
             ├─ T5 Raw-headless identity
             └─ T8 Session/controller binding
       └─ T6 Workspace store
          └─ T7 MEMORY.md projection
             └─ T8 Session/controller binding
                └─ T9 Foundation gate

Plan 2: Atomic Binary Cutover
  T1 Write authority
  T2 SQLite knowledge adapter
    └─ T3 BinaryMemoryService/retrieval
       ├─ T4 Notes/reports
       ├─ T5 Minimal legacy import
       └─ T6 Agent/commands/plans cutover
          ├─ T7 Subagent ownership
          └─ T8 Knowledge UI/switching
             └─ T9 Retire runtime JSONL/folder APIs
                └─ T10 Activate + document

Plan 3: Analysis Cases
  T1 Case schema
    └─ T2 Case CRUD/membership
       ├─ T3 Active-case binding
       └─ T4 Relations/suggestions
          └─ T5 Promotion/source drift
             └─ T6 Peer retrieval
                └─ T7 Agent context
                   ├─ T8 Commands
                   └─ T9 UI
                      └─ T10 Case integration gate

Plan 4: Interchange/Hardening
  T1 Storage guard
    └─ T2 Bundle schema/validator
       ├─ T3 Exporter
       └─ T4 Importer
          └─ T5 Full legacy migration
  T6 Backup/recovery
    └─ T7 Commands/UI/headless
  T1–T7 ──┬─ T8 Multiprocess stress
          ├─ T9 Performance bounds
          └─ T10 CI/docs
             └─ T11 Release rehearsal
```

---

## File-Ownership Scheduling Rules

These files recur across plans and must be edited sequentially:

| Shared file | Owning sequence |
|---|---|
| `lucnhan/memory/workspace_store.py` | Foundation T6 → Cutover T4/T5 → Cases T4/T6 → Hardening T4/T9 |
| `lucnhan/memory/registry.py` | Foundation T3/T4 → Cutover T5 → Cases T2 → Hardening T6 |
| `lucnhan/memory/manager.py` | Foundation T8 → Cases T3 |
| `lucnhan/memory/markdown.py` | Foundation T7 → Cutover T3 → Hardening T1/T8 |
| `lucnhan/memory/service.py` | Cutover T3–T5 → Hardening T3–T7 |
| `lucnhan/agent/loop.py` | Cutover T6/T7 → Cases T7/T8 |
| `lucnhan/agent/loop_commands.py` | Cutover T6 → Cases T8 → Hardening T7 |
| `lucnhan/ui/session_controller_base.py` | Foundation T8 → Cutover T8 → Cases T3/T9 |
| `lucnhan/ui/knowledge_panel.py` and `panel_core.py` | Cutover T8 → Cases T9 → Hardening T7 |
| `lucnhan/core/config.py` | Foundation T1 → Cutover T10 → Cases T7 |
| CI/docs/manifests | Foundation T1 → Cutover T9/T10 → Hardening T10/T11 |

Do not dispatch tasks touching the same row concurrently. Independent new test-only tasks may run in parallel only after their production prerequisites are committed.

---

## Phase Gates

### Gate 1 — Dark foundation

Run:

```bash
uv run python -m pytest tests/memory tests/state/test_memory_binding.py tests/cli/test_headless_memory_identity.py tests/ida/test_headless_bootstrap.py tests/agent/test_session_controller.py -v
uv run python -m pytest tests/ lucnhan/tests/ -q
uvx ruff format --check lucnhan/ tests/
uvx ruff check lucnhan/ tests/
uvx mypy lucnhan/core lucnhan/providers --pretty
```

Required state:

- default `memory_workspaces_enabled=False`;
- no new prompt/runtime consumer reads `MEMORY.md`;
- current behavior unchanged;
- identity/copy/move/raw/store/projection tests pass.

### Gate 2 — Atomic binary activation

Run:

```bash
uv run python -m pytest tests/memory tests/agent/test_memory_cutover.py tests/agent/test_memory_write_ownership.py tests/ui/test_memory_workspace_ui.py -v
uv run python -m pytest tests/ lucnhan/tests/ -q
uv lock --check
git grep -n "RIKUGAN.md\|\.lucnhan-kb" -- lucnhan ':!lucnhan/memory/legacy.py' ':!lucnhan/memory/raw_store.py'
```

Required state:

- central feature enabled;
- no folder runtime reader/writer or dual write;
- minimal explicit import available;
- all knowledge tests collected by configured roots;
- subagents cannot commit persistent data.

### Gate 3 — Analysis cases

Run:

```bash
uv run python -m pytest tests/memory/cases tests/agent/test_case_context.py tests/agent/test_case_commands.py tests/ui/test_case_memory_ui.py -v
uv run python -m pytest tests/ lucnhan/tests/ -q
```

Required state:

- membership/promotion explicit;
- five predicates enforced;
- source drift lazy/visible;
- peer retrieval deterministic, read-only, cited and bounded;
- two-process shared-case integration passes.

### Gate 4 — Interchange and release hardening

Run:

```bash
uv run python -m pytest tests/memory/interchange tests/memory/recovery tests/memory/stress -v
uv run python -m pytest tests/ lucnhan/tests/ -q
uv lock --check
uvx ruff format --check lucnhan/ tests/ scripts/
uvx ruff check lucnhan/ tests/ scripts/
uvx mypy lucnhan/core lucnhan/providers --pretty
```

Required state:

- bundle/migration limits and hostile fixtures pass;
- backup/recovery never auto-rebinds;
- repeated multiprocess stress is clean;
- release archive contains no user artifacts;
- migration/recovery/operator docs complete.

---

## Review Checkpoints

After every task:

1. review spec conformance for that task only;
2. review code quality and focused diff;
3. run its exact targeted commands;
4. commit only task files;
5. update task checkbox and record deviations.

After every plan:

1. run its exit checklist;
2. run both test roots;
3. run `git diff --check` and static checks;
4. run a code-review pass before proceeding;
5. do not waive a failed prerequisite for the next plan.

---

## Completion Definition

The program is complete only when all 40 tasks and four gates pass, the design success criteria are satisfied, the complete test inventory is collected in CI/release/local workflows, and release validation proves no user memory artifacts enter the plugin archive.
