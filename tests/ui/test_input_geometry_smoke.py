"""Real-PySide6 offscreen geometry smoke for the chat input resize.

The shared Qt-stub layer cannot model real splitter geometry, so this
test bypasses the stub and constructs a real vertical ``QSplitter``
holding a real ``InputArea``.  It proves four user-visible
properties:

  1. The minimum height is the documented 2-3 line default (60px).
  2. The vertical size policy is ``Expanding`` so the editor fills
     the chat-splitter bottom pane when the user drags the handle
     taller.
  3. ``lineWrapMode()`` is ``WidgetWidth`` so a long single-line
     message wraps within the editor rather than being horizontally
     clipped.
  4. Growing the bottom pane via ``setSizes`` increases the editor's
     height (the actual user-visible drag behavior).
"""

import os
import sys
import unittest

# Headless: must be set before any PySide6 import so Qt picks the
# offscreen platform plugin.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QWidget,
)

# Ensure a real QApplication exists before we import ``InputArea``,
# whose ``__init__`` calls ``apply_theme`` which touches Qt.
_app = QApplication.instance() or QApplication(sys.argv)

from rikugan.ui.input_area import InputArea


class TestInputGeometrySmoke(unittest.TestCase):
    """Real-PySide6 offscreen geometry checks for the chat input."""

    def _build_chat(
        self, *, total_height: int = 400
    ) -> tuple[QSplitter, InputArea]:
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setObjectName("chat_splitter")
        splitter.setHandleWidth(4)
        splitter.setChildrenCollapsible(False)

        # Top pane placeholder for the chat history area.
        top = QWidget()
        splitter.addWidget(top)

        # Bottom pane: an HBox holding the editor + action buttons.
        bottom = QWidget()
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(0)
        editor = InputArea(bottom)
        send = QPushButton("Send", bottom)
        bottom_layout.addWidget(editor, 1)
        bottom_layout.addWidget(send)
        splitter.addWidget(bottom)

        # Lay the splitter out at the requested total height.
        splitter.resize(400, total_height)
        # Use a fixed top pane so the bottom pane ends up at the
        # configured height; this exercises the user's drag handle.
        splitter.setSizes([total_height - 200, 200])
        splitter.show()
        # Pump events so Qt applies the layout / geometry.
        QApplication.processEvents()
        return splitter, editor

    def test_minimum_height_is_two_to_three_lines(self) -> None:
        """``InputArea`` defaults to a 2-3 line minimum (60px)."""
        editor = InputArea()
        self.assertEqual(editor.minimumHeight(), 60)

    def test_vertical_size_policy_is_expanding(self) -> None:
        """``InputArea`` vertical policy must be ``Expanding`` so the
        editor fills the chat-splitter bottom pane when the user
        drags the handle taller.
        """
        editor = InputArea()
        policy = editor.sizePolicy().verticalPolicy()
        self.assertEqual(policy, QSizePolicy.Policy.Expanding)

    def test_line_wrap_mode_is_widget_width(self) -> None:
        """Long single-line messages must wrap within the editor
        rather than being horizontally clipped.
        """
        editor = InputArea()
        self.assertEqual(
            editor.lineWrapMode(),
            QPlainTextEdit.LineWrapMode.WidgetWidth,
        )

    def test_growing_bottom_pane_grows_editor_height(self) -> None:
        """The user-visible drag behavior: when the user drags the
        handle taller, the editor's height grows with the pane.
        Without ``Expanding`` vertical policy, the pane grows but
        the editor stays at its size hint and leaves empty space.
        """
        splitter, editor = self._build_chat(total_height=400)
        # Baseline: bottom pane at 200px, editor fills it.
        baseline = editor.height()

        # Now grow the bottom pane to 300px and let Qt relayout.
        splitter.setSizes([100, 300])
        QApplication.processEvents()
        grown = editor.height()

        # Editor should have grown with the pane (allow a few px
        # slack for the splitter handle, margins, and the action
        # button row).
        self.assertGreater(
            grown,
            baseline + 50,
            f"editor height {grown} did not grow enough from baseline "
            f"{baseline}; the splitter drag did not enlarge the editor.",
        )


if __name__ == "__main__":
    unittest.main()
