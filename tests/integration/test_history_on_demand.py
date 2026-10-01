"""End-to-end behavioral regression for Chat History On-Demand (Task 11).

This integration test stitches together the real persistence layer
(``SessionHistory``), the real controller history APIs
(``capture_history_scope`` / ``list_history_sessions`` /
``load_history_session`` / ``attach_history_session`` /
``find_tab_for_session``), and the real ``RikuganPanelCore`` history
coordinator (``_start_history_list_request`` / ``_history_list_worker`` /
``_drain_history_results`` / ``_on_history_open_requested`` /
``_apply_history_loaded`` / ``on_database_changed`` / ``_invalidate_history``)
to verify the spec section 14.5 behavioral outline.

The panel is built via ``RikuganPanelCore.__new__`` so ``__init__`` (which
would touch every heavy IDA / Qt dependency) is bypassed. The
history-coordinator fields are seeded manually, mirroring the
``_make_history_panel`` idiom in ``tests/tools/test_panel_core.py``. The
controller is a REAL ``IdaSessionController`` backed by a temp config /
session directory so ``SessionHistory`` actually writes JSON, builds a
manifest, and validates current-IDB matching.

Per the brief, this test must NOT depend on ``install_ida_mocks`` ordering
nor on the process-wide IDA-flag state cached in ``rikugan.core.host``.
Each IDB path is assigned a deterministic 32-hex ``db_instance_id`` via a
module-local map; ``host.set_database_instance_id`` /
``host.get_database_instance_id`` are monkey-patched so the controller's
``_ensure_db_instance_id`` returns the same stable id for the same IDB
across controller instances. This mirrors what IDA's persistent netnode
provides in production.

Behavioral outline (brief Step 1):

    persist 2 IDB-A sessions + 1 IDB-B session
    panel_a = make_panel(idb_a)
    assert panel_a.tab_count() == 1
    assert panel_a.active_session.messages == []
    panel_a.open_history()
    assert panel_a.visible_history_ids() == {a1.id, a2.id}
    panel_a.open_history_session(a1.id)
    assert panel_a.tab_count() == 2
    panel_a.open_history_session(a1.id)
    assert panel_a.tab_count() == 2  # focus, no duplicate
    panel_a.on_database_changed(idb_b)
    assert panel_a.tab_count() == 1
    assert panel_a.active_session.messages == []
    panel_a.open_history()
    assert panel_a.visible_history_ids() == {b1.id}
    panel_restart = make_panel(idb_b)
    assert panel_restart.tab_count() == 1
    assert panel_restart.active_session.messages == []

Deterministic queue drains and direct slot calls are used in place of
sleeps -- no ``time.sleep`` appears anywhere in the file.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path + IDA mocks: install BEFORE importing anything from rikugan so
# ``rikugan.core.host`` caches the IDA path (HOST_IDA) and our patched
# netnode-backed database-instance helpers are used.
# ---------------------------------------------------------------------------
_TESTS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TESTS_ROOT)

from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

# Defensive: drop any ``_StubModule`` entries a sibling test file
# (e.g. ``tests/tools/test_panel_core.py``) left in ``sys.modules``
# before we import the real rikugan modules. Without this purge, the
# integration test would see ``MagicMock`` instances for
# ``rikugan.core.types.Message`` etc. when collected after a panel-core
# test in the same pytest invocation. Same pattern as
# ``tests/providers/test_providers.py``.
from tests import purge_rikugan_stubs

purge_rikugan_stubs()

# Install PySide6 stubs before importing panel_core (which imports Qt).
from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()

# ---------------------------------------------------------------------------
# Stub heavy rikugan submodules so ``panel_core`` can be imported without
# the real provider/agent/theme stack. Same idiom as
# ``tests/tools/test_panel_core.py``: a per-module ``MagicMock`` fallback
# keeps the stub resilient to new style getters.
# ---------------------------------------------------------------------------
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
    "rikugan.core.logging",
    "rikugan.agent.turn",
    "rikugan.agent.mutation",
    "rikugan.providers.auth_cache",
    "rikugan.providers.anthropic_provider",
    "rikugan.providers.ollama_provider",
    "rikugan.providers.registry",
]

_STUBBED_MODULE_BACKUPS: dict[str, object] = {name: sys.modules.get(name) for name in _STUBBED_MODULES}


class _StubModule(types.ModuleType):
    """Module whose every attribute resolves to a fresh ``MagicMock``."""

    def __getattr__(self, name):
        m = MagicMock()
        object.__setattr__(self, name, m)
        return m


for _mod_name in _STUBBED_MODULES:
    _stub = _StubModule(_mod_name)
    for _attr in [
        "build_small_button_stylesheet",
        "maybe_host_stylesheet",
        "use_native_host_theme",
        "ChatView",
        "InputArea",
        "ContextBar",
        "_SharedSpinnerTimer",
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

# ``DEFAULT_OLLAMA_URL`` must be a real string for comparisons.
_ollama_stub = sys.modules.get("rikugan.providers.ollama_provider")
if _ollama_stub and not isinstance(getattr(_ollama_stub, "DEFAULT_OLLAMA_URL", None), str):
    _ollama_stub.DEFAULT_OLLAMA_URL = "http://localhost:11434"


# Theme-manager stub so panel shutdown can disconnect from a real signal.
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

# Force-remove any prior stub for panel_core so we import cleanly here.
sys.modules.pop("rikugan.ui.panel_core", None)

import pytest

from rikugan.core.config import RikuganConfig
from rikugan.core.types import Message, Role
from rikugan.ida.ui.session_controller import IdaSessionController
from rikugan.state.history import SessionHistory
from rikugan.ui.panel_core import RikuganPanelCore


@pytest.fixture(scope="module", autouse=True)
def _restore_rikugan_modules_after_integration_tests():
    """Restore the real rikugan modules once this test module finishes.

    Task 6 isolation: the host helper patches are applied per-test in
    ``setUp`` and restored in ``tearDown`` (see
    ``_apply_host_helper_patches`` / ``_restore_host_helper_patches``)
    so they never leak across test modules regardless of collection
    order.  This fixture only restores the heavier module-level stubs
    (``rikugan.ui.chat_view`` etc.) that panel_core imports at module
    scope.
    """
    yield
    for name, original in _STUBBED_MODULE_BACKUPS.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original
    # Defensive: also restore host helpers at module teardown in case a
    # test crashed before ``tearDown`` could run.  Idempotent.
    _restore_host_helper_patches()


# ---------------------------------------------------------------------------
# Stable IDB-instance identity for the test run.
#
# Production reads/writes ``db_instance_id`` through an IDA netnode so the
# same IDB yields the same id across ``IdaSessionController`` instances.
# ``tests/mocks/ida_mock.py`` already provides a ``_PersistentNetnode`` that
# achieves this when the mock is installed before ``rikugan.core.host`` is
# imported. However, collection-order pollution (another test module
# importing ``rikugan.core.host`` before us with ``is_ida() == False``) can
# disable the persistent path. Rather than depend on collection order, we
# install a stable path -> id map and monkey-patch the host helpers.
# ---------------------------------------------------------------------------
_IDB_INSTANCE_MAP: dict[str, str] = {}


def _stable_instance_for(idb_path: str) -> str:
    """Return a deterministic 32-hex id for ``idb_path`` (assigned lazily).

    Uses uuid5 (deterministic, no hashseed) so the same idb_path yields
    the same 32-hex lowercase id across test runs and across processes.
    """
    import uuid

    normalized = os.path.normcase(os.path.realpath(idb_path))
    if normalized not in _IDB_INSTANCE_MAP:
        # uuid5 is deterministic across runs (unlike Python's hash()).
        # The result is a 32-char lowercase hex string, matching what
        # ``rikugan.core.host.set_database_instance_id`` writes to the
        # netnode and what ``_canonical_instance_id`` expects.
        _IDB_INSTANCE_MAP[normalized] = uuid.uuid5(uuid.NAMESPACE_URL, f"rikugan-test://{normalized}").hex
    return _IDB_INSTANCE_MAP[normalized]


# Rebind the host helpers used by the controller. The controller module
# does ``from ..core.host import get_database_instance_id`` at import
# time, which binds the FUNCTION OBJECT (not a deferred lookup) into its
# own namespace. So patching only ``rikugan.core.host.X`` has no effect:
# the controller keeps the original reference. We must rebind BOTH the
# host module attributes AND the controller module's bound imports.
import rikugan.core.host as _host
import rikugan.ida.ui.session_controller as _ida_sc
import rikugan.ui.session_controller_base as _scb

_CURRENT_IDB_PATH = {"path": ""}


def _patched_get_database_instance_id() -> str:
    path = _CURRENT_IDB_PATH["path"]
    if not path:
        return ""
    return _stable_instance_for(path)


def _patched_set_database_instance_id(_instance_id: str) -> bool:
    # The controller always passes a freshly-derived id for the current
    # path. We accept it and treat the mapping as idempotent so the id
    # never drifts within a test run.
    return True


def _patched_get_database_path() -> str:
    """Return the live IDB path the test scenario has armed.

    Task 6 (spec §11.5): the controller's delete boundary compares the
    captured ``HistoryScope.idb_path`` against the live ``_idb_path``
    that ``IdaSessionController`` materialized via
    ``database_path_getter`` (``rikugan.core.host.get_database_path``).
    The IDA mock returns a fixed ``d:\\tmp\\ida_test\\test.idb``, which
    does not match the temp paths we persist sessions under.  Without
    this patch the controller's ``_idb_path`` is the mock's default,
    the delete boundary returns ``WRONG_IDB``, and the row is never
    removed.  We mirror the existing
    ``_patched_get_database_instance_id`` pattern: rebind BOTH the host
    module attribute AND the IDA session-controller's bound import so
    the function object captured by ``database_path_getter=...`` in
    ``IdaSessionController.__init__`` resolves to the patched path.
    """
    return _CURRENT_IDB_PATH["path"]


# Task 6 isolation: snapshot the ORIGINAL helpers BEFORE overwriting
# them so the test class's ``setUp`` / ``tearDown`` can apply / restore
# the patches per-test.  Module-level patching (the pre-Task-6 idiom)
# leaked into subsequently-collected test modules (e.g.
# ``tests/agent/test_session_controller.py`` whose
# ``test_restore_preserves_*`` cases construct fresh
# ``IdaSessionController`` instances that read these helpers and depend
# on the IDA mock's ``_PersistentNetnode`` behavior, not our path-keyed
# stub).  Because pytest imports every collected module BEFORE running
# any test, module-level patches were active during
# ``test_session_controller``'s execution even though the integration
# test had not armed ``_CURRENT_IDB_PATH`` yet, producing spurious
# ``WRONG_IDB`` failures.  Per-test ``setUp`` / ``tearDown`` patching
# keeps the integration test hermetic and lets the rest of the suite
# see the IDA mock's original behavior.
_HOST_HELPER_BACKUPS: list[tuple[object, str, object]] = [
    (_host, "get_database_instance_id", _host.get_database_instance_id),
    (_host, "set_database_instance_id", _host.set_database_instance_id),
    (_host, "get_database_path", _host.get_database_path),
    (_scb, "get_database_instance_id", _scb.get_database_instance_id),
    (_scb, "set_database_instance_id", _scb.set_database_instance_id),
    (_ida_sc, "get_database_path", _ida_sc.get_database_path),
]


def _apply_host_helper_patches() -> None:
    """Install the path-keyed host helper stubs (call from ``setUp``).

    Idempotent: re-applying over an already-patched module is safe
    because the writes are simple attribute rebindings.
    """
    _host.get_database_instance_id = _patched_get_database_instance_id
    _host.set_database_instance_id = _patched_set_database_instance_id
    _host.get_database_path = _patched_get_database_path
    # CRITICAL: rebind the controller's already-imported references too.
    _scb.get_database_instance_id = _patched_get_database_instance_id
    _scb.set_database_instance_id = _patched_set_database_instance_id
    # Task 6: rebind the IDA controller's bound ``get_database_path``
    # import so the controller's ``_idb_path`` matches the path we
    # persisted sessions under.
    _ida_sc.get_database_path = _patched_get_database_path


def _restore_host_helper_patches() -> None:
    """Restore the original host helpers (call from ``tearDown``).

    Iterates the ``_HOST_HELPER_BACKUPS`` snapshot captured at module
    import time.  Swallows ``AttributeError`` / ``TypeError`` so a
    partial-init fixture does not raise during teardown.
    """
    for module, attr, original in _HOST_HELPER_BACKUPS:
        try:
            setattr(module, attr, original)
        except (AttributeError, TypeError):
            pass


# ---------------------------------------------------------------------------
# Minimal tracking QTabWidget stub. Production calls ``addTab`` /
# ``removeTab`` / ``setCurrentIndex`` / ``indexOf`` / ``widget`` / ``count``.
# We track inserted widgets on a list so ``tab_count()`` reflects reality.
# ---------------------------------------------------------------------------
class _FakeTabWidget:
    def __init__(self):
        self._tabs: list = []
        self._current = -1

    def addTab(self, widget, label):
        self._tabs.append(widget)
        self._current = len(self._tabs) - 1
        return self._current

    def removeTab(self, index):
        if 0 <= index < len(self._tabs):
            del self._tabs[index]
            if self._current >= len(self._tabs):
                self._current = len(self._tabs) - 1

    def insertTab(self, index, widget, label):
        self._tabs.insert(index, widget)
        self._current = index
        return index

    def setCurrentIndex(self, index):
        self._current = index

    def currentIndex(self):
        return self._current

    def count(self):
        return len(self._tabs)

    def widget(self, index):
        if 0 <= index < len(self._tabs):
            return self._tabs[index]
        return None

    def indexOf(self, widget):
        try:
            return self._tabs.index(widget)
        except ValueError:
            return -1


class _FakeSignal:
    def __init__(self):
        self._slots: list = []

    def connect(self, slot):
        self._slots.append(slot)

    def disconnect(self, slot=None):
        if slot is None:
            self._slots.clear()
        elif slot in self._slots:
            self._slots.remove(slot)

    def emit(self, *_args, **_kwargs):
        for slot in list(self._slots):
            slot(*_args, **_kwargs)


class _FakeChatView:
    """Minimal stand-in for ``ChatView``.

    ``_create_tab`` connects Qt signals on the view; we expose
    ``setProperty`` for the ``tab_id`` lookup and no-op ``shutdown`` /
    ``restore_from_messages_async`` so the panel can tear us down without
    crashing.
    """

    def __init__(self):
        self._props: dict[str, object] = {}
        # Attributes accessed via ``chat_view.tool_approval_submitted.connect``
        # etc. -- fake Qt signals.
        self.tool_approval_submitted = _FakeSignal()
        self.user_answer_submitted = _FakeSignal()
        self.orchestra_approval_decided = _FakeSignal()

    def setProperty(self, name, value):
        self._props[name] = value

    def property(self, name):
        return self._props.get(name)

    def shutdown(self):
        pass

    def restore_from_messages_async(self, _messages):
        pass

    def deleteLater(self):
        pass


# Replace the stubbed ``ChatView`` import on the stub module with our fake
# so ``_create_tab`` constructs instances of ``_FakeChatView``.
_chat_view_stub = sys.modules.get("rikugan.ui.chat_view")
if _chat_view_stub is not None:
    _chat_view_stub.ChatView = _FakeChatView


# ---------------------------------------------------------------------------
# Recording HistoryPanel -- records the calls PanelCore makes on the
# HistoryPanel. Mirrors the subset of the ``HistoryPanel`` public API
# exercised by PanelCore (``set_entries`` / ``set_loading`` / ``set_error``
# / ``clear`` / ``visible_session_ids`` / ``isVisible``).
# ---------------------------------------------------------------------------
class _RecordingHistoryPanel:
    """Recording HistoryPanel stub.

    Captures the calls PanelCore makes on the real HistoryPanel for the
    on-demand History tests (``set_entries`` / ``set_loading`` /
    ``set_error`` / ``clear`` / ``visible_session_ids`` / ``isVisible``)
    and the Task-5 delete-related calls (``remove_entry`` /
    ``set_operation_pending`` / ``show_notice`` / ``clear_notice``).

    Task 6 (spec §11.5): added ``notice_calls`` /
    ``pending_session_id`` and the delete-side methods so the
    end-to-end deletion regression can assert exact user-visible copy,
    row-removal ordering, and pending-row spinner state without
    depending on a real Qt widget.
    """

    def __init__(self):
        self.entries: list = []
        self.loading_calls = 0
        self.error_calls: list[tuple[str, bool]] = []
        self.cleared = 0
        self._visible = False
        # Task 6 (spec §11.5): recording sink for ``show_notice``.
        # Each tuple is (message, retry_visible, dismiss_visible) so the
        # integration test can assert the exact notice copy AND the
        # retry/dismiss button visibility the panel chose.
        self.notice_calls: list[tuple[str, bool, bool]] = []
        # Task 6: pending-row state.  ``set_operation_pending`` is called
        # by ``_start_history_delete`` (with the session id) and by
        # terminal apply / invalidate (with None).  We stash the last
        # value so the test can assert the spinner pinning behavior.
        self.pending_session_id: str | None = None

    def setVisible(self, visible):
        self._visible = bool(visible)

    def isVisible(self):
        return self._visible

    def set_entries(self, entries):
        self.entries = list(entries)

    def set_loading(self):
        self.loading_calls += 1

    def set_error(self, message, retry_visible=True):
        self.error_calls.append((message, bool(retry_visible)))

    def clear(self):
        self.cleared += 1
        self.entries = []

    def visible_session_ids(self):
        return [e.session_id for e in self.entries]

    def shutdown(self):
        pass

    # ------------------------------------------------------------------
    # Task 6 (spec §11.5): delete-side recording methods.
    # ------------------------------------------------------------------
    def remove_entry(self, session_id):
        """Row removal on terminal DELETED / NOT_FOUND apply.

        Production HistoryPanel rebuilds the filtered list; here we
        simply drop the matching entry so ``visible_session_ids()``
        reflects the post-delete state.
        """
        self.entries = [entry for entry in self.entries if entry.session_id != session_id]

    def set_operation_pending(self, session_id):
        """Spinner-pinning state for the delete flow.

        ``_start_history_delete`` pins the row by id; terminal apply /
        invalidate clears it via ``None``.  We store the latest value
        verbatim so the test can assert the pinning behavior exactly.
        """
        self.pending_session_id = session_id

    def show_notice(self, message, *, retry_visible=False, dismiss_visible=True):
        """Notice banner sink.

        Records the exact ``(message, retry_visible, dismiss_visible)``
        triple so the integration test can assert the precise user copy
        and button visibility for every notice path (busy, open-tab,
        FAILED, slow-delete).  Mirrors the production HistoryPanel
        signature.
        """
        self.notice_calls.append((message, bool(retry_visible), bool(dismiss_visible)))

    def clear_notice(self):
        """Notice banner clear.

        Production hides the notice widget; here the sink is a no-op
        because the assertion surface is the ``notice_calls`` list
        itself (a clear does not produce a new entry to assert on).
        """
        pass


def _build_panel(ctrl: IdaSessionController) -> RikuganPanelCore:
    """Construct a real ``RikuganPanelCore`` instance via ``__new__``.

    Bypasses ``__init__`` (which would touch every IDA / provider / Qt
    dependency) and seeds the history-coordinator fields the brief lists
    so the panel's history code paths can be exercised deterministically.
    """
    panel = RikuganPanelCore.__new__(RikuganPanelCore)
    panel._is_shutdown = False
    panel._polling = False
    panel._pending_answer = False
    panel._chat_views: dict[str, object] = {}
    panel._pending_restore_messages: dict[str, list] = {}
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
    panel._tab_bar = MagicMock()
    panel._ui_hooks = None
    panel._awaiting_button_approval = False
    # The real controller. Tests that need to read live controller state
    # (``active_session`` / ``tab_count``) do so through this object.
    panel._ctrl = ctrl
    panel._config = ctrl.config
    # Tracking tab widget so ``tab_count()`` reflects real ``addTab`` /
    # ``removeTab`` calls made by the panel's history slots.
    panel._tab_widget = _FakeTabWidget()
    # History coordinator fields (Task 8/9/10 -- spec section 8.1, 11.4).
    panel._history_panel = _RecordingHistoryPanel()
    panel._history_generation = 0
    panel._history_executor = None
    import queue

    panel._history_result_queue = queue.Queue(maxsize=2)
    panel._history_poll_timer = None
    panel._history_pending = False
    panel._history_closing = threading.Event()
    panel._history_retry_load_session_id = None
    panel._history_last_load_session_id = None
    # Task 6 (spec §11.5): seed the Task-5 delete-coordinator fields
    # explicitly so the production code paths no longer need to fall
    # back on the ``getattr(..., default)`` guards.  This mirrors the
    # field shape ``__init__`` would have created and keeps the
    # integration test hermetic with respect to deletion state.
    panel._history_retry_delete_session_id = None
    panel._history_last_delete_session_id = None
    panel._history_delete_intents = set()
    panel._history_delete_watchdog = None
    # Initial tab: panel construction always creates exactly one empty
    # ``New Chat`` tab in production (``_build_ui``); replicate that here
    # so ``tab_count() == 1`` from the start.
    panel._create_tab(ctrl.active_tab_id, "New Chat")
    return panel


class _HistoryOnDemandFacade:
    """High-level facade that maps the brief's behavioral outline onto
    real panel slot calls.

    Each method is a thin wrapper over the production panel slot so the
    integration test asserts behavior through the same code paths IDA's
    UI would trigger. No ``time.sleep`` -- workers are drained
    deterministically via ``_drain_history_results`` after the worker
    future resolves.
    """

    def __init__(self, panel: RikuganPanelCore):
        self._panel = panel

    # --- assertions helpers --------------------------------------------
    def tab_count(self) -> int:
        return self._panel._tab_widget.count()

    @property
    def active_session(self):
        return self._panel._ctrl.session

    def visible_history_ids(self) -> set[str]:
        return set(self._panel._history_panel.visible_session_ids())

    # --- behavioral actions --------------------------------------------
    def open_history(self) -> None:
        """Toggle the History button ON, then submit a list request.

        Production path: ``_on_history_btn_toggled(True)`` leads to
        ``_show_right_panel("history")`` + ``_start_history_list_request``.
        Here we go straight to ``_start_history_list_request`` after
        marking the panel visible so the drain's visibility check keeps
        the timer alive; the result is drained synchronously.
        """
        self._panel._history_panel.setVisible(True)
        self._panel._start_history_list_request()
        # Wait for the single-worker executor to finish the list job so
        # the typed result is in the queue. The executor is a
        # ``ThreadPoolExecutor(max_workers=1)``; ``shutdown(wait=True)``
        # blocks until the worker enqueues and exits, with no timeout
        # because the worker has no external dependency beyond
        # ``SessionHistory`` (which reads a local temp directory).
        executor = self._panel._history_executor
        if executor is not None:
            executor.shutdown(wait=True)
            # Re-create the executor so a subsequent open_history /
            # open_history_session can submit again. In production the
            # panel reuses the executor until ``_invalidate_history``
            # drops it; here we mirror that lazily-fresh behavior.
            from concurrent.futures import ThreadPoolExecutor

            self._panel._history_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="rikugan-history",
            )
        self._panel._drain_history_results()

    def open_history_session(self, session_id: str) -> None:
        """Click a row: pre-dedupe, load worker, drain, apply attach."""
        self._panel._on_history_open_requested(session_id)
        executor = self._panel._history_executor
        if executor is not None:
            executor.shutdown(wait=True)
            from concurrent.futures import ThreadPoolExecutor

            self._panel._history_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="rikugan-history",
            )
        self._panel._drain_history_results()

    def delete_history_session(self, session_id: str) -> None:
        """Click the row's delete button, confirm, drain, and reconcile.

        Task 6 (spec §11.5): drives the production delete seam
        end-to-end.  Pre-confirms via ``_confirm_history_delete`` (the
        panel's modal is patched to ``MagicMock(return_value=True)`` so
        no Qt dialog is raised), then calls the row-delete slot
        ``_on_history_delete_requested`` exactly as the widget signal
        would.  Branches on ``_history_pending``:

          * If the slot submitted a worker (the normal path), wait for
            the single-worker executor to finish the delete job so the
            typed ``HistoryDeleteResult`` is in the queue, then drop
            the executor reference (next production request will
            lazily recreate it) and drain.
          * If the slot refused to submit (open-tab invariant, busy,
            cancel), ``_history_pending`` stays False and there is no
            executor to drain -- return immediately.

        A successful DELETED / NOT_FOUND result queues exactly one
        reconciliation list request from ``_apply_history_deleted``;
        we drain that too so the cached list reconciles with the new
        on-disk state before the test asserts on it.
        """
        self._panel._confirm_history_delete = MagicMock(return_value=True)
        entry = next(entry for entry in self._panel._history_panel.entries if entry.session_id == session_id)
        self._panel._on_history_delete_requested(session_id, entry.title)
        if not self._panel._history_pending:
            # Open-tab/busy/cancel path: no worker was submitted.
            return

        executor = self._panel._history_executor
        assert executor is not None
        executor.shutdown(wait=True)
        self._panel._history_executor = None
        self._panel._drain_history_results()

        # DELETED/NOT_FOUND queues one reconciliation list request.
        if self._panel._history_pending:
            executor = self._panel._history_executor
            assert executor is not None
            executor.shutdown(wait=True)
            self._panel._history_executor = None
            self._panel._drain_history_results()

    def on_database_changed(self, new_path: str) -> None:
        # Sync the stable-instance map cell BEFORE the controller reset
        # so ``_ensure_db_instance_id`` reads the new id.
        _CURRENT_IDB_PATH["path"] = new_path
        self._panel.on_database_changed(new_path)

    def shutdown(self) -> None:
        self._panel._is_shutdown = True
        try:
            self._panel._invalidate_history(clear_panel=False)
        except Exception:
            pass


def _persist_session(
    cfg: RikuganConfig,
    *,
    idb_path: str,
    db_instance_id: str,
    user_content: str,
    assistant_content: str = "ok",
) -> str:
    """Persist one non-empty session for ``idb_path`` via ``SessionHistory``.

    Returns the persisted ``SessionState.id`` so the test can reference it
    by its manifest identity (not its in-memory tab id).
    """
    from rikugan.state.session import SessionState

    session = SessionState(
        idb_path=idb_path,
        db_instance_id=db_instance_id,
    )
    session.add_message(Message(role=Role.USER, content=user_content))
    session.add_message(Message(role=Role.ASSISTANT, content=assistant_content))
    SessionHistory(cfg).save_session(session)
    SessionHistory.flush_saves()
    return session.id


# ---------------------------------------------------------------------------
# The behavioral regression scenario (spec section 14.5).
# ---------------------------------------------------------------------------
class TestHistoryOnDemandIntegration(unittest.TestCase):
    """Spec section 14.5 behavioral outline as one deterministic scenario."""

    def setUp(self):
        # Fresh IDB-instance map so each test is hermetic.
        _IDB_INSTANCE_MAP.clear()
        # Fresh controller-backed temp config dir for real persistence.
        self._tmp_root = tempfile.mkdtemp(prefix="rikugan-int-")
        self.cfg = RikuganConfig()
        self.cfg._config_dir = self._tmp_root

        # IDB-A / IDB-B paths inside the same temp tree.
        self.idb_a = os.path.join(self._tmp_root, "a.i64")
        self.idb_b = os.path.join(self._tmp_root, "b.i64")
        # Stable ids for each IDB, matching what the patched host helpers
        # would yield for those paths.
        _CURRENT_IDB_PATH["path"] = self.idb_a
        self.idb_a_instance = _stable_instance_for(self.idb_a)
        self.idb_b_instance = _stable_instance_for(self.idb_b)

        # Task 6 isolation: apply the path-keyed host helper patches
        # per-test BEFORE any controller construction.  The patches
        # resolve the live db_instance_id / idb_path from
        # ``_CURRENT_IDB_PATH`` so the controller sees the same stable
        # ids we persist with.  Per-test application (vs module-level)
        # keeps the patches from leaking into other test modules.
        _apply_host_helper_patches()

        # Persist two non-empty IDB-A sessions + one IDB-B session BEFORE
        # constructing any panel/controller. This mirrors "prior sessions
        # exist on disk" -- the brief's starting state.
        self.a1_id = _persist_session(
            self.cfg,
            idb_path=self.idb_a,
            db_instance_id=self.idb_a_instance,
            user_content="Analyze parser",
            assistant_content="parser ok",
        )
        self.a2_id = _persist_session(
            self.cfg,
            idb_path=self.idb_a,
            db_instance_id=self.idb_a_instance,
            user_content="Triage imports",
            assistant_content="imports ok",
        )
        self.b1_id = _persist_session(
            self.cfg,
            idb_path=self.idb_b,
            db_instance_id=self.idb_b_instance,
            user_content="Identify crypto",
            assistant_content="crypto ok",
        )

        # The controller uses ``_config_dir`` to find the sessions dir.
        # The patched host helpers resolve the live db_instance_id from
        # ``_CURRENT_IDB_PATH`` so the controller sees the same stable
        # ids we persisted with.
        _CURRENT_IDB_PATH["path"] = self.idb_a
        self.ctrl = IdaSessionController(self.cfg)
        self.panel = _build_panel(self.ctrl)
        self.facade = _HistoryOnDemandFacade(self.panel)

    def tearDown(self):
        try:
            self.facade.shutdown()
        finally:
            SessionHistory.flush_saves()
            self.ctrl.shutdown()
            shutil.rmtree(self._tmp_root, ignore_errors=True)
            # Task 6 isolation: restore the original host helpers so the
            # patches do not leak into subsequently-run tests from other
            # modules.  Must run AFTER the controller + panel shutdown so
            # any final controller reads still see the patched values.
            _restore_host_helper_patches()

    def test_startup_one_empty_new_chat(self):
        """Spec 7.1: opening Rikugan shows exactly one empty New Chat."""
        self.assertEqual(self.facade.tab_count(), 1)
        self.assertEqual(self.facade.active_session.messages, [])

    def test_history_lists_only_current_idb(self):
        """Spec 8.3: History lists only sessions for the current IDB."""
        self.facade.open_history()
        self.assertEqual(
            self.facade.visible_history_ids(),
            {self.a1_id, self.a2_id},
        )

    def test_open_creates_one_tab_then_focus_no_duplicate(self):
        """Spec 10.2: open reuses the empty active draft, so the tab count
        does not grow; selecting again focuses it.
        """
        self.assertEqual(self.facade.tab_count(), 1)
        self.facade.open_history()
        self.facade.open_history_session(self.a1_id)
        # Active tab was an empty draft -> reused in place, no new tab.
        self.assertEqual(self.facade.tab_count(), 1)
        # Selecting the SAME persisted id again must focus the existing
        # tab, not open a duplicate (pre- or post-load dedupe).
        self.facade.open_history_session(self.a1_id)
        self.assertEqual(self.facade.tab_count(), 1)

    def test_idb_switch_resets_one_empty_new_chat(self):
        """Spec 7.2: switching IDBs leaves exactly one empty New Chat
        for the new IDB. The previous IDB's chats remain on disk.
        """
        self.facade.on_database_changed(self.idb_b)
        self.assertEqual(self.facade.tab_count(), 1)
        self.assertEqual(self.facade.active_session.messages, [])
        self.facade.open_history()
        self.assertEqual(
            self.facade.visible_history_ids(),
            {self.b1_id},
        )

    def test_restart_again_starts_one_empty_new_chat(self):
        """Spec 14.5 step 11: restarting the panel again starts with
        one empty tab. The previous IDB's chats are still on disk.
        """
        # First switch to IDB-B so the restart scenario mirrors the
        # brief outline (panel_restart = make_panel(idb_b)).
        self.facade.on_database_changed(self.idb_b)
        # Simulate a panel restart: tear down the current panel and
        # construct a fresh one for IDB-B.
        self.facade.shutdown()
        SessionHistory.flush_saves()
        self.ctrl.shutdown()

        _CURRENT_IDB_PATH["path"] = self.idb_b
        ctrl2 = IdaSessionController(self.cfg)
        try:
            panel2 = _build_panel(ctrl2)
            facade2 = _HistoryOnDemandFacade(panel2)
            self.assertEqual(facade2.tab_count(), 1)
            self.assertEqual(facade2.active_session.messages, [])
            facade2.shutdown()
        finally:
            SessionHistory.flush_saves()
            ctrl2.shutdown()

    def test_full_scenario_in_one_flow(self):
        """The complete behavioral outline from the brief, executed in
        sequence against one panel/controller pair.

        Asserts every step the brief lists, in order, so a regression in
        any of the sub-behaviors fails at the specific assertion that
        detected it.
        """
        # Step 1: startup exactly one empty New Chat.
        self.assertEqual(self.facade.tab_count(), 1)
        self.assertEqual(self.facade.active_session.messages, [])

        # Step 2: open History -- only IDB-A entries appear.
        self.facade.open_history()
        self.assertEqual(
            self.facade.visible_history_ids(),
            {self.a1_id, self.a2_id},
        )

        # Step 3: open a1 -> reuses the empty active draft (REUSED), so
        # the tab count stays 1 but the session is now a1.
        self.facade.open_history_session(self.a1_id)
        self.assertEqual(self.facade.tab_count(), 1)

        # Step 4: open a1 again -> focus, no duplicate (ALREADY_OPEN).
        self.facade.open_history_session(self.a1_id)
        self.assertEqual(self.facade.tab_count(), 1)

        # Step 5: switch to IDB-B -> exactly one empty New Chat.
        self.facade.on_database_changed(self.idb_b)
        self.assertEqual(self.facade.tab_count(), 1)
        self.assertEqual(self.facade.active_session.messages, [])

        # Step 6: open History -- only IDB-B's entry appears.
        self.facade.open_history()
        self.assertEqual(self.facade.visible_history_ids(), {self.b1_id})

        # Step 7: restart panel for IDB-B -> again exactly one empty tab.
        self.facade.shutdown()
        SessionHistory.flush_saves()
        self.ctrl.shutdown()
        _CURRENT_IDB_PATH["path"] = self.idb_b
        ctrl2 = IdaSessionController(self.cfg)
        try:
            panel2 = _build_panel(ctrl2)
            facade2 = _HistoryOnDemandFacade(panel2)
            self.assertEqual(facade2.tab_count(), 1)
            self.assertEqual(facade2.active_session.messages, [])
            facade2.shutdown()
        finally:
            SessionHistory.flush_saves()
            ctrl2.shutdown()

    # ------------------------------------------------------------------
    # Task 6: End-to-end deletion regression (spec §11.5).
    #
    # These four scenarios stitch together the real persistence layer
    # (ordered primary -> sidecar -> manifest delete), the real
    # controller delete boundary (scope + status mapping), and the real
    # PanelCore delete coordinator (confirm -> intent -> worker ->
    # apply -> reconcile).  Together they prove that:
    #
    #   * a closed chat can be permanently removed for the current IDB
    #     only (sibling IDBs are untouched) and stays gone after a
    #     panel restart;
    #   * an open chat is focused, not deleted, and the disk is left
    #     untouched;
    #   * a LOAD that lands while a DELETE is confirmed is dropped
    #     before attach (no resurrection);
    #   * a primary-file failure keeps the row and records retry state;
    #     Retry re-dispatches the DELETE without re-confirming and
    #     succeeds when the OS condition clears.
    # ------------------------------------------------------------------

    def test_delete_closed_chat_is_permanent_and_scoped(self):
        """Task 6 spec §11.5 terminal success path.

        Deleting a closed chat for the current IDB removes the primary
        JSON, its summary sidecar, and the manifest entry.  Sibling
        IDB-B chats are untouched.  Re-opening History after the
        deletion must NOT list the deleted id (no resurrection through
        the manifest or disk).
        """
        self.facade.open_history()
        primary = Path(self.cfg.checkpoints_dir) / "sessions" / f"{self.a1_id}.json"
        sidecar = primary.with_name(f"{self.a1_id}.summary.json")
        sidecar.write_text('{"messages":1}', encoding="utf-8")

        self.facade.delete_history_session(self.a1_id)

        # Row is gone from the cached list immediately after the
        # terminal apply + reconciliation list refresh.
        self.assertNotIn(self.a1_id, self.facade.visible_history_ids())
        # Primary + sidecar files are removed.
        self.assertFalse(primary.exists())
        self.assertFalse(sidecar.exists())
        # Manifest entry is removed.
        manifest = json.loads((primary.parent / "_session_manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn(self.a1_id, manifest["entries"])
        # Sibling IDB-B chat is untouched by the current-IDB delete.
        self.assertTrue(
            (primary.parent / f"{self.b1_id}.json").exists(),
            "IDB-B history must remain untouched",
        )

        # Re-opening History does NOT resurrect the deleted id (no
        # cached list reuse, no stale manifest).
        self.facade.open_history()
        self.assertNotIn(self.a1_id, self.facade.visible_history_ids())

    def test_delete_open_chat_focuses_tab_without_disk_mutation(self):
        """Task 6 spec §11.5 open-tab refusal.

        Deleting an already-open chat focuses the existing tab, surfaces
        the dismiss-only "Close this chat before deleting it from
        History." notice, and never touches disk.  The primary JSON
        remains so a subsequent panel restart can re-list the chat.
        """
        self.facade.open_history()
        self.facade.open_history_session(self.a1_id)
        primary = Path(self.cfg.checkpoints_dir) / "sessions" / f"{self.a1_id}.json"

        self.facade.delete_history_session(self.a1_id)

        self.assertTrue(primary.exists())
        self.assertEqual(
            self.panel._history_panel.notice_calls[-1][0],
            "Close this chat before deleting it from History.",
        )

    def test_confirmed_delete_intent_blocks_loaded_session_attach(self):
        """Task 6 spec §11.5 LOAD → DELETE race.

        Drive the production apply seam deterministically: capture a
        fresh scope, load the session through the controller, set the
        delete-intent gate, and call ``_apply_history_loaded`` directly.
        The load must be dropped BEFORE attach so the session cannot be
        resurrected into a tab.  After the intent is in place, the
        facade's delete must succeed and the session must stay gone on
        disk after a flush.
        """
        self.facade.open_history()
        scope = self.panel._ctrl.capture_history_scope(generation=1)
        loaded = self.panel._ctrl.load_history_session(self.a1_id, scope)
        self.panel._history_delete_intents = {self.a1_id}

        self.panel._apply_history_loaded(loaded)

        # Intent gate must have dropped the attach: no tab is bound to
        # the persisted id.
        self.assertIsNone(self.panel._ctrl.find_tab_for_session(self.a1_id))
        # Facade delete proceeds through the production worker path
        # (confirm -> submit -> apply -> reconcile) and succeeds.
        self.facade.delete_history_session(self.a1_id)
        SessionHistory.flush_saves(timeout=5)
        self.assertIsNone(SessionHistory(self.cfg).load_session(self.a1_id))

    def test_primary_delete_failure_keeps_row_and_retry_succeeds(self):
        """Task 6 spec §11.5 FAILED path + Retry without re-confirm.

        Patch ``os.remove`` in the history module so the target primary
        file raises ``PermissionError`` but every other path (sidecar,
        manifest) is allowed through.  The first delete attempt must:

          * keep the row (FAILED is non-terminal),
          * record the retry-delete id,
          * surface the "Could not delete this chat." notice with both
            Retry and Dismiss visible.

        A subsequent Retry must NOT re-pop the confirmation dialog
        (``_confirm_history_delete`` is replaced with a fresh MagicMock
        to assert non-invocation) and must succeed once the OS lock is
        released.  After Retry the primary file must be gone.
        """
        self.facade.open_history()
        primary = Path(self.cfg.checkpoints_dir) / "sessions" / f"{self.a1_id}.json"
        original_remove = os.remove

        def fail_target(path):
            if os.path.normcase(str(path)) == os.path.normcase(str(primary)):
                raise PermissionError("locked")
            original_remove(path)

        with patch("rikugan.state.history.os.remove", side_effect=fail_target):
            self.facade.delete_history_session(self.a1_id)

        # Row is preserved (FAILED is non-terminal).
        self.assertIn(self.a1_id, self.facade.visible_history_ids())
        # Retry-delete id is the target session.
        self.assertEqual(self.panel._history_retry_delete_session_id, self.a1_id)
        # Notice copy + button visibility.
        self.assertEqual(
            self.panel._history_panel.notice_calls[-1],
            ("Could not delete this chat.", True, True),
        )

        # Retry must NOT re-confirm.  Replacing the slot with a fresh
        # MagicMock lets us assert non-invocation cleanly.
        self.panel._confirm_history_delete = MagicMock()
        self.panel._on_history_retry()
        retry_executor = self.panel._history_executor
        assert retry_executor is not None
        retry_executor.shutdown(wait=True)
        self.panel._history_executor = None
        self.panel._drain_history_results()
        if self.panel._history_pending:
            refresh_executor = self.panel._history_executor
            assert refresh_executor is not None
            refresh_executor.shutdown(wait=True)
            self.panel._history_executor = None
            self.panel._drain_history_results()
        self.panel._confirm_history_delete.assert_not_called()
        self.assertFalse(primary.exists())


if __name__ == "__main__":
    unittest.main()
