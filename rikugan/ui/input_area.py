"""Multi-line input area with Enter/Shift+Enter handling and /skill autocomplete."""

from __future__ import annotations

from ..core.logging import get_logger
from .qt_compat import (
    QColor,
    QFrame,
    QLabel,
    QPalette,
    QPlainTextEdit,
    QSizePolicy,
    Qt,
    QVBoxLayout,
    QWidget,
)
from .styles import build_input_area_stylesheet
from .theme.manager import ThemeManager

logger = get_logger()


def _skill_popup_style() -> str:
    """Inline QSS for the ``_SkillPopup`` — always applied, never gated on host theme.

    The popup is a child of the Rikugan panel, not part of the host UI,
    so it must keep its border + selected-row highlight in *every* theme
    mode (including IDA-Native, where ``host_stylesheet``/``is_host_theme``
    would otherwise swap in a bald fallback and leave the popup as bare
    text). The selected row is both highlighted and bold.
    """
    t = ThemeManager.instance().tokens()
    return (
        f"QFrame#skill_popup {{ background: {t.alt_base}; border: 1px solid {t.mid}; "
        f"border-radius: 4px; padding: 2px; }}"
        f"QLabel {{ color: {t.text}; padding: 3px 8px; }}"
        f'QLabel[selected="true"] {{ background: {t.highlight};'
        f" border-radius: 3px; font-weight: bold; }}"
    )


class _SkillPopup(QFrame):
    """Lightweight autocomplete popup for /skill slugs.

    Uses QLabel items inside a QFrame — avoids QListWidget Shiboken surface.
    NO Signal definitions here — this class is defined during the Shiboken
    bypass window and adding Signal descriptors corrupts Shiboken's internal
    signal registry on Python 3.14 + Shiboken 6.8.2, causing SIGSEGV in
    checkQtSignal() on any later signal operation (e.g. QTimer.singleShot).
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("skill_popup")
        self.setWindowFlags(Qt.WindowType.ToolTip)
        # Inline QSS applied directly (NOT via host_stylesheet) so the
        # popup keeps border + highlight even in IDA-Native mode, where
        # is_host_theme() is True and host_stylesheet would return a bald
        # fallback. Rebuilt by apply_theme() on every themeChanged signal.
        self.setStyleSheet(_skill_popup_style())
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(2, 2, 2, 2)
        self._layout.setSpacing(0)
        self._labels: list[QLabel] = []
        self._slugs: list[str] = []
        self._selected_idx = 0

    def apply_theme(self) -> None:
        """Refresh the popup QSS for the current theme tokens."""
        self.setStyleSheet(_skill_popup_style())

    def set_items(self, slugs: list[str]) -> None:
        """Replace popup contents with filtered slugs."""
        # Clear old labels
        for lbl in self._labels:
            self._layout.removeWidget(lbl)
            lbl.setParent(None)
        self._labels.clear()
        self._slugs = list(slugs)
        self._selected_idx = 0

        for slug in slugs:
            lbl = QLabel(f"/{slug}")
            self._labels.append(lbl)
            self._layout.addWidget(lbl)

        self._update_highlight()
        self.adjustSize()

    def _update_highlight(self) -> None:
        for i, lbl in enumerate(self._labels):
            lbl.setProperty("selected", "true" if i == self._selected_idx else "false")
            lbl.style().unpolish(lbl)
            lbl.style().polish(lbl)

    def move_selection(self, delta: int) -> None:
        if not self._slugs:
            return
        self._selected_idx = (self._selected_idx + delta) % len(self._slugs)
        self._update_highlight()

    def current_slug(self) -> str | None:
        if 0 <= self._selected_idx < len(self._slugs):
            return self._slugs[self._selected_idx]
        return None

    def is_empty(self) -> bool:
        return len(self._slugs) == 0


class InputArea(QPlainTextEdit):
    """Chat input area with keyboard shortcuts.

    - Enter: submit message
    - Shift+Enter: newline
    - Escape: cancel running agent
    - /: skill autocomplete popup

    Uses plain Python callbacks instead of PySide6 Signals to avoid
    Shiboken C++ dispatch crashes (UAF in checkQtSignal on Python 3.14).
    """

    # Do NOT define Signal() here — Shiboken signal dispatch causes
    # random SIGSEGV during emit() on Python 3.14 + PySide6 6.8.2.

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("input_area")
        self.setPlaceholderText("Ask about this binary... (/ for skills, /modify to patch)")
        # Two-to-three-line minimum so the editor is comfortably usable
        # out of the box.  The vertical QSplitter in the panel owns the
        # upper bound — we deliberately do NOT cap the editor's
        # maximum height so the user can drag the splitter as tall as
        # they want and only scrolling constrains a single typing run.
        # Vertical policy is ``Expanding`` so the editor fills the
        # chat-splitter bottom pane when the user drags the handle
        # taller (``Preferred`` would leave empty space below the
        # editor).
        self.setMinimumHeight(60)
        # Vertical policy must be ``Expanding`` so the editor fills the
        # chat-splitter bottom pane when the user drags the handle.
        # ``Preferred`` would let the splitter grow the pane but leave
        # the editor at its size hint with empty space below.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        # Qt's default vertical scrollbar policy is ``ScrollBarAsNeeded``,
        # so we don't set it explicitly.  Only disable the horizontal
        # scrollbar — long lines should wrap, not scroll horizontally.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Enable widget-width line wrap so a long single-line message
        # wraps within the editor rather than being horizontally
        # clipped when the horizontal scrollbar is disabled.
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._enabled = True
        self._skill_slugs: list[str] = []
        self._popup: _SkillPopup | None = None
        self._submit_callback = None  # Callable[[str], None]
        self._cancel_callback = None  # Callable[[], None]
        # Apply the theme-aware QSS at construction and subscribe to
        # subsequent theme changes so the editor and skill popup
        # always match the user-selected palette.  Without this, the
        # host's default Qt palette (often dark) bleeds through and
        # paints a black box around the editor in Rikugan Light mode.
        self.apply_theme()
        try:
            ThemeManager.instance().themeChanged.connect(self.apply_theme)
        except Exception as exc:
            logger.debug("InputArea themeChanged connect failed", exc_info=exc)

    def apply_theme(self) -> None:
        """Refresh the editor (and skill popup) QSS and palette for the current theme.

        Builds the stylesheet from the live :class:`ThemeTokens` and
        applies it.  Foreground / background / placeholder colors are
        also pushed into ``QPalette`` so they survive in host/IDA-native
        mode where the surrounding chrome is not themed.  The
        stylesheet targets only ``QPlainTextEdit#input_area`` (this
        widget's object name) so it does not affect other plain text
        editors in the host.
        """
        try:
            tokens = ThemeManager.instance().tokens()
        except Exception:
            return
        try:
            self.apply_palette(tokens)
        except Exception as exc:
            logger.debug("InputArea apply_palette failed", exc_info=exc)
        try:
            self.setStyleSheet(build_input_area_stylesheet(tokens))
        except Exception as exc:
            logger.debug("InputArea stylesheet apply failed", exc_info=exc)
        if self._popup is not None:
            try:
                self._popup.apply_theme()
            except Exception as exc:
                logger.debug("InputArea popup apply_theme failed", exc_info=exc)

    def apply_palette(self, tokens: object) -> None:
        """Push the editor's Base / Text / PlaceholderText roles from tokens.

        ``QPlainTextEdit`` does not expose foreground, background, or
        placeholder colors as QSS sub-controls; the only way to make
        typed text visible in every theme (including host/IDA-native
        where the surrounding chrome may be light-on-light or
        dark-on-dark) is to set the ``QPalette`` roles explicitly.
        ``QColor`` and ``QPalette`` are re-exported from
        ``rikugan.ui.qt_compat`` (the project binding seam) so the
        test stub layer is honoured consistently with production.
        """
        base = getattr(tokens, "base", "#ffffff")
        text = getattr(tokens, "text", "#1e1e1e")
        muted = getattr(tokens, "muted_text", "#888888")
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Base, QColor(base))
        pal.setColor(QPalette.ColorRole.Text, QColor(text))
        # ``PlaceholderText`` exists on Qt 5.12+ / Qt 6.  Guarded so
        # older PySide6 builds (where the role is absent) silently
        # skip the placeholder role but still keep Base/Text visible.
        if hasattr(QPalette.ColorRole, "PlaceholderText"):
            pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(muted))
        self.setPalette(pal)

    def set_submit_callback(self, callback) -> None:
        """Set the callback for submit (Enter key). Callback signature: (str) -> None."""
        self._submit_callback = callback

    def set_cancel_callback(self, callback) -> None:
        """Set the callback for cancel (Escape key). Callback signature: () -> None."""
        self._cancel_callback = callback

    def set_skill_slugs(self, slugs: list[str]) -> None:
        """Set the list of available skill slugs for autocomplete.

        Automatically includes /goal, /plan, /modify, /explore, and /research as built-in commands.
        """
        combined = set(slugs)
        combined.update(("goal", "plan", "modify", "explore", "research"))
        self._skill_slugs = sorted(combined)

    def focusOutEvent(self, event) -> None:
        """Dismiss the skill popup whenever the editor loses focus.

        The popup is a top-level ``Qt.ToolTip`` window, so it stays on
        top of *every* application — including the one the user alt-tabs
        to — until explicitly hidden. Dismissing it here covers the
        normal leave paths (click elsewhere, alt-tab, window switch),
        so the popup never leaks onto another program's window.
        """
        self._dismiss_popup()
        super().focusOutEvent(event)

    def keyPressEvent(self, event) -> None:
        # Handle popup navigation when popup is visible
        if self._popup and self._popup.isVisible():
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
                slug = self._popup.current_slug()
                if slug:
                    self._accept_completion(slug)
                return
            elif event.key() == Qt.Key.Key_Escape:
                self._dismiss_popup()
                return
            elif event.key() == Qt.Key.Key_Down:
                self._popup.move_selection(1)
                return
            elif event.key() == Qt.Key.Key_Up:
                self._popup.move_selection(-1)
                return

        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
            else:
                text = self.toPlainText().strip()
                if text and self._enabled:
                    # Plain Python callback — no Shiboken signal dispatch
                    if self._submit_callback:
                        self._submit_callback(text)
                    self.clear()
        elif event.key() == Qt.Key.Key_Escape:
            if self._cancel_callback:
                self._cancel_callback()
        else:
            # Let the base class process the key first (inserts character),
            # then check if we need to show/update/dismiss the autocomplete.
            super().keyPressEvent(event)
            self._check_autocomplete()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.setReadOnly(not enabled)
        if enabled:
            self.setPlaceholderText("Ask about this binary... (/ for skills, /modify to patch)")
        else:
            self.setPlaceholderText("Luc Nhan is thinking...")

    # ------------------------------------------------------------------
    # Autocomplete
    # ------------------------------------------------------------------

    def _check_autocomplete(self) -> None:
        """Check current text and show/hide the skill autocomplete popup."""
        text = self.toPlainText()
        if not text.startswith("/") or not self._skill_slugs:
            self._dismiss_popup()
            return

        # Extract partial slug (everything after / up to first space)
        parts = text[1:].split(None, 1)
        # If there's already a space, the slug is complete — dismiss
        if len(parts) > 1:
            self._dismiss_popup()
            return

        partial = parts[0] if parts else ""
        matches = [s for s in self._skill_slugs if s.startswith(partial)]

        if not matches:
            self._dismiss_popup()
            return

        self._show_popup(matches)

    def _show_popup(self, slugs: list[str]) -> None:
        if self._popup is None:
            self._popup = _SkillPopup()
        self._popup.set_items(slugs)

        # Position above the input area
        pos = self.mapToGlobal(self.rect().topLeft())
        popup_height = self._popup.sizeHint().height()
        self._popup.move(pos.x(), pos.y() - popup_height - 4)
        self._popup.show()

    def _dismiss_popup(self) -> None:
        if self._popup and self._popup.isVisible():
            self._popup.hide()

    def _accept_completion(self, slug: str) -> None:
        """Replace current text with /slug and a trailing space."""
        self._dismiss_popup()
        self.setPlainText(f"/{slug} ")
        # Move cursor to end
        cursor = self.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.setTextCursor(cursor)
