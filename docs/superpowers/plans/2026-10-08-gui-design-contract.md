# Luc Nhan GUI Design Contract

**Status:** Normative handoff cho native UI/UX redesign đã chốt. Agent triển khai không tự chọn một visual direction khác.
**Implementation plan:** [Native UI/UX redesign](2026-10-08-native-ui-ux-redesign.md).
**Reference:** [Interactive mockup](../../../design-preview/index.html), các ảnh ở G10. Đây là reference GUI, không phải app để embed vào IDA.
**Scope:** Workspace, Settings, Tools, auxiliary panes, approval và footer picker. Không tạo brand identity, marketing page hoặc UI framework.

## G0. Authority và quy tắc chống design drift

1. Safety/threading/credential constraints trong repo và plan luôn thắng bất kỳ hình minh họa nào.
2. Tài liệu này là source of truth cho GUI: màu theo theme mode, geometry, hierarchy, states và responsive behavior. Plan là source of truth cho behavior/controller/persistence.
3. PNG + HTML dùng để đối chiếu composition. Nếu PNG khác phần extension được ghi rõ dưới đây, extension trong contract thắng. Không suy business logic/capability từ sample data.
4. Chỉ phần plugin nằm trong `#stage` là reference. Không triển khai review sidebar 12 màn hình, review toolbar, Dock/Theme preview selectors, footer-note, viền ngoài 7 px hoặc fixed app height 710 px của preview.
5. Không thay top subnavigation thành left sidebar, compact rows thành oversized cards, footer popup thành modal, hoặc layout phẳng thành dashboard/bento “đẹp hơn”. Những thay đổi này cần người dùng chấp thuận, không phải quyền tự quyết của slice agent.
6. Native Dark/Light palette đã áp dụng mapping ở G1 qua palette pipeline hiện có. Giữ mapping này khi sửa giao diện; không hardcode màu riêng trong từng widget.
7. Hai extension được khóa bằng text vì PNG chưa minh họa đầy đủ: picker nhiều connection dùng grouped list + search (G6), narrow dock vẫn cho truy cập mọi session tab (G3). Không copy single-provider combo/hide-tabs của preview như behavior production.
8. Qt native có thể khác browser ở font metrics, scrollbar và control chrome; không được dùng lý do đó để thay hierarchy, thứ tự vùng, vị trí action hoặc tăng mật độ whitespace tùy ý.

## G1. Visual language và color roles

**Direction:** Compact technical workspace. Flat surfaces, neutral charcoal/off-white, một accent mint cho navigation/focus/primary action. Thông tin binary/code rõ; chrome nhẹ; không chiếm không gian làm việc bằng trang trí.

- Không gradient, glow/neon, glass/blur, texture/noise, hero typography, serif headings, avatar decoration hoặc emoji làm icon.
- Sections dùng whitespace nhỏ + divider, không bao mỗi section trong một card có shadow. Approval và selected-record detail được phép có border vì có ý nghĩa.
- Buttons/input radius 4 logical px; compact badge/inline code 3 px; popup 5 px; borders/dividers 1 px. Không capsule/pill buttons hoặc corners 12–24 px.
- Không thêm animation, custom fade/slide/scale hoặc recurring animation timer. Giữ native feedback, đổi hover/pressed/focus state trực tiếp.
- Icon dùng assets/QIcon/native primitives đang có, một style nhất quán, kích thước danh nghĩa 14–16 px. Có accessible name/tooltips. Không tải icon/font từ mạng hoặc thêm library.

**Target palette qua `ThemeTokens` hiện có:**

| Token(s) | Dark | Light | Vai trò |
|---|---|---|---|
| `window` | `#202427` | `#ffffff` | Main plugin surface |
| `window_text`, `text`, `button_text`, `code_text` | `#e0e5e7` | `#252e2a` | Nội dung đọc được |
| `base`, `code_bg`, `dark` | `#1b1f21` | `#edf1ee` | Fields, code/details inset |
| `alt_base`, `button`, `light` | `#282d30` | `#eef1ef` | Hover/raised control surface |
| `mid` | `#363d41` | `#d4dcd7` | Border/divider |
| `muted_text` | `#a0abb0` | `#596a61` | Caption, count, hint |
| `accent`, `highlight` | `#86c7b8` | `#326d5b` | Focus/navigation/primary fill |
| `highlight_text` | `#142621` | `#ffffff` | Text trên primary fill |
| `selection` | `#293e38` | `#e0ece5` | Active nav/selected row fill |
| `success` | `#9bc7a7` | `#356c43` | Completed/verified |
| `warning` | `#dbbd82` | `#846020` | Pending approval/unverified |
| `error` | `#e5a0a0` | `#a23838` | Failure/destructive affordance |

Dùng `highlight_text` cho filled primary buttons, không dùng `button_text` mặc định trên accent. Active nav dùng `selection` + `accent`, không tô toàn section bằng accent. Status màu luôn đi kèm text/icon, không chỉ một dot.

**AUTO/IDA:** Tiếp tục follow host QPalette theo ThemeManager hiện có; không ép mint/RGB của Dark/Light lên IDA native mode. Geometry và hierarchy vẫn như contract. Không sửa `palette_ida.py` để biến native mode thành forced theme, không thay QPalette toàn QApplication.

## G2. Typography, units và density

Mọi kích thước là **Qt logical px**, không phải physical PNG pixels. Baseline reference lấy từ CSS của mockup ở font mặc định; DPI scaling do Qt xử lý, không nhân thêm với devicePixelRatio.

Font UI dùng font pipeline hiện có: inherit host và tôn trọng font override trong Settings. Windows reference dùng Segoe UI; monospace dùng font code đang có, ưu tiên Consolas nếu khả dụng. Không ép font download hoặc font family khác để “premium” hơn.

| Role | Baseline reference size | Weight/use |
|---|---:|---|
| Chat/body/button text | 13 px | Regular; body line-height khoảng 1.5 |
| Page heading (Settings) | 15 px | Semibold |
| Tool name, section title, nav, table cells | 12 px | Tool/section semibold; cells regular |
| Code blocks/addresses/model IDs | 12 px | Monospace, line-height khoảng 1.55 |
| Tool argument, context row, secondary hints | 11 px | Regular; tool argument monospace |
| Footer/count/table headings | 10–11 px | Muted, không thay bằng màu gần background |

Ở user font override lớn: giữ role hierarchy, để controls/rows tăng height theo font metrics; không clip chữ để giữ baseline fixed height. Chỉ dùng uppercase cho small metadata label như CONTEXT, không uppercase toàn tool timeline.

| Component | Baseline geometry | Quy tắc |
|---|---|---|
| Main header | Min-height 38 px; padding 3 px vertical / 10 px horizontal | Status trái, navigation phải |
| Session strip | Min-height 31 px; inset 9 px; gap 7 px | Active underline 2 px |
| Context row | Min-height 27 px; padding 4 / 12 px; gap 9 px | Elide metadata trước primary context |
| Chat body | Padding 14 top / 15 horizontal / 12 bottom px | Scroll riêng, flex-grow |
| Tool summary | Min-height 30 px; padding 5 / 6 px; gap 8 px | Một hàng, không card chrome |
| Button / input | Button min-height 28 px; input baseline 30 px | Padding button 4 / 9; input 5 / 8 px |
| Composer wrapper | Padding 7 top / 10 horizontal / 5 bottom px | Border-top, không row action dài bên cạnh |
| Composer text / action row | Text baseline 44 px, min 38 px; action controls min 25 px | Text trên, commands/hint/send-stop dưới |
| Footer | Min-height 25 px; padding 2 / 11 px; gap 8 px | Pinned, không scroll cùng chat |
| Wide auxiliary pane | Width danh nghĩa 295 px | Có title + Close, scroll riêng |
| Settings body | Padding 12 vertical / 18 horizontal px | Body scroll, actions pinned |
| Settings form | Label column 138 px; row gap 7 px; column gap 12 px | Field column stretch |
| List/table rows | List padding 7 / 3 px; table cell padding 6 / 8 px | Divider ngang, row height grow theo nội dung/font |

Spacing lấy từ tập compact 2/4/6/8/10/12/16 px; giữ các số lẻ trong bảng khi cần khớp reference. Không tự thêm padding 24–40 px vào mỗi vùng.

## G3. Workspace composition và responsive behavior

Thứ tự top-to-bottom phải giữ: **header -> session strip -> context -> chat/content -> composer -> footer**. Main composition layout không có outer margins/gap; padding do từng region ở G2 quản lý, tránh cộng thêm một lớp gutter. Chat/content là vùng nhận chiều cao còn lại. Không lấy 710 px của preview làm fixed-height dock.

- Header trái: existing mark nếu có, `Luc Nhan`, trạng thái thật. Phải: `Chat`, `Tools`, `History`, `Settings`. New chat dùng `+` trên session strip; Export và action ít dùng ở overflow. Không thêm thanh New/Export/Settings thứ hai cạnh input.
- Session: title elided, close affordance, active underline; counters cuối strip là secondary. Không tạo fake tab/title/count lấy từ PNG.
- Context: label -> symbol -> address -> optional binary metadata -> `Changes` action/count. Changes mở Mutations, không phải một setting mới.
- User message: sender/time nhỏ, bubble phẳng nhẹ với left rule; assistant: sender/time, nội dung full-width, tool rows. Không giant avatar, center alignment hoặc bubble max-width nhỏ tùy ý.
- Composer: một border radius 4 px quanh input + action row. Commands trái, existing keyboard hint giữa khi đủ chỗ, Send/Stop phải. Trong approval input disabled nhưng Stop/cancel vẫn có đường rõ ràng.
- Footer: model/thinking trái; context tokens/usage phải; binary metadata cuối nếu đủ chỗ. Meter 49 x 3 px là reference, không thành chart lớn.

**Breakpoint duy nhất cho chrome compact:** `panel_width <= 640` logical px là narrow. Width là của plugin/dock, không phải desktop screen hoặc browser viewport.

Ở narrow: header gap 5 px; `Chat`, `Tools` vẫn direct; `History`, `Settings`, `Export` vào overflow có accessible labels. Header state có thể rút gọn nhưng tooltip/text status vẫn lấy được. Context binary metadata và secondary counters/hints có thể ẩn, không ẩn symbol/address hoặc critical actions.

Chat padding chuyển thành 12 px. Tool arguments elide sớm, status vẫn thấy. Session tabs dùng native tab scrolling/overflow để mọi tab và New vẫn truy cập được; không hide toàn bộ inactive tabs như preview. Không hủy runner khi chọn tab hoặc resize.

Test widths: 460 và 1,100 logical px. Tại 640/641 transition không mất draft/focus, không duplicate actions hoặc phát sinh horizontal scrollbar toàn workspace.

## G4. Tool rows và script approval

Summary row: disclosure chevron -> status/icon + **tool name** -> target/argument monospace elided -> result count/status phải. Tooltip giữ argument đầy đủ đã sanitize/escape. Chỉ hiển thị duration/count thật, không random/sample values.

Expanded output: inset code surface, padding 7 top / 10 right-bottom / 22 left px; dùng table/code/text formatter đang có. Raw/Copy actions phía dưới phải. Chỉ build chi tiết khi cần; không pre-expand mọi tool call.

Approval là exception có border warning 1 px, radius 4 px; header gồm operation thực + `Approval required` + state `Paused`; tiếp theo description/target, read-only code, explanation, action strip cuối. **Deny trái, Allow once phải** trong strip canh phải. No Always Allow cho script.

Nếu code dài hoặc dock thấp, chỉ vùng code/details scroll; header và Deny/Allow once giữ visible. Không giảm code font hoặc đẩy decision buttons dưới fold để khớp ảnh. Giữ full script, không truncate phần cần người dùng review.

Không lấy imports/direct execution trong sample code screenshot làm API instruction; dùng repo script guard/approval chính thức. Docs-review UI là trạng thái khác, không được thay approval gate.

## G5. History và Mutations panes

Wide: chat/content trái, một auxiliary pane phải, nominal width ở G2. History và Mutations mutually exclusive qua coordinator hiện có.

Narrow: cùng widget pane phủ **content region**, không phủ header/composer/footer và không thành top-level modal. Pane header gồm tên, count/scope nếu có, Close/Back; body scroll riêng. Close trả focus về action đã mở pane, chat draft/caret không đổi.

History order: header -> search/filter -> session rows với title/date/model metadata -> loading/error/retry hoặc detail. Mutations order: header -> records với operation/target/time/status -> Undo explicit cho record/group được chọn theo existing logic. Không biến Undo thành icon khó đoán hoặc action mặc định khi click row.

## G6. Footer picker nhiều connection — normative extension

Ảnh `workspace-model-picker.png` chỉ có single-provider combo. Production phải dùng **search + grouped list**, không giữ combo phẳng khi có nhiều connections. `benchmark-native-picker.png` chỉ là prototype đo, không phải reference màu/typography.

- Trigger text: `Connection / Model` + divider + thinking level/off + chevron; trigger max 68% footer width. Elide tên/model trước token summary; tooltip có identity đầy đủ và busy reason nếu có.
- Popup anchor: trên trigger, left-aligned, gap 8 px; clamp trong available screen và dock bounds. Không centered dialog hoặc separate window chrome.
- Nominal width 360 px (extension từ prototype grouped); actual width tối đa `min(360, dock_width - 24, available_screen_width - 24)`. Padding 12 px, radius 5 px, border 1 px, surface `window`.
- Max height 420 px hoặc screen space đang có nếu thấp hơn; list là vùng co/scroll trước, không đẩy thinking/status ra ngoài. Các mức này là target GUI mới, không ngụ ý benchmark cũ đã đo đúng mọi row height.
- Order: `Model & thinking` title -> search -> grouped model list -> `Thinking level` label/control -> support/busy/status hint. Không thêm token slider, capability dashboard hoặc provider setup form vào popup.
- Connection header 24 px baseline, semibold; model row 28 px baseline, active checkmark + model ID. Optional model name dùng metadata thật, không thêm secondary empty line cho mọi row.
- Group label là configured connection name; provider family là metadata phụ nếu đã biết. Hai connection có cùng model ID vẫn là hai rows độc lập.
- Catalog giữ được saved/manual model. Chưa fetch: chỉ model thật đã lưu + `Open Settings to refresh models`; không spinner vô hạn hoặc fake cloud models.
- Search local, không call API. No results: `No matching models`, giữ query và cho clear; không tự switch selection.
- Open focus vào search; arrows di chuyển selection, Enter commit model, Tab tới thinking, Escape đóng trả focus. Click model/đổi thinking chỉ commit khi controller cho phép; popup giữ mở để chọn tiếp, outside click đóng.
- Busy: current selection vẫn đọc được; search có thể dùng để xem, selection/thinking disabled; hint `Stop the running agent to change model`. Guard controller vẫn bắt race ở tab khác.
- Unsupported/unknown thinking theo policy trong plan; unknown hint `Thinking levels are not confirmed for this model`, không lấy `Off · not supported` synthetic làm truth.
- Không rebuild catalog khi đổi thinking. Current draft/transcript/scroll giữ nguyên.

## G7. Settings composition — top subnavigation, không left sidebar

Order: main header -> `Settings` heading/Back to chat action -> **horizontal subnavigation** `Provider | Appearance | Skills | MCP | Profiles` -> scrollable body -> pinned Cancel/Save strip.

**Native wrapper exception được khóa:** Giữ `SettingsDialog` và modality/accept/reject semantics hiện có. PNG trình bày Settings trong common preview shell; không duplicate Chat/Tools/History navigation bên trong blocking modal và không tự chuyển Settings thành embedded workspace page. Dialog content bắt đầu ở Settings heading -> subnavigation -> body -> Cancel/Save; native title bar/Close được phép khác PNG. Nếu có `Back to chat`, nó đóng theo Cancel/reject, không ngầm Save. Muốn đổi modality/routing phải xin design-change approval.

Subnavigation padding 5 / 10 px, gap 3 px; active item dùng selection fill + accent text; item min-height 29 px. Narrow vẫn là horizontal scrollable tab strip có current item visible và keyboard access. Không thay bằng left sidebar hoặc dropdown chỉ vì wide dock nhiều khoảng trống.

Body sections và field order:

| Page | Nội dung chính theo thứ tự | Presentation |
|---|---|---|
| Provider | Connection/provider + add/remove -> auth/key/base -> model/manual/Refresh -> Generation (temperature/thinking, output/context) -> Behavior -> Advanced/GLM theo điều kiện hiện có | Form compact; advanced disclosure, không giant connection cards |
| Appearance | Theme -> font family/size -> sample text/code | Inline reading sample, không thêm branded theme gallery |
| Skills | Sources/filter nếu có -> enable/disable rows -> nguồn/config note | Name + description ngang ở wide; description xuống hàng ở narrow |
| MCP | Configuration/source controls -> server rows/status -> permissions/notes hiện có | Explicit actions, không mô phỏng auto-connected trạng thái |
| Profiles | Active profile -> existing profile fields -> redaction/denied-tool controls | Không hide safety controls sau tab chưa được đặt tên |

Generation: hai field columns bằng nhau với gap 12 px; narrow vẫn hai cột gap 8 px nếu text/control fit, nếu user font lớn thì stack theo row order, không clip. Form labels 108 px ở narrow, column gap 8 px; body padding 12 px. Behavior checkboxes hai cột wide, một cột narrow. Section separators 1 px, khoảng cách section 13 px.

Footer padding 8 / 14 px; dirty/status hint trái; **Cancel rồi Save** canh phải. Save mapped về accepted/apply semantics hiện có, không thêm một nút Apply thứ hai. Button width tối thiểu 68 px wide / 55 px narrow, phải expand khi translated/user font cần. Save/Cancel không scroll cùng body.

Credential input masked, reveal chỉ nếu existing policy cho phép; không hiển thị plaintext key/base credentials trong group summaries. Không copy `Preview only`, `Local design draft`, sample endpoints/capabilities vào product.

## G8. Tools composition

Order: main header với Tools active -> horizontal `Agents | A2A | Knowledge` -> page toolbar/body -> common footer. Không thêm persistent left sidebar. Reuse existing dock wrapper nếu host cần; không tạo song song embedded Tools và một independent stateful Tools renderer.

| Page | Vùng trên | Vùng nội dung | Vùng chi tiết |
|---|---|---|---|
| Agents | Existing cancel/clean/filter controls; running/completed count phải | Dense table/tree: name/type/status/turns/time hoặc hierarchy đang có | Selected agent output preview ở dưới |
| A2A | Available agents/transport/availability + explicit Refresh | Delegate task: target, textarea, context option theo policy, Send Task phải | Task history ở dưới |
| Knowledge | Search stretch -> type filter -> Refresh -> retrieved-in-chat option | Count/scope line + record table | Selected record detail ở dưới, không mặc định thêm cột sidebar |

Table: readable row separators, header inset background, selection fill; primary name/title left, numeric fields align nhất quán. Giữ hierarchy thật của AgentTree nếu đang có, không flatten để bắt chước fake sample rows. Knowledge details và agent output có một thin border, không nested cards/shadows.

Narrow: toolbar wrap theo thứ tự action, giữ Search/Refresh reachable; cho table region horizontal scroll khi thật sự cần để giữ dữ liệu, **không** gây horizontal scroll toàn app. Detail ở dưới table. Context-sharing option của A2A tiếp tục disabled nếu integration hiện tại không hỗ trợ; UI redesign không tự cấp quyền chia sẻ binary context.

## G9. Shared visual/interaction states

| State | Visual rule | Behavior rule |
|---|---|---|
| Default | Neutral surface/text; divider nhẹ | Không action tự chạy do paint/navigation |
| Hover | Raised/alt-base background cho neutral controls | Không scale, shift hoặc animate geometry |
| Active/selected | Selection background + accent/text/checkmark | Không chỉ thay màu dot |
| Keyboard focus | Accent focus outline 2 px, radius theo control | Tab order theo vị trí đọc; không mất focus khi repaint |
| Disabled/busy | Muted label + explicit reason; border/layout giữ nguyên | Disabled action không có side effect; không dim toàn pane |
| Loading | Inline status/count trong đúng region | Giữ controls/context ổn định; không skeleton animation mới |
| Empty | Short heading + một câu giải thích + action thật nếu có | Không fake data hoặc tự gọi network để lấp chỗ trống |
| Error | Error text/icon cạnh operation + retry/correction phù hợp | Không dùng toast duy nhất cho persistent error, không giấu traceback bằng UI giả |
| Approval | Warning border/status + visible Allow/Deny | Chặn input/action xung đột, không auto-approve |
| Save pending/failure | Footer/field status; selection cũ khi failure | Không báo Saved trước persistence success, không mất credentials/draft |

Body text target contrast >=4.5:1; focus và essential selection/control indicators target >=3:1. Decorative dividers/borders trong palette không phải essential state indicator; không dùng chúng làm dấu hiệu duy nhất nhận diện action/focus. Dùng native accessibility names cho icon-only controls. Read-only code không bị render như disabled text. Long names có elide/tooltip; errors/hints quan trọng wrap thay vì biến mất.

## G10. Reference manifest

Ảnh hiện có là wide dark baseline, có browser/device scaling; **không lấy số physical pixels của PNG làm Qt dimensions**. Dùng G2/G3/G6/G7 cho narrow/DPI/extension. Không giả định đã có native screenshots cho toàn redesign.

| Surface | Reference trong repo | Contract áp dụng |
|---|---|---|
| Workspace | [workspace.png](../../../design-preview/workspace.png) | G1–G4, G9 |
| History | [history.png](../../../design-preview/history.png) | G3, G5, G9 |
| Mutations | [mutations.png](../../../design-preview/mutations.png) | G3, G5, G9 |
| Approval | [approval.png](../../../design-preview/approval.png) | G3, G4, G9 |
| Provider | [provider.png](../../../design-preview/provider.png) | G7, G9 |
| Appearance | [appearance.png](../../../design-preview/appearance.png) | G1, G2, G7 |
| Skills | [skills.png](../../../design-preview/skills.png) | G7, G9 |
| MCP | [mcp.png](../../../design-preview/mcp.png) | G7, G9 |
| Profiles | [profiles.png](../../../design-preview/profiles.png) | G7, G9 |
| Knowledge | [knowledge.png](../../../design-preview/knowledge.png) | G8, G9 |
| Agents | [agents.png](../../../design-preview/agents.png) | G8, G9 |
| A2A | [a2a.png](../../../design-preview/a2a.png) | G8, G9 |
| Footer picker | [workspace-model-picker.png](../../../design-preview/workspace-model-picker.png) | G6 overrides single-provider form |

## G11. Agent handoff và visual review gate

Mỗi agent nhận một slice phải đọc **toàn contract này**, rồi task tương ứng trong plan và đúng ảnh trong G10. Không chỉ nhận task title hoặc một crop thiếu header/footer. Primary integration owner giữ shared layout/theme; không giao palette/toolbar/settings navigation cho nhiều agents tự quyết độc lập.

Handoff cần chứa: task number, allowed files, G sections áp dụng, reference paths, proposed APIs/shared state boundaries và requirement không sửa behavior ngoài scope. Nêu rõ no build/lint/tests/formatters mid-flight khi các agents đang sửa shared workspace; chạy verification sau integration, không chạy khi files còn race.

**Không được tự thay đổi:** palette roles, density, typography hierarchy, region order, top nav/subnav, footer anchor, selected-record placement, breakpoint hoặc primary/destructive action semantics. Nếu native constraint bắt buộc thay đổi, báo exact control/constraint và ảnh minh họa; integration owner quyết định, user approval nếu đổi visual direction.

**Allowed adaptation:** Native SettingsDialog wrapper chính xác như G7, native scrollbar/combobox chrome, font metrics theo host/override, localized label expansion, table overflow trong table region, control height growth vì DPI/font. Không chấp nhận các adaptations làm mất essential controls hoặc làm popup/pane đổi loại interaction.

**Evidence bắt buộc khi integrate slice:**

- [ ] Screenshot native wide/narrow của surface đã sửa, nêu logical viewport, DPI, theme và font override. Không dùng HTML PNG để chứng minh native fidelity.
- [ ] So sánh side-by-side với đúng G10 reference; region order/action placement/palette/density đúng. Geometry baseline tolerance khoảng +/-2 logical px cho spacing/control metrics khi dùng baseline font; larger user fonts là adaptation được giải thích, không phải silent drift.
- [ ] Dark và Light khớp target roles; AUTO/IDA khớp host palette, không bị forced colors. Không recolor host application.
- [ ] Long connection/model/title và error/loading/empty/disabled/approval state vẫn đọc được; test 640/641 px transition, 460/1,100 px docks và DPI 100%/150% cho representative affected controls.
- [ ] Keyboard focus, Escape/Close, active/selected state, draft/scroll retention hoạt động thật. Screenshot đẹp không thay functional smoke.
- [ ] Với picker, kiểm tra grouped duplicate model IDs và busy state; với Settings, Cancel/Save pinned và unopened lazy-tab preservation; với Approval, full script + decisions visible.
- [ ] Native performance gate trong plan được đo trên integrated UI. Không đổi UI requirements để “đạt benchmark” hoặc trình bày prototype numbers như kết quả product.

Handoff report ngắn: files changed; G sections fulfilled; native screenshot paths + smoke evidence; intentional native deviations và approval tương ứng. Không đánh dấu slice hoàn thành chỉ vì compile hoặc unit tests pass.
