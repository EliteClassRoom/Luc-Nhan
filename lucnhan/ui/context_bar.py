"""Context bar: shows current address, function, model, and token count."""

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

    GUI contract G3: ``CONTEXT`` label -> symbol -> address -> optional
    binary metadata -> actions.  Address / symbol are the primary context
    and are never hidden; the model / token-count metadata is secondary
    and is hidden at the narrow breakpoint (G3) so a narrow dock still
    reads the binary context.
    """

    # GUI contract G2: min-height 27 px, padding 4 / 12, gap 9.
    _ROW_HEIGHT = 27
    _ROW_MARGIN_H = 12
    _ROW_MARGIN_V = 4
    _ROW_SPACING = 9

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("context_bar")
        self.setFixedHeight(self._ROW_HEIGHT)

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

        # Secondary metadata (hides at the narrow breakpoint).
        self._model_label = self._make_pair("MODEL:", "—")
        self._tokens_label = self._make_pair("TOKENS:", "0")
        for pair in (self._model_label, self._tokens_label):
            layout.addWidget(pair[0])
            layout.addWidget(pair[1])
        self._secondary_pairs = (self._model_label, self._tokens_label)

        layout.addStretch()

        self._narrow = False
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

    def set_narrow(self, narrow: bool) -> None:
        """Compact the row for the narrow dock (GUI contract G3).

        Only *secondary metadata* hides — the symbol and the address stay
        visible so the primary context is always readable.  Presentation
        only: no value is reset and no timer restarts.
        """
        if self._narrow == narrow:
            return
        self._narrow = narrow
        for pair in self._secondary_pairs:
            for widget in pair:
                widget.setVisible(not narrow)

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

    def set_model(self, model: str) -> None:
        self._model_label[1].setText(model)

    def set_tokens(self, count: int, context_window: int = 0) -> None:
        if count >= 1000:
            text = f"{count / 1000:.1f}k"
        else:
            text = str(count)
        if context_window > 0:
            pct = min(int(count * 100 / context_window), 100)
            text += f" ({pct}%)"
        self._tokens_label[1].setText(text)

    def tokens_text(self) -> str:
        """The context-token summary as displayed (e.g. ``4.2k (2%)``).

        Public accessor so the footer's token readout never has to reach
        into the label tuple.
        """
        return self._tokens_label[1].text()

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
