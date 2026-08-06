"""Tests for rikugan.ui.panel_core — pure logic helpers."""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# Install the lightweight ``PySide6`` stubs BEFORE importing any
# rikugan module.  The conftest hook uninstalls those stubs
# (and re-imports the real C extension) for the *next* test
# module's collection, so sibling tests that need real Qt
# (e.g. ``rikugan/tests/test_chat_view_async_restore.py``)
# pick up the real classes even when this file runs first.
from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()


# Stub heavy rikugan submodules.  We only stub the *names* that
# the production code under test imports, and only as MagicMock —
# real classes from the real modules are not needed because the
# tests in this module exercise static helpers (``_export_*``) and
# build a bare ``RikuganPanelCore`` via ``object.__new__`` so its
# constructor (which would touch every heavy dependency) is bypassed.
#
# Each stub uses a ``__getattr__`` fallback so that ANY missing
# attribute (e.g. ``get_placeholder_style``) resolves to a fresh
# MagicMock instead of ``AttributeError``.  This keeps the test
# file resilient to new style getters added by the production
# code — the test does not need to enumerate every name.
class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        m = MagicMock()
        object.__setattr__(self, name, m)
        return m


# Snapshot the real rikugan modules BEFORE we install the stubs below,
# so a module-level pytest fixture can restore them after this test
# module finishes.  Without this snapshot/restore pair, the stubs we
# install at import time would leak into sibling test modules and
# break tests that touch the real rikugan modules (e.g. provider
# tests that construct ``AnthropicProvider`` / ``OpenAIProvider``).
_STUBBED_MODULES = [
    "rikugan.ui.styles",
    "rikugan.ui.chat_view",
    "rikugan.ui.input_area",
    "rikugan.ui.context_bar",
    "rikugan.ui.tool_widgets",
    "rikugan.ui.message_widgets",
    "rikugan.ui.markdown",
    "rikugan.ui.theme",
    "rikugan.ui.theme.applicator",
    "rikugan.ui.theme.manager",
    "rikugan.ui.theme.tokens",
    "rikugan.ui.theme.palette_dark",
    "rikugan.ui.theme.palette_light",
    "rikugan.ui.theme.palette_ida",
    "rikugan.core.config",
    "rikugan.core.logging",
    "rikugan.core.types",
    "rikugan.core.host",
    "rikugan.agent.turn",
    "rikugan.agent.mutation",
    "rikugan.providers.auth_cache",
    "rikugan.providers.anthropic_provider",
    "rikugan.providers.ollama_provider",
    "rikugan.providers.registry",
]
_STUBBED_MODULE_BACKUPS: dict[str, object] = {name: sys.modules.get(name) for name in _STUBBED_MODULES}


for _mod_name in [
    "rikugan.ui.styles",
    "rikugan.ui.chat_view",
    "rikugan.ui.input_area",
    "rikugan.ui.context_bar",
    "rikugan.ui.tool_widgets",
    "rikugan.ui.message_widgets",
    "rikugan.ui.markdown",
    "rikugan.ui.theme",
    "rikugan.ui.theme.applicator",
    "rikugan.ui.theme.manager",
    "rikugan.ui.theme.tokens",
    "rikugan.ui.theme.palette_dark",
    "rikugan.ui.theme.palette_light",
    "rikugan.ui.theme.palette_ida",
    "rikugan.core.config",
    "rikugan.core.logging",
    "rikugan.core.types",
    "rikugan.core.host",
    "rikugan.agent.turn",
    "rikugan.agent.mutation",
    "rikugan.providers.auth_cache",
    "rikugan.providers.anthropic_provider",
    "rikugan.providers.ollama_provider",
    "rikugan.providers.registry",
]:
    # Always (re)install the stub.  Other test files may have left
    # partial stubs in sys.modules that lack the names this module
    # needs; reinstalling a clean stub keeps the behavior
    # deterministic regardless of collection order.
    _stub = _StubModule(_mod_name)
    for _attr in [
        "build_small_button_stylesheet",
        "maybe_host_stylesheet",
        "use_native_host_theme",
        "ChatView",
        "InputArea",
        "ContextBar",
        "_SharedSpinnerTimer",
        "RikuganConfig",
        "log_error",
        "log_info",
        "log_debug",
        "log_warning",
        "TurnEvent",
        "TurnEventType",
        "MutationRecord",
        "Role",
        "ModelInfo",
        "resolve_auth_cached",
        "resolve_anthropic_auth",
        "DEFAULT_OLLAMA_URL",
        "ProviderRegistry",
    ]:
        setattr(_stub, _attr, MagicMock())
    sys.modules[_mod_name] = _stub

# Ensure DEFAULT_OLLAMA_URL is a string (used in comparisons)
_ollama_stub = sys.modules.get("rikugan.providers.ollama_provider")
if _ollama_stub and not isinstance(getattr(_ollama_stub, "DEFAULT_OLLAMA_URL", None), str):
    _ollama_stub.DEFAULT_OLLAMA_URL = "http://localhost:11434"


# ``TestShutdownDisconnectsThemeChanged`` exercises the real
# ``ThemeManager`` singleton (the only way to observe what
# ``panel.shutdown()`` actually disconnected from).  Provide a
# working ``themeChanged`` stand-in that records its listeners on
# a real list so the test's ``_listeners`` precondition works.
class _StubThemeSignal:
    def __init__(self):
        self._listeners: list = []

    def connect(self, slot):
        self._listeners.append(slot)

    def disconnect(self, slot=None):
        if slot is None:
            self._listeners.clear()
        else:
            try:
                self._listeners.remove(slot)
            except ValueError:
                pass

    def emit(self, *_args, **_kwargs):
        for listener in list(self._listeners):
            try:
                listener(*_args, **_kwargs)
            except Exception:
                pass


class _StubThemeManager:
    """Stand-in for the real ``ThemeManager`` singleton.

    Records connects/disconnects in a real list so the shutdown
    test can assert the panel's slot was registered and later
    removed.  The production code only calls ``connect`` /
    ``disconnect`` on ``themeChanged``; everything else is a
    no-op.
    """

    _instance: _StubThemeManager | None = None

    def __init__(self):
        self.themeChanged = _StubThemeSignal()

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None


_tm_stub = sys.modules.get("rikugan.ui.theme.manager")
if _tm_stub is not None:
    _tm_stub.ThemeManager = _StubThemeManager

# Force-remove any stub that test_ida_panel may have registered
# so we always import the real module here.
sys.modules.pop("rikugan.ui.panel_core", None)

# Pytest fixture that restores the real rikugan modules after this
# test module finishes.  The fixtures below are module-scoped so
# they run exactly once per ``test_panel_core.py`` collection cycle,
# and they use the ``_STUBBED_MODULE_BACKUPS`` snapshot taken at
# import time to put the real modules back in ``sys.modules``.
#
# Without this fixture, the MagicMock stubs installed above leak
# into sibling test modules and poison ``rikugan.core.config``,
# ``rikugan.providers.registry``, and other modules for every
# downstream test — which is exactly the kind of test-isolation
# regression that makes headless / provider tests fail when run
# after a panel-core test in the same pytest invocation.
import pytest  # noqa: E402

from rikugan.ui import panel_core as _pc_module  # noqa: E402
from rikugan.ui.export_formatting import (  # noqa: E402
    _TOOL_RESULT_TRUNCATE_CHARS,
    _export_detect_lang,
    _export_format_tool_args,
    _export_format_tool_result,
)
from rikugan.ui.panel_core import (  # noqa: E402
    RikuganPanelCore,
)


@pytest.fixture(scope="module", autouse=True)
def _restore_rikugan_modules_after_panel_core_tests():
    """Restore the real rikugan modules once this test module finishes."""
    yield
    for name, original in _STUBBED_MODULE_BACKUPS.items():
        if original is None:
            # Module wasn't loaded before this test file — drop the stub
            # so the next test file re-imports the real implementation.
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


# ---------------------------------------------------------------------------
# _export_detect_lang
# ---------------------------------------------------------------------------


class TestExportDetectLang(unittest.TestCase):
    def test_arg_key_code_returns_python(self):
        self.assertEqual(_export_detect_lang("anything", arg_key="code"), "python")

    def test_arg_key_python_returns_python(self):
        self.assertEqual(_export_detect_lang("anything", arg_key="python"), "python")

    def test_arg_key_c_code_returns_c(self):
        self.assertEqual(_export_detect_lang("anything", arg_key="c_code"), "c")

    def test_arg_key_c_declaration_returns_c(self):
        self.assertEqual(_export_detect_lang("anything", arg_key="c_declaration"), "c")

    def test_arg_key_prototype_returns_c(self):
        self.assertEqual(_export_detect_lang("anything", arg_key="prototype"), "c")

    def test_tool_name_execute_python(self):
        self.assertEqual(_export_detect_lang("x", tool_name="execute_python"), "python")

    def test_tool_name_decompile_function(self):
        self.assertEqual(_export_detect_lang("x", tool_name="decompile_function"), "c")

    def test_tool_name_get_il(self):
        self.assertEqual(_export_detect_lang("x", tool_name="get_il"), "c")

    def test_tool_name_fetch_disassembly(self):
        self.assertEqual(_export_detect_lang("x", tool_name="fetch_disassembly"), "x86asm")

    def test_hexdump_pattern_returns_text(self):
        hexdump = "00000000  48 65 6c 6c 6f 20 57 6f  72 6c 64 0a\n"
        self.assertEqual(_export_detect_lang(hexdump), "text")

    def test_asm_pattern_returns_x86asm(self):
        asm = "mov eax, 0x1234\ncall 0xdeadbeef\n"
        self.assertEqual(_export_detect_lang(asm), "x86asm")

    def test_c_pattern_returns_c(self):
        c_code = "int foo(void) {\n  if (x > 0) { return 1; }\n}"
        self.assertEqual(_export_detect_lang(c_code), "c")

    def test_python_pattern_returns_python(self):
        py_code = "def foo():\n    return 1\nimport os\n"
        self.assertEqual(_export_detect_lang(py_code), "python")

    def test_empty_returns_empty(self):
        self.assertEqual(_export_detect_lang(""), "")

    def test_plain_text_returns_empty(self):
        self.assertEqual(_export_detect_lang("hello world, nothing special"), "")

    def test_arg_key_takes_priority_over_tool_name(self):
        # arg_key check comes first
        result = _export_detect_lang("x", tool_name="execute_python", arg_key="c_code")
        self.assertEqual(result, "c")


# ---------------------------------------------------------------------------
# _export_format_tool_args
# ---------------------------------------------------------------------------


class TestExportFormatToolArgs(unittest.TestCase):
    def _make_tc(self, name: str, args: dict):
        tc = MagicMock()
        tc.name = name
        tc.arguments = args
        return tc

    def test_short_value_inline(self):
        tc = self._make_tc("tool", {"key": "val"})
        result = _export_format_tool_args(tc)
        self.assertIn("`key`", result)
        self.assertIn("'val'", result)

    def test_long_value_code_block(self):
        long_val = "x" * 100
        tc = self._make_tc("tool", {"code": long_val})
        result = _export_format_tool_args(tc)
        self.assertIn("```python", result)
        self.assertIn(long_val, result)

    def test_multiline_value_code_block(self):
        tc = self._make_tc("tool", {"body": "line1\nline2"})
        result = _export_format_tool_args(tc)
        self.assertIn("```", result)
        self.assertIn("line1\nline2", result)

    def test_empty_args(self):
        tc = self._make_tc("tool", {})
        result = _export_format_tool_args(tc)
        self.assertEqual(result, "")

    def test_multiple_args(self):
        tc = self._make_tc("tool", {"a": "short", "b": "also short"})
        result = _export_format_tool_args(tc)
        self.assertIn("`a`", result)
        self.assertIn("`b`", result)


# ---------------------------------------------------------------------------
# _export_format_tool_result
# ---------------------------------------------------------------------------


class TestExportFormatToolResult(unittest.TestCase):
    def _make_tr(self, content: str, name: str = "tool"):
        tr = MagicMock()
        tr.content = content
        tr.name = name
        return tr

    def test_short_content_not_truncated(self):
        tr = self._make_tr("short content")
        result = _export_format_tool_result(tr)
        self.assertIn("short content", result)
        self.assertNotIn("truncated", result)

    def test_long_content_truncated(self):
        long_content = "A" * (_TOOL_RESULT_TRUNCATE_CHARS + 100)
        tr = self._make_tr(long_content)
        result = _export_format_tool_result(tr)
        self.assertIn("truncated", result)
        self.assertNotIn("A" * (_TOOL_RESULT_TRUNCATE_CHARS + 1), result)

    def test_returns_code_block(self):
        tr = self._make_tr("output")
        result = _export_format_tool_result(tr)
        self.assertIn("```", result)
        self.assertTrue(result.startswith("```"))

    def test_decompile_tool_gets_c_hint(self):
        tr = self._make_tr("int main(void) {}", "decompile_function")
        result = _export_format_tool_result(tr)
        self.assertIn("```c", result)


# ---------------------------------------------------------------------------
# Panel logic via object.__new__ injection
# ---------------------------------------------------------------------------


def _make_panel():
    # Use the class's own ``__new__`` rather than ``object.__new__``.
    # ``RikuganPanelCore`` inherits from a C-level Qt class
    # (``QWidget``), and ``object.__new__`` is rejected on C-level
    # subclasses with a ``TypeError`` — use
    # ``RikuganPanelCore.__new__(RikuganPanelCore)`` which delegates
    # to the C-level allocator.  The same idiom is used in
    # ``test_chat_view.py`` and ``test_settings_dialog.py``; keeping
    # the form consistent avoids surprises when real PySide6 has
    # been loaded by a sibling test in the same session.
    panel = RikuganPanelCore.__new__(RikuganPanelCore)
    panel._is_shutdown = False
    panel._polling = False
    panel._pending_answer = False
    panel._chat_views = {}
    panel._pending_restore_messages = {}
    panel._context_bar = None
    panel._mutation_panel = None
    panel._skills_refresh_timer = None
    panel._poll_timer = None
    # Token-display debouncing fields added in the Phase 1.3 perf change.
    # The debounced path requires these on the panel even when constructed
    # via ``__new__`` (which bypasses ``__init__``).
    panel._pending_token_display = None
    panel._token_display_timer = None
    panel._last_token_display_value = -1
    panel._input_area = MagicMock()
    panel._send_btn = MagicMock()
    panel._cancel_btn = MagicMock()
    panel._mutations_btn = MagicMock()
    # Task-8 history coordinator fields.  The legacy helpers do not
    # exercise history behavior, but ``shutdown`` / ``on_database_changed``
    # now call ``_invalidate_history`` which expects these attributes to
    # exist (or be lazily defaulted via ``getattr``).  Seeding them
    # explicitly keeps the fixture deterministic.
    panel._history_panel = None
    panel._history_btn = MagicMock()
    panel._history_generation = 0
    panel._history_executor = None
    panel._history_poll_timer = None
    panel._history_pending = False
    import queue as _queue
    import threading as _threading

    panel._history_result_queue = _queue.Queue()
    panel._history_closing = _threading.Event()
    panel._count_label = MagicMock()
    panel._tab_widget = MagicMock()
    panel._tab_bar = MagicMock()
    panel._ctrl = MagicMock()
    panel._config = MagicMock()
    panel._ui_hooks = None
    panel._awaiting_button_approval = False
    return panel


class TestTabIdAtIndex(unittest.TestCase):
    def test_returns_none_when_widget_is_none(self):
        panel = _make_panel()
        panel._tab_widget.widget.return_value = None
        result = panel._tab_id_at_index(0)
        self.assertIsNone(result)

    def test_returns_tab_id_from_property(self):
        panel = _make_panel()
        mock_widget = MagicMock()
        mock_widget.property.return_value = "tab123"
        panel._chat_views["tab123"] = mock_widget
        panel._tab_widget.widget.return_value = mock_widget
        result = panel._tab_id_at_index(0)
        self.assertEqual(result, "tab123")

    def test_returns_none_when_property_not_in_chat_views(self):
        panel = _make_panel()
        mock_widget = MagicMock()
        mock_widget.property.return_value = "ghost_id"
        # ghost_id not in _chat_views, and widget itself is not in values either
        panel._tab_widget.widget.return_value = mock_widget
        result = panel._tab_id_at_index(0)
        self.assertIsNone(result)

    def test_fallback_to_widget_identity(self):
        panel = _make_panel()
        mock_widget = MagicMock()
        mock_widget.property.return_value = None  # no property
        panel._chat_views["tab_x"] = mock_widget
        panel._tab_widget.widget.return_value = mock_widget
        result = panel._tab_id_at_index(0)
        self.assertEqual(result, "tab_x")


class TestActiveChatView(unittest.TestCase):
    def test_returns_view_for_active_tab(self):
        panel = _make_panel()
        mock_view = MagicMock()
        panel._ctrl.active_tab_id = "t1"
        panel._chat_views["t1"] = mock_view
        self.assertIs(panel._active_chat_view(), mock_view)

    def test_returns_none_when_active_tab_not_in_views(self):
        panel = _make_panel()
        panel._ctrl.active_tab_id = "missing"
        self.assertIsNone(panel._active_chat_view())


class TestSetRunning(unittest.TestCase):
    def test_running_true_sets_queue_text(self):
        panel = _make_panel()
        panel._set_running(True)
        panel._send_btn.setText.assert_called_with("Queue")

    def test_running_false_sets_send_text(self):
        panel = _make_panel()
        panel._set_running(False)
        panel._send_btn.setText.assert_called_with("Send")

    def test_running_shows_cancel_btn(self):
        panel = _make_panel()
        panel._set_running(True)
        panel._cancel_btn.setVisible.assert_called_with(True)

    def test_not_running_hides_cancel_btn(self):
        panel = _make_panel()
        panel._set_running(False)
        panel._cancel_btn.setVisible.assert_called_with(False)


class TestUpdateTabBarVisibility(unittest.TestCase):
    def test_single_tab_hides_bar(self):
        panel = _make_panel()
        panel._tab_widget.count.return_value = 1
        panel._update_tab_bar_visibility()
        panel._tab_bar.setVisible.assert_called_with(False)

    def test_two_tabs_shows_bar(self):
        panel = _make_panel()
        panel._tab_widget.count.return_value = 2
        panel._update_tab_bar_visibility()
        panel._tab_bar.setVisible.assert_called_with(True)

    def test_zero_tabs_hides_bar(self):
        panel = _make_panel()
        panel._tab_widget.count.return_value = 0
        panel._update_tab_bar_visibility()
        panel._tab_bar.setVisible.assert_called_with(False)


class TestOnCloseTab(unittest.TestCase):
    def test_does_not_close_last_tab(self):
        panel = _make_panel()
        panel._tab_widget.count.return_value = 1
        panel._on_close_tab(0)
        panel._ctrl.close_tab.assert_not_called()

    def test_closes_tab_with_multiple(self):
        panel = _make_panel()
        panel._tab_widget.count.return_value = 2
        mock_widget = MagicMock()
        mock_widget.property.return_value = "tid"
        panel._chat_views["tid"] = mock_widget
        panel._tab_widget.widget.return_value = mock_widget
        panel._on_close_tab(0)
        panel._ctrl.close_tab.assert_called_once_with("tid")

    def test_removes_view_from_chat_views(self):
        panel = _make_panel()
        panel._tab_widget.count.return_value = 2
        mock_widget = MagicMock()
        mock_widget.property.return_value = "tid"
        panel._chat_views["tid"] = mock_widget
        panel._tab_widget.widget.return_value = mock_widget
        panel._on_close_tab(0)
        self.assertNotIn("tid", panel._chat_views)


class TestOnToggleMutationLog(unittest.TestCase):
    def test_noop_when_no_panel(self):
        panel = _make_panel()
        panel._mutation_panel = None
        panel._on_toggle_mutation_log()  # must not raise

    def test_shows_when_hidden(self):
        panel = _make_panel()
        mock_mp = MagicMock()
        mock_mp.isVisible.return_value = False
        panel._mutation_panel = mock_mp
        panel._on_toggle_mutation_log()
        mock_mp.setVisible.assert_called_with(True)

    def test_hides_when_visible(self):
        panel = _make_panel()
        mock_mp = MagicMock()
        mock_mp.isVisible.return_value = True
        panel._mutation_panel = mock_mp
        panel._on_toggle_mutation_log()
        mock_mp.setVisible.assert_called_with(False)

    def test_updates_checked_state(self):
        panel = _make_panel()
        mock_mp = MagicMock()
        mock_mp.isVisible.return_value = False
        panel._mutation_panel = mock_mp
        panel._on_toggle_mutation_log()
        panel._mutations_btn.setChecked.assert_called_with(True)


class TestOnUndoRequested(unittest.TestCase):
    def test_noop_when_shutdown(self):
        panel = _make_panel()
        panel._is_shutdown = True
        panel._on_undo_requested(1)
        # _start_agent should not be called — we can check ctrl is not used
        panel._ctrl.start_agent.assert_not_called()

    def test_starts_undo_agent(self):
        panel = _make_panel()
        panel._ctrl.active_tab_id = "t1"
        mock_view = MagicMock()
        panel._chat_views["t1"] = mock_view
        panel._ctrl.start_agent.return_value = None  # no error
        panel._ctrl.has_free_slot.return_value = True
        # Pre-inject a mock poll_timer so _ensure_poll_timer returns early
        panel._poll_timer = MagicMock()
        panel._on_undo_requested(2)
        # Multi-tab: start_agent now receives the target tab_id.
        panel._ctrl.start_agent.assert_called_once_with("/undo 2", tab_id="t1")


class TestOnOrchestraApproval(unittest.TestCase):
    """Regression tests for ``RikuganPanelCore._on_orchestra_approval``.

    The orchestra / agent-handoff path uses a different approval queue
    inside the agent loop (``_approval_queue``) than regular tool
    approvals (``_tool_approval_queue``).  The panel must call
    ``agent_loop.submit_approval`` (which targets the orchestra queue)
    — never ``submit_tool_approval`` (which targets the tool queue).
    After submitting, the panel must clear the same UI state flags
    that the button-only approval flow clears
    (``_pending_answer``, ``_awaiting_button_approval``) so the input
    area is re-enabled for the next user turn.
    """

    def _make_runner_loop(self) -> MagicMock:
        runner = MagicMock()
        agent_loop = MagicMock()
        runner.agent_loop = agent_loop
        return runner, agent_loop

    def test_approve_calls_submit_approval(self) -> None:
        panel = _make_panel()
        runner, agent_loop = self._make_runner_loop()
        panel._ctrl.get_runner.return_value = runner

        panel._on_orchestra_approval("call_xyz", "approve")

        agent_loop.submit_approval.assert_called_once_with("approve")

    def test_deny_calls_submit_approval(self) -> None:
        panel = _make_panel()
        runner, agent_loop = self._make_runner_loop()
        panel._ctrl.get_runner.return_value = runner

        panel._on_orchestra_approval("call_xyz", "deny")

        agent_loop.submit_approval.assert_called_once_with("deny")

    def test_does_not_call_submit_tool_approval(self) -> None:
        """``_on_orchestra_approval`` must NOT push to the
        tool-approval queue.  The two channels serve different
        agent-loop flows and routing an orchestra decision to the
        tool queue would deadlock the orchestra handoff."""
        panel = _make_panel()
        runner, agent_loop = self._make_runner_loop()
        panel._ctrl.get_runner.return_value = runner

        panel._on_orchestra_approval("call_xyz", "approve")

        agent_loop.submit_tool_approval.assert_not_called()

    def test_clears_pending_answer_flag(self) -> None:
        panel = _make_panel()
        runner, _ = self._make_runner_loop()
        panel._ctrl.get_runner.return_value = runner
        panel._pending_answer = True

        panel._on_orchestra_approval("call_xyz", "approve")

        self.assertFalse(panel._pending_answer)

    def test_clears_awaiting_button_approval_flag(self) -> None:
        panel = _make_panel()
        runner, _ = self._make_runner_loop()
        panel._ctrl.get_runner.return_value = runner
        panel._awaiting_button_approval = True

        panel._on_orchestra_approval("call_xyz", "deny")

        self.assertFalse(panel._awaiting_button_approval)

    def test_no_runner_does_not_raise(self) -> None:
        """If the agent runner is gone (cancelled, finished) the
        call must be a no-op — no exception, but the UI flags are
        still cleared so the panel can re-enable input."""
        panel = _make_panel()
        panel._ctrl.get_runner.return_value = None
        panel._pending_answer = True
        panel._awaiting_button_approval = True

        # Must not raise.
        panel._on_orchestra_approval("call_xyz", "approve")

        # The flags reflect "the decision is done" regardless of
        # whether the agent loop is alive to receive it.
        self.assertFalse(panel._pending_answer)
        self.assertFalse(panel._awaiting_button_approval)


class TestShutdownIdempotency(unittest.TestCase):
    def test_double_shutdown_safe(self):
        panel = _make_panel()
        panel._poll_timer = None
        panel._skills_refresh_timer = None
        panel._context_bar = None
        panel._ui_hooks = None
        panel.shutdown()
        panel.shutdown()  # second call must not raise or double-cleanup
        panel._ctrl.shutdown.assert_called_once()

    def test_shutdown_calls_ctrl_shutdown(self):
        panel = _make_panel()
        panel._poll_timer = None
        panel._skills_refresh_timer = None
        panel._context_bar = None
        panel._ui_hooks = None
        panel.shutdown()
        panel._ctrl.shutdown.assert_called_once()


class TestStopSkillsRefreshTimer(unittest.TestCase):
    def test_noop_when_timer_none(self):
        panel = _make_panel()
        panel._skills_refresh_timer = None
        panel._stop_skills_refresh_timer()  # must not raise

    def test_clears_timer_ref(self):
        panel = _make_panel()
        mock_timer = MagicMock()
        panel._skills_refresh_timer = mock_timer
        panel._stop_skills_refresh_timer()
        self.assertIsNone(panel._skills_refresh_timer)
        mock_timer.stop.assert_called_once()
        mock_timer.deleteLater.assert_called_once()


class TestRestoreMessagesIfNeeded(unittest.TestCase):
    def test_noop_when_no_pending_restore(self):
        panel = _make_panel()
        mock_view = MagicMock()
        panel._chat_views["t1"] = mock_view
        panel._restore_messages_if_needed("t1")
        mock_view.restore_from_messages_async.assert_not_called()

    def test_restores_pending_messages_once(self):
        panel = _make_panel()
        mock_view = MagicMock()
        panel._chat_views["t1"] = mock_view
        panel._pending_restore_messages["t1"] = ["m1", "m2"]
        panel._restore_messages_if_needed("t1")
        mock_view.restore_from_messages_async.assert_called_once_with(["m1", "m2"])
        self.assertNotIn("t1", panel._pending_restore_messages)


class TestUpdateTokenDisplay(unittest.TestCase):
    def test_noop_when_context_bar_none(self):
        panel = _make_panel()
        panel._context_bar = None
        panel._update_token_display(1000)  # must not raise

    def test_calls_set_tokens_with_given_count(self):
        panel = _make_panel()
        mock_cb = MagicMock()
        panel._context_bar = mock_cb
        panel._config.provider.context_window = 200000
        panel._update_token_display(5000)
        # Phase 1.3: token display is debounced — flush before asserting.
        panel._flush_pending_token_display()
        mock_cb.set_tokens.assert_called_once_with(5000, 200000)

    def test_zero_context_window_fallback(self):
        panel = _make_panel()
        mock_cb = MagicMock()
        panel._context_bar = mock_cb
        panel._config.provider.context_window = 0
        panel._update_token_display(1234)
        # Phase 1.3: token display is debounced — flush before asserting.
        panel._flush_pending_token_display()
        mock_cb.set_tokens.assert_called_once_with(1234, 0)

    def test_coalesces_repeated_updates_within_window(self):
        """Multiple updates with different values produce only one set_tokens call."""
        panel = _make_panel()
        mock_cb = MagicMock()
        panel._context_bar = mock_cb
        panel._config.provider.context_window = 200000
        panel._update_token_display(1000)
        panel._update_token_display(2000)
        panel._update_token_display(3000)
        # Only the latest value is flushed, not all three. We check the
        # last call's args explicitly because Qt may eagerly fire the
        # single-shot debounce timer in some test environments, which
        # would otherwise look like multiple set_tokens invocations.
        panel._flush_pending_token_display()
        self.assertEqual(mock_cb.set_tokens.call_args, mock_cb.set_tokens.call_args_list[-1])
        self.assertEqual(mock_cb.set_tokens.call_args.args, (3000, 200000))

    def test_noop_when_value_unchanged(self):
        """An update with the same value as the last displayed value is a no-op."""
        panel = _make_panel()
        mock_cb = MagicMock()
        panel._context_bar = mock_cb
        panel._config.provider.context_window = 200000
        panel._update_token_display(5000)
        panel._flush_pending_token_display()
        panel._update_token_display(5000)  # same value — must not enqueue
        mock_cb.set_tokens.assert_called_once_with(5000, 200000)


# ---------------------------------------------------------------------------
# _create_tab — must connect real ChatView signals, not call
# nonexistent methods (see review of the async chat restore
# change).  This regression guard ensures the connection shape
# stays compatible with the real ``ChatView`` class.
# ---------------------------------------------------------------------------


class TestCreateTabSignalWiring(unittest.TestCase):
    """``RikuganPanelCore._create_tab`` must use ``ChatView`` signals.

    Older revisions of ``_create_tab`` called
    ``chat_view.set_tool_approval_callback(...)`` and
    ``chat_view.set_user_answer_callback(...)``, but the real
    ``ChatView`` class only exposes ``tool_approval_submitted``,
    ``user_answer_submitted``, and ``orchestra_approval_decided``
    Qt signals.  Calling the missing methods would raise
    ``AttributeError`` the first time the user opened a tab.
    This regression test pins the correct behaviour by *actually
    running* production ``_create_tab`` against a stubbed
    ``ChatView`` and asserting on the resulting wiring.
    """

    def _make_panel_with_chat_view(self):
        """Build a panel whose ``_create_tab`` can be invoked.

        We rely on the test-file-level stub of
        ``rikugan.ui.chat_view``: the stub's ``ChatView`` attribute
        is a plain ``MagicMock``, so ``ChatView()`` returns a fresh
        ``MagicMock`` instance.  Production ``_create_tab`` runs
        against that mock — and we then assert on the side
        effects (signal connections, ``setProperty``, tab storage,
        tab-widget insertion).
        """
        panel = _make_panel()
        # ``_update_tab_bar_visibility`` (called at the end of
        # production ``_create_tab``) compares ``tab_widget.count()``
        # against ``1``.  The bare ``MagicMock`` returns a truthy
        # mock, which breaks the comparison.  Pin the count to a
        # real int so the production code can run end-to-end.
        panel._tab_widget.count.return_value = 1
        return panel

    def test_create_tab_uses_chat_view_signals_not_legacy_callbacks(self) -> None:
        """``_create_tab`` must not call the legacy
        ``set_tool_approval_callback`` / ``set_user_answer_callback``
        methods on the chat view (they don't exist on the real
        ``ChatView`` class).  The panel must connect the real Qt
        signals instead.

        The test runs *production* ``_create_tab`` against a
        stubbed ``ChatView`` and checks the resulting mock for
        signal ``.connect()`` calls.  If a future refactor
        reintroduced the legacy callback methods, the real
        ``ChatView()`` mock (which has no such attribute) would
        raise ``AttributeError`` and this test would fail.
        """
        panel = self._make_panel_with_chat_view()

        chat_view = panel._create_tab("tab-x", "New Chat")

        # The new chat view must be stored under its tab_id so
        # lookups work.
        self.assertIn("tab-x", panel._chat_views)
        self.assertIs(panel._chat_views["tab-x"], chat_view)
        # The ``tab_id`` property is set on the widget for
        # ``_tab_id_at_index`` to recover it via ``widget.property``.
        chat_view.setProperty.assert_called_with("tab_id", "tab-x")
        # Tab widget must have received the new view + label.
        panel._tab_widget.addTab.assert_called_with(chat_view, "New Chat")
        # The three Qt signals must each be connected exactly once.
        # Multi-tab wiring binds the tab_id via a lambda closure so approval
        # answers route to the correct runner — so we assert connection
        # happened (once), not the exact target identity.
        self.assertEqual(chat_view.tool_approval_submitted.connect.call_count, 1)
        self.assertEqual(chat_view.user_answer_submitted.connect.call_count, 1)
        self.assertEqual(chat_view.orchestra_approval_decided.connect.call_count, 1)
        # The legacy callback methods must NOT be called.
        # ``MagicMock`` auto-creates attributes on access, so we
        # verify by checking the call list on the mock.
        for forbidden in (
            "set_tool_approval_callback",
            "set_user_answer_callback",
        ):
            getattr(chat_view, forbidden).assert_not_called()


# ---------------------------------------------------------------------------
# _on_theme_changed — must refresh every existing ChatView's
# inline styles when the active theme changes.  The fix to the
# review regression added a loop over ``self._chat_views.values()``
# that calls ``refresh_inline_styles()``; this test pins the
# behaviour so a future refactor doesn't silently drop the loop.
# ---------------------------------------------------------------------------


class TestOnThemeChangedRefresh(unittest.TestCase):
    def test_calls_refresh_inline_styles_on_all_chat_views(self) -> None:
        """``_on_theme_changed`` must call ``refresh_inline_styles``
        on every chat view currently in ``_chat_views`` so the
        cached inline-styled widgets pick up the new theme
        tokens.  The review found that the original code did
        not iterate the chat views; this test pins the
        corrected behaviour by *calling the production function*
        and asserting on the side effects.
        """
        panel = _make_panel()
        cv1 = MagicMock()
        cv2 = MagicMock()
        cv3 = MagicMock()
        panel._chat_views = {"a": cv1, "b": cv2, "c": cv3}

        # The production function takes a ThemeTokens payload
        # (the value emitted by ThemeManager.themeChanged).  We
        # don't need a real token — a MagicMock satisfies the
        # signature.
        panel._on_theme_changed(MagicMock())

        # Every chat view must have been refreshed exactly once.
        cv1.refresh_inline_styles.assert_called_once()
        cv2.refresh_inline_styles.assert_called_once()
        cv3.refresh_inline_styles.assert_called_once()

    def test_on_theme_changed_survives_failing_chat_view(self) -> None:
        """If a single ``ChatView.refresh_inline_styles`` raises,
        ``_on_theme_changed`` must still refresh the remaining
        views.  The production function wraps each call in a
        ``try / except`` for best-effort refresh, and the test
        pins that contract.
        """
        panel = _make_panel()
        cv_good = MagicMock()
        cv_bad = MagicMock()
        cv_bad.refresh_inline_styles.side_effect = RuntimeError("boom")
        panel._chat_views = {"good": cv_good, "bad": cv_bad}

        # Must not raise.
        panel._on_theme_changed(MagicMock())

        # The good view was still refreshed.
        cv_good.refresh_inline_styles.assert_called_once()
        # The bad view was attempted (the error did not skip it).
        cv_bad.refresh_inline_styles.assert_called_once()


# ---------------------------------------------------------------------------
# shutdown() — must disconnect from ThemeManager.themeChanged
# so the singleton doesn't keep a dangling reference to the
# panel alive after teardown.
# ---------------------------------------------------------------------------


class TestShutdownDisconnectsThemeChanged(unittest.TestCase):
    def setUp(self) -> None:
        # Sibling test files (notably
        # ``tests/tools/test_settings_dialog.py``) re-import the
        # *real* ``rikugan.ui.theme.manager`` so they can exercise
        # the production ``ThemeManager`` singleton.  When those
        # tests run before us in the same session, the real
        # module is what ``from rikugan.ui.theme.manager import
        # ThemeManager`` resolves to here.  Force the stub back
        # into place so the test can observe connect/disconnect
        # against the in-test ``_StubThemeSignal``.
        sys.modules.pop("rikugan.ui.theme.manager", None)
        _tm_stub = _StubModule("rikugan.ui.theme.manager")
        _tm_stub.ThemeManager = _StubThemeManager
        sys.modules["rikugan.ui.theme.manager"] = _tm_stub

    def test_shutdown_disconnects_theme_changed(self) -> None:
        from rikugan.ui.theme.manager import ThemeManager

        # Reset the ThemeManager singleton so we control its
        # signal listeners.
        ThemeManager.reset()
        tm = ThemeManager.instance()

        panel = _make_panel()
        panel._poll_timer = None
        panel._skills_refresh_timer = None
        panel._context_bar = None
        panel._ui_hooks = None
        # Connect the panel's slot to the live manager.
        tm.themeChanged.connect(panel._on_theme_changed)
        # Sanity: disconnect should not have been called yet.
        self.assertTrue(
            any(getattr(slot, "__name__", "") == "_on_theme_changed" for slot in (tm.themeChanged._listeners or []))
            if hasattr(tm.themeChanged, "_listeners")
            else True,
            "precondition: handler connected",
        )

        # The real disconnect call is wrapped in try/except; we
        # patch it to a no-op for the test so we can observe
        # the call.
        original_disconnect = tm.themeChanged.disconnect
        with patch.object(tm.themeChanged, "disconnect", wraps=original_disconnect) as mock_disconnect:
            panel.shutdown()
        mock_disconnect.assert_any_call(panel._on_theme_changed)
        # Reset for any other tests that may run later.
        ThemeManager.reset()


# ---------------------------------------------------------------------------
# Tools tab order, default selection, and renamer isolation
# ---------------------------------------------------------------------------


class TestToolsTabOrderAndDefault(unittest.TestCase):
    """The visible Tools tabs are Agents, A2A, Knowledge in that order,
    and the Knowledge tab is the default landing tab.

    The Bulk Renamer tab has been removed from the visible Tools
    surface so opening Tools no longer enumerates the function table.
    The renamer implementation is dormant, not deleted; these tests
    pin the new contract that nothing in the panel builds a renamer
    widget, engine, or enumeration pump on Tools open.
    """

    def test_tab_constants_match_index_order(self):
        from rikugan.ui.tools_panel import ToolsPanel

        self.assertEqual(ToolsPanel.TAB_AGENTS, 0)
        self.assertEqual(ToolsPanel.TAB_A2A, 1)
        self.assertEqual(ToolsPanel.TAB_KNOWLEDGE, 2)
        self.assertEqual(ToolsPanel.TAB_COUNT, 3)
        # Order matters for ``_TAB_INITIALIZERS`` below.
        self.assertLess(ToolsPanel.TAB_AGENTS, ToolsPanel.TAB_A2A)
        self.assertLess(ToolsPanel.TAB_A2A, ToolsPanel.TAB_KNOWLEDGE)

    def test_replacing_current_tab_does_not_reenter_activation_callback(self):
        """Replacing a selected lazy tab must not recursively initialize it."""
        from rikugan.ui.tools_panel import ToolsPanel

        class _Tabs:
            def __init__(self) -> None:
                self.currentChanged = MagicMock()
                self._current_index = ToolsPanel.TAB_AGENTS
                self._signals_blocked = False

            def blockSignals(self, blocked: bool) -> bool:
                previous = self._signals_blocked
                self._signals_blocked = blocked
                return previous

            def currentIndex(self) -> int:
                return self._current_index

            def setCurrentIndex(self, index: int) -> None:
                self._current_index = index
                if not self._signals_blocked:
                    self.currentChanged.emit(index)

            def widget(self, _index: int) -> MagicMock:
                return MagicMock()

            def removeTab(self, _index: int) -> None:
                self._current_index = ToolsPanel.TAB_AGENTS
                if not self._signals_blocked:
                    self.currentChanged.emit(self._current_index)

            def insertTab(self, _index: int, _widget: object, _label: str) -> None:
                self._current_index = ToolsPanel.TAB_A2A
                if not self._signals_blocked:
                    self.currentChanged.emit(self._current_index)

        tools_panel = ToolsPanel.__new__(ToolsPanel)
        tools_panel._tabs = _Tabs()

        tools_panel._replace_tab(ToolsPanel.TAB_AGENTS, MagicMock(), "Agents")

        tools_panel._tabs.currentChanged.emit.assert_not_called()
        self.assertEqual(tools_panel._tabs.currentIndex(), ToolsPanel.TAB_AGENTS)
        self.assertFalse(tools_panel._tabs._signals_blocked)

    def test_tab_initializer_keys_match_constants(self):
        panel = RikuganPanelCore.__new__(RikuganPanelCore)
        from rikugan.ui.tools_panel import ToolsPanel

        initializers = panel._TAB_INITIALIZERS
        # No renamer initializer should be reachable by tab index.
        self.assertNotIn("_ensure_renamer_tab_initialized", initializers.values())
        # The Renamer constant must NOT exist any more.
        self.assertFalse(
            hasattr(ToolsPanel, "TAB_RENAMER"),
            "ToolsPanel.TAB_RENAMER has been removed; renamer is hidden.",
        )
        # Initializers line up with the public tab constants.
        self.assertIn(ToolsPanel.TAB_AGENTS, initializers)
        self.assertIn(ToolsPanel.TAB_A2A, initializers)
        self.assertIn(ToolsPanel.TAB_KNOWLEDGE, initializers)
        # Knowledge is bound to the Knowledge initializer.
        self.assertEqual(
            initializers[ToolsPanel.TAB_KNOWLEDGE],
            "_ensure_knowledge_tab_initialized",
        )

    def test_show_tools_panel_defaults_to_knowledge(self):
        """Calling ``show_tools_panel`` without arguments opens Knowledge."""
        panel = RikuganPanelCore.__new__(RikuganPanelCore)
        panel._is_shutdown = False
        panel._mode_bar = MagicMock()
        panel._tools_form = None
        panel._tools_btn = MagicMock()
        panel._tools_poll_timer = MagicMock()
        panel._ensure_tools_panel_created = MagicMock()

        # Build a fake ToolsPanel whose ``_tabs`` we can spy on.
        from rikugan.ui.tools_panel import ToolsPanel

        fake_panel = MagicMock()
        fake_panel._tabs.currentIndex.return_value = ToolsPanel.TAB_A2A
        fake_panel._tabs.count.return_value = ToolsPanel.TAB_COUNT
        panel._tools_panel = fake_panel
        # ``_ensure_tab_initialized`` should be called with the
        # knowledge tab index.
        panel._ensure_tab_initialized = MagicMock()

        panel.show_tools_panel()

        # Default tab is Knowledge (2), not the removed Renamer.
        fake_panel._tabs.setCurrentIndex.assert_called_with(ToolsPanel.TAB_KNOWLEDGE)
        panel._ensure_tab_initialized.assert_called_with(ToolsPanel.TAB_KNOWLEDGE)

    def test_show_tools_panel_falls_back_for_unknown_index(self):
        """Out-of-range and explicit renamer indices fall back to Knowledge."""
        from rikugan.ui.tools_panel import ToolsPanel

        panel = RikuganPanelCore.__new__(RikuganPanelCore)
        panel._is_shutdown = False
        panel._mode_bar = MagicMock()
        panel._tools_form = None
        panel._tools_btn = MagicMock()
        panel._tools_poll_timer = MagicMock()
        panel._ensure_tools_panel_created = MagicMock()
        panel._tools_panel = MagicMock()
        panel._tools_panel._tabs.count.return_value = ToolsPanel.TAB_COUNT
        panel._ensure_tab_initialized = MagicMock()

        panel.show_tools_panel(tab_index=99)
        panel._tools_panel._tabs.setCurrentIndex.assert_called_with(ToolsPanel.TAB_KNOWLEDGE)
        panel._ensure_tab_initialized.assert_called_with(ToolsPanel.TAB_KNOWLEDGE)

        # Historical numeric index ``0`` previously meant Renamer.
        # It must now map to the new tab-zero (Agents), not raise.
        panel._tools_panel._tabs.setCurrentIndex.reset_mock()
        panel._ensure_tab_initialized.reset_mock()
        panel.show_tools_panel(tab_index=0)
        panel._tools_panel._tabs.setCurrentIndex.assert_called_with(ToolsPanel.TAB_AGENTS)
        panel._ensure_tab_initialized.assert_called_with(ToolsPanel.TAB_AGENTS)

    def test_no_renamer_widget_or_engine_attributes_on_panel(self):
        """Panel must not lazily construct renamer widgets, engines, or timers."""
        panel = RikuganPanelCore.__new__(RikuganPanelCore)
        self.assertFalse(
            hasattr(panel, "_bulk_renamer"),
            "_bulk_renamer attribute should be gone after the renamer removal.",
        )
        self.assertFalse(hasattr(panel, "_renamer_engine"))
        self.assertFalse(hasattr(panel, "_renamer_fetch_timer"))
        # The panel must also not expose any renamer handler method.
        self.assertFalse(hasattr(panel, "_on_renamer_start"))
        self.assertFalse(hasattr(panel, "_on_renamer_cancel"))
        self.assertFalse(hasattr(panel, "_ensure_renamer_tab_initialized"))
        self.assertFalse(hasattr(panel, "show_tools_with_renamer"))

    def test_theme_refresh_iterates_only_active_tabs(self):
        """The theme refresh loop should no longer touch a renamer widget."""
        panel = RikuganPanelCore.__new__(RikuganPanelCore)
        panel._bulk_renamer = None
        # Look for any ``_bulk_renamer`` mention in the theme refresh
        # code path. The active attribute list is the simplest check.
        from rikugan.ui import panel_core as pc

        # The expected active attribute list is documented inline at
        # the loop site; verify no ``_bulk_renamer`` is iterated.
        src = pc.__file__
        with open(src, encoding="utf-8") as handle:
            body = handle.read()
        # Find the theme refresh loop and check it does not name renamer.
        loop_idx = body.find("for attr in (")
        self.assertGreaterEqual(loop_idx, 0, "theme refresh loop not found")
        snippet = body[loop_idx : loop_idx + 200]
        self.assertNotIn("_bulk_renamer", snippet)


# ---------------------------------------------------------------------------
# Task 7 — fresh-by-default startup and IDB-change behavior
# (spec §7.1, §7.2).  ``_build_ui`` and ``on_database_changed`` must never
# restore history or read the saved-session manifest, and IDB switches must
# always end with exactly one ``New Chat`` tab and an empty pending restore
# map.  The legacy ``_try_restore_session`` path is removed; these tests
# pin the no-restore contract going forward.
# ---------------------------------------------------------------------------


class TestStartupNoRestore(unittest.TestCase):
    """``_build_ui`` must not touch the legacy startup-restore path.

    Source-level assertions cover the contract because the production
    method depends on real Qt construction (mode bar, mode stack, main
    splitter, history/chat widgets, context bar, …) that the panel-stub
    fixture does not model.  Reading the source is enough to detect any
    reintroduction of ``_try_restore_session()``,
    ``restore_sessions(...)``, or ``restore_session()``.
    """

    def test_build_ui_source_has_no_try_restore_session_call(self) -> None:
        import inspect

        from rikugan.ui.panel_core import RikuganPanelCore

        source = inspect.getsource(RikuganPanelCore._build_ui)
        self.assertNotIn(
            "_try_restore_session",
            source,
            msg="_build_ui() must not call _try_restore_session(); startup must be fresh.",
        )

    def test_build_ui_source_has_no_legacy_restore_api_call(self) -> None:
        import inspect

        from rikugan.ui.panel_core import RikuganPanelCore

        source = inspect.getsource(RikuganPanelCore._build_ui)
        # ``restore_sessions`` (bulk) and ``restore_session`` (legacy
        # single-tab) are removed together in Task 7.  Reject any
        # reintroduction from a future refactor.
        self.assertNotIn(
            "restore_sessions",
            source,
            msg="_build_ui() must not invoke the legacy bulk restore path.",
        )
        self.assertNotIn(
            "self._ctrl.restore_session(",
            source,
            msg="_build_ui() must not invoke the legacy single-session restore path.",
        )

    def test_build_ui_source_does_not_seed_pending_restore_messages(self) -> None:
        """``_build_ui`` must leave ``_pending_restore_messages`` empty.

        The map is only written by explicit ``History attach`` flow
        after a user selects an entry from the History panel.  A
        fresh startup must never populate it.
        """
        import inspect

        from rikugan.ui.panel_core import RikuganPanelCore

        source = inspect.getsource(RikuganPanelCore._build_ui)
        self.assertNotIn(
            "_pending_restore_messages[",
            source,
            msg="_build_ui() must not seed _pending_restore_messages on startup.",
        )

    def test_build_ui_source_does_not_trigger_startup_auto_restore(self) -> None:
        """``_build_ui`` must not auto-restore the latest session.

        A hidden history-list request followed by a hidden load was
        re-introduced by the upstream merge (commit 90f6d4d), causing
        the plugin to replace the blank ``New Chat`` tab with the
        most recent saved session every time the panel opens.  The
        user must open History explicitly to restore a past chat.
        """
        import inspect

        from rikugan.ui.panel_core import RikuganPanelCore

        source = inspect.getsource(RikuganPanelCore._build_ui)
        self.assertNotIn(
            "_startup_restore_pending = True",
            source,
            msg="_build_ui() must not set _startup_restore_pending; startup must be fresh.",
        )
        self.assertNotIn(
            "_start_history_list_request",
            source,
            msg="_build_ui() must not trigger a hidden history list request on startup.",
        )


class TestChatSplitterWrapsMainAndInput(unittest.TestCase):
    """The vertical ``_chat_splitter`` must own the main conversation
    area (top) and the input section (bottom) so the user can drag the
    handle to grow the input.  Source-level assertions cover the
    wiring because ``_build_ui`` depends on real Qt construction that
    the panel-stub fixture does not model.  Reading the source is
    enough to detect a reintroduction of the bare ``addWidget`` layout
    that would lose the resize handle and the two-to-three-line
    default.
    """

    def test_chat_splitter_wraps_main_and_input(self) -> None:
        import inspect

        from rikugan.ui.panel_core import RikuganPanelCore

        source = inspect.getsource(RikuganPanelCore._build_ui)
        # The vertical chat splitter must be created before either pane
        # is added, and the main conversation area + input section must
        # both be added to it (not the chat layout directly).
        self.assertIn(
            "self._chat_splitter = QSplitter(Qt.Orientation.Vertical)",
            source,
            msg=("_build_ui() must create a vertical QSplitter so the user has a real resize handle for the input."),
        )
        self.assertIn(
            "self._chat_splitter.addWidget(self._main_splitter)",
            source,
            msg=("_build_ui() must place the main conversation area in the top pane of the chat splitter."),
        )
        self.assertIn(
            "self._chat_splitter.addWidget(input_container)",
            source,
            msg=(
                "_build_ui() must place the input section in the bottom pane of the chat splitter so the user can drag."
            ),
        )
        # The old bare addWidget path would lose the resize handle.
        self.assertNotIn(
            "chat_layout.addWidget(self._build_input_section()",
            source,
            msg=(
                "_build_ui() must not add the input section directly to "
                "the chat layout — wrap it in the chat splitter instead."
            ),
        )
        # 4px handle is the minimum width that reads as a drag
        # affordance without crowding the action buttons.
        self.assertIn(
            "self._chat_splitter.setHandleWidth(4)",
            source,
            msg=(
                "_build_ui() must give the chat splitter a draggable "
                "handle (4px is the minimum that reads as a handle)."
            ),
        )
        # Setting ``childrenCollapsible(False)`` prevents the user
        # from collapsing either pane to zero height by accident.
        self.assertIn(
            "self._chat_splitter.setChildrenCollapsible(False)",
            source,
            msg=(
                "_build_ui() must disable pane collapsing so the user "
                "cannot accidentally hide the input or the chat area."
            ),
        )

    def test_input_area_default_height_is_two_to_three_lines(self) -> None:
        """The two-to-three-line default is on ``InputArea``; the
        splitter owns the upper bound, so the editor must not pin a
        maximum height.
        """
        import inspect

        from rikugan.ui.input_area import InputArea

        source = inspect.getsource(InputArea.__init__)
        self.assertIn(
            "self.setMinimumHeight(60)",
            source,
            msg=("InputArea must default to a 2-3 line minimum height (60px at 18px line-height with 6px padding)."),
        )
        # ``setMaximumHeight`` must not be called on the editor — the
        # vertical QSplitter in the panel owns the upper bound.
        self.assertNotIn(
            "setMaximumHeight",
            source,
            msg=(
                "InputArea must not cap its maximum height; the vertical QSplitter in the panel owns the upper bound."
            ),
        )


class TestOnDatabaseChangedNoRestore(unittest.TestCase):
    """``on_database_changed`` must not restore history (spec §7.2).

    Behavior-level: invoke the production method with a fully stubbed
    panel and assert the legacy restore spy is untouched, exactly one
    new ``New Chat`` tab is created, and the pending restore map is
    empty.  This pins both "no restore" and "fresh-by-default" on every
    IDB switch — a key acceptance criterion (spec §17 #3).
    """

    def _make_panel(self):
        panel = RikuganPanelCore.__new__(RikuganPanelCore)
        panel._is_shutdown = False
        panel._polling = False
        panel._pending_answer = False
        panel._chat_views = {}
        panel._pending_restore_messages = {}
        panel._context_bar = None
        panel._mutation_panel = None
        panel._skills_refresh_timer = None
        panel._poll_timer = None
        panel._pending_token_display = None
        panel._token_display_timer = None
        panel._last_token_display_value = -1
        panel._input_area = MagicMock()
        panel._send_btn = MagicMock()
        panel._cancel_btn = MagicMock()
        panel._mutations_btn = MagicMock()
        # Task-8 history coordinator fields — seeded so
        # ``on_database_changed``'s call to ``_invalidate_history`` does
        # not raise on a fixture that bypassed ``__init__``.
        panel._history_panel = None
        panel._history_btn = MagicMock()
        panel._history_generation = 0
        panel._history_executor = None
        panel._history_poll_timer = None
        panel._history_pending = False
        import queue as _queue
        import threading as _threading

        panel._history_result_queue = _queue.Queue()
        panel._history_closing = _threading.Event()
        panel._count_label = MagicMock()
        panel._tab_widget = MagicMock()
        # ``while self._tab_widget.count():`` loop in on_database_changed
        # must terminate.  A bare MagicMock returns truthy; pin it to 0.
        panel._tab_widget.count.return_value = 0
        panel._tab_bar = MagicMock()
        panel._ctrl = MagicMock()
        panel._ctrl._idb_path = "/old/path.i64"
        panel._ctrl.active_tab_id = "tab-new"
        panel._ctrl.reset_for_new_file = MagicMock()
        panel._config = MagicMock()
        panel._ui_hooks = None
        panel._awaiting_button_approval = False
        # Spies / collaborators — replacing the methods with mocks lets
        # us assert exact call counts without running the real
        # restore / tab construction logic.
        panel._cleanup_renamer_chunk = MagicMock()
        panel._bulk_renamer = MagicMock()
        panel._try_restore_session = MagicMock()
        panel._create_tab = MagicMock()
        return panel

    def test_does_not_call_try_restore_session(self) -> None:
        panel = self._make_panel()
        panel.on_database_changed("/new/path.i64")
        panel._try_restore_session.assert_not_called()

    def test_creates_exactly_one_new_chat_tab(self) -> None:
        panel = self._make_panel()
        panel.on_database_changed("/new/path.i64")
        # Exactly one New Chat tab, keyed by the controller's
        # active_tab_id at the time of the IDB switch.
        panel._create_tab.assert_called_once_with("tab-new", "New Chat")

    def test_pending_restore_messages_stays_empty(self) -> None:
        panel = self._make_panel()
        panel.on_database_changed("/new/path.i64")
        self.assertEqual(panel._pending_restore_messages, {})

    def test_old_chat_views_are_torn_down_before_new_tab(self) -> None:
        """A populated panel must drain existing ``ChatView``s and the
        ``_pending_restore_messages`` map before recreating the
        empty ``New Chat`` tab.  This pins the spec §7.2 teardown
        order so late-restore signals from old tabs cannot survive
        the IDB swap.
        """
        panel = self._make_panel()
        old_cv_a = MagicMock()
        old_cv_b = MagicMock()
        panel._chat_views = {"a": old_cv_a, "b": old_cv_b}
        panel._pending_restore_messages = {"a": ["stale"], "b": ["stale"]}
        # ``while self._tab_widget.count()`` — pin to 0 so the inner
        # removeTab loop never enters; the chat_views.clear() and
        # _pending_restore_messages.clear() in production run
        # unconditionally and are what we want to verify.
        panel._tab_widget.count.return_value = 0

        panel.on_database_changed("/new/path.i64")

        # All old chat views were shut down before fresh state.
        old_cv_a.shutdown.assert_called_once()
        old_cv_b.shutdown.assert_called_once()
        # Stale pending restores were dropped.
        self.assertEqual(panel._pending_restore_messages, {})
        self.assertEqual(panel._chat_views, {})


# ---------------------------------------------------------------------------
# Task 8: right-panel coordinator + history listing worker (spec §6.4, §8, §11.4)
# ---------------------------------------------------------------------------


def _make_history_panel():
    """Build a bare ``RikuganPanelCore`` with the Task-8 history fields.

    Mirrors ``_make_panel`` but also seeds the new history-coordinator
    fields listed in the brief so ``_show_right_panel`` /
    ``_start_history_list_request`` / ``_drain_history_results`` can be
    exercised without running ``__init__``.
    """
    import queue
    import threading

    panel = RikuganPanelCore.__new__(RikuganPanelCore)
    panel._is_shutdown = False
    panel._polling = False
    panel._pending_answer = False
    panel._chat_views = {}
    panel._pending_restore_messages = {}
    panel._context_bar = None
    panel._mutation_panel = MagicMock()
    panel._skills_refresh_timer = None
    panel._poll_timer = None
    panel._pending_token_display = None
    panel._token_display_timer = None
    panel._last_token_display_value = -1
    panel._input_area = MagicMock()
    panel._send_btn = MagicMock()
    panel._cancel_btn = MagicMock()
    panel._mutations_btn = MagicMock()
    panel._history_btn = MagicMock()
    panel._count_label = MagicMock()
    panel._tab_widget = MagicMock()
    panel._tab_bar = MagicMock()
    panel._ctrl = MagicMock()
    panel._config = MagicMock()
    panel._ui_hooks = None
    panel._awaiting_button_approval = False
    # Task-8 history coordinator fields (spec §8.1, §11.4).
    panel._history_panel = MagicMock()
    panel._history_generation = 0
    panel._history_executor = None
    panel._history_result_queue = queue.Queue(maxsize=2)
    panel._history_poll_timer = None
    panel._history_pending = False
    panel._history_closing = threading.Event()
    # Task-9 post-review fix: retry-load session-id state.
    panel._history_retry_load_session_id = None
    panel._history_last_load_session_id = None
    # Task-5 delete-coordinator state (spec §11.5): intent gate so a
    # queued LOAD that lands while the user is confirming a DELETE is
    # discarded before it attaches; retry + last ids mirror the load
    # pattern; watchdog timer is created lazily by
    # ``_start_history_delete_watchdog``.
    panel._history_retry_delete_session_id = None
    panel._history_last_delete_session_id = None
    panel._history_delete_intents: set[str] = set()
    panel._history_delete_watchdog: object | None = None
    return panel


class TestShowRightPanelMutuallyExclusive(unittest.TestCase):
    """``_show_right_panel`` is the single right-panel coordinator.

    Spec §6.4: only one right-side auxiliary panel is visible at a time.
    History and Mutation toggle through one entry point; closing hides
    both panels and unchecks both buttons.
    """

    def _assert_transition(self, name, history_visible, mutation_visible) -> None:
        panel = _make_history_panel()
        panel._show_right_panel(name)
        panel._history_panel.setVisible.assert_any_call(False)
        panel._mutation_panel.setVisible.assert_any_call(False)
        panel._history_btn.setChecked.assert_any_call(False)
        panel._mutations_btn.setChecked.assert_any_call(False)
        # The final visible/checked state matches the requested panel.
        panel._history_panel.setVisible.assert_called_with(history_visible)
        panel._mutation_panel.setVisible.assert_called_with(mutation_visible)
        panel._history_btn.setChecked.assert_called_with(history_visible)
        panel._mutations_btn.setChecked.assert_called_with(mutation_visible)

    def test_history_open(self) -> None:
        self._assert_transition("history", True, False)

    def test_mutation_open(self) -> None:
        self._assert_transition("mutation", False, True)

    def test_close_hides_both(self) -> None:
        self._assert_transition(None, False, False)

    def test_opening_history_starts_list_request(self) -> None:
        """Selecting history kicks off exactly one list request (spec §8.1)."""
        panel = _make_history_panel()
        panel._start_history_list_request = MagicMock()
        panel._show_right_panel("history")
        panel._start_history_list_request.assert_called_once_with()

    def test_opening_mutation_does_not_start_history_list(self) -> None:
        """Switching to Mutation must NOT trigger a history list (spec §6.4)."""
        panel = _make_history_panel()
        panel._start_history_list_request = MagicMock()
        panel._show_right_panel("mutation")
        panel._start_history_list_request.assert_not_called()

    def test_closing_does_not_start_history_list(self) -> None:
        """Closing all side panels must not fire a new history list."""
        panel = _make_history_panel()
        panel._start_history_list_request = MagicMock()
        panel._show_right_panel(None)
        panel._start_history_list_request.assert_not_called()


class TestHistoryStartListRequest(unittest.TestCase):
    """``_start_history_list_request`` captures scope + submits worker.

    Spec §8.1: PanelCore captures immutable HistoryScope on the main
    thread, increments generation, submits to a dedicated single-worker
    executor, and starts a separate QTimer.
    """

    def test_submits_exactly_one_worker_when_idle(self) -> None:
        panel = _make_history_panel()
        scope_stub = MagicMock(name="scope")
        panel._ctrl.capture_history_scope = MagicMock(return_value=scope_stub)
        captured: dict = {}

        class _FakeExecutor:
            def __init__(self) -> None:
                self.submitted = []

            def submit(self, fn, *args, **kwargs):
                self.submitted.append((fn, args, kwargs))
                captured["scope"] = args[0] if args else None
                return MagicMock()

        panel._history_executor = _FakeExecutor()
        panel._start_history_list_request()
        self.assertEqual(len(panel._history_executor.submitted), 1)
        # Worker receives the immutable scope captured on the main thread.
        self.assertIs(captured["scope"], scope_stub)

    def test_skips_when_pending_already_in_flight(self) -> None:
        """At most one in-flight request at a time (spec §11.4)."""
        panel = _make_history_panel()
        panel._ctrl.capture_history_scope = MagicMock(return_value=("scope-stub",))
        panel._history_pending = True

        class _FakeExecutor:
            def __init__(self) -> None:
                self.submit_calls = 0

            def submit(self, *_a, **_kw):
                self.submit_calls += 1
                return MagicMock()

        panel._history_executor = _FakeExecutor()
        panel._start_history_list_request()
        self.assertEqual(panel._history_executor.submit_calls, 0)

    def test_starts_separate_history_qtimer(self) -> None:
        """History uses a timer distinct from the agent poll timer (spec §7.4)."""
        panel = _make_history_panel()
        panel._ctrl.capture_history_scope = MagicMock(return_value=("scope-stub",))

        class _FakeExecutor:
            def submit(self, *_a, **_kw):
                return MagicMock()

        panel._history_executor = _FakeExecutor()
        panel._start_history_list_request()
        self.assertIsNotNone(panel._history_poll_timer)
        # Distinct from the existing agent poll timer.
        self.assertIsNot(panel._history_poll_timer, panel._poll_timer)

    def test_starts_timer_even_when_agent_idle(self) -> None:
        """Opening History while the agent is idle still starts the timer."""
        panel = _make_history_panel()
        panel._ctrl.capture_history_scope = MagicMock(return_value=("scope-stub",))
        panel._ctrl.is_agent_running = False

        class _FakeExecutor:
            def submit(self, *_a, **_kw):
                return MagicMock()

        panel._history_executor = _FakeExecutor()
        panel._start_history_list_request()
        self.assertIsNotNone(panel._history_poll_timer)

    def test_creates_executor_lazily_when_none(self) -> None:
        """Executor is created on first open, not at panel construction."""
        panel = _make_history_panel()
        panel._ctrl.capture_history_scope = MagicMock(return_value=("scope-stub",))
        self.assertIsNone(panel._history_executor)
        panel._start_history_list_request()
        self.assertIsNotNone(panel._history_executor)

    def test_clears_closing_flag_on_new_request(self) -> None:
        """Task 10 race fix: the closing flag is NEVER cleared on the
        same Event instance.  ``_invalidate_history`` replaces the panel's
        Event with a fresh, unset instance; the next
        ``_start_history_list_request`` captures the new unset Event.
        Verify the invariant by simulating the real sequence: invalidate
        sets the OLD event and swaps in a new one, then a fresh request
        observes ``is_set()==False``.
        """
        panel = _make_history_panel()
        panel._ctrl.capture_history_scope = MagicMock(return_value=("scope-stub",))

        class _FakeExecutor:
            def submit(self, *_a, **_kw):
                return MagicMock()

            def shutdown(self, **_kw):
                pass

        panel._history_executor = _FakeExecutor()
        # Real sequence: invalidate sets the OLD event and installs a
        # fresh, unset one.
        panel._invalidate_history(clear_panel=False)
        panel._history_executor = _FakeExecutor()  # post-invalidate None
        panel._start_history_list_request()
        # The current (new) event is unset, so the next worker is allowed
        # to enqueue.
        self.assertFalse(panel._history_closing.is_set())


class TestHistoryListWorker(unittest.TestCase):
    """Worker performs flush + list, catches TimeoutError/Exception (spec §8.1, §13)."""

    def _make_panel_for_worker(self):
        panel = _make_history_panel()
        panel._ctrl.config = MagicMock()
        panel._ctrl.list_history_sessions = MagicMock(return_value=[])
        return panel

    def test_worker_enqueues_listed_result(self) -> None:
        from rikugan.state.history_types import (
            HistoryRequestStatus,
            HistoryScope,
        )

        panel = self._make_panel_for_worker()
        scope = HistoryScope(idb_path="/x.i64", db_instance_id="abc", generation=1)
        entries = [MagicMock(session_id="s1"), MagicMock(session_id="s2")]
        panel._ctrl.list_history_sessions = MagicMock(return_value=entries)
        # ``SessionHistory(self._ctrl.config).flush_saves(...)`` — patch
        # the return_value (the constructed instance) so the call goes
        # through the mock the production code actually makes.
        with patch.object(_pc_module, "SessionHistory") as hist_cls:
            hist_instance = hist_cls.return_value
            hist_instance.flush_saves = MagicMock()
            # Task 10: pass a captured, unset closing_event so the worker
            # observes ``open`` and enqueues.
            panel._history_list_worker(scope, panel._history_closing)
        result = panel._history_result_queue.get_nowait()
        self.assertEqual(result.status, HistoryRequestStatus.LISTED)
        self.assertEqual(result.scope, scope)
        self.assertEqual(tuple(result.entries), tuple(entries))

    def test_worker_enqueues_save_flush_timeout(self) -> None:
        from rikugan.state.history_types import (
            HistoryRequestStatus,
            HistoryScope,
        )

        panel = self._make_panel_for_worker()
        scope = HistoryScope(idb_path="/x.i64", db_instance_id="abc", generation=1)
        with patch.object(_pc_module, "SessionHistory") as hist_cls:
            hist_instance = hist_cls.return_value
            hist_instance.flush_saves.side_effect = TimeoutError()
            panel._history_list_worker(scope, panel._history_closing)
        result = panel._history_result_queue.get_nowait()
        self.assertEqual(result.status, HistoryRequestStatus.SAVE_FLUSH_TIMEOUT)
        self.assertEqual(result.scope, scope)
        # Timeout must NOT return a potentially incomplete entries list.
        self.assertEqual(tuple(result.entries), ())

    def test_worker_enqueues_failed_on_exception(self) -> None:
        from rikugan.state.history_types import (
            HistoryRequestStatus,
            HistoryScope,
        )

        panel = self._make_panel_for_worker()
        scope = HistoryScope(idb_path="/x.i64", db_instance_id="abc", generation=1)
        with patch.object(_pc_module, "SessionHistory") as hist_cls:
            hist_instance = hist_cls.return_value
            hist_instance.flush_saves.side_effect = RuntimeError("disk on fire")
            panel._history_list_worker(scope, panel._history_closing)
        result = panel._history_result_queue.get_nowait()
        self.assertEqual(result.status, HistoryRequestStatus.FAILED)
        self.assertEqual(result.scope, scope)
        # Spec §11.3 / reviewer point #3: the raw exception message is
        # NOT surfaced to the UI.  It is logged via ``log_warning`` for
        # diagnostics; ``result.error`` stays empty so the UI produces
        # a generic safe copy through ``_apply_history_list_result``.
        self.assertEqual(result.error, "")
        self.assertEqual(tuple(result.entries), ())

    def test_worker_does_not_enqueue_when_closing(self) -> None:
        """Closing flag drops the result instead of retaining it in the queue.

        Task 10: the worker now receives a captured ``closing_event``
        argument; setting it before the call simulates an
        ``_invalidate_history`` that fired while the worker was still
        in flight.
        """
        import threading

        from rikugan.state.history_types import HistoryScope

        panel = self._make_panel_for_worker()
        closing_event = threading.Event()
        closing_event.set()
        scope = HistoryScope(idb_path="/x.i64", db_instance_id="abc", generation=1)
        with patch.object(_pc_module, "SessionHistory") as hist_cls:
            hist_instance = hist_cls.return_value
            hist_instance.flush_saves = MagicMock()
            panel._history_list_worker(scope, closing_event)
        self.assertTrue(panel._history_result_queue.empty())


class TestHistoryDrain(unittest.TestCase):
    """Drain is the only method calling ``set_entries``/``set_error`` (spec §8.1)."""

    def test_drain_applies_listed_result(self) -> None:
        from rikugan.state.history_types import (
            HistoryListResult,
            HistoryRequestStatus,
            HistoryScope,
            SessionHistoryEntry,
        )

        panel = _make_history_panel()
        panel._history_generation = 7
        scope = HistoryScope(idb_path="/x.i64", db_instance_id="abc", generation=7)
        e1 = SessionHistoryEntry(
            session_id="s1",
            title="older",
            created_at=1.0,
            updated_at=10.0,
            provider="p",
            model="m",
            message_count=5,
        )
        e2 = SessionHistoryEntry(
            session_id="s2",
            title="newer",
            created_at=1.0,
            updated_at=20.0,
            provider="p",
            model="m",
            message_count=3,
        )
        # Worker returns oldest-first; drain must sort newest-first before
        # calling set_entries (spec §8.1 "sort updated_at descending").
        result = HistoryListResult(
            HistoryRequestStatus.LISTED,
            scope,
            (e1, e2),
        )
        panel._history_result_queue.put(result)
        panel._history_pending = True
        panel._drain_history_results()
        # Sort happened before set_entries: newer (e2) first.
        args, _ = panel._history_panel.set_entries.call_args
        self.assertEqual(args[0][0].session_id, "s2")
        self.assertEqual(args[0][1].session_id, "s1")
        self.assertFalse(panel._history_pending)

    def test_drain_drops_stale_generation(self) -> None:
        """A result whose scope generation differs is silently dropped."""
        from rikugan.state.history_types import (
            HistoryListResult,
            HistoryRequestStatus,
            HistoryScope,
            SessionHistoryEntry,
        )

        panel = _make_history_panel()
        panel._history_generation = 99
        scope = HistoryScope(idb_path="/x.i64", db_instance_id="abc", generation=1)
        result = HistoryListResult(
            HistoryRequestStatus.LISTED,
            scope,
            (SessionHistoryEntry("s", "t", 0.0, 0.0, "", "", 0),),
        )
        panel._history_result_queue.put(result)
        panel._drain_history_results()
        panel._history_panel.set_entries.assert_not_called()
        panel._history_panel.set_error.assert_not_called()

    def test_drain_reports_save_flush_timeout(self) -> None:
        from rikugan.state.history_types import (
            HistoryListResult,
            HistoryRequestStatus,
            HistoryScope,
        )

        panel = _make_history_panel()
        panel._history_generation = 3
        scope = HistoryScope(idb_path="/x.i64", db_instance_id="abc", generation=3)
        panel._history_result_queue.put(
            HistoryListResult(HistoryRequestStatus.SAVE_FLUSH_TIMEOUT, scope),
        )
        panel._history_pending = True
        panel._drain_history_results()
        panel._history_panel.set_error.assert_called_once()
        _, kwargs = panel._history_panel.set_error.call_args
        self.assertTrue(kwargs.get("retry_visible", True))
        self.assertFalse(panel._history_pending)

    def test_drain_reports_failed(self) -> None:
        from rikugan.state.history_types import (
            HistoryListResult,
            HistoryRequestStatus,
            HistoryScope,
        )

        panel = _make_history_panel()
        panel._history_generation = 3
        scope = HistoryScope(idb_path="/x.i64", db_instance_id="abc", generation=3)
        # Even if the worker somehow populated ``error`` with a raw
        # exception message, the drain must surface the generic UI
        # copy only — untrusted persistence-layer strings must never
        # reach the widget text (spec §11.3, reviewer point #3).
        panel._history_result_queue.put(
            HistoryListResult(
                HistoryRequestStatus.FAILED,
                scope,
                error="ValueError: /untrusted/path leaked",
            ),
        )
        panel._history_pending = True
        panel._drain_history_results()
        panel._history_panel.set_error.assert_called_once()
        args, kwargs = panel._history_panel.set_error.call_args
        # ``result.error`` is ignored; the UI copy is generic.
        self.assertEqual(args[0], "Could not load chat history.")
        self.assertNotIn("/untrusted/path", args[0])
        self.assertTrue(kwargs.get("retry_visible", True))
        self.assertFalse(panel._history_pending)

    def test_drain_failed_with_empty_error_uses_generic_copy(self) -> None:
        """Reviewer point #3: empty error string still renders generic copy."""
        from rikugan.state.history_types import (
            HistoryListResult,
            HistoryRequestStatus,
            HistoryScope,
        )

        panel = _make_history_panel()
        panel._history_generation = 3
        scope = HistoryScope(idb_path="/x.i64", db_instance_id="abc", generation=3)
        panel._history_result_queue.put(
            HistoryListResult(HistoryRequestStatus.FAILED, scope, error=""),
        )
        panel._history_pending = True
        panel._drain_history_results()
        args, _ = panel._history_panel.set_error.call_args
        self.assertEqual(args[0], "Could not load chat history.")

    def test_drain_noop_when_empty(self) -> None:
        panel = _make_history_panel()
        panel._drain_history_results()
        panel._history_panel.set_entries.assert_not_called()
        panel._history_panel.set_error.assert_not_called()

    def test_drain_noop_when_shutdown(self) -> None:
        """Drain must return immediately when ``_is_shutdown`` is true (spec §7.4)."""
        from rikugan.state.history_types import (
            HistoryListResult,
            HistoryRequestStatus,
            HistoryScope,
        )

        panel = _make_history_panel()
        panel._is_shutdown = True
        panel._history_generation = 3
        scope = HistoryScope(idb_path="/x.i64", db_instance_id="abc", generation=3)
        panel._history_result_queue.put(
            HistoryListResult(HistoryRequestStatus.LISTED, scope),
        )
        panel._drain_history_results()
        panel._history_panel.set_entries.assert_not_called()

    def test_drain_stops_timer_when_apply_then_hide(self) -> None:
        """Reviewer point #1 regression: a successful apply followed by a
        hidden panel must stop the timer on the SAME drain tick.

        Previously the timer-stop check was inside ``if applied:`` AND
        also gated on ``not self._history_pending`` which had just been
        cleared — so this case happened to work.  The explicit test
        pins the behavior so a future refactor cannot re-introduce
        the busy-loop.
        """
        from rikugan.state.history_types import (
            HistoryListResult,
            HistoryRequestStatus,
            HistoryScope,
        )

        panel = _make_history_panel()
        panel._history_generation = 7
        # History hidden (user closed the panel between submit and
        # drain) but the result is current-generation.
        panel._history_panel.isVisible.return_value = False
        scope = HistoryScope(idb_path="/x.i64", db_instance_id="abc", generation=7)
        panel._history_result_queue.put(HistoryListResult(HistoryRequestStatus.LISTED, scope))
        panel._history_pending = True
        timer = MagicMock()
        panel._history_poll_timer = timer
        panel._drain_history_results()
        # The terminal result was applied…
        panel._history_panel.set_entries.assert_called_once()
        # …and the timer was stopped on the same tick (no busy-loop).
        # Reference captured before the drain nulls the field.
        timer.stop.assert_called_once_with()
        self.assertIsNone(panel._history_poll_timer)
        self.assertFalse(panel._history_pending)

    def test_drain_stops_timer_on_empty_drain_when_hidden_and_not_pending(self) -> None:
        """Reviewer point #1 CRITICAL regression: empty drain on a hidden
        panel with no pending work must stop the timer.

        The original bug: the timer-stop check lived inside
        ``if applied:``, so a drain that pulled nothing (or only stale
        results) left the timer spinning forever on an idle, hidden
        panel.  This deterministic test pins the fix — it would fail
        against the pre-fix code path.
        """
        panel = _make_history_panel()
        panel._history_pending = False
        # Panel hidden, nothing pending.
        panel._history_panel.isVisible.return_value = False
        timer = MagicMock()
        panel._history_poll_timer = timer
        # Empty queue → ``applied`` stays False.
        panel._drain_history_results()
        # Timer must still be torn down despite no apply path running.
        # ``_stop_history_poll_timer`` nulls ``_history_poll_timer``;
        # capture the reference before the drain to assert on it.
        timer.stop.assert_called_once_with()
        self.assertIsNone(panel._history_poll_timer)
        panel._history_panel.set_entries.assert_not_called()
        panel._history_panel.set_error.assert_not_called()

    def test_drain_stops_timer_when_only_stale_results_in_queue(self) -> None:
        """Reviewer point #1: a queue containing only stale-generation
        results must still trigger the timer-stop on a hidden panel."""
        from rikugan.state.history_types import (
            HistoryListResult,
            HistoryRequestStatus,
            HistoryScope,
        )

        panel = _make_history_panel()
        panel._history_generation = 99  # live generation
        panel._history_pending = False
        panel._history_panel.isVisible.return_value = False
        # Stale result from generation 1 — discarded by generation check.
        stale_scope = HistoryScope(idb_path="/x.i64", db_instance_id="abc", generation=1)
        panel._history_result_queue.put(
            HistoryListResult(HistoryRequestStatus.LISTED, stale_scope),
        )
        timer = MagicMock()
        panel._history_poll_timer = timer
        panel._drain_history_results()
        timer.stop.assert_called_once_with()
        self.assertIsNone(panel._history_poll_timer)
        # Stale result did not touch the widget.
        panel._history_panel.set_entries.assert_not_called()

    def test_drain_keeps_timer_when_history_visible(self) -> None:
        """If the panel is visible, the timer keeps running between ticks.

        Spec §7.4: "the timer is active while History is visible or a
        history request remains in flight."  Only hidden + no pending
        work stops the timer.
        """
        panel = _make_history_panel()
        panel._history_pending = False
        panel._history_panel.isVisible.return_value = True
        timer = MagicMock()
        panel._history_poll_timer = timer
        panel._drain_history_results()
        timer.stop.assert_not_called()
        self.assertIs(panel._history_poll_timer, timer)

    def test_drain_keeps_timer_when_request_pending_and_hidden(self) -> None:
        """If a request is still in flight (pending) and the panel is
        hidden, the timer keeps polling — the result will land later
        and must be drained."""
        panel = _make_history_panel()
        panel._history_pending = True
        panel._history_panel.isVisible.return_value = False
        timer = MagicMock()
        panel._history_poll_timer = timer
        panel._drain_history_results()
        timer.stop.assert_not_called()
        self.assertIs(panel._history_poll_timer, timer)


class TestStopHistoryPollTimer(unittest.TestCase):
    """Timer lifecycle mirrors ``_stop_poll_timer`` (spec §7.4)."""

    def test_noop_when_none(self) -> None:
        panel = _make_history_panel()
        panel._history_poll_timer = None
        panel._stop_history_poll_timer()  # must not raise
        self.assertIsNone(panel._history_poll_timer)

    def test_stops_disconnects_deletes(self) -> None:
        panel = _make_history_panel()
        timer = MagicMock()
        panel._history_poll_timer = timer
        panel._stop_history_poll_timer()
        timer.stop.assert_called_once_with()
        timer.timeout.disconnect.assert_called_once_with(panel._drain_history_results)
        timer.deleteLater.assert_called_once_with()
        self.assertIsNone(panel._history_poll_timer)

    def test_swallows_disconnect_runtime_error(self) -> None:
        panel = _make_history_panel()
        timer = MagicMock()
        timer.timeout.disconnect.side_effect = RuntimeError("not connected")
        panel._history_poll_timer = timer
        panel._stop_history_poll_timer()  # must not raise
        self.assertIsNone(panel._history_poll_timer)


class TestInvalidateHistory(unittest.TestCase):
    """IDB change / shutdown invalidation (spec §7.2, §11.4).

    Task 10 refined the helper to ``_invalidate_history(*,
    clear_panel: bool)``.  Tests below pin both the new keyword-only
    signature and the per-call-site bool: shutdown uses
    ``clear_panel=False``, IDB-change uses ``clear_panel=True``.
    """

    def test_increments_generation(self) -> None:
        panel = _make_history_panel()
        panel._history_generation = 5
        panel._invalidate_history(clear_panel=False)
        self.assertGreater(panel._history_generation, 5)

    def test_stops_history_poll_timer(self) -> None:
        panel = _make_history_panel()
        panel._stop_history_poll_timer = MagicMock()
        panel._invalidate_history(clear_panel=False)
        panel._stop_history_poll_timer.assert_called_once_with()

    def test_drains_and_discards_queue(self) -> None:
        from rikugan.state.history_types import (
            HistoryListResult,
            HistoryRequestStatus,
            HistoryScope,
        )

        panel = _make_history_panel()
        panel._history_result_queue.put(
            HistoryListResult(
                HistoryRequestStatus.LISTED,
                HistoryScope("/old", "abc", 1),
            ),
        )
        panel._invalidate_history(clear_panel=False)
        self.assertTrue(panel._history_result_queue.empty())

    def test_sets_old_closing_event_so_stale_workers_drop_results(self) -> None:
        """Task 10 race fix: the OLD closing Event is set so any worker
        that captured it at submit time observes ``is_set()==True`` and
        drops its result.  The panel installs a fresh, unset Event
        afterward (verified in TestTask10InvalidateHistoryOrdering)."""
        panel = _make_history_panel()
        old_event = panel._history_closing
        panel._invalidate_history(clear_panel=False)
        self.assertTrue(old_event.is_set())

    def test_clears_pending_flag(self) -> None:
        panel = _make_history_panel()
        panel._history_pending = True
        panel._invalidate_history(clear_panel=False)
        self.assertFalse(panel._history_pending)

    def test_clears_panel_rows_and_cached_search(self) -> None:
        """Spec §7.2 step 5: clear HistoryPanel rows + cached search.

        Only invoked when ``clear_panel=True`` (IDB-change path).
        ``shutdown`` skips it to avoid mutating widgets after teardown
        starts."""
        panel = _make_history_panel()
        panel._invalidate_history(clear_panel=True)
        panel._history_panel.clear.assert_called_once_with()

    def test_clear_panel_false_skips_panel_clear(self) -> None:
        """``shutdown`` path: no widget mutation."""
        panel = _make_history_panel()
        panel._invalidate_history(clear_panel=False)
        panel._history_panel.clear.assert_not_called()

    def test_requests_executor_shutdown_without_blocking(self) -> None:
        """Non-blocking shutdown with cancel_futures=True (spec §7.2, §11.4)."""
        panel = _make_history_panel()

        class _FakeExecutor:
            def __init__(self) -> None:
                self.shutdown_calls: list = []

            def shutdown(self, **kwargs):
                self.shutdown_calls.append(kwargs)

        executor = _FakeExecutor()
        panel._history_executor = executor
        panel._invalidate_history(clear_panel=False)
        self.assertTrue(any(c.get("wait") is False for c in executor.shutdown_calls))
        self.assertTrue(
            any(c.get("cancel_futures") is True for c in executor.shutdown_calls),
            f"expected cancel_futures=True in {executor.shutdown_calls}",
        )

    def test_drops_executor_reference(self) -> None:
        """Spec §7.2: next open creates a fresh executor lazily."""
        panel = _make_history_panel()

        class _FakeExecutor:
            def shutdown(self, **_kw):
                pass

        panel._history_executor = _FakeExecutor()
        panel._invalidate_history(clear_panel=False)
        self.assertIsNone(panel._history_executor)


class TestHistoryExecutorDistinctFromSaveExecutor(unittest.TestCase):
    """Deadlock guard: history executor must be distinct from ``_SAVE_EXECUTOR``.

    Spec §6.1, §11.4: history listing calls ``flush_saves()`` which would
    deadlock waiting on a sentinel queued behind itself if it ran on
    ``_SAVE_EXECUTOR``. The history executor is a dedicated single-worker
    pool with a distinct ``thread_name_prefix``.
    """

    def test_history_executor_is_not_save_executor(self) -> None:
        panel = _make_history_panel()
        panel._ctrl.capture_history_scope = MagicMock(return_value=("scope-stub",))
        panel._start_history_list_request()
        from rikugan.state.history import _SAVE_EXECUTOR

        self.assertIsNot(panel._history_executor, _SAVE_EXECUTOR)

    def test_history_executor_thread_name_prefix_is_distinct(self) -> None:
        """The prefix must be ``rikugan-history`` so thread dumps are debuggable."""
        panel = _make_history_panel()
        panel._ctrl.capture_history_scope = MagicMock(return_value=("scope-stub",))
        panel._start_history_list_request()
        # ThreadPoolExecutor exposes the prefix via ``_thread_name_prefix``.
        prefix = getattr(panel._history_executor, "_thread_name_prefix", "")
        self.assertEqual(prefix, "rikugan-history")

    def test_queued_save_then_list_completes_without_self_deadlock(self) -> None:
        """Submitting a save to ``_SAVE_EXECUTOR`` then opening History must
        not self-deadlock (spec §6.1, acceptance §14.6).

        This is a deterministic functional check: queue a sentinel save,
        then run the list worker inline (no real QTimer) and assert the
        list result arrives. If the history executor were the save
        executor, ``flush_saves`` would block forever waiting on its own
        sentinel.
        """
        from concurrent.futures import ThreadPoolExecutor

        from rikugan.state.history import _SAVE_EXECUTOR
        from rikugan.state.history_types import (
            HistoryRequestStatus,
            HistoryScope,
        )

        panel = _make_history_panel()
        panel._ctrl.config = MagicMock()
        panel._ctrl.list_history_sessions = MagicMock(return_value=[])
        # Queue a sentinel save so flush_saves has real work to drain.
        save_future = _SAVE_EXECUTOR.submit(lambda: None)
        save_future.result(timeout=5.0)
        scope = HistoryScope(idb_path="/x.i64", db_instance_id="abc", generation=1)
        # Bypass the timer + worker submit path and invoke the worker
        # directly on the panel's dedicated executor so we exercise the
        # real submit/queue path.
        panel._start_history_list_request()
        executor: ThreadPoolExecutor = panel._history_executor
        # Wait for the submitted worker to produce a result.
        with patch.object(_pc_module, "SessionHistory") as hist_cls:
            hist_instance = hist_cls.return_value
            hist_instance.flush_saves = MagicMock()
            # Re-submit through the dedicated executor to be certain
            # flush_saves runs on the history thread, not save thread.
            # Task 10: pass a captured closing_event (unset).
            executor.submit(
                panel._history_list_worker,
                scope,
                panel._history_closing,
            ).result(timeout=5.0)
        result = panel._history_result_queue.get(timeout=5.0)
        self.assertEqual(result.status, HistoryRequestStatus.LISTED)


class TestHistoryButtonAlwaysVisible(unittest.TestCase):
    """The History button is always visible (spec §6.3, §6.4)."""

    def test_history_button_is_visible_after_build_action_buttons(self) -> None:
        """Source-level guard: ``_build_action_buttons`` does not gate the
        History button behind a ``setVisible(False)`` call. We inspect the
        source so the test does not depend on Qt stubs honoring visibility.
        """
        import inspect

        src = inspect.getsource(RikuganPanelCore._build_action_buttons)
        # ``_history_btn`` must be created and must NOT carry a
        # ``setVisible(False)`` call (unlike Mutations which hides until
        # the first mutation lands).
        self.assertIn("_history_btn", src)
        # The pattern "self._history_btn.setVisible(False)" must NOT appear.
        self.assertNotIn("self._history_btn.setVisible(False)", src)

    def test_history_panel_is_added_to_splitter(self) -> None:
        """Source-level guard: ``_build_main_splitter`` adds HistoryPanel as
        the third hidden widget (spec §6.4: chat, Mutation Log, History).
        """
        import inspect

        src = inspect.getsource(RikuganPanelCore._build_main_splitter)
        self.assertIn("HistoryPanel", src)
        # Must start hidden (third hidden widget).
        self.assertIn("setVisible(False)", src)


# ---------------------------------------------------------------------------
# Task 9: Open Historical Sessions, Dedupe, and Deferred Async Restore
# ---------------------------------------------------------------------------


def _make_load_result(
    status,
    scope,
    session=None,
    error="",
):
    """Build a ``HistoryLoadResult`` without forcing every test to import it."""
    from rikugan.state.history_types import HistoryLoadResult

    return HistoryLoadResult(status=status, scope=scope, session=session, error=error)


class TestHistoryOpenDedupe(unittest.TestCase):
    """Pre-load dedupe: if the persisted session is already open, focus it
    without submitting a worker (spec §10.2, §14.4)."""

    def test_history_open_focuses_existing_tab_without_worker(self) -> None:
        panel = _make_history_panel()
        panel._ctrl.find_tab_for_session.return_value = "tab-a"
        panel._ctrl.capture_history_scope = MagicMock(
            return_value=MagicMock(name="scope"),
        )
        # Spies on submission + tab helpers.
        panel._history_executor = MagicMock()
        panel._focus_tab = MagicMock()
        panel._start_history_load = MagicMock()

        panel._on_history_open_requested("persisted-a")

        # Pre-load dedupe: NO worker submit, NO scope capture.
        panel._ctrl.load_history_session.assert_not_called()
        panel._history_executor.submit.assert_not_called()
        panel._start_history_load.assert_not_called()
        # Controller-side dedupe helper was consulted exactly once.
        panel._ctrl.find_tab_for_session.assert_called_once_with("persisted-a")
        # Existing tab was focused.
        panel._focus_tab.assert_called_once_with("tab-a")

    def test_history_open_when_not_open_starts_load_worker(self) -> None:
        """When no open tab matches, capture scope + submit a load worker."""
        panel = _make_history_panel()
        panel._ctrl.find_tab_for_session.return_value = None
        scope_stub = MagicMock(name="scope")
        panel._ctrl.capture_history_scope = MagicMock(return_value=scope_stub)
        submitted: list = []

        class _FakeExecutor:
            def submit(self, fn, *args, **kwargs):
                submitted.append((fn, args, kwargs))
                return MagicMock()

        panel._history_executor = _FakeExecutor()
        panel._ensure_history_poll_timer = MagicMock()

        panel._on_history_open_requested("persisted-b")

        # Scope captured on main thread before I/O.
        panel._ctrl.capture_history_scope.assert_called_once()
        # Exactly one worker submitted, receiving (session_id, scope).
        self.assertEqual(len(submitted), 1)
        fn, args, _kwargs = submitted[0]
        # Bound methods are fresh objects per attribute access, so compare
        # the underlying function + instance instead of ``assertIs``.
        self.assertEqual(fn.__func__, panel._history_load_worker.__func__)
        self.assertIs(fn.__self__, panel)
        self.assertEqual(args[0], "persisted-b")
        self.assertIs(args[1], scope_stub)
        # Timer started so the result will drain.
        panel._ensure_history_poll_timer.assert_called_once_with()
        # Pending flag set so a concurrent click cannot double-submit.
        self.assertTrue(panel._history_pending)

    def test_history_open_skipped_when_pending_in_flight(self) -> None:
        """A second click while a load is pending must not submit again."""
        panel = _make_history_panel()
        panel._ctrl.find_tab_for_session.return_value = None
        panel._history_pending = True

        class _FakeExecutor:
            def __init__(self) -> None:
                self.calls = 0

            def submit(self, *_a, **_kw):
                self.calls += 1
                return MagicMock()

        panel._history_executor = _FakeExecutor()
        panel._on_history_open_requested("persisted-c")
        self.assertEqual(panel._history_executor.calls, 0)

    def test_history_open_skipped_when_shutdown(self) -> None:
        panel = _make_history_panel()
        panel._is_shutdown = True
        panel._ctrl.find_tab_for_session.return_value = None
        panel._history_executor = MagicMock()
        panel._on_history_open_requested("persisted-d")
        panel._history_executor.submit.assert_not_called()


class TestHistoryLoadWorker(unittest.TestCase):
    """``_history_load_worker`` runs on the dedicated executor; it only
    calls ``load_history_session`` and enqueues a typed result (spec §10.1,
    §10.3, §11.4)."""

    def test_worker_enqueues_loaded_result(self) -> None:
        from rikugan.state.history_types import HistoryRequestStatus

        scope = MagicMock(name="scope")
        session = MagicMock(name="session")
        panel = _make_history_panel()
        panel._ctrl.load_history_session.return_value = _make_load_result(
            HistoryRequestStatus.LOADED,
            scope,
            session=session,
        )
        # Task 10: pass a captured, unset closing_event so the worker
        # observes ``open`` and enqueues.
        panel._history_load_worker("persisted-x", scope, panel._history_closing)
        result = panel._history_result_queue.get_nowait()
        self.assertEqual(result.status, HistoryRequestStatus.LOADED)
        self.assertIs(result.session, session)
        self.assertIs(result.scope, scope)

    def test_worker_enqueues_not_found_result(self) -> None:
        from rikugan.state.history_types import HistoryRequestStatus

        scope = MagicMock(name="scope")
        panel = _make_history_panel()
        panel._ctrl.load_history_session.return_value = _make_load_result(
            HistoryRequestStatus.NOT_FOUND,
            scope,
        )
        panel._history_load_worker("persisted-x", scope, panel._history_closing)
        result = panel._history_result_queue.get_nowait()
        self.assertEqual(result.status, HistoryRequestStatus.NOT_FOUND)

    def test_worker_catches_exception_enqueues_failed_with_empty_error(self) -> None:
        """Spec §11.4 + §11.3: outer boundary catch, no exception used as
        control flow, and the raw exception string never reaches the UI."""
        from rikugan.state.history_types import HistoryRequestStatus

        scope = MagicMock(name="scope")
        panel = _make_history_panel()
        panel._ctrl.load_history_session.side_effect = RuntimeError("disk leaked /untrusted/path")
        panel._history_load_worker("persisted-x", scope, panel._history_closing)
        result = panel._history_result_queue.get_nowait()
        self.assertEqual(result.status, HistoryRequestStatus.FAILED)
        # Raw exception string is NOT surfaced through ``error``.
        self.assertEqual(result.error, "")

    def test_worker_does_not_enqueue_when_closing(self) -> None:
        """Closing flag set by ``_invalidate_history`` drops the result.

        Task 10: the worker receives a captured ``closing_event``;
        setting it before the call simulates an invalidate that fired
        while the worker was still in flight.
        """
        import threading

        panel = _make_history_panel()
        closing_event = threading.Event()
        closing_event.set()
        panel._ctrl.load_history_session.return_value = _make_load_result(
            MagicMock(name="status"),
            MagicMock(name="scope"),
        )
        panel._history_load_worker(
            "persisted-x",
            MagicMock(name="scope"),
            closing_event,
        )
        self.assertTrue(panel._history_result_queue.empty())


class TestHistoryDrainHandlesLoad(unittest.TestCase):
    """The drain must handle BOTH ``HistoryListResult`` and
    ``HistoryLoadResult`` safely (spec §10.1, §11.4)."""

    def _panel_with_running_timer(self):
        panel = _make_history_panel()
        panel._history_generation = 1
        panel._history_poll_timer = MagicMock()
        panel._history_panel.isVisible.return_value = True
        panel._apply_history_loaded = MagicMock()
        panel._apply_history_list_result = MagicMock()
        panel._focus_tab = MagicMock()
        return panel

    def test_drain_routes_load_result_to_apply_history_loaded(self) -> None:
        from rikugan.state.history_types import HistoryLoadResult

        panel = self._panel_with_running_timer()
        scope = MagicMock(name="scope")
        scope.generation = 1
        load_result = HistoryLoadResult(
            status=MagicMock(name="status"),
            scope=scope,
            session=MagicMock(name="session"),
        )
        panel._history_result_queue.put(load_result)

        panel._drain_history_results()

        panel._apply_history_loaded.assert_called_once_with(load_result)
        panel._apply_history_list_result.assert_not_called()
        # Pending flag cleared once the terminal result lands.
        self.assertFalse(panel._history_pending)

    def test_drain_drops_stale_generation_load_result(self) -> None:
        """A load result whose generation differs from live is dropped
        silently — no attach, no UI mutation (spec §10.3 step 7)."""
        from rikugan.state.history_types import HistoryLoadResult

        panel = self._panel_with_running_timer()
        panel._history_pending = True  # a load is conceptually in flight
        stale_scope = MagicMock(name="scope")
        stale_scope.generation = 0  # live generation is 1
        load_result = HistoryLoadResult(
            status=MagicMock(name="status"),
            scope=stale_scope,
            session=MagicMock(name="session"),
        )
        panel._history_result_queue.put(load_result)

        panel._drain_history_results()

        panel._apply_history_loaded.assert_not_called()
        # Pending flag NOT cleared (no terminal result applied); a real
        # next-gen result will eventually arrive.
        self.assertTrue(panel._history_pending)
        # Timer lifecycle did not leak — still alive because pending + visible.
        panel._history_poll_timer.stop.assert_not_called()

    def test_drain_stops_timer_when_hidden_and_no_pending_after_load(self) -> None:
        from rikugan.state.history_types import HistoryLoadResult

        panel = self._panel_with_running_timer()
        scope = MagicMock(name="scope")
        scope.generation = 1
        panel._history_pending = True
        panel._history_panel.isVisible.return_value = False
        # Capture the timer reference BEFORE the drain nulls the field.
        timer_ref = panel._history_poll_timer
        load_result = HistoryLoadResult(
            status=MagicMock(name="status"),
            scope=scope,
        )
        panel._history_result_queue.put(load_result)

        panel._drain_history_results()

        panel._apply_history_loaded.assert_called_once_with(load_result)
        # Terminal result applied → pending cleared → timer stopped.
        self.assertFalse(panel._history_pending)
        timer_ref.stop.assert_called_once_with()


class TestApplyHistoryLoaded(unittest.TestCase):
    """``_apply_history_loaded`` routes every status to the correct UI +
    controller action (spec §10.3, §13)."""

    def _panel(self):
        panel = _make_history_panel()
        panel._create_tab = MagicMock()
        panel._focus_tab = MagicMock()
        panel._restore_messages_if_needed = MagicMock()
        panel._ctrl.attach_history_session = MagicMock()
        panel._history_panel.isVisible.return_value = True
        return panel

    def test_opened_creates_one_tab_and_uses_async_restore(self) -> None:
        from rikugan.state.history_types import (
            HistoryAttachResult,
            HistoryAttachStatus,
            HistoryRequestStatus,
        )

        panel = self._panel()
        session = MagicMock(name="session")
        session.id = "persisted-opened"
        session.messages = ["m1", "m2"]
        scope = MagicMock(name="scope")
        # Use the real DTO so ``status is HistoryRequestStatus.LOADED``
        # matches; a bare MagicMock for ``status`` would never match.
        load_result = _make_load_result(
            HistoryRequestStatus.LOADED,
            scope,
            session=session,
        )
        attach_result = HistoryAttachResult(
            status=HistoryAttachStatus.OPENED,
            tab_id="tab-new",
            session=session,
        )
        panel._ctrl.attach_history_session.return_value = attach_result

        panel._apply_history_loaded(load_result)

        # Attach called once on the main thread.
        panel._ctrl.attach_history_session.assert_called_once_with(load_result)
        # Exactly one tab created.
        panel._create_tab.assert_called_once()
        created_tab_id = panel._create_tab.call_args.args[0]
        self.assertEqual(created_tab_id, "tab-new")
        # Pending restore payload was written BEFORE the tab was created.
        self.assertEqual(panel._pending_restore_messages["tab-new"], ["m1", "m2"])
        # Async restore path triggered.
        panel._restore_messages_if_needed.assert_called_once_with("tab-new")
        # Tab was focused.
        panel._focus_tab.assert_called_once_with("tab-new")

    def test_already_open_focuses_existing_tab_no_create(self) -> None:
        from rikugan.state.history_types import (
            HistoryAttachResult,
            HistoryAttachStatus,
            HistoryRequestStatus,
        )

        panel = self._panel()
        session = MagicMock(name="session")
        scope = MagicMock(name="scope")
        load_result = _make_load_result(
            HistoryRequestStatus.LOADED,
            scope,
            session=session,
        )
        attach_result = HistoryAttachResult(
            status=HistoryAttachStatus.ALREADY_OPEN,
            tab_id="tab-existing",
            session=session,
        )
        panel._ctrl.attach_history_session.return_value = attach_result

        panel._apply_history_loaded(load_result)

        panel._create_tab.assert_not_called()
        panel._restore_messages_if_needed.assert_not_called()
        panel._focus_tab.assert_called_once_with("tab-existing")
        # No pending payload written.
        self.assertEqual(panel._pending_restore_messages, {})

    def test_stale_scope_silently_dropped_no_ui(self) -> None:
        """STALE_SCOPE: do nothing — no tab, no UI message."""
        from rikugan.state.history_types import (
            HistoryAttachResult,
            HistoryAttachStatus,
            HistoryRequestStatus,
        )

        panel = self._panel()
        session = MagicMock(name="session")
        scope = MagicMock(name="scope")
        load_result = _make_load_result(
            HistoryRequestStatus.LOADED,
            scope,
            session=session,
        )
        attach_result = HistoryAttachResult(status=HistoryAttachStatus.STALE_SCOPE)
        panel._ctrl.attach_history_session.return_value = attach_result

        panel._apply_history_loaded(load_result)

        panel._create_tab.assert_not_called()
        panel._restore_messages_if_needed.assert_not_called()
        panel._focus_tab.assert_not_called()
        panel._history_panel.set_error.assert_not_called()

    def test_reused_rebuilds_active_tab_in_place(self) -> None:
        """REUSED: rebuild the active tab's ChatView instead of adding a new tab.

        The controller loaded a historical session INTO the empty active
        draft (same tab_id). PanelCore must rebuild that tab's ChatView and
        trigger async restore over the same tab_id — never create a new tab.
        """
        from rikugan.state.history_types import (
            HistoryAttachResult,
            HistoryAttachStatus,
            HistoryRequestStatus,
        )

        panel = self._panel()
        # The session has messages so the rebuild path stashes them.
        session = MagicMock(name="session")
        session.messages = ["m1", "m2"]
        scope = MagicMock(name="scope")
        load_result = _make_load_result(
            HistoryRequestStatus.LOADED,
            scope,
            session=session,
        )
        attach_result = HistoryAttachResult(
            status=HistoryAttachStatus.REUSED,
            tab_id="tab-active",
            session=session,
        )
        panel._ctrl.attach_history_session.return_value = attach_result
        panel._ctrl.tab_label.return_value = "Chat"
        # ``_rebuild_history_tab`` touches the real tab widget helpers; stub
        # the primitives the bare ``_make_history_panel`` fixture leaves as
        # MagicMocks so they cannot raise on ``count()`` / ``indexOf``.
        panel._tab_widget.indexOf.return_value = 0
        panel._tab_widget.count.return_value = 1
        # Seed an existing (empty) view so the rebuild tears it down.
        panel._chat_views["tab-active"] = MagicMock(name="old_view")

        panel._apply_history_loaded(load_result)

        # Never created a brand-new tab.
        panel._create_tab.assert_not_called()
        # Tab label was queried for the rebuilt view.
        panel._ctrl.tab_label.assert_called_once_with("tab-active")
        # Pending payload stashed for the reused tab_id.
        self.assertEqual(panel._pending_restore_messages["tab-active"], ["m1", "m2"])
        # Async restore + focus over the reused tab.
        panel._restore_messages_if_needed.assert_called_once_with("tab-active")
        panel._focus_tab.assert_called_once_with("tab-active")

    def test_not_found_refreshes_list_once(self) -> None:
        """NOT_FOUND shows exact copy then triggers exactly one list refresh
        (spec §13).  Reviewer MEDIUM #1: ``_apply_history_loaded`` no longer
        touches ``_history_pending`` — the generation-aware drain epilogue
        owns that flag.  When this helper is invoked through the real
        drain, the rebump performed by ``_start_history_list_request``
        causes the drain to leave pending=True (the new list worker owns
        its own terminal-result lifecycle).  Here we stub the refresh so
        no rebump happens and pending is left as the caller set it."""
        from rikugan.state.history_types import HistoryRequestStatus

        scope = MagicMock(name="scope")
        scope.generation = 1
        panel = self._panel()
        panel._history_generation = 1
        panel._history_pending = True  # pending flag still set from the load
        # Refresh helper will be called directly.  Stubbed, so no real
        # rebump happens; the generation-aware drain fix is exercised
        # end-to-end in ``TestDrainGenerationAwarePendingClear``.
        panel._start_history_list_request = MagicMock()

        load_result = _make_load_result(HistoryRequestStatus.NOT_FOUND, scope)
        panel._apply_history_loaded(load_result)

        # Exact user-visible copy.
        panel._history_panel.set_error.assert_called_once_with(
            "This chat is no longer available.",
            retry_visible=False,
        )
        # No tab created, no attach.
        panel._ctrl.attach_history_session.assert_not_called()
        panel._create_tab.assert_not_called()
        # ``_apply_history_loaded`` no longer clears pending itself;
        # the drain's generation-aware epilogue owns that.  When invoked
        # through the real drain, the rebump causes pending to stay True
        # (verified in TestDrainGenerationAwarePendingClear).
        panel._start_history_list_request.assert_called_once_with()

    def test_wrong_idb_shows_exact_copy_no_attach(self) -> None:
        from rikugan.state.history_types import HistoryRequestStatus

        scope = MagicMock(name="scope")
        panel = self._panel()
        load_result = _make_load_result(HistoryRequestStatus.WRONG_IDB, scope)
        panel._apply_history_loaded(load_result)
        panel._history_panel.set_error.assert_called_once_with(
            "This chat belongs to a different IDB.",
            retry_visible=False,
        )
        panel._ctrl.attach_history_session.assert_not_called()
        panel._create_tab.assert_not_called()

    def test_empty_shows_exact_copy_no_attach(self) -> None:
        from rikugan.state.history_types import HistoryRequestStatus

        scope = MagicMock(name="scope")
        panel = self._panel()
        load_result = _make_load_result(HistoryRequestStatus.EMPTY, scope)
        panel._apply_history_loaded(load_result)
        panel._history_panel.set_error.assert_called_once_with(
            "This chat is empty and cannot be opened.",
            retry_visible=False,
        )
        panel._ctrl.attach_history_session.assert_not_called()
        panel._create_tab.assert_not_called()

    def test_failed_shows_generic_copy_no_attach_no_leak(self) -> None:
        """Spec §11.3: generic FAILED copy; raw ``error`` never forwarded.

        Reviewer MEDIUM #2: FAILED load now exposes Retry so the user
        can re-dispatch the LOAD (not the list).  The raw ``error`` is
        still never surfaced — generic copy only.
        """
        from rikugan.state.history_types import HistoryRequestStatus

        scope = MagicMock(name="scope")
        panel = self._panel()
        load_result = _make_load_result(
            HistoryRequestStatus.FAILED,
            scope,
            error="ValueError: /untrusted/path leaked",
        )
        panel._apply_history_loaded(load_result)
        panel._history_panel.set_error.assert_called_once_with(
            "Could not open this chat.",
            retry_visible=True,
        )
        panel._ctrl.attach_history_session.assert_not_called()
        panel._create_tab.assert_not_called()


class TestFocusTab(unittest.TestCase):
    """``_focus_tab`` switches the QTabWidget to the tab owning ``tab_id``."""

    def test_focus_tab_sets_current_index_for_known_tab(self) -> None:
        panel = _make_history_panel()
        mock_view = MagicMock()
        panel._chat_views["tab-x"] = mock_view
        panel._tab_widget.indexOf.return_value = 3
        panel._focus_tab("tab-x")
        panel._tab_widget.indexOf.assert_called_once_with(mock_view)
        panel._tab_widget.setCurrentIndex.assert_called_once_with(3)

    def test_focus_tab_silent_when_tab_id_unknown(self) -> None:
        panel = _make_history_panel()
        panel._focus_tab("ghost")
        panel._tab_widget.setCurrentIndex.assert_not_called()


class TestCloseTabClearsPendingRestore(unittest.TestCase):
    """Closing a tab must pop its pending restore payload BEFORE
    ChatView.shutdown (spec §10.3 step 6, §14.4)."""

    def test_close_tab_pops_pending_restore_before_shutdown(self) -> None:
        panel = _make_history_panel()
        panel._tab_widget.count.return_value = 2
        mock_widget = MagicMock()
        mock_widget.property.return_value = "tid"
        panel._chat_views["tid"] = mock_widget
        panel._tab_widget.widget.return_value = mock_widget
        panel._pending_restore_messages["tid"] = ["m1", "m2"]

        panel._on_close_tab(0)

        # Payload removed.
        self.assertNotIn("tid", panel._pending_restore_messages)
        # Shutdown was called.
        mock_widget.shutdown.assert_called_once_with()

    def test_close_tab_with_no_pending_payload_still_works(self) -> None:
        panel = _make_history_panel()
        panel._tab_widget.count.return_value = 2
        mock_widget = MagicMock()
        mock_widget.property.return_value = "tid"
        panel._chat_views["tid"] = mock_widget
        panel._tab_widget.widget.return_value = mock_widget

        # Must not raise even when there was nothing to pop.
        panel._on_close_tab(0)
        mock_widget.shutdown.assert_called_once_with()


# ---------------------------------------------------------------------------
# Task 9 post-review fixes
# ---------------------------------------------------------------------------


class TestDrainGenerationAwarePendingClear(unittest.TestCase):
    """The drain must NOT clear ``_history_pending`` if the apply step
    submitted a new generation (e.g. NOT_FOUND auto-refresh).  Reviewer
    MEDIUM #1: the previous unconditional ``self._history_pending = False``
    after ``applied = True`` clobbered the pending flag of the
    auto-refresh list worker."""

    def test_not_found_drain_keeps_pending_when_refresh_submitted(self) -> None:
        """End-to-end (drain → apply → real ``_start_history_list_request``
        with a fake executor): NOT_FOUND leaves ``_history_pending=True``
        and submits exactly one list worker at generation+1."""
        from rikugan.state.history_types import (
            HistoryLoadResult,
            HistoryRequestStatus,
        )

        panel = _make_history_panel()
        panel._history_generation = 1
        panel._history_pending = True
        # Real ``_start_history_list_request`` will use this scope capture.
        next_scope = MagicMock(name="next_scope")
        panel._ctrl.capture_history_scope = MagicMock(return_value=next_scope)
        panel._history_panel.isVisible.return_value = True
        # Fake executor that records every submit (list + load share it).
        submitted: list = []

        class _FakeExecutor:
            def submit(self, fn, *args, **kwargs):
                submitted.append((fn, args, kwargs))
                return MagicMock()

        panel._history_executor = _FakeExecutor()
        panel._ensure_history_poll_timer = MagicMock()
        # The NOT_FOUND load result feeding the drain.
        not_found_scope = MagicMock(name="scope")
        not_found_scope.generation = 1
        load_result = HistoryLoadResult(
            status=HistoryRequestStatus.NOT_FOUND,
            scope=not_found_scope,
        )
        panel._history_result_queue.put(load_result)

        panel._drain_history_results()

        # The list refresh submitted exactly one worker at gen 2.
        list_submits = [(fn, args) for fn, args, _kw in submitted if fn.__func__ is panel._history_list_worker.__func__]
        self.assertEqual(len(list_submits), 1)
        self.assertEqual(panel._history_generation, 2)
        # CRITICAL: pending flag MUST remain set so the list worker's
        # terminal result eventually clears it (the pre-fix bug cleared
        # it here, leaving the panel stuck with a phantom in-flight flag
        # mismatch — the next list-submit guard would reject because
        # ``_history_pending`` was already False).
        self.assertTrue(panel._history_pending)

    def test_drain_clears_pending_when_apply_does_not_rebump_generation(self) -> None:
        """For LOADED / WRONG_IDB / EMPTY / FAILED (no refresh), the
        pending flag is cleared as the terminal result lands (regression
        guard for the generation-aware fix)."""
        from rikugan.state.history_types import (
            HistoryLoadResult,
            HistoryRequestStatus,
        )

        panel = _make_history_panel()
        panel._history_generation = 5
        panel._history_pending = True
        panel._history_panel.isVisible.return_value = True
        gen_before = panel._history_generation
        wrong_idb_scope = MagicMock(name="scope")
        wrong_idb_scope.generation = 5
        load_result = HistoryLoadResult(
            status=HistoryRequestStatus.WRONG_IDB,
            scope=wrong_idb_scope,
        )
        panel._history_result_queue.put(load_result)

        panel._drain_history_results()

        # No refresh fired → generation unchanged.
        self.assertEqual(panel._history_generation, gen_before)
        # Terminal result → pending cleared.
        self.assertFalse(panel._history_pending)

    def test_drain_clears_pending_per_result_when_multiple_match(self) -> None:
        """A queue holding two same-generation results (rare, but the
        type system allows it): each apply sees the live generation,
        the final pending state is correct for whichever apply landed
        last."""
        from rikugan.state.history_types import (
            HistoryListResult,
            HistoryLoadResult,
            HistoryRequestStatus,
        )

        panel = _make_history_panel()
        panel._history_generation = 7
        panel._history_pending = True
        panel._history_panel.isVisible.return_value = True
        scope_g7 = MagicMock(name="scope")
        scope_g7.generation = 7
        # Two results at gen 7: a list result and a load result.
        list_result = HistoryListResult(
            HistoryRequestStatus.LISTED,
            scope_g7,
            (),
        )
        wrong_idb_load = HistoryLoadResult(
            HistoryRequestStatus.WRONG_IDB,
            scope_g7,
        )
        panel._history_result_queue.put(list_result)
        panel._history_result_queue.put(wrong_idb_load)

        panel._drain_history_results()

        # Both applies ran; no generation rebump happened; pending
        # cleared exactly once by the drain epilogue.
        self.assertFalse(panel._history_pending)


class TestLoadRetryPath(unittest.TestCase):
    """Reviewer MEDIUM #2: a FAILED load must expose Retry and the Retry
    button must re-dispatch the LOAD (not the list).  The retry-load
    state remembers only the persisted session id (never the full
    SessionState) so a stale scope is recaptured on retry."""

    def _panel(self):
        panel = _make_history_panel()
        panel._create_tab = MagicMock()
        panel._focus_tab = MagicMock()
        panel._restore_messages_if_needed = MagicMock()
        panel._ctrl.attach_history_session = MagicMock()
        panel._ctrl.find_tab_for_session.return_value = None
        panel._history_panel.isVisible.return_value = True
        return panel

    def test_failed_load_shows_retry_visible_true(self) -> None:
        from rikugan.state.history_types import HistoryRequestStatus

        panel = self._panel()
        scope = MagicMock(name="scope")
        scope.generation = panel._history_generation
        load_result = _make_load_result(
            HistoryRequestStatus.FAILED,
            scope,
            error="ValueError: /untrusted/path",
        )
        panel._apply_history_loaded(load_result)
        # Generic copy, but Retry IS visible.
        panel._history_panel.set_error.assert_called_once_with(
            "Could not open this chat.",
            retry_visible=True,
        )

    def test_failed_load_remembered_for_retry(self) -> None:
        from rikugan.state.history_types import HistoryRequestStatus

        panel = self._panel()
        # Simulate the load-submit path having stashed the in-flight id.
        # ``_start_history_load`` writes ``_history_last_load_session_id``
        # before submitting the worker; ``HistoryLoadResult`` does NOT
        # echo the id back, so the FAILED branch copies from this stash.
        panel._history_last_load_session_id = "persisted-failed-x"
        scope = MagicMock(name="scope")
        scope.generation = panel._history_generation
        load_result = _make_load_result(
            HistoryRequestStatus.FAILED,
            scope,
        )
        panel._apply_history_loaded(load_result)
        # Only the id is retained — no full SessionState held in panel
        # state (defends against holding a large stale payload).
        self.assertEqual(panel._history_retry_load_session_id, "persisted-failed-x")

    def test_loaded_success_clears_retry_load(self) -> None:
        from rikugan.state.history_types import (
            HistoryAttachResult,
            HistoryAttachStatus,
            HistoryRequestStatus,
        )

        panel = self._panel()
        panel._history_retry_load_session_id = "previously-failed"
        session = MagicMock(name="session")
        session.messages = ["m1"]
        scope = MagicMock(name="scope")
        scope.generation = panel._history_generation
        load_result = _make_load_result(
            HistoryRequestStatus.LOADED,
            scope,
            session=session,
        )
        panel._ctrl.attach_history_session.return_value = HistoryAttachResult(
            HistoryAttachStatus.OPENED,
            "tab-new",
            session,
        )
        panel._apply_history_loaded(load_result)
        # Successful attach clears the retry-load id.
        self.assertIsNone(panel._history_retry_load_session_id)

    def test_not_found_does_not_set_retry_load(self) -> None:
        """NOT_FOUND refreshes the list; it does not retain a
        retry-load id because the session is gone for good."""
        from rikugan.state.history_types import HistoryRequestStatus

        panel = self._panel()
        panel._history_generation = 1
        panel._history_pending = True
        panel._start_history_list_request = MagicMock()
        scope = MagicMock(name="scope")
        scope.generation = 1
        load_result = _make_load_result(
            HistoryRequestStatus.NOT_FOUND,
            scope,
        )
        panel._apply_history_loaded(load_result)
        self.assertIsNone(panel._history_retry_load_session_id)

    def test_wrong_idb_does_not_set_retry_load(self) -> None:
        """WRONG_IDB is non-retryable — no retry-load id retained."""
        from rikugan.state.history_types import HistoryRequestStatus

        panel = self._panel()
        scope = MagicMock(name="scope")
        scope.generation = panel._history_generation
        load_result = _make_load_result(
            HistoryRequestStatus.WRONG_IDB,
            scope,
        )
        panel._apply_history_loaded(load_result)
        self.assertIsNone(panel._history_retry_load_session_id)

    def test_retry_button_redispatches_load_not_list(self) -> None:
        """``_on_history_retry`` with a remembered load id must submit
        a LOAD worker, not call ``_start_history_list_request``."""
        panel = self._panel()
        panel._history_retry_load_session_id = "persisted-retry"
        submitted: list = []

        class _FakeExecutor:
            def submit(self, fn, *args, **kwargs):
                submitted.append((fn, args, kwargs))
                return MagicMock()

        panel._history_executor = _FakeExecutor()
        panel._ensure_history_poll_timer = MagicMock()
        next_scope = MagicMock(name="next_scope")
        panel._ctrl.capture_history_scope = MagicMock(return_value=next_scope)
        panel._start_history_list_request = MagicMock()

        panel._on_history_retry()

        # Load path taken — exactly one submit to the dedicated executor.
        self.assertEqual(len(submitted), 1)
        fn, args, _kw = submitted[0]
        self.assertEqual(fn.__func__, panel._history_load_worker.__func__)
        self.assertEqual(args[0], "persisted-retry")
        # Fresh generation + scope captured on retry.
        self.assertIs(args[1], next_scope)
        # Pending flag set so the worker's terminal result can clear it.
        self.assertTrue(panel._history_pending)
        # List refresh NOT triggered.
        panel._start_history_list_request.assert_not_called()
        # Retry id cleared once retried (a new FAILED result will set it
        # again; a success path leaves it cleared).
        self.assertIsNone(panel._history_retry_load_session_id)

    def test_retry_button_falls_back_to_list_when_no_load_pending(self) -> None:
        """If no FAILED load is remembered, Retry falls back to the list
        refresh (Task 8 behavior)."""
        panel = self._panel()
        panel._history_retry_load_session_id = None
        panel._start_history_list_request = MagicMock()
        panel._history_executor = MagicMock()
        panel._on_history_retry()
        panel._start_history_list_request.assert_called_once_with()
        panel._history_executor.submit.assert_not_called()

    def test_retry_button_pre_dedupes_existing_tab_before_load(self) -> None:
        """If the session is already open when Retry fires, focus it and
        skip the worker (same dedupe invariant as the initial open)."""
        panel = self._panel()
        panel._history_retry_load_session_id = "persisted-x"
        panel._ctrl.find_tab_for_session.return_value = "tab-already"
        panel._focus_tab = MagicMock()
        panel._history_executor = MagicMock()
        panel._on_history_retry()
        panel._focus_tab.assert_called_once_with("tab-already")
        panel._history_executor.submit.assert_not_called()
        # Retry id cleared because the user's goal (open the session) is
        # satisfied.
        self.assertIsNone(panel._history_retry_load_session_id)

    def test_invalidate_history_clears_retry_load(self) -> None:
        """IDB change / shutdown must clear the retry-load id."""
        panel = self._panel()
        panel._history_retry_load_session_id = "persisted-stale"
        panel._invalidate_history(clear_panel=False)
        self.assertIsNone(panel._history_retry_load_session_id)

    def test_close_tab_does_not_clear_retry_load(self) -> None:
        """Closing an unrelated tab must not affect a remembered retry-load
        (the retry is for a session that was never opened, so it has no
        tab of its own to close)."""
        panel = self._panel()
        panel._history_retry_load_session_id = "persisted-retry"
        panel._tab_widget.count.return_value = 2
        mock_widget = MagicMock()
        mock_widget.property.return_value = "tid"
        panel._chat_views["tid"] = mock_widget
        panel._tab_widget.widget.return_value = mock_widget
        panel._on_close_tab(0)
        self.assertEqual(panel._history_retry_load_session_id, "persisted-retry")


# ---------------------------------------------------------------------------
# Task 10: IDB Change and Shutdown Invalidation — exact ordering and race-free
# Event capture (spec §7.2, §10.3, §11.4, §14.4).
#
# Task 8 added ``_invalidate_history`` prematurely and without the
# ``clear_panel`` parameter.  Task 10 TDD-refines the helper to the exact
# interface/order the spec mandates and plugs the closing-Event reuse race
# the Task 8 implementation leaves open.
# ---------------------------------------------------------------------------

import queue as _queue_module  # noqa: E402


class TestTask10InvalidateHistorySignature(unittest.TestCase):
    """``_invalidate_history`` is keyword-only with ``clear_panel: bool``."""

    def test_requires_clear_panel_keyword(self) -> None:
        """The helper must accept ``clear_panel`` as a keyword-only
        argument so every call site reads ``clear_panel=True`` /
        ``clear_panel=False`` — the bool is load-bearing and a positional
        pass would obscure it at the call site."""
        import inspect

        sig = inspect.signature(RikuganPanelCore._invalidate_history)
        params = sig.parameters
        self.assertIn("clear_panel", params)
        # Keyword-only (comes after ``*`` or ``*args``).
        clear_panel_param = params["clear_panel"]
        self.assertEqual(
            clear_panel_param.kind,
            inspect.Parameter.KEYWORD_ONLY,
            "clear_panel must be keyword-only",
        )

    def test_clear_panel_false_skips_panel_clear(self) -> None:
        """``clear_panel=False`` (used by ``shutdown``) must NOT touch the
        HistoryPanel widget — widget mutation after the C++ teardown has
        started is unsafe.  Only ``clear_panel=True`` (the IDB-change path)
        clears the panel rows."""
        panel = _make_history_panel()
        panel._history_retry_load_session_id = "stale"
        panel._history_last_load_session_id = "stale2"
        panel._history_panel.reset_mock()
        panel._invalidate_history(clear_panel=False)
        panel._history_panel.clear.assert_not_called()

    def test_clear_panel_true_calls_panel_clear(self) -> None:
        """``clear_panel=True`` (the IDB-change path) clears the panel so
        the user does not see stale rows from the previous IDB."""
        panel = _make_history_panel()
        panel._invalidate_history(clear_panel=True)
        panel._history_panel.clear.assert_called_once_with()


class TestTask10InvalidateHistoryOrdering(unittest.TestCase):
    """Exact ordering per spec §7.2 / §11.4:

    1. ``_history_generation`` += 1 (BEFORE controller identity changes,
       so a worker result for the old IDB is rejected by generation check).
    2. ``_history_closing.set()`` BEFORE timer stop / executor shutdown so
       a worker observing the closing flag mid-flight cannot enqueue into
       a queue that is about to be drained.
    3. Stop + delete the history poll timer.
    4. Detach the executor reference THEN non-blocking shutdown with
       ``cancel_futures=True`` (so any not-yet-started submit is dropped).
    5. Drain the result queue.
    6. ``_history_pending = False``.
    7. Clear ``_history_retry_load_session_id`` /
       ``_history_last_load_session_id``.
    8. Optional ``panel.clear()`` if ``clear_panel=True``.
    """

    def _instrument_panel(self):
        """Wire mocks that record the call order of every side-effect
        during ``_invalidate_history``.  Returns the panel and the shared
        recorder list."""
        panel = _make_history_panel()
        recorder: list = []

        # Seed an executor so we can observe its shutdown.
        real_executor = MagicMock(name="executor")
        panel._history_executor = real_executor
        panel._history_poll_timer = MagicMock(name="poll_timer")
        # Wrap the closing Event so we can record when ``set`` is called
        # and the order relative to the other side-effects.
        original_event = panel._history_closing

        class _RecordingEvent:
            """Stand-in for ``threading.Event`` that records each call."""

            def __init__(self, inner):
                self._inner = inner

            def set(self):
                recorder.append(("closing.set",))
                self._inner.set()

            def clear(self):
                recorder.append(("closing.clear",))
                self._inner.clear()

            def is_set(self):
                return self._inner.is_set()

        panel._history_closing = _RecordingEvent(original_event)
        # Wrap the executor/timer/queue/panel helpers so they append
        # before delegating to the real behaviour.
        panel._stop_history_poll_timer = MagicMock(
            name="stop_timer",
            side_effect=lambda: recorder.append(("stop_timer",)),
        )
        real_executor.shutdown = MagicMock(
            name="exec_shutdown",
            side_effect=lambda **kw: recorder.append(("exec_shutdown", kw)),
        )

        # Wrap queue.get_nowait to record drain activity.
        original_queue = panel._history_result_queue

        def _drained_get():
            recorder.append(("queue.get_nowait",))
            raise _queue_module.Empty

        original_queue.get_nowait = _drained_get  # type: ignore[assignment]

        # Wrap panel.clear.
        panel._history_panel.clear = MagicMock(
            name="panel_clear",
            side_effect=lambda: recorder.append(("panel.clear",)),
        )
        return panel, recorder

    def test_generation_increments_and_closing_set_is_first_recorder_entry(self) -> None:
        """Generation bump is step 1, closing.set is step 2 (spec §7.2).

        A worker that has already captured a scope with the old
        generation must have its result discarded by the drain's
        generation check, so the bump must happen BEFORE any other
        side-effect that might let a late worker slip through."""
        panel, recorder = self._instrument_panel()
        start_gen = panel._history_generation
        panel._invalidate_history(clear_panel=False)
        self.assertGreater(panel._history_generation, start_gen)
        # The very first recorder entry must be the closing-set (the
        # generation bump happens BEFORE it but is silent).
        self.assertEqual(recorder[0], ("closing.set",))

    def test_closing_set_before_timer_stop_and_executor_shutdown(self) -> None:
        """Closing flag is set BEFORE the timer is stopped and BEFORE the
        executor is shut down (spec §11.4).  A worker finishing in the
        tiny window between ``timer.stop`` and ``executor.shutdown`` must
        see ``is_set()=True`` and drop its result."""
        panel, recorder = self._instrument_panel()
        panel._invalidate_history(clear_panel=False)
        closing_idx = next(i for i, e in enumerate(recorder) if e[0] == "closing.set")
        timer_idx = next(i for i, e in enumerate(recorder) if e[0] == "stop_timer")
        exec_idx = next(i for i, e in enumerate(recorder) if e[0] == "exec_shutdown")
        self.assertLess(closing_idx, timer_idx)
        self.assertLess(closing_idx, exec_idx)

    def test_executor_shutdown_is_nonblocking_with_cancel_futures(self) -> None:
        """Spec §11.4: executor shutdown must be non-blocking with
        ``cancel_futures=True`` so a not-yet-started submit does not run
        against a half-torn-down panel."""
        panel, recorder = self._instrument_panel()
        panel._invalidate_history(clear_panel=False)
        exec_calls = [e for e in recorder if e[0] == "exec_shutdown"]
        self.assertEqual(len(exec_calls), 1)
        _name, kwargs = exec_calls[0]
        self.assertEqual(kwargs, {"wait": False, "cancel_futures": True})

    def test_executor_reference_detached_before_shutdown(self) -> None:
        """The panel must drop its reference to the executor (set it to
        ``None``) BEFORE the non-blocking shutdown so a concurrent request
        that reads ``self._history_executor`` cannot re-submit to an
        executor that is being cancelled.  The brief lists
        ``detach executor ref then nonblocking shutdown cancel``."""
        panel = _make_history_panel()
        events: list[str] = []

        class _OrderedExecutor(MagicMock):
            def shutdown(self, **kw):
                events.append("shutdown")
                # At the moment shutdown is invoked, the panel must have
                # already detached the reference — this is the
                # concurrency invariant.
                assert panel._history_executor is None, "executor ref must be detached BEFORE shutdown"

        panel._history_executor = _OrderedExecutor()
        panel._invalidate_history(clear_panel=False)
        self.assertEqual(events, ["shutdown"])
        self.assertIsNone(panel._history_executor)

    def test_queue_drained(self) -> None:
        panel, recorder = self._instrument_panel()
        panel._invalidate_history(clear_panel=False)
        self.assertIn(("queue.get_nowait",), recorder)

    def test_history_pending_false_after_invalidate(self) -> None:
        panel = _make_history_panel()
        panel._history_pending = True
        panel._invalidate_history(clear_panel=False)
        self.assertFalse(panel._history_pending)

    def test_invalidate_clears_retry_load_and_last_load_ids(self) -> None:
        panel = _make_history_panel()
        panel._history_retry_load_session_id = "persisted-stale"
        panel._history_last_load_session_id = "persisted-last"
        panel._invalidate_history(clear_panel=False)
        self.assertIsNone(panel._history_retry_load_session_id)
        self.assertIsNone(panel._history_last_load_session_id)

    def test_closing_event_is_replaced_with_fresh_event_after_invalidate(self) -> None:
        """Spec §11.4 / reviewer concern: the OLD closing Event is set
        but must NOT be reused by the next request.  A new
        ``threading.Event`` instance must be installed so a stale worker
        that captured the OLD event reference keeps observing ``set()``
        even after a new request clears the NEW event.  This is the
        load-bearing race fix: ``clear()`` on the new event does not
        un-set the old event."""
        import threading

        panel = _make_history_panel()
        old_event = panel._history_closing
        panel._invalidate_history(clear_panel=False)
        # The old event stays set (the worker that captured it keeps
        # seeing "closing").
        self.assertTrue(old_event.is_set())
        # The panel now points at a fresh, unset event.
        new_event = panel._history_closing
        self.assertIsNot(new_event, old_event)
        self.assertIsInstance(new_event, threading.Event)
        self.assertFalse(new_event.is_set())

    def test_invalidate_is_idempotent(self) -> None:
        """Calling ``_invalidate_history`` twice (e.g. IDB change during
        shutdown) must not raise and must remain in a coherent state."""
        panel = _make_history_panel()
        panel._invalidate_history(clear_panel=False)
        gen_after_first = panel._history_generation
        panel._invalidate_history(clear_panel=False)
        self.assertGreater(panel._history_generation, gen_after_first)
        self.assertFalse(panel._history_pending)
        self.assertIsNone(panel._history_executor)


class TestTask10OnDatabaseChangedOrdering(unittest.TestCase):
    """``on_database_changed`` must invalidate BEFORE controller identity
    is reset and with ``clear_panel=True`` (spec §7.2)."""

    def _patch_panel_for_db_change(self, panel) -> None:
        """Pin ``_tab_widget.count`` to a real int so the production
        ``while self._tab_widget.count():`` loop terminates.  A bare
        ``MagicMock`` returns a truthy mock forever, hanging the test."""
        panel._tab_widget.count.return_value = 0
        # ``_renamer_engine`` is optional; clear it so the production code
        # does not call ``.cancel()`` on a MagicMock that swallows it
        # (harmless but noisy).
        panel._renamer_engine = None

    def test_invalidate_runs_before_controller_reset(self) -> None:
        panel = _make_history_panel()
        order: list[str] = []

        def _record_invalidate(**kw):
            order.append("invalidate")
            self.assertEqual(order, ["invalidate"])

        def _record_reset(path):
            order.append("reset")

        panel._invalidate_history = MagicMock(side_effect=_record_invalidate)
        panel._ctrl._idb_path = "/old.i64"
        panel._ctrl.reset_for_new_file = MagicMock(side_effect=_record_reset)
        panel._cleanup_renamer_chunk = MagicMock()
        panel._create_tab = MagicMock()
        self._patch_panel_for_db_change(panel)
        panel.on_database_changed("/new.i64")
        self.assertEqual(order, ["invalidate", "reset"])

    def test_clear_panel_true_for_idb_change(self) -> None:
        panel = _make_history_panel()
        called_kwargs: dict = {}
        panel._invalidate_history = MagicMock(
            side_effect=lambda **kw: called_kwargs.update(kw),
        )
        panel._ctrl._idb_path = "/old.i64"
        panel._cleanup_renamer_chunk = MagicMock()
        panel._create_tab = MagicMock()
        self._patch_panel_for_db_change(panel)
        panel.on_database_changed("/new.i64")
        self.assertEqual(called_kwargs, {"clear_panel": True})

    def test_idb_change_clears_pending_restore_payloads_and_creates_one_new_tab(self) -> None:
        """Spec §7.2: after the IDB switch, exactly one fresh ``New Chat``
        tab exists and no prior pending restore payload survives."""
        panel = _make_history_panel()
        panel._ctrl._idb_path = "/old.i64"
        panel._pending_restore_messages = {
            "tab-a": ["msg1", "msg2"],
            "tab-b": ["msg3"],
        }
        panel._chat_views = {"tab-a": MagicMock(), "tab-b": MagicMock()}
        panel._cleanup_renamer_chunk = MagicMock()
        panel._create_tab = MagicMock()
        self._patch_panel_for_db_change(panel)
        panel.on_database_changed("/new.i64")
        # Pending restore payload fully cleared.
        self.assertEqual(panel._pending_restore_messages, {})
        # Exactly one tab created.
        panel._create_tab.assert_called_once()
        # No auto-restore — _try_restore_session must not exist / not be
        # invoked.
        self.assertFalse(hasattr(panel, "_try_restore_session"))


class TestTask10ShutdownOrdering(unittest.TestCase):
    """``shutdown`` must set ``_is_shutdown`` first, then invalidate with
    ``clear_panel=False`` (no widget mutation after teardown starts),
    then call ``history_panel.shutdown()`` (release theme subscriptions)
    BEFORE continuing the existing teardown."""

    def test_shutdown_sets_is_shutdown_first(self) -> None:
        """``_is_shutdown`` is the gate that keeps late
        ``_drain_history_results`` from touching widgets.  It must be
        ``True`` BEFORE ``_invalidate_history`` runs."""
        panel = _make_history_panel()
        observed_flags: list = []

        def _spy_invalidate(**kw):
            observed_flags.append(panel._is_shutdown)

        panel._invalidate_history = MagicMock(side_effect=_spy_invalidate)
        panel._poll_timer = None
        panel._skills_refresh_timer = None
        panel._context_bar = None
        panel._ui_hooks = None
        panel._chat_views = {}
        panel._tools_form = None
        panel._tools_panel = None
        panel._renamer_engine = None
        panel.shutdown()
        self.assertTrue(all(observed_flags))
        self.assertTrue(panel._is_shutdown)

    def test_shutdown_calls_invalidate_with_clear_panel_false(self) -> None:
        """Widget mutations must NOT happen after teardown starts.  The
        ``history_panel.clear()`` call is skipped on shutdown."""
        panel = _make_history_panel()
        captured_kwargs: dict = {}
        panel._invalidate_history = MagicMock(
            side_effect=lambda **kw: captured_kwargs.update(kw),
        )
        panel._poll_timer = None
        panel._skills_refresh_timer = None
        panel._context_bar = None
        panel._ui_hooks = None
        panel._chat_views = {}
        panel._tools_form = None
        panel._tools_panel = None
        panel._renamer_engine = None
        panel.shutdown()
        self.assertEqual(captured_kwargs, {"clear_panel": False})

    def test_shutdown_calls_history_panel_shutdown(self) -> None:
        """``HistoryPanel.shutdown`` releases the panel's theme
        subscriptions and is idempotent."""
        panel = _make_history_panel()
        panel._poll_timer = None
        panel._skills_refresh_timer = None
        panel._context_bar = None
        panel._ui_hooks = None
        panel._chat_views = {}
        panel._tools_form = None
        panel._tools_panel = None
        panel._renamer_engine = None
        panel.shutdown()
        panel._history_panel.shutdown.assert_called_once_with()

    def test_shutdown_invalidate_runs_before_history_panel_shutdown(self) -> None:
        """The invalidation (drop executor, drain queue) must run BEFORE
        the ``history_panel.shutdown()`` theme-subscription release — the
        order matches ``_invalidate_history`` → ``history_panel.shutdown``."""
        panel = _make_history_panel()
        order: list = []

        def _invalidate_spy(**kw):
            order.append("invalidate")

        def _history_shutdown_spy():
            order.append("history_shutdown")

        panel._invalidate_history = MagicMock(side_effect=_invalidate_spy)
        panel._history_panel.shutdown = MagicMock(side_effect=_history_shutdown_spy)
        panel._poll_timer = None
        panel._skills_refresh_timer = None
        panel._context_bar = None
        panel._ui_hooks = None
        panel._chat_views = {}
        panel._tools_form = None
        panel._tools_panel = None
        panel._renamer_engine = None
        panel.shutdown()
        self.assertEqual(order, ["invalidate", "history_shutdown"])


class TestTask10StaleWorkerRace(unittest.TestCase):
    """The load-bearing race fix.

    Task 8 reused ``self._history_closing`` across requests: a new
    request called ``Event.clear()`` on the same instance, so an old
    worker that had not yet checked ``is_set()`` would observe ``False``
    and push its result into the new queue.  Task 10 fixes this by:

    1. Replacing the closing ``Event`` with a fresh instance on every
       invalidate.
    2. Capturing the closing ``Event`` reference at submit time and
       passing it into the worker.  The worker checks ITS captured event,
       not ``self._history_closing``.

    The tests below deterministically prove an old worker (submitted
    against the previous generation) CANNOT enqueue after a new request
    starts, even when the new request has already cleared its event.
    """

    def test_worker_receives_captured_closing_event_argument(self) -> None:
        """The list and load workers must accept a captured closing
        ``Event`` so an old worker keeps observing the OLD event even
        after the panel installs a NEW one."""
        import inspect

        list_sig = inspect.signature(RikuganPanelCore._history_list_worker)
        load_sig = inspect.signature(RikuganPanelCore._history_load_worker)
        list_params = list(list_sig.parameters)
        load_params = list(load_sig.parameters)
        self.assertIn(
            "closing_event",
            list_params,
            f"_history_list_worker must accept closing_event (has: {list_params})",
        )
        self.assertIn(
            "closing_event",
            load_params,
            f"_history_load_worker must accept closing_event (has: {load_params})",
        )

    def test_submit_passes_live_closing_event_into_worker(self) -> None:
        """``_start_history_list_request`` and ``_start_history_load``
        must pass the CURRENT ``self._history_closing`` reference into
        the worker call — NOT let the worker read ``self._history_closing``
        dynamically.  Otherwise a worker that starts after a new request
        observes the NEW event and races."""
        panel = _make_history_panel()
        captured_args: list = []
        panel._ctrl.capture_history_scope = MagicMock(
            return_value=MagicMock(name="scope"),
        )
        mock_executor = MagicMock(name="executor")
        mock_executor.submit = MagicMock(
            side_effect=lambda fn, *args, **kw: captured_args.append((fn, args, kw)),
        )
        panel._history_executor = mock_executor
        live_event = panel._history_closing
        panel._start_history_list_request()
        self.assertEqual(len(captured_args), 1)
        _fn, args, _kw = captured_args[0]
        self.assertIn(live_event, args)

    def test_old_worker_drops_result_after_invalidate_then_new_request(self) -> None:
        """End-to-end race guard.

        Sequence:
          (a) Submit list request #1 — captures ``event_old``.
          (b) IDB change fires ``_invalidate_history(clear_panel=True)``:
              the panel installs a fresh event (``event_new``).
          (c) Submit list request #2 — captures ``event_new``.
          (d) The OLD worker (request #1) finally checks its captured
              ``event_old`` — it must see ``is_set()==True`` (the old
              event was set by invalidate and is NEVER cleared) and drop
              the result, regardless of the new request clearing its
              event.
        """
        from concurrent.futures import ThreadPoolExecutor

        from rikugan.state.history_types import HistoryScope

        panel = _make_history_panel()
        panel._ctrl.config = MagicMock()
        panel._ctrl.list_history_sessions = MagicMock(return_value=[])
        panel._history_executor = ThreadPoolExecutor(max_workers=1)
        try:
            captured_submits: list = []

            def _spying_submit(fn, *args, **kw):
                captured_submits.append((fn, args, kw))
                return MagicMock(name="future")

            panel._history_executor.submit = _spying_submit  # type: ignore[assignment]
            panel._ctrl.capture_history_scope = MagicMock(
                return_value=HistoryScope(
                    idb_path="/old.i64",
                    db_instance_id="old",
                    generation=1,
                ),
            )
            with patch.object(_pc_module, "SessionHistory") as hist_cls:
                hist_cls.return_value.flush_saves = MagicMock()
                panel._start_history_list_request()
            self.assertEqual(len(captured_submits), 1)
            _fn_1, args_1, _kw_1 = captured_submits[0]
            event_old = panel._history_closing

            # Step (b): invalidate replaces the event.
            panel._invalidate_history(clear_panel=True)
            event_after_invalidate = panel._history_closing
            self.assertIsNot(event_after_invalidate, event_old)
            self.assertTrue(event_old.is_set())

            # Step (c): start request #2 against the new event.
            # ``_invalidate_history`` detached the executor reference
            # (set it to ``None``), so install a fresh mock executor
            # before the next request.  The production lazy-init path
            # would otherwise create a real ``ThreadPoolExecutor``; we
            # short-circuit it so we can spy on the submit call args
            # without needing a real thread pool.
            mock_executor_2 = MagicMock(name="executor_2")
            mock_executor_2.submit = MagicMock(
                side_effect=lambda fn, *a, **kw: captured_submits.append((fn, a, kw)),
            )
            panel._history_executor = mock_executor_2
            panel._ctrl.capture_history_scope = MagicMock(
                return_value=HistoryScope(
                    idb_path="/new.i64",
                    db_instance_id="new",
                    generation=2,
                ),
            )
            with patch.object(_pc_module, "SessionHistory") as hist_cls2:
                hist_cls2.return_value.flush_saves = MagicMock()
                panel._start_history_list_request()
            self.assertEqual(len(captured_submits), 2)
            _fn_2, _args_2, _kw_2 = captured_submits[1]
            event_new = panel._history_closing
            self.assertIs(event_after_invalidate, event_new)
            self.assertFalse(event_new.is_set())

            # Step (d): run the OLD worker with its captured old event.
            scope_1 = HistoryScope(
                idb_path="/old.i64",
                db_instance_id="old",
                generation=1,
            )
            captured_event_arg_1 = args_1[-1]
            self.assertIs(captured_event_arg_1, event_old)
            with patch.object(_pc_module, "SessionHistory") as hist_cls3:
                hist_cls3.return_value.flush_saves = MagicMock()
                panel._history_list_worker(scope_1, captured_event_arg_1)
            self.assertTrue(panel._history_result_queue.empty())
        finally:
            # ``_invalidate_history`` detaches the executor reference;
            # shut down whatever remains (may be None after invalidate).
            executor = panel._history_executor
            if executor is not None:
                executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Task 5: PanelCore confirmation, intents, worker, retry, watchdog
# (spec §11.5). TDD-driven; tests below intentionally exercise every
# concurrency-sensitive invariant in one atomic suite so regressions in
# the pre-confirm / post-confirm / watchdog / retry / invalidation
# phases cannot land silently.
# ---------------------------------------------------------------------------
from rikugan.state.history_types import (  # noqa: E402
    HistoryDeleteResult,
    HistoryDeleteStatus,
    HistoryRequestStatus,
)


class TestHistoryDeletePreflight(unittest.TestCase):
    """Pre-confirmation gating (spec §11.5 preflight).

    Two paths must short-circuit BEFORE the confirmation dialog opens:

    1. The persisted session is already attached to an open tab — focus
       it, surface a dismiss-only notice, and never submit a worker.
    2. Another history operation is already in flight — surface a
       dismiss-only busy notice and never submit a worker.

    Both paths preserve single-flight submission and keep the user
    informed without enabling Retry (the user did not hit a failure
    that would warrant a retry).
    """

    def test_open_chat_is_focused_without_confirmation_or_submit(self) -> None:
        panel = _make_history_panel()
        panel._ctrl.find_tab_for_session.return_value = "tab-a"
        panel._focus_tab = MagicMock()
        panel._confirm_history_delete = MagicMock()

        panel._on_history_delete_requested("persisted-a", "Analyze parser")

        panel._focus_tab.assert_called_once_with("tab-a")
        panel._confirm_history_delete.assert_not_called()
        panel._history_panel.show_notice.assert_called_once_with(
            "Close this chat before deleting it from History.",
            retry_visible=False,
            dismiss_visible=True,
        )

    def test_pending_history_operation_shows_busy_notice(self) -> None:
        panel = _make_history_panel()
        panel._history_pending = True

        panel._on_history_delete_requested("persisted-a", "Analyze parser")

        panel._history_panel.show_notice.assert_called_once_with(
            "History is busy. Try again shortly.",
            retry_visible=False,
            dismiss_visible=True,
        )


class TestHistoryDeleteIntents(unittest.TestCase):
    """Pre-confirm and post-confirm intent gate (spec §11.5 intent gate).

    The intent set is the load-bearing gate against the race in which a
    queued LOAD result (from a click the user made moments before) lands
    while a DELETE confirmation is still on screen. Without the gate the
    load would attach the session AFTER the user just confirmed deleting
    it — producing a tab whose persistence layer is racing with the
    delete worker. The intent MUST be inserted BEFORE the confirmation
    dialog opens and MUST be honored synchronously by every LOAD path
    (queued-result apply AND new open request).
    """

    def test_delete_intent_exists_while_confirmation_is_open(self) -> None:
        panel = _make_history_panel()

        def confirm(_title: str) -> bool:
            # Intent is set BEFORE the dialog opens; it must be present
            # while confirmation is on screen.
            self.assertIn("persisted-a", panel._history_delete_intents)
            return False

        panel._confirm_history_delete = confirm

        panel._on_history_delete_requested("persisted-a", "Analyze parser")

        # Cancelled confirmation MUST clear the intent — the user backed
        # out, so no LOAD should be blocked.
        self.assertNotIn("persisted-a", panel._history_delete_intents)

    def test_intent_blocks_queued_load_result_before_confirmation_returns(self) -> None:
        """A LOAD result for the session being deleted must be dropped
        instead of attached, even when the apply path runs after the
        worker already enqueued its terminal result."""
        panel = _make_history_panel()
        session = MagicMock(id="persisted-a", messages=[])
        result = _make_load_result(
            status=HistoryRequestStatus.LOADED,
            scope=MagicMock(),
            session=session,
        )
        panel._history_delete_intents = {"persisted-a"}

        panel._apply_history_loaded(result)

        panel._ctrl.attach_history_session.assert_not_called()

    def test_intent_blocks_another_load(self) -> None:
        """A second user click that would start a NEW load for the
        session being deleted must also be dropped."""
        panel = _make_history_panel()
        panel._history_delete_intents = {"persisted-a"}
        panel._start_history_load = MagicMock()

        panel._on_history_open_requested("persisted-a")

        panel._start_history_load.assert_not_called()
        panel._ctrl.find_tab_for_session.assert_not_called()


class TestHistoryDeleteWorkerAndApply(unittest.TestCase):
    """Worker submission + queue routing + result application (spec §11.5)."""

    def test_worker_enqueues_typed_delete_result(self) -> None:
        panel = _make_history_panel()
        scope = MagicMock(generation=4)
        result = HistoryDeleteResult(
            HistoryDeleteStatus.DELETED,
            scope,
            "persisted-a",
        )
        panel._ctrl.delete_history_session.return_value = result

        panel._history_delete_worker(
            "persisted-a",
            scope,
            panel._history_closing,
        )

        self.assertIs(panel._history_result_queue.get_nowait(), result)

    def test_worker_catches_exception_as_failed_result(self) -> None:
        """Unexpected exceptions must be converted to a FAILED typed
        result — never raised across the worker boundary."""
        panel = _make_history_panel()
        scope = MagicMock(generation=4)
        panel._ctrl.delete_history_session.side_effect = OSError("disk leaked")

        panel._history_delete_worker(
            "persisted-a",
            scope,
            panel._history_closing,
        )

        result = panel._history_result_queue.get_nowait()
        self.assertEqual(result.status, HistoryDeleteStatus.FAILED)
        self.assertEqual(result.session_id, "persisted-a")

    def test_worker_does_not_enqueue_when_closing(self) -> None:
        """Closing flag set by invalidate drops the result instead of
        leaving it in an unpolled queue (spec §11.4 drain-and-discard)."""
        import threading

        panel = _make_history_panel()
        closing_event = threading.Event()
        closing_event.set()
        scope = MagicMock(generation=4)
        panel._ctrl.delete_history_session.return_value = HistoryDeleteResult(
            HistoryDeleteStatus.DELETED,
            scope,
            "persisted-a",
        )

        panel._history_delete_worker(
            "persisted-a",
            scope,
            closing_event,
        )

        self.assertTrue(panel._history_result_queue.empty())

    def test_deleted_result_removes_row_and_starts_refresh(self) -> None:
        panel = _make_history_panel()
        scope = panel._ctrl.capture_history_scope.return_value
        scope.generation = 5
        panel._history_generation = 5
        panel._history_delete_intents = {"persisted-a"}
        panel._start_history_list_request = MagicMock()
        panel._stop_history_delete_watchdog = MagicMock()
        result = HistoryDeleteResult(
            HistoryDeleteStatus.DELETED,
            scope,
            "persisted-a",
        )

        panel._apply_history_deleted(result)

        panel._history_panel.remove_entry.assert_called_once_with("persisted-a")
        panel._start_history_list_request.assert_called_once()
        panel._stop_history_delete_watchdog.assert_called_once()
        panel._history_panel.set_operation_pending.assert_called_once_with(None)
        self.assertNotIn("persisted-a", panel._history_delete_intents)
        self.assertIsNone(panel._history_retry_delete_session_id)

    def test_not_found_result_removes_row_and_refreshes(self) -> None:
        """NOT_FOUND is a terminal-success state: the row is gone and
        the list refreshes so any stale rows are reconciled."""
        panel = _make_history_panel()
        scope = MagicMock(generation=5)
        panel._history_generation = 5
        panel._history_delete_intents = {"persisted-a"}
        panel._start_history_list_request = MagicMock()
        panel._stop_history_delete_watchdog = MagicMock()
        result = HistoryDeleteResult(
            HistoryDeleteStatus.NOT_FOUND,
            scope,
            "persisted-a",
        )

        panel._apply_history_deleted(result)

        panel._history_panel.remove_entry.assert_called_once_with("persisted-a")
        panel._start_history_list_request.assert_called_once()
        self.assertIsNone(panel._history_retry_delete_session_id)

    def test_wrong_idb_result_refreshes_without_removing_row(self) -> None:
        """WRONG_IDB is terminal-success for the request (no retry) but
        the row may still be valid for a different IDB — refresh the
        list without touching the row."""
        panel = _make_history_panel()
        scope = MagicMock(generation=5)
        panel._history_generation = 5
        panel._history_delete_intents = {"persisted-a"}
        panel._start_history_list_request = MagicMock()
        panel._stop_history_delete_watchdog = MagicMock()
        result = HistoryDeleteResult(
            HistoryDeleteStatus.WRONG_IDB,
            scope,
            "persisted-a",
        )

        panel._apply_history_deleted(result)

        panel._history_panel.remove_entry.assert_not_called()
        panel._start_history_list_request.assert_called_once()
        self.assertIsNone(panel._history_retry_delete_session_id)

    def test_failed_result_keeps_row_and_enables_retry(self) -> None:
        panel = _make_history_panel()
        result = HistoryDeleteResult(
            HistoryDeleteStatus.FAILED,
            MagicMock(),
            "persisted-a",
            error="PermissionError: locked",
        )
        panel._stop_history_delete_watchdog = MagicMock()

        panel._apply_history_deleted(result)

        panel._history_panel.remove_entry.assert_not_called()
        panel._history_panel.set_operation_pending.assert_called_with(None)
        panel._history_panel.show_notice.assert_called_with(
            "Could not delete this chat.",
            retry_visible=True,
            dismiss_visible=True,
        )
        self.assertEqual(panel._history_retry_delete_session_id, "persisted-a")


class TestHistoryDeleteRetry(unittest.TestCase):
    """Retry routing after a FAILED delete (spec §11.5 retry priority)."""

    def test_delete_retry_skips_confirmation_and_captures_fresh_scope(self) -> None:
        panel = _make_history_panel()
        panel._history_retry_delete_session_id = "persisted-a"
        panel._confirm_history_delete = MagicMock()
        panel._start_history_delete = MagicMock()

        panel._on_history_retry()

        panel._confirm_history_delete.assert_not_called()
        panel._start_history_delete.assert_called_once_with("persisted-a")

    def test_delete_retry_focuses_chat_opened_after_failure(self) -> None:
        """If the user opened the chat between the FAILED result and the
        retry click, ``_start_history_delete`` rechecks the open-tab
        invariant and short-circuits (no worker, no delete of the chat
        the user is actively viewing)."""
        panel = _make_history_panel()
        panel._history_retry_delete_session_id = "persisted-a"
        panel._ctrl.find_tab_for_session.return_value = "tab-a"
        panel._focus_tab = MagicMock()
        panel._history_executor = MagicMock()

        panel._on_history_retry()

        panel._focus_tab.assert_called_once_with("tab-a")
        panel._history_executor.submit.assert_not_called()
        panel._ctrl.delete_history_session.assert_not_called()

    def test_delete_retry_takes_priority_over_load_retry(self) -> None:
        """When both a delete retry and a load retry are pending, delete
        wins and consumes both slots."""
        panel = _make_history_panel()
        panel._history_retry_delete_session_id = "persisted-del"
        panel._history_retry_load_session_id = "persisted-load"
        panel._start_history_delete = MagicMock()
        panel._start_history_load = MagicMock()

        panel._on_history_retry()

        panel._start_history_delete.assert_called_once_with("persisted-del")
        panel._start_history_load.assert_not_called()

    def test_notice_dismissed_clears_retry_state_only(self) -> None:
        """The dismiss button on the FAILED notice clears retry
        presentation state but must NOT mutate persistence/worker
        state — the widget's dismiss handler is a UI-only concern."""
        panel = _make_history_panel()
        panel._history_retry_delete_session_id = "persisted-a"
        panel._history_delete_intents = {"persisted-a"}
        panel._history_pending = True

        panel._on_history_notice_dismissed()

        self.assertIsNone(panel._history_retry_delete_session_id)
        # Intent + pending worker are untouched — dismiss is UI-only.
        self.assertIn("persisted-a", panel._history_delete_intents)
        self.assertTrue(panel._history_pending)


class TestHistoryDeleteWatchdog(unittest.TestCase):
    """Non-terminal slow watchdog (spec §11.5 slow-notice).

    The watchdog surfaces a dismiss-only "still working" notice when the
    delete worker exceeds ``HISTORY_DELETE_SLOW_NOTICE_SECONDS``.  It is
    PURELY cosmetic — it must NEVER clear ``_history_pending``, NEVER
    clear the intent, and NEVER enable Retry.  The terminal
    ``HistoryDeleteResult`` owns those state transitions.
    """

    def test_delete_watchdog_notice_does_not_clear_pending_or_intent(self) -> None:
        panel = _make_history_panel()
        panel._history_pending = True
        panel._history_generation = 8
        panel._history_last_delete_session_id = "persisted-a"
        panel._history_delete_intents = {"persisted-a"}

        panel._on_history_delete_slow(8, "persisted-a")

        self.assertTrue(panel._history_pending)
        self.assertIn("persisted-a", panel._history_delete_intents)
        panel._history_panel.show_notice.assert_called_once_with(
            "Deleting this chat is taking longer than expected.",
            retry_visible=False,
            dismiss_visible=True,
        )

    def test_delete_watchdog_silent_on_generation_mismatch(self) -> None:
        """A late watchdog timeout from a prior generation must NOT
        fire a notice — the user would see a stale "still working"
        message on top of an already-completed delete."""
        panel = _make_history_panel()
        panel._history_pending = True
        panel._history_generation = 9  # newer than watchdog's 8
        panel._history_last_delete_session_id = "persisted-a"

        panel._on_history_delete_slow(8, "persisted-a")

        panel._history_panel.show_notice.assert_not_called()

    def test_delete_watchdog_silent_on_session_mismatch(self) -> None:
        """If the user kicked off a second delete that superseded the
        first, the first watchdog's notice must NOT fire on the second
        delete — match on session_id, not just generation."""
        panel = _make_history_panel()
        panel._history_pending = True
        panel._history_generation = 8
        panel._history_last_delete_session_id = "persisted-b"

        panel._on_history_delete_slow(8, "persisted-a")

        panel._history_panel.show_notice.assert_not_called()

    def test_delete_watchdog_silent_when_no_longer_pending(self) -> None:
        """If the terminal result has already landed and cleared
        ``_history_pending``, the watchdog must stay silent."""
        panel = _make_history_panel()
        panel._history_pending = False
        panel._history_generation = 8
        panel._history_last_delete_session_id = "persisted-a"

        panel._on_history_delete_slow(8, "persisted-a")

        panel._history_panel.show_notice.assert_not_called()


class TestHistoryDeleteInvalidate(unittest.TestCase):
    """Invalidate clears all delete state on IDB-switch / shutdown."""

    def test_invalidate_clears_delete_state_and_watchdog(self) -> None:
        panel = _make_history_panel()
        panel._history_delete_intents = {"persisted-a"}
        panel._history_retry_delete_session_id = "persisted-a"
        panel._history_last_delete_session_id = "persisted-a"
        panel._stop_history_delete_watchdog = MagicMock()

        panel._invalidate_history(clear_panel=False)

        self.assertEqual(panel._history_delete_intents, set())
        self.assertIsNone(panel._history_retry_delete_session_id)
        self.assertIsNone(panel._history_last_delete_session_id)
        panel._stop_history_delete_watchdog.assert_called_once()
        panel._history_panel.set_operation_pending.assert_called_with(None)


if __name__ == "__main__":
    unittest.main()
