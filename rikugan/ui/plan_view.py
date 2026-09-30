"""Plan mode view: step-by-step plan display with approve/reject.

The previous revision hardcoded all colours (``#d4d4d4``, ``#007acc``,
``#4ec9b0``, ``#f44747``, ``#808080``) at construction time, so the
plan view stayed on its VS Code-dark palette even after the user
switched Rikugan to Light.  The fix routes every colour through the
live :class:`ThemeTokens` resolved via :class:`ThemeManager` and
re-applies the resulting QSS on every ``themeChanged`` emit.

The widget still uses plain Python callbacks (not Qt ``Signal()``)
for the approve/reject user actions — that comment is preserved in
``PlanView.__init__``.  Theme subscription is wired through the
:class:`ThemeManager.themeChanged` signal, which the singleton
already exposes at class level; we don't define new per-widget
``Signal`` instances, so the Python-3.14/Shiboken warning still does
not apply.
"""

from __future__ import annotations

from .qt_compat import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from .theme.applicator import bind_theme, disconnect_theme
from .theme.manager import ThemeManager


def _plan_step_color(t, status: str) -> str:
    """Resolve the status-icon colour from the live tokens.

    The mapping mirrors the previous hardcoded palette so the
    iconography reads identically to the old dark defaults:

    - ``pending`` / ``skipped`` -> muted (was ``#808080``)
    - ``active`` -> accent / highlight (was ``#007acc``)
    - ``done`` -> success (was ``#4ec9b0``)
    - ``error`` -> error (was ``#f44747``)
    """
    if status == "active":
        return t.accent
    if status == "done":
        return t.success
    if status == "error":
        return t.error
    if status == "turn_limit":
        # Distinct from both ``success`` (done) and ``error``
        # (failed): a step that exhausted its turn budget stopped
        # without committing the model intent.
        return t.warning
    return t.muted_text


class PlanStepWidget(QFrame):
    """Single plan step with status indicator."""

    def __init__(self, index: int, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("plan_step")
        self._index = index
        self._status = "pending"

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)

        self._status_label = QLabel("○")
        self._status_label.setFixedWidth(20)
        # ``_apply_styles`` paints the icon once tokens are available.
        self._status_label.setStyleSheet("color: #808080; font-size: inherit;")
        layout.addWidget(self._status_label)

        self._step_label = QLabel(f"{index + 1}. {text}")
        self._step_label.setWordWrap(True)
        # The default colour matches the legacy fallback so a paint
        # that races the first theme emit still reads.  ``_apply_styles``
        # overwrites it as soon as tokens resolve.
        self._step_label.setStyleSheet("color: #d4d4d4; font-size: inherit;")
        layout.addWidget(self._step_label, 1)

    def set_status(self, status: str) -> None:
        self._status = status
        self._apply_status_style()
        # ``objectName`` flips let any future global ``#plan_step_active``
        # rule match the active step without an inline color override.
        if status == "active":
            self.setObjectName("plan_step_active")
        elif status == "done":
            self.setObjectName("plan_step_done")
        elif status == "turn_limit":
            # Distinct objectName so the per-status QSS hook (if any
            # is added later) targets the step that hit the
            # per-step turn budget without affecting regular steps.
            self.setObjectName("plan_step_turn_limit")
        else:
            self.setObjectName("plan_step")
        # Force Qt to re-evaluate object-name selectors so dynamic
        # object names take effect immediately.
        self.style().unpolish(self)
        self.style().polish(self)

    def _apply_status_style(self) -> None:
        """Refresh the status icon colour from the live tokens."""
        try:
            t = ThemeManager.instance().tokens()
        except Exception:
            t = None
        color = _plan_step_color(t, self._status) if t is not None else "#808080"
        font_size = "font-size: inherit;"
        if self._status == "active":
            glyph = "▶"
        elif self._status == "done":
            glyph = "✓"
        elif self._status == "error":
            glyph = "✗"
        elif self._status == "skipped":
            glyph = "−"
        elif self._status == "turn_limit":
            # Distinct from "done" so a step that exhausted its
            # per-step turn budget reads as "stopped at limit"
            # rather than "completed normally".
            glyph = "⏱"
        else:
            glyph = "○"
        self._status_label.setText(glyph)
        self._status_label.setStyleSheet(f"color: {color}; {font_size}")

    def _apply_styles(self, _tokens: object = None) -> None:
        """Theme refresh entry point — see :class:`PlanView` for context."""
        self._apply_status_style()


class PlanView(QFrame):
    """Plan mode view with approve/reject controls.

    Uses plain Python callbacks instead of ``Signal()`` to avoid
    corrupting Shiboken's global signal registry on Python 3.14.
    Theme subscription, however, is routed through the
    :class:`ThemeManager` singleton's class-level ``themeChanged``
    signal — no per-widget ``Signal`` definitions, so the Python-3.14
    caveat still does not apply.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("plan_view")
        self._steps: list[PlanStepWidget] = []
        self._on_approved = None
        self._on_rejected = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        # Header — accent colour, bold.
        self._header = QLabel("Plan")
        # Default fallback matches the legacy ``#569cd6`` so a paint
        # that races the first theme emit still reads.
        self._header.setStyleSheet("color: #569cd6; font-weight: bold; font-size: inherit;")
        layout.addWidget(self._header)

        # Steps container
        self._steps_container = QVBoxLayout()
        layout.addLayout(self._steps_container)

        # Approve/reject buttons — success / error fills with
        # contrasting white text.  QSS is regenerated on every theme
        # change via :meth:`_apply_styles`.
        btn_layout = QHBoxLayout()

        self._approve_btn = QPushButton("Approve & Execute")
        self._approve_btn.setStyleSheet(
            "QPushButton { background: #2ea043; color: white; border: none; "
            "border-radius: 6px; padding: 6px 16px; font-weight: bold; }"
            "QPushButton:hover { background: #3fb950; }"
        )
        self._approve_btn.clicked.connect(self._fire_approved)
        btn_layout.addWidget(self._approve_btn)

        self._reject_btn = QPushButton("Reject")
        self._reject_btn.setStyleSheet(
            "QPushButton { background: #c72e2e; color: white; border: none; "
            "border-radius: 6px; padding: 6px 16px; font-weight: bold; }"
            "QPushButton:hover { background: #d73a49; }"
        )
        self._reject_btn.clicked.connect(self._fire_rejected)
        btn_layout.addWidget(self._reject_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        layout.addStretch()

        # Bind theme refresh now that the children exist.  ``bind_theme``
        # runs the callback synchronously so the legacy fallback
        # colours are replaced with the live tokens before the first
        # paint under normal load.
        bind_theme(self, self._apply_styles)

    def set_plan(self, steps: list[str]) -> None:
        """Set plan steps and display them."""
        self.clear()
        for i, text in enumerate(steps):
            step_widget = PlanStepWidget(i, text)
            self._steps.append(step_widget)
            self._steps_container.addWidget(step_widget)
            # New step picks up the current theme so a rebuild during
            # a theme transition does not leave the step on the
            # construction-time fallback colours.
            step_widget._apply_styles()

    def set_step_status(self, index: int, status: str) -> None:
        if 0 <= index < len(self._steps):
            self._steps[index].set_status(status)

    def set_buttons_visible(self, visible: bool) -> None:
        self._approve_btn.setVisible(visible)
        self._reject_btn.setVisible(visible)

    def set_approved_callback(self, callback) -> None:
        self._on_approved = callback

    def set_rejected_callback(self, callback) -> None:
        self._on_rejected = callback

    def _fire_approved(self) -> None:
        if self._on_approved is not None:
            self._on_approved()

    def _fire_rejected(self) -> None:
        if self._on_rejected is not None:
            self._on_rejected()

    def _apply_styles(self, _tokens: object = None) -> None:
        """Refresh header and button QSS from the live tokens.

        The previous revision built the approve/reject button QSS
        once in ``__init__`` and never updated it; light-mode users
        saw a green/red button row against an otherwise dark frame.
        Re-applying the QSS here keeps the success/error palette
        in sync with the rest of the theme.

        Status-icon colours for existing steps are refreshed by
        walking ``self._steps`` — a step that was already on the
        panel when the theme changed would otherwise keep its
        construction-time ``#808080`` / ``#4ec9b0`` colour.
        """
        try:
            t = ThemeManager.instance().tokens()
        except Exception:
            return
        # Header: ``#569cd6`` -> ``t.accent``.
        self._header.setStyleSheet(f"color: {t.accent}; font-weight: bold; font-size: inherit;")
        # Approve button: success fill with high-contrast text.
        # ``_pick_contrasting_text`` (mirrored inline here) keeps the
        # label readable in light mode where ``highlight_text`` is
        # also near-white.
        from .message_widgets import _pick_contrasting_text

        approve_text = _pick_contrasting_text(t.success, t.text, t.highlight_text)
        self._approve_btn.setStyleSheet(
            f"QPushButton {{ background: {t.success}; color: {approve_text}; "
            f"border: none; border-radius: 6px; padding: 6px 16px; "
            f"font-weight: bold; font-size: inherit; }}"
            f"QPushButton:hover {{ background: {t.success}; border: 1px solid {t.accent}; }}"
            f"QPushButton:pressed {{ background: {t.mid}; color: {t.text}; }}"
            f"QPushButton:focus {{ border: 1px solid {t.accent}; }}"
        )
        reject_text = _pick_contrasting_text(t.error, t.text, t.highlight_text)
        self._reject_btn.setStyleSheet(
            f"QPushButton {{ background: {t.error}; color: {reject_text}; "
            f"border: none; border-radius: 6px; padding: 6px 16px; "
            f"font-weight: bold; font-size: inherit; }}"
            f"QPushButton:hover {{ background: {t.error}; border: 1px solid {t.accent}; }}"
            f"QPushButton:pressed {{ background: {t.mid}; color: {t.text}; }}"
            f"QPushButton:focus {{ border: 1px solid {t.accent}; }}"
        )
        # Refresh any existing steps so a theme change after
        # ``set_plan`` updates the icon colours.
        for step in self._steps:
            step._apply_styles()

    def shutdown(self) -> None:
        """Detach the theme subscription (matches the panel-level teardown contract)."""
        disconnect_theme(self)

    def clear(self) -> None:
        for step in self._steps:
            step.deleteLater()
        self._steps.clear()
