"""Context bar: shows the current function / address and the Changes action."""

from __future__ import annotations

import importlib

from ..core.host import (
    get_current_address,
    is_ida,
)
from ..core.logging import log_debug
from .qt_compat import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    Qt,
    QTimer,
    QWidget,
)

if is_ida():
    try:
        ida_funcs = importlib.import_module("ida_funcs")
        ida_name = importlib.import_module("ida_name")
    except ImportError:
        ida_funcs = ida_name = None  # type: ignore[assignment]
else:
    ida_funcs = ida_name = None  # type: ignore[assignment]


def _function_name_at(ea: int) -> str | None:
    if is_ida() and ida_funcs is not None and ida_name is not None:
        try:
            func = ida_funcs.get_func(ea)
            if func:
                return ida_name.get_name(func.start_ea)
        except Exception:
            return None

    return None


# GUI contract G3: the single breakpoint for chrome compaction — a panel /
# dock narrower than this switches to the narrow presentation.
NARROW_BREAKPOINT_PX = 640


class ContextBar(QFrame):
    """Context row showing current binary context and session info.

    GUI contract G3: ``CONTEXT`` label -> symbol -> address -> actions.
    Model and token usage live in the footer only — showing them here too
    duplicated the same numbers in two places.
    """

    # GUI contract G2: min-height 27 px, padding 4 / 12, gap 9.
    _ROW_HEIGHT = 27
    _ROW_MARGIN_H = 12
    _ROW_MARGIN_V = 4
    _ROW_SPACING = 9

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("context_bar")
        # G2's 27 px is a minimum: a user font override grows the text and
        # a fixed height would clip the row.
        self.setMinimumHeight(self._ROW_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(self._ROW_MARGIN_H, self._ROW_MARGIN_V, self._ROW_MARGIN_H, self._ROW_MARGIN_V)
        layout.setSpacing(self._ROW_SPACING)

        # Primary context (never hidden): symbol then address, matching
        # the contract's label -> symbol -> address order.
        self._context_title = QLabel("CONTEXT")
        self._context_title.setObjectName("context_title")
        layout.addWidget(self._context_title)

        self._function_label = self._make_pair("", "—")
        self._address_label = self._make_pair("ADDR:", "—")

        for label, value in (self._function_label, self._address_label):
            layout.addWidget(label)
            layout.addWidget(value)

        layout.addStretch()

        # G3 context row: label -> symbol -> address -> optional binary
        # metadata -> ``Changes`` action/count.  Changes opens Mutations; it
        # is a real action, not a setting, and carries the live record
        # count.
        self._changes_btn = QPushButton("Changes")
        self._changes_btn.setObjectName("context_changes_btn")
        self._changes_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._changes_btn.setToolTip("Show the mutation log (IDA database edits made by the agent)")
        self._changes_btn.setAccessibleName("Changes — show the mutation log")
        # The panel wires ``clicked`` to its own Mutations handler, so the
        # button's own signal is the whole contract — no re-emit wrapper.
        layout.addWidget(self._changes_btn)
        self._mutation_count = 0
        self._update_changes_label()

        self._stopped = False

        # Auto-update cursor position
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_cursor)
        self._timer.start(2000)

    def apply_theme(self) -> None:
        """Push the token-driven context-row QSS from the live palette."""
        try:
            from .styles import maybe_host_stylesheet
            from .theme.widgets_common import get_context_bar_style

            self.setStyleSheet(maybe_host_stylesheet(get_context_bar_style()))
        except Exception as e:  # never let theming break the context row
            log_debug(f"ContextBar.apply_theme failed: {e}")

    def _make_pair(self, label_text: str, initial: str):
        label = QLabel(label_text)
        label.setObjectName("context_label")
        value = QLabel(initial)
        value.setObjectName("context_value")
        # Make the value selectable so a user can copy an address, a long
        # function name, or a model id straight off the bar.
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label, value

    def stop(self) -> None:
        """Stop the auto-update timer. Call before destruction."""
        self._stopped = True
        try:
            self._timer.stop()
            self._timer.timeout.disconnect(self._update_cursor)
        except (RuntimeError, TypeError) as e:
            log_debug(f"ContextBar.stop: timer already destroyed: {e}")

    def set_address(self, addr: str) -> None:
        self._address_label[1].setText(addr)

    def set_function(self, name: str) -> None:
        """Display the function name, truncating long names with a tooltip.

        C++ mangled / Rust generic names routinely exceed the 27-char
        display slice, so the full name is mirrored into the tooltip —
        otherwise the truncated value is unrecoverable.
        """
        value = self._function_label[1]
        if len(name) < 30:
            value.setText(name)
            value.setToolTip("")
        else:
            value.setText(name[:27] + "...")
            value.setToolTip(name)

    def changes_button(self) -> QPushButton:
        """The row's ``Changes`` action — the panel's Mutations entry point.

        Public accessor: ``panel_core`` wires this button's ``clicked`` to
        the mutation-log handler and must not reach into a private widget.
        """
        return self._changes_btn

    def set_mutation_count(self, count: int) -> None:
        """Show how many reversible mutations this session recorded."""
        self._mutation_count = max(0, int(count))
        self._update_changes_label()

    def mutation_count(self) -> int:
        return self._mutation_count

    def _update_changes_label(self) -> None:
        count = self._mutation_count
        text = "Changes" if count <= 0 else f"Changes · {count}"
        self._changes_btn.setText(text)
        self._changes_btn.setToolTip(
            "Show the mutation log (IDA database edits made by the agent)"
            if count <= 0
            else f"Show {count} recorded database edit{'s' if count != 1 else ''}"
        )
        self._changes_btn.setAccessibleName(f"Changes — show the mutation log ({count} recorded)")

    def _update_cursor(self) -> None:
        if self._stopped:
            return
        try:
            ea = get_current_address()
            if ea is None:
                return
            self.set_address(f"0x{int(ea):x}")
            name = _function_name_at(int(ea))
            self.set_function(name or "\u2014")
        except Exception as e:
            log_debug(f"ContextBar._update_cursor failed: {e}")
