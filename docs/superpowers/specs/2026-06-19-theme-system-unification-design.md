# Theme System Unification — Design Spec

**Date:** 2026-06-19
**Status:** Approved (Approach 3 — Incremental strangler)
**Branch target:** `feat/theme-unification` (per-phase branches)
**Origin:** Sprint 1 UI/UX review (CRITICAL #3 — theme architecture)

---

## Context

A UI/UX review flagged the theme system as a CRITICAL architectural concern:
"4 overlapping style sources cause theme-switch bugs." After mapping the
system with 3 parallel explorer agents, the actual picture is different and
less severe:

| Source | Actual state |
|--------|-------------|
| `LIGHT_THEME` / `DARK_THEME` (styles.py:72-1252, ~1180 lines) | **Fully dead** — no function returns them; `build_theme_stylesheet()` always returns `""` |
| `widgets_*.py` getters (5 files, `is_dark_theme()` based) | **Working**, refresh correctly via `themeChanged` signal |
| Inline `setStyleSheet(f"...")` (204 calls, 13 files) | **Working**, use `tokens()`, refresh correctly |
| `ida/ui/panel.py` `minimal_style` (object-name-scoped, init-only) | **Independent**, different region — but **does NOT re-apply on theme switch** (the real bug) |

There is no "4-layer conflict." The two real problems are:

1. **Dead code** (~1180 lines `LIGHT_THEME`/`DARK_THEME`) confusing maintainers.
2. **Bug**: `panel.py` `minimal_style` is applied at construction only and never
   re-applied when the user switches theme mid-session → some widgets keep the
   old palette. This is the root cause behind the "theme doesn't apply" reports.

On top of that, there are **two parallel state sources of truth**:
- `ThemeManager` (modern): `mode` enum + `tokens()` + `themeChanged` signal.
- Legacy bridge (styles.py): `_current_theme`, `_effective_theme`,
  `is_dark_theme()`, `set_current_theme()` — read by 5 `widgets_*.py` files
  via `_branch()`. When these drift out of sync (e.g. `set_mode` called but
  `set_current_theme` not yet), widgets can pick the wrong dark/light branch.

## End-state goal (B — full unification)

One state source of truth (`ThemeManager.tokens()`) + one styling mechanism
(every inline style token-driven; no legacy `is_dark_theme()` bridge; no dead
`LIGHT_THEME`/`DARK_THEME`).

## Approach: Incremental strangler (4 phases)

Each phase ships independently, is revertable, and is visually verified in IDA.
**The user can stop after any phase** — Phase 1 already fixes the real bug.

### Phase 1 — Bug-fix + dead code removal (user-facing value first)

**Goal:** Kill the real "theme doesn't apply" bug + remove 1180 lines of dead
code.

Changes:
- **Fix bug**: wire `panel.py` `minimal_style` re-application into the theme
  change path. Expose a `_reapply_minimal_style()` method (or rebuild on
  `_on_theme_changed`) so switching theme mid-session repaints the host-scoped
  QSS. Decide during planning whether panel.py subscribes to
  `ThemeManager.themeChanged` or panel_core forwards the event down.
- **Remove dead code**: delete `LIGHT_THEME` (styles.py:72-661) and
  `DARK_THEME` (styles.py:664-1252). Delete `build_theme_stylesheet()` (the
  no-op). Remove its 2 call sites in `panel_core.py` (lines 531, 730).
- **Migrate tests**: `test_panel_core.py:107-108` and
  `test_settings_dialog.py:69-70,123-124` reference `DARK_THEME` /
  `build_theme_stylesheet` — update them to not depend on deleted symbols.

Verify:
- Full `pytest` suite green.
- mypy on `lucnhan/core` + `lucnhan/providers` clean.
- **IDA visual**: open plugin, switch dark↔light↔ida mid-session, confirm no
  stale widgets (especially the host-scoped objects: `thinking_block`,
  `message_queued`, `message_question`, `message_thinking`, `input_area`,
  `send_button`, `cancel_button`, `history_nav`).

### Phase 2 — Unify state (widgets_*.py → ThemeManager)

**Goal:** Single state source of truth; remove the legacy bridge.

Changes:
- Replace `_branch()` in all 5 `widgets_*.py` (common, agent, bulk, mutation,
  orchestra) — currently `return "dark" if is_dark_theme() else "light"` —
  with a token-based resolution. The minimal, lowest-risk change: keep the
  existing dark/light style dicts, but pick the key via
  `is_dark_tokens(ThemeManager.instance().tokens())` instead of
  `is_dark_theme()`. This unifies the *state source* (both code paths now
  read from `ThemeManager`) without rewriting the getter return values.
  Refactoring getters to read token values directly is explicitly out of
  scope for Phase 2 (that belongs to Phase 3).
- Remove the **dark/light branch** half of the legacy bridge ONLY:
  `_effective_theme`, `is_dark_theme()`, `get_current_theme()`. These are
  read exclusively by the 5 `widgets_*.py` `_branch()` helpers (verified),
  so they are safe to delete once `_branch()` is rewritten.
- **KEEP the host-inherit half**: `_current_theme`, `is_host_theme()`,
  `use_native_host_theme()`, `host_stylesheet()`, `maybe_host_stylesheet()`.
  These answer a *different* question ("inherit host Qt palette vs force
  Luc Nhan palette") and are consumed widely (~40 sites: `markdown.py`,
  `markdown_renderer.py`, `chat_view.py`, `input_area.py`,
  `message_widgets.py` via `host_stylesheet`, `panel_core.py`,
  `settings_dialog.py`, and the 3 `build_*_stylesheet` builders). They are
  NOT part of the "dark/light branch" problem and must survive Phase 2.
- Remove the `set_current_theme(..., effective_theme=...)` *effective-theme*
  parameter and its uses at `panel_core.py:1084` and `settings_dialog.py:668`
  (the callers only needed it to feed `is_dark_theme()`, now gone). The
  `set_current_theme` function itself is kept (it still syncs `_current_theme`
  for `is_host_theme()`) but simplified to a single-arg form.
  `settings_dialog.py:668` (ThemeManager.set_mode already drives everything).
- Re-export shim: `styles.py` keeps re-exporting the `widgets_*.py` getters so
  consumers importing from `lucnhan.ui.styles` keep working.

Verify:
- Full `pytest` green; mypy clean.
- **IDA visual**: dark↔light↔ida, every widget class (agent tree, bulk renamer,
  mutation log, tool approval, orchestra/delegation, settings) repaints
  correctly with no drift.

### Phase 3 — Unify inline values (token-driven)

**Goal:** No hardcoded hex in inline `setStyleSheet`; every value comes from
`ThemeTokens`.

Changes:
- Sweep the 204 inline `setStyleSheet(f"...")` calls across 13 files.
  Replace hardcoded hex literals with `tokens.*` references.
- Highest-priority files (most hardcoded hex, ordered by the review):
  1. `plan_view.py` (lines 29-101 — the worst offender: `#808080`, `#d4d4d4`,
     `#007acc`, `#4ec9b0`, `#f44747`, `#569cd6` hardcoded).
  2. `message_widgets.py` (41 calls).
  3. `tool_widgets.py` (36 calls).
  4. `panel_core.py` (27 calls — mostly token-driven already, audit only).
  5. `bulk_renamer.py` (20), `settings_dialog.py` (17), `profiles_tab.py` (15),
     `chat_view.py` (12), `plan_view.py`, `mutation_log_view.py` (7),
     `agent_tree.py` (6), `tools_panel.py` (5), `orchestra_approval_dialog.py`
     (4), `input_area.py` (3), `oauth_consent.py` (1).

Each file is its own commit + IDA visual verification before moving on. This
phase is the longest; split across sessions as needed.

Verify:
- **IDA visual per file**: open the surface(s) that exercise each file's
  widgets, switch theme, confirm correct colors.
- Full `pytest` green after each file.

### Phase 4 — Optional: root stylesheet paradigm shift

**Goal:** Reduce per-widget `setStyleSheet` churn by hoisting common styles
into a single root QSS built from tokens, using objectName selectors.

This phase is **optional**. It is only worth doing if Phases 1-3 leave the
codebase still feeling inconsistent, or if the user wants the full paradigm
shift (Qt-recommended: one stylesheet + objectName, fewer `setStyleSheet`).

Skip by default unless explicitly requested. Document the decision either way.

## Safety nets

- Each phase is its own branch (`feat/theme-phase1`, ...). Each commits
  independently. Each is visually verified in IDA before merging.
- TDD applies to testable logic (e.g. Phase 2 `_branch()` replacement — unit
  test the new resolution before swapping).
- IDA visual verification checklist per phase is the primary quality gate
  (Qt theme cannot be unit-tested).
- If a phase regresses visually, revert the branch — prior phases remain intact.

## Non-goals

- Rewriting the `ThemeManager`/`ThemeTokens`/palette architecture itself — it
  is sound; only its consumers and the dead/legacy surface change.
- Changing the `IDAThemeWatcher` polling mechanism.
- Touching the markdown code-block theme path (`markdown.py` /
  `markdown_renderer.py` already correctly read `is_host_theme()`; after
  Phase 2 removes the bridge, they read the equivalent host check).
- The other Sprint-1 HIGH findings (copy button, jump-to-bottom, etc.) — out
  of scope for this spec; separate work.

## Risks

- **Phase 1 test migration**: 2 test files reference deleted symbols. Must
  update carefully so the test still asserts what it meant to (whitelist
  of stubbed names, not just delete lines).
- **Phase 2 import cycle**: `widgets_*.py` currently lazy-import
  `is_dark_theme` from `styles` to break a cycle. Reading
  `ThemeManager.instance().tokens()` must not reintroduce a cycle
  (`ThemeManager` → `palette_ida` → `_blend_hex` already carefully ordered).
- **Phase 3 surface area**: 204 call sites. Risk of introducing a typo'd
  token name that renders a wrong color only visible in IDA. Mitigation:
  one file per commit, IDA verify each.
- **Phase 3 IDA host theme**: when `is_host_theme()` is true, some styles
  intentionally clear (`host_stylesheet` returns fallback). Must preserve
  that behavior, not blindly token-fill everything.
