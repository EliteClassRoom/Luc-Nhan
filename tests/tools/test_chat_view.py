"""Tests for rikugan.ui.chat_view — pure logic helpers."""

from __future__ import annotations

import json
import sys
import types
import unittest
from unittest.mock import MagicMock

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()

# Stub all heavy submodules that chat_view imports.
# Reinstall them unconditionally because other tests may have left behind
# incomplete stubs in sys.modules.  Each stub has a ``__getattr__``
# fallback so any missing attribute resolves to a fresh MagicMock,
# keeping this test file resilient to new names added by the
# production code.


class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        m = MagicMock()
        object.__setattr__(self, name, m)
        return m


for _mod_name in [
    "rikugan.agent.turn",
    "rikugan.core.types",
]:
    _stub = _StubModule(_mod_name)
    # Add commonly-needed attrs
    for _attr in [
        "PlanView",
        "TurnEvent",
        "TurnEventType",
        "Message",
        "Role",
        "ToolCall",
        "ToolResult",
    ]:
        setattr(_stub, _attr, MagicMock())
    sys.modules[_mod_name] = _stub

# Other tests may leave stubbed UI modules behind; force fresh imports.
# Note: we must also pop the parent package ``rikugan.ui.theme`` so
# that Python can re-import its submodules from disk — a stub parent
# (a ``types.ModuleType`` without ``__path__``) would otherwise block
# the relative ``from .theme.manager import ...`` resolution that
# ``markdown.py`` performs at import time.
for _mod_name in [
    "rikugan.ui.chat_view",
    "rikugan.ui.message_widgets",
    "rikugan.ui.plan_view",
    "rikugan.ui.tool_widgets",
    "rikugan.ui.styles",
    "rikugan.ui.theme",
    "rikugan.ui.theme.manager",
    "rikugan.ui.theme.tokens",
    "rikugan.ui.markdown",
]:
    sys.modules.pop(_mod_name, None)

from rikugan.ui.bulk_renamer import BulkRenamerWidget
from rikugan.ui.chat_view import _TOOL_GROUP_MIN_CALLS, _is_hidden_system_user_message

# ---------------------------------------------------------------------------
# _is_hidden_system_user_message
# ---------------------------------------------------------------------------


class TestIsHiddenSystemUserMessage(unittest.TestCase):
    def test_empty_string_returns_false(self):
        self.assertFalse(_is_hidden_system_user_message(""))

    def test_none_equivalent_empty_returns_false(self):
        self.assertFalse(_is_hidden_system_user_message(""))

    def test_system_prefix_returns_true(self):
        self.assertTrue(_is_hidden_system_user_message("[SYSTEM] some hint"))

    def test_system_prefix_with_leading_whitespace(self):
        self.assertTrue(_is_hidden_system_user_message("   [SYSTEM] some hint"))

    def test_regular_message_returns_false(self):
        self.assertFalse(_is_hidden_system_user_message("Hello world"))

    def test_lowercase_system_returns_false(self):
        self.assertFalse(_is_hidden_system_user_message("[system] hint"))

    def test_partial_system_keyword_returns_false(self):
        self.assertFalse(_is_hidden_system_user_message("SYSTEM"))

    def test_system_in_middle_returns_false(self):
        self.assertFalse(_is_hidden_system_user_message("not [SYSTEM] hint"))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestChatViewConstants(unittest.TestCase):
    def test_tool_group_min_calls_is_positive(self):
        self.assertGreater(_TOOL_GROUP_MIN_CALLS, 0)

    def test_tool_group_min_calls_value(self):
        self.assertEqual(_TOOL_GROUP_MIN_CALLS, 2)


class TestBulkRenamerLookup(unittest.TestCase):
    def test_find_row_iterates_table(self):
        """``_find_row_for_address`` walks the table rows; verify
        the stub's item() / data() contract is honored."""
        # ``BulkRenamerWidget`` is a QWidget subclass; constructing it via
        # ``__new__`` skips the Qt widget initialization (which would
        # require a live ``QApplication`` and a fully-built UI tree) so we
        # can unit-test the table-row lookup logic in isolation.
        widget = BulkRenamerWidget.__new__(BulkRenamerWidget)

        # Build a 2-row stub table where row 1 holds our address.
        row1_item = MagicMock()
        row1_item.data.return_value = 0x401000
        table = MagicMock()
        table.rowCount.return_value = 2
        table.item.side_effect = lambda r, c: row1_item if r == 1 else None
        widget._table = table
        self.assertEqual(widget._find_row_for_address(0x401000), 1)
        table.rowCount.assert_called()


# ---------------------------------------------------------------------------
# Task 5 — Route `execute_python` to `ExecutePythonWidget` in ChatView.
#
# These tests exercise ``ChatView._handle_tool_event`` and
# ``ChatView.handle_event`` with real ``TurnEvent`` objects, so the
# dispatch path needs the *real* ``TurnEvent`` / ``TurnEventType`` symbols
# (the stubs above are MagicMocks and would mask ``DOCS_GATE_STATUS``).
#
# We pop the stubs and re-import the real modules in :meth:`setUpClass`
# — ``ChatView`` is then bound to real submodules for the lifetime of
# this class. The other tests in this file (which use pure helpers and
# ``BulkRenamerWidget``) captured their references at module-load time and
# remain unaffected by the swap.
# ---------------------------------------------------------------------------


class TestExecutePythonRouting(unittest.TestCase):
    """ChatView routes execute_python to ExecutePythonWidget."""

    @classmethod
    def setUpClass(cls) -> None:
        # Pop stubs so the re-imported ChatView binds to real submodules.
        # Order matters: pop chat_view LAST so its import chain sees the
        # freshly-imported real agent.turn / core.types / tool_widgets.
        for _mod_name in [
            "rikugan.ui.chat_view",
            "rikugan.ui.tool_widgets",
            "rikugan.agent.turn",
            "rikugan.core.types",
        ]:
            sys.modules.pop(_mod_name, None)

        from rikugan import constants as _constants
        from rikugan.agent.turn import TurnEvent, TurnEventType
        from rikugan.ui.chat_view import ChatView
        from rikugan.ui.tool_widgets import (
            ExecutePythonWidget,
            ToolApprovalWidget,
            ToolCallWidget,
        )

        cls._ChatView = ChatView
        cls._TurnEvent = TurnEvent
        cls._TurnEventType = TurnEventType
        cls._ExecutePythonWidget = ExecutePythonWidget
        cls._ToolCallWidget = ToolCallWidget
        cls._ToolApprovalWidget = ToolApprovalWidget
        cls._EXEC_PY = _constants.EXECUTE_PYTHON_TOOL_NAME

    def setUp(self) -> None:
        self.ChatView = self._ChatView
        self.TurnEvent = self._TurnEvent
        self.TurnEventType = self._TurnEventType
        self.ExecutePythonWidget = self._ExecutePythonWidget
        self.ToolCallWidget = self._ToolCallWidget
        self.ToolApprovalWidget = self._ToolApprovalWidget
        self.EXEC_PY = self._EXEC_PY

    def _make_view(self):
        """Bypass __init__ to avoid Qt container construction.

        Stubs the collaborators ``_handle_tool_event`` (and downstream
        ``_register_tool_widget``) calls: ``_insert_widget``,
        ``_scroll_to_bottom``, ``_hide_thinking``, ``_reset_tool_run``,
        ``_scroll_timer``, ``_layout``, ``_on_tool_approval``.
        """
        view = self.ChatView.__new__(self.ChatView)
        view._thinking = None
        view._thinking_shown_at = 0.0
        view._tool_widgets = {}
        view._tool_run_ids = []
        view._tool_run_names = []
        view._tool_run_widgets = []
        view._tool_group = None
        view._group_map = {}
        view._current_assistant = None
        view._layout = MagicMock()
        view._scroll_timer = MagicMock()
        view._scroll_timer.isActive.return_value = False
        view._insert_widget = MagicMock()
        view._scroll_to_bottom = lambda: None
        view._hide_thinking = lambda: None
        view._reset_tool_run = lambda: None
        view._on_tool_approval = lambda *a, **k: None
        # ``handle_event`` calls ``_begin_live_tail_append`` which reads
        # ``_restore_paged``. Default to False (no paginated restore).
        view._restore_paged = False
        view._restore_live_tail_started = False
        view._restore_messages = []
        view._restore_pages = []
        view._nav_widgets = []
        view._begin_live_tail_append = lambda: None
        return view

    # ------------------------------------------------------------------
    # TOOL_CALL_START routing
    # ------------------------------------------------------------------

    def test_tool_call_start_creates_execute_python_widget(self):
        view = self._make_view()
        ev = self.TurnEvent.tool_call_start("tc1", self.EXEC_PY)
        view._handle_tool_event(ev)
        self.assertIsInstance(view._tool_widgets["tc1"], self.ExecutePythonWidget)

    def test_other_tool_still_uses_tool_call_widget(self):
        view = self._make_view()
        ev = self.TurnEvent.tool_call_start("tc2", "rename_function")
        view._handle_tool_event(ev)
        self.assertIsInstance(view._tool_widgets["tc2"], self.ToolCallWidget)

    def test_tool_call_start_registers_widget_in_tool_run(self):
        """``_register_tool_widget`` must be invoked for the new widget so
        tool-run grouping continues to work for ``execute_python``."""
        view = self._make_view()
        view._handle_tool_event(self.TurnEvent.tool_call_start("tc1", self.EXEC_PY))
        self.assertIn("tc1", view._tool_run_ids)
        self.assertEqual(view._tool_run_names, [self.EXEC_PY])

    # ------------------------------------------------------------------
    # TOOL_CALL_ARGS_DELTA → append_args_delta must not crash
    # ------------------------------------------------------------------

    def test_tool_call_args_delta_does_not_crash(self):
        """execute_python streams args via deltas; the widget must handle
        append_args_delta without crashing (was a missing-method AttributeError)."""
        view = self._make_view()
        view._handle_tool_event(self.TurnEvent.tool_call_start("tc3", self.EXEC_PY))
        # Streaming a delta must not raise.
        view._handle_tool_event(self.TurnEvent.tool_call_args_delta("tc3", '{"code": "prin'))
        view._handle_tool_event(self.TurnEvent.tool_call_args_delta("tc3", 't(1)"}'))
        self.assertIsInstance(view._tool_widgets["tc3"], self.ExecutePythonWidget)

    # ------------------------------------------------------------------
    # TOOL_CALL_DONE → set_arguments(code extraction)
    # ------------------------------------------------------------------

    def test_tool_call_done_sets_code(self):
        view = self._make_view()
        view._handle_tool_event(self.TurnEvent.tool_call_start("tc1", self.EXEC_PY))
        view._handle_tool_event(
            self.TurnEvent.tool_call_done(
                "tc1",
                self.EXEC_PY,
                json.dumps({"code": "print(1)"}),
            )
        )
        self.assertEqual(view._tool_widgets["tc1"]._code, "print(1)")

    # ------------------------------------------------------------------
    # TOOL_APPROVAL_REQUEST → reuse existing ExecutePythonWidget
    # ------------------------------------------------------------------

    def test_approval_request_routes_into_existing_widget(self):
        """TOOL_APPROVAL_REQUEST for execute_python must NOT create a new
        ToolApprovalWidget — it routes into the existing ExecutePythonWidget."""
        view = self._make_view()
        view._handle_tool_event(self.TurnEvent.tool_call_start("tc1", self.EXEC_PY))
        ev = self.TurnEvent.tool_approval_request("tc1", self.EXEC_PY, '{"code":"x"}', "")
        view._handle_tool_event(ev)
        self.assertIsInstance(view._tool_widgets["tc1"], self.ExecutePythonWidget)
        self.assertTrue(view._tool_widgets["tc1"]._buttons_visible)

    def test_approval_request_for_other_tool_still_creates_approval_widget(self):
        """Regression: TOOL_APPROVAL_REQUEST for non-execute_python tools
        must keep creating the legacy ``ToolApprovalWidget``."""
        view = self._make_view()
        view._handle_tool_event(self.TurnEvent.tool_call_start("tc2", "rename_function"))
        ev = self.TurnEvent.tool_approval_request("tc2", "rename_function", '{"new_name":"x"}', "")
        view._handle_tool_event(ev)
        # The legacy approval widget is inserted via _insert_widget; verify
        # the call by inspecting the stub's recorded calls.
        inserted = [c.args[0] for c in view._insert_widget.call_args_list]
        # _insert_widget may have been called by _register_tool_widget too;
        # the last call must be a ToolApprovalWidget for this tool_call_id.
        self.assertTrue(any(isinstance(w, self.ToolApprovalWidget) for w in inserted))

    # ------------------------------------------------------------------
    # Approval promoted out of a collapsed ToolGroupWidget
    # ------------------------------------------------------------------

    def test_approval_widget_promoted_out_of_collapsed_group(self):
        """When an execute_python call is grouped (collapsed) alongside
        other tool calls, the approval request must surface the widget so
        the Allow button is visible — otherwise the user sees a "hanging"
        collapsed group with no indication action is needed."""
        view = self._make_view()
        # Simulate a 2-tool run: both get grouped into _tool_group, hidden.
        view._handle_tool_event(self.TurnEvent.tool_call_start("ta", "decompile_function"))
        view._handle_tool_event(self.TurnEvent.tool_call_start("tb", self.EXEC_PY))
        # Widget tb is now nested inside _tool_group's _body (collapsed).
        tb_widget = view._tool_widgets["tb"]
        group = view._group_map.get("tb")
        self.assertIsNotNone(group, "execute_python widget should be grouped")

        # Approval arrives for the grouped execute_python widget.
        ev = self.TurnEvent.tool_approval_request("tb", self.EXEC_PY, '{"code":"x"}', "")
        view._handle_tool_event(ev)

        # After promotion: buttons visible (so the user can act), the widget
        # is no longer mapped to the group, and it was re-inserted into the
        # main layout (the last _insert_widget call is this widget).
        self.assertTrue(tb_widget._buttons_visible)
        self.assertNotIn("tb", view._group_map)
        inserted = [c.args[0] for c in view._insert_widget.call_args_list]
        self.assertIs(inserted[-1], tb_widget)

    def test_approval_widget_promoted_updates_group_map(self):
        """After promotion, _group_map must drop the entry so a later
        TOOL_RESULT for the same id does not route notify_result to a
        group the widget left."""
        view = self._make_view()
        view._handle_tool_event(self.TurnEvent.tool_call_start("ta", "decompile_function"))
        view._handle_tool_event(self.TurnEvent.tool_call_start("tb", self.EXEC_PY))
        ev = self.TurnEvent.tool_approval_request("tb", self.EXEC_PY, '{"code":"x"}', "")
        view._handle_tool_event(ev)
        self.assertNotIn("tb", view._group_map)

    # ------------------------------------------------------------------
    # DOCS_GATE_STATUS dispatch (UI-level handle_event path)
    # ------------------------------------------------------------------

    def test_docs_gate_status_routes_to_widget(self):
        view = self._make_view()
        view._handle_tool_event(self.TurnEvent.tool_call_start("tc1", self.EXEC_PY))
        ev = self.TurnEvent.docs_gate_status("tc1", "running", reasons=("2 IDA modules",))
        view.handle_event(ev)
        self.assertIn("Reviewing", view._tool_widgets["tc1"]._status_text)

    def test_docs_gate_status_for_unknown_tool_call_is_noop(self):
        """DOCS_GATE_STATUS with no matching widget must not raise."""
        view = self._make_view()
        ev = self.TurnEvent.docs_gate_status("nope", "running")
        # No prior tool_call_start; should silently do nothing.
        view.handle_event(ev)
        self.assertEqual(view._tool_widgets, {})


if __name__ == "__main__":
    unittest.main()
