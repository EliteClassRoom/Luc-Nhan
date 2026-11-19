"""Rich-text guard for the agent tree's output title.

The title embeds the agent name, which the model writes in
``spawn_subagent``; a name like ``<b>x</b>`` must not be parsed as markup.
"""

from __future__ import annotations

import unittest

from tests.qt_real import requires_real_qt

_APP = None


def _app():
    """One QString-free QApplication for this module (see qt_real)."""
    global _APP
    from lucnhan.ui.qt_compat import QApplication

    _APP = QApplication.instance() or QApplication([])
    return _APP


class TestPreviewTitleIsPlainText(unittest.TestCase):
    @requires_real_qt
    def test_title_label_renders_plain_text(self):
        from lucnhan.ui.agent_tree import AgentTreeWidget
        from lucnhan.ui.qt_compat import Qt

        assert _app() is not None
        widget = AgentTreeWidget()
        assert widget._preview_title.textFormat() == Qt.TextFormat.PlainText
