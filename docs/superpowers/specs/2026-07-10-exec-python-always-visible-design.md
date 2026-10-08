# ExecutePythonWidget — luôn-visible, bỏ collapse/expand

**Date:** 2026-07-10
**Supersedes:** `2026-07-09-exec-python-unified-widget-design.md` (thiết kế collapse-on-result gây regression — output không hiển thị)
**Status:** Approved (pending implementation)

## Bối cảnh & vấn đề

Sau loạt commit collapse-result-block (`a347221`, `9c44700`, `8388413`), `ExecutePythonWidget.set_result()` tự tay ẩn label + block frame sau khi nhận result (`tool_widgets.py:1734-1739`). Hệ quả: khi script Python chạy xong, user chỉ thấy `▶ ● execute_python ✓` — **không có output nào** cho đến khi click `▶`. Cùng lúc, `set_docs_gate_status("blocked")` và error result cũng bị collapse theo.

Người dùng quyết định **bỏ hoàn toàn cơ chế collapse/expand** cho `execute_python`: mọi phần (code, buttons khi cần, result) luôn visible theo state. Result nằm trong một **block output có kích thước cố định + scroll**, giống code editor hiện tại.

## Mục tiêu

1. Output của script **luôn hiển thị** ngay khi `TOOL_RESULT` về — không cần thao tác toggle.
2. Output nằm trong block scrollable, cao theo content, cap tại ~15 dòng rồi scroll — nhất quán với cách code editor render code.
3. Error/success phân biệt chỉ bằng màu (đỏ vs preview), cùng block, cùng vị trí.
4. Gỡ bỏ toàn bộ logic collapse/expand thừa: nút `▶`, `toggle_all()`, `_set_expanded()`, và các state flag liên quan.

## Phi mục tiêu

- Không đổi pipeline exec (`script_guard.py`, `scripting.py`) — output đã sinh đúng.
- Không đổi `ToolCallWidget` / `ToolBatchWidget` / `ToolGroupWidget` — các tool khác vẫn dùng cơ chế collapse riêng.
- Không thêm auto-approve hay thay đổi security gate.

## Thiết kế

### Layout kết quả

```
┌─────────────────────────────────────────┐
│ ● execute_python                    ✓   │  ← header: KHÔNG còn nút ▶
├─────────────────────────────────────────┤
│ Python code — 3 lines                   │  ← _code_info_label (luôn hiện khi có code)
│ ┌─────────────────────────────────────┐ │
│ │ print(1)                            │ │  ← _code_edit (QPlainTextEdit, luôn hiện)
│ │ print(2)                            │ │
│ └─────────────────────────────────────┘ │
│ [Allow] [Always Allow] [Deny]           │  ← chỉ hiện khi cần approve
├─────────────────────────────────────────┤
│ Result:                                 │  ← _result_header_label (luôn hiện khi có result)
│ ┌─────────────────────────────────────┐ │
│ │ stdout:                             ▲ │  ← _result_edit (QPlainTextEdit read-only)
│ │ 1                                     │     cao theo content, cap ~15 dòng + scroll
│ │ 2                                   ▼ │
│ └─────────────────────────────────────┘ │
└─────────────────────────────────────────┘
```

### Thay đổi trong `lucnhan/ui/tool_widgets.py`, class `ExecutePythonWidget`

#### a. Header — `_build_header()`
- **Xóa** `self._toggle_btn` (QToolButton ▶/▼) và dòng `header.addWidget(self._toggle_btn)`.
- Header chỉ còn: `●` (bullet) + `execute_python` (name) + stretch + status icon (`✓`/`✗`/`⟳`).

#### b. Code section — `_build_code_section()` + `set_code()`
- `_build_code_section()`: giữ nguyên, nhưng phần `section.setVisible(False)` cuối — widget cha luôn visible (visibility do có/không code quyết định trong `set_code`, không phải collapse).
- `set_code()`: cuối hàm **bỏ** `self._set_expanded(self._code_expanded)`, thay bằng luôn hiện: nếu có code thì `self._code_section().setVisible(True)`; nếu không code thì `setVisible(False)`.

#### c. Result block — `_build_result_block()` (thay đổi chính)
- **Thay** `self._result_label` (`_HeightCachedLabel` word-wrap) bằng `self._result_edit`: một `QPlainTextEdit` read-only, cấu hình y hệt `_code_edit` (trừ highlighter):
  - `setReadOnly(True)`
  - `setStyleSheet(get_tool_approval_code_editor_style())`
  - `setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)` (giống code editor — tránh wrap phá vỡ cấu trúc output)
  - Không gắn `_PythonHighlighter` (output là text thuần, không phải Python code)
- Dynamic height theo số dòng, cap tại 15 dòng:
  ```python
  lines = display.splitlines() if display.strip() else []
  visible = min(len(lines), _RESULT_MAX_LINES)
  line_height = self._result_edit.fontMetrics().lineSpacing()
  self._result_edit.setFixedHeight(line_height * visible + 16)  # +16 padding
  ```
- `_result_block` frame + `_result_header_label`: luôn visible khi có result (xem `set_result()`).
- Constant mới: `_RESULT_MAX_LINES = 15` (module-level, cạnh `_MAX_RESULT_DISPLAY`).

#### d. `set_result()` — viết lại
```python
def set_result(self, result: str, is_error: bool = False) -> None:
    tool_colors = get_tool_colors()
    self._is_error = is_error
    if self._blocked:
        # Docs gate đã blocked → TOOL_RESULT mang reviewer summary (error).
        # Summary đã ở status line; không render result block trùng lặp.
        self._buttons_visible = False
        self._buttons_container.setVisible(False)
        return
    display = result[:_MAX_RESULT_DISPLAY] + "\n... (truncated)" if len(result) > _MAX_RESULT_DISPLAY else result
    self._result_edit.setPlainText(display)
    # Dynamic height, cap ~15 dòng.
    lines = display.splitlines() if display.strip() else []
    visible = min(len(lines), _RESULT_MAX_LINES)
    line_height = self._result_edit.fontMetrics().lineSpacing()
    self._result_edit.setFixedHeight(max(line_height * visible + 16, line_height + 16))
    # Luôn visible.
    self._result_header_label.setVisible(True)
    self._result_block.setVisible(True)
    self._buttons_visible = False
    self._buttons_container.setVisible(False)
    if is_error:
        self._result_edit.setStyleSheet(get_tool_result_editor_style(tool_colors['status_error']))
        self._status_icon.setText("✗")
        self._status_icon.setStyleSheet(f"color: {tool_colors['status_error']}; font-size: inherit;")
        self._bullet.setStyleSheet(f"color: {tool_colors['status_error']}; font-size: inherit;")
    else:
        self._result_edit.setStyleSheet(get_tool_result_editor_style())
        self._status_icon.setText("✓")
        self._status_icon.setStyleSheet(f"color: {tool_colors['status_success']}; font-size: inherit;")
```

**Lưu ý style error:** `get_tool_approval_code_editor_style()` đã set `color: {t.code_text}` trong selector `QPlainTextEdit` (xem `theme/widgets_mutation.py:87`). Append `{{ color: red }}` rời sẽ KHÔNG override được (QSS ưu tiên rule đầu khớp). Giải pháp: thêm function `get_tool_result_editor_style(text_color: str | None = None)` trong `widgets_mutation.py` — clone `_tool_approval_code_editor_style()` nhưng nhận `text_color` param, default `t.code_text` khi `None`, `tool_colors['status_error']` khi error. Gọi function này trong cả `set_result()` và `_apply_styles()`.

#### e. Bỏ toàn bộ logic collapse/expand
**Xóa các method:**
- `_set_expanded(self, expanded)`
- `toggle_all(self)`

**Xóa các instance attribute:**
- `self._code_expanded`
- `self._result_block_visible`, `self._result_block_visible_current`
- `self._result_content_visible`
- `self._result_header_visible`
- `self._status_detail_visible`, `self._status_detail_text`

**Xóa reference trong:**
- `show_approval_buttons()`: bỏ `self._set_expanded(True)` — chỉ hiện buttons container.
- `hide_preview()`: bỏ thân (hoặc xóa hàm nếu ChatView không còn gọi — verify trước).

#### f. `_apply_styles()` — cập nhật
- Thay `_result_label` reference bằng `_result_edit`.
- Error color path: kiểm tra `_is_error` rồi `self._result_edit.setStyleSheet(get_tool_result_editor_style(tool_colors['status_error']))`, ngược lại `get_tool_result_editor_style()` — mirror logic `set_result()`.
- Bỏ logic `_result_content_visible` gate (không còn flag này).

#### g. `set_docs_gate_status("blocked")` — điều chỉnh
- `_status_detail` (reviewer summary đầy đủ) giờ **luôn visible** khi blocked (bỏ toggle) — text đầy đủ hiện thẳng, không còn "click ▶ for details".
- Cập nhật `_status_text` header: bỏ "click ▶ for details", chỉ giữ "✗ Docs review blocked".
- `_status_detail` set text + `setVisible(True)` ngay khi state == "blocked".
- Xóa các dòng clear `_status_detail_text` / `_status_detail_visible` ở cuối method (không còn khái niệm collapsed detail).

### Điều chỉnh caller

- `chat_view.py`: kiểm tra mọi call tới `toggle_all()`, `hide_preview()`, `append_args_delta()` trên `ExecutePythonWidget`. `append_args_delta()` giữ (no-op, compat với ToolCallWidget API). `hide_preview()` — nếu ChatView gọi cho ExecutePythonWidget khi grouping, thì hoặc giữ no-op, hoặc bỏ call. Verify trong implementation.

### Testing

Test file: `tests/tools/test_execute_python_widget.py`.

**Test cần sửa (đảo ngược assertion — chúng đang lock bug):**
- `test_result_collapsed_by_default` → đổi tên `test_result_visible_by_default`: `assertTrue(w._result_block.isVisible())` (hoặc check `_result_edit` có text).
- `test_result_label_hidden_when_collapsed` → xóa (không còn khái niệm collapsed).
- `test_result_block_hidden_when_collapsed` → xóa.
- `test_result_expandable` → xóa (không còn toggle).
- `test_single_header_toggle_controls_all_content` → xóa.
- `test_result_error_collapsed_by_default` → đổi thành `test_result_error_visible_by_default`.
- `test_blocked_status_collapsed_by_default` / `test_blocked_status_expandable` → đổi thành `test_blocked_status_detail_visible_by_default`.
- `test_hide_preview_collapses_code` → xóa hoặc đổi thành `test_hide_preview_noop` nếu giữ hàm.

**Test mới:**
- `test_set_result_shows_output_block`: sau `set_result("42")`, `_result_edit` chứa "42" và visible.
- `test_result_long_output_scrollable`: set result 50 dòng → `_result_edit` fixed height = 15 dòng (cap), text đầy đủ vẫn trong document (scroll).
- `test_result_short_output_compact`: set result 2 dòng → height = 2 dòng (không cap).
- `test_result_error_colors`: `set_result(..., is_error=True)` → `_result_edit` foreground đỏ, `_status_icon` = "✗".
- `test_no_toggle_button_in_header`: header không còn QToolButton collapse.
- `test_set_code_always_visible`: sau `set_code("print(1)")`, code section visible=True.

### Cân nhắc theme

- Output block dùng cùng style nền/border với code editor (`get_tool_approval_code_editor_style()`) → nhất quán light/dark.
- Error: verify text đỏ có contrast đủ trên nền editor cả light & dark theme (dùng `tool_colors['status_error']` token, đã tested cho text labels).
- `_apply_styles()` phải repaint `_result_edit` đúng màu khi theme switch mid-session.

## Verify trước khi merge

- [ ] `./ci-local.sh` pass (format + lint + mypy + pytest + desloppify).
- [ ] Test cũ đã đảo ngược / xóa; test mới pass.
- [ ] `set_result()` success → output visible ngay, scroll khi dài.
- [ ] `set_result()` error → output visible + đỏ.
- [ ] `set_docs_gate_status("blocked")` → summary đầy đủ visible.
- [ ] Không còn nút `▶` trong header `execute_python`.
- [ ] Code editor luôn visible khi có code.
- [ ] Approval buttons vẫn hiện đúng khi `show_approval_buttons()`.
- [ ] Theme switch repaint output block đúng (light ↔ dark).
- [ ] ChatView không crash khi `hide_preview()` / `append_args_delta()` được gọi.

## File ảnh hưởng

- `lucnhan/ui/theme/widgets_mutation.py` (thêm `get_tool_result_editor_style(text_color=None)`).
- `lucnhan/ui/tool_widgets.py` (class `ExecutePythonWidget` — viết lại đáng kể).
- `lucnhan/ui/chat_view.py` (xóa/điều chỉnh caller nếu cần — verify trong implementation).
- `tests/tools/test_execute_python_widget.py` (đảo ngược + thêm test).
