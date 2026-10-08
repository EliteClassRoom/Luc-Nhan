# Task 1 — Compact Workspace và theme integration (report)

**Status:** DONE_WITH_CONCERNS (all acceptance criteria met; two deviations documented below for the integration owner)
**Branch:** `feat/native-ui-ux-redesign` — commit `c67e46b` "feat(ui): compact native workspace + G1/G2 theme integration" (local only, not pushed)
**GUI contract sections applied:** G0, G1, G2, G3, G9, G10, G11

---

## 1. Files changed

| File | Change |
|---|---|
| `lucnhan/ui/panel_core.py` | Header builder + footer builder + overflow menu; workspace composition; narrow/wide presentation; `_build_input_section` (composer), `_build_action_buttons` (secondary strip); `_AddButtonTabBar` → session strip; `_set_running` drives the header status; obsolete `_MODE_BAR_STYLE_TEMPLATE` / `_tab_label` removed |
| `lucnhan/ui/context_bar.py` | G2 geometry (27 px row, padding 4/12, gap 9), `CONTEXT` label, symbol-before-address order, `apply_theme()`, `set_narrow()` (secondary metadata only), module-level `NARROW_BREAKPOINT_PX` |
| `lucnhan/ui/input_area.py` | G2 composer minimum height (38 px; panel pins the 44 px baseline) |
| `lucnhan/ui/theme/widgets_common.py` | G1 radius 4 px / 1 px borders / 2 px accent focus outline / 12 px chrome type; G2 session-strip + header + context-row + footer builders (new getters) |
| `lucnhan/ui/theme/palette_dark.py`, `palette_light.py` | G1 target palette (all 20 tokens) |
| `lucnhan/ui/styles.py` | `build_input_area_stylesheet` → G1 radius 4 px, G2 padding 5/8, 13 px body |
| `tests/qt_stubs.py` | Added the one missing stub method (`setAutoRaise`) used by the header/`+` buttons |
| `tests/ui/test_action_button_layout.py`, `tests/ui/test_ui_a11y_fixes.py`, `tests/ui/test_input_visibility_and_resize.py`, `tests/tools/test_panel_core.py` | Adapted to the new composition; added new coverage (see §5) |

**Outside the allowed file list (flagged as required):** `lucnhan/ui/styles.py` (composer QSS geometry is part of G2 and the builder lives there — `build_input_area_stylesheet` is the composer surface this slice owns), `tests/qt_stubs.py` (shared Qt stub lacked `QToolButton.setAutoRaise`, needed by the new header/`+` buttons), and the four test files listed above.

---

## 2. Workspace composition (G3)

```
LucNhanPanelCore (QVBoxLayout, margins 0, spacing 0)
├── header        QFrame #panel_header      min-height 38, padding 3/10
├── session_strip QFrame #session_strip     min-height 31, inset 9, gap 7  (tab bar + "+", hidden with 1 tab)
├── context_row   ContextBar                min-height 27, padding 4/12, gap 9
├── content_area  QStackedWidget            stretch 1  (chat main splitter | tools)
├── composer      QWidget #composer         padding 7/10/5; editor 44 px + action row
└── footer        QFrame #panel_footer      min-height 25, padding 2/11
```

- Header right side, wide: `Chat` `Tools` `History` `Settings` + `⋯` overflow.
- Overflow entries (Export, History, Settings, Mutations) are built from the **same button instances** and fire their existing handlers via `action.triggered → btn.click`; check state stays in sync. No second stateful copy of any action.
- Send/Stop stay beside the composer; `/ Commands` and the keyboard hint fill the left/centre of the same action row (hint uses `QSizePolicy.Ignored` so it can never widen the dock).
- `New` exists only as `+` on the session strip (`_tab_bar._add_btn`), with tooltip + accessible name; Ctrl+T unchanged.
- Secondary actions are never duplicated beside the input.

**Duplication removed:** the old `_MODE_BAR_STYLE_TEMPLATE` Chat/Tools `QTabBar` was a second, parallel navigation surface; `Chat` now toggles `_mode_stack` index 0 and `Tools` index 1. `_mode_bar` is gone; `show_tools_panel`, `_on_mode_changed`, `_on_toggle_tools` drive the stack directly.

**Composer splitter:** the existing draggable vertical `_chat_splitter` was repurposed rather than deleted: it now holds only the composer, pinned to the contract baseline (44 + 34 = 78 px) with stretch 1 so *growing* the composer takes height from the transcript instead of pushing the footer off-screen. `_seed_chat_splitter` runs once from `showEvent` via `QTimer.singleShot(0, …)`.

---

## 3. Palette and geometry (G1, G2)

`DARK_TOKENS` / `LIGHT_TOKENS` are exactly the G1 target table (window `#202427`/`#ffffff`, text `#e0e5e7`/`#252e2a`, accent `#86c7b8`/`#326d5b`, highlight_text `#142621`/`#ffffff`, selection `#293e38`/`#e0ece5`, success/warning/error per table, etc.). `palette_ida.py` untouched — AUTO/IDA still follow the host `QPalette`; all new chrome styles go through `maybe_host_stylesheet(...)` so host mode stays unstyled.

Geometry is centralised as module constants in `panel_core.py` (`_HEADER_MIN_HEIGHT=38`, `_HEADER_GAP=10`, `_HEADER_GAP_NARROW=5`, `_SESSION_STRIP_MIN_HEIGHT=31`, `_COMPOSER_TEXT_HEIGHT=44`, `_FOOTER_MIN_HEIGHT=25`) and token-driven style builders in `widgets_common.py`. No per-widget hardcoded colours, no second stylesheet pipeline, no new dependency.

---

## 4. Responsive behaviour (G3) — verified in IDA GUI

`resizeEvent` → `_apply_narrow_presentation(width <= 640)`: header gap 10→5 px, History/Settings hidden (reachable with label + tooltip via overflow), context secondary metadata (MODEL/TOKENS) hidden — symbol, address, Stop/Send, overflow and the `+` stay. Only presentation changes: no transcript/session rebuild, draft + caret + focus + runner survive 640/641, tab switching and theme switches.

---

## 5. Tests

- Adapted (old incidental layout wording removed): action-button layout/a11y lists (now include `_chat_btn`, drop `_new_btn`), `InputArea` 60 px → 38 px min height, `showEvent` seeding → `_seed_chat_splitter`, chat-splitter pane assertions, `_history_btn` source guard moved to `_build_header` + `_apply_narrow_presentation`, tab-bar-visibility guards now target the strip container.
- Added: header navigation order (`Chat, Tools, History, Settings`), no duplicate action instances, composer editor on top of the action row, strip visibility at 2+ tabs, narrow presentation matrix.

`python -m pytest tests/ -q` → **5 failed, 3997 passed, 68 skipped**. The 5 failures are the pre-existing `tests/tools/test_web_ssrf.py` `ModuleNotFoundError: No module named 'requests'` cases described in the brief (unchanged baseline). No regression.

---

## 6. Native smoke evidence (IDA GUI 9.4)

Script: `.superpowers/sdd/2026-10-08-native-ui-ux-redesign/task-1-smoke-ida.py`
Launcher: `.superpowers/sdd/2026-10-08-native-ui-ux-redesign/task-1-smoke-launch.ps1` (`ida.exe -t -A -S"<script>"`, empty DB, project IDAPython interpreter)
Output: `.superpowers/sdd/2026-10-08-native-ui-ux-redesign/task-1-smoke-ida.json` → **`ok: true`, 32/32 checks passed**

Covered: region order and per-region heights (38 / 31 / 27 / 25), composer full width at 1100 and 460, draft + focus retained across 1100 → 641 → 640 → 460, Stop visible + Send = "Queue" + header status text at every width, narrow compaction (gap 5, History/Settings hidden, secondary metadata hidden, symbol/address kept), no horizontal overflow, 6 session tabs natively scrollable at 460 with `+` on the strip, Light/Dark theme switches preserving draft and runner, overflow actions labelled and backed by the existing buttons, Stop hidden after cancel.

**Not verified (honest gaps):**
- **No native screenshots.** The smoke run is headless-ish (`-A` with an empty database, no `grab()` capture). G11's screenshot comparison gate is Task 7's integration job; this slice supplies geometry/behaviour evidence only.
- **Streaming a real turn + Stop against a live provider** was not exercised (no provider/API authorisation for this slice). The run covers `_set_running` state transitions (Stop visible, Send → Queue, status text) rather than an actual agent run.
- **Focus retention** is asserted via `hasFocus()`; it passed under IDA's `-A` launch, but an interactive pointer-driven session was not simulated.
- DPI 150% was not measured (Task 7 evidence).

---

## 7. Concerns / deviations for the integration owner

1. **Session strip implementation.** The tab bar is *hosted* in a dedicated strip container next to `+` rather than inside the `QTabWidget`. Qt reserves ~30 px of chrome for a tab bar inside the tab widget, which pushed the strip to 64 px (contract: 31 px) and hid `+` whenever the tabs overflowed into scroll buttons. Re-parenting keeps the strip at 31 px, keeps every tab reachable via native scroll buttons, and keeps `+` visible — but it is a native-mechanics change worth a visual confirmation in Task 7.
2. **Composer splitter repurposed** (single pane, composer at the G2 baseline, growing takes height from the transcript). No existing behavior was dropped — the drag handle, `setChildrenCollapsible(False)` and 4 px handle width are unchanged — but the *direction* of the drag changed (previously the splitter divided transcript/composer). If a reviewer prefers the original two-pane splitter inside the content area, that is a contained follow-up.
3. **`_export_btn` is now a hidden placeholder button** carrying Export's tooltip/accessible name and its `_on_export_current` handler; the reachable control is the overflow menu entry bound to the same instance. `tests/ui/test_action_button_layout.py` pins its existence.
4. **`InputArea.setMinimumHeight` 60 → 38 px** and the composer editor is pinned to a fixed 44 px baseline. This follows G2 ("composer text baseline 44 px, min 38 px") but removes the old 2–3 line default; with the transcript above, this is the intended density. Flagging because two existing tests encoded the 60 px value (updated, not deleted).
5. **Context row labels changed** from `Addr:` / `Func:` / `Model:` / `Tokens:` to `CONTEXT` + symbol + `ADDR:` + `MODEL:` + `TOKENS:` per G3 ordering. No test or caller depended on the old strings.
6. **`_ApplyHistoryLoaded`/history-pane width** is unchanged in this slice; Task 3 owns the narrow overlay behaviour for History/Mutations.
---

## Fix round 1 (review findings) — commit `de7b048`

| Finding | Fix |
|---|---|
| **P0-1** root-layout `addWidget(self._tab_widget, 1)` stole the chat pane from `_main_splitter` (context row rendered below the transcript; blank content region in Chat mode) | Deleted the root-layout add; only `_session_strip` is a root-level region. `_mode_stack` (which owns `_main_splitter`) now goes into the vertical chat splitter. Verified: `_main_splitter.count() == 3`, root layout is `header, [banner], strip, context, chat_splitter, footer`, region y-order `0 / 38 / 69 / 96 / 693 / 775` |
| **P1-2** one-pane splitter + `setFixedHeight(44)` removed composer resizing | `_chat_splitter` is two panes again (`mode_stack | composer`) with `stretchFactor(0,1)/(1,0)`, 4 px handle, `setChildrenCollapsible(False)` — the pre-existing drag-to-grow behavior is restored. Editor is now `setMinimumHeight(38)` with **no** pinned maximum and stretch 1 inside the composer. Verified offscreen: `[593, 82]` → drag to `[375, 300]`, editor 38 → 256 px |
| **P1-3** dependency-warnings banner created but never added to a layout | Restored `layout.insertWidget(1, self._dependency_banner)` under the header, only when warnings exist |
| **P1-4** `+` new-chat invisible with a single tab | `_update_tab_bar_visibility` now keeps `_session_strip` visible (`setVisible(True)`); the tab bar still only labels tabs |
| **P1-5** composer controls unstyled until `themeChanged` | `_build_ui` ends with `_apply_action_button_styles()` + `_composer_hint_style()`; verified: send/cancel/hint/mutations all carry a stylesheet at build |

**Smoke hardening (so P0-1 cannot regress silently):** `region_order` now measures `_session_strip` (not `_tab_widget`), plus new structural checks — `tab_widget_inside_main_splitter`, `chat_splitter_two_panes`, `content_above_composer`, `composer_resizable`, `composer_controls_styled_at_build`, `new_chat_visible_with_single_tab`.

**Tests updated:** `TestUpdateTabBarVisibility` (strip always visible), `TestChatSplitterWrapsMainAndInput` (two-pane + no root-layout reparent of the tab widget).

**Commands and output**
- `python -m pytest tests/tools/test_panel_core.py tests/ui -q` → `523 passed, 47 skipped, 32 subtests passed`
- IDA GUI smoke (`ida.exe -t -A -S task-1-smoke-ida.py`) → `ok: true`, **38/38 checks passed** (evidence: `task-1-smoke-ida.json`)
- `python -m pytest tests/ -q` → `5 failed, 3997 passed, 68 skipped` — the 5 are the pre-existing `tests/tools/test_web_ssrf.py` `ModuleNotFoundError: No module named 'requests'` baseline; no regression
- `python -m ruff check` / `ruff format --check` on the touched files → clean
