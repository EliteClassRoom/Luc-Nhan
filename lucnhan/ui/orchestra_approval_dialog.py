"""Inline delegation approval widget for Orchestra sub-agent delegation.

The modal ``QDialog`` variant that used to live here was dead code —
the chat renders delegation approvals inline (embedded in the message
stream) via :class:`DelegationApprovalWidget`, never as a modal dialog.
Only the inline widget is wired up by ``chat_view``.
"""

from __future__ import annotations

from typing import Any

from ..ui.qt_compat import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
    Signal,
)
from .styles import (
    get_delegation_approval_widget_style,
    get_delegation_header_style,
    get_delegation_info_style,
    get_delegation_preview_style,
)
from .theme.applicator import bind_theme, disconnect_theme


class DelegationApprovalWidget(QFrame):
    """Inline widget version of delegation approval for embedding in chat view.

    Use this when the approval should be shown inline rather than as a modal dialog.
    """

    approved = Signal(str, str)  # (task_name, decision)
    denied = Signal(str, str)  # (task_name, decision)

    def __init__(
        self,
        task_name: str,
        instruction: str,
        context: str,
        tools: list[str],
        model: str,
        max_steps: int = 20,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("delegation_approval")
        self.setStyleSheet(get_delegation_approval_widget_style())
        self._task_name = task_name
        self._instruction = instruction
        self._context = context
        self._tools = tools
        self._model = model
        self._max_steps = max_steps

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)

        # Stored on self so ``_apply_styles`` can repaint them on
        # theme change.  Previously these were local variables only;
        # a theme switch after the widget was constructed left them
        # on their construction-time palette.
        self._header = QLabel(f"Sub-Agent Delegation: {task_name}")
        self._header.setStyleSheet(get_delegation_header_style())
        layout.addWidget(self._header)

        self._info = QLabel(f"Model: {model} | Tools: {len(tools)} | Max Steps: {max_steps}")
        self._info.setStyleSheet(get_delegation_info_style())
        layout.addWidget(self._info)

        self._instruction_preview = QLabel(f"Task: {instruction[:200]}{'...' if len(instruction) > 200 else ''}")
        self._instruction_preview.setStyleSheet(get_delegation_preview_style())
        self._instruction_preview.setWordWrap(True)
        layout.addWidget(self._instruction_preview)

        # Buttons — also stored on self so a theme switch repaints
        # them against the new palette.  Object names already route
        # through the ``delegation_dialog_style`` selectors so the
        # refresh is cheap.
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self._approve_btn = QPushButton("Approve")
        self._approve_btn.setObjectName("approve_btn")
        self._approve_btn.clicked.connect(self._on_approve)
        self._deny_btn = QPushButton("Deny")
        self._deny_btn.setObjectName("deny_btn")
        self._deny_btn.clicked.connect(self._on_deny)
        btn_layout.addWidget(self._approve_btn)
        btn_layout.addWidget(self._deny_btn)
        layout.addLayout(btn_layout)

        # Subscribe to theme changes so the frame border, header,
        # info, preview, and buttons all repaint when the user
        # switches palettes.
        bind_theme(self, self._apply_styles)

    def _apply_styles(self, _tokens: object = None) -> None:
        """Refresh every per-widget QSS from the live tokens."""
        self.setStyleSheet(get_delegation_approval_widget_style())
        if getattr(self, "_header", None) is not None:
            self._header.setStyleSheet(get_delegation_header_style())
        if getattr(self, "_info", None) is not None:
            self._info.setStyleSheet(get_delegation_info_style())
        if getattr(self, "_instruction_preview", None) is not None:
            self._instruction_preview.setStyleSheet(get_delegation_preview_style())
        # Approve / Deny buttons rely on the dialog-wide style via
        # their ``objectName``; no per-widget stylesheet needed.

    def shutdown(self) -> None:
        """Detach the theme subscription (idempotent)."""
        disconnect_theme(self)

    def _on_approve(self) -> None:
        self.approved.emit(self._task_name, "approve")

    def _on_deny(self) -> None:
        self.denied.emit(self._task_name, "deny")

    def get_spec(self) -> dict[str, Any]:
        return {
            "task": self._task_name,
            "instruction": self._instruction,
            "context": self._context,
            "tools": self._tools,
            "model": self._model,
            "max_steps": self._max_steps,
        }
