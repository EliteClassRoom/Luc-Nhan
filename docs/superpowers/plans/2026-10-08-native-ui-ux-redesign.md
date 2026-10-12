# Luc Nhan Native UI/UX Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use superpowers:subagent-driven-development only when independent, sufficiently large slices justify delegation. **Mọi agent MUST đọc toàn bộ [GUI Design Contract](2026-10-08-gui-design-contract.md) trước khi làm bất kỳ task nào; không tự đổi visual direction.** Steps use checkbox (`- [ ]`) syntax for tracking. Do not commit or push without explicit user authorization.

**Goal:** Triển khai toàn bộ UI/UX compact đã được người dùng chốt, gồm Workspace, Settings, Tools và quick model/thinking picker, bằng Qt native trong plugin IDA.

**Architecture:** Giữ các widget, controller, TurnEvent và hệ thống theme hiện có; thay bố cục và cách trình bày, không viết lại agent loop. Popup model dùng catalog đã có trong bộ nhớ, nhóm theo connection và cập nhật lựa chọn qua một đường controller riêng, không chạy lại toàn bộ Apply Settings. Không nhúng HTML preview vào plugin.

**Tech Stack:** Python >=3.11 theo `pyproject.toml`; PySide6/Qt6 qua `lucnhan.ui.qt_compat`; IDA >=9.0; pytest và workflow kiểm tra hiện có. Không thêm dependency UI hoặc đổi package manager/lockfile.

**Spec:** Layout được chốt trong hội thoại ngày 2026-10-08; [GUI Design Contract](2026-10-08-gui-design-contract.md) là source of truth bắt buộc cho visual language, geometry, states và responsive behavior. Phần “Thiết kế đã chốt” dưới đây giữ behavior contract; GUI contract thắng nếu mô tả visual cũ mâu thuẫn. Reference trực quan: `design-preview/index.html`, các PNG được map trong GUI contract G10. Evidence hiệu năng: `design-preview/benchmark-results.json`, `design-preview/benchmark-native-first.json`, `design-preview/benchmark-native.json`, `design-preview/benchmark-browser.json`.

**Status:** Redesign đã được tích hợp vào plugin, gồm workspace, Settings/Tools và grouped model picker. Tài liệu này lưu kế hoạch gốc để đối chiếu; các checkbox và nhãn “proposed” bên dưới không phải trạng thái triển khai hiện tại. Benchmark ở §2 đo prototype trước tích hợp, không chứng minh hiệu năng của toàn bộ plugin.

## Global Constraints

- Dùng interpreter trong `IDAPYTHON_VENV_EXECUTABLE` cho mọi Python tooling. Không cài global, đổi PATH hoặc thay venv IDAPython.
- Tất cả Qt/IDA operations chạy trên main thread. Worker giao kết quả qua queue + QTimer; không dùng Qt signals xuyên thread.
- Không import C extensions/SDK mới trong lúc Qt signal đang dispatch. Reuse đường pre-initialization hiện có; việc import SDK/IDA modules không được chuyển tùy tiện sang worker.
- Host API imports dùng `importlib.import_module()` trong `try/except ImportError`; Qt symbols chỉ import qua `lucnhan.ui.qt_compat`.
- Theme đi qua `ThemeTokens`, stylesheet builders và ThemeManager hiện có; không tạo palette hoặc global stylesheet thứ hai, không ảnh hưởng IDA host.
- Không thay đổi semantics của streaming, cancel, compaction, history persistence, mutation/undo, script guard, OAuth consent hoặc credential encryption để phục vụ UI.
- `execute_python` vẫn cần Allow/Deny rõ ràng, không Always Allow và không auto-approve. Trong code, tên tool dùng `EXECUTE_PYTHON_TOOL_NAME`.
- Giữ sanitize tại tất cả binary/MCP/persistence boundaries; provider/model labels và tool output phải được escape đúng trước khi đi vào rich text.
- Không đọc credential thật để phát triển/test, không đưa token/key/password vào screenshot, tooltip, log hoặc benchmark artifact.
- Giữ behavior headless; không thêm import Qt vào đường headless controller.
- Không tự commit, push, bump version hoặc triển khai plugin ở task viết plan này.

- GUI contract G0–G11 áp dụng cho mọi task, kể cả delegated slices. Shared layout/theme có một integration owner; slice agents không tự thay palette, spacing, navigation hoặc breakpoint. GUI completion cần native screenshots + visual review, không chỉ unit tests.

## 1. Thiết kế đã chốt

### Workspace

1. Header trên cùng: Luc Nhan/trạng thái agent trái, Chat/Tools/History/Settings phải. New chat dùng `+` trên session strip; Export và action thứ cấp vào overflow theo GUI contract G3. Không lấy chỗ ngang của composer để đặt cả dãy nút.
2. Session tabs compact, tab active rõ ràng; giữ các hành vi multi-tab và running tab hiện có.
3. Context row riêng: binary/function/address hoặc context thật đang có. Không lấy dữ liệu demo trong mockup đưa vào product.
4. Chat ưu tiên chiều rộng; giữ renderer/streaming hiện có. Tool activity là các hàng compact, mở chi tiết theo yêu cầu.
5. Composer full-width, giữ draft, autocomplete, skill suggestions và Enter/Stop semantics hiện có. Send/Stop ở vị trí dễ thấy; không làm mất input khi đổi model/theme/dock width.
6. Footer trái: `Connection / Model · Thinking level`, click mở popup neo phía trên. Footer phải có elide + tooltip, không gây horizontal overflow.
7. Footer phải còn thể hiện token/context usage và binary/session metadata thật nếu đã có; không tạo telemetry mới.
8. History/Mutations chỉ mở theo yêu cầu. Wide dock dùng pane bên phải; narrow dock phủ vùng nội dung, có Close/Back và không ép chat/composer thành cột siêu hẹp.

### Model/thinking picker và nhiều provider

- Nhóm theo **configured connection**, không chỉ theo provider family. Ví dụ Work OpenAI và Personal OpenAI là hai nhóm khác nhau dù cùng model ID.
- Identity của model là `(connection_name, model_id)`. Không deduplicate hoặc cache chỉ theo `model_id`.
- Reuse tên built-in/custom connection hiện có trong `LucNhanConfig.providers` và `custom_providers`. Hai OpenAI-compatible endpoints dùng hai custom connection được đặt tên riêng; không cần schema provider-account mới.
- Có search local theo connection/model, active checkmark và thinking selector. Keyboard: focus rõ, arrows/Enter cho selection, Escape đóng và trả focus về trigger; click ngoài đóng.
- Không fetch catalog khi mở popup hoặc gõ search. Nguồn catalog là kết quả model discovery đã cache; chưa có catalog thì hiển thị saved/current model thật, kèm đường vào Settings để Refresh hoặc nhập model ID. Không dùng danh sách giả, không coi trạng thái chưa fetch là lỗi mạng.
- Cache popup lazily; chỉ rebuild catalog khi catalog/connection thực sự thay đổi. Đổi thinking chỉ cập nhật thinking state, footer và config liên quan.
- Lựa chọn footer và Settings dùng cùng config source of truth. Đổi model không mất draft, không dựng lại transcript, không restart MCP.
- Chặn thay đổi khi **bất kỳ tab nào** đang chạy agent; kiểm tra ở controller lúc commit, không chỉ disable UI. Mở được popup để xem nhưng selection disabled và có lý do rõ.
- Reuse `get_thinking_levels()`, `has_model_thinking_levels()`, `default_thinking_level()` trong `lucnhan/core/thinking.py`. Giữ level đang có nếu hợp lệ; nếu không, dùng fallback của registry.
- Unknown model hiện dùng permissive fallback của core; UI phải ghi đây là lựa chọn chưa được xác nhận từ endpoint. Không suy capability thật từ catalog synthetic của HTML hoặc hardcode o3/gpt theo mockup.
- Chỉ disable thinking vì unsupported khi metadata authoritative cho biết không có mức điều chỉnh; không tự xem unknown là unsupported.
- Đổi selection được áp dụng cho config/global idle sessions như semantics hiện có. Không ngầm thêm cấu hình per-tab.

### Settings

- Năm mục: Provider, Appearance, Skills, MCP, Profiles bằng **horizontal subnavigation** ở cả wide/narrow theo GUI contract G7; narrow cho tab strip scroll/keyboard access. Không tạo left sidebar hoặc tự đổi sang dropdown.
- Provider: connection list, thông tin kết nối/auth hiện có, model + thinking, generation và behavior controls hiện có. Refresh explicit; hiện loading/error/empty rõ, giữ model ID tùy chỉnh.
- Appearance: Auto/IDA/Dark/Light và font controls hiện có. Không thêm hệ thống theme mới.
- Skills/MCP/Profiles: tái bố cục tab hiện có, giữ lazy creation, enable/disable/edit/apply semantics. Không bỏ trường cấu hình đang có chỉ vì mockup không minh họa.
- Save/Cancel rõ ràng, actions pinned theo GUI contract G7; Save dùng accepted/apply semantics hiện có, không thêm nút Apply thứ hai. Cancel phải phục hồi snapshot, không persist thay đổi connection hoặc theme ngoài semantics hiện có. Không làm mất config của lazy tab chưa mở.
- Giữ encrypted-key/password workflow và OAuth consent; không hiển thị credential trong connection summary.

### Tools, History, Mutations, Approval

- Tools gồm Agents, A2A, Knowledge. Reuse panel hiện có; không đưa Bulk Renamer trở lại navigation nếu hiện đang ẩn.
- Agents: running/completed/error state, cancel action, chi tiết mở theo yêu cầu.
- A2A: discovery/configuration và agent status hiện có; layout đổi nhưng không tự phát sinh thêm network discovery.
- Knowledge: dùng central memory/SQLite hiện có; không thêm storage backend hoặc eager refresh theo mỗi lần resize.
- History: search/session list/detail/loading/error/retry/delete confirmation theo logic hiện có; không auto-restore lịch sử khi startup.
- Mutations: event records đọc được, Undo explicit, không thay mutation capture/reverse operations.
- Script approval: code preview read-only, loại operation/cảnh báo, Allow/Deny thấy được ngay ở dock hẹp. Không trộn UI docs-review với approval gate; docs review vẫn theo chế độ on_error hiện có.

## 2. Evidence và giới hạn benchmark

Native prototype đã chạy trong IDA GUI với database rỗng, dùng Qt 6.8.2, PySide6 6.8.0 và Python 3.13.14 đúng interpreter của project. Hai lượt chạy; 100 mẫu cho mỗi thao tác warm/lượt. Bảng lấy giá trị lớn hơn của hai lượt, cold là một mẫu/case/lượt.

| Connections / models | Cold create + show | Warm open p95 | Search p95 | UI selection p95 | Working-set delta |
|---|---:|---:|---:|---:|---:|
| 3 / 60 | 32.3 ms | 36.9 ms | 1.5 ms | 1.5 ms | 1.7 MiB |
| 10 / 1,000 | 41.1 ms | 41.2 ms | 2.4 ms | 1.6 ms | 1.5 MiB |
| 20 / 5,000 | 90.4 ms | 32.8 ms | 3.9 ms | 1.5 ms | 6.2 MiB |

- Thời gian là synchronous operation + `QApplication.processEvents()`, không phải compositor/input-to-screen latency.
- Warm open dao động giữa lượt chạy; có outlier 202.7 ms, nguyên nhân chưa cô lập. Không tuyên bố native popup luôn đạt một frame.
- Working-set delta là số đo process, có allocator/cache noise; không phải allocation chính xác hoặc peak RAM.
- Idle đóng/mở gồm toàn process IDA; ba interval xấp xỉ 5 giây/trạng thái. Chưa thấy tăng CPU rõ, không chứng minh overhead bằng zero.
- Chưa đo full native Workspace/Settings redesign, long transcript/tool output, config persistence, provider activation hoặc SDK initialization.
- HTML Settings render p95 cao nhất 7.2 ms chỉ là số browser. HTML với 5,000 models có thinking-change p95 133.9 ms vì `refreshModelControls()` rebuild option list; không port cách đó sang native.
- Harness: `design-preview/benchmark_qt.py`, `design-preview/benchmark-launch.ps1`, `design-preview/benchmark_browser.js`. Native prototype không phải product implementation.

Chạy lại native prototype trên Windows bằng `.\design-preview\benchmark-launch.ps1 -IdaExecutable "C:\path\to\ida.exe"` từ checkout. Dùng IDA GUI và interpreter được cấu hình qua `IDAPYTHON_VENV_EXECUTABLE`; launcher tự lấy `benchmark_qt.py` cạnh nó. Lượt chạy sẽ ghi đè `benchmark-native.json` và `benchmark-native-picker.png`; giữ bản evidence gốc nếu cần so sánh.

**Performance gate đề xuất, chưa phải kết quả đã đạt của plugin:** Với 1,000 models, warm-open p95 <=50 ms, local search p95 <=10 ms, selection UI p95 <=10 ms bằng cùng phương pháp đo. Với 5,000 models, cold <=150 ms và search p95 <=15 ms. Đo cả request-to-visible và controller/persistence riêng để không giấu SDK/I/O latency. Nếu fail, profile phần vượt budget trước; chỉ chuyển sang custom model/proxy model khi row creation/filter thực sự là bottleneck.

## 3. File map và thứ tự triển khai

| Slice | Existing files chủ yếu | New files dự kiến |
|---|---|---|
| Compact Workspace + theme | `lucnhan/ui/panel_core.py`, `input_area.py`, `context_bar.py`, `theme/widgets_common.py`, `theme/tokens.py`, `theme/palette_dark.py`, `theme/palette_light.py` | Không |
| Tool rows + approval | `lucnhan/ui/tool_widgets.py`, `chat_view.py`, `message_widgets.py`, `theme/widgets_mutation.py` | Không |
| History/Mutations adaptive panes | `lucnhan/ui/panel_core.py`, `history_panel.py`, `mutation_log_view.py` | Không |
| Quick selection state | `lucnhan/ui/session_controller_base.py`, `lucnhan/core/config.py`, `lucnhan/core/thinking.py`, `lucnhan/providers/registry.py` | `tests/agent/test_model_selection.py` |
| Native picker | `lucnhan/ui/panel_core.py`, `settings_dialog.py`, `theme/widgets_common.py` | `lucnhan/ui/model_picker.py`, `tests/ui/test_model_picker.py` |
| Settings | `lucnhan/ui/settings_dialog.py`, `theme/widgets_common.py` | Không |
| Tools | `lucnhan/ui/panel_core.py`, `agent_tree.py`, `a2a_widget.py`, `knowledge_panel.py`, `theme/widgets_agent.py`, `theme/widgets_orchestra.py` | Không |

Mở lại code trước mỗi task vì line numbers có thể thay đổi. Không edit `providers/registry.py` hoặc thinking registry nếu public APIs hiện có đã đủ. Không tạo một generic UI framework, menu factory hoặc layout configuration layer.

### Task 1: Compact Workspace và theme integration

**GUI contract:** G0–G3, G9–G11; Workspace reference và palette mapping bắt buộc. Không triển khai review-page chrome bên ngoài `#stage`.

**Files:** `lucnhan/ui/panel_core.py` (`_build_input_section`, `_build_action_buttons` và workspace composition), `lucnhan/ui/input_area.py`, `lucnhan/ui/context_bar.py`, `lucnhan/ui/theme/widgets_common.py`, `lucnhan/ui/theme/palette_dark.py`, `lucnhan/ui/theme/palette_light.py`.

**Consumes:** Existing submit/cancel/new/settings/tools/history/mutation handlers và ThemeTokens.
**Produces:** Top toolbar + tabs/context/chat/composer/footer bố cục native; các action vẫn dùng handler hiện có.

- [ ] Đọc workspace builders và tất cả nơi dùng các button members trước khi chuyển vị trí. Dùng LSP references khi khả dụng cho symbol bị thay đổi.
- [ ] Chuyển secondary actions khỏi input HBox lên toolbar/overflow; giữ Send/Stop cạnh composer. Reuse button instances/handlers, không tạo hai bản action có state khác nhau.
- [ ] Dùng nested native layouts, ví dụ composition bên dưới; thứ tự widget là contract, không phải tạo parallel renderer:

```python
workspace_layout = QVBoxLayout(workspace)
workspace_layout.setContentsMargins(0, 0, 0, 0)
workspace_layout.setSpacing(0)
workspace_layout.addWidget(toolbar)
workspace_layout.addWidget(session_tabs)
workspace_layout.addWidget(context_row)
workspace_layout.addWidget(content_area, 1)
workspace_layout.addWidget(composer)
workspace_layout.addWidget(footer)
```

- [ ] Reuse theme builders, cập nhật existing Dark/Light token constants theo GUI contract G1 và geometry/type scale theo G2. AUTO/IDA vẫn lấy host palette. Không hardcode màu trong widgets, không tạo theme pipeline hoặc global QPalette thứ hai.
- [ ] Resize chỉ thay presentation/visibility; không recreate transcript hoặc session. Khi hẹp, move secondary actions vào overflow mà vẫn giữ keyboard access/tooltips.
- [ ] Smoke trong IDA GUI ở dock 460 px và 1,100 px: nhập draft, resize, đổi tab/theme, stream một turn và Stop. Chứng minh draft/caret/scroll và cancel vẫn đúng.

**Acceptance:** Composer dùng đầy đủ chiều ngang; không horizontal overflow; tất cả action cũ vẫn truy cập được; tab đang chạy không bị hủy do resize/navigation.

### Task 2: Tool activity rows và approval presentation

**GUI contract:** G0–G4, G9–G11; compact tool rows và approval action placement là invariant.

**Files:** `lucnhan/ui/tool_widgets.py` (`ToolCallWidget`, `ToolBatchWidget`, `ToolGroupWidget`, `ToolApprovalWidget`), `lucnhan/ui/chat_view.py`, `lucnhan/ui/message_widgets.py`, `lucnhan/ui/theme/widgets_mutation.py`.

**Consumes:** Existing tool events, result/status data, approval callbacks và sanitize boundaries.
**Produces:** Compact summary rows, lazy details và rõ ràng pending approval/error states.

- [ ] Đọc flow từ TurnEvent tới tool widget, grouping và approval callback. Không đổi ordering, batching hoặc result semantics.
- [ ] Tái bố cục header theo icon/status, tool name, target/argument elided, result count/duration khi dữ liệu thật có sẵn. Tooltip chứa text escaped, không credential.
- [ ] Giữ detail widgets lazy; chỉ render raw/table/code khi mở. Không parse lại markdown của tất cả tool rows mỗi TEXT_DELTA.
- [ ] Code preview read-only; dùng gate hiện có để ẩn Always Allow cho script, ví dụ:

```python
is_script = tool_name == EXECUTE_PYTHON_TOOL_NAME
always_allow_button.setVisible(not is_script)
code_editor.setReadOnly(True)
```

- [ ] Khi operation cần approval, Allow/Deny phải nhìn thấy trong viewport đầu ở dock hẹp; busy state không cho double-submit.
- [ ] Chạy existing message/approval tests liên quan và native smoke: success/error/cancel, grouped calls, expanding raw output, explicit deny script. Không chạy script binary-derived chỉ để lấy screenshot.

**Acceptance:** Có thể đọc tool timeline mà không expand tất cả; error không chỉ phân biệt bằng màu; approval gate không suy yếu, giữ docs-review on_error semantics.

### Task 3: Adaptive History và Mutations panes

**GUI contract:** G0, G2, G3, G5, G9–G11; pane chỉ phủ content region ở narrow, không thành modal.

**Files:** `lucnhan/ui/panel_core.py` (`_build_main_splitter`, `_show_right_panel`), `lucnhan/ui/history_panel.py`, `lucnhan/ui/mutation_log_view.py`.

**Consumes:** Existing right-panel coordinator, history executor/queue/generation và mutation records.
**Produces:** Mutually exclusive side pane ở wide dock; content overlay/back-close presentation ở narrow dock.

- [ ] Giữ `_show_right_panel(name: Literal['history', 'mutation'] | None)` làm owner duy nhất của pane state; button check state và Close cùng đi qua coordinator này.
- [ ] Dùng breakpoint cố định theo GUI contract G3: panel width <=640 logical px là narrow, >640 là wide; không dùng toàn bộ screen width hoặc resize-driven timers.
- [ ] Reuse cùng History/Mutations widget khi đổi wide/narrow. Overlay nằm trong content area, không thành top-level modal che IDA; composer và Close vẫn truy cập được.
- [ ] Preserve history executor ownership, generation checks, bounded queue, loading/error/retry/delete-confirmation và không auto-restore. Không chuyển history sang `_SAVE_EXECUTOR`.
- [ ] Smoke: mở History -> search -> mở session; đổi sang Mutations -> undo một mutation có reverse record trên database thử nghiệm riêng; đổi dock width khi pane mở. Quan sát stale results không ghi vào session mới.
- [ ] Chạy `tests/ui/test_history_panel.py`, `test_history_open_click.py`, `test_startup_history_restore.py` và các history/restore regressions liên quan.

**Acceptance:** Hai pane không đồng thời active; state không mất khi resize; history không eager load lúc startup; undo vẫn explicit và đúng DB/session.

### Task 4: Controller path riêng cho model/thinking selection

**GUI contract:** G0, G6, G9, G11; giữ visual pending/busy/error state và selection cũ khi persistence fail. Task này không tự tạo UI/theme.

**Files:** `lucnhan/ui/session_controller_base.py`, `lucnhan/core/config.py`; reuse `lucnhan/core/thinking.py`, `lucnhan/providers/registry.py`, `lucnhan/core/atomic_io.py`. New test: `tests/agent/test_model_selection.py`; extend `tests/core/test_config.py` nếu sửa save boundary.

**Proposed API:** `SessionControllerBase.select_model(connection_name: str, model_id: str, thinking_level: str) -> None`. `ValueError` cho input/connection/level không hợp lệ; `RuntimeError` khi có agent đang chạy; persistence errors propagate để UI báo lỗi và giữ selection cũ.

**Consumes:** `LucNhanConfig.switch_provider()`, per-connection saved config, thinking registry, `is_agent_running`, cached provider registry.
**Produces:** Một selection committed, config/session metadata đồng bộ; không restart MCP, reload skills, reset tools hoặc dựng SDK lúc chọn.

- [ ] Viết regression trước cho busy guard ở một tab khác, invalid connection/model input, giữ credentials/extra độc lập qua connection switches, thinking normalization và save failure. Dùng các fixture/controller harness hiện có; không khởi tạo runtime/network chỉ để test config logic.
- [ ] Kiểm tra busy tại thời điểm commit. Validate connection tồn tại và model ID non-empty; custom/manual ID hợp lệ không bắt buộc phải nằm trong fetched catalog. Chỉ chọn level thuộc registry đang áp dụng cho model.
- [ ] Stage provider/config changes trên snapshot, normalize thinking theo schema hiện có, preserve các extra fields ngoài thinking:

```python
extra = candidate.provider.extra
previous = extra.get("thinking")
preserve = previous.get("preserve", True) if isinstance(previous, dict) else True
extra["thinking"] = {
    "enabled": thinking_level != "none",
    "reasoning_effort": thinking_level,
    "preserve": preserve if isinstance(preserve, bool) else True,
}
```

- [ ] Persist bằng existing config/encryption serializer; encrypted config có blob phải giữ nguyên blob khi save không password. Không tự decrypt, đổi credentials hoặc downgrade encryption để lưu model.
- [ ] Trước khi thêm đường save thường xuyên, bảo vệ file cũ khỏi partial write: giữ serialization hiện có, ghi temp cùng directory rồi dùng existing `atomic_replace()`; không thêm retry policy riêng. Failure phải giữ file trước đó và config/UI selection trước đó, xóa temp do operation tạo. Test interrupted write/replace failure và encrypted passwordless save. Không refactor storage khác.
- [ ] Commit về **cùng config object** mà panel/Settings đang giữ; không swap sang object mới khiến owner khác stale. Cập nhật metadata của idle sessions tương ứng semantics global hiện có. Nếu busy state đổi trước commit, abort.
- [ ] Không gọi `update_settings()` hoặc `reload_mcp()` từ API này. Model-only change có thể reuse `ProviderRegistry.get_or_create()` khi provider được dùng ở turn kế tiếp; thinking extra changes vẫn phải đi qua cache invalidation hiện có, không sửa cached provider internals bằng tay.
- [ ] Cold SDK initialization chỉ diễn ra ở existing safe initialization/submission path. Đo config save/controller latency riêng; nếu file I/O vượt budget, chỉ chuyển pure persistence sang worker queue sau khi có profile, không chuyển Qt/SDK/IDA initialization sang worker.

**Behavior check tối thiểu:** Hai connection có cùng model ID phải giữ endpoint/credential identity khác nhau; agent ở tab B làm selection ở tab A bị từ chối; write failure không mất file cũ hoặc đổi footer.

**Acceptance:** Model/thinking switch không thay credentials, extra unrelated, transcript, draft hoặc MCP processes; Settings mở sau đó thấy cùng lựa chọn; restart đọc lại selection persisted an toàn.

### Task 5: Footer popup native có grouped search

**GUI contract:** G0–G3, G6, G9–G11; grouped popup extension trong G6 thắng single-provider combo trong PNG.

**Files:** Create `lucnhan/ui/model_picker.py`, `tests/ui/test_model_picker.py`; modify `lucnhan/ui/panel_core.py`, `lucnhan/ui/settings_dialog.py`, `lucnhan/ui/theme/widgets_common.py`.

**Proposed interfaces trong `model_picker.py`:**

```python
@dataclass(frozen=True)
class ModelChoice:
    connection_name: str
    model_id: str
    thinking_level: str

# ModelPicker is a QFrame, not a WebView.
# __init__(parent: QWidget, on_select: Callable[[ModelChoice], None]) -> None
# set_catalog(models_by_connection: dict[str, list[ModelInfo]]) -> None
# set_selection(choice: ModelChoice) -> None
# set_busy(busy: bool) -> None
# open_above(anchor: QWidget) -> None
```

**Consumes:** Task 4 controller API, `ModelInfo` và existing model discovery results.
**Produces:** Clickable footer, cached popup và local filter; Settings/picker cùng config source.

- [ ] Test duplicate IDs across connections, filter connection/model, zero results, invalid thinking fallback và selection bị chặn khi busy. Native geometry/focus không được coi là verified nếu suite đang dùng Qt stubs hoặc skipped real-Qt markers.
- [ ] Tạo QFrame popup, QLineEdit search, QTreeWidget grouped rows với uniform row heights và QComboBox thinking như benchmark candidate. Store `(connection_name, model_id)` trong item data, không parse display text để xác định provider.
- [ ] Cache normalized search text khi set catalog; filter không allocate lại tất cả rows. Reuse existing Qt widgets, không thêm custom proxy-model framework khi benchmark chưa cần.
- [ ] Từ Settings Refresh, lưu model catalog thành per-connection cache do panel/controller sở hữu. Invalidate connection tương ứng khi endpoint/credential/config connection đổi, không vì thinking hoặc theme đổi. Cache không chứa raw credential trong row metadata.
- [ ] Không có fetched list thì tạo saved/current-model row từ config thật và cung cấp Settings entry point; dùng existing explicit refresh/manual-model controls để hoàn thiện catalog.
- [ ] Anchor popup trong available screen bounds, không overflow dock; Escape/outside click đóng, restore focus, keyboard chọn được. Busy trạng thái phải cập nhật theo existing turn lifecycle, không thêm polling timer chạy vĩnh viễn cho picker.
- [ ] Chỉ update footer, context metadata và relevant controls sau controller commit thành công. Failure hiện message không chứa secret và giữ lựa chọn trước đó. Không `show()` lại toàn workspace để cập nhật footer.
- [ ] Native smoke với 3/60, 10/1,000, 20/5,000 synthetic catalog; duplicate model IDs, dark/light/IDA, DPI 100%/150%, wide/narrow, draft/caret retention và active agent ở tab khác.

**Acceptance:** Popup ngoài góc trái dưới đúng reference; selection dùng connection identity; model/thinking dùng cùng core policy với Settings; popup idle không có thêm recurring work ngoài native focus/cursor behavior.

### Task 6: Settings và Tools layout cutover

**GUI contract:** G0–G2, G7–G11; Settings/Tools đều horizontal subnavigation, selected-record detail ở dưới table.

**Files:** `lucnhan/ui/settings_dialog.py`, `lucnhan/ui/panel_core.py`, `lucnhan/ui/agent_tree.py`, `lucnhan/ui/a2a_widget.py`, `lucnhan/ui/knowledge_panel.py`, existing theme builders. Existing tests: `tests/ui/test_settings_dialog_fixes.py`, `test_a2a_widget.py`, `test_knowledge_rebind.py`, `test_knowledge_panel_sqlite_read.py`.

**Consumes:** Existing Settings groups/lazy tabs/rollback; Task 5 model catalog and config sync; existing Tools dock/panel ownership.
**Produces:** Năm Settings pages và ba Tools pages theo reference, giữ toàn bộ functionality hiện có.

- [ ] Tái dùng SettingsDialog/groups/lazy tab builders; giữ existing modality và accept/reject semantics theo native-wrapper exception ở GUI contract G7. Tạo horizontal subnavigation/content/pinned Cancel–Save footer; không duplicate global workspace nav trong dialog, config bindings hoặc bỏ generation/behavior/GLM/auth fields. Không thêm left sidebar hoặc tự chuyển Settings sang embedded page.
- [ ] Provider chọn connection rõ ràng, model/thinking đồng bộ với footer. Empty/error/loading có action explicit, custom model ID không bị ghi đè bởi fallback khi catalog tới muộn.
- [ ] Giữ delayed heavy initialization 200 ms và queue fetch polling hiện có. Mở/resize dialog không fetch lại models hoặc rebuild toàn bộ tab contents.
- [ ] Cancel giữ snapshot rollback và dừng/disconnect timers/theme subscriptions; stale fetch results không mutate closed dialog hoặc connection khác.
- [ ] Appearance reuse ThemeManager; Skills/MCP/Profiles mở theo nhu cầu, Save khi chưa mở các tab này không ghi đè config của chúng.
- [ ] Tools chuyển layout/navigation Agents/A2A/Knowledge, reuse hiện có lazy dock creation và widget lifecycle. Không thay command execution, discovery policy hoặc central memory storage.
- [ ] Smoke Save/Cancel, encrypted settings bằng credential giả trong isolated temp config, unopened lazy tabs, custom connection với same model ID, Knowledge đổi IDB, A2A loading/error, cancel agent. Network/API smoke chỉ khi được user authorize riêng; không cần network để xác minh layout.

**Acceptance:** Đủ cả năm Settings và ba Tools surfaces; Cancel/Save đúng, top subnavigation và action/detail placement theo GUI contract; không eager filesystem/SQLite/network work do navigation/resize; settings-only changes không phá approval hoặc active agent guard.

### Task 7: Integration verification và release documentation

**GUI contract:** Toàn bộ G0–G11. Integration owner review native evidence theo G11; không dùng HTML screenshots thay native proof.

**Files:** Các files sửa ở Task 1–6; `CHANGELOG.md`, `DEVELOPMENT.md`; lưu native screenshots cho visual review. Không thay ảnh reference hoặc GUI contract để hợp thức hóa implementation bị lệch nếu chưa có design-change approval. Không bump version khi chưa có release request.

- [ ] Run relevant behavior tests sau mỗi slice bằng configured interpreter; giữ regression thực sự cho duplicate identity, busy guard, persistence/encryption failure và cancel/lazy-tab behavior. Không thêm assertions chỉ kiểm tra source text, exact wording hoặc mock forwarding. Delete incidental wording/implementation tests bị obsolete thay vì repin theo layout mới.
- [ ] Với symbols exported thay đổi, kiểm tra references và migrate mọi caller; không để compatibility aliases hoặc duplicate old navigation code.
- [ ] Native visual review từng surface theo GUI contract G10/G11: capture theme/font/logical geometry/DPI, đối chiếu composition/palette/density/action placement. Ghi intentional native differences; thay đổi visual direction phải được người dùng chấp thuận, không chỉ agent tự ghi “deviation”.
- [ ] Smoke full native plugin trong IDA GUI: 12 reference views, wide/narrow, Dark/Light/IDA/Auto, DPI 100%/150%. Thực hiện enter/send/stop, search/filter, keyboard focus, pane close/resize, apply/cancel và script deny/allow trong môi trường test được phép. Không dùng API/tool auto-approval cho smoke.
- [ ] Dùng transcript/tool output lớn theo restore caps hiện có; quan sát typing/streaming, scroll anchoring, collapse/expand và switching tabs. Không tắt compaction/truncation để đạt screenshot.
- [ ] Re-run benchmark trên **widget đã tích hợp**, không dùng prototype thay bằng chứng. Warm up, 100 samples/operation, ít nhất hai fresh GUI runs; record CPU/RAM/context, median/p95/max. Đo full Workspace/Settings first paint, picker UI, config persistence và provider initialization riêng. Idle dùng paired closed/open; báo variance/outlier, không suy GPU/compositor latency từ processEvents timing.
- [ ] So sánh trước/sau trên cùng machine/runtime và workload. Gate regression của existing workspace/streaming và Settings bằng paired measurements; nếu chậm do SDK/I/O thì không loại sample khỏi end-to-end measurement để làm đẹp kết quả.
- [ ] Update `CHANGELOG.md` với UI cutover và quick picker; `DEVELOPMENT.md` với native smoke/benchmark procedure và giới hạn. Giữ benchmark artifacts để audit nhưng xóa temporary smoke scripts không còn cần; không đóng gói mockup/harness như production plugin UI.
- [ ] Chạy documented format/lint/mypy/pytest bằng project interpreter. Nếu real Qt thiếu ở standalone environment, báo test skips rõ và dùng IDA GUI smoke; không bypass vendor GUI prerequisite, không cài Qt khác vào IDAPython venv chỉ để suite xanh.

**Commands khi thực thi, từ repo root trong PowerShell:**

```powershell
$py = $env:IDAPYTHON_VENV_EXECUTABLE
if (-not $py) { throw 'IDAPYTHON_VENV_EXECUTABLE is required' }
& $py -m pytest tests/core/test_config.py tests/core/test_thinking.py tests/agent/test_session_controller.py tests/agent/test_model_selection.py -v
& $py -m pytest tests/ui/ -v
& $py -m ruff format --check lucnhan/ tests/
& $py -m ruff check lucnhan/ tests/
& $py -m mypy lucnhan/core lucnhan/providers
& $py -m pytest tests/ -v
```

Các test files mới chỉ tồn tại sau task tương ứng. Tool thiếu phải provision theo project workflow và interpreter constraint; không chạy global Python. `ci-local.sh` chỉ dùng khi đã xác minh script chọn đúng configured interpreter; không để script tự tạo environment khác cho IDAPython task.

**Acceptance:** Có native visual/runtime evidence, targeted behavior checks và integrated performance measurements; tất cả lỗi/skips/khía cạnh chưa kiểm chứng được báo chính xác. Không kết luận full redesign “không ảnh hưởng hiệu năng” chỉ từ prototype.

## 4. Definition of Done

- [ ] Toàn bộ 12 surfaces trong reference có counterpart native: Workspace, History, Mutations, Approval, Provider, Appearance, Skills, MCP, Profiles, Knowledge, Agents, A2A.
- [ ] Footer picker hoạt động end-to-end với nhiều configured connections, duplicate IDs, thinking policy, busy guard, safe persistence và Settings sync.
- [ ] Composer/streaming/session/history/mutation/approval behavior không bị mất hoặc bypass.
- [ ] Native surfaces đọc được ở wide/narrow, có keyboard/focus/accessibility, không horizontal overflow hoặc hidden approval controls.
- [ ] Mọi surface đạt GUI Design Contract G0–G11, có native screenshot evidence; agent không tự đổi style/navigation/geometry hoặc sửa reference để che design drift.
- [ ] Không dependency UI mới, WebView, global stylesheet, persistent idle polling mới hoặc eager API fetch từ picker.
- [ ] Tests, native smoke và benchmark integrated có output thật; docs/callsites cập nhật; mọi giới hạn được ghi rõ.
- [ ] Chỉ commit/push khi người dùng yêu cầu riêng. Chốt plan này không phải authorization thực thi hoặc release.
