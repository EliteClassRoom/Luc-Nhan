"""Tool call, batch, group, and approval widgets."""

from __future__ import annotations

import json
import re as _re
from collections.abc import Callable
from typing import ClassVar

from .. import constants
from .message_widgets import _HeightCachedLabel
from .qt_compat import (
    QApplication,
    QColor,
    QFont,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QSyntaxHighlighter,
    Qt,
    QTextCharFormat,
    QTimer,
    QToolButton,
    QVBoxLayout,
    QWidget,
    Signal,
)
from .styles import (
    get_tool_approval_allow_btn_style,
    get_tool_approval_always_btn_style,
    get_tool_approval_body_style,
    get_tool_approval_code_editor_style,
    get_tool_approval_deny_btn_style,
    get_tool_approval_disabled_btn_style,
    get_tool_approval_frame_style,
    get_tool_approval_header_style,
    get_tool_approval_state_style,
    get_tool_colors,
    get_tool_detail_surface_style,
    get_tool_result_editor_style,
    get_tool_row_meta_style,
    get_tool_row_name_style,
    get_tool_row_style,
    get_tool_row_target_style,
)
from .theme.applicator import bind_theme, disconnect_theme
from .theme.manager import ThemeManager

_MAX_ARGS_DISPLAY = 2000
_MAX_RESULT_DISPLAY = 3000
#: Maximum visible lines in the ExecutePythonWidget result editor before it
#: scrolls. Mirrors the code editor's 15-line cap.
_RESULT_MAX_LINES = 15
_TOOL_PREVIEW_LINES = 3
#: GUI contract G2 — tool summary row geometry.
_TOOL_ROW_MIN_HEIGHT = 30
#: GUI contract G4 — the expanded detail inset pads 7 top / 10 right-bottom /
#: 22 left so nested code reads as a block without a nested card.
_TOOL_DETAIL_MARGINS = (7, 10, 22, 10)  # (top, right, left, bottom)
#: Characters kept in the elided target column. Qt elides the rest with
#: ``…``; the full text always reaches the row tooltip.
_TOOL_TARGET_MAX_CHARS = 120


# ---------------------------------------------------------------------------
# Tool row styling (GUI contract G2/G4)
#
# A tool activity row is ONE compact row — chevron, status glyph, tool
# name, elided monospace target, right-hand status/count — with no card
# chrome. The bordered card look is what G4 replaced; the only bordered
# surface left in a tool row is the expanded detail inset.
# ---------------------------------------------------------------------------


def _tool_bg(t) -> str:
    """Background color for the expanded detail surface (base tier)."""
    return t.base


def _tool_border(t) -> str:
    """Border color for the expanded detail surface (mid tier)."""
    return t.mid


def _tool_card_css(
    *,
    accent: str | None = None,
    background: str | None = None,
    radius: int = 3,
    object_name: str = "tool_detail",
) -> str:
    """Border/background QSS for an expanded tool-detail inset (G4).

    Only the expanded detail is a bordered surface now. ``accent`` /
    ``background`` stay accepted so callers can paint a highlighted
    surface without rebuilding the QSS by hand.
    """
    t = ThemeManager.instance().tokens()
    border = accent or _tool_border(t)
    bg = background or _tool_bg(t)
    return f"QFrame#{object_name} {{ background-color: {bg}; border: 1px solid {border}; border-radius: {radius}px;}}"


# ---------------------------------------------------------------------------
# MCP prefix stripping — works with any MCP server, not just a specific one
# ---------------------------------------------------------------------------


def _strip_mcp_prefix(name: str) -> str:
    """Strip MCP server prefix (``mcp__<server>__``) from tool names."""
    if name.startswith("mcp__"):
        rest = name[5:]  # after "mcp__"
        idx = rest.find("__")
        if idx >= 0:
            return rest[idx + 2 :]
    return name


_TOOL_GROUP_LABELS: dict[str, tuple[str, str]] = {
    "decompile_function": ("Decompiled", "function"),
    "read_disassembly": ("Disassembled", "function"),
    "read_function_disassembly": ("Disassembled", "function"),
    "search_strings": ("Searched", "string"),
    "list_strings_filter": ("Searched", "string"),
    "search_functions": ("Searched", "function"),
    "search_functions_by_name": ("Searched", "function"),
    "read_file": ("Read", "file"),
}


# ---------------------------------------------------------------------------
# Summary-row primitives (shared by ToolCallWidget/ToolBatchWidget/
# ToolGroupWidget — GUI contract G4)
#
# The row order is fixed by G4: disclosure chevron -> status/icon -> tool
# name -> target/argument (monospace, elided) -> result count/status right.
# Status is carried by a glyph AND a word, never by colour alone (G9), so
# every terminal state gets a distinct, readable pair.
# ---------------------------------------------------------------------------

#: ``(icon, right-hand text)`` per state. The icon is the non-colour
#: carrier required by G9; the text is what a screen reader reads.
STATE_RUNNING = ("…", "Running")
STATE_SUCCESS = ("✓", "Done")
STATE_ERROR = ("✗", "Failed")
STATE_DISCARDED = ("⊘", "Skipped")
STATE_APPROVAL = ("⏸", "Paused")


def _row_target_text(summary: str) -> str:
    """Collapse a tool summary to one elided-ready line (G4)."""
    flat = " ".join(summary.split())
    if len(flat) > _TOOL_TARGET_MAX_CHARS:
        flat = flat[: _TOOL_TARGET_MAX_CHARS - 1] + "…"
    return flat


def _row_status_colors() -> dict[str, str]:
    """Status colours from the shared tool-colour mapping.

    ``get_tool_colors`` is branch-keyed off ``is_dark_theme`` and already
    carries the success/error/spinner/preview roles, so no new palette
    (and no hardcoded hex) is introduced here.
    """
    return get_tool_colors()


def _status_style(tool_colors: dict[str, str], state: str) -> str:
    """QSS for the status icon + right-hand status text of one state."""
    key = {
        "running": "status_spinner",
        "success": "status_success",
        "error": "status_error",
        "discarded": "preview",
    }.get(state, "preview")
    return f"color: {tool_colors.get(key, tool_colors['preview'])}; font-size: inherit;"


def _make_detail_frame() -> QFrame:
    """Build the inset detail surface used for expanded tool output (G4).

    Flat ``base`` fill with a 1px ``mid`` border and radius 3 (the
    compact inset radius from G1), padding 7 top / 10 right-bottom /
    22 left so nested code reads as a block rather than a nested card.
    """
    frame = QFrame()
    frame.setObjectName("tool_detail")
    frame.setStyleSheet(get_tool_detail_surface_style())
    return frame


def _make_detail_layout(parent: QWidget) -> QVBoxLayout:
    """Vertical layout with the G4 inset paddings for a detail surface."""
    layout = QVBoxLayout(parent)
    top, right, left, bottom = _TOOL_DETAIL_MARGINS
    layout.setContentsMargins(left, top, right, bottom)
    layout.setSpacing(4)
    return layout


def _make_result_actions(
    text: str,
    *,
    on_raw: Callable[[], None],
    on_copy: Callable[[], None],
) -> tuple[QWidget, QHBoxLayout]:
    """Raw / Copy actions, bottom-right of an expanded detail surface (G4).

    Two flat caption-height buttons sharing one QSS keep the strip
    unobtrusive; they are appended after a stretch so they land at the
    bottom-right of the detail surface.
    """
    container = QWidget()
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    row.addStretch()

    t = ThemeManager.instance().tokens()
    btn_css = (
        f"QToolButton {{ color: {t.muted_text}; background: transparent; "
        f"border: 1px solid {t.mid}; border-radius: 3px; padding: 2px 8px; "
        f"font-size: inherit; }}"
        f"QToolButton:hover {{ background: {t.alt_base}; color: {t.text}; }}"
        f"QToolButton:focus {{ border: 1px solid {t.accent}; }}"
    )

    raw_btn = QToolButton()
    raw_btn.setText("Raw")
    raw_btn.setToolTip("Show the unformatted tool output")
    raw_btn.setAccessibleName("Show raw tool output")
    raw_btn.setStyleSheet(btn_css)
    raw_btn.clicked.connect(on_raw)
    row.addWidget(raw_btn)

    copy_btn = QToolButton()
    copy_btn.setText("Copy")
    copy_btn.setToolTip("Copy the tool output to the clipboard")
    copy_btn.setAccessibleName("Copy tool output to clipboard")
    copy_btn.setStyleSheet(btn_css)
    copy_btn.clicked.connect(on_copy)
    row.addWidget(copy_btn)

    del text
    return container, row


def _copy_to_clipboard(text: str) -> None:
    """Copy *text* to the system clipboard (best effort, main thread)."""
    try:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
    except RuntimeError:  # pragma: no cover — clipboard may be gone at teardown
        pass


class _ElidedLabel(QLabel):
    """Single-line QLabel that elides with ``…`` instead of clipping.

    Qt's QLabel clips at the widget boundary without an ellipsis and QSS
    ``text-overflow`` is not honoured for QLabel, so the elided string is
    recomputed from the live font metrics on every width change (G2/G4).
    The full text is kept for the row tooltip — that is where G4 wants
    the complete, already-sanitised argument to live.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setTextFormat(Qt.TextFormat.PlainText)

    def set_full_text(self, text: str) -> None:
        """Set the complete text; the label shows the elided form."""
        self._full_text = text or ""
        self.setToolTip(self._full_text)
        self._refresh_elide()

    def _refresh_elide(self) -> None:
        width = self.width()
        if width <= 0:
            super().setText(self._full_text)
            return
        super().setText(self.fontMetrics().elidedText(self._full_text, Qt.TextElideMode.ElideRight, width - 2))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_elide()


def _build_approval_header(tool_name: str) -> str:
    """Build the approval-gate header label for a tool call.

    The header names the *actual* tool being approved (with the MCP
    ``mcp__<server>__`` prefix stripped) instead of the historical
    hard-coded ``"Approve execute_python?"``. The same approval widget
    is reused for every mutating tool when ``config.approve_mutations``
    is set, so a label that always says ``execute_python`` left the user
    unable to tell whether they were authorising a rename, a type
    change, or arbitrary script execution.
    """
    return f"Approve {_strip_mcp_prefix(tool_name)}?"


def _format_tool_group_label(tool_names: list[str]) -> str:
    """Human-friendly group label for collapsed tool-call runs."""
    count = len(tool_names)
    if count <= 0:
        return "0 tools called"

    base_names = [_strip_mcp_prefix(name) for name in tool_names]
    unique_names = {name for name in base_names if name}

    if len(unique_names) == 1:
        only_name = base_names[0]
        template = _TOOL_GROUP_LABELS.get(only_name)
        if template:
            action, noun = template
            suffix = "" if count == 1 else "s"
            return f"{action} {count} {noun}{suffix}"

    return f"{count} tool called" if count == 1 else f"{count} tools called"


# ---------------------------------------------------------------------------
# Smart tool parameter summaries
# ---------------------------------------------------------------------------

#: Maximum length of a one-line tool summary (truncated with "..." if exceeded).
_SUMMARY_MAX_CHARS = 80


def _trunc(text: str, limit: int) -> str:
    """Truncate *text* to *limit* chars, appending ``...`` when shortened."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


# Each formatter takes a ``_get(key, *fallbacks)`` accessor (which returns the
# first non-None arg as a string) and returns the summary for one tool. Tools
# sharing the same shape share the same callable, so adding a tool is a
# one-line table entry rather than a new elif branch.
def _fmt_address(getter) -> str:
    return getter("address", "ea", "name", "target", "func_id")


def _fmt_address_with_type(getter) -> str:
    target = getter("address", "ea", "func_address", "var_name")
    type_str = getter("type_str", "prototype", "type")
    return f"{target}: {type_str}" if target and type_str else ""


def _fmt_xref_target(getter) -> str:
    return getter("address", "ea", "name", "struct_name")


def _fmt_search_query(getter) -> str:
    query = getter("pattern", "query", "filter", "name")
    return f'"{query}"' if query else ""


def _fmt_define_types(getter) -> str:
    code = getter("c_code", "c_declaration", "types")
    return _trunc(code, 60) if code else ""


def _fmt_struct_name(getter) -> str:
    return getter("name", "struct_name")


def _fmt_execute_python(getter) -> str:
    code = getter("code", "script")
    if not code:
        return ""
    return _trunc(code.strip().split("\n")[0], 60)


def _fmt_read_disassembly(getter) -> str:
    return getter("name", "ea", "address", "start")


def _fmt_hexdump(getter) -> str:
    return getter("address", "ea", "name")


def _fmt_rename_function(getter) -> str:
    old = getter("old_name", "current_name", "ea")
    new = getter("new_name")
    return f"{old} → {new}" if old and new else ""


def _fmt_rename_variable(getter) -> str:
    func = getter("function_name", "func_address", "ea")
    old = getter("variable_name", "old_name")
    new = getter("new_name")
    if not (old and new):
        return ""
    return f"{func}: {old} → {new}" if func else f"{old} → {new}"


def _fmt_set_comment(getter) -> str:
    addr = getter("address", "ea")
    comment = _trunc(getter("comment", "text"), 50)
    if addr and comment:
        return f"{addr}: {comment}"
    return comment


def _fmt_exploration_report(getter) -> str:
    cat = getter("category")
    sm = _trunc(getter("summary"), 50)
    return f"[{cat}] {sm}" if cat and sm else (sm or cat or "")


def _fmt_phase_transition(getter) -> str:
    phase = getter("to_phase")
    reason = _trunc(getter("reason"), 40)
    return f"→ {phase}" + (f": {reason}" if reason else "")


#: Maps a stripped tool name to its summary formatter. Lookup happens after
#: ``_strip_mcp_prefix``, so e.g. ``mcp__bn__decompile_function`` resolves here
#: as ``decompile_function``.
_TOOL_SUMMARY_FORMATTERS: dict[str, Callable[[Callable[..., str]], str]] = {
    "decompile_function": _fmt_address,
    "rename_function": _fmt_rename_function,
    "rename_variable": _fmt_rename_variable,
    "set_comment": _fmt_set_comment,
    "set_function_comment": _fmt_set_comment,
    "set_type": _fmt_address_with_type,
    "set_function_prototype": _fmt_address_with_type,
    "apply_type_to_variable": _fmt_address_with_type,
    "xrefs_to": _fmt_xref_target,
    "xrefs_from": _fmt_xref_target,
    "function_xrefs": _fmt_xref_target,
    "search_strings": _fmt_search_query,
    "search_functions": _fmt_search_query,
    "search_functions_by_name": _fmt_search_query,
    "list_strings_filter": _fmt_search_query,
    "define_types": _fmt_define_types,
    "declare_c_type": _fmt_define_types,
    "create_struct": _fmt_struct_name,
    constants.EXECUTE_PYTHON_TOOL_NAME: _fmt_execute_python,
    "read_disassembly": _fmt_read_disassembly,
    "read_function_disassembly": _fmt_read_disassembly,
    "hexdump_address": _fmt_hexdump,
    "hexdump_data": _fmt_hexdump,
    "get_data_decl": _fmt_hexdump,
    "exploration_report": _fmt_exploration_report,
    "phase_transition": _fmt_phase_transition,
}

#: Generic fallback: first matching common parameter wins.
_GENERIC_SUMMARY_KEYS = (
    "target",
    "address",
    "ea",
    "name",
    "path",
    "query",
    "pattern",
    "command",
)


def _format_tool_summary(tool_name: str, args_text: str) -> str:
    """Extract the most relevant parameter for a compact one-line summary."""
    try:
        args = json.loads(args_text) if args_text else {}
    except (json.JSONDecodeError, TypeError):
        return ""

    if not isinstance(args, dict):
        return ""

    def _get(*keys: str) -> str:
        for k in keys:
            v = args.get(k)
            if v is not None:
                return str(v)
        return ""

    # Strip MCP prefix for matching (works with any MCP server)
    short_name = _strip_mcp_prefix(tool_name)

    fmt = _TOOL_SUMMARY_FORMATTERS.get(short_name)
    if fmt is not None:
        summary = fmt(_get)
    else:
        # Generic: try common parameter names
        summary = ""
        for key in _GENERIC_SUMMARY_KEYS:
            val = _get(key)
            if val:
                summary = val
                break

    return _trunc(summary, _SUMMARY_MAX_CHARS)


def _truncate_preview(text: str, max_lines: int = _TOOL_PREVIEW_LINES) -> str:
    """Return first N lines with a '… +M lines' indicator if truncated."""
    lines = text.split("\n")
    if len(lines) <= max_lines:
        return text
    preview = "\n".join(lines[:max_lines])
    remaining = len(lines) - max_lines
    return f"{preview}\n… +{remaining} lines"


def _make_preview_label() -> QLabel:
    """Create a collapsed-state preview QLabel (shared by ToolCallWidget/ToolBatchWidget).

    Uses ``_HeightCachedLabel`` so the word-wrapped label opts out of Qt's
    ``heightForWidth`` protocol — the same layout-cascade fix as the message
    content labels. Agentic loops emit many tool calls; each preview label
    would otherwise trigger an O(text_length) walk on its siblings whenever
    ``setText`` fires.
    """
    lbl = _HeightCachedLabel()
    lbl.setObjectName("tool_content")
    lbl.setWordWrap(True)
    tool_colors = get_tool_colors()
    lbl.setStyleSheet(f"color: {tool_colors['preview']}; font-size: inherit; margin-left: 28px;")
    lbl.setVisible(False)
    return lbl


# ---------------------------------------------------------------------------
# Shared spinner timer
# ---------------------------------------------------------------------------


class _SharedSpinnerTimer:
    """Single QTimer shared across all ToolCallWidget instances.

    Instead of N timers (one per widget), one 100ms timer ticks all
    active spinners.  Starts automatically when the first widget
    registers and stops when the last one unregisters.
    """

    _instance: _SharedSpinnerTimer | None = None

    def __init__(self) -> None:
        self._timer = QTimer()
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)
        self._widgets: set[ToolCallWidget] = set()

    @classmethod
    def get(cls) -> _SharedSpinnerTimer:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(self, widget: ToolCallWidget) -> None:
        self._widgets.add(widget)
        # NOTE: Do NOT connect to widget.destroyed here.  PySide6/Shiboken
        # under IDA may GC the lambda slot, leaving a dangling C++ pointer
        # in Qt's connection list.  When *any* QWidget is later destroyed,
        # the stale slot pointer causes a SIGBUS (0xaaaa freed-memory read).
        # Stale widgets are already handled safely in _tick() via RuntimeError.
        if not self._timer.isActive():
            self._timer.start()

    def unregister(self, widget: ToolCallWidget) -> None:
        self._widgets.discard(widget)
        if not self._widgets and self._timer.isActive():
            self._timer.stop()

    def _tick(self) -> None:
        stale: set = set()
        for w in list(self._widgets):
            try:
                w._spin_tick()
            except RuntimeError:
                # Shiboken raises RuntimeError when the C++ object was already deleted.
                stale.add(w)
        if stale:
            self._widgets -= stale
            if not self._widgets and self._timer.isActive():
                self._timer.stop()

    @classmethod
    def shutdown(cls) -> None:
        """Stop and discard the singleton so the QTimer doesn't outlive QApplication."""
        inst = cls._instance
        if inst is not None:
            inst._widgets.clear()
            inst._timer.stop()
            inst._timer.deleteLater()
            cls._instance = None


class ToolCallWidget(QFrame):
    """Compact tool activity row (GUI contract G2/G4).

    Row order is fixed by G4: ``chevron -> status icon -> tool name ->
    target (monospace, elided) -> status/count right``. One row, 30 px
    minimum, no card chrome. The expanded detail is an inset code surface
    that is **built on first expand** — args/result text is not measured
    or wrapped while the row is collapsed, so a chat full of tool calls
    stays cheap to lay out.

    Status is carried by a glyph *and* a word (``Failed`` / ``Done`` /
    ``Running`` / ``Skipped``) so the state survives greyscale and colour
    vision deficiency (G9).
    """

    _SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self, tool_name: str, tool_call_id: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("message_tool")
        self.setMinimumHeight(_TOOL_ROW_MIN_HEIGHT)
        self._tool_name = tool_name
        self._tool_call_id = tool_call_id
        self._args_text = ""
        self._result_text = ""
        self._is_error = False
        self._expanded = False
        self._details_built = False
        self._spin_idx = 0
        self._state = "running"
        #: Duration only when the host actually measured one — never a
        #: fabricated number (G4: "only real data").
        self._duration_s: float | None = None

        layout = QVBoxLayout(self)
        # G2: tool summary padding 5 / 6 px, gap 8 px.
        layout.setContentsMargins(6, 5, 6, 5)
        layout.setSpacing(2)

        layout.addLayout(self._build_header(tool_name))
        # Detail container starts empty; ``_toggle`` fills it on first
        # expand so nothing is rendered (or height-measured) while collapsed.
        self._detail_holder = QWidget()
        self._detail_holder.setVisible(False)
        layout.addWidget(self._detail_holder)

        _SharedSpinnerTimer.get().register(self)
        # Subscribed *after* the header is built so an early themeChanged
        # signal finds the widget fully constructed. ``bind_theme`` runs the
        # apply callback synchronously so the initial paint already
        # reflects the live tokens.
        bind_theme(self, self._apply_styles)

    # ------------------------------------------------------------------
    # Styling
    # ------------------------------------------------------------------

    def _apply_styles(self, _tokens: object = None) -> None:
        """Re-apply row QSS and refresh every child label colour.

        Re-runs on ``themeChanged``. ``_tokens`` is accepted for the
        ``bind_theme`` contract; the callback always reads the live
        tokens itself so the hookup works regardless of which signal
        path delivered the refresh (Qt ``Signal`` carries the token;
        ``_DummySignal`` in the headless fallback does not).
        """
        self.setStyleSheet(get_tool_row_style())
        tool_colors = _row_status_colors()
        if getattr(self, "_name_label", None) is not None:
            self._name_label.setStyleSheet(get_tool_row_name_style())
        if getattr(self, "_summary_label", None) is not None:
            self._summary_label.setStyleSheet(get_tool_row_target_style())
        self._apply_status_style(tool_colors)
        if getattr(self, "_preview_label", None) is not None:
            self._preview_label.setStyleSheet(get_tool_row_target_style())

    def _apply_status_style(self, tool_colors: dict[str, str] | None = None) -> None:
        """Paint the status icon + right-hand status text from the state.

        Keeps the spinner / success / error / discarded mapping in ONE
        place so a theme switch preserves the current outcome instead of
        reverting an in-flight or finished call to the default colour.
        """
        colors = tool_colors or _row_status_colors()
        qss = _status_style(colors, self._state)
        for name in ("_status_icon", "_status_label"):
            lbl = getattr(self, name, None)
            if lbl is not None:
                lbl.setStyleSheet(qss)

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    def _build_header(self, tool_name: str) -> QHBoxLayout:
        """Build the one-line summary row per G4."""
        display_name = _strip_mcp_prefix(tool_name)

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)

        self._toggle_btn = QToolButton()
        self._toggle_btn.setObjectName("collapse_button")
        self._toggle_btn.setText("▶")
        self._toggle_btn.setFixedSize(14, 14)
        self._toggle_btn.setToolTip("Show the full tool arguments and output")
        self._toggle_btn.setAccessibleName(f"Expand {display_name} details")
        self._toggle_btn.clicked.connect(self._toggle)
        header_layout.addWidget(self._toggle_btn)

        self._status_icon = QLabel(STATE_RUNNING[0])
        self._status_icon.setFixedWidth(14)
        self._status_icon.setAccessibleName("Tool status")
        header_layout.addWidget(self._status_icon)

        self._name_label = QLabel(display_name)
        self._name_label.setTextFormat(Qt.TextFormat.PlainText)
        self._name_label.setStyleSheet(get_tool_row_name_style())
        header_layout.addWidget(self._name_label)

        self._summary_label = _ElidedLabel()
        self._summary_label.setStyleSheet(get_tool_row_target_style())
        header_layout.addWidget(self._summary_label, 1)

        self._status_label = QLabel(STATE_RUNNING[1])
        self._status_label.setStyleSheet(get_tool_row_meta_style())
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header_layout.addWidget(self._status_label)

        self._apply_status_style()

        return header_layout

    def _build_detail_section(self) -> QWidget:
        """Build the expanded detail surface (args + result + Raw/Copy).

        Called only from ``_toggle`` on the first expand, which is what
        keeps raw/table/code content out of the layout while collapsed.
        """
        detail = _make_detail_frame()
        detail_layout = _make_detail_layout(detail)

        self._args_label = _HeightCachedLabel()
        self._args_label.setObjectName("tool_content")
        self._args_label.setWordWrap(True)
        self._args_label.setTextInteractionFlags(
            Qt.TextInteractionFlag(
                Qt.TextInteractionFlag.TextSelectableByMouse.value
                | Qt.TextInteractionFlag.TextSelectableByKeyboard.value
            )
        )
        detail_layout.addWidget(self._args_label)

        self._result_header = QLabel("Result")
        self._result_header.setTextFormat(Qt.TextFormat.PlainText)
        self._result_header.setStyleSheet(get_tool_row_name_style())
        self._result_header.setVisible(False)
        detail_layout.addWidget(self._result_header)

        self._result_label = _HeightCachedLabel()
        self._result_label.setObjectName("tool_content")
        self._result_label.setWordWrap(True)
        self._result_label.setTextInteractionFlags(
            Qt.TextInteractionFlag(
                Qt.TextInteractionFlag.TextSelectableByMouse.value
                | Qt.TextInteractionFlag.TextSelectableByKeyboard.value
            )
        )
        self._result_label.setVisible(False)
        detail_layout.addWidget(self._result_label)

        actions, _row = _make_result_actions(
            self._result_text,
            on_raw=self._on_raw,
            on_copy=self._on_copy,
        )
        self._raw_btn_row = actions
        detail_layout.addWidget(actions)

        holder_layout = QVBoxLayout(self._detail_holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setSpacing(0)
        holder_layout.addWidget(detail)

        self._render_detail_text()
        return detail

    # ------------------------------------------------------------------
    # Detail rendering (lazy)
    # ------------------------------------------------------------------

    def _render_detail_text(self) -> None:
        """Fill the (already built) detail surface from the stored text.

        Called when the detail is first built and again whenever new
        args/result text arrives *while expanded*, so a collapsed row
        never measures or wraps text it is not showing.
        """
        if not self._details_built:
            return
        display = (
            self._args_text[:_MAX_ARGS_DISPLAY] + "..." if len(self._args_text) > _MAX_ARGS_DISPLAY else self._args_text
        )
        self._args_label.setText(display)
        self._args_label.pin_height()
        if self._result_text:
            result = self._result_text
            display_res = (
                result[:_MAX_RESULT_DISPLAY] + "\n... (truncated)" if len(result) > _MAX_RESULT_DISPLAY else result
            )
            self._result_label.setText(display_res)
            self._result_label.pin_height()
            self._result_label.setVisible(True)
            self._result_header.setVisible(True)
        if self._is_error:
            self._result_label.setStyleSheet(f"color: {_row_status_colors()['status_error']}; font-size: inherit;")

    def _on_raw(self) -> None:
        """Reveal the untruncated result text inside the detail surface."""
        if not self._details_built:
            return
        self._result_label.setText(self._result_text)
        self._result_label.pin_height()
        self._result_label.setVisible(True)

    def _on_copy(self) -> None:
        """Copy the tool output (args + result) to the clipboard."""
        parts = [p for p in (self._args_text.strip(), self._result_text.strip()) if p]
        _copy_to_clipboard("\n\n".join(parts))

    # ------------------------------------------------------------------
    # Spinner / expand
    # ------------------------------------------------------------------

    def _spin_tick(self) -> None:
        """Advance the spinner animation frame."""
        self._spin_idx = (self._spin_idx + 1) % len(self._SPINNER_FRAMES)
        self._status_icon.setText(self._SPINNER_FRAMES[self._spin_idx])

    def _stop_spinner(self) -> None:
        """Unregister from the shared spinner timer."""
        _SharedSpinnerTimer.get().unregister(self)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        if self._expanded and not self._details_built:
            self._build_detail_section()
            self._details_built = True
        self._detail_holder.setVisible(self._expanded)
        self._toggle_btn.setText("▼" if self._expanded else "▶")
        self._toggle_btn.setAccessibleName(
            f"{'Collapse' if self._expanded else 'Expand'} {_strip_mcp_prefix(self._tool_name)} details"
        )

    def _set_state(self, state: str, detail: str = "") -> None:
        """Apply a terminal/in-flight state to icon + right-hand text."""
        self._state = state
        icon, text = {
            "running": STATE_RUNNING,
            "success": STATE_SUCCESS,
            "error": STATE_ERROR,
            "discarded": STATE_DISCARDED,
        }.get(state, STATE_RUNNING)
        self._status_icon.setText(icon)
        self._status_label.setText(text)
        self._apply_status_style()

    def _set_meta(self, text: str) -> None:
        """Show measured extra info (duration, result counts) on the right."""
        if text:
            self._status_label.setText(text)
            self._status_label.setToolTip(text)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_arguments(self, args_text: str) -> None:
        self._args_text = args_text
        # Elided target column; the full, sanitised text lives in the
        # tooltip (G4) and in the expanded detail.
        self._summary_label.set_full_text(_row_target_text(_format_tool_summary(self._tool_name, args_text)))
        if self._expanded:
            self._render_detail_text()

    def append_args_delta(self, delta: str) -> None:
        self._args_text += delta
        # Don't update the target column during streaming — wait for
        # ``set_arguments`` when the JSON is complete (a half-parsed
        # value would render as a truncated or wrong target).

    def set_result(self, result: str, is_error: bool = False) -> None:
        self._stop_spinner()
        self._result_text = result
        self._is_error = is_error
        self._set_state("error" if is_error else "success")
        self._set_meta(_describe_result(result, is_error))
        if self._expanded:
            self._render_detail_text()

    def mark_done(self) -> None:
        self._stop_spinner()
        if self._state == "running":
            self._set_state("success")

    def mark_discarded(self, reason: str = "discarded") -> None:
        """Stop the spinner and set a neutral terminal glyph without result.

        Called when a partial tool call is dropped instead of executed
        (truncated args, degenerated JSON). Shows a short status line
        so the user understands why the call has no result.
        """
        self._stop_spinner()
        self._set_state("discarded")
        self._set_meta(f"Skipped — {reason}")

    def hide_preview(self) -> None:
        """No-op retained for ChatView tool-grouping compatibility.

        Collapsed tool rows no longer carry a separate preview label: the
        args preview is the target column in the summary row and the full
        text lives in the detail surface, so grouping needs no extra step.
        """
        return


def _describe_result(result: str, is_error: bool) -> str:
    """Right-hand meta text for a finished call.

    Only real data: a failed call gets ``Failed`` plus the first line of
    the error (G9 wants the reason next to the operation, not only a
    colour), a successful call reports line/char counts so the row shows
    output size without expanding.
    """
    if is_error:
        first = result.strip().splitlines()[0] if result.strip() else ""
        return f"Failed — {_trunc(first, 48)}" if first else "Failed"
    lines = len(result.splitlines()) if result.strip() else 0
    if not lines:
        return "Done"
    return f"Done — {lines} line{'s' if lines != 1 else ''}"


# ---------------------------------------------------------------------------
# Tool batch widget — groups N consecutive calls to the same tool
# ---------------------------------------------------------------------------


class ToolBatchWidget(QFrame):
    """Groups consecutive calls to the same tool.

    Same one-row G4 shape as :class:`ToolCallWidget`; the right-hand
    column carries the live count so a batched run reads without being
    expanded. ``add_call`` / ``set_result_for_call`` keep their
    ordering and batching semantics — only the presentation changed.
    """

    def __init__(self, tool_name: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("message_tool")
        self.setMinimumHeight(_TOOL_ROW_MIN_HEIGHT)
        self._tool_name = tool_name
        self._count = 0
        self._expanded = False
        self._first_args: str = ""
        self._tool_call_ids: list[str] = []
        self._results: dict[str, str] = {}
        self._errors: dict[str, str] = {}
        self._entry_labels: list[QLabel] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 5, 6, 5)
        layout.setSpacing(2)

        layout.addLayout(self._build_header(tool_name))
        self._detail_holder = QWidget()
        self._detail_holder.setVisible(False)
        layout.addWidget(self._detail_holder)

        bind_theme(self, self._apply_styles)

    def _apply_styles(self, _tokens: object = None) -> None:
        """Refresh row QSS plus every child label colour on theme change.

        Mirrors :meth:`ToolCallWidget._apply_styles` so a theme switch
        updates the tool name, the elided target, the count and the
        status together.
        """
        self.setStyleSheet(get_tool_row_style())
        if getattr(self, "_name_label", None) is not None:
            self._name_label.setStyleSheet(get_tool_row_name_style())
        if getattr(self, "_summary_label", None) is not None:
            self._summary_label.setStyleSheet(get_tool_row_target_style())
        tool_colors = _row_status_colors()
        if getattr(self, "_status_icon", None) is not None:
            self._status_icon.setStyleSheet(_status_style(tool_colors, self._state))
        if getattr(self, "_status_label", None) is not None:
            self._status_label.setStyleSheet(_status_style(tool_colors, self._state))
        # Per-call detail entries.
        for entry in getattr(self, "_entry_labels", []):
            entry.setStyleSheet(get_tool_row_target_style())

    def _build_header(self, tool_name: str) -> QHBoxLayout:
        """Build the one-line batch row: chevron, status, name, count."""
        display_name = _strip_mcp_prefix(tool_name)
        self._state = "running"

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)

        self._toggle_btn = QToolButton()
        self._toggle_btn.setObjectName("collapse_button")
        self._toggle_btn.setText("▶")
        self._toggle_btn.setFixedSize(14, 14)
        self._toggle_btn.setToolTip("Show every call in this batch")
        self._toggle_btn.setAccessibleName(f"Expand {display_name} batch")
        self._toggle_btn.clicked.connect(self._toggle)
        header_layout.addWidget(self._toggle_btn)

        self._status_icon = QLabel(STATE_RUNNING[0])
        self._status_icon.setFixedWidth(14)
        self._status_icon.setAccessibleName("Batch status")
        header_layout.addWidget(self._status_icon)

        self._name_label = QLabel(display_name)
        self._name_label.setTextFormat(Qt.TextFormat.PlainText)
        self._name_label.setStyleSheet(get_tool_row_name_style())
        header_layout.addWidget(self._name_label)

        self._summary_label = _ElidedLabel()
        self._summary_label.setStyleSheet(get_tool_row_target_style())
        header_layout.addWidget(self._summary_label, 1)

        self._status_label = QLabel("0 calls")
        self._status_label.setStyleSheet(get_tool_row_meta_style())
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header_layout.addWidget(self._status_label)

        self._apply_status_style()
        return header_layout

    def _apply_status_style(self, tool_colors: dict[str, str] | None = None) -> None:
        colors = tool_colors or _row_status_colors()
        if getattr(self, "_status_icon", None) is not None:
            self._status_icon.setStyleSheet(_status_style(colors, self._state))
        if getattr(self, "_status_label", None) is not None:
            self._status_label.setStyleSheet(get_tool_row_meta_style())

    def _build_detail_section(self) -> QWidget:
        """Build the expanded detail surface listing every call."""
        detail = _make_detail_frame()
        self._detail_layout = _make_detail_layout(detail)
        holder_layout = QVBoxLayout(self._detail_holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setSpacing(0)
        holder_layout.addWidget(detail)
        return detail

    def add_call(self, tool_call_id: str, args_text: str = "") -> None:
        """Add another call to this batch.

        Collapsed batches record the call id only — the per-call entry
        label is created on expand (G4: detail content is built on
        demand, never up front).
        """
        self._count += 1
        self._tool_call_ids.append(tool_call_id)
        self._update_status()
        if not getattr(self, "_detail_layout", None):
            return

        if self._count == 1 and args_text.strip():
            self._first_args = args_text
            self._summary_label.set_full_text(_row_target_text(_format_tool_summary(self._tool_name, args_text)))

        summary = _format_tool_summary(self._tool_name, args_text) if args_text else ""
        entry = _ElidedLabel()
        entry.setFull_text(f"#{self._count}: {summary}" if summary else f"#{self._count}")
        entry.setStyleSheet(get_tool_row_target_style())
        self._entry_labels.append(entry)
        self._detail_layout.addWidget(entry)

    def set_args_for_call(self, tool_call_id: str, args_text: str) -> None:
        """Update args for a specific call (used when streaming completes)."""
        idx = -1
        for i, tid in enumerate(self._tool_call_ids):
            if tid == tool_call_id:
                idx = i
                break
        if idx < 0:
            return

        if idx == 0 and not self._first_args:
            self._first_args = args_text
            self._summary_label.set_full_text(_row_target_text(_format_tool_summary(self._tool_name, args_text)))

        if idx < len(self._entry_labels):
            summary = _format_tool_summary(self._tool_name, args_text)
            self._entry_labels[idx].set_full_text(f"#{idx + 1}: {summary}" if summary else f"#{idx + 1}")

    def set_result_for_call(self, tool_call_id: str, result: str, is_error: bool) -> None:
        """Record a result for one call in the batch."""
        if is_error:
            self._errors[tool_call_id] = result
        else:
            self._results[tool_call_id] = result
        self._update_status()

    def _update_status(self) -> None:
        """Right-hand column: call count + progress, then the real outcome.

        The outcome is derived from the recorded results, not from a
        separate state flag, so a batch that already has an error always
        reads "Failed" (icon + word, never colour alone — G9).
        """
        done = len(self._results) + len(self._errors)
        if done >= self._count and self._count:
            self._state = "error" if self._errors else "success"
        else:
            self._state = "running"
        icon, count_text = {
            "running": (STATE_RUNNING[0], f"{done}/{self._count}"),
            "success": (STATE_SUCCESS[0], "All done"),
            "error": (STATE_ERROR[0], "Failed"),
        }[self._state]
        self._status_icon.setText(icon)
        self._status_label.setText(f"{self._count} call{'s' if self._count != 1 else ''} · {count_text}")
        self._apply_status_style()

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        if self._expanded and not getattr(self, "_detail_layout", None):
            self._build_detail_section()
            # Calls that arrived while collapsed were never rendered; add
            # their entries now so expanding shows the whole batch.
            self._render_entries()
        self._detail_holder.setVisible(self._expanded)
        self._toggle_btn.setText("▼" if self._expanded else "▶")

    def _render_entries(self) -> None:
        """Create one entry label per recorded call (expansion only)."""
        while self._detail_layout.count():
            item = self._detail_layout.takeAt(0)
            widget = item.widget() if item else None
            if widget is not None:
                widget.deleteLater()
        self._entry_labels = []
        for idx, tool_call_id in enumerate(self._tool_call_ids):
            del tool_call_id
            summary = _format_tool_summary(self._tool_name, self._first_args) if idx == 0 and self._first_args else ""
            entry = _ElidedLabel()
            entry.set_full_text(f"#{idx + 1}: {summary}" if summary else f"#{idx + 1}")
            entry.setStyleSheet(get_tool_row_target_style())
            self._entry_labels.append(entry)
            self._detail_layout.addWidget(entry)

    @property
    def tool_name(self) -> str:
        return self._tool_name

    @property
    def count(self) -> int:
        return self._count


# ---------------------------------------------------------------------------
# Tool group widget — collapsible container for runs of tool calls
# ---------------------------------------------------------------------------


class ToolGroupWidget(QFrame):
    """Collapsible group for a consecutive run of tool calls.

    One row like every other tool surface (G2/G4): chevron, status icon,
    group label, and a right-hand count of real completed/error calls.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("message_tool")
        self.setMinimumHeight(_TOOL_ROW_MIN_HEIGHT)
        self._expanded = False
        self._count = 0
        self._done = 0
        self._errors = 0
        self._state = "running"
        self._tool_names: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 5, 6, 5)
        layout.setSpacing(2)

        # Header: chevron, status, label, count.
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)

        self._toggle_btn = QToolButton()
        self._toggle_btn.setObjectName("collapse_button")
        self._toggle_btn.setText("▶")
        self._toggle_btn.setFixedSize(14, 14)
        self._toggle_btn.setToolTip("Show the tool calls in this run")
        self._toggle_btn.setAccessibleName("Expand grouped tool calls")
        self._toggle_btn.clicked.connect(self._toggle)
        header_layout.addWidget(self._toggle_btn)

        self._status_icon = QLabel(STATE_RUNNING[0])
        self._status_icon.setFixedWidth(14)
        self._status_icon.setAccessibleName("Group status")
        header_layout.addWidget(self._status_icon)

        self._label = QLabel("0 tools called")
        self._label.setTextFormat(Qt.TextFormat.PlainText)
        header_layout.addWidget(self._label, 1)

        self._status_label = QLabel("")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header_layout.addWidget(self._status_label)

        layout.addLayout(header_layout)

        # Container for child widgets (hidden by default)
        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(22, 4, 0, 4)
        self._body_layout.setSpacing(2)
        self._body.setVisible(False)
        layout.addWidget(self._body)

        # Subscribe to theme changes so the group label / status
        # label re-paint when the user switches palettes.  Without
        # this the group header keeps its construction-time colour
        # even when child tool widgets have already updated.
        bind_theme(self, self._apply_styles)

    def _apply_styles(self, _tokens: object = None) -> None:
        """Re-apply header QSS from the live tokens on theme change.

        Mirrors the colour-mapping logic used in
        :meth:`_update_status` so a theme switch preserves the
        running / success / error distinction on the status label.
        """
        self.setStyleSheet(get_tool_row_style())
        if getattr(self, "_label", None) is not None:
            self._label.setStyleSheet(get_tool_row_name_style())
        tool_colors = _row_status_colors()
        if getattr(self, "_status_icon", None) is not None:
            self._status_icon.setStyleSheet(_status_style(tool_colors, self._state))
        if getattr(self, "_status_label", None) is not None:
            self._status_label.setStyleSheet(get_tool_row_meta_style())

    def add_widget(self, widget: QWidget, tool_name: str = "") -> None:
        """Add a tool widget into this group."""
        self._count += 1
        self._tool_names.append(tool_name)
        self._body_layout.addWidget(widget)
        self._update_label()
        self._update_status()

    def notify_result(self, is_error: bool = False) -> None:
        """Called when a tool inside this group finishes."""
        self._done += 1
        if is_error:
            self._errors += 1
        self._update_status()

    def _update_label(self) -> None:
        self._label.setText(_format_tool_group_label(self._tool_names))

    def _update_status(self) -> None:
        if self._done >= self._count:
            icon, text = (STATE_ERROR[0], "Failed") if self._errors else (STATE_SUCCESS[0], "All done")
            self._state = "error" if self._errors else "success"
        else:
            icon, text = STATE_RUNNING[0], f"{self._done}/{self._count}"
            self._state = "running"
        self._status_icon.setText(icon)
        self._status_label.setText(text)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._toggle_btn.setText("▼" if self._expanded else "▶")

    @property
    def count(self) -> int:
        return self._count


# ---------------------------------------------------------------------------
# Python syntax highlighter (used by ToolApprovalWidget)
# ---------------------------------------------------------------------------


class _PythonHighlighter(QSyntaxHighlighter):
    """Minimal VS Code-dark-style Python syntax highlighter."""

    _RULES: ClassVar[list] = []  # built once in __init_subclass__ — see below

    def __init__(self, parent=None):
        super().__init__(parent)
        if not _PythonHighlighter._RULES:
            _PythonHighlighter._RULES = self._build_rules()

    @staticmethod
    def _fmt(color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
        f = QTextCharFormat()
        f.setForeground(QColor(color))
        if bold:
            f.setFontWeight(QFont.Weight.Bold)
        if italic:
            f.setFontItalic(True)
        return f

    @staticmethod
    def _build_rules():
        rules = []
        kw_fmt = _PythonHighlighter._fmt("#c586c0", bold=True)
        for kw in (
            "and",
            "as",
            "assert",
            "async",
            "await",
            "break",
            "class",
            "continue",
            "def",
            "del",
            "elif",
            "else",
            "except",
            "finally",
            "for",
            "from",
            "global",
            "if",
            "import",
            "in",
            "is",
            "lambda",
            "nonlocal",
            "not",
            "or",
            "pass",
            "raise",
            "return",
            "try",
            "while",
            "with",
            "yield",
        ):
            rules.append((_re.compile(rf"\b{kw}\b"), kw_fmt))
        # Built-ins
        bi_fmt = _PythonHighlighter._fmt("#dcdcaa")
        for bi in (
            "print",
            "len",
            "range",
            "int",
            "str",
            "bytes",
            "list",
            "dict",
            "set",
            "tuple",
            "hex",
            "ord",
            "chr",
            "type",
            "isinstance",
            "enumerate",
            "zip",
            "map",
            "filter",
            "sorted",
            "open",
            "True",
            "False",
            "None",
        ):
            rules.append((_re.compile(rf"\b{bi}\b"), bi_fmt))
        # Numbers
        rules.append((_re.compile(r"\b0[xX][0-9a-fA-F]+\b"), _PythonHighlighter._fmt("#b5cea8")))
        rules.append((_re.compile(r"\b\d+\.?\d*\b"), _PythonHighlighter._fmt("#b5cea8")))
        # Strings (single/double, including f/r/b prefixes)
        str_fmt = _PythonHighlighter._fmt("#ce9178")
        rules.append((_re.compile(r'[brfu]?""".*?"""', _re.DOTALL), str_fmt))
        rules.append((_re.compile(r"[brfu]?'''.*?'''", _re.DOTALL), str_fmt))
        rules.append((_re.compile(r'[brfu]?"[^"\n]*"'), str_fmt))
        rules.append((_re.compile(r"[brfu]?'[^'\n]*'"), str_fmt))
        # Comments
        rules.append((_re.compile(r"#[^\n]*"), _PythonHighlighter._fmt("#6a9955", italic=True)))
        # Decorators
        rules.append((_re.compile(r"@\w+"), _PythonHighlighter._fmt("#dcdcaa")))
        # self
        rules.append((_re.compile(r"\bself\b"), _PythonHighlighter._fmt("#9cdcfe", italic=True)))
        return rules

    def highlightBlock(self, text: str) -> None:
        for pattern, fmt in _PythonHighlighter._RULES:
            for m in pattern.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), fmt)


class ToolApprovalWidget(QFrame):
    """Script/tool approval request (GUI contract G4).

    Structure, in order: header (real operation + ``Approval required`` +
    state ``Paused``) -> description/target -> READ-ONLY code preview ->
    explanation -> action strip, bottom-right, **Deny left, Allow once
    right**.

    Two invariants the gate depends on:

    * ``Always Allow`` never appears for ``execute_python``. The widget is
      the only path to the agent's ``allow_all`` decision, and the loop
      reads that as "skip the prompt for every later script this
      session" (``AgentLoop._always_allow_scripts``). The contract
      forbids an always-allow affordance for scripts, so the button is
      hidden for exactly that tool and for nothing else.
    * The buttons disable themselves before the decision signal fires
      (``_disable_buttons`` first), so a double click cannot submit two
      decisions into the approval queue.
    """

    approved = Signal(str, str)  # (tool_call_id, "allow"/"allow_all"/"deny")

    def __init__(
        self,
        tool_call_id: str,
        tool_name: str,
        args_text: str,
        description: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("message_question")
        self.setStyleSheet(get_tool_approval_frame_style())
        self._tool_call_id = tool_call_id
        self._tool_name = tool_name
        #: Gate: no always-allow for scripts (see class docstring).
        self._is_script = tool_name == constants.EXECUTE_PYTHON_TOOL_NAME
        self._decided = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)

        layout.addLayout(self._build_header())

        # ``description`` is the human-readable summary of what the tool
        # will do (e.g. "rename sub_1000 → process_data"). The agent
        # loop builds it via ``_describe_tool_call`` for mutating tools
        # that have no ``code`` field; rendering it prominently lets the
        # user decide whether to approve without having to decode the
        # raw JSON args below.
        if description and description.strip():
            desc_label = QLabel(description.strip())
            desc_label.setWordWrap(True)
            desc_label.setStyleSheet(get_tool_approval_body_style())
            layout.addWidget(desc_label)

        code = self._extract_code(args_text)
        code_lines = code.strip().splitlines() if code.strip() else []
        self._code = code

        if code.strip():
            # Only label the preview "Python script" when there is code to
            # show. For mutating tools without a ``code`` field the
            # description above already says what will happen.
            self._info = QLabel(f"Script — {len(code_lines)} line{'s' if len(code_lines) != 1 else ''}")
            self._info.setStyleSheet(get_tool_row_meta_style())
            layout.addWidget(self._info)

        editor = self._build_code_editor(code, code_lines)
        if editor is not None:
            layout.addWidget(editor)

        layout.addWidget(self._build_explanation())
        layout.addLayout(self._build_approval_buttons())

        # Subscribe to theme changes so the frame border, header,
        # info label, code editor, and allow/deny buttons all repaint
        # when the user switches palettes.  Disabled button visuals are
        # also refreshed, but we have to be careful: ``_apply_styles``
        # must NOT promote a disabled button back to its active style.
        # See the runtime state check below.
        bind_theme(self, self._apply_styles)

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    def _build_header(self) -> QHBoxLayout:
        """Operation + 'Approval required' left, state 'Paused' right."""
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        self._header = QLabel(_build_approval_header(self._tool_name))
        self._header.setTextFormat(Qt.TextFormat.PlainText)
        self._header.setStyleSheet(get_tool_approval_header_style())
        header.addWidget(self._header, 1)

        self._state_label = QLabel(f"{STATE_APPROVAL[0]} {STATE_APPROVAL[1]}")
        self._state_label.setStyleSheet(get_tool_approval_state_style())
        self._state_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self._state_label)
        return header

    def _build_explanation(self) -> QWidget:
        """Why the gate exists, in plain words, next to the buttons."""
        explanation = QLabel(
            "Review the operation above. Deny stops the tool; Allow once runs it exactly "
            "one time. The agent stays paused until you decide."
        )
        explanation.setWordWrap(True)
        explanation.setStyleSheet(get_tool_approval_body_style())
        return explanation

    def _apply_styles(self, _tokens: object = None) -> None:
        """Refresh every visual element of the approval widget.

        Disabled buttons keep their disabled-state visual even after
        a theme change: ``_disable_buttons`` flips ``enabled`` to
        False and replaces the text + style with the muted disabled
        style, and ``_apply_styles`` must not overwrite that.  We
        read ``isEnabled()`` before re-applying each button's
        stylesheet so a button that was clicked (now disabled with
        "Allowed" / "Denied" text) stays in the disabled style.
        """
        try:
            ThemeManager.instance().tokens()
        except Exception:
            return
        # Frame / header.
        self.setStyleSheet(get_tool_approval_frame_style())
        if getattr(self, "_header", None) is not None:
            self._header.setStyleSheet(get_tool_approval_header_style())
        if getattr(self, "_state_label", None) is not None:
            self._state_label.setStyleSheet(get_tool_approval_state_style())
        # Info label: ``muted_text`` (AA on both window and alt_base).
        if getattr(self, "_info", None) is not None:
            self._info.setStyleSheet(get_tool_row_meta_style())
        # Code editor.
        if getattr(self, "_code_edit", None) is not None:
            self._code_edit.setStyleSheet(get_tool_approval_code_editor_style())
        # Buttons — preserve disabled state if the user already decided.
        if getattr(self, "_allow_btn", None) is not None:
            if self._allow_btn.isEnabled():
                self._allow_btn.setStyleSheet(get_tool_approval_allow_btn_style())
            else:
                self._allow_btn.setStyleSheet(get_tool_approval_disabled_btn_style())
        if getattr(self, "_always_btn", None) is not None:
            if self._always_btn.isEnabled():
                self._always_btn.setStyleSheet(get_tool_approval_always_btn_style())
            else:
                self._always_btn.setStyleSheet(get_tool_approval_disabled_btn_style())
        if getattr(self, "_deny_btn", None) is not None:
            if self._deny_btn.isEnabled():
                self._deny_btn.setStyleSheet(get_tool_approval_deny_btn_style())
            else:
                self._deny_btn.setStyleSheet(get_tool_approval_disabled_btn_style())

    def shutdown(self) -> None:
        """Detach the theme subscription so teardown does not warn."""
        disconnect_theme(self)

    @staticmethod
    def _extract_code(args_text: str) -> str:
        """Extract the code/script value from JSON args, falling back to raw text."""
        try:
            args = json.loads(args_text) if args_text.strip() else {}
            return args.get("code", args.get("script", ""))
        except (json.JSONDecodeError, TypeError, AttributeError):
            return args_text

    def _build_code_editor(self, code: str, lines: list) -> QPlainTextEdit | None:
        """Build the READ-ONLY code preview, or None if there is no code.

        Full script is kept: the height cap only adds a scroll region, it
        never truncates the text, and the font is never shrunk (G4).
        """
        if not code.strip():
            return None
        self._code_edit = QPlainTextEdit()
        self._code_edit.setReadOnly(True)
        self._code_edit.setPlainText(code)
        self._code_edit.setStyleSheet(get_tool_approval_code_editor_style())
        self._code_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        visible_lines = min(len(lines), 15)
        line_height = self._code_edit.fontMetrics().lineSpacing()
        self._code_edit.setFixedHeight(line_height * visible_lines + 16)
        self._highlighter = _PythonHighlighter(self._code_edit.document())
        return self._code_edit

    def _build_approval_buttons(self) -> QHBoxLayout:
        """Action strip, bottom-right: Deny left, Allow once right (G4)."""
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        self._deny_btn = QToolButton()
        self._deny_btn.setText("Deny")
        self._deny_btn.setToolTip("Refuse this operation")
        self._deny_btn.setAccessibleName("Deny this operation")
        self._deny_btn.setStyleSheet(get_tool_approval_deny_btn_style())
        self._deny_btn.clicked.connect(self._on_deny)
        btn_layout.addWidget(self._deny_btn)

        self._always_btn = QToolButton()
        self._always_btn.setText("Always Allow")
        self._always_btn.setToolTip("Allow this tool without asking again this session")
        self._always_btn.setAccessibleName("Always allow this tool this session")
        self._always_btn.setStyleSheet(get_tool_approval_always_btn_style())
        self._always_btn.clicked.connect(self._on_always_allow)
        btn_layout.addWidget(self._always_btn)

        self._allow_btn = QToolButton()
        self._allow_btn.setText("Allow once")
        self._allow_btn.setToolTip("Run this operation exactly one time")
        self._allow_btn.setAccessibleName("Allow this operation once")
        self._allow_btn.setStyleSheet(get_tool_approval_allow_btn_style())
        self._allow_btn.clicked.connect(self._on_allow)
        btn_layout.addWidget(self._allow_btn)

        # The agent skips every later prompt once it sees 'allow_all', so a
        # script must never be able to set that flag (G4 / brief gate).
        self._always_btn.setVisible(not self._is_script)

        btn_layout.addStretch()
        return btn_layout

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------

    def _disable_buttons(self) -> None:
        """Close the gate before emitting, so a double click cannot submit twice."""
        self._decided = True
        for btn in (self._allow_btn, self._always_btn, self._deny_btn):
            btn.setEnabled(False)

    def _on_allow(self):
        if self._decided:
            return
        self._disable_buttons()
        self._allow_btn.setText("Allowed")
        self._allow_btn.setStyleSheet(get_tool_approval_disabled_btn_style())
        self._set_state_text("Allowed")
        self.approved.emit(self._tool_call_id, "allow")

    def _on_always_allow(self):
        if self._decided:
            return
        self._disable_buttons()
        self._always_btn.setText("Always Allowed")
        self._always_btn.setStyleSheet(get_tool_approval_disabled_btn_style())
        self._set_state_text("Allowed")
        self.approved.emit(self._tool_call_id, "allow_all")

    def _on_deny(self):
        if self._decided:
            return
        self._disable_buttons()
        self._deny_btn.setText("Denied")
        self._deny_btn.setStyleSheet(get_tool_approval_disabled_btn_style())
        self._set_state_text("Denied")
        self.approved.emit(self._tool_call_id, "deny")

    def _set_state_text(self, text: str) -> None:
        """Swap the header state from 'Paused' to the decision taken."""
        if getattr(self, "_state_label", None) is not None:
            self._state_label.setText(text)


# ---------------------------------------------------------------------------
# Unified execute_python lifecycle widget
# ---------------------------------------------------------------------------


class ExecutePythonWidget(QFrame):
    """Unified lifecycle widget for the ``execute_python`` tool.

    Renders code, an optional docs-review status line, approval buttons,
    and the execution result — all visible by default. State is inferred
    from the events received: the widget shows buttons only when
    ``show_approval_buttons()`` is called (driven by TOOL_APPROVAL_REQUEST)
    and shows the result after ``set_result()``. There is no
    collapse/expand toggle: every section is visible whenever its state
    is active, and the result lives in a scrollable read-only editor so
    a long script output never dominates the card.
    """

    approved = Signal(str, str)  # (tool_call_id, "allow"/"allow_all"/"deny")

    def __init__(self, tool_call_id: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("message_tool")
        self.setMinimumHeight(_TOOL_ROW_MIN_HEIGHT)
        self._tool_call_id = tool_call_id
        self._code = ""
        self._result_text = ""
        self._buttons_visible = False
        self._status_visible = False
        self._status_text = ""
        self._is_error = False
        self._blocked = False
        self._decided = False
        self._state = "running"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 5, 6, 5)
        layout.setSpacing(2)

        layout.addLayout(self._build_header())
        layout.addWidget(self._build_code_section())
        self._status_line = self._build_status_line()
        layout.addWidget(self._status_line)
        layout.addLayout(self._build_approval_buttons())
        layout.addWidget(self._build_result_block())

        bind_theme(self, self._apply_styles)

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    def _apply_styles(self, _tokens: object = None) -> None:
        """Re-apply row QSS and refresh child label colours on theme change."""
        self.setStyleSheet(get_tool_row_style())
        tool_colors = _row_status_colors()
        if getattr(self, "_name_label", None) is not None:
            self._name_label.setStyleSheet(get_tool_row_name_style())
        status_icon = getattr(self, "_status_icon", None)
        if status_icon is not None and status_icon.text():
            status_icon.setStyleSheet(_status_style(tool_colors, self._state))
        result_edit = getattr(self, "_result_edit", None)
        if result_edit is not None:
            result_color = tool_colors["status_error"] if getattr(self, "_is_error", False) else None
            result_edit.setStyleSheet(get_tool_result_editor_style(result_color))
        # Re-paint the docs-review status label + detail against the live palette.
        if getattr(self, "_status_label", None) is not None and getattr(self, "_status_visible", False):
            self._status_label.setStyleSheet(f"color: {tool_colors['preview']}; font-size: inherit;")
        if getattr(self, "_status_detail", None) is not None and self._status_detail.isVisible():
            self._status_detail.setStyleSheet(f"color: {tool_colors['status_error']}; font-size: inherit;")

    def shutdown(self) -> None:
        """Detach the theme subscription so teardown does not warn."""
        disconnect_theme(self)

    def _build_header(self) -> QHBoxLayout:
        """Same one-row summary shape as ToolCallWidget (G2/G4).

        The script itself is NOT in the summary row — a script can be
        hundreds of lines. The row carries the tool name plus the real
        line count, and the full (never truncated) script lives in the
        read-only editor below it.
        """
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        # Indent aligned with the toggle button column in ToolCallWidget
        # so the name sits at the same x.
        indent = QLabel("")
        indent.setFixedWidth(14)
        header.addWidget(indent)

        self._status_icon = QLabel(STATE_RUNNING[0])
        self._status_icon.setFixedWidth(14)
        self._status_icon.setAccessibleName("Script status")
        header.addWidget(self._status_icon)

        self._name_label = QLabel(_strip_mcp_prefix(constants.EXECUTE_PYTHON_TOOL_NAME))
        self._name_label.setTextFormat(Qt.TextFormat.PlainText)
        self._name_label.setStyleSheet(get_tool_row_name_style())
        header.addWidget(self._name_label)

        self._script_meta = _ElidedLabel()
        self._script_meta.setStyleSheet(get_tool_row_target_style())
        header.addWidget(self._script_meta, 1)

        self._status_label_right = QLabel(STATE_RUNNING[1])
        self._status_label_right.setStyleSheet(get_tool_row_meta_style())
        self._status_label_right.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self._status_label_right)

        return header

    def _set_state(self, state: str, text: str | None = None) -> None:
        """Paint the row icon + right-hand word for one state (G9)."""
        self._state = state
        icon, default_text = {
            "running": STATE_RUNNING,
            "success": STATE_SUCCESS,
            "error": STATE_ERROR,
            "discarded": STATE_DISCARDED,
        }.get(state, STATE_RUNNING)
        self._status_icon.setText(icon)
        self._status_label_right.setText(text or default_text)
        self._apply_styles()

    def _build_code_section(self) -> QWidget:
        section = QWidget()
        layout = QVBoxLayout(section)
        layout.setContentsMargins(22, 4, 0, 4)
        layout.setSpacing(2)

        self._code_info_label = QLabel("")
        self._code_info_label.setStyleSheet(get_tool_row_meta_style())
        self._code_info_label.setVisible(False)
        layout.addWidget(self._code_info_label)

        self._code_edit = QPlainTextEdit()
        self._code_edit.setReadOnly(True)
        self._code_edit.setStyleSheet(get_tool_approval_code_editor_style())
        self._code_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._code_edit.setVisible(False)
        layout.addWidget(self._code_edit)
        self._code_highlighter = _PythonHighlighter(self._code_edit.document())

        # Visible only when set_code() provides code (handled there).
        section.setVisible(False)
        return section

    def _build_status_line(self) -> QWidget:
        """Build the docs-review status row (header + always-on detail)."""
        tool_colors = get_tool_colors()
        wrapper = QWidget()
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(0)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(4)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet(f"color: {tool_colors['preview']}; font-size: inherit;")
        self._status_label.setVisible(False)
        header_row.addWidget(self._status_label, 1)
        wrapper_layout.addLayout(header_row)

        self._status_detail = QLabel("")
        self._status_detail.setWordWrap(True)
        self._status_detail.setTextInteractionFlags(
            Qt.TextInteractionFlag(
                Qt.TextInteractionFlag.TextSelectableByMouse.value
                | Qt.TextInteractionFlag.TextSelectableByKeyboard.value
            )
        )
        self._status_detail.setStyleSheet(f"color: {tool_colors['status_error']}; font-size: inherit;")
        self._status_detail.setVisible(False)
        wrapper_layout.addWidget(self._status_detail)

        wrapper.setVisible(False)
        return wrapper

    def _build_approval_buttons(self) -> QHBoxLayout:
        """Action strip, bottom-right: Deny left, Allow once right (G4).

        ``Always Allow`` is hidden here unconditionally: this widget is
        only ever used for ``execute_python``, and the loop reads an
        ``allow_all`` decision as "skip the prompt for every later
        script", which the contract forbids for scripts.
        """
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        self._deny_btn = QToolButton()
        self._deny_btn.setText("Deny")
        self._deny_btn.setToolTip("Refuse this script")
        self._deny_btn.setAccessibleName("Deny this script")
        self._deny_btn.setStyleSheet(get_tool_approval_deny_btn_style())
        self._deny_btn.clicked.connect(self._on_deny)
        btn_layout.addWidget(self._deny_btn)

        self._always_btn = QToolButton()
        self._always_btn.setText("Always Allow")
        self._always_btn.setToolTip("Allow this tool without asking again this session")
        self._always_btn.setAccessibleName("Always allow this tool this session")
        self._always_btn.setStyleSheet(get_tool_approval_always_btn_style())
        self._always_btn.clicked.connect(self._on_always_allow)
        btn_layout.addWidget(self._always_btn)
        # Gate: no always-allow for scripts.
        self._always_btn.setVisible(False)

        self._allow_btn = QToolButton()
        self._allow_btn.setText("Allow once")
        self._allow_btn.setToolTip("Run this script exactly one time")
        self._allow_btn.setAccessibleName("Allow this script once")
        self._allow_btn.setStyleSheet(get_tool_approval_allow_btn_style())
        self._allow_btn.clicked.connect(self._on_allow)
        btn_layout.addWidget(self._allow_btn)

        btn_layout.addStretch()

        # Wrap in a container so we can toggle visibility as a unit.
        self._buttons_container = QWidget()
        self._buttons_container.setLayout(btn_layout)
        self._buttons_container.setVisible(False)
        # Return a layout-like wrapper: embed the container in a layout.
        wrapper = QHBoxLayout()
        wrapper.setContentsMargins(0, 0, 0, 0)
        wrapper.addWidget(self._buttons_container)
        return wrapper

    def _build_result_block(self) -> QWidget:
        """Script output on the G4 inset code surface, with Raw/Copy right."""
        self._result_block = _make_detail_frame()
        layout = _make_detail_layout(self._result_block)

        self._result_header_label = QLabel("Output")
        self._result_header_label.setTextFormat(Qt.TextFormat.PlainText)
        self._result_header_label.setStyleSheet(get_tool_row_name_style())
        self._result_header_label.setVisible(False)
        layout.addWidget(self._result_header_label)

        self._result_edit = QPlainTextEdit()
        self._result_edit.setReadOnly(True)
        self._result_edit.setStyleSheet(get_tool_result_editor_style())
        self._result_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self._result_edit)

        actions, _row = _make_result_actions(
            "",
            on_raw=self._on_raw,
            on_copy=self._on_copy,
        )
        self._raw_btn_row = actions
        layout.addWidget(actions)

        self._result_block.setVisible(False)
        return self._result_block

    def _on_raw(self) -> None:
        """Show the untruncated script output."""
        self._result_edit.setPlainText(self._result_text)
        self._fit_result_height()

    def _on_copy(self) -> None:
        """Copy the script output to the clipboard."""
        _copy_to_clipboard(self._result_text)

    def _fit_result_height(self) -> None:
        display = self._result_edit.toPlainText()
        lines = display.splitlines() if display.strip() else []
        visible = max(min(len(lines), _RESULT_MAX_LINES), 1)
        line_height = self._result_edit.fontMetrics().lineSpacing()
        self._result_edit.setFixedHeight(line_height * visible + 16)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_code(self, code: str) -> None:
        self._code = code
        self._code_edit.setPlainText(code)
        lines = code.strip().splitlines() if code.strip() else []
        if lines:
            count = f"{len(lines)} line{'s' if len(lines) != 1 else ''}"
            self._code_info_label.setText(f"Script — {count}")
            self._code_info_label.setVisible(True)
            # Real line count in the summary row (G4: only real data).
            self._script_meta.set_full_text(f"{count} of Python")
            visible = min(len(lines), 15)
            line_height = self._code_edit.fontMetrics().lineSpacing()
            self._code_edit.setFixedHeight(line_height * visible + 16)
            self._code_edit.setVisible(True)
            self._code_section().setVisible(True)
        else:
            self._code_info_label.setVisible(False)
            self._code_edit.setVisible(False)
            self._script_meta.set_full_text("")
            self._code_section().setVisible(False)

    def append_args_delta(self, delta: str) -> None:
        """Accumulate streaming args (TOOL_CALL_ARGS_DELTA).

        ExecutePythonWidget renders code only after ``set_arguments()`` parses
        the complete JSON on TOOL_CALL_DONE, so deltas are a no-op here — but
        ChatView calls this unconditionally for every tool widget.
        """
        # No-op: code is extracted and rendered in set_arguments() on TOOL_CALL_DONE.

    def set_arguments(self, args_text: str) -> None:
        """Parse JSON args and extract the code (compat with ToolCallWidget API)."""
        try:
            args = json.loads(args_text) if args_text.strip() else {}
            code = args.get("code", args.get("script", "")) or args_text
        except (json.JSONDecodeError, TypeError, AttributeError):
            code = args_text
        self.set_code(code)

    def set_docs_gate_status(
        self,
        state: str,
        reasons: tuple[str, ...] = (),
        summary: str = "",
    ) -> None:
        """Render the docs-review status line (on_error semantics unchanged).

        ``blocked`` still hard-hides the approval buttons, ``failed``
        still leaves them up for a manual decision, and the ``✗`` prefix
        in ``_status_text`` is what :meth:`show_approval_buttons` keys
        off — those three rules are the docs-review contract and are
        unchanged here.
        """
        self._status_visible = True
        tool_colors = _row_status_colors()
        if state == "running":
            self._status_text = "Reviewing script..."
            if reasons:
                self._status_text += f" (complex: {', '.join(reasons[:3])})"
            self._status_label.setStyleSheet(f"color: {tool_colors['preview']}; font-size: inherit;")
            self._status_detail.setVisible(False)
        elif state == "approved":
            self._status_text = "✓ Docs review passed"
            self._status_label.setStyleSheet(
                f"color: {tool_colors['status_success']}; font-size: inherit; opacity: 0.7;"
            )
            self._status_detail.setVisible(False)
        elif state == "blocked":
            self._blocked = True
            self._status_text = "✗ Docs review blocked"
            self._status_label.setStyleSheet(
                f"color: {tool_colors['status_error']}; font-weight: bold; font-size: inherit;"
            )
            self._status_detail.setText(summary or "The reviewer flagged the script.")
            self._status_detail.setStyleSheet(f"color: {tool_colors['status_error']}; font-size: inherit;")
            self._status_detail.setVisible(True)
            self._buttons_visible = False
            self._buttons_container.setVisible(False)
        elif state == "failed":
            self._status_text = f"⚠ Docs review error — review manually. ({summary})"
            self._status_label.setStyleSheet(f"color: {tool_colors['status_spinner']}; font-size: inherit;")
            self._status_detail.setVisible(False)
            # FAILED keeps buttons visible so the user can still approve.
        else:
            self._status_text = ""
            self._status_visible = False
            self._status_detail.setVisible(False)

        self._status_label.setText(self._status_text)
        self._status_line.setVisible(self._status_visible)

    def show_approval_buttons(self) -> None:
        if not self._status_visible or not self._status_text.startswith("✗"):
            # Keep buttons hidden if currently hard-blocked by docs gate.
            self._buttons_visible = True
            self._buttons_container.setVisible(True)
            # G4/G9: the row says the agent is waiting on a human.
            self._set_state("running", text=f"{STATE_APPROVAL[0]} {STATE_APPROVAL[1]}")

    def mark_done(self) -> None:
        """Mark the call complete (used by history restore). Safe to call
        multiple times."""
        if self._state == "running":
            self._set_state("success")

    def mark_discarded(self, reason: str = "discarded") -> None:
        """Stop the lifecycle and set a neutral terminal glyph without result.

        Same terminal semantics as ``ToolCallWidget.mark_discarded``:
        the call was started but dropped (truncated args, degenerated
        JSON) instead of executed.
        """
        self._set_state("discarded", text=f"Skipped — {reason}")
        # Hide approval buttons — no result will arrive.
        self._buttons_visible = False
        if hasattr(self, "_buttons_container"):
            self._buttons_container.setVisible(False)
        # Show a short status line instead of a result.
        self._result_text = f"({reason})"
        self._result_header_label.setVisible(True)
        self._result_edit.setPlainText(self._result_text)
        self._result_block.setVisible(True)

    def hide_preview(self) -> None:
        """No-op retained for ChatView tool-grouping compatibility.

        ToolCallWidget / ToolBatchWidget collapse their content when nested
        inside a ToolGroupWidget; ExecutePythonWidget has no collapse state,
        so grouping leaves it fully visible.
        """
        return

    def set_result(self, result: str, is_error: bool = False) -> None:
        tool_colors = _row_status_colors()
        self._is_error = is_error
        self._result_text = result
        # When the docs gate blocked the script, the loop emits a TOOL_RESULT
        # carrying the reviewer summary as an error. That summary already
        # lives in the status detail line — rendering a separate result block
        # would duplicate it. Skip.
        if self._blocked:
            self._buttons_visible = False
            self._buttons_container.setVisible(False)
            return
        display = result[:_MAX_RESULT_DISPLAY] + "\n... (truncated)" if len(result) > _MAX_RESULT_DISPLAY else result
        self._result_edit.setPlainText(display)
        self._fit_result_height()
        self._result_header_label.setVisible(True)
        self._result_block.setVisible(True)
        # Hide approval buttons after result arrives.
        self._buttons_visible = False
        self._buttons_container.setVisible(False)

        if is_error:
            self._result_edit.setStyleSheet(get_tool_result_editor_style(tool_colors["status_error"]))
            self._set_state("error", text=_describe_result(result, True))
        else:
            self._result_edit.setStyleSheet(get_tool_result_editor_style())
            self._set_state("success", text=_describe_result(result, False))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _code_section(self) -> QWidget:
        # The code section is the 2nd widget in the main layout.
        return self.layout().itemAt(1).widget()

    def _disable_buttons(self) -> None:
        self._decided = True
        self._allow_btn.setEnabled(False)
        self._always_btn.setEnabled(False)
        self._deny_btn.setEnabled(False)

    def _on_allow(self) -> None:
        if self._decided:
            return
        self._disable_buttons()
        self._allow_btn.setText("Allowed")
        self._allow_btn.setStyleSheet(get_tool_approval_disabled_btn_style())
        self._set_state("success", text="Allowed")
        self.approved.emit(self._tool_call_id, "allow")

    def _on_always_allow(self) -> None:
        if self._decided:
            return
        self._disable_buttons()
        self._always_btn.setText("Always Allowed")
        self._always_btn.setStyleSheet(get_tool_approval_disabled_btn_style())
        self._set_state("success", text="Allowed")
        self.approved.emit(self._tool_call_id, "allow_all")

    def _on_deny(self) -> None:
        if self._decided:
            return
        self._disable_buttons()
        self._deny_btn.setText("Denied")
        self._deny_btn.setStyleSheet(get_tool_approval_disabled_btn_style())
        self._set_state("error", text="Denied")
        self.approved.emit(self._tool_call_id, "deny")
