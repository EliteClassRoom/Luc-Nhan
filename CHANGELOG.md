# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Multi-tab parallel agents** — multiple chat tabs can now run their agents
  concurrently instead of one-at-a-time. Switching tabs no longer cancels a
  running agent; each tab streams to its own view even while you read another.
  Reuses the proven bulk-renamer pattern (`SubagentRunner` over a shared
  `ToolRegistry`, serialized through IDA's `execute_sync(MFF_WRITE)`).
  - New config: `parallel_agent_enabled` (default `true`) and
    `parallel_agent_max_concurrent` (default `3`), configurable in
    Settings → Behavior. Disable to restore the legacy single-agent behavior
    (tab switch cancels) as a safe fallback.
  - Approval routing is per-tab: an answer/question on tab B reaches tab B's
    runner, not the active tab's. Free-text `USER_QUESTION` answers route to
    the tab that asked.
  - Concurrency cap queues overflow messages and starts them as slots free.

### Changed

- **Mutating tools now serialize** (`ToolRegistry._mutate_lock`) so concurrent
  agents don't interleave IDB writes. This keeps the undo stack coherent
  across tabs. `/undo` remains global (reverses the most recent mutation
  across all tabs) — per-tab undo segmentation is a future enhancement.
- Token-context display is driven by the active tab's usage only, so
  concurrent background tabs no longer make it flicker between values.

### Fixed

- Headless/control API stays backward-compatible: `get_runner`,
  `get_event`, `cancel`, and `on_agent_finished` keep zero-argument defaults
  that target the active tab, so `headless/runner.py` and `control/server.py`
  are unaffected by the multi-runner refactor.

## [1.13.2] - 2026-07-20

### Fixed

- Fixed `get_function_prototype` raising `get_tinfo() takes 1 positional argument but 2 were given`. The tool called `idc.get_tinfo(tif, ea)` (1-arg signature) instead of `ida_nalt.get_tinfo(tif, ea)` (2-arg signature). As the pre-state getter for `set_function_prototype` undo, the swallowed exception had silently produced empty `old_prototype` records, breaking `/undo` for prototype changes.

## [1.13.1] - 2026-07-20

### Fixed

- Fixed Tools tab initialization under PySide6 by using typed Qt alignment flags and suppressing recursive tab-change signals while lazy widgets replace placeholders.

## [1.13.0] — 2026-07-20

### Added

- **Delete individual saved chats from History** — hover or focus a row's
  delete control, confirm permanent removal, and keep the current search
  context. Open chats are protected and focused instead; failed deletion
  remains visible with Retry.

## [1.12.0] — 2026-07-17

### Added

- **Chat History on demand** — a History panel lists saved chats for the
  current IDB (scoped by 32-hex `db_instance_id` with path fallback).
  Opening Rikugan or switching IDBs starts with one empty `New Chat`
  draft; reopen a past chat any time from History. Sessions load
  lazily — only the chats you open are parsed, so large histories no
  longer slow startup. Search filters titles case-insensitively.
- Central memory subsystem (`BinaryMemoryService`) now the sole persistent
  memory path — SQLite structured facts + `MEMORY.md` managed region.

### Changed

- **Opening a chat from History now reuses the empty active tab** —
  a historical chat loads into the current blank `New Chat` draft
  instead of spawning a new tab, so the empty tab no longer lingers
  beside the opened conversation. When the active tab already has
  content, History still opens the chat in a new tab. Re-selecting an
  already-open chat focuses its tab instead of duplicating it.

### Removed

- Removed `startup_restore_sessions`. Older config files may retain the key, but Rikugan ignores it and omits it on the next save.
- Legacy `RIKUGAN.md` runtime read/write. **Legacy `RIKUGAN.md` data is
  not migrated — the old file is ignored.**
- `rikugan/memory/legacy.py` importer (clean break, no migration tool).
- Config flags `memory_workspaces_enabled`, `case_memory_enabled`,
  `peer_retrieval_enabled` (central memory is always-on).

## [1.11.0] — 2026-07-16

### Added — Central Memory Subsystem

- **Central memory workspaces** — per-binary SQLite workspace stores
  (`memory.db`) replace folder-scoped `RIKUGAN.md` / `.rikugan-kb/*.jsonl`
  as the authoritative structured-memory backend. Each workspace has
  deterministic `MEMORY.md` projection with managed/unmanaged region
  separation and cross-process `portalocker` locking. UUID-priority
  identity resolution prevents duplicate workspaces when Windows file
  index changes between opens.
- **Identity resolver** — ordered copy/move/conflict decision table
  resolves IDB identity via filesystem evidence (POSIX `st_dev/st_ino`,
  Windows volume serial + 64-bit file index) and netnode UUID. Raw
  binaries use full SHA-256, hashed before IDA launch in headless mode.
- **Write authority protocol** — non-serializable `MemoryWriteAuthority`
  bound to a frozen `MemoryRunContext`; subagents never receive write
  authority and can only emit `MemoryCandidate` records for explicit
  main-agent review.
- **Prompt source separation** — system prompt reads structured facts
  from SQLite and manual notes from unmanaged `MEMORY.md` when central
  memory is enabled. Legacy `RIKUGAN.md` path used when disabled.
- **Analysis cases** — cross-binary case membership with five relation
  types (`embeds_or_loads`, `communicates_with`, `derived_from`,
  `same_family_as`, `shares_artifact_with`), explicit promotion with
  provenance, lazy source-drift validation, and controlled peer
  retrieval with read-only/query-only opens.
- **`/case` commands** — parser and dispatch for case CRUD, membership,
  promotion, and active-case binding.
- **Bundle interchange** — versioned ZIP format with validated manifest,
  member-name safety checks, coherent SQLite-snapshot export, and staged
  import with graph-wide ID remap.
- **SQLite backup/restore** — `Connection.backup()` API for coherent
  snapshots; restore creates new workspace with updated owner metadata.
- **Storage guard** — centralized path containment, symlink, size, and
  permission checks for all central-memory file operations.
- **Legacy importer** — one-time migration from `RIKUGAN.md` /
  `.rikugan-kb/*.jsonl` into the central workspace, idempotent by
  source fingerprint + target + selected items.
- **Config flags** — `memory_workspaces_enabled`, `case_memory_enabled`,
  and `peer_retrieval_enabled` (all default `False`). Typed-load
  rejection prevents string values from silently enabling features.

### Fixed — Central Memory Runtime

- First-open workspace DB creation (was calling `open()` on missing file)
- SQLite `check_same_thread=False` for agent worker threads
- UUID-priority identity linking when Windows file index changes
- MEMORY.md projection now works after `portalocker` installed in IDA

## [1.10.4] — 2026-07-14

### Changed

- **Docs-review gate moved from pre-execute to post-error** — the IDA docs-reviewer subagent no longer runs before every "complex" `execute_python` script. Scripts now execute immediately after user approval, and the reviewer only spawns when a script fails at runtime with an API-shaped exception (`AttributeError`, `ImportError`, `ModuleNotFoundError`, `NameError`). Logic bugs (`ValueError`, `TypeError`, etc.) do not trigger the reviewer — the main agent handles those itself. This eliminates the LLM round-trip for scripts that would have run fine, making `execute_python` significantly faster for the common case while keeping a safety net for genuine API misuse.
- **Module Quick Reference preloaded into the system prompt** — a compact IDA API router (task → module map with key items) plus verified Core Patterns and Critical rules is now embedded in the main agent's system prompt, reducing API-hallucination rate from the start of a session.
- **Reviewer auto-injects module reference on failure** — when the post-error reviewer runs, it automatically pulls the offline docs of the IDA modules referenced in the failed script and appends them to the tool result, so the main agent can fix the script with full reference context.
- **Config field replaced with an enum** — `require_ida_docs_for_complex_scripts` (boolean) is now `docs_review_mode` (`"on_error"` / `"off"`). Legacy config migrates automatically: `False` → `"off"`, `True` or missing → `"on_error"`. The settings dialog exposes this as a combobox instead of a checkbox.

### Added

- **`rikugan/tools/traceback_classifier.py`** — new pure-function module that classifies an `execute_python` traceback as API-shaped (or not) and extracts the IDA modules referenced in the script, used by the post-error gate to decide whether to spawn the reviewer and which module docs to inject.

### Fixed

- **Traceback no longer leaks into non-`execute_python` tool error results** — when the post-error gate was added, the full Python traceback was inadvertently appended to the error result of every tool. It is now scoped to `execute_python` only (where the classifier consumes it); other tools keep the clean one-liner, so internal paths and line numbers no longer enter the LLM context.

### Security

- All existing `execute_python` invariants preserved: the tool still always requires explicit user approval; the `validate_idapython` static validator still hard-blocks known-hallucinated APIs before execution; the `script_guard` AST check still blocks process-execution and reflective-escape primitives.

## [1.10.3] — 2026-07-13

### Fixed

- **Blank approval button when `ask_user` options contained empty strings** — options are now filtered before rendering so the button row never shows an empty button. Affects mid-turn `USER_QUESTION` flows where a tool returned a mix of populated and empty option strings.

## [1.10.2] — 2026-07-10

### Fixed

- **`execute_python` output was invisible after a run** — the collapse-on-result redesign (1.10.0) made `ExecutePythonWidget.set_result()` hide the result label and block frame after receiving the output, so the user saw only `▶ ● execute_python ✓` with no output until manually expanding. The collapse/expand toggle and all associated state flags have been removed entirely. Output now renders in an always-visible, scrollable read-only editor (`QPlainTextEdit`) whose height tracks the content up to 15 lines, then scrolls. Error output is shown in the same block in red with a `✗` icon; the docs-review blocked summary is shown in full (no "click for details" indirection).

### Changed

- Added `get_tool_result_editor_style(text_color=None)` to `rikugan/ui/theme/widgets_mutation.py` — a token-driven QSS builder for the result editor that lets the caller override the foreground color for error output. (QSS keeps the first matching rule, so the color override replaces the `color:` value inside the `QPlainTextEdit` rule rather than appending a second rule.)

## [1.10.1] — 2026-07-10

### Fixed

- **Double-spacing between paragraphs** — each paragraph `<div>` ended with a trailing `<br>` on top of its block-level close tag, so Qt rich text rendered a second blank line. Surfaced as a visible extra blank line between every two paragraphs in the thinking block and the assistant bubble. Removed the trailing `<br>`.
- **Large empty gap inside assistant bubbles after restore/resize** — `QLabel.setFixedHeight` poisons `heightForWidth`: once a fixed height is set, a later `heightForWidth(w)` echoes the cached value for any width instead of recomputing. On the restore path (and after a resize) `_HeightCachedLabel.pin_height` ran again with a different width, but the poisoned call returned the stale height, so the wrong height was re-locked and the bubble rendered far taller than its text (a ~150-240px gap on the last assistant message). `pin_height` now clears the min/max height constraints before measuring, and re-pins inside `resizeEvent` so the height tracks subsequent width changes. `hasHeightForWidth` stays `False`, preserving the O(N × msg_length) layout-cascade optimisation.

## [1.10.0] — 2026-07-09

### Added

- **Unified `execute_python` widget** (`ExecutePythonWidget`) — the tool's full lifecycle (code preview → docs-review status → approval buttons → result) now renders in one card instead of the previous split `ToolCallWidget` + `ToolApprovalWidget` pair. A single header toggle expands/collapses all content.
- `DOCS_GATE_STATUS` event type — the docs-review gate now reports its state (`running` / `approved` / `blocked` / `failed`) as a UI-only signal keyed by `tool_call_id`, instead of mixing progress text into the assistant bubble and persisting it to history.

### Changed

- Docs-review messages no longer leak into the assistant message or session history. The status renders as a compact one-line row inside the `execute_python` card; a blocked review shows a short header with the reviewer summary in an expandable detail.
- `execute_python` approvals inside a collapsed multi-tool group are now **promoted out** of the group so the Allow button is never hidden behind a collapsed header (previously looked like a hang while the loop waited on approval).
- Result content and code editor are collapsed by default; a long script output no longer dominates the chat.
- `_describe_tool_call` returns an empty description for `execute_python` — the unified widget renders its own code block, so the previously duplicated first line is gone.

### Fixed

- **Critical:** `ExecutePythonWidget` was missing `append_args_delta`, which `ChatView` calls on every `TOOL_CALL_ARGS_DELTA`. Every `execute_python` call from a streaming provider (Anthropic, OpenAI, Codex, Gemini) crashed the UI with `AttributeError`. Added as a no-op (code renders on `TOOL_CALL_DONE`).
- Docs-gate `FAILED` (reviewer exception) now falls through to user approval instead of hard-blocking. A subagent crash is an infrastructure fault, not a script fault.
- No duplicate reviewer summary: when the docs gate blocks, `set_result` skips rendering the result block (the summary already lives in the collapsible status line).
- No empty gap below the widget when collapsed — the code section and result block frame are hidden alongside their content.

## [1.9.1] — 2026-07-08

### Added

- `lookup_idapython_doc` gains an optional `name` parameter for cheap point-lookups. `lookup_idapython_doc(module="ida_typeinf", name="apply_cdecl")` returns ~20 lines of context around each match, no user approval required. Makes verifying whether a specific function exists much cheaper than `hasattr()` or `inspect.signature()` probes.
- README at `rikugan/data/idapython-docs/README.md` — documents the bundle, the right/wrong way to access it, and the update command.

### Changed

- Main agent system prompt (`rikugan/agent/prompts/base.py`) now includes a "Verifying APIs with the offline docs tool" section that explicitly names `lookup_idapython_doc`, mentions the `name=` parameter for point-lookups, and forbids `os.path.open()` / `pathlib.Path.read_text()` direct file access to the bundle.
- Main agent prompt and `ida-scripting` SKILL.md now call `hasattr()` / `inspect.signature()` in `execute_python` scripts as an anti-pattern: prefer the docs tool for API verification.
- `ida-scripting` SKILL.md frontmatter `triggers` list extended with `ida_frame`, `idaapi`, `ida_ua`, `ida_nalt`, `ida_ida`, `ida_lines`, `idc` so the skill auto-activates on more module mentions.

### Fixed

- Docs-reviewer gate fallback semantics: `web_fetch` against Hex-Rays is now strictly a last resort, only triggered after `lookup_idapython_doc` either reports the module is not in the bundle OR was consulted but did not resolve the verification. (Previously LLM could skip offline and reach for `web_fetch` directly.)

## [1.9.0] — 2026-07-08

### Added

- **Offline IDAPython docs bundle** — the docs-reviewer subagent now ships its own copy of the Hex-Rays Python reference (`rikugan/data/idapython-docs/`, 54 modules, ~1.94 MiB raw RST). Replaces network fetches to `python.docs.hex-rays.com` (which returns `403 Forbidden` on deep-link HTML pages due to bot protection) with deterministic, offline reads.
- `lookup_idapython_doc(module, offset, limit)` tool (`rikugan/tools/idapython_docs.py`) — reads from the bundled RST source. Strict path-traversal prevention (regex `[a-z0-9_]+`); missing modules return a clear error listing the 54 available modules.
- `scripts/build_idapython_docs.py` — stdlib-only CLI for fetching and rebuilding the bundle: `python scripts/build_idapython_docs.py` for full build, `--verify` for drift detection against upstream. Atomic writes (tempfile + fsync + `os.replace`), 3x exponential-backoff retry on transient network errors.
- IDAPython docs-review gate prompt refined: web_fetch is now strictly the LAST resort, only triggered after `lookup_idapython_doc` either reports the module is not in the bundle OR was consulted but didn't resolve the verification.

### Changed

- Reviewer prompt section B and `ida-scripting` SKILL.md "When to fetch more" both lead with `lookup_idapython_doc`; `web_fetch` against Hex-Rays is documented as fallback only after offline lookup fails.

### Security

- `lookup_idapython_doc` accepts only module names matching `^[a-z0-9_]+$` — rejects path-traversal attempts (`../`, `foo/bar`, uppercase, empty, null bytes, URL-encoded). Verified by 6 explicit tests including a real-FS "does not read outside DOCS_DIR" check.

## [1.8.0] — 2026-07-06

### Breaking

- **Dropped PyQt5 support.** Rikugan now uses PySide6 (Qt6) exclusively. Minimum IDA Pro version is **9.0** (all 9.x releases ship PySide6 as their primary binding; IDA 9.x's `PyQt5` module is a thin shim over PySide6 and is no longer used). Users on IDA 8.x or Qt5-only hosts must stay on `1.7.0`.
- `ida-plugin.json` `idaVersions` is now `[">=9.0"]` (range) instead of an explicit version list. If the IDA Plugin Manager does not parse range syntax and fails to list Rikugan, install manually via the plugin directory.

### Fixed

- IDA 9.1 crash: `QVBoxLayout(QWidget): argument 1 has unexpected type 'PySide6.QtWidgets.QWidget'`. Root cause was `_detect_binding()` in `rikugan/ui/qt_compat.py` selecting PyQt5 when another plugin had pre-imported it into `sys.modules`, while the host actually ran PySide6. The entire detection layer is removed; Qt symbols now come from PySide6 unconditionally.

### Removed

- `rikugan/ui/qt_compat.py`: `_detect_binding()`, `QT_BINDING`, `is_pyside6()`, `qt_flags()`, `qt_run()`, and the PyQt5 import branch.
- `rikugan/ida/ui/panel.py` and `rikugan/ida/ui/tools_form.py`: the `FormToPyQtWidget` / `FormToPySideWidget` try-except branch — `OnCreate` now calls `FormToPySideWidget(form)` directly.
- `rikugan/tests/conftest.py`: PyQt5 fallback in the `qapp` fixture import.

### Changed

- `rikugan/ui/qt_compat.py` is now a thin PySide6 re-export layer (kept as the single Qt import seam). Call sites that used `qt_flags(A, B)` now use `A | B`; `qt_run(x)` now uses `x.exec()`.

## [1.7.0] — 2026-07-03

### Added

- `naming-convention` skill (`rikugan/skills/builtins/naming-convention/`) — comprehensive naming standard covering functions, variables, globals, structs, enums, and typedefs, plus edge cases (wrappers/thunks, C++ mangling, Go/Rust, vtable) and a confidence-based escalation ladder with `Unknown_<Hint>_<addr>` placeholders.

### Changed

- **BREAKING (behavior):** `bulk_renamer` Quick and Deep prompts now generate PascalCase function names (`InitializeGlobals`) instead of snake_case (`initialize_globals`). This unifies bulk-rename output with the system prompt and the new `naming-convention` skill. Existing IDBs are NOT migrated — old snake_case names persist; only new renames follow the standard. If you relied on snake_case output from Bulk Rename, regenerate names for affected functions.
- `RENAMING_SECTION` in the system prompt (`rikugan/agent/prompts/base.py`) expanded from 3 naming rules to 6 (now covers variables, enums, typedefs) and references the `/naming-convention` skill for edge cases. Also removes the ghost-tool reference to `rename_multi_variables` (which never existed).
- `malware-analysis` and `generic-re` skills: naming sections expanded from 1-3 rules to the full 6-rule summary, cross-referencing `/naming-convention`.
- Removed ghost-tool references to `rename_multi_variables` in `rikugan/agent/exploration_mode.py` and `rikugan/agent/modes/research.py` (the tool never existed — agents in `/explore` and research modes would attempt to call it and waste a turn).

## [1.6.0] — 2026-07-02

### Added

- `set_runtime_config` wiring in `rikugan/web/__init__.py` (fixes silent `getattr` no-op; security-constant-real-bug step 2).
- `EXECUTE_PYTHON_TOOL_NAME` constant in `rikugan/constants.py` (security-constant-real-bug step 1).

### Fixed

- CI: master trigger + push hook + concurrency + Python 3.12 matrix (`f191722`).
- CI: base_ref diff guard for push events + runtime deps install in test job (`d2545d0`).
- CI: pin Python 3.11 via uv for reproducible desloppify scores (`d518cb6`).
- Release: sync version check across 3 sources (`4270f69`).
- Subprocess injection guard in a2a (port from fork `57caf5e`).
- `a2a_widget` threading model refactored from `QThread`/`_A2AWorker(QObject)` to `threading.Thread` + `queue.Queue` + `QTimer` polling.

### Refactor / Quality

- Pseudo tool schemas extracted to `rikugan/agent/pseudo_tool_schemas.py` (6 of 7 schemas imported into `loop.py`; `DELEGATE_EXTERNAL_TASK_SCHEMA` import pending — see Phase 2).
- Purged IDA 8.x `ida_struct` paths from `types_tools.py` (step 8 of dead-code-purge).
- Removed duplicate `completed_tool_call_ids.add()` at `loop.py:775` (step 7).
- Removed 58 empty legacy `{dark:'', light:''}` dict constants (step 6).
- Applied ruff format to 9 files + removed invalid `noqa` (step 5).
- Extended `sanitize.py` with `strip_lone_surrogates` + `sanitize_messages_for_provider`.

### Tooling

- Added Dependabot weekly config with grouped updates.
- Added `CODEOWNERS` for security and CI paths.

## [1.6.1] — 2026-07-02

### Fixed

- `delegate_external_task` pseudo-tool is now visible to the LLM. The handler (`_handle_delegate_external_task_tool`) and dispatch (`elif tc.name == "delegate_external_task"`) were previously unreachable because `DELEGATE_EXTERNAL_TASK_SCHEMA` was never appended to the tool list in `_build_tools_schema`. The schema is now imported and wired in `rikugan/agent/loop.py` (C.4 final step).

## [1.5.0] — 2026-06-29

### Added

- **Tool substitution guard** (`rikugan.tools.tool_substitution`): when the agent
  calls `execute_python` with a script that re-implements an existing dedicated
  tool, the tool now emits a non-blocking suggestion pointing at the dedicated
  alternative. Suggest-only — the script still runs, but the LLM sees the
  hint and learns the pattern for future turns. Mapping table covers
  imports/exports/strings/functions/xrefs/segments plus contributed entries
  for annotations, decompiler, disassembly/IL, and type/struct APIs.
- **`search_imports` and `imports_by_module` tools**: fill the capability gap
  where `list_imports` could only return the full set. The LLM no longer
  needs to script a custom filter to find imports by name or by DLL.
- **Categorized tool catalog in the system prompt** (`format_tools_catalog`):
  the `## Available Tools` section is now a per-category markdown table
  with one-line description hints, replacing the bare comma-separated list.
  The LLM can scan it to find the right tool without reading the full
  provider schema.

### Changed

- **Tool descriptions use the full docstring**, not just the first line.
  Each `database` tool now documents its output format, capacity limits,
  and sibling tools (search/filter variants) so the LLM has enough
  context to choose correctly instead of falling through to `execute_python`.
- `list_imports`, `list_exports`, `list_segments`, `get_binary_info`,
  `read_bytes`, and `read_global_value` got multi-line docstrings
  covering output format and "use the search variant when filtering".

### Security

- Tool-substitution layer is suggest-only; no new auto-approval path.
  `execute_python` still requires explicit user approval for every call
  regardless of suggestion presence.

## [1.4.0] — 2026-06-26

### Changed

- **Breaking:** Removed the obsolete `ida-docs` and `ida-pro-mcp` built-in skills.
  These were no longer maintained and have been deleted from `rikugan/skills/builtins/`.
- Bumped version to 1.4.0.

### Security

- Closed a markdown XSS vector: the `javascript:` scheme (and other executable
  URL schemes) in markdown links are now stripped before rendering, so they no
  longer become clickable `<a>` tags in the Qt rich-text panel.
- Hardened the `execute_python` script sandbox (`script_guard.py`) and added
  SSRF guards.
- Marked the SHA1 hash used as an HTML-cache key in `markdown.py` as
  `usedforsecurity=False` — it is a non-security cache key, not a cryptographic
  primitive.
- Refactored the headless `--token` validation into dedicated helpers
  (`_validate_token_format` / `_reject_bad_token_format`) that keep the
  rejection text separate from the secret-bearing variable.
- Documented why the sandboxed `exec` in `microcode_optim.py` (user-supplied
  optimizer code run inside `safe_builtins`) is intentional.

### Removed

- Deleted `rikugan/providers/auth_compat.py` — a 61-LOC compatibility shim
  ported from the upstream fork whose two public functions had zero callers.

### Fixed

- Silenced spurious `themeChanged` disconnect warnings.
- Curated the subprocess environment passed to a2a agents and tightened event
  typing.

### Refactor / Quality

- Extracted markdown export helpers from `panel_core.py` into a dedicated
  `rikugan/ui/export_formatting.py` module (`panel_core.py` 2039 → 1937 LOC).
- Documented the mutable-state contract on `RikuganConfig` (instances owned by
  the host entry point, intentionally mutable for settings edit-in-place).
- Replaced silent `except: pass` blocks in `a2a/registry.py` and
  `bulk_renamer.py` with `log_debug` so best-effort failures are traceable.
- Added `from __future__ import annotations` to `pseudo_tool_schemas.py`.
- Extracted the `_PARSE_ERROR_PREVIEW_BYTES` magic constant in `mcp/protocol.py`.
- Added `TestMutationContractConsistency` — 6 tests enforcing that every
  mutating tool's reverse builder has a matching `capture_pre_state` branch
  (and vice versa), plus graceful failure handling for unknown tools.
- Scoped mypy per-file error overrides for shiboken-safe modules.

### Tooling

- Added `pytest`, `pytest-cov`, `ruff`, and `mypy` to the uv dev
  dependency-group so `uv sync` provisions a working local CI environment.
- Added `ci-local.ps1`, a PowerShell port of `ci-local.sh` for Windows
  development (uses `uvx` for ruff/mypy, `uv run python -m pytest` for tests).

## [1.3.0]

### Added

- Pygments syntax-highlight caching keyed by `(code, lang, style)` to speed up
  repeated markdown renders.
- Skill rescan is now skipped on non-skill config changes.

### Changed

- Unified the theme system: routed widget QSS through theme tokens, fixed
  contrast failures, removed dead `LIGHT_THEME` / `DARK_THEME` constants and
  the `build_theme_stylesheet` helper.
- Re-applies the IDA host's minimal style on theme change via the extracted
  `_reapply_minimal_style` helper.

### Fixed

- Disabled raw HTML in markdown to close an earlier injection vector.
- Removed a confusing dead Orchestra tab and dialog class.
- Reverted config when the settings dialog is cancelled.
- Named the actual tool in the approval gate and rendered its description.

## [1.2]

Initial public release line.
