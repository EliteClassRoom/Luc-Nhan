"""Tests for lucnhan.ui.agent_tree preview content selection.

The preview pane shows a running agent's live tool feed and a finished
agent's summary. These tests drive ``update_agent`` / ``_on_item_selected``
against stub Qt children (same hermetic strategy as test_a2a_widget.py),
so no event loop is required.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks
from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()
install_ida_mocks()

from lucnhan.ui.agent_tree import (
    FINISHED_STATUSES,
    AgentInfo,
    AgentTreeWidget,
)


def _build_tree() -> tuple[AgentTreeWidget, MagicMock]:
    """Build an AgentTreeWidget with stub Qt children.

    Bypasses ``__init__`` (which builds real Qt widgets and binds theme
    signals) and wires only the state the preview logic touches.
    """
    tree = AgentTreeWidget.__new__(AgentTreeWidget)
    tree._agents = {}
    tree._items = {}
    tree._running_count = 0
    tree._completed_count = 0
    tree._preview = MagicMock()
    return tree, tree._preview


def _select(tree: AgentTreeWidget, agent_id: str) -> None:
    """Make the stub tree report *agent_id* as the selected item."""
    selected = MagicMock()
    selected.data.return_value = agent_id
    tree._tree = MagicMock()
    tree._tree.selectedItems.return_value = [selected]
    tree._tree.indexOfTopLevelItem.return_value = 0
    tree._filter_combo = MagicMock()
    tree._filter_combo.currentText.return_value = "All Agents"
    tree._status_label = MagicMock()


class TestPreviewTextSelection(unittest.TestCase):
    """``_preview_text`` picks the live feed only while the agent runs."""

    def test_running_agent_shows_activity_feed(self) -> None:
        info = AgentInfo(
            agent_id="a1",
            name="child",
            agent_type="custom",
            status="RUNNING",
            summary="",
            activity="→ read_file main.c\n← read_file: ok",
        )
        self.assertEqual(
            AgentTreeWidget._preview_text(info),
            "→ read_file main.c\n← read_file: ok",
        )

    def test_completed_agent_shows_summary(self) -> None:
        info = AgentInfo(
            agent_id="a1",
            name="child",
            agent_type="custom",
            status="COMPLETED",
            summary="found 3 functions",
            activity="→ read_file main.c",
        )
        self.assertEqual(AgentTreeWidget._preview_text(info), "found 3 functions")

    def test_running_agent_without_activity_falls_back_to_placeholder(self) -> None:
        info = AgentInfo(
            agent_id="a1", name="child", agent_type="custom", status="RUNNING"
        )
        self.assertEqual(AgentTreeWidget._preview_text(info), "(no output yet)")

    def test_all_finished_statuses_prefer_summary(self) -> None:
        for status in ("COMPLETED", "FAILED", "CANCELLED"):
            self.assertIn(status, FINISHED_STATUSES)
            info = AgentInfo(
                agent_id="a1",
                name="child",
                agent_type="custom",
                status=status,
                summary="final answer",
                activity="→ read_file main.c",
            )
            self.assertEqual(AgentTreeWidget._preview_text(info), "final answer")

    def test_pending_agent_with_activity_prefers_activity(self) -> None:
        """Not-yet-finished means the feed wins, regardless of pending/running."""
        info = AgentInfo(
            agent_id="a1",
            name="child",
            agent_type="custom",
            status="PENDING",
            summary="",
            activity="→ decompile main",
        )
        self.assertEqual(AgentTreeWidget._preview_text(info), "→ decompile main")


class TestPreviewPaneUpdates(unittest.TestCase):
    """The widget routes both entry points through the same helper."""

    def test_item_selected_renders_preview_text(self) -> None:
        tree, preview = _build_tree()
        info = AgentInfo(
            agent_id="a1",
            name="child",
            agent_type="custom",
            status="RUNNING",
            activity="→ read_file main.c",
        )
        tree._agents["a1"] = info
        _select(tree, "a1")

        AgentTreeWidget._on_item_selected(tree)
        preview.setPlainText.assert_called_once_with("→ read_file main.c")

    def test_selected_completed_agent_renders_summary(self) -> None:
        tree, preview = _build_tree()
        info = AgentInfo(
            agent_id="a1",
            name="child",
            agent_type="custom",
            status="COMPLETED",
            summary="the answer",
            activity="→ read_file main.c",
        )
        tree._agents["a1"] = info
        _select(tree, "a1")

        AgentTreeWidget._on_item_selected(tree)
        preview.setPlainText.assert_called_once_with("the answer")

    def test_auto_update_of_selected_running_agent_shows_feed(self) -> None:
        """update_agent refreshes a selected running agent with the live feed."""
        tree, preview = _build_tree()
        running = AgentInfo(
            agent_id="a1",
            name="child",
            agent_type="custom",
            status="RUNNING",
            activity="→ read_file a.c",
        )
        tree._agents["a1"] = running
        tree._items["a1"] = MagicMock()
        _select(tree, "a1")

        # Advance the feed, then push the newer snapshot.
        updated = AgentInfo(
            agent_id="a1",
            name="child",
            agent_type="custom",
            status="RUNNING",
            activity="→ read_file a.c\n← read_file: 42 lines",
        )
        AgentTreeWidget.update_agent(tree, updated)
        preview.setPlainText.assert_called_once_with(
            "→ read_file a.c\n← read_file: 42 lines"
        )


if __name__ == "__main__":
    unittest.main()
