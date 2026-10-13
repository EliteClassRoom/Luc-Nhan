"""Shared Luc Nhan panel widget used by host-specific wrappers."""

from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from ..agent.turn import TurnEvent, TurnEventType
from ..constants import HISTORY_DELETE_SLOW_NOTICE_SECONDS
from ..core.config import LucNhanConfig
from ..core.early_log import _early_log, _early_log_crash
from ..core.logging import log_debug, log_error, log_info, log_warning
from ..core.types import Role
from ..providers.auth_cache import resolve_auth_cached
from ..state.history import SessionHistory
from ..state.history_types import (
    HistoryAttachStatus,
    HistoryDeleteResult,
    HistoryDeleteStatus,
    HistoryListResult,
    HistoryLoadResult,
    HistoryRequestStatus,
    HistoryScope,
)

if TYPE_CHECKING:
    from ..state.session import SessionState
    from .agent_tree import AgentInfo

from .chat_view import ChatView
from .context_bar import NARROW_BREAKPOINT_PX, ContextBar
from .export_formatting import (
    _export_format_subagent_log,
    _export_format_tool_args,
    _export_format_tool_result,
)
from .history_panel import HistoryPanel
from .input_area import InputArea, _composer_min_height
from .model_picker import (
    MAX_TRIGGER_WIDTH_RATIO,
    ModelCatalogCache,
    ModelChoice,
    ModelPicker,
)
from .mutation_log_view import MutationLogPanel
from .qt_compat import (
    OK_CANCEL_BUTTONS,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QKeySequence,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QShortcut,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    Qt,
    QTabBar,
    QTimer,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from .session_pages import SessionPages
from .styles import (
    get_cancel_btn_style,
    get_composer_frame_style,
    get_primary_btn_style,
    get_small_btn_style,
    get_transcript_surface_style,
    maybe_host_stylesheet,
    role_size,
    use_native_host_theme,
)
from .theme.manager import ThemeManager
from .tool_widgets import _SharedSpinnerTimer
from .tools_panel import _TOOL_PAGES, ToolsPanel

# History poll interval (ms). Spec §7.4: a dedicated main-thread QTimer
# drains history results so History works while the agent is idle.
# 50ms keeps the UI responsive without busy-looping an idle panel.
_HISTORY_POLL_INTERVAL_MS = 50
# Bounded result queue: one slot for the single in-flight list/load
# request + one for its terminal result (spec §11.4).
_HISTORY_RESULT_QUEUE_MAXSIZE = 2
# Distinct prefix for the history worker pool (spec §6.1, §11.4).
_HISTORY_EXECUTOR_PREFIX = "lucnhan-history"

# Baseline width for the fixed-width action buttons (Send, Stop, New,
# Export, Settings, Mutations, Tools). Square-ish so icon + short label
# fit without the row growing when one button gets a longer label — but
# only a *floor*: the real width is metric-derived (see
# ``_action_button_width``) so a large user font is not clipped.
_ACTION_BUTTON_WIDTH = 64

# GUI contract G2 / G3 — workspace geometry, all values in Qt logical px.
# Header: min-height 38 px, padding 3 vertical / 10 horizontal, gap 10 px
# (5 px at the narrow breakpoint).
_HEADER_MIN_HEIGHT = 38
_HEADER_GAP = 10
_HEADER_GAP_NARROW = 5
# Session strip: min-height 31 px, inset 9 px, gap 7 px.
_SESSION_STRIP_MIN_HEIGHT = 31
# Composer text baseline / minimum, and the action-control row.  Both
# values are *baselines*: the effective minimum is metric-derived
# (``InputArea._composer_min_height``) so a user font override grows the
# composer instead of clipping it.
_COMPOSER_TEXT_HEIGHT = 44
_COMPOSER_MIN_HEIGHT = 38
# Action-row chrome (button padding + border) the composer seed adds on
# top of the editor baseline.
_COMPOSER_ACTION_ROW_CHROME = 34
# Footer: min-height 25 px, padding 2 / 11 px.
_FOOTER_MIN_HEIGHT = 25
# GUI contract G2: wide auxiliary pane nominal width.  Pinned (not
# user-draggable) so opening History / Mutations can never squeeze the
# conversation into a super-narrow column (G3).
_PANE_NOMINAL_WIDTH = 295
# ``QWIDGETSIZE_MAX`` — the narrow presentation releases the width pin so
# the same widgets fill the whole content region.
_PANE_WIDTH_UNBOUND = 16777215
# Tooltip / accessible-name text per action-button attribute.  Single
# source of truth for the toolbar, the composer row and the overflow menu.
_ACTION_BUTTON_TOOLTIPS = {
    "_chat_btn": "Back to the chat workspace",
    "_send_btn": "Send the message (Enter)",
    "_cancel_btn": "Stop the running agent (Esc)",
    "_export_btn": "Export this chat to a file",
    "_settings_btn": "Open Luc Nhan settings",
    # NOTE: ``_mutations_btn`` is deliberately absent — it is the context
    # row's ``Changes`` action (G3), whose tooltip, accessible name and style
    # are owned by ``ContextBar``.  Re-adding it here would both repaint it
    # with push-button chrome inside a fixed 27 px row and double its
    # accessible name (``Changes — Changes — …``).
    "_history_btn": "Show past chat sessions",
    "_tools_btn": "Open the tools panel",
}


@dataclass
class _TabGateState:
    """One tab's blocking gates.

    ``pending_answer`` and ``button_only`` are separate because a single
    tab can hold both at once: a question the user answers with buttons
    (plan / save / option-only ask_user) is a pending answer *and* a
    button-only gate, while a free-text question only blocks the answer
    channel.  Both are per tab — the panel-wide booleans they replace
    could only ever describe ONE tab and silently cleared another tab's
    gate.
    """

    pending_answer: bool = False
    button_only: bool = False


@dataclass
class _TabDraft:
    """A tab's composer draft: text plus the caret/selection to restore."""

    text: str = ""
    anchor: int = 0
    position: int = 0


def _action_button_width(button: QPushButton) -> int:
    """Width that fits *button*'s styled label, never below the contract.

    ``setFixedWidth(64)`` clipped ``Send`` / ``Stop`` / ``Queue`` at a 24 pt
    user font.  ``sizeHint()`` is the only measure that already accounts
    for the QSS the button carries (its ``font-size``, padding and
    borders), so it tracks a font override without a second copy of the
    padding numbers.  The 64 px floor keeps the compact default layout
    byte-identical, and the splitter resize is unaffected.
    """
    try:
        return max(_ACTION_BUTTON_WIDTH, int(button.sizeHint().width()))
    except (AttributeError, RuntimeError, TypeError, ValueError):  # stubbed / torn-down button
        return _ACTION_BUTTON_WIDTH


def _palette_from_tokens(widget: QWidget, tokens: object) -> object | None:
    """Build the widget palette for a Luc Nhan surface.

    Starts from the widget's own palette and pins only the roles Luc Nhan
    owns, so everything else still inherits.  Returns ``None`` if the Qt
    seam is unavailable (headless / Qt-stub sessions) so the caller simply
    leaves the widget alone.
    """
    try:
        from .qt_compat import QColor, QPalette
    except ImportError:  # pragma: no cover — headless fallback
        return None
    try:
        pal = widget.palette()
        for role, token in (
            (QPalette.ColorRole.Window, "window"),
            (QPalette.ColorRole.WindowText, "window_text"),
            (QPalette.ColorRole.Base, "base"),
            (QPalette.ColorRole.Text, "text"),
            (QPalette.ColorRole.Button, "button"),
            (QPalette.ColorRole.ButtonText, "button_text"),
            (QPalette.ColorRole.Highlight, "highlight"),
            (QPalette.ColorRole.HighlightedText, "highlight_text"),
        ):
            pal.setColor(role, QColor(getattr(tokens, token)))
        if hasattr(QPalette.ColorRole, "PlaceholderText"):
            pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(tokens.muted_text))
        return pal
    except Exception as exc:  # pragma: no cover — defensive
        log_debug(f"palette build skipped: {exc}")
        return None


def _format_token_usage(count: int, context_window: int = 0) -> str:
    """Footer readout for context usage, e.g. ``4.2k (2%)``."""
    text = f"{count / 1000:.1f}k" if count >= 1000 else str(count)
    if context_window > 0:
        text += f" ({min(int(count * 100 / context_window), 100)}%)"
    return text


class _AddButtonTabBar(QTabBar):
    """Session strip: chat tabs plus a trailing ``+`` new-chat button.

    The bar is *hosted* in the panel's own session-strip container next to
    the ``+`` button instead of living inside the pages container.  Qt hides
    a tab bar that is not in the tab widget's own layout (and reserves
    ~30 px of chrome for it), which would either strand New behind the
    strip's scroll buttons in a narrow dock or inflate the strip height.
    The tabs themselves stay the pages container's widgets — only the tab
    strip's chrome moves — so tab switching, close buttons and the export
    / fork context menu are unchanged.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._add_tab_callback: Callable[[], None] | None = None
        self._export_tab_callback: Callable[[int], None] | None = None
        self._fork_tab_callback: Callable[[int], None] | None = None
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self._add_btn = QToolButton(self)
        self._add_btn.setText("+")
        self._add_btn.setAutoRaise(True)
        self._add_btn.setFixedSize(20, 20)
        self._add_btn.setToolTip("Start a new chat tab (Ctrl+T)")
        self._add_btn.setAccessibleName("New chat — Start a new chat tab (Ctrl+T)")
        self._add_btn.clicked.connect(self._handle_add_tab)
        # GUI contract G3: native tab scrolling keeps every session tab
        # reachable at narrow widths (the preview's "hide inactive tabs"
        # shortcut is explicitly out of contract).
        self.setUsesScrollButtons(True)
        self.setElideMode(Qt.TextElideMode.ElideRight)
        self._apply_styles()

    def _apply_styles(self) -> None:
        """Style the bar and the ``+`` button from the shared builders.

        The bar is owned by the session strip, not by the tab widget, so
        its chrome must be applied to the bar itself — the owner calls
        ``_apply_session_strip_styles`` once the reparent is done.  The
        sheet is applied in every mode: in AUTO/IDA the tokens come from
        the real host palette, and the G3 2 px active underline has to
        survive there too.
        """
        from .theme.widgets_common import get_add_tab_btn_style, get_tab_widget_style

        self.setStyleSheet(get_tab_widget_style())
        self._add_btn.setStyleSheet(maybe_host_stylesheet(get_add_tab_btn_style()))

    def set_add_tab_callback(self, callback: Callable[[], None] | None) -> None:
        self._add_tab_callback = callback

    def set_export_tab_callback(self, callback: Callable[[int], None] | None) -> None:
        self._export_tab_callback = callback

    def set_fork_tab_callback(self, callback: Callable[[int], None] | None) -> None:
        self._fork_tab_callback = callback

    def _handle_add_tab(self) -> None:
        if self._add_tab_callback is not None:
            self._add_tab_callback()

    def _show_context_menu(self, pos):
        index = self.tabAt(pos)
        if index < 0:
            return
        menu = QMenu(self)
        export_action = menu.addAction("Export Chat")
        fork_action = menu.addAction("Fork Session")
        action = menu.exec(self.mapToGlobal(pos))
        if action == export_action and self._export_tab_callback is not None:
            self._export_tab_callback(index)
        elif action == fork_action and self._fork_tab_callback is not None:
            self._fork_tab_callback(index)


class LucNhanPanelCore(QWidget):
    """Host-agnostic chat panel widget."""

    def __init__(
        self,
        controller_factory: Callable[[LucNhanConfig], Any],
        ui_hooks_factory: Callable[[Callable[[], Any]], Any] | None = None,
        tools_form_factory: Callable[..., Any] | None = None,
        parent: QWidget | None = None,
    ):
        _early_log("panel_core:init:entry")
        super().__init__(parent)
        try:
            self._config = LucNhanConfig.load_or_create()
            _early_log("panel_core:config_loaded")
        except Exception as _exc:
            _early_log_crash(_exc)
            raise
        # NOTE: the host-theme answer is read live (``use_native_host_theme()``)
        # on every apply — it changes when the user switches between
        # AUTO/IDA and Dark/Light, so it is never cached on the instance.
        # Per-connection model catalog owned by the panel; filled from
        # Settings Refresh results, dropped on connection changes only.
        self._model_catalog = ModelCatalogCache(persist_path=os.path.join(self._config.cache_dir, "model_catalog.json"))
        self._footer_width_timer: QTimer | None = None
        log_debug(
            f"Config loaded: provider={self._config.provider.name} model={self._config.provider.model}",
        )
        # ``ProviderRegistry().dependency_warnings()`` walks all provider
        # modules and is non-trivial (≈0.7ms cold). Defer it: log the
        # warnings the first time the user looks at the dependency list,
        # or the next event-loop turn — whichever comes first.
        self._dependency_warnings: list[str] = []
        QTimer.singleShot(0, self._resolve_dependency_warnings)
        if self._config.has_encrypted_keys():
            self._prompt_decryption_password()
        try:
            self._ctrl = controller_factory(self._config)
            _early_log("panel_core:controller_built")
        except Exception as _exc:
            _early_log_crash(_exc)
            raise
        _early_log("panel_core:post_controller_fields:entry")
        self._poll_timer: QTimer | None = None
        self._polling = False
        # Per-tab gate state.  The composer is global, but every gate
        # (pending question, button-only approval) belongs to ONE tab's
        # runner, so each tab owns its own entry: two tabs can wait on
        # separate questions without either clearing the other, and the
        # composer / Send / Stop / status presentation is derived from the
        # ACTIVE tab only.
        self._tab_gates: dict[str, _TabGateState] = {}
        # Per-tab composer draft (text + caret/selection).  Lives in
        # memory only — never persisted, never part of the session schema.
        self._tab_drafts: dict[str, _TabDraft] = {}
        self._is_shutdown = False
        self._ui_hooks_factory = ui_hooks_factory
        self._ui_hooks = None
        self._tools_form_factory = tools_form_factory
        self._tools_form: Any = None  # IDA PluginForm wrapper (if available)

        # Tab-to-ChatView mapping
        self._chat_views: dict[str, ChatView] = {}
        self._pending_restore_messages: dict[str, list] = {}
        self._context_bar: ContextBar | None = None
        self._mutation_panel: MutationLogPanel | None = None
        self._skills_refresh_timer: QTimer | None = None
        # History-on-demand coordinator state (spec §6.1, §8.1, §11.4).
        # ``_history_panel`` / ``_history_btn`` are created in
        # ``_build_main_splitter`` / ``_build_action_buttons``; they are
        # declared here so every history helper can null-check them
        # without an ``AttributeError`` if a method runs before the UI
        # build completed (or in tests that bypass ``__init__``).
        self._history_panel: HistoryPanel | None = None
        self._history_btn: QPushButton | None = None
        # Adaptive pane presentation (GUI contract G5).  ``_pane_name``
        # is the state ``_show_right_panel`` owns: ``None`` means closed,
        # so it survives a wide/narrow resize untouched.  The stack is
        # built next to ``_mode_stack`` and is the pane's parent in both
        # presentations, which is why a wide dock can slide to narrow
        # without re-parenting a single widget.
        self._pane_stack: QStackedWidget | None = None
        self._pane_name: Literal["history", "mutation"] | None = None
        self._pane_opener: QWidget | None = None
        # Last applied pane presentation; ``None`` forces the first
        # application.  Only the breakpoint changes it — never the pane
        # state — so an open pane survives a resize untouched.
        self._pane_narrow: bool | None = None
        self._history_generation: int = 0
        self._history_executor: ThreadPoolExecutor | None = None
        # Typed union queue: every history worker (list, load, delete)
        # enqueues exactly one terminal result and the drain dispatches
        # on the result type.  The same queue is safe to share because
        # at most one request is in flight at a time (``_history_pending``
        # single-flight invariant, spec §11.4).
        self._history_result_queue: queue.Queue[HistoryListResult | HistoryLoadResult | HistoryDeleteResult] = (
            queue.Queue(maxsize=_HISTORY_RESULT_QUEUE_MAXSIZE)
        )
        self._history_poll_timer: QTimer | None = None
        self._history_pending: bool = False
        # Reviewer MEDIUM #2: when a load FAILS, the persisted session
        # id is retained so the Retry button can re-dispatch the LOAD
        # (not the list refresh).  Only the id is held — never the
        # full ``SessionState`` — so a stale scope is recaptured fresh
        # on retry and no large payload is retained.  Cleared on
        # successful attach, IDB change, shutdown, or any non-FAILED /
        # non-LOADED status that has no retry-load semantics.
        # ``_history_last_load_session_id`` tracks the in-flight load's
        # target so a FAILED result (which does NOT carry the id back)
        # can copy it into the retry slot.  ``HistoryLoadResult`` only
        # echoes the captured scope, not the requested session id.
        self._history_retry_load_session_id: str | None = None
        self._history_last_load_session_id: str | None = None
        # Task 5 delete-coordinator state (spec §11.5):
        # ``_history_delete_intents`` is the load-bearing intent gate.
        # An id is inserted BEFORE the confirmation dialog opens and
        # removed by the terminal delete apply / cancel / invalidate.
        # Both LOAD paths (queued-result apply and new open request)
        # consult the set so a LOAD that lands while a DELETE is in
        # flight is dropped instead of attaching a session the user
        # just confirmed deleting.  Immutable-replacement updates keep
        # the set race-free for future per-row intents.
        self._history_retry_delete_session_id: str | None = None
        self._history_last_delete_session_id: str | None = None
        self._history_delete_intents: set[str] = set()
        # Watchdog timer for slow-delete notices.  Created lazily by
        # ``_start_history_delete_watchdog`` and torn down by
        # ``_stop_history_delete_watchdog`` / ``_invalidate_history``.
        # Cosmetic only — never cancels the in-flight delete.
        self._history_delete_watchdog: QTimer | None = None
        # Closing flag: set by ``_invalidate_history`` so a worker that
        # finishes after IDB-switch / shutdown drops its result instead
        # of pushing it onto a queue nobody drains (spec §11.4).
        self._history_closing: threading.Event = threading.Event()
        # Debounced token display: streaming can fire 30+ usage events per
        # second; each one used to call set_tokens directly. We coalesce to
        # a single update at most every 100ms and skip updates that would
        # display the same value already shown.
        self._pending_token_display: int | None = None
        self._token_display_timer: QTimer | None = None
        self._last_token_display_value: int = -1
        # Footer readout text; the footer is the only place usage is shown.
        self._token_text: str = "0"
        # Startup auto-restore flags (Task 1 follow-up — see
        # ``_arm_startup_restore_if_idle``).  ``_startup_restore_pending``
        # is set by the deferred QTimer slot at the end of ``_build_ui``
        # and consumed by ``_apply_history_list_result``; the chained
        # load sets ``_startup_restore_load_pending`` and is consumed by
        # ``_apply_history_loaded``.  Both are cleared by
        # ``_invalidate_history`` on IDB change / shutdown.
        self._startup_restore_pending: bool = False
        self._startup_restore_load_pending: bool = False
        _early_log("panel_core:post_controller_fields:done")

        _early_log("panel_core:oauth_consent:entry")
        try:
            self._check_oauth_consent()
        except Exception as _exc:
            _early_log_crash(_exc)
            raise
        _early_log("panel_core:oauth_consent:done")

        def _warm_oauth() -> None:
            _early_log("panel_core:oauth_warm_thread:entry")
            try:
                resolve_auth_cached()
                _early_log("panel_core:oauth_warm_thread:done")
            except Exception as e:
                _early_log(f"panel_core:oauth_warm_thread:error:{type(e).__name__}:{e}")
                log_debug(f"OAuth warm-up failed: {e}")

        _early_log("panel_core:oauth_warm_thread:create")
        _oauth_thread = threading.Thread(target=_warm_oauth, daemon=True)
        _early_log("panel_core:oauth_warm_thread:started")
        _oauth_thread.start()
        try:
            _early_log("panel_core:build_ui:about_to_call")
            self._build_ui()
            _early_log("panel_core:ui_built")
        except Exception as _exc:
            _early_log_crash(_exc)
            raise
        # Refresh themed widgets when the user switches the active theme.
        # The hookup is narrow on purpose: it only catches the
        # connect-time exceptions (RuntimeError / TypeError when the
        # signal is on a partially-initialised manager, SystemError
        # from PySide6's ``returned a result with an exception set``
        # quirk).  Any other failure is logged but does not crash
        # the panel — the theme change path can still work via the
        # initial-theme application below.
        try:
            ThemeManager.instance().themeChanged.connect(self._on_theme_changed)
        except (RuntimeError, TypeError, SystemError) as e:
            log_debug(f"ThemeManager.themeChanged hookup failed: {e}")
        # Honor the persisted theme from config so the user does not
        # have to re-pick their theme on every restart.  Map the
        # legacy config.theme string to the new ThemeMode enum.
        try:
            self._apply_initial_theme_from_config(self._config)
            _early_log("panel_core:initial_theme_applied")
        except Exception as _exc:
            _early_log_crash(_exc)
            raise
        _early_log("panel_core:init:done")

    @staticmethod
    def _apply_initial_theme_from_config(config: Any) -> None:
        """Set the active ``ThemeManager`` mode from ``config.theme``.

        Pulled out of ``__init__`` so the bootstrap can be exercised
        in isolation by unit tests.  Safe to call repeatedly: a
        no-op when the persisted mode matches the live mode.
        """
        try:
            from .theme.tokens import ThemeMode

            theme_str = getattr(config, "theme", "ida") or "ida"
            mode_map = {
                "ida": ThemeMode.IDA_NATIVE,
                "dark": ThemeMode.DARK,
                "light": ThemeMode.LIGHT,
                "auto": ThemeMode.AUTO,
            }
            initial_mode = mode_map.get(theme_str)
            if initial_mode is not None and initial_mode != ThemeManager.instance().mode:
                ThemeManager.instance().set_mode(initial_mode)
        except Exception as e:  # best-effort
            log_debug(f"ThemeManager initial mode from config failed: {e}")

    def _resolve_dependency_warnings(self) -> None:
        """Compute and log dependency warnings for the active providers.

        Called once via ``QTimer.singleShot(0, …)`` so it lands after
        the first paint. Storing the list on ``self`` also lets
        ``dependency_warnings()`` callers avoid recomputing it.
        """
        try:
            from ..providers.registry import ProviderRegistry

            self._dependency_warnings = ProviderRegistry().dependency_warnings()
            for warning in self._dependency_warnings:
                log_warning(f"Dependency warning: {warning}")
        except Exception as e:  # pragma: no cover - defensive
            log_debug(f"dependency_warnings() failed: {e}")

    def _prompt_decryption_password(self) -> None:
        """Prompt for the encryption password at session start."""
        from .qt_compat import QDialog, QDialogButtonBox, QLabel, QLineEdit, QMessageBox, QVBoxLayout

        for _attempt in range(3):
            dlg = QDialog()
            dlg.setWindowTitle("Luc Nhan — Encrypted API Keys")
            dlg.setMinimumWidth(350)
            layout = QVBoxLayout(dlg)
            layout.addWidget(QLabel("Enter password to decrypt API keys:"))
            pw_edit = QLineEdit()
            pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
            pw_edit.setPlaceholderText("Password")
            layout.addWidget(pw_edit)
            buttons = QDialogButtonBox(OK_CANCEL_BUTTONS)
            buttons.accepted.connect(dlg.accept)
            buttons.rejected.connect(dlg.reject)
            layout.addWidget(buttons)

            if dlg.exec() != QDialog.DialogCode.Accepted:
                break  # user cancelled — keys stay empty
            if self._config.decrypt_stored_keys(pw_edit.text()):
                log_debug("API keys decrypted successfully")
                return
            QMessageBox.warning(None, "Wrong Password", "Incorrect password. Please try again.")
        log_debug("API key decryption skipped or failed — keys will be empty")

    def _check_oauth_consent(self) -> None:
        """Apply persisted OAuth consent to the auth cache.

        The consent dialog itself is only shown from the settings checkbox
        (``_on_oauth_toggled``).  This method just restores the persisted
        state so the warm-up thread knows whether keychain autoload is
        allowed.
        """
        from ..providers.auth_cache import set_keychain_consent

        set_keychain_consent(self._config.oauth_consent_accepted)

    def _ensure_skills_refresh_timer(self) -> None:
        """Refresh skill autocomplete once background discovery completes."""
        if self._skills_refresh_timer is not None:
            return
        self._skills_refresh_timer = QTimer(self)
        self._skills_refresh_timer.setInterval(300)
        self._skills_refresh_timer.timeout.connect(self._refresh_skill_slugs)
        self._skills_refresh_timer.start()

    def _stop_skills_refresh_timer(self) -> None:
        if self._skills_refresh_timer is None:
            return
        self._skills_refresh_timer.stop()
        try:
            self._skills_refresh_timer.timeout.disconnect(self._refresh_skill_slugs)
        except (RuntimeError, TypeError) as e:
            log_debug(f"skills refresh timer disconnect failed: {e}")
        self._skills_refresh_timer.deleteLater()
        self._skills_refresh_timer = None

    def _refresh_skill_slugs(self) -> None:
        if self._is_shutdown:
            self._stop_skills_refresh_timer()
            return
        slugs = self._ctrl.skill_slugs
        if slugs:
            self._input_area.set_skill_slugs(slugs)
            self._stop_skills_refresh_timer()
            return
        if getattr(self._ctrl, "runtime_ready", False):
            # Runtime init completed but no skills found; stop polling.
            self._stop_skills_refresh_timer()

    def _dependency_banner_style(self) -> str:
        """Themed QSS for the yellow dependency-warnings banner."""
        from .theme.manager import blend_hex

        t = ThemeManager.instance().tokens()
        # Derive a warning pair: muted amber background, brighter amber border.
        warn_bg = blend_hex(t.base, t.error, 0.2)  # dark amber
        warn_fg = blend_hex(t.error, t.highlight_text, 0.4)
        warn_border = blend_hex(t.error, t.highlight, 0.4)
        return (
            f"QLabel#dependency_banner {{"
            f"background: {warn_bg}; color: {warn_fg}; "
            f"border-top: 1px solid {warn_border}; "
            f"border-bottom: 1px solid {warn_border}; "
            f"padding: 6px 8px; font-size: {role_size(11)}; }}"
        )

    def _tab_widget_style(self) -> str:
        """Themed QSS for the session strip (GUI contract G2).

        Delegates to the shared theme builder so the strip uses exactly one
        stylesheet pipeline; only the object names change here so the QSS
        stays scoped to this widget.
        """
        from .theme.widgets_common import get_tab_widget_style

        return get_tab_widget_style()

    def _apply_session_strip_styles(self) -> None:
        """Style the session strip, then the tab bar that lives inside it.

        Qt applies a widget's stylesheet to the widget *and its children*,
        so the strip rule styles the bar's frame — but only when it is set
        on the widget that currently *owns* the tab bar.  Setting it on
        the pages container (or before the reparent) left the bar
        unstyled.  The bar styles itself and the ``+`` button in turn, so
        neither can depend on cascade order.

        The sheet is applied in every mode (AUTO/IDA included): the tokens
        come from the real host palette there, and the 2 px active
        underline must survive the switch.
        """
        strip = getattr(self, "_session_strip", None)
        if strip is not None:
            strip.setStyleSheet(self._tab_widget_style())
        bar = getattr(self, "_tab_bar", None)
        if bar is not None:
            bar._apply_styles()

    def _apply_surface_palettes(self) -> None:
        """Paint the panel root and the content region from live tokens.

        Qt resolves a bare ``QWidget`` background through the *application*
        palette, and a host stylesheet can leave ``QApplication``'s Window
        role at the platform default while a plain ``QWidget`` inside the
        host already carries the host colors — so the transcript kept the
        host's dark ``#282a36`` while every other region followed Luc
        Nhan's Light tokens.  Setting the root's palette + auto-fill and
        letting the children inherit it is the local fix.

        In AUTO/IDA the tokens are derived from the ThemeManager's own
        independent host probe (see ``ThemeManager._host_probe_widget``) —
        never from this panel, whose own palette Luc Nhan has just
        repainted.  The host's application palette and stylesheet are
        never touched.
        """
        try:
            tokens = ThemeManager.instance().tokens()
        except Exception as e:  # pragma: no cover - defensive
            log_debug(f"surface palette skipped: {e}")
            return
        for widget in (self, getattr(self, "_content_area", None)):
            if widget is None:
                continue
            pal = _palette_from_tokens(widget, tokens)
            if pal is None:
                continue
            widget.setAutoFillBackground(True)
            widget.setPalette(pal)

    def _apply_transcript_surface(self) -> None:
        """Paint the transcript's own surface from the live tokens.

        ``ChatView`` is a ``QScrollArea`` (a ``QFrame`` subclass), so it
        resolves an unstyled background through the *application* palette
        — the host default, not the theme.  A stylesheet scoped to the two
        object names is enough on its own, but the page-level rule loses
        to a widget stylesheet that sets no background for it, so the
        scroll area and its container are also given an auto-filled
        palette.  Neither touches the host application.

        Every transcript widget is walked so a view built later (a second
        tab) is painted too, not just the draft one.
        """
        try:
            surface = get_transcript_surface_style()
        except Exception as e:  # pragma: no cover - defensive
            log_debug(f"transcript surface skipped: {e}")
            return
        self.setStyleSheet(surface)
        try:
            tokens = ThemeManager.instance().tokens()
        except Exception:  # pragma: no cover - defensive
            return
        for view in list(self._chat_views.values()):
            # The scroll area's own frame line paints from the application
            # palette (measured: #828790 at the first row of
            # ``QScrollArea#chat_scroll`` in Light mode), so the scroll area
            # itself needs the palette as well as the QSS rule.
            container = getattr(view, "_container", None)
            for widget in (view, container, getattr(view, "viewport", lambda: None)()):
                if widget is None:
                    continue
                pal = _palette_from_tokens(widget, tokens)
                if pal is None:
                    continue
                widget.setAutoFillBackground(True)
                widget.setPalette(pal)

    def _main_splitter_style(self) -> str:
        """Themed QSS for the main horizontal splitter handle."""
        t = ThemeManager.instance().tokens()
        return f"QSplitter::handle {{ background: {t.mid}; }}"

    def _chat_splitter_style(self) -> str:
        """Themed QSS for the vertical chat/input splitter handle.

        Mirrors :meth:`_main_splitter_style` so the handle is visible
        against the active palette.  The handle width is set to 4px in
        :meth:`_build_ui` so users can grab and drag it; the style only
        paints the colors.
        """
        t = ThemeManager.instance().tokens()
        return f"QSplitter#chat_splitter::handle {{ background: {t.mid}; }}"

    # directly get the themed value at access time.

    def _build_ui(self) -> None:
        _early_log("panel_core:build_ui:entry")
        _early_log("panel_core:build_ui:set_object_name:entry")
        self.setObjectName("lucnhan_panel")
        _early_log("panel_core:build_ui:set_object_name:done")

        # GUI contract G3 — workspace composition.  Nested native layouts,
        # top-to-bottom: header -> session strip -> context row ->
        # chat/content -> composer -> footer.  The composition layout has no
        # outer margins or gap: each region owns its padding (G2) so we
        # never stack an extra gutter on top of the region padding.
        _early_log("panel_core:build_ui:root_layout:entry")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        # A narrow dock (GUI contract G3: 460 px is a supported test width)
        # must not be forced wide by the children's size hints: the
        # composer hint, the footer label and the header navigation all
        # shrink below their preferred width instead.
        self.setMinimumWidth(320)
        _early_log("panel_core:build_ui:root_layout:done")

        # Header: wordmark + status left, Chat / Tools / History / Settings
        # right, secondary actions in the overflow menu.  The Chat entry is
        # the ``Chat`` mode of ``_mode_stack`` — the native ``Tools`` nav
        # button already toggles that stack, so the second, duplicate
        # ``Chat``/``Tools`` tab bar is gone (GUI contract G3).
        _early_log("panel_core:build_ui:header:entry")
        layout.addWidget(self._build_header())
        _early_log("panel_core:build_ui:header:done")

        # The ToolsPanel shell is intentionally NOT constructed here.
        # Doing so would force A2A imports / heavy widget construction at
        # panel-build time (during the Tools button being pressed at
        # startup, on the user's main thread).  It is created lazily by
        # :meth:`_ensure_tools_panel_created` on the first Tools open;
        # until then page 1 keeps a lightweight placeholder so the page
        # index stays stable.
        _early_log("panel_core:build_ui:tools_panel:placeholder")
        self._tools_panel: ToolsPanel | None = None
        self._tools_placeholder = QWidget()
        self._tools_tab_index = -1  # kept for IDA compat

        # Stacked content region: page 0 = chat, page 1 = tools.  It takes
        # all remaining height (the flex-grow region of GUI contract G3).
        _early_log("panel_core:build_ui:mode_stack:entry")
        self._mode_stack = QStackedWidget()
        self._mode_stack.setObjectName("content_mode_stack")
        # GUI contract G5: the content area is a *wrapper* holding the
        # Chat/Tools stack and the auxiliary-pane stack side by side.
        # At wide width the horizontal splitter gives the pane stack its
        # nominal width (G2); at narrow width the wrapper gives the
        # stack the full content region, so the SAME pane widgets cover
        # the conversation while header, composer and footer stay
        # untouched.  No top-level window, no modal.
        self._content_area = QWidget()
        self._content_area.setObjectName("content_area")
        content_layout = QHBoxLayout(self._content_area)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        self._content_layout = content_layout
        _early_log("panel_core:build_ui:dependency_banner:entry")
        self._dependency_banner = QLabel()
        self._dependency_banner.setObjectName("dependency_banner")
        self._dependency_banner.setWordWrap(True)
        self._dependency_banner.setStyleSheet(maybe_host_stylesheet(self._dependency_banner_style()))
        if self._dependency_warnings:
            self._dependency_banner.setText("Warnings: " + " ".join(self._dependency_warnings))
            # Below the header, above the session strip: the banner must be
            # in a layout or the warnings are silently invisible.
            layout.insertWidget(1, self._dependency_banner)
        else:
            self._dependency_banner.hide()
        _early_log("panel_core:build_ui:dependency_banner:done")

        # --- Page 0: Chat ---
        # The session strip, the context row and the footer are *chrome*
        # regions and stay outside the Chat/Tools content stack, exactly
        # like the header.  Only the conversation area flips to the Tools
        # page, so the composer below it never shifts when navigating.
        _early_log("panel_core:build_ui:tab_widget:entry")
        self._build_tab_widget()
        _early_log("panel_core:build_ui:tab_widget:done")
        _early_log("panel_core:build_ui:main_splitter:entry")
        self._build_main_splitter()
        _early_log("panel_core:build_ui:main_splitter:done")
        _early_log("panel_core:build_ui:create_initial_tab:entry")
        self._create_tab(self._ctrl.active_tab_id, "New Chat")
        _early_log("panel_core:build_ui:create_initial_tab:done")
        # Session strip region (chrome).
        layout.addWidget(self._session_strip)
        _early_log("panel_core:build_ui:session_strip:done")

        # Context row: CONTEXT -> symbol -> address -> metadata -> Changes
        # (GUI contract G3).  The ``Changes`` action opens Mutations and is
        # built here because it belongs to the context row's sequence.
        _early_log("panel_core:build_ui:context_bar:entry")
        self._context_bar = ContextBar()
        self._mutations_btn = self._context_bar.changes_button()
        self._mutations_btn.setCheckable(True)
        self._mutations_btn.clicked.connect(self._on_toggle_mutation_log)
        self._context_bar.apply_theme()
        layout.addWidget(self._context_bar)
        _early_log("panel_core:build_ui:context_bar:done")

        self._mode_stack.addWidget(self._main_splitter)
        _early_log("panel_core:build_ui:mode_stack_chat_page:done")
        self._mode_stack.addWidget(self._tools_placeholder)
        _early_log("panel_core:build_ui:tools_panel:done")
        # Both stacks share the content-area wrapper.  ``_build_main_splitter``
        # (called above) has already created ``_pane_stack``.
        content_layout.addWidget(self._mode_stack, 1)
        # Equal stretch: with the stack capped (wide) the splitter's 3:1
        # sizing drives the ratio; with the cap released (narrow) the
        # stack's own stretch gives it exactly the content region, which
        # is what turns the same widget into a content overlay (G5).
        content_layout.addWidget(self._pane_stack, 1)

        # Content region: [content stack | composer] in the *existing*
        # two-pane vertical chat splitter, so the user keeps the drag
        # handle that grows / shrinks the composer (GUI contract G3: the
        # chat/content area takes the remaining height above the composer).
        _early_log("panel_core:build_ui:input_section:entry")
        input_container = self._build_input_section()
        self._chat_splitter = QSplitter(Qt.Orientation.Vertical)
        self._chat_splitter.setObjectName("chat_splitter")
        self._chat_splitter.setHandleWidth(4)
        self._chat_splitter.setChildrenCollapsible(False)
        self._chat_splitter.setStyleSheet(maybe_host_stylesheet(self._chat_splitter_style()))
        self._chat_splitter.addWidget(self._content_area)
        self._chat_splitter.addWidget(input_container)
        self._chat_splitter.setStretchFactor(0, 1)
        self._chat_splitter.setStretchFactor(1, 0)
        layout.addWidget(self._chat_splitter, 1)
        _early_log("panel_core:build_ui:mode_stack:done")
        _early_log("panel_core:build_ui:input_section:done")

        # Footer: pinned strip below the composer, never scrolls with chat.
        _early_log("panel_core:build_ui:footer:entry")
        layout.addWidget(self._build_footer())
        _early_log("panel_core:build_ui:footer:done")

        # The typography roles are sized from the configured override, so the
        # value has to reach the manager BEFORE the first themed QSS is
        # built — a panel opened with an override already configured would
        # otherwise paint its default 13/12/11 px chrome.
        self._apply_font_override()
        # Style every themed control at build time: a panel rebuilt while
        # the persisted theme is already active never receives a
        # ``themeChanged`` signal, so build-time styling is the only path
        # that guarantees the composer controls are not left unstyled.
        self._apply_surface_palettes()
        self._apply_transcript_surface()
        self._apply_action_button_styles()
        self._composer_hint_style()

        self._narrow = False
        self._apply_narrow_presentation(self.width() <= NARROW_BREAKPOINT_PX)

        _early_log("panel_core:build_ui:ui_hooks:entry")
        if self._ui_hooks_factory is not None:
            try:
                self._ui_hooks = self._ui_hooks_factory(lambda: self)
                if self._ui_hooks is not None:
                    self._ui_hooks.hook()
            except Exception as e:
                log_debug(f"UI hook setup failed: {e}")
                self._ui_hooks = None
        _early_log("panel_core:build_ui:ui_hooks:done")
        # Startup auto-restore: on every fresh panel-open the user lands
        # on the most recent saved session for the current IDB instead
        # of a blank ``New Chat`` draft.  The probe is deferred one
        # event-loop turn via ``QTimer.singleShot(0, ...)`` so the
        # UI is fully built (draft tab exists, poll timer can run,
        # history poll QTimer is safe to spin up) before we submit a
        # background worker.  All UI work stays off IDA's startup path.
        #
        # IDB-scoping is inherited from the existing list worker
        # (``_ctrl.list_history_sessions`` filters through
        # ``_matches_current_idb``); a session belonging to a different
        # binary can never be opened by this probe.  Failure paths
        # (no sessions, manifest corrupt, save-flush timeout, load
        # failure) are silent: the existing ``startup_load`` /
        # ``_startup_restore_pending`` branches in
        # ``_apply_history_list_result`` / ``_apply_history_loaded``
        # drop the flags and leave the blank draft tab intact without
        # surfacing any error copy on the (hidden) History panel.
        #
        # User-initiated History opens during the in-flight probe are
        # detected via ``_history_panel.isVisible()`` in both the list
        # and load apply paths and yield to the user's intent instead
        # of silently swapping the draft for a session they did not
        # pick.
        QTimer.singleShot(0, self._arm_startup_restore_if_idle)
        self._install_shortcuts()
        # Pre-warm markdown-it renderer on a background thread so the
        # first user-facing ``_render()`` is a warm-cache hit, not a
        # 250-550 ms cold-start import+render on the main thread.
        import threading as _threading

        from .markdown import prewarm_markdown

        _prewarm_thread = _threading.Thread(target=prewarm_markdown, daemon=True)
        _prewarm_thread.start()
        _early_log("panel_core:build_ui:done")

    def showEvent(self, event) -> None:
        """Seed the chat-splitter default sizes on first show.

        ``setSizes`` is meaningless before the widget is laid out, so
        we wait for the first show event.  The user can still drag
        the handle afterwards; ``setSizes`` is a one-shot default, not
        a clamp.  The bottom pane starts at the composer's contract
        baseline (GUI contract G2: 44 px editor + action row) so the user
        sees the editor and Send/Stop without dragging.
        """
        super().showEvent(event)
        # ``setSizes`` needs a laid-out splitter, so the one-shot seed runs
        # after the event loop has applied the panel layout.
        QTimer.singleShot(0, self._seed_chat_splitter)

    def _seed_chat_splitter(self) -> None:
        """Pin the composer to its contract baseline, one shot only.

        GUI contract G2: the composer starts at its ~78 px baseline so the
        user sees the editor plus Send/Stop without dragging.  Any extra
        height goes to the transcript above it; dragging the handle taller
        is left to the user (the seed never runs twice).
        """
        splitter = getattr(self, "_chat_splitter", None)
        if splitter is None or getattr(self, "_chat_splitter_seeded", False):
            return
        total = splitter.height() or 0
        if total <= 0:
            return
        self._chat_splitter_seeded = True
        composer_size = _COMPOSER_TEXT_HEIGHT + _COMPOSER_ACTION_ROW_CHROME
        # Never seed below what the editor now demands (a large user font
        # needs more than the 44 px baseline for one full line).
        floor = max(_COMPOSER_MIN_HEIGHT, _composer_min_height(self._input_area))
        composer_size = min(composer_size, max(floor, total - 80))
        transcript_size = max(40, total - composer_size)
        splitter.setSizes([transcript_size, composer_size])

    def _install_shortcuts(self) -> None:
        """Wire window-scoped panel shortcuts (Ctrl+T new tab, Ctrl+W close).

        ``WindowShortcut`` confines the binding to this panel's window so it
        never collides with the host's global shortcut namespace (IDA owns
        nearly every plain key; the panel must not fight it). Kept off the
        QAction path so it works in both the Qt and headless-host test
        harnesses without an IDA action registry.
        """
        new_tab_sc = QShortcut(QKeySequence("Ctrl+T"), self)
        new_tab_sc.setContext(Qt.ShortcutContext.WindowShortcut)
        new_tab_sc.activated.connect(self._on_new_tab)

        close_tab_sc = QShortcut(QKeySequence("Ctrl+W"), self)
        close_tab_sc.setContext(Qt.ShortcutContext.WindowShortcut)
        close_tab_sc.activated.connect(self._close_current_tab)

        # GUI contract G5: Escape is the narrow-dock equivalent of the
        # pane's Close button.  Parented to the *pane stack*, not to the
        # panel root: a ``WidgetWithChildrenShortcut`` whose parent is the
        # panel would still contain the composer in its scope, so Qt would
        # consume Escape at the shortcut layer and the editor's
        # documented Esc-to-cancel-agent path would never run.  Scoped to
        # the stack, the pane claims the key only while the pane (or one
        # of its children) actually holds focus.
        close_pane_sc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self._pane_stack)
        close_pane_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        close_pane_sc.activated.connect(self._on_close_pane_shortcut)

    def _close_current_tab(self) -> None:
        """Close the active chat tab (Ctrl+W handler)."""
        self._on_close_tab(self._tab_widget.currentIndex())

    def _on_close_pane_shortcut(self) -> None:
        """Escape: close the auxiliary pane when one is open.

        Precedence (GUI contract G5 must not cost the composer its key):

        1. No pane open  → inert.  Escape keeps whatever meaning it has
           elsewhere (approval cancel, dialogs).
        2. Focus in the composer → let the editor own the key.  Its
           documented Esc-to-cancel-agent behavior always wins; closing a
           pane is not something the user is asking for while typing.
        3. Otherwise → close the pane and hand focus to the opener.

        Case 2 is enforced structurally: the shortcut's scope is the pane
        stack (see :meth:`_install_shortcuts`), which does not contain
        the composer, so Qt never routes the key here while the editor
        has focus.  This explicit check is a second line of defence for
        hosts that route focus differently, and keeps the rule assertable
        in the stub harness where focus is simulated rather than
        dispatched by Qt.
        """
        if getattr(self, "_pane_name", None) is None:
            return
        if self._focus_in_composer():
            return
        self._close_right_panel()

    def _focus_in_composer(self) -> bool:
        """True when the composer editor holds keyboard focus."""
        input_area = getattr(self, "_input_area", None)
        if input_area is None:
            return False
        focus = getattr(input_area, "hasFocus", None)
        return bool(focus()) if callable(focus) else False

    def _build_tab_widget(self) -> None:
        """Create the session strip and the conversation pages it drives.

        GUI contract G2/G3: the strip is its own region (min-height 31 px,
        inset 9 px, gap 7 px) holding the tab bar and the ``+`` button; the
        conversation region below it only holds the pages.

        The pages are a ``SessionPages`` stack, not a ``QTabWidget``: a tab
        widget reserves a tab-bar row for the bar it owns, and that row
        rendered as a host-colored band between the strip and the first
        message (measured natively: 28 px, occupant = the QTabWidget
        itself).  The stack never lays the bar out, so there is no row to
        reserve and nothing paints over the transcript.
        """
        self._tab_widget = SessionPages()
        self._tab_widget.currentChanged.connect(self._on_tab_changed)
        # No pane chrome of its own (the session strip owns the chrome).
        # Structural only — no theme colors — so it holds in host mode too.
        self._tab_widget.setStyleSheet("QStackedWidget#session_pages { border: none; }")

        # Session strip: tab bar + trailing ``+``, always visible.
        self._session_strip = QFrame()
        self._session_strip.setObjectName("session_strip")
        # G2 baseline is 31 px, but a *minimum*: the pin made a larger user
        # font clip the tabs instead of growing the row (G2 "grow for a
        # larger font rather than clip").  The layout's own height is the
        # floor, so 31 px is only what Qt hands the strip today.
        self._session_strip.setMinimumHeight(_SESSION_STRIP_MIN_HEIGHT)
        self._session_strip.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self._session_strip_layout = QHBoxLayout(self._session_strip)
        self._session_strip_layout.setContentsMargins(9, 0, 9, 0)
        self._session_strip_layout.setSpacing(7)

        self._tab_bar = _AddButtonTabBar()
        # Hand the strip's bar to the pages container so tab labels and
        # selection live in one place; the strip still hosts the widget.
        self._tab_widget.setTabBar(self._tab_bar)
        self._tab_bar.setTabsClosable(True)
        self._tab_bar.set_add_tab_callback(self._on_new_tab)
        self._tab_bar.set_export_tab_callback(self._on_export_tab)
        self._tab_bar.set_fork_tab_callback(self._on_fork_tab)
        self._tab_bar.setExpanding(False)
        # Move the bar into the strip last, so nothing above touches the
        # stylesheet that ``_apply_session_strip_styles`` installs below.
        self._tab_bar.setParent(self._session_strip)
        self._session_strip_layout.addWidget(self._tab_bar, 1)
        self._session_strip_layout.addWidget(self._tab_bar._add_btn)
        # Close is wired once, on the bar itself.
        self._tab_bar.tabCloseRequested.connect(self._on_close_tab)
        self._apply_session_strip_styles()

    def _build_context_bar_for_tests(self) -> ContextBar:
        """Build just the context row, for the lightweight panel harnesses.

        ``_build_ui`` inlines the construction; the tests that only exercise
        the header / composer wiring need the context bar's ``Changes``
        action without the full (config-loading, controller-creating)
        ``__init__``.  Wiring is identical to ``_build_ui``.
        """
        self._context_bar = ContextBar()
        self._mutations_btn = self._context_bar.changes_button()
        self._mutations_btn.setCheckable(True)
        self._mutations_btn.clicked.connect(self._on_toggle_mutation_log)
        self._context_bar.apply_theme()
        return self._context_bar

    def _build_main_splitter(self) -> QSplitter:
        """Create the horizontal splitter (chat | auxiliary pane) and return it.

        Spec §6.4 / GUI contract G5: History and Mutations are mutually
        exclusive.  Both live in the *same* stack — the pane stack built
        by :meth:`_build_pane_stack` — and only that stack's active page
        is shown, so exactly one pane can ever be visible.  The splitter
        therefore holds two widgets: the conversation and the stack.

        The splitter is *not* added to any layout here — the caller
        decides where the splitter lives.  In the normal panel layout
        it is page 0 of the content stack, but tests and host wrappers
        may place it elsewhere.
        """
        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.setObjectName("main_splitter")
        self._main_splitter.setHandleWidth(1)
        # The conversation pane is the flex-grow region of the workspace
        # (GUI contract G3); without an explicit Expanding policy the
        # vertical chat splitter hands the leftover height to the composer.
        self._main_splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._main_splitter.setStyleSheet(maybe_host_stylesheet(self._main_splitter_style()))
        self._main_splitter.addWidget(self._tab_widget)

        # Both auxiliary panes — created once, never recreated on a
        # wide/narrow transition (G5).  ``_build_pane_stack`` owns their
        # signals and adds them as stack pages.
        self._mutation_panel = MutationLogPanel()
        self._mutation_panel.undo_requested.connect(self._on_undo_requested)
        self._mutation_panel.setVisible(False)

        # History panel — passive widget: only main-thread signals fire
        # here (spec §6.3).  PanelCore owns the worker / queue / timer
        # that drive its content.
        self._history_panel = HistoryPanel()
        self._history_panel.setVisible(False)
        self._history_panel.close_requested.connect(lambda: self._close_right_panel())
        self._history_panel.retry_requested.connect(self._on_history_retry)
        # ``session_open_requested`` is consumed by Task 9; the slot is
        # wired now so the passive widget has a stable connection owner
        # and tests can assert the signal reaches PanelCore.
        self._history_panel.session_open_requested.connect(self._on_history_open_requested)
        # Task 5 (spec §11.5): row delete + notice dismiss signals.
        # PanelCore is the only legitimate owner of panel state mutation;
        # the widget signals let the user express intent without ever
        # touching persistence / worker / queue state directly.
        self._history_panel.session_delete_requested.connect(self._on_history_delete_requested)
        self._history_panel.notice_dismissed.connect(self._on_history_notice_dismissed)
        # Mutations gets the same Close / Escape affordance as History.
        self._mutation_panel.close_requested.connect(self._close_right_panel)

        # G5: one stack, one visible page — the panes can never be
        # active at the same time, in either presentation.
        self._build_pane_stack()
        # NOTE: the pane stack is deliberately NOT added here. ``_build_ui``
        # puts it in the content-area HBox beside the conversation, where
        # ``_apply_pane_presentation`` pins it to the G2 nominal width
        # (min == max at wide, released at narrow).  Adding it to the
        # splitter too would give one widget two parents' worth of sizing
        # rules, and ``setSizes`` on a stack the splitter does not own does
        # nothing.
        self._main_splitter.setStretchFactor(0, 1)

        return self._main_splitter

    def _build_pane_stack(self) -> None:
        """Stack the two auxiliary panes over one another (G5).

        The stack is a *sibling* of the content stack inside the same
        content-area wrapper (see :meth:`_build_ui`), so at wide width the
        wrapper's width pin gives it the G2 nominal pane width and at
        narrow width that pin is released and the stack covers the
        conversation.  Neither transition moves a widget between parents, so
        the running agent, transcript and draft are never disturbed.
        """
        stack = QStackedWidget()
        stack.setObjectName("aux_pane_stack")
        # Expanding: at a narrow width the released width pin hands the
        # stack the *whole* content region (the overlay), which only
        # happens for a widget that wants to grow.  At the wide width
        # the cap pins it at the G2 nominal width anyway.
        stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        stack.addWidget(self._mutation_panel)
        stack.addWidget(self._history_panel)
        # Closed presentation: the wide splitter hides the whole stack,
        # so the exact current page never matters while it is closed.
        stack.setVisible(False)
        self._pane_stack = stack

    def _pane_widget(self, name: Literal["history", "mutation"] | None) -> QWidget | None:
        """Return the widget backing *name* (``None`` → nothing)."""
        if name == "history":
            return self._history_panel
        if name == "mutation":
            return self._mutation_panel
        return None

    def _build_input_section(self) -> QWidget:
        """Build the composer region: editor + Send/Stop + overflow.

        GUI contract G3: the editor spans the full workspace width; Send /
        Stop sit in a compact action row *below* the editor instead of
        stealing horizontal space from it, and the secondary actions live
        in the header's overflow menu — not beside the input.
        """
        self._input_container = QWidget()
        self._input_container.setObjectName("composer")
        input_layout = QVBoxLayout(self._input_container)
        # G2: composer wrapper padding 7 top / 10 horizontal / 5 bottom.
        # The wrapper owns the divider and frame in every theme; the
        # editor inside it stays borderless.
        input_layout.setContentsMargins(10, 7, 10, 5)
        input_layout.setSpacing(0)

        self._input_area = InputArea(self._input_container)
        self._input_area.set_submit_callback(self._on_submit)
        self._input_area.set_cancel_callback(self._on_cancel)
        self._input_area.set_skill_slugs(self._ctrl.skill_slugs)
        self._ensure_skills_refresh_timer()
        # GUI contract G2: composer text baseline 44 px.  The *minimum* is
        # metric-derived by the editor itself (one full text line plus
        # padding), so a user font override grows the composer instead of
        # clipping the first line, while the default 13 px role keeps the
        # 38 px contract floor.  The editor stays *stretchable* — the
        # drag-to-grow composer behaviour is unchanged, so no maximum
        # height is pinned here (the vertical chat splitter owns the pane
        # the editor lives in).
        self._input_area.setMinimumHeight(_composer_min_height(self._input_area))
        self._input_area.setMaximumHeight(16777215)
        input_layout.addWidget(self._input_area, 1)

        # Keyboard hint centre, Send/Stop right (slash commands are offered
        # by the editor's own autocomplete, so there is no Commands button).
        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 2, 0, 0)
        action_row.setSpacing(9)

        # Secondary actions that still need a visible control at narrow
        # widths (GUI contract G3 keeps Mutations out of the overflow).
        action_row.addLayout(self._build_action_buttons())

        self._hint_label = QLabel("Enter to send · Shift+Enter for newline")
        self._hint_label.setObjectName("composer_hint")
        self._hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Secondary hint: it must never widen the dock, it just disappears
        # at narrow widths (GUI contract G3).
        self._hint_label.setMinimumWidth(0)
        self._hint_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        action_row.addWidget(self._hint_label, 1)

        # Send / Stop — the only buttons that stay beside the composer.
        self._send_btn = QPushButton("Send")
        self._send_btn.setObjectName("send_button")
        self._send_btn.setToolTip("Send the message (Enter)")
        self._send_btn.setAccessibleName("Send — Send the message (Enter)")
        # Metric-derived floor; ``_apply_composer_metric_minima`` re-fits it
        # whenever a font override changes the styled label.
        self._send_btn.setFixedWidth(_action_button_width(self._send_btn))
        self._send_btn.clicked.connect(self._on_send_clicked)
        action_row.addWidget(self._send_btn)
        self._cancel_btn = QPushButton("Stop")
        self._cancel_btn.setObjectName("cancel_button")
        self._cancel_btn.setToolTip("Stop the running agent (Esc)")
        self._cancel_btn.setAccessibleName("Stop — Stop the running agent (Esc)")
        self._cancel_btn.setFixedWidth(_action_button_width(self._cancel_btn))
        self._cancel_btn.setVisible(False)
        self._cancel_btn.clicked.connect(self._on_cancel)
        action_row.addWidget(self._cancel_btn)

        input_layout.addLayout(action_row)
        return self._input_container

    def _build_action_buttons(self) -> QHBoxLayout:
        """Build the *secondary* action row (overflow entries + Mutations).

        The returned strip is empty: every secondary action lives in the
        header (wide) or its overflow menu (narrow), and Mutations moved to
        the context row's ``Changes`` action (GUI contract G3).

        Only *existing* button instances are used: Send / Stop are built in
        :meth:`_build_input_section` and every other action reuses the
        instance created here, so no action ever has two stateful copies.
        """
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(4)

        # G3's composer action row is: commands left, keyboard hint centre,
        # send/stop right.  Mutations lives in the CONTEXT row as its
        # ``Changes`` action (built with the context bar) — not here.
        btn_layout.addStretch()
        return btn_layout

    # ------------------------------------------------------------------
    # Header: wordmark + status left, Chat / Tools / History / Settings
    # right, secondary actions in an overflow menu (GUI contract G3).
    # ------------------------------------------------------------------

    def _build_header(self) -> QWidget:
        """Main header strip (min-height 38 px, padding 3 / 10)."""
        header = QFrame()
        header.setObjectName("panel_header")
        # G2 baseline is 38 px, kept as a *minimum*: with a user font
        # override the text grows and a fixed height would clip it.
        header.setMinimumHeight(_HEADER_MIN_HEIGHT)
        header.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self._header = header
        self._header_layout = QHBoxLayout(header)
        self._header_layout.setContentsMargins(10, 3, 10, 3)
        self._header_layout.setSpacing(_HEADER_GAP)

        from .theme.widgets_common import (
            get_header_status_style,
            get_header_title_style,
        )

        self._title_label = QLabel("Luc Nhan")
        self._title_label.setObjectName("panel_title")
        self._title_label.setStyleSheet(get_header_title_style())
        self._header_layout.addWidget(self._title_label)

        self._status_label = QLabel("Ready")
        self._status_label.setObjectName("panel_status")
        self._status_label.setStyleSheet(get_header_status_style())
        self._status_label.setToolTip("Agent status")
        # The status may be *shortened* at narrow widths, never clipped
        # without its tooltip (GUI contract G3).  ``Ignored`` made the
        # label claim zero width, so ``Ready`` measured 0 px wide even at
        # 1100 px and disappeared entirely.  The stretch below it is what
        # absorbs the leftover width, and the minimum width stays 0 so a
        # long status may still shorten at 460 px (with its tooltip).
        self._status_label.setMinimumWidth(0)
        self._status_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._header_layout.addWidget(self._status_label)
        self._header_layout.addStretch()

        # Navigation buttons, in the contract's fixed order.  They are
        # the *same* instances used at the narrow breakpoint; only their
        # placement changes (hidden at wide, docked in the overflow menu
        # at narrow).
        self._chat_btn = self._make_nav_button("Chat", self._on_toggle_chat)
        self._chat_btn.setChecked(True)
        self._tools_btn = self._make_nav_button("Tools", self._on_toggle_tools)
        self._history_btn = self._make_nav_button("History", self._on_toggle_history)
        self._settings_btn = self._make_nav_button("Settings", self._on_settings)
        for btn in (self._chat_btn, self._tools_btn, self._history_btn, self._settings_btn):
            self._header_layout.addWidget(btn)

        self._overflow_btn = QToolButton()
        self._overflow_btn.setText("⋯")
        self._overflow_btn.setAutoRaise(True)
        self._overflow_btn.setToolTip("More actions")
        self._overflow_btn.setAccessibleName("More actions")
        self._overflow_btn.clicked.connect(self._show_overflow_menu)
        self._header_layout.addWidget(self._overflow_btn)

        # Export has no dedicated control any more (GUI contract G3 keeps
        # it in the overflow, never beside the composer).  A button that
        # only carries the accessible name / tooltip keeps that contract
        # and every existing ``_export_btn`` caller working.
        self._export_btn = QPushButton("Export")
        self._export_btn.setObjectName("export_button")
        self._export_btn.setFixedWidth(_action_button_width(self._export_btn))
        self._export_btn.setVisible(False)
        self._export_btn.clicked.connect(self._on_export_current)

        self._apply_action_button_tooltips()
        self._apply_header_styles()
        return header

    def _make_nav_button(self, label: str, handler: Callable[[], None]) -> QPushButton:
        """Create one header navigation button bound to *handler*."""
        btn = QPushButton(label)
        btn.setObjectName(f"nav_{label.lower()}")
        btn.setCheckable(True)
        btn.clicked.connect(handler)
        return btn

    def _build_footer(self) -> QWidget:
        """Pinned footer strip (min-height 25 px, padding 2 / 11).

        Left: clickable model/thinking trigger (GUI contract G6 —
        ``Connection / Model`` + divider + thinking level + chevron,
        max 68% of the footer width).  Right: context token usage.
        Both read straight from the existing provider config — no new
        state and no second copy of the model selection.
        """
        from .theme.widgets_common import (
            get_footer_picker_trigger_style,
            get_footer_style,
        )

        footer = QFrame()
        footer.setObjectName("panel_footer")
        footer.setMinimumHeight(_FOOTER_MIN_HEIGHT)
        footer.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self._footer = footer
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(11, 2, 11, 2)
        footer_layout.setSpacing(8)

        self._picker = ModelPicker(footer, on_select=self._on_model_picker_select)
        self._picker.set_open_settings_callback(self._on_settings)
        self._picker.set_catalog(self._model_catalog.catalog_for(self._known_connection_names()))
        self._picker.set_selection(self._current_model_choice())
        self._picker.set_busy(self._ctrl.is_agent_running)
        self._footer_trigger = QToolButton()
        self._footer_trigger.setObjectName("footer_picker_trigger")
        self._footer_trigger.setCursor(Qt.CursorShape.PointingHandCursor)
        self._footer_trigger.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # The text already ends in a ``⌄`` chevron; a native DownArrow icon
        # on top of it painted a second, oversized triangle before the name.
        self._footer_trigger.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._footer_trigger.setText(self._footer_trigger_text())
        self._footer_trigger.setToolTip(self._footer_tooltip())
        self._footer_trigger.setAccessibleName("Model and thinking — choose model, connection or thinking level")
        self._footer_trigger.setMinimumWidth(0)
        # G6: the trigger may never eat the token summary's space.
        self._footer_trigger.setMaximumWidth(int(MAX_TRIGGER_WIDTH_RATIO * 640))
        # Preferred (not Ignored): the button hugs its text at the left edge
        # and the stretch below takes the spare width; an Ignored trigger
        # swallowed the whole row and centred the model name in it.
        self._footer_trigger.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._footer_trigger.clicked.connect(self._open_model_picker)
        footer_layout.addWidget(self._footer_trigger)
        footer_layout.addStretch(1)

        self._footer_tokens = QLabel("—")
        self._footer_tokens.setObjectName("footer_tokens")
        self._footer_tokens.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        footer_layout.addWidget(self._footer_tokens)
        footer.setStyleSheet(get_footer_style())
        self._footer_trigger.setStyleSheet(get_footer_picker_trigger_style())
        self._refresh_footer_trigger_width()
        return footer

    def _current_thinking_level(self) -> str:
        """Current thinking level read from live config (``none`` = off)."""
        provider = self._config.provider
        extra = provider.extra if isinstance(provider.extra, dict) else {}
        thinking = extra.get("thinking")
        if isinstance(thinking, dict):
            level = str(thinking.get("reasoning_effort") or "").strip()
            if level:
                return level
        return "none"

    def _current_model_choice(self) -> ModelChoice:
        provider = self._config.provider
        return ModelChoice(provider.name, provider.model, self._current_thinking_level())

    def _known_connection_names(self) -> list[str]:
        """Active connection first, then every saved one (group order)."""
        names = [self._config.provider.name]
        names.extend(name for name in self._config.providers if name not in names)
        return names

    def _footer_trigger_text(self) -> str:
        provider = self._config.provider
        level = self._current_thinking_level()
        shown = "off" if level == "none" else level
        return f"{provider.name} / {provider.model}  │  {shown}  ⌄"

    def _footer_tooltip(self) -> str:
        provider = self._config.provider
        level = self._current_thinking_level()
        lines = [f"Connection: {provider.name}", f"Model: {provider.model}", f"Thinking: {level}"]
        if self._ctrl.is_agent_running:
            lines.append("Agent running — stop it to change the model")
        return "\n".join(lines)

    def _refresh_footer_trigger_width(self) -> None:
        """Keep the trigger at <= 68% of the footer (G6).

        The footer is pinned full-width under the composer, so its
        ``width()`` IS its share of the dock — which is exactly why the
        ceiling has to be recomputed on every resize instead of once at
        build time (back then the footer was still parentless and 640 px).
        """
        footer = getattr(self, "_footer", None)
        trigger = getattr(self, "_footer_trigger", None)
        if footer is None or trigger is None:
            return
        trigger.setMaximumWidth(int(footer.width() * MAX_TRIGGER_WIDTH_RATIO) or 1)

    def _open_model_picker(self) -> None:
        """Open the picker above its trigger; no fetch, no workspace rebuild."""
        picker = getattr(self, "_picker", None)
        trigger = getattr(self, "_footer_trigger", None)
        if picker is None or trigger is None:
            return
        picker.set_busy(self._ctrl.is_agent_running)
        picker.set_catalog(self._model_catalog.catalog_for(self._known_connection_names()))
        picker.set_selection(self._current_model_choice())
        picker.open_above(trigger)

    def _on_model_picker_select(self, choice: ModelChoice) -> None:
        """Single commit path: controller first, presentation only on success."""
        try:
            self._ctrl.select_model(choice.connection_name, choice.model_id, choice.thinking_level)
        except (ValueError, RuntimeError) as exc:
            # Message carries no credential — controller errors are about
            # names / levels / state, never secrets.
            self._show_model_selection_error(str(exc))
            return
        except Exception as exc:  # persistence failure keeps the old selection
            log_error(f"Model selection failed: {exc}")
            self._show_model_selection_error("Could not save the model selection. Previous selection kept.")
            return
        self._model_catalog.note_selection_committed(choice.connection_name, choice.model_id, choice.thinking_level)
        self._refresh_after_model_commit()

    def _show_model_selection_error(self, message: str) -> None:
        status = getattr(self, "_status_label", None)
        if status is not None:
            status.setText("Model unchanged")
            status.setToolTip(message)
        # The commit was refused, so the popup keeps showing a selection
        # the controller never accepted.  Snap the checkmark and the
        # thinking combo back to what the config really holds.
        picker = getattr(self, "_picker", None)
        if picker is not None:
            picker.set_selection(self._current_model_choice())

    def _refresh_after_model_commit(self) -> None:
        """Update the footer without showing() the workspace."""
        self._update_footer()

    def _update_footer(self) -> None:
        """Refresh the footer from live config + token state.

        Presentation only — never rebuilds the transcript or a session.
        """
        trigger = getattr(self, "_footer_trigger", None)
        if trigger is not None:
            trigger.setText(self._footer_trigger_text())
            trigger.setToolTip(self._footer_tooltip())
        footer_tokens = getattr(self, "_footer_tokens", None)
        if footer_tokens is not None:
            footer_tokens.setText(self._token_text)
            footer_tokens.setToolTip("Context tokens used")

    # ------------------------------------------------------------------
    # Responsive presentation (GUI contract G3)
    # ------------------------------------------------------------------

    def resizeEvent(self, event) -> None:
        """Switch chrome presentation at the 640 px breakpoint.

        Resize only changes presentation and visibility: no transcript, no
        session, no draft, no caret and no runner is touched (GUI contract
        G3).  The tooltips, accessible names and menu entries stay intact
        so every relocated action keeps its keyboard/AT access.
        """
        super().resizeEvent(event)
        self._apply_narrow_presentation(self.width() <= NARROW_BREAKPOINT_PX)
        self._schedule_footer_trigger_width()

    def _schedule_footer_trigger_width(self) -> None:
        """Re-clamp the picker trigger to 68% of the footer (G6).

        The first resize event arrives while Qt is still handing the
        footer its pre-layout width, so the clamp is deferred by one
        event-loop turn; the value then comes from the footer's real
        width.  Deferred, not repeated: a single-shot 0 ms timer, so no
        recurring work is added while the panel sits idle.  A host that
        re-lays out without a panel resize (dock reflow) still keeps the
        clamp correct because ``footer.width()`` is only read at that
        moment.
        """
        timer = self._footer_width_timer
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._refresh_footer_trigger_width)
            self._footer_width_timer = timer
        if not timer.isActive():
            timer.start(0)

    def _apply_narrow_presentation(self, narrow: bool) -> None:
        """Apply the narrow / wide chrome presentation.

        Wide  : ``Chat``  ``Tools``  ``History``  ``Settings`` + overflow
        Narrow: ``Tools`` direct, ``History`` / ``Settings`` / ``Export``
        move into the overflow menu (labels + tooltips preserved), header
        gap drops to 5 px.

        The auxiliary pane presentation (GUI contract G5) is applied here
        too — a resize is exactly when it has to change — but it depends
        only on the breakpoint, never on the current pane state, so an
        open pane survives the transition either way.
        """
        self._apply_pane_presentation(narrow)
        if getattr(self, "_narrow", None) == narrow:
            return
        self._narrow = narrow
        header_layout = getattr(self, "_header_layout", None)
        if header_layout is not None:
            header_layout.setSpacing(_HEADER_GAP_NARROW if narrow else _HEADER_GAP)
        for btn, wide_visible in (
            (self._history_btn, True),
            (self._settings_btn, True),
        ):
            btn.setVisible(wide_visible and not narrow)
        overflow = getattr(self, "_overflow_btn", None)
        if overflow is not None:
            overflow.setVisible(True)

    # ------------------------------------------------------------------
    # Adaptive auxiliary panes (GUI contract G5)
    # ------------------------------------------------------------------

    def _apply_pane_presentation(self, narrow: bool) -> None:
        """Present the pane stack as a docked column (wide) or an overlay.

        Wide  : the stack lives in the main splitter at the G2 nominal
        width (295 px), with the 3:1 conversation-to-pane stretch ratio.
        Narrow: the same stack — same widgets, same parent — fills the
        content region and covers the conversation while leaving header,
        composer and footer reachable (G5).  Nothing else changes: the
        stack's current page, the running agent, the transcript and the
        composer draft are untouched, so an open pane survives the
        breakpoint transition.

        Depends on the breakpoint alone, never on the pane state, so it
        is safe to call on every resize (no timer, no state re-entry).
        """
        stack = getattr(self, "_pane_stack", None)
        layout = getattr(self, "_content_layout", None)
        mode = getattr(self, "_mode_stack", None)
        if getattr(self, "_pane_narrow", None) != narrow:
            self._pane_narrow = narrow
        if layout is not None and stack is not None:
            # 3:1 mirrors the splitter's wide ratio.  At the narrow
            # breakpoint the pane takes the whole stretch and the
            # conversation falls back to its minimum hint — it is behind
            # the overlay either way, so that spare column is exactly
            # what buys the pane the full content region (G5).
            if mode is not None:
                layout.setStretchFactor(mode, 1)
            layout.setStretchFactor(stack, 1)
        if stack is not None:
            # The width is *pinned*, not merely capped: a cap alone leaves
            # Qt free to keep the split as-is, so wide gets the G2 nominal
            # column and narrow gets the content region minus the
            # conversation's own minimum column.
            stack.setMinimumWidth(_PANE_NOMINAL_WIDTH)
            if narrow:
                # ``self.width()`` is the NEW panel width in
                # ``resizeEvent`` — the layout has not been applied yet,
                # so the measured region would still be stale.  Recomputed
                # on EVERY resize while narrow, not just when the
                # breakpoint is crossed, so shrinking the dock inside the
                # narrow band cannot leave the pane overflowing the region
                # (which would clip its Close button).
                region = self.width()
                chat_min = mode.minimumSizeHint().width() if mode is not None else 0
                stack.setMinimumWidth(max(region - chat_min, _PANE_NOMINAL_WIDTH))
                stack.setMaximumWidth(_PANE_WIDTH_UNBOUND)
            else:
                stack.setMaximumWidth(_PANE_NOMINAL_WIDTH)
            # Geometry hints changed, so the cached split is stale.
            if layout is not None:
                layout.invalidate()
        # The wide width pin above is the whole sizing rule: the stack is
        # not in the splitter, so there is nothing to split.
        history = getattr(self, "_history_panel", None)
        if history is not None and hasattr(history, "set_overlay_presentation"):
            history.set_overlay_presentation(narrow)

    def _close_right_panel(self) -> None:
        """Close the open pane and hand focus back to a *visible* target.

        GUI contract G5: Close returns focus to the action that opened the
        pane, and never silently to nowhere.  At a narrow width the
        opener is not on screen — the History / Mutations entries live in
        the header overflow menu — so focusing it would be a no-op the
        user cannot see.  The fallback chain is:

        1. the opener, when it is still visible;
        2. the overflow button, which is where that opener now lives;
        3. the composer editor, which is always visible and always safe
           (the draft and caret are untouched either way).
        """
        self._show_right_panel(None)
        opener = self._pane_opener
        self._pane_opener = None
        target = None
        if opener is not None and getattr(opener, "isVisible", bool)():
            target = opener
        else:
            # The action moved into the overflow menu at this width, so
            # put focus where the user can actually reach it.
            target = getattr(self, "_overflow_btn", None)
        if target is None or not getattr(target, "isVisible", bool)():
            target = getattr(self, "_input_area", None)
        if target is not None:
            # ``setFocus()`` defaults to ``OtherFocusReason`` — no reason
            # argument needed, and the composer draft/caret are untouched.
            target.setFocus()

    def _apply_header_styles(self) -> None:
        """Push token-driven chrome + navigation styles onto the header.

        Every navigation button — the active page and the neutral ones —
        goes through the *same* shared builder, so the active rule can
        never be a second, unchecked implementation (GUI contract G1:
        active nav = ``selection`` fill + ``accent``).
        """
        from .theme.widgets_common import (
            get_add_tab_btn_style,
            get_header_frame_style,
            get_header_status_style,
            get_header_title_style,
        )

        self._header.setStyleSheet(maybe_host_stylesheet(get_header_frame_style()))
        self._title_label.setStyleSheet(maybe_host_stylesheet(get_header_title_style()))
        self._status_label.setStyleSheet(maybe_host_stylesheet(get_header_status_style()))
        self._overflow_btn.setStyleSheet(maybe_host_stylesheet(get_add_tab_btn_style()))

        nav_style = maybe_host_stylesheet(get_small_btn_style())
        for btn in (
            self._chat_btn,
            self._tools_btn,
            self._history_btn,
            self._settings_btn,
        ):
            btn.setStyleSheet(nav_style)
        # ``_mutations_btn`` lives in the context row now and is styled by
        # ``ContextBar.apply_theme``; do not repaint it as header chrome.
        self._composer_hint_style()

    def _composer_hint_style(self) -> None:
        """Apply the muted 11 px composer-hint style (G2 secondary hints)."""
        from .theme.widgets_common import get_hint_status_style

        hint = getattr(self, "_hint_label", None)
        if hint is not None:
            hint.setStyleSheet(maybe_host_stylesheet(get_hint_status_style()))

    def _apply_footer_styles(self) -> None:
        """Apply host-derived footer tokens even in Auto / IDA mode."""
        from .theme.widgets_common import (
            get_footer_picker_trigger_style,
            get_footer_style,
        )

        footer = getattr(self, "_footer", None)
        if footer is not None:
            footer.setStyleSheet(get_footer_style())
        trigger = getattr(self, "_footer_trigger", None)
        if trigger is not None:
            trigger.setStyleSheet(get_footer_picker_trigger_style())
        picker = getattr(self, "_picker", None)
        if picker is not None:
            self._model_catalog.note_theme_changed()
            picker.refresh_theme()

    def _apply_action_button_tooltips(self) -> None:
        """Attach a one-line tooltip + accessible name to each header action.

        ``Mutations`` is a Luc Nhan-specific concept (the undo tracking log),
        so a bare label gives a new user no way to discover what it opens.
        Tooltips also double as the accessible description for screen readers
        when an explicit accessible name is present.
        """
        for btn, tip in self._iter_action_buttons():
            btn.setToolTip(tip)
            btn.setAccessibleName(f"{btn.text()} — {tip}")

    def _iter_action_buttons(self):
        """Yield ``(button, tooltip)`` for every workspace action button.

        Single source of truth for tooltips / accessible names / styling so
        a relocated action can never lose its description.
        """
        for attr, tip in _ACTION_BUTTON_TOOLTIPS.items():
            btn = getattr(self, attr, None)
            if btn is not None:
                yield btn, tip

    def _show_overflow_menu(self) -> None:
        """Show the secondary actions menu (Export / History / Settings).

        The menu is built from the *same* button instances as the header,
        so an action toggled from here keeps the button's checked state in
        sync with the pane it opened.
        """
        menu = QMenu(self)
        entries = (
            ("Export", self._export_btn),
            ("History", self._history_btn),
            ("Settings", self._settings_btn),
        )
        actions = {}
        for label, btn in entries:
            if btn is None:
                continue
            action = menu.addAction(label)
            action.setCheckable(btn.isCheckable())
            action.setChecked(btn.isChecked())
            action.setToolTip(btn.toolTip())
            # ``clicked`` on the button fires the original handler, so
            # button/pane state stays in sync with the menu entry.
            action.triggered.connect(lambda _checked=False, b=btn: b.click())
            actions[action] = btn
        chosen = menu.exec(self._overflow_btn.mapToGlobal(self._overflow_btn.rect().bottomLeft()))
        if chosen is None or chosen not in actions:
            return
        # ``triggered`` already delegated to the button's own handler;
        # this keeps the check state in sync for actions that mirror a
        # pane toggle without a pressed-state click.
        btn = actions[chosen]
        if btn.isCheckable() and btn.isChecked() != chosen.isChecked():
            btn.setChecked(chosen.isChecked())

    def _apply_font_override(self) -> None:
        """Push the configured font size into the theme manager.

        The size is in *points* (see ``LucNhanConfig.font_size_override``);
        the typography builders convert it per role, so a single value
        rescales the whole G2 hierarchy without touching a global font.
        Repainting happens through the manager's ``themeChanged``.
        """
        try:
            points = getattr(self._config, "font_size_override", 0) or 0
            ThemeManager.instance().set_font_size_override(points)
        except Exception as e:  # pragma: no cover - defensive
            log_debug(f"font override sync skipped: {e}")

    def _apply_action_button_styles(self) -> None:
        """Refresh the action-bar button styles from the current theme.

        Three variants, all from the shared token builders: the neutral
        header actions, the composer's primary ``Send`` (accent fill with
        ``highlight_text``) and the ``Stop`` danger variant.
        """
        send = getattr(self, "_send_btn", None)
        cancel = getattr(self, "_cancel_btn", None)
        for btn in (b for b, _tip in self._iter_action_buttons()):
            if btn is send:
                style = get_primary_btn_style()
            elif btn is cancel:
                style = get_cancel_btn_style()
            else:
                style = get_small_btn_style()
            btn.setStyleSheet(maybe_host_stylesheet(style))
        # The composer frame repaints with the theme; the editor inside it
        # is borderless (see ``build_input_area_stylesheet``).
        container = getattr(self, "_input_container", None)
        if container is not None:
            container.setStyleSheet(get_composer_frame_style())

    def _on_theme_changed(self, _tokens) -> None:
        """Refresh themed widgets when the user switches the active theme.

        Every chrome region is refreshed on every switch. Host-inheriting
        widgets clear their bundled styles in AUTO/IDA; the footer,
        composer and transcript retain styles built from host-derived tokens,
        so platform-default colors cannot leak into these surfaces.

        ``use_native_host_theme()`` is read live (not cached) because the
        user can switch between AUTO/IDA_NATIVE (host styles win) and
        DARK/LIGHT (Luc Nhan styles win) at runtime.

        Long-lived panels that are *not* embedded inside a
        :class:`ChatView` (tools panel, mutation log, renamer, agent
        tree, knowledge panel, and any external tools form) subscribe
        to ``ThemeManager.themeChanged`` on their own, so this hook
        is a safety net — it calls their public ``_apply_styles``
        method directly to catch panels that have not been refreshed
        for whatever reason (e.g. the user opened the tools panel
        after the theme change).  Each call is wrapped in a
        ``try/except`` so a misbehaving panel cannot block the
        others.
        """
        host = use_native_host_theme()
        # Re-apply the token-driven QSS with the new values (or clear it
        # when the host theme wins).
        self._apply_action_button_styles()
        # The button QSS is what carries a font override, so the
        # metric-derived widths can only be fitted once it is in place.
        self._apply_composer_metric_minima()
        self._apply_header_styles()
        self._apply_footer_styles()
        context_bar = getattr(self, "_context_bar", None)
        if context_bar is not None:
            context_bar.apply_theme()
        if hasattr(self, "_dependency_banner") and self._dependency_banner is not None:
            self._dependency_banner.setStyleSheet(maybe_host_stylesheet(self._dependency_banner_style()))
        if getattr(self, "_session_strip", None) is not None:
            self._apply_session_strip_styles()
        if getattr(self, "_main_splitter", None) is not None:
            self._main_splitter.setStyleSheet(maybe_host_stylesheet(self._main_splitter_style()))
        if getattr(self, "_chat_splitter", None) is not None:
            self._chat_splitter.setStyleSheet(maybe_host_stylesheet(self._chat_splitter_style()))
        # Surfaces that must never inherit the host's app-wide Window/Base
        # colors: the panel root and the content region that holds the
        # transcript.  In host mode the tokens come from the manager's
        # independent host probe, so the panel still tracks the host
        # instead of being painted an arbitrary color.
        self._apply_surface_palettes()
        # The transcript is its own surface (QScrollArea resolves an
        # unstyled background through the application palette), so it is
        # repainted from the tokens here too.
        self._apply_transcript_surface()
        if not host:
            # Tools-panel shell (title / placeholders / tab widget chrome).
            tools_panel = getattr(self, "_tools_panel", None)
            if tools_panel is not None:
                try:
                    tools_panel._apply_styles()
                except Exception as e:  # best-effort refresh
                    log_debug(f"ToolsPanel._apply_styles failed: {e}")
            # Per-tab heavy widgets (built lazily — may be absent
            # until the user first selects that tab).
            for attr in ("_agent_tree", "_knowledge_panel", "_a2a_bridge_widget"):
                widget = getattr(self, attr, None)
                if widget is None:
                    continue
                apply = getattr(widget, "_apply_styles", None)
                if not callable(apply):
                    continue
                try:
                    apply()
                except Exception as e:  # best-effort refresh
                    log_debug(f"{attr}._apply_styles failed: {e}")
            # Mutation log panel (a sibling of the chat tabs in the
            # main splitter, not a child of any ChatView).
            mutation_panel = getattr(self, "_mutation_panel", None)
            if mutation_panel is not None:
                apply = getattr(mutation_panel, "_apply_styles", None)
                if callable(apply):
                    try:
                        apply()
                    except Exception as e:  # best-effort refresh
                        log_debug(f"MutationLogPanel._apply_styles failed: {e}")
        # Inline-styled widgets (ToolCallWidget, UserMessageWidget, code
        # blocks, etc.) cache their colors at construction time.  Pushing
        # a theme change must refresh them so they pick up the new tokens.
        # ``refresh_inline_styles`` is a no-op for views that don't override
        # it, so the call is safe for every existing ChatView.
        for cv in list(self._chat_views.values()):
            try:
                cv.refresh_inline_styles()
            except Exception as e:  # best-effort refresh
                log_debug(f"ChatView.refresh_inline_styles failed: {e}")

    # --- Tab management ---

    # --- Per-tab gates ---

    def _gate(self, tab_id: str | None) -> _TabGateState:
        """Return *tab_id*'s gate record, creating it on first use.

        The record is mutated in place, so every slot that opened or
        closed a gate for this tab observes the same object.
        """
        gates = getattr(self, "_tab_gates", None)
        if gates is None:  # panels built via ``__new__`` in older fixtures
            gates = {}
            self._tab_gates = gates
        gate = gates.get(tab_id)
        if gate is None:
            gate = _TabGateState()
            gates[tab_id] = gate
        return gate

    def _gate_for_active_tab(self) -> _TabGateState:
        """The active tab's gate state (never stored as a panel-wide flag)."""
        return self._gate(self._ctrl.active_tab_id)

    def _clear_gate(self, tab_id: str | None) -> None:
        """Release *tab_id*'s gates only; other tabs keep waiting.

        Called from every path that resolves a gate: a decision, a
        finished run, Stop, a tab close or a new-chat reset.  Clearing is
        always tab-scoped so cancelling tab B can never unlock tab A.
        """
        if tab_id is None:
            return
        gates = getattr(self, "_tab_gates", None)
        if gates is not None:
            gates.pop(tab_id, None)

    def _active_tab_button_gate(self) -> bool:
        """True when the ACTIVE tab is behind a button-only approval."""
        return self._gate_for_active_tab().button_only

    # --- Per-tab composer drafts ---

    def _save_active_tab_draft(self) -> None:
        """Stash the composer's current text and caret for the active tab.

        Called before anything that re-targets the composer (tab switch,
        new chat, close, IDB switch, history rebuild) so the draft the
        user typed is not silently overwritten by the next tab's text.
        Memory only — a draft never enters the session or the disk schema.
        """
        input_area = getattr(self, "_input_area", None)
        tab_id = getattr(self._ctrl, "active_tab_id", None)
        if input_area is None or tab_id is None:
            return
        drafts = getattr(self, "_tab_drafts", None)
        if drafts is None:  # panels built via ``__new__`` in older fixtures
            drafts = {}
            self._tab_drafts = drafts
        try:
            cursor = input_area.textCursor()
            draft = _TabDraft(
                text=input_area.toPlainText(),
                anchor=cursor.anchor(),
                position=cursor.position(),
            )
        except (AttributeError, RuntimeError):  # stubbed / torn-down editor
            return
        if draft.text:
            drafts[tab_id] = draft
        else:
            drafts.pop(tab_id, None)

    def _restore_active_tab_draft(self) -> None:
        """Load the active tab's saved draft (and caret) into the composer.

        Restoring is also what makes a *new* tab start empty: a tab with
        no saved draft is cleared, so B never inherits A's text.
        """
        input_area = getattr(self, "_input_area", None)
        tab_id = getattr(self._ctrl, "active_tab_id", None)
        if input_area is None or tab_id is None:
            return
        drafts = getattr(self, "_tab_drafts", None) or {}
        draft = drafts.get(tab_id)
        if draft is None:
            if input_area.toPlainText():
                input_area.clear()
            return
        input_area.setPlainText(draft.text)
        cursor = input_area.textCursor()
        cursor.setPosition(draft.anchor)
        # ``KeepAnchor`` extends from the anchor, so a saved selection
        # (anchor != position) comes back selected in the same direction.
        cursor.setPosition(draft.position, cursor.MoveMode.KeepAnchor)
        input_area.setTextCursor(cursor)

    def _drop_tab_draft(self, tab_id: str) -> None:
        """Release one tab's draft (tab close / new chat / IDB switch)."""
        drafts = getattr(self, "_tab_drafts", None)
        if drafts is not None:
            drafts.pop(tab_id, None)

    def _update_tab_bar_visibility(self) -> None:
        """Keep the session strip (and its ``+``) visible.

        GUI contract G3 puts New chat on the session strip, so the strip
        must not disappear with a single tab — otherwise a mouse-only user
        could never open a second chat.  The tab *bar* still only shows
        labels once there are 2+ tabs to label.
        """
        strip = getattr(self, "_session_strip", None)
        if strip is not None:
            strip.setVisible(True)

    def _create_tab(self, tab_id: str, label: str) -> ChatView:
        """Create a new ChatView and add it as a tab."""
        # Park the outgoing tab's draft before the pages switch away from
        # it — the composer is global, so its text is about to belong to
        # somebody else.
        self._save_active_tab_draft()
        chat_view = ChatView()
        chat_view.setProperty("tab_id", tab_id)  # O(1) lookup in _tab_id_at_index
        # ChatView exposes Qt signals (not Python callbacks).  Connect them
        # to the matching panel slots so tool approvals, user answers, and
        # orchestra approvals all flow into the agent loop.  Disconnections
        # happen automatically when ``chat_view.deleteLater()`` is called
        # on close/shutdown — Qt removes the connections along with the
        # signal owner.
        chat_view.tool_approval_submitted.connect(
            lambda tcid, d, tid=tab_id: self._on_tool_approval(tool_call_id=tcid, decision=d, tab_id=tid)
        )
        chat_view.user_answer_submitted.connect(
            lambda ans, tid=tab_id: self._on_user_answer_submitted(answer=ans, tab_id=tid)
        )
        chat_view.orchestra_approval_decided.connect(
            lambda tcid, d, tid=tab_id: self._on_orchestra_approval(tool_call_id=tcid, decision=d, tab_id=tid)
        )
        self._chat_views[tab_id] = chat_view
        index = self._tab_widget.addTab(chat_view, label)
        self._tab_widget.setCurrentIndex(index)
        self._update_tab_bar_visibility()
        # A tab opened after the last theme switch still needs the
        # transcript surface (see ``_apply_transcript_surface``).
        self._apply_transcript_surface()
        return chat_view

    def _rebuild_history_tab(self, tab_id: str, session: SessionState | None) -> None:
        """Replace the ChatView of an existing tab with a fresh one.

        Used by the History ``REUSED`` path (spec §10.3): the active tab was
        an empty New Chat draft and the loaded session replaced it in place.
        The old ChatView (an empty ``New Chat`` surface) is torn down and a
        new one is inserted at the same splitter index, then the historical
        messages are restored asynchronously.  ``tab_id`` does not change, so
        ``_pending_restore_messages`` and ``_chat_views`` stay consistent.
        """
        old_view = self._chat_views.get(tab_id)
        index = -1
        if old_view is not None:
            index = self._tab_widget.indexOf(old_view)
        # Stash the pending payload first so the freshly built ChatView
        # renders it on the deferred restore path.
        messages = list(session.messages) if session and session.messages else []
        self._pending_restore_messages[tab_id] = messages
        # The tab now shows a loaded session, so an unsent draft typed
        # into the empty New Chat surface belongs to neither view.
        self._drop_tab_draft(tab_id)
        if tab_id == self._ctrl.active_tab_id:
            self._input_area.clear()
        log_debug(f"HIST-TRACE rebuild: tab={tab_id} msgs={len(messages)}")  # TEMP-DIAG
        # Tear down the old (empty) ChatView if it exists.  ``shutdown``
        # bumps its restore generation so any late restore signal drops.
        if old_view is not None:
            try:
                old_view.shutdown()
            except (RuntimeError, TypeError) as exc:  # defensive — never block restore
                log_debug(f"history rebuild old view shutdown failed: {exc}")
            if index >= 0:
                self._tab_widget.removeTab(index)
            del self._chat_views[tab_id]
            try:
                old_view.deleteLater()
            except (RuntimeError, TypeError):
                pass
        new_view = ChatView()
        new_view.setProperty("tab_id", tab_id)
        new_view.tool_approval_submitted.connect(
            lambda tcid, d, tid=tab_id: self._on_tool_approval(tool_call_id=tcid, decision=d, tab_id=tid)
        )
        new_view.user_answer_submitted.connect(
            lambda ans, tid=tab_id: self._on_user_answer_submitted(answer=ans, tab_id=tid)
        )
        new_view.orchestra_approval_decided.connect(
            lambda tcid, d, tid=tab_id: self._on_orchestra_approval(tool_call_id=tcid, decision=d, tab_id=tid)
        )
        self._chat_views[tab_id] = new_view
        label = self._ctrl.tab_label(tab_id)
        if index >= 0:
            self._tab_widget.insertTab(index, new_view, label)
        else:
            index = self._tab_widget.addTab(new_view, label)
        self._tab_widget.setCurrentIndex(index)
        self._update_tab_bar_visibility()
        self._restore_messages_if_needed(tab_id)
        self._focus_tab(tab_id)
        return

    def _on_orchestra_approval(self, tool_call_id: str, decision: str, tab_id: str | None = None) -> None:
        """Forward orchestra delegation approval to the agent loop of *tab_id*.

        The orchestra / agent-handoff path uses the dedicated
        ``submit_approval`` channel — which routes to the agent
        loop's orchestra approval queue (``_approval_queue``) —
        NOT the regular tool-approval queue.  The agent loop's
        orchestra flow blocks on a different queue from tool
        approvals, so the two must stay on separate channels.
        ``submit_tool_approval`` would push to the wrong queue and
        the orchestra decision would never be observed.
        """
        del tool_call_id  # unused; approval queue does not track ids
        tid = tab_id if tab_id is not None else self._ctrl.active_tab_id
        runner = self._ctrl.get_runner(tid)
        if runner:
            runner.agent_loop.submit_approval(decision)
        # The gate is over for THIS tab only: another tab waiting on its
        # own question or approval keeps waiting.  Re-apply the composer
        # presentation only when the decided tab is the one on screen.
        self._clear_gate(tid)
        if tid == self._ctrl.active_tab_id:
            self._sync_composer_state()

    def _on_new_tab(self) -> None:
        """Create a new chat tab, with optional context clearing."""
        if self._is_shutdown:
            return
        session = self._ctrl.session
        has_messages = session and session.messages
        if has_messages:
            ctx_window = self._config.provider.context_window or 200000
            used = (
                session.last_prompt_tokens
                if session.last_prompt_tokens is not None
                else session.total_usage.total_tokens
            )
            pct = min(int(used * 100 / ctx_window), 100) if ctx_window > 0 else 0
            result = self._show_new_chat_dialog(pct)
            if result == "no":
                return
            if result == "clear":
                # Clear current tab instead of creating a new one.  A
                # reset releases the tab's gate and draft state: the
                # session it belonged to is gone, so neither can be
                # resurrected from stale UI state.
                tab_id = self._ctrl.active_tab_id
                self._ctrl.new_chat()
                self._clear_gate(tab_id)
                self._drop_tab_draft(tab_id)
                chat_view = self._active_chat_view()
                if chat_view:
                    chat_view.clear_chat()
                self._input_area.clear()
                self._sync_composer_state()
                self._refresh_mutation_pane()
                self._reset_token_display()
                self._update_tab_label(self._ctrl.active_tab_id)
                return
            # "yes" — fall through to create a new tab
        tab_id = self._ctrl.create_tab()
        self._create_tab(tab_id, "New Chat")
        self._ctrl.switch_tab(tab_id)
        # The new tab starts on a fresh SessionState whose usage is zero, so
        # the bar must be reset explicitly — ``switch_tab``'s no-arg refresh
        # path reads the (now zero) session, but the displayed value is
        # deduped against the previous tab's, so it would not change.
        self._reset_token_display()

    def _on_fork_tab(self, index: int) -> None:
        """Fork (duplicate) a session into a new tab."""
        source_tab_id = self._tab_id_at_index(index)
        if source_tab_id is None:
            return
        new_tab_id = self._ctrl.fork_session(source_tab_id)
        if new_tab_id is None:
            return
        label = self._ctrl.tab_label(new_tab_id)
        chat_view = self._create_tab(new_tab_id, f"{label} (fork)")
        # Restore messages into the forked chat view
        source_session = self._ctrl.get_session(new_tab_id)
        if source_session and source_session.messages:
            chat_view.restore_from_messages_async(source_session.messages)
        self._ctrl.switch_tab(new_tab_id)
        log_info(f"Forked tab {source_tab_id} → {new_tab_id}")

    def _on_close_tab(self, index: int) -> None:
        """Close a tab. Prevents closing the last tab."""
        if self._tab_widget.count() <= 1:
            return  # Don't close the last tab
        tab_id = self._tab_id_at_index(index)
        if tab_id is None:
            return
        # Drop the pending restore payload BEFORE shutting down the
        # ChatView (spec §10.3 step 6, §14.4).  ``ChatView.shutdown``
        # already bumps its restore generation so any in-flight
        # ``restore_from_messages_async`` signal is dropped, but the
        # panel-level payload must be released too so closing a tab
        # before lazy restore does not retain a full message list.
        self._pending_restore_messages.pop(tab_id, None)
        # Release the tab's own gate + draft before it leaves: its runner
        # is cancelled by ``close_tab``, so a surviving gate would lock a
        # composer that no longer has anything to answer.
        if tab_id == self._ctrl.active_tab_id:
            self._save_active_tab_draft()
        self._clear_gate(tab_id)
        self._drop_tab_draft(tab_id)
        self._ctrl.close_tab(tab_id)
        chat_view = self._chat_views.pop(tab_id, None)
        self._tab_widget.removeTab(index)
        if chat_view:
            chat_view.shutdown()
            chat_view.deleteLater()
        self._update_tab_bar_visibility()
        if tab_id != self._ctrl.active_tab_id:
            # Closing a *background* tab leaves the active composer alone,
            # except for the mutation pane whose scope followed the
            # active session.
            self._refresh_mutation_pane()

    def _on_export_tab(self, index: int) -> None:
        """Export a tab's chat to a Markdown file."""
        tab_id = self._tab_id_at_index(index)
        if tab_id is None:
            return
        session = self._ctrl.get_session(tab_id)
        if session is None or not session.messages:
            return

        # Show export options dialog if there are subagent logs
        include_subagents = False
        if session.subagent_logs:
            dlg = QDialog(self)
            dlg.setWindowTitle("Export Options")
            t = ThemeManager.instance().tokens()
            dlg.setStyleSheet(
                maybe_host_stylesheet(
                    f"QDialog {{ background: {t.base}; }}"
                    f"QLabel {{ color: {t.text}; font-size: {role_size(12)}; }}"
                    f"QCheckBox {{ color: {t.text}; font-size: {role_size(12)}; }}"
                )
            )
            layout = QVBoxLayout(dlg)
            cb = QCheckBox(f"Include subagent logs ({len(session.subagent_logs)} subagent runs)")
            cb.setChecked(True)
            layout.addWidget(cb)
            buttons = QDialogButtonBox(OK_CANCEL_BUTTONS)
            buttons.accepted.connect(dlg.accept)
            buttons.rejected.connect(dlg.reject)
            layout.addWidget(buttons)
            if not dlg.exec():
                return
            include_subagents = cb.isChecked()

        label = self._ctrl.tab_label(tab_id).replace("/", "-").replace("\\", "-")
        default_name = f"lucnhan-{label}-{time.strftime('%Y%m%d-%H%M%S')}.md"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Chat",
            default_name,
            "Markdown (*.md);;Text (*.txt);;All Files (*)",
        )
        if not path:
            return
        try:
            self._export_session_to_file(session, path, include_subagents=include_subagents)
            log_info(f"Exported chat to {path}")
        except Exception as e:
            log_error(f"Failed to export chat: {e}")

    @staticmethod
    def _export_session_to_file(
        session,
        path: str,
        include_subagents: bool = False,
    ) -> None:
        """Write session messages to a Markdown file."""
        lines = ["# Luc Nhan Chat Export\n"]
        lines.append(f"- **Model**: {session.model_name or 'unknown'}")
        lines.append(f"- **Exported**: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        if session.idb_path:
            lines.append(f"- **File**: `{os.path.basename(session.idb_path)}`")
        lines.append("")
        lines.append("---\n")

        subagent_logs = session.subagent_logs if include_subagents else {}

        for msg in session.messages:
            if msg.role == Role.USER:
                lines.append(f"## You\n\n{msg.content}\n")
            elif msg.role == Role.ASSISTANT:
                if msg.content:
                    lines.append(f"## Luc Nhan\n\n{msg.content}\n")
                for tc in msg.tool_calls:
                    lines.append(f"**Tool call**: `{tc.name}`\n")
                    lines.append(_export_format_tool_args(tc))
                    lines.append("")
            elif msg.role == Role.TOOL:
                for tr in msg.tool_results:
                    status = "Error" if tr.is_error else "Result"
                    lines.append(f"**{status}** (`{tr.name}`):\n")
                    lines.append(_export_format_tool_result(tr))
                    lines.append("")
                    # Insert subagent log after the spawn_subagent result
                    if tr.name == "spawn_subagent" and tr.tool_call_id in subagent_logs:
                        lines.append(
                            _export_format_subagent_log(
                                subagent_logs[tr.tool_call_id],
                            )
                        )

        # Append exploration subagent logs that aren't tied to a tool_call_id
        if include_subagents:
            for key, msgs in subagent_logs.items():
                if key.startswith("exploration_"):
                    lines.append("\n---\n\n### Exploration Subagent Log\n")
                    lines.append(_export_format_subagent_log(msgs))

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def _on_export_current(self) -> None:
        """Export the currently active tab's chat."""
        index = self._tab_widget.currentIndex()
        if index >= 0:
            self._on_export_tab(index)

    def _on_tab_changed(self, index: int) -> None:
        """Handle tab switch."""
        # ponytail: tab signal can fire while the UI is still being built (composer not yet created).
        if index < 0 or self._is_shutdown or not hasattr(self, "_input_area"):
            return
        tab_id = self._tab_id_at_index(index)
        if tab_id is None:
            return
        # Park the outgoing tab's draft *before* ``switch_tab`` moves the
        # controller's active id, so the composer is saved against the
        # tab that actually owned the text.
        self._save_active_tab_draft()
        self._ctrl.switch_tab(tab_id)
        self._restore_messages_if_needed(tab_id)
        self._update_token_display()
        # The composer, Send / Stop and the status line all describe the
        # ACTIVE tab: hand it its own draft and this tab's gate, so a tab
        # that is waiting on an approval shows its locked composer and a
        # tab that is not, sends normally.
        self._restore_active_tab_draft()
        self._sync_composer_state()
        # The Mutations pane is active-session scoped: switching tabs
        # swaps the records it lists.
        self._refresh_mutation_pane()
        # Rebind the Knowledge tab to the switched-to session's binary.
        self._on_knowledge_event_refresh("tab_changed")

    def _tab_id_at_index(self, index: int) -> str | None:
        """Find the tab_id for a given tab index via the stored property (O(1))."""
        widget = self._tab_widget.widget(index)
        if widget is None:
            return None
        tid = widget.property("tab_id")
        if tid and tid in self._chat_views:
            return tid
        # Fallback for tabs created before property was set
        for tid, cv in self._chat_views.items():
            if cv is widget:
                return tid
        return None

    def _active_chat_view(self) -> ChatView | None:
        """Return the ChatView for the currently active tab."""
        return self._chat_views.get(self._ctrl.active_tab_id)

    def _restore_messages_if_needed(self, tab_id: str) -> None:
        """Replay deferred restored messages for a tab the first time it is shown."""
        messages = self._pending_restore_messages.pop(tab_id, None)
        if not messages:
            return
        chat_view = self._chat_views.get(tab_id)
        if chat_view is not None:
            chat_view.restore_from_messages_async(messages)

    def _update_token_display(self, token_count: int | None = None) -> None:
        """Update the footer token readout with context window percentage.

        Direct updates are coalesced via :meth:`_schedule_token_display` so
        streaming usage events do not pound the context bar at 30+ Hz. A
        ``token_count`` of ``None`` (the no-arg refresh path used after tab
        switches and restore) flushes any pending value first.

        ``0`` is a meaningful value — it is what a fresh session must show —
        so it is passed through rather than treated as "no value".
        """
        if token_count is None:
            # Flush any pending value before recomputing from session state.
            self._flush_pending_token_display()
            session = self._ctrl.session
            # Show current context size (last prompt), not cumulative total
            token_count = (
                session.last_prompt_tokens
                if session.last_prompt_tokens is not None
                else session.total_usage.total_tokens
            )
        self._schedule_token_display(token_count)

    def _reset_token_display(self) -> None:
        """Force the token readout back to zero after a session reset.

        ``_schedule_token_display``'s dedupe remembers the last value it
        *displayed*, which after a fresh session is stale — a reset must
        clear that memory or the bar keeps showing the old session's usage
        until the next non-zero value happens to differ from it. The pending
        timer is cancelled first so an in-flight value from the discarded
        session cannot land after the reset.

        Never raises: this runs from ``new_chat`` / IDB-switch teardown paths,
        where failing to repaint the bar must not abort the reset itself.
        """

        try:
            timer = self._token_display_timer
            if timer is not None:
                timer.stop()
            self._pending_token_display = None
            self._last_token_display_value = -1
            ctx_window = self._config.provider.context_window or 0
            self._token_text = _format_token_usage(0, ctx_window)
            self._update_footer()
        except Exception as e:  # pragma: no cover - teardown must not raise
            log_debug(f"_reset_token_display failed: {e}")

    def _schedule_token_display(self, token_count: int) -> None:
        """Schedule a token-display update at most every 100ms.

        Skips redundant updates where the value did not change. The
        coalescing matters most during streaming: a single LLM turn can
        emit dozens of ``USAGE_UPDATE`` events, and each ``set_tokens``
        triggers a layout pass in :class:`ContextBar` that becomes a
        perceptible UI hitch on slower machines.
        """
        if token_count < 0 or token_count == self._last_token_display_value:
            return
        self._pending_token_display = token_count
        if self._token_display_timer is not None and self._token_display_timer.isActive():
            return
        timer = self._token_display_timer
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(100)
            timer.timeout.connect(self._flush_pending_token_display)
            self._token_display_timer = timer
        timer.start()

    def _flush_pending_token_display(self) -> None:
        """Apply the most recent pending token display value, if any."""
        timer = self._token_display_timer
        if timer is not None:
            timer.stop()
        value = self._pending_token_display
        if value is None or value == self._last_token_display_value:
            self._pending_token_display = None
            return
        self._pending_token_display = None
        self._last_token_display_value = value
        ctx_window = self._config.provider.context_window or 0
        self._token_text = _format_token_usage(value, ctx_window)
        self._update_footer()

    def _update_tab_label(self, tab_id: str) -> None:
        """Update tab label from the first user message."""
        label = self._ctrl.tab_label(tab_id)
        cv = self._chat_views.get(tab_id)
        if cv is None:
            return
        for i in range(self._tab_widget.count()):
            if self._tab_widget.widget(i) is cv:
                self._tab_widget.setTabText(i, label)
                break

    # --- Public API ---

    def prefill_input(self, text: str, auto_submit: bool = False) -> None:
        if self._is_shutdown:
            return
        self._input_area.setPlainText(text)
        if auto_submit:
            self._input_area.clear()
            self._on_submit(text)
        else:
            self._input_area.setFocus()

    def set_theme(self, mode: str, effective_theme: str | None = None) -> None:
        """Apply a theme to the panel core.

        The IDA Pro wrapper (and any future host wrapper) calls this
        method to align the panel's helper palette with the host's
        detected color scheme.  ``mode`` is the user-configured theme
        (``"light"``, ``"dark"``, ``"ida"``); ``effective_theme`` is
        the resolved helper palette for inline-styled widgets and is
        only consulted when ``mode == "ida"``.

        The method also drives the new ThemeManager singleton so the
        themed QSS gets rebuilt via the existing ``themeChanged``
        signal.  Calling it on a shut-down panel is a no-op.
        """
        if self._is_shutdown:
            return
        try:
            from .styles import set_current_theme
            from .theme.manager import ThemeManager
            from .theme.tokens import ThemeMode
        except ImportError as e:  # pragma: no cover — defensive
            log_debug(f"set_theme: theme modules unavailable: {e}")
            return

        # Update the legacy helper-palette selector (used by inline
        # stylesheet templates that read ``is_dark_theme()``).
        try:
            set_current_theme(mode, effective_theme=effective_theme)
        except Exception as e:
            log_debug(f"set_current_theme failed: {e}")

        # Drive the ThemeManager so the ``themeChanged`` signal fires
        # and the panel-wide QSS gets rebuilt.  Map legacy mode names
        # to the new ThemeMode values.
        try:
            mode_map = {
                "ida": ThemeMode.IDA_NATIVE,
                "dark": ThemeMode.DARK,
                "light": ThemeMode.LIGHT,
                "auto": ThemeMode.AUTO,
            }
            mgr_mode = mode_map.get(mode)
            if mgr_mode is not None:
                ThemeManager.instance().set_mode(mgr_mode)
        except Exception as e:
            log_debug(f"ThemeManager.set_mode failed: {e}")

    def shutdown(self) -> None:
        if self._is_shutdown:
            return
        self._is_shutdown = True
        try:
            tools_form = getattr(self, "_tools_form", None)
            tools_panel = getattr(self, "_tools_panel", None)
            self._stop_poll_timer()
            self._stop_skills_refresh_timer()
            # History on-demand teardown (spec §11.4): bump generation,
            # stop the dedicated timer, non-blocking executor shutdown,
            # drain result queue.  ``shutdown`` reuses
            # ``_invalidate_history`` — it already performs every step
            # the spec lists, and the ``_is_shutdown`` flag set at the
            # top of ``shutdown`` keeps ``_drain_history_results`` from
            # touching widgets even if a late result lands between
            # ``_invalidate_history`` and the C++ destructor.
            # ``clear_panel=False`` because widget mutation after the
            # C++ teardown starts is unsafe; the HistoryPanel's own
            # ``shutdown()`` call below releases its theme
            # subscriptions without mutating the row model.
            self._invalidate_history(clear_panel=False)
            # Release HistoryPanel's theme subscriptions (idempotent).
            history_panel = getattr(self, "_history_panel", None)
            if history_panel is not None and hasattr(history_panel, "shutdown"):
                try:
                    history_panel.shutdown()
                except Exception as e:  # defensive — never block teardown
                    log_debug(f"history panel shutdown skipped: {e}")
            # Stop the token-display debounce timer so a late flush
            # cannot touch the context bar after teardown.
            token_timer = getattr(self, "_token_display_timer", None)
            if token_timer is not None:
                try:
                    token_timer.stop()
                except Exception:
                    pass
                self._token_display_timer = None
            # Stop the knowledge-panel debounce timer so it can't fire
            # a refresh after the panel is destroyed.
            timer = getattr(self, "_knowledge_refresh_timer", None)
            if timer is not None:
                try:
                    timer.stop()
                except Exception:
                    pass
            # Stop the tools-event poll timer (started lazily by
            # ``_ensure_tools_panel_created``) so it cannot run on
            # a torn-down panel.
            poll_timer = getattr(self, "_tools_poll_timer", None)
            if poll_timer is not None:
                try:
                    poll_timer.stop()
                except Exception:
                    pass
                self._tools_poll_timer = None
            # Detach from ThemeManager.themeChanged so the singleton
            # doesn't keep a dangling reference to this panel alive
            # after teardown.  ``disconnect`` may raise if the panel
            # never connected (e.g. tests that bypass _build_ui), so
            # we swallow a broad set of disconnect-time errors:
            # ``RuntimeError`` / ``TypeError`` for already-disconnected
            # signals, and ``SystemError`` for PySide6's
            # ``returned a result with an exception set`` quirk
            # (observed on PySide6 6.7+ when the signal is on a
            # partially-initialised manager).
            try:
                import warnings

                from .theme.manager import ThemeManager

                # PySide6 emits a RuntimeWarning (NOT an exception) when
                # disconnecting a slot that was never connected. Suppress
                # just that case to keep test output clean; the call itself
                # is idempotent at the Qt level.
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore",
                        category=RuntimeWarning,
                        message=".*Failed to disconnect.*",
                    )
                    ThemeManager.instance().themeChanged.disconnect(self._on_theme_changed)
            except (RuntimeError, TypeError, SystemError, ImportError) as e:
                log_debug(f"ThemeManager disconnect skipped: {e}")
            _SharedSpinnerTimer.shutdown()
            if self._context_bar:
                self._context_bar.stop()
            for cv in self._chat_views.values():
                cv.shutdown()
            if self._ui_hooks:
                self._ui_hooks.unhook()
                self._ui_hooks = None
            # Propagate shutdown to the tools panel BEFORE hiding/deleting/
            # closing it, so its child widgets (e.g. A2ABridgeWidget) get
            # a chance to cancel in-flight background threads and break
            # queue / timer references while Python state is still
            # coherent.  Without this, runners keep running across
            # teardown and may dereference disposed widgets.
            if tools_panel is not None and hasattr(tools_panel, "shutdown"):
                try:
                    tools_panel.shutdown()
                except Exception as e:  # defensive — never block teardown
                    log_debug(f"tools panel shutdown skipped: {e}")
            if tools_form is not None:
                tools_form.hide()
                # In IDA mode, hide() orphans the tools widget via
                # OnClose -> setParent(None).  Schedule it for deletion
                # while Python is still alive to prevent crashes during
                # QApplication::~QApplication() exit cleanup.
                if tools_panel is not None:
                    tools_panel.deleteLater()
            elif tools_panel is not None:
                tools_panel.close()
            self._tools_panel = None
            self._ctrl.shutdown()
        except Exception as e:
            log_error(f"Panel teardown error: {e}")

    def on_database_changed(self, new_path: str) -> None:
        """Called when the user opens a different file."""
        if self._is_shutdown:
            return
        if new_path:
            try:
                normalized = os.path.normcase(os.path.realpath(os.path.abspath(new_path)))
            except (FileNotFoundError, OSError):
                # File may not exist yet (e.g., database closed without opening a new one)
                normalized = os.path.normcase(os.path.abspath(new_path))
        else:
            normalized = ""
        if normalized == self._ctrl._idb_path:
            return
        # Invalidate in-flight history work BEFORE resetting controller
        # identity (spec §7.2): bump generation, stop timer, drop
        # executor, drain queue, clear panel.  A worker that returns
        # for the old IDB is rejected by the generation check in
        # ``_drain_history_results``.  This must run before
        # ``reset_for_new_file`` swaps ``_idb_path`` /
        # ``_db_instance_id`` so the captured scope still reflects the
        # pre-switch IDB when the worker consults it.  ``clear_panel=True``
        # because the panel is still alive and the user should not see
        # stale rows from the previous IDB.
        self._invalidate_history(clear_panel=True)
        self._ctrl.reset_for_new_file(normalized)
        # Remove all existing tabs
        for cv in self._chat_views.values():
            cv.shutdown()
        while self._tab_widget.count():
            w = self._tab_widget.widget(0)
            self._tab_widget.removeTab(0)
            if w:
                w.deleteLater()
        self._chat_views.clear()
        self._pending_restore_messages.clear()
        # Every gate and draft belonged to the previous binary's tabs:
        # release them with the tabs so nothing survives the IDB switch.
        self._tab_gates.clear()
        self._tab_drafts.clear()
        self._input_area.clear()
        # Create default tab and try to restore saved sessions
        # Task 7 (spec §7.2): fresh-by-default after every IDB
        # switch.  No auto-restore — the user opens History
        # explicitly when they want a previous chat.
        self._create_tab(self._ctrl.active_tab_id, "New Chat")
        self._sync_composer_state()
        self._refresh_mutation_pane()
        # Fresh-by-default after an IDB switch: the new session's usage is
        # zero, so the bar must be reset rather than deduped against the
        # previous binary's value.
        self._reset_token_display()
        # Rebind the Knowledge tab to the newly opened binary's store.
        self._on_knowledge_event_refresh("database_changed")

    def _on_submit(self, text: str) -> bool:
        """Submit *text* to the active tab. Returns False when rejected.

        The return value is the composer's contract: a rejected message
        keeps the draft in the editor (the ``Send`` button and the Enter
        key both consult it before clearing).
        """
        if not text or self._is_shutdown:
            return False
        chat_view = self._active_chat_view()
        if chat_view is None:
            return False
        gate = self._gate_for_active_tab()
        # Script / plan / save approvals are button-only: free text must
        # not be able to bypass the gate (GUI contract G4), and the draft
        # the user typed survives the refusal.
        if gate.button_only:
            log_debug(f"Ignoring text input while awaiting button approval: {text!r}")
            return False
        if gate.pending_answer:
            # C2: the ACTIVE tab asked a question — the typed text is its
            # answer, and it resumes that tab's run.
            self._clear_gate(self._ctrl.active_tab_id)
            chat_view.add_user_message(text)
            self._sync_composer_state()
            runner = self._ctrl.get_runner(self._ctrl.active_tab_id)
            if runner:
                runner.agent_loop.submit_user_answer(text)
            return True
        active = self._ctrl.active_tab_id
        # B3: queue only if THIS tab's agent is already running — not when
        # some other tab runs in the background (that's the whole point of
        # multi-tab). An idle tab starts its agent immediately.
        if self._ctrl.is_tab_running(active):
            self._ctrl.queue_message(text)
            chat_view.add_queued_message(text)
            return True
        self._start_agent(text)
        return True

    def _on_send_clicked(self) -> None:
        text = self._input_area.toPlainText().strip()
        if not text:
            return
        # Reject *before* clearing: a refused message (a button-only
        # approval on this tab) must leave the draft the user typed.
        if not self._on_submit(text):
            return
        self._input_area.clear()

    def _on_cancel(self) -> None:
        if self._is_shutdown:
            return
        active = self._ctrl.active_tab_id
        # Stop is owned by the active tab: it releases THAT tab's gate and
        # cancels that tab's run only.  Tab A keeps waiting when the user
        # presses Stop while looking at tab B.
        self._clear_gate(active)
        # Cancel the active tab's agent (other tabs keep running).
        self._ctrl.cancel()
        # Stop is also the way out of a pending approval, so the composer
        # has to be re-enabled and Stop hidden again — otherwise the gate
        # clears but the panel stays in its blocked presentation.
        self._sync_composer_state()
        # Remove [queued] widgets from the active chat view
        chat_view = self._active_chat_view()
        if chat_view is not None:
            chat_view.remove_queued_messages()

    def _on_settings(self) -> None:
        try:
            from .settings_dialog import SettingsDialog

            dlg = SettingsDialog(
                self._config,
                registry=self._ctrl.provider_registry,
                tool_registry=self._ctrl.tool_registry,
                is_running_callback=lambda: self._ctrl.is_agent_running,
                model_catalog=self._model_catalog,
            )
            result = dlg.exec()
            if result:
                self._config.save(password=dlg.encryption_password)
                self._ctrl.update_settings()
                self._ctrl.reload_mcp()
                # Refresh autocomplete with updated skill list
                self._input_area.set_skill_slugs(self._ctrl.skill_slugs)
                self._refresh_after_model_commit()
                log_info(f"Settings updated: {self._config.provider.name}/{self._config.provider.model}")
            dlg.setParent(None)
        except Exception as e:
            log_error(f"Settings dialog error: {e}")

    def _show_new_chat_dialog(self, context_pct: int) -> str:
        """Show a confirmation dialog with context usage. Returns 'yes', 'clear', or 'no'."""
        dlg = QMessageBox(self)
        dlg.setWindowTitle("New Chat")
        dlg.setText("Start a new chat? Current conversation will be saved.")
        dlg.setInformativeText(f"Context usage: {context_pct}%")
        t = ThemeManager.instance().tokens()
        dlg.setStyleSheet(
            maybe_host_stylesheet(
                f"QMessageBox {{ background: {t.base}; color: {t.text}; }}"
                f"QLabel {{ color: {t.text}; font-size: {role_size(12)}; }}"
                f"QPushButton {{ background: {t.alt_base}; color: {t.text}; border: 1px solid {t.mid}; "
                f"border-radius: 4px; padding: 6px 16px; font-size: {role_size(11)}; min-width: 80px; }}"
                f"QPushButton:hover {{ background: {t.mid}; }}"
            )
        )
        yes_btn = dlg.addButton("Yes, new tab", QMessageBox.ButtonRole.AcceptRole)
        clear_btn = dlg.addButton(
            f"Yes, clear context ({context_pct}% used)",
            QMessageBox.ButtonRole.AcceptRole,
        )
        no_btn = dlg.addButton("No", QMessageBox.ButtonRole.RejectRole)
        dlg.setDefaultButton(no_btn)
        dlg.exec()
        clicked = dlg.clickedButton()
        if clicked is clear_btn:
            return "clear"
        if clicked is yes_btn:
            return "yes"
        return "no"

    def _start_agent(self, user_message: str) -> None:
        """Start the agent for the active tab (legacy entry point)."""
        self._start_agent_for_tab(self._ctrl.active_tab_id, user_message)

    def _start_agent_for_tab(self, tab_id: str, user_message: str, *, add_user_msg: bool = True) -> None:
        """Start (or queue) the agent for a specific tab."""
        chat_view = self._chat_views.get(tab_id)
        if chat_view is None:
            return
        if add_user_msg:
            chat_view.add_user_message(user_message)
        # Presentation follows the ACTIVE tab only: a run started in a
        # background tab must not flip the visible composer to Running.
        self._sync_composer_state()

        # Update tab label after first user message
        self._update_tab_label(tab_id)

        # Concurrency cap: if too many agents run already, queue for this tab
        # and let _on_agent_finished start it when a slot frees.
        if not self._ctrl.has_free_slot():
            self._ctrl.queue_message_for_tab(tab_id, user_message)
            chat_view.add_queued_message(user_message)
            return

        error = self._ctrl.start_agent(user_message, tab_id=tab_id)
        if error:
            chat_view.add_error_message(error)
            self._sync_composer_state()
            return
        # The run is live now — refresh the derived composer state (the
        # label flips Send -> Queue only for the active tab).
        self._sync_composer_state()

        self._ensure_poll_timer()
        assert self._poll_timer is not None
        self._poll_timer.start(50)

    def _ensure_poll_timer(self) -> None:
        if self._poll_timer is not None:
            return
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_events)

    def _stop_poll_timer(self) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()
            try:
                self._poll_timer.timeout.disconnect(self._poll_events)
            except (RuntimeError, TypeError) as e:
                log_debug(f"panel_core timer disconnect failed: {e}")
            self._poll_timer.deleteLater()
            self._poll_timer = None

    def _poll_events(self) -> None:
        if self._polling or self._is_shutdown:
            return
        self._polling = True
        try:
            # Drain EVERY live runner (not just the active one) so background
            # tabs keep streaming. Each event routes to its owning ChatView.
            for tid, runner in list(self._ctrl.iter_runners()):
                chat_view = self._chat_views.get(tid)
                container = chat_view._container if chat_view is not None else None
                # Defer layout/paint passes until the whole batch is processed
                # for this tab (see O(k*n) → O(n) note above).
                if container is not None:
                    container.setUpdatesEnabled(False)
                try:
                    for _ in range(30):
                        event = self._ctrl.get_event(tid, timeout=0)
                        if event is None:
                            if not runner.agent_loop.is_running:
                                self._on_agent_finished(tid)
                            break
                        self._on_event(event, tid)
                finally:
                    if container is not None:
                        container.setUpdatesEnabled(True)
        finally:
            self._polling = False

    def _on_event(self, event: TurnEvent, tab_id: str | None = None) -> None:
        if self._is_shutdown:
            return
        # Route the event to the ChatView that OWNS it (the emitting tab),
        # not necessarily the active tab. Without this, text streamed by a
        # background agent would land in the tab the user is looking at.
        tid = tab_id if tab_id is not None else self._ctrl.active_tab_id
        chat_view = self._chat_views.get(tid)
        if chat_view is None:
            return
        chat_view.handle_event(event)
        # Side-effects that don't belong in chat_view: refresh the
        # Knowledge tab on events that imply the store changed.
        # KNOWLEDGE_RETRIEVED is a READ-SIDE indicator (per-turn context
        # rebuild) and does NOT mutate the store, so it must not trigger
        # a refresh.
        if event.type in (
            TurnEventType.RESEARCH_NOTE_SAVED,
            TurnEventType.EXPLORATION_FINDING,
            TurnEventType.MEMORY_SAVED,
            TurnEventType.HYPOTHESIS_VERDICT,
        ):
            self._on_knowledge_event_refresh(event.type.value)
        if event.usage:
            # Use prompt_tokens from the event directly — session hasn't
            # been updated yet during streaming, so session.last_prompt_tokens
            # would be stale.  prompt_tokens reflects current context size.
            token_count = event.usage.context_tokens if event.usage.context_tokens > 0 else event.usage.total_tokens
            # C3: only the active tab's usage drives the global token display,
            # otherwise concurrent tabs make it flicker between values.
            if token_count > 0 and tid == self._ctrl.active_tab_id:
                self._update_token_display(token_count)
        if event.type in (
            TurnEventType.USER_QUESTION,
            TurnEventType.SAVE_APPROVAL_REQUEST,
            TurnEventType.PLAN_GENERATED,
        ):
            # The gate belongs to the tab that ASKED, so it is recorded on
            # that tab only — the composer is global, but its state is
            # derived from the active tab.
            gate = self._gate(tid)
            gate.pending_answer = True
            # Plan approvals, save approvals, and any question with
            # predefined options MUST be answered via buttons only.
            # Disable text input so free-text ("continue", "redo", etc.)
            # cannot bypass the approval gate.
            has_options = bool(event.metadata.get("options")) if event.metadata else False
            allow_text = bool(event.metadata.get("allow_text")) if event.metadata else False
            needs_button = event.type in (
                TurnEventType.PLAN_GENERATED,
                TurnEventType.SAVE_APPROVAL_REQUEST,
            ) or (has_options and not allow_text)
            if needs_button:
                gate.button_only = True
            if tid == self._ctrl.active_tab_id:
                self._sync_composer_state()
        if event.type == TurnEventType.TOOL_APPROVAL_REQUEST:
            # Script approval is button-only (GUI contract G4): the gate
            # lives in the transcript, so free text must not be able to
            # bypass it.  The event never reached the branch above, which
            # only covered questions / plan / save.
            self._gate(tid).button_only = True
            if tid == self._ctrl.active_tab_id:
                # The composer presentation keeps Stop visible while the
                # gate is pending — see ``_sync_composer_state``.
                self._sync_composer_state()
        if event.type == TurnEventType.MUTATION_RECORDED:
            self._on_mutation_recorded(event)
        if event.type == TurnEventType.MUTATION_UNDONE:
            # Successful reversals removed records; a background tab's undo
            # must not touch the pane the user is looking at.
            self._refresh_mutation_pane()

    def _on_tool_approval(self, tool_call_id: str, decision: str, tab_id: str | None = None) -> None:
        """Forward tool approval to the agent loop of *tab_id*."""
        tid = tab_id if tab_id is not None else self._ctrl.active_tab_id
        runner = self._ctrl.get_runner(tid)
        if runner:
            runner.agent_loop.submit_tool_approval(decision)
        # Deciding releases THIS tab's gate only.  Allow once / Deny both
        # resume the turn (G4), so re-apply the presentation for the tab
        # on screen — another tab waiting on its own approval is untouched.
        self._clear_gate(tid)
        if tid == self._ctrl.active_tab_id:
            self._sync_composer_state()

    def _on_user_answer_submitted(self, answer: str, tab_id: str | None = None) -> None:
        """Handle a button click from UserQuestionWidget (plan/save/ask_user).

        Routes the answer to *tab_id*'s runner (the tab that asked the
        question), not necessarily the active tab.
        """
        tid = tab_id if tab_id is not None else self._ctrl.active_tab_id
        if tid is None or not self._gate(tid).pending_answer:
            return
        self._clear_gate(tid)
        chat_view = self._chat_views.get(tid)
        if chat_view is not None:
            chat_view.add_user_message(answer)
        if tid == self._ctrl.active_tab_id:
            self._sync_composer_state()
        runner = self._ctrl.get_runner(tid)
        if runner:
            runner.agent_loop.submit_user_answer(answer)

    def _on_agent_finished(self, tab_id: str | None = None) -> None:
        if self._is_shutdown:
            return
        tid = tab_id if tab_id is not None else self._ctrl.active_tab_id

        # Only stop the poll timer when NO runner is left alive.
        if not self._ctrl.is_agent_running and self._poll_timer:
            self._poll_timer.stop()

        # A run that ends mid-approval must restore free text for THAT
        # tab (a crash cannot leave the composer locked), and must not
        # clobber a pending question on a different tab.
        self._clear_gate(tid)
        # An undo run removes records from the session stack, and the
        # ``MUTATION_UNDONE`` event may have been dropped on a saturated
        # queue — re-read the stack so the pane never shows a stale count.
        self._refresh_mutation_pane()

        chat_view = self._chat_views.get(tid)
        # The controller pops this tab's runner and returns its next queued
        # follow-up (if any). Cancellation paths clear the queue via cancel().
        next_message = self._ctrl.on_agent_finished(tid)

        if next_message and chat_view is not None:
            # Replace the first queued placeholder with a real user
            # bubble and start the next run.  ``_start_agent_for_tab`` calls
            # ``add_user_message`` which inserts the normal bubble, so
            # we pop the queued widget first to avoid two visible
            # user entries for the same text.  Remaining queued
            # widgets stay in place; the next finish will pop them
            # one at a time in order.
            chat_view.pop_first_queued_message()
            self._start_agent_for_tab(tid, next_message, add_user_msg=True)
            return

        # No more pending messages — drop any stale [queued] widgets
        # for this tab and refresh the running state for the active tab.
        if chat_view is not None:
            chat_view.remove_queued_messages()
        self._sync_composer_state()

    # --- Mutation log integration ---

    def _on_mutation_recorded(self, event: TurnEvent) -> None:
        """Handle a MUTATION_RECORDED event.

        The pane is re-read from the session's undo stack rather than
        appended to: ``AgentLoop`` appends the record to the tab's stack
        *before* it yields this event, and it also emits the event for a
        successful mutation that turned out not to be reversible (which
        never reaches the stack).  One source of truth means the pane and
        what ``/undo`` can reverse can never disagree.
        """
        del event  # the stack snapshot carries everything the pane shows
        self._refresh_mutation_pane()

    def _refresh_mutation_pane(self) -> None:
        """Rebuild the Mutations pane from the ACTIVE session's undo stack.

        Active-session scoped by construction: it reads
        ``SessionControllerBase.mutation_records(active_tab)``, so tab B's
        records never appear while tab A is on screen and vice versa.
        ``Undo Last`` runs through ``_start_agent``, which targets the
        active tab — the same session the pane displays.
        """
        panel = getattr(self, "_mutation_panel", None)
        if panel is None:
            return
        records = self._ctrl.mutation_records(self._ctrl.active_tab_id)
        panel.clear_all()
        for record in records:
            panel.add_mutation(record)
        context_bar = getattr(self, "_context_bar", None)
        if context_bar is not None:
            context_bar.set_mutation_count(panel.entry_count())

    def _on_toggle_mutation_log(self) -> None:
        """Toggle visibility of the mutation log panel.

        Routed through the shared right-panel coordinator (spec §6.4,
        GUI contract G5) so Mutation Log and History remain mutually
        exclusive and the ``Mutations`` button stays in sync with the
        open pane.  The coordinator — not ``isVisible`` — is the single
        owner of pane state, so the button reads ``_pane_name``.
        """
        if self._mutation_panel is None:
            return
        self._show_right_panel(
            "mutation" if getattr(self, "_pane_name", None) != "mutation" else None,
            opener=self._mutations_btn,
        )

    def _on_toggle_history(self) -> None:
        """Toggle the History side panel via the shared coordinator.

        The History button is the user entry point for history-on-demand
        (spec §6.3, §6.4).  Checked → open history; unchecked → close
        every side panel.
        """
        if self._history_panel is None:
            return
        log_debug(f"HIST-TRACE toggle: panel_visible={getattr(self, '_pane_name', None) != 'history'}")  # TEMP-DIAG
        self._show_right_panel(
            "history" if getattr(self, "_pane_name", None) != "history" else None,
            opener=self._history_btn,
        )

    def _show_right_panel(
        self,
        name: Literal["history", "mutation"] | None,
        *,
        opener: QWidget | None = None,
    ) -> None:
        """Single auxiliary-pane coordinator (spec §6.4, GUI contract G5).

        Both panes live in one stack, so showing the requested page
        *is* the mutual exclusion: the other pane becomes hidden because
        Qt shows only the stack's current page.  The button check states
        follow ``name``, so the header / overflow / composer triggers can
        never disagree with what the user sees.

        Opening History also kicks off a list request; closing History
        simply hides the pane — the cached rows and search query survive
        in ``HistoryPanel`` so a reopen restores them while a fresh
        background refresh runs (spec §6.3 "Closing and reopening History
        preserves the last successful rows and search query, but reopening
        always starts a background refresh").

        ``opener`` is the control the user activated; it is remembered so
        :meth:`_close_right_panel` can hand focus back (G5).
        """
        # Defensive: the stack / panels are built during ``_build_ui``; in
        # tests that bypass construction the attributes may be
        # ``MagicMock`` or ``None``.  Guard each call so the coordinator
        # never crashes on a partially-built pane.
        self._pane_name = name
        if opener is not None:
            self._pane_opener = opener
        if getattr(self, "_mutations_btn", None) is not None:
            self._mutations_btn.setChecked(name == "mutation")
        if getattr(self, "_history_btn", None) is not None:
            self._history_btn.setChecked(name == "history")

        stack = getattr(self, "_pane_stack", None)
        pane = self._pane_widget(name) if name is not None else None
        if stack is not None and pane is not None:
            stack.setCurrentWidget(pane)
            stack.setVisible(True)
            # Wide docks start at the G2 nominal pane width; the stack is
            # capped there by ``_apply_pane_presentation``.
            self._apply_pane_presentation(getattr(self, "_narrow", False))
        elif stack is not None:
            stack.setVisible(False)

        if name == "history":
            self._start_history_list_request()

    # ------------------------------------------------------------------
    # History on-demand coordinator (spec §8.1, §11.4)
    # ------------------------------------------------------------------

    def _on_history_retry(self) -> None:
        """Retry button in the History error state (spec §13 "Retry").

        Priority order (Task 5 extension):

        1. Delete retry — if a previous delete FAILED, Retry
           re-dispatches the DELETE — not the load, not the list —
           using the persisted session id stashed in
           ``_history_retry_delete_session_id``.  The retry bypasses
           confirmation (the user already confirmed once) and re-runs
           ``_start_history_delete`` which recaptures fresh scope +
           re-applies the open-tab invariant.
        2. Load retry — Reviewer MEDIUM #2: if a previous load FAILED,
           Retry re-dispatches the LOAD using the id stashed in
           ``_history_retry_load_session_id``.  Fresh scope is
           recaptured.
        3. List retry — Task 8 fallback when no FAILED result is
           remembered.
        """
        if self._is_shutdown:
            return
        # Delete retry has priority — the user just confirmed a delete
        # and the worker FAILED; re-dispatching the LOAD instead would
        # open the chat the user is trying to remove.
        retry_delete_id = self._history_retry_delete_session_id
        if retry_delete_id is not None:
            self._history_retry_delete_session_id = None
            self._start_history_delete(retry_delete_id)
            return
        retry_load_id = self._history_retry_load_session_id
        if retry_load_id is not None:
            # The retry consumed the slot; a new FAILED result will
            # re-populate it, a success path leaves it cleared.
            self._history_retry_load_session_id = None
            self._start_history_load(retry_load_id)
            return
        # List retry (Task 8 behavior).
        self._start_history_list_request()

    def _start_history_load(self, session_id: str) -> None:
        """Submit a single load request through the dedicated executor.

        Shared by ``_on_history_open_requested`` (initial open) and
        ``_on_history_retry`` (FAILED-load retry).  Pre-dedupes an
        already-open persisted session and applies the same
        single-flight guard as the list path so a retry burst cannot
        queue redundant workers.  Captures a fresh immutable scope on
        every call so an IDB switch between FAILED and Retry cannot
        leak the old IDB identity into the retry.

        Spec §10.1, §10.2, §11.4.
        """
        if self._is_shutdown:
            return
        # Pre-load dedupe on retry too: if the session was opened by
        # some other path while the FAILED result sat in the queue,
        # focus it and skip the worker (spec §10.2).
        existing_tab_id = self._ctrl.find_tab_for_session(session_id)
        if existing_tab_id is not None:
            self._focus_tab(existing_tab_id)
            return
        # Single-flight: at most one history request in flight at a time.
        if self._history_pending:
            log_debug("HIST-TRACE load: dropped — pending already True")  # TEMP-DIAG
            return
        # Stash the in-flight load's target session id so a FAILED
        # result (which does NOT carry the id back) can copy it into
        # the retry slot (reviewer MEDIUM #2).
        self._history_last_load_session_id = session_id
        # Capture immutable scope on the main thread, bump generation so
        # any in-flight result from a prior generation is discarded, then
        # submit the load worker to the dedicated single-worker executor
        # (distinct from ``_SAVE_EXECUTOR`` per spec §6.1).  The
        # ``_history_closing`` Event is fresh-and-unset from the last
        # ``_invalidate_history`` (or from ``__init__``); no ``clear()``
        # is needed here and clearing would be unsafe if a stale worker
        # shared the Event (Task 10 race fix: the Event is NEVER reused
        # across requests; ``invalidate`` replaces it with a fresh
        # instance).
        self._history_generation += 1
        scope = self._ctrl.capture_history_scope(self._history_generation)
        if self._history_executor is None:
            self._history_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=_HISTORY_EXECUTOR_PREFIX,
            )
        self._history_pending = True
        # Capture the LIVE closing Event reference and pass it into the
        # worker so it checks ITS captured event, not
        # ``self._history_closing`` dynamically — same race fix as the
        # list path.
        closing_event = self._history_closing
        self._history_executor.submit(
            self._history_load_worker,
            session_id,
            scope,
            closing_event,
        )
        # The dedicated poll timer is the only path back to the Qt main
        # thread for a load result (spec §7.4, §10.1).
        self._ensure_history_poll_timer()

    def _on_history_open_requested(self, session_id: str) -> None:
        """Row click in HistoryPanel → open a historical session.

        Pre-load dedupe (spec §10.2 step 1): if the persisted
        ``session.id`` is already attached to some open tab, focus it
        immediately and skip the worker entirely.  Otherwise delegate
        to ``_start_history_load`` which captures a fresh immutable
        ``HistoryScope`` on the Qt main thread and submits a single load
        request through the dedicated history executor.

        Spec §11.4 keeps at most one list-or-load request in flight at a
        time (``_history_pending``); a second click while a load is
        pending is dropped without a worker submit.

        Task 5 intent gate (spec §11.5): if the user just confirmed (or
        is currently confirming) a delete of this session, the open
        request is dropped BEFORE consulting the controller.  Attaching
        a session the persistence layer is racing to delete would leave
        a tab pointing at a file the worker just unlinked.
        """
        if self._is_shutdown:
            return
        # Task 5 intent gate: short-circuit BEFORE any controller I/O
        # so the find_tab lookup does not even run.  This mirrors the
        # queued-result gate in ``_apply_history_loaded`` and keeps the
        # two LOAD entry points consistent.  ``getattr`` default keeps
        # the read safe on a stripped-down fixture that bypassed
        # ``__init__`` (integration tests build a panel via
        # ``__new__`` and seed only the fields they exercise).
        if session_id in getattr(self, "_history_delete_intents", ()):
            return
        # Pre-load dedupe: focus the existing tab without I/O if the
        # persisted session is already open.  The parameter is the
        # persisted ``SessionState.id`` (manifest / filename key), never
        # the ephemeral ``_sessions`` dictionary key (spec §10.2).
        existing_tab_id = self._ctrl.find_tab_for_session(session_id)
        if existing_tab_id is not None:
            self._focus_tab(existing_tab_id)
            return
        log_debug(f"HIST-TRACE open-request: sid={session_id}")  # TEMP-DIAG
        self._start_history_load(session_id)

    def _history_load_worker(
        self,
        session_id: str,
        scope: HistoryScope,
        closing_event: threading.Event,
    ) -> None:
        """Background load worker (spec §10.1, §10.3, §11.4).

        Runs on the dedicated history executor.  Calls only
        ``self._ctrl.load_history_session`` and enqueues a typed
        ``HistoryLoadResult``.  No Qt/signal/tab mutation happens here —
        attach and rendering run on the Qt main thread in
        ``_apply_history_loaded``.

        Every failure path is converted to a typed result; exceptions
        are never used as cross-thread control flow.  The outer boundary
        ``except Exception`` mirrors the list worker: diagnostics are
        logged via ``log_warning`` but the raw exception message is NOT
        surfaced (it may include session-id / path / OS strings derived
        from untrusted binary content — spec §11.3).

        The captured ``closing_event`` is the Event that was live at
        submit time — Task 10 race fix (see ``_history_list_worker``).
        """
        result: HistoryLoadResult
        try:
            result = self._ctrl.load_history_session(session_id, scope)
        except Exception as exc:
            log_warning(
                f"history load worker failed: {type(exc).__name__}: {exc}",
            )
            result = HistoryLoadResult(
                HistoryRequestStatus.FAILED,
                scope,
                error="",
            )
        # If a close / IDB-switch / shutdown beat us to it, drop the
        # result instead of leaving a full ``SessionState`` sitting in
        # an unpolled queue (spec §11.4 "shutdown … drain-and-discard").
        # The check uses the CAPTURED ``closing_event`` so a stale
        # worker cannot be fooled by an invalidate-then-new-request
        # sequence into observing the new Event's cleared state.
        if not closing_event.is_set():
            self._history_result_queue.put(result)

    def _focus_tab(self, tab_id: str) -> None:
        """Switch the session pages to the tab owning ``tab_id``.

        Spec §10.2 / §10.3: both the pre-load dedupe path and the
        post-load ``ALREADY_OPEN`` path focus an existing tab instead of
        creating a duplicate.  Silently ignores unknown ``tab_id`` so a
        race (tab closed between dedupe and focus) cannot raise into the
        Qt slot that invoked us.
        """
        chat_view = self._chat_views.get(tab_id)
        if chat_view is None:
            return
        index = self._tab_widget.indexOf(chat_view)
        if index >= 0:
            self._tab_widget.setCurrentIndex(index)

    def _start_history_list_request(self) -> None:
        """Capture an immutable scope and submit the list worker (spec §8.1).

        * Capture ``HistoryScope`` on the Qt main thread before any
          background I/O so the worker never reads live controller
          fields while an IDB switch may be mutating them.
        * Submit to the dedicated single-worker ``_history_executor``
          (distinct from ``_SAVE_EXECUTOR`` so a ``flush_saves`` call
          cannot self-deadlock).
        * Start a separate ``QTimer`` because History normally opens
          while the agent is idle (spec §7.4).

        At most one list request may be in flight at a time
        (``_history_pending`` guard, spec §11.4).
        """
        if self._is_shutdown:
            return
        if self._history_pending:
            # A list/load is already queued or running.  Dropping the
            # second submit keeps the executor serialized and prevents
            # a burst of retry clicks from queueing redundant scans.
            log_debug("HIST-TRACE list: dropped — pending already True")  # TEMP-DIAG
            return
        # A new request reopens the worker path.  ``_invalidate_history``
        # already installed a fresh, unset ``_history_closing`` Event for
        # the next request, so no ``clear()`` is needed here — and
        # clearing the current Event would be unsafe if a stale worker
        # from a prior generation happened to share it (Task 10 race fix:
        # the Event is NEVER reused across requests; ``invalidate``
        # replaces it with a fresh instance).
        # Bump the generation BEFORE capturing the scope so any
        # in-flight result from a prior generation is discarded by
        # ``_drain_history_results``.
        self._history_generation += 1
        log_debug(f"HIST-TRACE list: submit gen={self._history_generation}")  # TEMP-DIAG
        scope = self._ctrl.capture_history_scope(self._history_generation)
        # Lazy executor: created on first open, dropped on
        # ``_invalidate_history``.  Never reuse ``_SAVE_EXECUTOR``.
        if self._history_executor is None:
            self._history_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=_HISTORY_EXECUTOR_PREFIX,
            )
        self._history_pending = True
        if self._history_panel is not None:
            self._history_panel.set_loading()
        # Capture the LIVE closing Event reference and pass it into the
        # worker.  The worker checks ITS captured event, not
        # ``self._history_closing`` dynamically — so an invalidate that
        # installs a fresh Event (and sets the OLD one) cannot be hidden
        # from a stale worker that has already captured the old
        # reference at submit time.
        closing_event = self._history_closing
        self._history_executor.submit(
            self._history_list_worker,
            scope,
            closing_event,
        )
        self._ensure_history_poll_timer()

    def _history_list_worker(
        self,
        scope: HistoryScope,
        closing_event: threading.Event,
    ) -> None:
        """Background list worker (spec §8.1, §13).

        Runs on the dedicated history executor.  Performs
        ``SessionHistory.flush_saves`` (so a chat saved moments before
        the History open appears in the list) then lists manifest
        metadata.  Every failure path is converted to a typed
        ``HistoryListResult`` — exceptions are never used as
        cross-thread control flow.

        The worker receives only the immutable ``scope`` and a captured
        ``closing_event`` reference; it does not read mutable controller
        fields (``_idb_path`` / ``_db_instance_id``) because an IDB
        switch may be mutating them concurrently (spec §8.1).  The
        captured ``closing_event`` is the Event that was live at submit
        time — Task 10 race fix: an ``_invalidate_history`` call that
        installs a fresh Event sets the OLD Event, so a stale worker
        that captured the old reference observes ``is_set()==True`` and
        drops its result even after the new request has cleared the
        new Event.
        """
        result: HistoryListResult
        try:
            SessionHistory(self._ctrl.config).flush_saves(timeout=10.0)
            entries = self._ctrl.list_history_sessions(scope)
            log_debug(f"HIST-TRACE list-worker: gen={scope.generation} entries={len(entries)}")  # TEMP-DIAG
            result = HistoryListResult(
                HistoryRequestStatus.LISTED,
                scope,
                tuple(entries),
            )
        except TimeoutError:
            # Save flush did not drain in time.  Do NOT return a
            # potentially incomplete list — the spec requires the UI
            # show a Retry state instead (spec §13).
            result = HistoryListResult(
                HistoryRequestStatus.SAVE_FLUSH_TIMEOUT,
                scope,
            )
        except Exception as exc:
            # Outer boundary catch (spec §11.4): convert any other
            # failure to a typed terminal result so the UI cannot
            # remain stuck in Loading.  The exception message is logged
            # in full for diagnostics but NOT surfaced to the UI —
            # ``result.error`` stays empty because persistence-layer
            # exceptions may include session-id / path / OS strings
            # derived from untrusted binary content, and spec §11.3
            # forbids sending untrusted content to the UI without
            # going through ``core/sanitize``.  The generic UI copy
            # is produced by ``_apply_history_list_result``.
            log_warning(
                f"history list worker failed: {type(exc).__name__}: {exc}",
            )
            result = HistoryListResult(
                HistoryRequestStatus.FAILED,
                scope,
                error="",
            )
        # If a close/invalidation beat us to it, drop the result
        # instead of leaving a full entry list sitting in an unpolled
        # queue (spec §11.4 "shutdown … drain-and-discard").  The
        # check uses the CAPTURED ``closing_event`` (the Event that was
        # live at submit time) so an invalidate-then-new-request
        # sequence cannot trick a stale worker into observing the new
        # Event's cleared state.
        if not closing_event.is_set():
            self._history_result_queue.put(result)

    def _arm_startup_restore_if_idle(self) -> None:
        """Arm the startup auto-restore probe on the Qt main thread.

        Called via ``QTimer.singleShot(0, ...)`` at the end of
        ``_build_ui`` so the request runs one event-loop turn AFTER
        the UI is fully constructed.  At that point:

        * the draft tab exists (``_create_tab`` already populated
          ``_chat_views``),
        * the ``HistoryPanel`` widget exists (so ``set_loading`` /
          ``isVisible`` calls are safe — the panel stays hidden
          until the user opens History),
        * ``_history_executor`` is None (no prior request), so the
          submit creates it lazily, and
        * ``_history_pending`` is False, so the single-flight guard
          inside ``_start_history_list_request`` is the only barrier.

        IDB-scoping is inherited from the existing list worker: the
        ``HistoryScope`` captured through ``self._ctrl.capture_history_scope``
        carries the live ``_idb_path`` / ``_db_instance_id``, and
        ``_ctrl.list_history_sessions`` filters its result through
        ``_matches_current_idb`` before returning.  A session belonging
        to a different binary therefore cannot survive the filter and
        is never a candidate for the auto-load that ``_apply_history_list_result``
        triggers.

        Failure paths (no sessions, corrupt manifest, save-flush timeout,
        load NOT_FOUND / WRONG_IDB / EMPTY / FAILED) are silent: the
        ``_startup_restore_pending`` / ``_startup_restore_load_pending``
        branches in ``_apply_history_list_result`` and
        ``_apply_history_loaded`` consume the flags and return without
        ever calling ``set_entries`` / ``set_error``, so the user just
        sees the blank draft tab they would have seen without this
        probe.

        Invariants preserved:
        * ``_history_pending`` single-flight: if some other path has
          already queued a request, the ``_start_history_list_request``
          call below is a no-op.
        * Generation counter: the existing
          ``_start_history_list_request`` bumps ``_history_generation``
          before submitting, so a later user-initiated History open is
          on a fresh generation and cannot be confused with this probe.
        * The History panel stays hidden: this method never calls
          ``_show_right_panel`` / ``_history_panel.setVisible(True)``.
        """
        if self._is_shutdown:
            return
        if self._history_panel is None:
            # ``_build_ui`` did not finish (or a test bypassed it).
            # Nothing to arm against — the draft tab is also missing
            # so an auto-load would have nowhere to attach.
            return
        if self._history_pending:
            # Another list/load is in flight (e.g. a user-initiated
            # History open raced us).  ``_start_history_list_request``
            # would no-op anyway, but we can also skip the flag set so
            # a future result is not mis-routed through the startup
            # branch.
            return
        if getattr(self, "_startup_restore_pending", False):
            # Already armed (e.g. the deferred slot ran twice because
            # of an early test harness, or an upstream caller wired a
            # second probe).  Idempotent.
            return
        self._startup_restore_pending = True
        self._start_history_list_request()

    def _ensure_history_poll_timer(self) -> None:
        """Create + start the history poll timer if it is not already running.

        Spec §7.4: a dedicated QTimer (distinct from the agent poll
        timer) is the only way to drain history results while the agent
        is idle.  ``timeout`` is wired exactly once to
        ``_drain_history_results``.
        """
        if self._history_poll_timer is not None:
            return
        timer = QTimer(self)
        timer.timeout.connect(self._drain_history_results)
        timer.start(_HISTORY_POLL_INTERVAL_MS)
        self._history_poll_timer = timer

    def _drain_history_results(self) -> None:
        """Qt main-thread slot: drain typed results and apply to HistoryPanel.

        Spec §7.4, §8.1, §10.1: the ONLY method that calls
        ``HistoryPanel.set_entries`` / ``set_error`` (list path) or
        ``_apply_history_loaded`` (load path).  Worker callbacks never
        touch widgets.  Discards any result whose scope generation differs
        from the live ``_history_generation``.

        Reviewer MEDIUM #1: the pending-flag clear is GENERATION-AWARE.
        The terminal result has LANDED, so we clear ``_history_pending``
        BEFORE invoking the apply step.  If the apply then submits a new
        request (NOT_FOUND auto-refresh via ``_start_history_list_request``,
        or a load retry), that submit sets ``_history_pending=True`` again
        at a new generation; the new request's terminal result owns its
        own pending-flag lifecycle.  Clearing up-front avoids the
        ordering deadlock where ``_apply_history_loaded`` →
        ``_start_history_list_request`` would have early-returned because
        ``_history_pending`` was still True from the just-drained result.
        """
        if self._is_shutdown:
            return
        # Drain every queued result; keep the latest matching one so
        # a fast retry (new generation) supersedes a stale result.
        for _ in range(self._history_result_queue.qsize() + 1):
            try:
                result = self._history_result_queue.get_nowait()
            except queue.Empty:
                break
            # Stale generation → silently drop (spec §13 "IDB changes
            # during metadata load" and §11.4 "Request generations
            # discard late results").  Applies to BOTH list and load
            # results — the queue is a typed union and both types carry
            # the scope used to discard them.
            if result.scope.generation != self._history_generation:
                continue
            # The terminal result for this generation has landed → no
            # request remains in flight for this generation.  Clear
            # BEFORE apply so an apply-side submit (NOT_FOUND refresh)
            # is not rejected by the new submit's pending guard.
            self._history_pending = False
            if isinstance(result, HistoryLoadResult):
                # Load result → attach + tab/restore path (Task 9).
                self._apply_history_loaded(result)
            elif isinstance(result, HistoryDeleteResult):
                # Delete result → terminal delete apply (Task 5, §11.5).
                self._apply_history_deleted(result)
            else:
                # List result → existing list-render path (Task 8).
                self._apply_history_list_result(result)
        # Timer-stop check runs on EVERY drain, not only when a result
        # was applied.  Without this, an empty drain (or one that only
        # discarded stale results) would leave the timer spinning on
        # a hidden panel with no pending work — a busy-loop on an idle
        # IDA.  Spec §7.4: "Stop the timer when History is hidden and
        # no request remains."  The close path (``_show_right_panel(None)``
        # hides the panel) and the shutdown path both rely on this
        # terminal drain stopping the timer once the last in-flight
        # worker has either delivered or been discarded.
        history_visible = self._history_panel is not None and self._history_panel.isVisible()
        if not history_visible and not self._history_pending:
            self._stop_history_poll_timer()

    def _apply_history_loaded(self, result: HistoryLoadResult) -> None:
        """Apply a load result on the Qt main thread (spec §10.3, §13).

        ``LOADED`` → ``attach_history_session`` which decides OPENED /
        ALREADY_OPEN / STALE_SCOPE.  ``OPENED`` writes the pending
        restore payload, creates exactly one tab, focuses it, and invokes
        ``_restore_messages_if_needed`` so ``ChatView.restore_from_messages_async``
        remains the rendering path.  ``ALREADY_OPEN`` focuses the
        existing tab.  ``STALE_SCOPE`` is silently dropped (the drain's
        generation check already discarded stale generations; this is a
        defense-in-depth check for the path/instance identity).

        Every non-LOADED status renders exact user-visible copy from
        spec §13.  ``NOT_FOUND`` triggers exactly one list refresh
        (``_start_history_list_request``) so a freshly-deleted session
        disappears from the list.  The drain's generation-aware epilogue
        detects the rebump and leaves ``_history_pending=True`` so the
        new list worker owns its own terminal-result lifecycle.

        ``FAILED`` retains the persisted session id in
        ``_history_retry_load_session_id`` so the Retry button can
        re-dispatch the LOAD (not the list).  Only the id is held —
        never the full ``SessionState`` — so a stale scope is recaptured
        on retry and no large payload is retained.

        ``result.error`` is NEVER surfaced to the widget because worker
        exception messages may include session-id / path / OS strings
        derived from untrusted binary content (spec §11.3).

        Task 5 intent gate (spec §11.5): a queued LOAD result whose
        session id is in ``_history_delete_intents`` is dropped BEFORE
        attach.  The race looks like: user clicks OPEN on a row, the
        LOAD worker starts, user then clicks DELETE on the same row and
        confirms before the LOAD result lands.  Without this gate the
        session would be attached AFTER the user just confirmed
        deleting it, producing a tab whose backing file the worker is
        racing to unlink.  The gate runs after the ``result.session``
        None-check so a NOT_FOUND / FAILED result is still routed to
        its UI copy below.
        """
        # Task 5 intent gate: drop a LOAD that targets a session being
        # deleted.  ``result.session`` is None on every non-LOADED
        # status, so guard the attribute access.  ``getattr`` default
        # keeps the read safe on a stripped-down fixture that bypassed
        # ``__init__``.
        if (
            result.status is HistoryRequestStatus.LOADED
            and result.session is not None
            and result.session.id in getattr(self, "_history_delete_intents", ())
        ):
            return
        # Startup auto-restore: a hidden load follows the startup list
        # request. Consume the flag on every terminal result. Failure
        # paths drop the flag and leave the blank draft tab intact;
        # the History panel stays hidden, so no error copy surfaces.
        startup_load = bool(getattr(self, "_startup_restore_load_pending", False))
        if startup_load:
            self._startup_restore_load_pending = False
            # If the user explicitly opened History between the startup
            # list request and the load result, defer to them: drop
            # the loaded session silently (they want to browse rows,
            # not auto-open one) and submit a fresh list request so
            # the panel actually shows rows.  ``_history_pending`` was
            # cleared by the drain before this apply runs, so the
            # refresh submit is accepted by the single-flight guard.
            user_showing_history = self._history_panel is not None and self._history_panel.isVisible()
            if user_showing_history:
                self._start_history_list_request()
                return
            if result.status is not HistoryRequestStatus.LOADED:
                return
        status = result.status
        log_debug(f"HIST-TRACE load-apply: status={status.name}")  # TEMP-DIAG
        if status is HistoryRequestStatus.LOADED:
            attach = self._ctrl.attach_history_session(result)
            log_debug(f"HIST-TRACE attach: {attach.status.name} tab={attach.tab_id}")  # TEMP-DIAG
            if attach.status is HistoryAttachStatus.OPENED:
                tab_id = attach.tab_id
                session = attach.session
                # Pending payload written BEFORE tab creation so the
                # first ``_restore_messages_if_needed`` on the new tab
                # has the messages ready (spec §10.3 step 1).  The list
                # is a snapshot copy so the worker's SessionState can
                # be mutated later without aliasing this payload.
                messages = list(session.messages) if session and session.messages else []
                self._pending_restore_messages[tab_id] = messages
                self._create_tab(tab_id, "Chat")
                self._restore_messages_if_needed(tab_id)
                self._focus_tab(tab_id)
            elif attach.status is HistoryAttachStatus.REUSED:
                # The active tab was an empty New Chat draft; the
                # loaded session replaced it in place (same tab_id).
                # Rebuild the ChatView over that tab slot and render the
                # historical messages so the user sees the chat they
                # opened instead of a blank tab + a duplicate tab.
                self._rebuild_history_tab(attach.tab_id, attach.session)
            elif attach.status is HistoryAttachStatus.ALREADY_OPEN:
                self._focus_tab(attach.tab_id)
            # Rebind the Knowledge tab to the loaded session's binary
            # (per-binary store model — no per-history snapshot).
            self._on_knowledge_event_refresh("history_loaded")
            # Any successful attach resolution clears a retained
            # retry-load id (reviewer MEDIUM #2): the user's goal — open
            # the session — is satisfied, so a stale retry-load id
            # cannot leak into a future Retry click.
            self._history_retry_load_session_id = None
            # STALE_SCOPE: silently drop (spec §10.3, §13).
            return
        # Non-LOADED statuses: show exact copy per spec §13.  None of
        # these retain a retry-load id (only FAILED does, handled last).
        self._history_retry_load_session_id = None
        if status is HistoryRequestStatus.NOT_FOUND:
            # The drain's generation-aware epilogue detects the rebump
            # performed by ``_start_history_list_request`` and leaves
            # ``_history_pending=True`` so the new list worker owns its
            # terminal-result lifecycle.  No defensive manual clear
            # here — the drain is the single source of truth.
            if self._history_panel is not None:
                self._history_panel.set_error(
                    "This chat is no longer available.",
                    retry_visible=False,
                )
            # Exactly one refresh — no duplicate / infinite generation.
            self._start_history_list_request()
            return
        if status is HistoryRequestStatus.WRONG_IDB:
            if self._history_panel is not None:
                self._history_panel.set_error(
                    "This chat belongs to a different IDB.",
                    retry_visible=False,
                )
            return
        if status is HistoryRequestStatus.EMPTY:
            if self._history_panel is not None:
                self._history_panel.set_error(
                    "This chat is empty and cannot be opened.",
                    retry_visible=False,
                )
            return
        # FAILED (and any future non-success): generic copy with Retry
        # VISIBLE (reviewer MEDIUM #2).  The raw ``error`` is ignored
        # at the UI boundary — diagnostics are logged by the worker.
        # ``HistoryLoadResult`` echoes only the captured scope, not the
        # requested session id; copy the stashed in-flight id
        # (``_history_last_load_session_id``) into the retry slot so
        # the Retry button can re-dispatch the LOAD with a fresh scope.
        # Only the id is retained — never the full ``SessionState``.
        self._history_retry_load_session_id = self._history_last_load_session_id
        if self._history_panel is not None:
            self._history_panel.set_error(
                "Could not open this chat.",
                retry_visible=True,
            )

    def _apply_history_list_result(self, result: HistoryListResult) -> None:
        """Translate a typed list result into HistoryPanel calls (main thread).

        Every user-visible string here is a PanelCore-owned literal —
        ``result.error`` is NEVER surfaced to the widget because the
        worker's exception messages may include session-id / path / OS
        strings derived from untrusted binary content (spec §11.3,
        reviewer point #3).  Diagnostics are logged separately by the
        worker.
        """
        status = result.status
        log_debug(f"HIST-TRACE list-apply: status={status.name} entries={len(result.entries)}")  # TEMP-DIAG
        # Startup auto-restore: a single hidden list request followed by
        # a hidden load for the newest entry. Non-LISTED or empty outcomes
        # clear the flag and suppress the History panel render so the
        # blank draft tab remains the visible default with no error.
        if getattr(self, "_startup_restore_pending", False):
            self._startup_restore_pending = False
            # If the user explicitly opened History while the startup
            # probe was in flight, defer to them: render the rows
            # normally instead of silently swapping the draft tab for
            # the newest saved session.  The History panel's
            # ``set_loading`` spinner (set by ``_start_history_list_request``)
            # is replaced by the real entry list as soon as we reach
            # ``set_entries`` further down.
            user_showing_history = self._history_panel is not None and self._history_panel.isVisible()
            if not user_showing_history:
                if status is HistoryRequestStatus.LISTED and result.entries:
                    sorted_entries = sorted(
                        result.entries,
                        key=lambda e: e.updated_at,
                        reverse=True,
                    )
                    self._startup_restore_load_pending = True
                    self._start_history_load(sorted_entries[0].session_id)
                    return
                return
        if self._history_panel is None:
            return
        if status == HistoryRequestStatus.LISTED:
            # Sort newest-first before handing to the widget (spec §8.1
            # "sort updated_at descending").  Controller listing is
            # already supposed to be newest-first, but the spec makes
            # PanelCore responsible, so re-sort defensively.
            sorted_entries = sorted(
                result.entries,
                key=lambda e: e.updated_at,
                reverse=True,
            )
            self._history_panel.set_entries(list(sorted_entries))
        elif status == HistoryRequestStatus.SAVE_FLUSH_TIMEOUT:
            self._history_panel.set_error(
                "Recent chats are still being saved.",
                retry_visible=True,
            )
        else:
            # FAILED (and any future non-success): generic Retry state.
            # The user-visible copy is a PanelCore-owned literal;
            # ``result.error`` is ignored at the UI boundary.
            self._history_panel.set_error(
                "Could not load chat history.",
                retry_visible=True,
            )

    # ------------------------------------------------------------------
    # Task 5: History delete coordinator (spec §11.5)
    # ------------------------------------------------------------------

    def _clear_history_delete_intent(self, session_id: str) -> None:
        """Remove ``session_id`` from the delete-intent gate.

        Uses immutable replacement so the gate stays race-free for any
        future per-row intent extension.  Safe to call when the id is
        not present (idempotent).
        """
        self._history_delete_intents = {
            intent_id for intent_id in self._history_delete_intents if intent_id != session_id
        }

    def _confirm_history_delete(self, title: str) -> bool:
        """Modal confirmation dialog for deleting a persisted chat.

        Extracted as its own method so tests can patch it without
        driving a real ``QMessageBox`` modal loop.  The dialog is
        window-modal, plain-text (no QSS interpolation of the title —
        the title is untrusted content derived from user/LLM messages
        and must never be concatenated into a stylesheet), default to
        Cancel, and escape to Cancel.  Returns True only when the user
        explicitly clicks the destructive Delete button.
        """
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Delete chat?")
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        # Fixed literal copy only.  ``title`` is untrusted user/LLM
        # content; it is rendered via Qt plain-text (no markup) but
        # never interpolated into QSS or HTML (spec §11.3 sanitize
        # boundary for binary-derived strings).
        dialog.setText(f'"{title}" will be permanently deleted from History.\nThis action cannot be undone.')
        delete_btn = dialog.addButton("Delete", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = dialog.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(cancel_btn)
        dialog.setEscapeButton(cancel_btn)
        dialog.exec()
        return dialog.clickedButton() is delete_btn

    def _on_history_delete_requested(self, session_id: str, title: str) -> None:
        """Row delete button → confirm and submit a delete (spec §11.5).

        Sequence:

          1. Drop silently on shutdown.
          2. ``_history_pending`` busy guard — never start a delete
             while another history operation is in flight (single
             flight invariant, spec §11.4).  Surface a dismiss-only
             busy notice so the user knows why nothing happened.
          3. Open-tab invariant — if the persisted session is already
             attached to a tab, focus the tab and refuse to delete
             until the user closes it.  Deleting an attached session
             would orphan the tab's persistence layer.  Surface a
             dismiss-only notice so the user knows what to do.
          4. INSERT the intent BEFORE the confirmation dialog opens.
             This is the load-bearing race fix: a LOAD worker already
             in flight that lands while the dialog is on screen must
             see the intent and drop its attach (see
             ``_apply_history_loaded``).
          5. ``_confirm_history_delete`` modal — on cancel, clear the
             intent (no DELETE is in flight anymore).
          6. Delegate to ``_start_history_delete`` for single-flight
             scope capture + worker submission.

        ``session_id`` is the persisted ``SessionState.id`` (12-hex by default);
        ``title`` is the untrusted, sanitized, truncated display title
        for the dialog body.  Both come from the widget signal, never
        from live controller state, so a stale row cannot be deleted
        by mistake.
        """
        if self._is_shutdown:
            return
        # Single-flight busy guard (spec §11.4).
        if self._history_pending:
            if self._history_panel is not None:
                self._history_panel.show_notice(
                    "History is busy. Try again shortly.",
                    retry_visible=False,
                    dismiss_visible=True,
                )
            return
        # Open-tab invariant (spec §11.5 preflight).
        existing_tab = self._ctrl.find_tab_for_session(session_id)
        if existing_tab is not None:
            self._focus_tab(existing_tab)
            if self._history_panel is not None:
                self._history_panel.show_notice(
                    "Close this chat before deleting it from History.",
                    retry_visible=False,
                    dismiss_visible=True,
                )
            return
        # Intent inserted BEFORE the modal opens.  Immutable replacement
        # keeps the set race-free against concurrent apply paths.
        self._history_delete_intents = {
            *self._history_delete_intents,
            session_id,
        }
        if not self._confirm_history_delete(title):
            self._clear_history_delete_intent(session_id)
            return
        self._start_history_delete(session_id)

    def _start_history_delete(self, session_id: str) -> None:
        """Submit a single delete request through the dedicated executor.

        Shared by ``_on_history_delete_requested`` (initial request) and
        ``_on_history_retry`` (FAILED retry — skips confirmation).
        Re-applies the shutdown / single-flight / open-tab invariants so
        a retry that lands after the user opened the chat focuses the
        tab instead of deleting it out from under them.  Captures a
        fresh immutable scope on every call so an IDB switch between
        FAILED and Retry cannot leak the old IDB identity into the
        retry.  Lazy-creates the dedicated history executor on first
        use (same pattern as list / load — never reuse
        ``_SAVE_EXECUTOR``, spec §6.1).
        """
        if self._is_shutdown or self._history_pending:
            # Invariants changed between confirmation and submit.  Drop
            # the intent so LOAD paths can proceed again; the retry
            # click (if any) will re-add it.
            self._clear_history_delete_intent(session_id)
            return
        # Re-check the open-tab invariant.  The user may have opened
        # the chat between the FAILED result and the retry click.
        existing_tab = self._ctrl.find_tab_for_session(session_id)
        if existing_tab is not None:
            self._clear_history_delete_intent(session_id)
            self._focus_tab(existing_tab)
            if self._history_panel is not None:
                self._history_panel.show_notice(
                    "Close this chat before deleting it from History.",
                    retry_visible=False,
                    dismiss_visible=True,
                )
            return

        # (Re-)add the intent so a LOAD that races with a retry-delete
        # is still dropped (the retry path skips the pre-confirm insert
        # in ``_on_history_delete_requested``).  Immutable replacement.
        self._history_delete_intents = {
            *self._history_delete_intents,
            session_id,
        }
        # Capture fresh immutable scope on the main thread; bump the
        # generation so any in-flight result from a prior request is
        # discarded by ``_drain_history_results``.  Same pattern as
        # ``_start_history_load`` (spec §11.4).
        self._history_generation += 1
        scope = self._ctrl.capture_history_scope(self._history_generation)
        self._history_pending = True
        # Stash the in-flight target so the slow watchdog (which may
        # fire after a tab change) can decide whether to surface a
        # notice for THIS delete.  Cleared by terminal apply /
        # invalidate / dismiss.
        self._history_last_delete_session_id = session_id
        self._history_retry_delete_session_id = None
        if self._history_panel is not None:
            self._history_panel.clear_notice()
            self._history_panel.set_operation_pending(session_id)
        # Create / reuse the dedicated History executor; never use
        # ``_SAVE_EXECUTOR`` (spec §6.1 — a ``flush_saves`` call cannot
        # self-deadlock against the history worker this way).
        if self._history_executor is None:
            self._history_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=_HISTORY_EXECUTOR_PREFIX,
            )
        executor = self._history_executor
        # Capture the LIVE closing Event reference and pass it into the
        # worker so it checks ITS captured event, not
        # ``self._history_closing`` dynamically — same race fix as the
        # list / load paths.
        closing_event = self._history_closing
        try:
            executor.submit(
                self._history_delete_worker,
                session_id,
                scope,
                closing_event,
            )
        except RuntimeError:
            # Executor was shut down between the None check and submit
            # (e.g. invalidate raced this request).  Roll back the
            # state we just touched so the next user click starts
            # cleanly.
            self._clear_history_delete_intent(session_id)
            self._history_pending = False
            if self._history_panel is not None:
                self._history_panel.set_operation_pending(None)
            raise
        # Cosmetic slow-delete watchdog.  Purely UI — never cancels the
        # in-flight delete (spec §11.5).
        self._start_history_delete_watchdog(scope)
        # Dedicated poll timer is the only path back to the Qt main
        # thread for the delete result (spec §7.4).
        self._ensure_history_poll_timer()

    def _history_delete_worker(
        self,
        session_id: str,
        scope: HistoryScope,
        closing_event: threading.Event,
    ) -> None:
        """Background delete worker (spec §11.5, §11.4).

        Runs on the dedicated history executor.  Calls only
        ``self._ctrl.delete_history_session`` and enqueues a typed
        ``HistoryDeleteResult``.  No Qt / signal / row mutation happens
        here — apply and rendering run on the Qt main thread in
        ``_apply_history_deleted``.

        ``CancelledError`` is re-raised so a Qt-cancelled future can
        unwind the executor's future chain.  Every other failure path
        is converted to a typed terminal result; exceptions are never
        used as cross-thread control flow (spec §11.4).

        The captured ``closing_event`` is the Event that was live at
        submit time — same race fix as list / load: an
        ``_invalidate_history`` call that installs a fresh Event sets
        the OLD Event, so a stale worker that captured the old
        reference observes ``is_set()==True`` and drops its result even
        after the new request has cleared the new Event.
        """
        result: HistoryDeleteResult
        try:
            result = self._ctrl.delete_history_session(session_id, scope)
        except CancelledError:
            # Cancellation propagates so the executor's future chain
            # can unwind; never enqueue a synthetic result for a
            # request the user / system actively cancelled.
            raise
        except Exception as exc:
            # Outer boundary catch (spec §11.4): convert any other
            # failure to a typed terminal result so the UI cannot
            # remain stuck in pending.  The exception diagnostic is
            # logged but its raw text is preserved in ``error`` for
            # the log file only; ``_apply_history_deleted`` never
            # surfaces it to the user (spec §11.3).
            log_error(
                f"history delete worker failed: {type(exc).__name__}: {exc}",
            )
            result = HistoryDeleteResult(
                HistoryDeleteStatus.FAILED,
                scope,
                session_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        # Drop the result if a close / IDB-switch / shutdown beat us to
        # it, instead of leaving it sitting in an unpolled queue (spec
        # §11.4 drain-and-discard).  The check uses the CAPTURED
        # ``closing_event`` so a stale worker cannot be fooled by an
        # invalidate-then-new-request sequence into observing the new
        # Event's cleared state.
        if not closing_event.is_set():
            self._history_result_queue.put(result)

    def _apply_history_deleted(self, result: HistoryDeleteResult) -> None:
        """Apply a delete result on the Qt main thread (spec §11.5).

        Terminal delete apply.  Routes the typed status to the correct
        UI + controller action:

        * ``DELETED`` / ``NOT_FOUND``: terminal success — the row is
          removed from the cached list, the notice is cleared, and a
          single list refresh runs so the list reconciles with the new
          on-disk state.  No retry-load / retry-delete id retained.
        * ``WRONG_IDB``: terminal non-retryable — the row is preserved
          (it may still belong to a different IDB), the notice is
          cleared, and a single list refresh runs.  No retry id.
        * ``FAILED``: non-terminal for the user — the row is preserved
          and the Retry button is enabled so the user can re-attempt
          the delete without re-confirming.  Only the id is retained
          for retry; no SessionState payload is held.

        The watchdog is stopped first so a late slow-delete notice
        cannot fire after the terminal result has already updated the
        UI.  The intent is cleared so LOAD paths can proceed again.
        ``_history_pending`` is cleared by the drain before this method
        runs.
        """
        # Watchdog teardown runs BEFORE any UI mutation so a late
        # timeout cannot surface a "still working" notice on top of
        # the terminal result.
        self._stop_history_delete_watchdog()
        # Intent clear frees the LOAD paths to proceed for this
        # session again (only matters for FAILED — the row is gone on
        # DELETED / NOT_FOUND — but the clear is unconditional and
        # idempotent).
        self._clear_history_delete_intent(result.session_id)
        if self._history_panel is not None:
            self._history_panel.set_operation_pending(None)

        if result.status in {
            HistoryDeleteStatus.DELETED,
            HistoryDeleteStatus.NOT_FOUND,
        }:
            # Terminal success for the request.  The row is gone (or
            # was already gone); refresh the cached list.
            self._history_retry_delete_session_id = None
            if self._history_panel is not None:
                self._history_panel.remove_entry(result.session_id)
                self._history_panel.clear_notice()
            self._start_history_list_request()
            return

        if result.status is HistoryDeleteStatus.WRONG_IDB:
            # Terminal non-retryable.  Row is preserved; refresh so
            # the list reconciles with on-disk state.
            self._history_retry_delete_session_id = None
            if self._history_panel is not None:
                self._history_panel.clear_notice()
            self._start_history_list_request()
            return

        # FAILED: non-terminal.  Row is preserved, Retry is enabled so
        # the user can re-attempt without re-confirming.  Only the id
        # is retained; no payload held.
        self._history_retry_delete_session_id = result.session_id
        if self._history_panel is not None:
            self._history_panel.show_notice(
                "Could not delete this chat.",
                retry_visible=True,
                dismiss_visible=True,
            )

    def _on_history_notice_dismissed(self) -> None:
        """Widget dismiss button → clear retry presentation state only.

        The dismiss button is the user's "I saw the notice, hide it"
        gesture.  It clears the retry-delete slot (the FAILED notice is
        the only one currently wired to retry — the busy / open-tab /
        slow notices are dismiss-only and never set the slot) but does
        NOT mutate the intent set, pending flag, or in-flight worker.
        Widget-side dismiss is purely a UI concern; PanelCore owns all
        persistence / worker / queue state.
        """
        self._history_retry_delete_session_id = None

    def _start_history_delete_watchdog(self, scope: HistoryScope) -> None:
        """Start the cosmetic slow-delete watchdog (spec §11.5).

        ``HISTORY_DELETE_SLOW_NOTICE_SECONDS`` after the request was
        submitted, fire ``_on_history_delete_slow`` which — if the
        generation / session / pending state still match — surfaces a
        dismiss-only "still working" notice.  The watchdog NEVER
        cancels the delete, NEVER clears ``_history_pending``, NEVER
        clears the intent, and NEVER enables Retry.  The terminal
        ``HistoryDeleteResult`` owns all of those transitions.

        Teardown: ``_stop_history_delete_watchdog`` is idempotent and
        is called by terminal apply, invalidate, and any subsequent
        ``_start_history_delete_watchdog`` so at most one watchdog is
        alive at a time (single-flight invariant).
        """
        self._stop_history_delete_watchdog()
        timer = QTimer(self)
        timer.setSingleShot(True)
        # Capture the session id at submit time so a later watchdog
        # timeout cannot mis-fire for a different delete.  The lambda
        # also captures the generation; both are checked at fire time.
        target_session_id = self._history_last_delete_session_id
        target_generation = scope.generation
        timer.timeout.connect(
            lambda: self._on_history_delete_slow(target_generation, target_session_id),
        )
        timer.start(int(HISTORY_DELETE_SLOW_NOTICE_SECONDS * 1000))
        self._history_delete_watchdog = timer

    def _stop_history_delete_watchdog(self) -> None:
        """Stop + tear down the delete watchdog.

        Idempotent; safe to call when no watchdog is alive.  Mirrors
        ``_stop_history_poll_timer``: stop, disconnect, deleteLater,
        null the reference.  Swallows ``RuntimeError`` / ``TypeError``
        on disconnect so partial-init fixtures and post-teardown calls
        stay safe.

        ``getattr`` default keeps this safe when a test fixture
        bypassed ``__init__`` and never seeded the Task-5 fields (the
        legacy Task 7 shutdown / on_database_changed tests construct a
        bare panel via ``object.__new__`` and exercise only the
        restore-removal path).
        """
        timer = getattr(self, "_history_delete_watchdog", None)
        if timer is None:
            return
        try:
            timer.stop()
        except (RuntimeError, TypeError) as e:
            log_debug(f"history delete watchdog stop failed: {e}")
        try:
            timer.timeout.disconnect()
        except (RuntimeError, TypeError) as e:
            log_debug(f"history delete watchdog disconnect failed: {e}")
        try:
            timer.deleteLater()
        except (RuntimeError, TypeError) as e:
            log_debug(f"history delete watchdog deleteLater failed: {e}")
        self._history_delete_watchdog = None

    def _on_history_delete_slow(
        self,
        generation: int,
        session_id: str | None,
    ) -> None:
        """Watchdog timeout → cosmetic "still working" notice.

        Purely UI.  Fire ONLY when the watchdog's captured generation
        matches the live ``_history_generation``, the captured session
        id matches ``_history_last_delete_session_id``, AND
        ``_history_pending`` is still True.  Any mismatch means the
        terminal result has already landed (or a new request
        superseded this one) and the notice would be stale or
        misleading.  Notice is dismiss-only (no Retry — the delete is
        still in flight and there is nothing to retry yet).
        """
        if self._is_shutdown:
            return
        if not self._history_pending:
            return
        if generation != self._history_generation:
            return
        if session_id is None or session_id != self._history_last_delete_session_id:
            return
        if self._history_panel is not None:
            self._history_panel.show_notice(
                "Deleting this chat is taking longer than expected.",
                retry_visible=False,
                dismiss_visible=True,
            )

    def _stop_history_poll_timer(self) -> None:
        """Stop + tear down the history poll timer (spec §7.4).

        Mirrors ``_stop_poll_timer``: stop, disconnect, deleteLater,
        null the reference.  Swallows ``RuntimeError`` / ``TypeError``
        on disconnect to stay idempotent when the signal was never
        wired or was already disconnected by Qt teardown.
        """
        timer = self._history_poll_timer
        if timer is None:
            return
        try:
            timer.stop()
        except (RuntimeError, TypeError) as e:
            log_debug(f"history timer stop failed: {e}")
        try:
            timer.timeout.disconnect(self._drain_history_results)
        except (RuntimeError, TypeError) as e:
            log_debug(f"history timer disconnect failed: {e}")
        try:
            timer.deleteLater()
        except (RuntimeError, TypeError) as e:
            log_debug(f"history timer deleteLater failed: {e}")
        self._history_poll_timer = None

    def _invalidate_history(self, *, clear_panel: bool) -> None:
        """Invalidate in-flight history work on IDB change / shutdown.

        Spec §7.2 (IDB-change order) and §11.4 (shutdown invalidation).
        Idempotent: called from both ``on_database_changed`` and
        ``shutdown``.

        Exact order (Task 10 TDD refinement of the Task 8 helper):

        1. ``_history_generation`` += 1 — BEFORE controller identity
           changes so a worker result for the old IDB is rejected by
           the generation check in ``_drain_history_results``.
        2. ``_history_closing.set()`` — BEFORE timer/executor teardown
           so a worker observing the closing flag mid-flight cannot
           enqueue into a queue that is about to be drained.
        3. Stop + delete the history poll timer.
        4. Detach the executor reference (``self._history_executor =
           None``) THEN non-blocking shutdown with
           ``cancel_futures=True`` so a concurrent request reading
           ``self._history_executor`` cannot re-submit to an executor
           being cancelled, and any not-yet-started submit is dropped.
        5. Drain the result queue.
        6. ``_history_pending = False``.
        7. Clear ``_history_retry_load_session_id`` /
           ``_history_last_load_session_id`` — the stashed ids belong
           to the old IDB; a retry after IDB change must not silently
           re-dispatch a load for the old IDB.
        8. Task 5 (spec §11.5): stop the delete watchdog, clear the
           delete-intent gate, and clear
           ``_history_retry_delete_session_id`` /
           ``_history_last_delete_session_id``.  The watchdog is
           cosmetic-only and must not fire a stale notice after the
           panel has reset; the intent gate must release so LOAD paths
           can proceed for the new IDB; the retry/last ids belong to
           the old IDB and must not leak into a future Retry click.
           Tell the widget to clear its pending-row state so the
           spinner does not stick to a row that no longer exists.
        9. Optional ``panel.clear()`` iff ``clear_panel=True``
           (IDB-change path).  ``shutdown`` passes ``False`` because
           widget mutation after the C++ teardown starts is unsafe.
        10. Replace ``_history_closing`` with a fresh unset
            ``threading.Event``.  THE LOAD-BEARING RACE FIX (spec §11.4
            closing-flag reuse): a stale worker that captured the OLD
            event reference at submit time keeps observing
            ``is_set()==True`` even after a new request has cleared the
            NEW event.  ``Event.clear()`` on the new event does NOT
            un-set the old event, so the old worker drops its result and
            cannot enqueue into the new queue.  ``clear_panel`` does
            not affect this step — the new event is unset in both paths
            because the next ``_start_history_*`` request is the only
            legitimate path to a worker that should be allowed to
            enqueue.

        ``getattr`` defaults keep this helper safe when a test fixture
        bypassed ``__init__`` and never seeded the Task-8 fields (the
        legacy Task 7 shutdown / on_database_changed tests construct a
        bare panel via ``object.__new__`` and exercise only the
        restore-removal path).
        """
        # 1. Generation bump.
        self._history_generation = getattr(self, "_history_generation", 0) + 1
        # 2. Closing flag set BEFORE teardown so a worker that has just
        #    finished its I/O observes ``is_set()==True`` and drops the
        #    result instead of racing the queue drain.
        closing = getattr(self, "_history_closing", None)
        if closing is None:
            closing = threading.Event()
            self._history_closing = closing
        closing.set()
        # 3. Stop + delete the poll timer.
        self._stop_history_poll_timer()
        # 4. Detach the executor reference BEFORE shutting it down so a
        #    concurrent request reading ``self._history_executor`` sees
        #    ``None`` and creates a fresh executor for the new request,
        #    never re-submitting to an executor being cancelled.
        executor = self._history_executor
        self._history_executor = None
        if executor is not None:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except (RuntimeError, TypeError) as e:
                log_debug(f"history executor shutdown failed: {e}")
        # 5. Drain-and-discard any queued results (Task 8 step 4 /
        #    spec §11.4).
        result_queue = getattr(self, "_history_result_queue", None)
        if result_queue is not None:
            while True:
                try:
                    result_queue.get_nowait()
                except queue.Empty:
                    break
        # 6. Pending flag clear.
        self._history_pending = False
        # 6a. Startup auto-restore flags — the pending list/load for the
        #     old IDB must not attach a session to the new IDB.
        self._startup_restore_pending = False
        self._startup_restore_load_pending = False
        #    the stashed ids belong to the old IDB.
        self._history_retry_load_session_id = None
        self._history_last_load_session_id = None
        # 8. Task 5: clear delete-coordinator state.  Stop the watchdog
        #    first so a stale timeout cannot fire after the panel has
        #    reset.  Use ``getattr`` so a bare fixture that bypassed
        #    ``__init__`` and never seeded the Task-5 fields does not
        #    raise AttributeError on the helper call.
        watchdog_stop = getattr(self, "_stop_history_delete_watchdog", None)
        if callable(watchdog_stop):
            watchdog_stop()
        self._history_delete_intents = set()
        self._history_retry_delete_session_id = None
        self._history_last_delete_session_id = None
        history_panel_for_pending = getattr(self, "_history_panel", None)
        if history_panel_for_pending is not None:
            # ``set_operation_pending`` may be absent on a stripped-down
            # recording fixture in integration tests; guard both the
            # attribute lookup and the call so invalidate never raises
            # from a missing widget method.
            set_pending = getattr(history_panel_for_pending, "set_operation_pending", None)
            if callable(set_pending):
                try:
                    set_pending(None)
                except (RuntimeError, TypeError, AttributeError) as e:
                    log_debug(f"history panel set_operation_pending(None) on invalidate failed: {e}")
        # 9. Optional HistoryPanel.clear — only on IDB change.
        if clear_panel:
            history_panel = getattr(self, "_history_panel", None)
            if history_panel is not None:
                try:
                    history_panel.clear()
                except (RuntimeError, TypeError) as e:
                    log_debug(f"history panel clear on invalidate failed: {e}")
        # 10. Replace the closing Event with a fresh unset one so the
        #     next request starts from a clean closing state.  The OLD
        #     event stays set; any worker that captured it at submit
        #     time keeps observing ``is_set()==True`` and drops its
        #     result, even after the new request has cleared the NEW
        #     event — this is the load-bearing race fix.
        self._history_closing = threading.Event()

    def _on_toggle_chat(self) -> None:
        """Return to the chat workspace from the Tools page."""
        self._mode_stack.setCurrentIndex(0)
        self._chat_btn.setChecked(True)
        self._tools_btn.setChecked(False)

    def _on_toggle_tools(self) -> None:
        """Toggle the Tools view (IDA-docked or embedded page)."""
        # Lazy shell creation runs before we decide to show/hide.  In
        # the IDA-docked path the panel may already exist (created by
        # an earlier show_tools_panel call); the helper is idempotent.
        self._ensure_tools_panel_created()

        if self._tools_form is not None:
            # IDA dockable form
            if self._tools_form.is_visible:
                self._tools_form.hide()
                self._tools_btn.setChecked(False)
            else:
                self._tools_form.show()
                self._tools_btn.setChecked(True)
                if self._tools_panel is not None:
                    self._activate_tools_tab(self._tools_panel.current_page())
        else:
            # Toggle the workspace stack between Chat (0) and Tools (1)
            self._mode_stack.setCurrentIndex(1 if self._mode_stack.currentIndex() == 0 else 0)
            if self._mode_stack.currentIndex() == 1:
                self._tools_btn.setChecked(True)
                self._chat_btn.setChecked(False)
                if self._tools_panel is not None:
                    self._activate_tools_tab(self._tools_panel.current_page())
            else:
                self._tools_btn.setChecked(False)
                self._chat_btn.setChecked(True)

    def show_tools_panel(self, tab_index: int | None = None) -> None:
        """Show the tools view and switch to the given tab.

        ``tab_index`` defaults to the Knowledge tab (the read-only
        default that does not trigger any function enumeration).  When
        ``None`` or an unknown index is supplied, the call also falls
        back to Knowledge so legacy numeric callers keep working.

        Public API used by the IDA Open Tools action.  Lazy-creates the
        ToolsPanel shell + the requested tab on first invocation so the
        user only pays the cost of what they actually open.  Subsequent
        calls reuse the existing widgets.
        """
        if self._is_shutdown:
            return
        # ``ToolsPanel.TAB_KNOWLEDGE`` is the default.  Import locally
        # so the constant is read fresh — the panel class is part of
        # the public UI surface and may be subclassed by tests.
        from .tools_panel import ToolsPanel

        default_tab = ToolsPanel.TAB_KNOWLEDGE
        if tab_index is None or tab_index not in (
            ToolsPanel.TAB_AGENTS,
            ToolsPanel.TAB_A2A,
            ToolsPanel.TAB_KNOWLEDGE,
        ):
            tab_index = default_tab
        _t0 = time.monotonic()
        self._ensure_tools_panel_created()
        if self._tools_panel is None:
            return

        # Per-tab init: only build the widget the caller asked for.
        # ``_ensure_tab_initialized`` is idempotent and is a no-op when
        # the tab has already been wired up.
        self._ensure_tab_initialized(tab_index)

        if self._tools_form is not None:
            self._tools_form.show()
            self._tools_form.set_tab(tab_index)
        else:
            self._mode_stack.setCurrentIndex(1)
            self._chat_btn.setChecked(False)
            if hasattr(self._tools_panel, "set_current_page"):
                self._tools_panel.set_current_page(tab_index)
        self._tools_btn.setChecked(True)
        _early_log(
            f"panel_core:show_tools_panel:done:tab={tab_index}:elapsed_ms={int((time.monotonic() - _t0) * 1000)}"
        )

    def _ensure_tools_initialized(self) -> None:
        """Backward-compat: ensure shell + every tab.

        The original Tools-open path called this once and built every
        tab eagerly.  The lazy version now builds only the requested
        tabs via :meth:`_ensure_tab_initialized`, but external callers
        (and tests) may still invoke this method.  We keep it as an
        idempotent "init the shell + every tab" so existing behaviour
        does not regress.
        """
        self._ensure_tools_panel_created()
        if self._tools_panel is None:
            return
        for idx in range(len(_TOOL_PAGES)):
            self._ensure_tab_initialized(idx)

    def _ensure_tools_panel_created(self) -> None:
        """Create the ``ToolsPanel`` shell on first Tools access.

        The previous design built the shell during ``_build_ui``,
        which forced ``ToolsPanel.__init__`` (and its eager A2A
        import + widget construction) to run on the startup path.
        Deferring the shell until the user actually wants it keeps
        Tools-related heavy work off the main startup tick.

        Idempotent — repeated calls are no-ops once the shell exists.
        Safe to call from any entry point (``_on_toggle_tools``,
        ``show_tools_panel``).
        """
        if self._tools_panel is not None:
            return
        if self._is_shutdown:
            return
        _t0 = time.monotonic()
        _early_log("panel_core:_ensure_tools_panel_created:entry")
        try:
            self._tools_panel = ToolsPanel()
            _early_log("panel_core:_ensure_tools_panel_created:constructed")
            self._tools_panel.hide_header()
            # Hook per-tab lazy init.  The panel calls back into us
            # whenever a tab becomes active; we route to the right
            # per-tab initializer.
            self._tools_panel.set_tab_activation_callback(self._activate_tools_tab)
            if self._tools_form_factory is not None:
                # IDA dockable form factory: leave the placeholder in
                # the stack alone and create the wrapper, which will
                # embed ``self._tools_panel`` into its own QWidget.
                self._tools_form = self._tools_form_factory(self._tools_panel)
            else:
                # Embedded mode: replace the placeholder in the
                # mode stack with the real panel.  Use
                # ``removeWidget`` (not deleteLater) so the
                # placeholder is immediately gone instead of
                # surviving until the next event-loop tick.
                placeholder_idx = self._mode_stack.indexOf(self._tools_placeholder)
                if placeholder_idx >= 0:
                    self._mode_stack.removeWidget(self._tools_placeholder)
                    self._tools_placeholder.deleteLater()
                    self._tools_placeholder = None
                self._mode_stack.insertWidget(placeholder_idx, self._tools_panel)
            # Shared tools-event poll timer.  Cheap (100ms idle) and
            # only used by SubagentManager events now that the Renamer
            # tab is hidden.
            self._tools_poll_timer = QTimer(self)
            self._tools_poll_timer.setInterval(100)
            self._tools_poll_timer.timeout.connect(self._poll_tools_events)
            self._tools_poll_timer.start()
        except Exception as e:
            _early_log_crash(e)
            log_error(f"ToolsPanel lazy creation failed: {e}")
            self._tools_panel = None
            return
        _early_log(f"panel_core:_ensure_tools_panel_created:done:elapsed_ms={int((time.monotonic() - _t0) * 1000)}")

    def _activate_tools_tab(self, index: int) -> None:
        """Lazy-init the tab that became active.

        Hooked from ``ToolsPanel._on_tab_changed``.  Per-tab init is
        idempotent so back-and-forth tab switching is cheap.  We also
        guard against ``self._tools_panel`` being ``None`` (e.g. if
        the callback fires during shutdown).
        """
        if self._tools_panel is None:
            return
        self._ensure_tab_initialized(index)

    # Per-tab index → initializer name.  Tab order must remain stable:
    # 0 = Agents, 1 = A2A, 2 = Knowledge.  Indices intentionally mirror
    # ``ToolsPanel.TAB_AGENTS``/``TAB_A2A``/``TAB_KNOWLEDGE``.
    _TAB_INITIALIZERS: ClassVar[dict[int, str]] = {
        0: "_ensure_agents_tab_initialized",
        1: "_ensure_a2a_tab_initialized",
        2: "_ensure_knowledge_tab_initialized",
    }

    def _ensure_tab_initialized(self, index: int) -> None:
        """Dispatch to the per-tab initializer for ``index``.

        Silently no-ops for unknown indices so a future tab addition
        does not crash callers that ask by index.
        """
        method_name = self._TAB_INITIALIZERS.get(index)
        if method_name is None:
            return
        if not hasattr(self, "_tab_initialized_flags"):
            self._tab_initialized_flags = {}
        if self._tab_initialized_flags.get(index):
            return
        method = getattr(self, method_name, None)
        if method is None:
            return
        _t0 = time.monotonic()
        try:
            method()
            self._tab_initialized_flags[index] = True
        except Exception as e:
            log_error(f"Tab {index} initialization failed: {e}")
            return
        _early_log(f"panel_core:tab_init:index={index}:elapsed_ms={int((time.monotonic() - _t0) * 1000)}")

    # -- Per-tab initializers -----------------------------------------------

    def _ensure_agents_tab_initialized(self) -> None:
        """Build the agents tab widget."""
        if self._tools_panel is None:
            return
        from .agent_tree import AgentTreeWidget

        _t0 = time.monotonic()
        self._agent_tree = AgentTreeWidget()
        _early_log(f"panel_core:_ensure_agents_tab_initialized:built:elapsed_ms={int((time.monotonic() - _t0) * 1000)}")
        self._agent_tree.cancel_requested.connect(self._on_cancel_agent)
        self._agent_tree.inject_summary_requested.connect(self._on_inject_summary)
        self._tools_panel.set_agents_widget(self._agent_tree)

    def _ensure_a2a_tab_initialized(self) -> None:
        """Build the A2A bridge widget on first A2A tab activation.

        The previous design created ``A2ABridgeWidget`` in
        ``ToolsPanel.__init__`` (which forced A2A imports +
        ``A2ADispatcher()`` + a ``QTimer.singleShot(0, _refresh_agents)``
        that ran the full discovery — PATH checks, ``orchestra.toml``
        load, optional live HTTP agent-card fetches — on Tools open).
        Deferring widget construction to first A2A-tab selection
        removes that cost from the Tools open path.  Discovery is
        also deferred to first Refresh (see ``A2ABridgeWidget``
        changes); the placeholder tells the user to click Refresh.
        """
        if self._tools_panel is None:
            return
        from .a2a_widget import A2ABridgeWidget

        _t0 = time.monotonic()
        self._a2a_bridge_widget = A2ABridgeWidget(self)
        _early_log(f"panel_core:_ensure_a2a_tab_initialized:built:elapsed_ms={int((time.monotonic() - _t0) * 1000)}")
        self._tools_panel.set_a2a_widget(self._a2a_bridge_widget)

    def _ensure_knowledge_tab_initialized(self) -> None:
        """Build the Knowledge panel on first Knowledge tab activation."""
        if self._tools_panel is None:
            return
        from .knowledge_panel import KnowledgePanel

        _t0 = time.monotonic()
        self._knowledge_panel = KnowledgePanel()
        _early_log(
            f"panel_core:_ensure_knowledge_tab_initialized:built:elapsed_ms={int((time.monotonic() - _t0) * 1000)}"
        )
        try:
            self._knowledge_panel.set_show_retrieved(
                bool(getattr(self._config, "knowledge_show_retrieved_in_chat", False))
            )
        except Exception:
            pass
        self._knowledge_panel.set_disabled_state(not bool(getattr(self._config, "knowledge_enabled", True)))
        self._knowledge_panel.show_retrieved_changed.connect(self._on_knowledge_show_changed)
        self._knowledge_panel.refresh_requested.connect(self._refresh_knowledge_panel)
        self._tools_panel.set_knowledge_widget(self._knowledge_panel)
        # Debounce knowledge-panel refresh: many write-side events
        # (e.g. exploration findings) can fire in a single burst, and
        # we only need to repaint the table once.  Reusing a single
        # QTimer coalesces bursts into one refresh ~50ms after the
        # last event lands.
        self._knowledge_refresh_timer = QTimer(self)
        self._knowledge_refresh_timer.setSingleShot(True)
        self._knowledge_refresh_timer.setInterval(50)
        self._knowledge_refresh_timer.timeout.connect(self._refresh_knowledge_panel)
        # Refresh on first activation.  Subsequent refreshes go through
        # the event-driven debounce path in :meth:`_on_knowledge_event_refresh`.
        self._schedule_initial_knowledge_refresh()

    def _schedule_initial_knowledge_refresh(self) -> None:
        """Schedule a one-shot initial knowledge refresh.

        Skips the refresh entirely if the panel has been shut down or
        replaced before the timer fires (e.g. the user closed the
        panel during the first event-loop turn).  Uses
        ``QTimer.singleShot`` rather than a member ``QTimer`` so the
        reference cannot outlive the panel.
        """
        if self._is_shutdown:
            return
        if getattr(self, "_knowledge_panel", None) is None:
            return

        def _fire() -> None:
            if self._is_shutdown:
                return
            if getattr(self, "_knowledge_panel", None) is None:
                return
            self._refresh_knowledge_panel()

        QTimer.singleShot(0, _fire)

    def _get_or_create_subagent_manager(self):
        """Return the session's shared SubagentManager.

        Owned by the controller so agent loops and the Tools panel use one
        registry and one event queue; without this split, chat-spawned
        children would be invisible to the Agents tab.
        """
        manager = self._ctrl.get_or_create_subagent_manager()
        if manager is not None:
            self._subagent_manager = manager
        return manager

    def _on_cancel_agent(self, agent_id: str) -> None:
        """Handle agent cancel request from AgentTreeWidget."""
        mgr = self._get_or_create_subagent_manager()
        if mgr is not None:
            mgr.cancel(agent_id)

    def _on_inject_summary(self, agent_id: str) -> None:
        """Inject a completed agent's summary into the active chat."""
        mgr = self._get_or_create_subagent_manager()
        if mgr is None:
            return
        info = mgr.get(agent_id)
        if info is None or not info.summary:
            return
        elapsed = (info.completed_at or info.created_at) - info.created_at
        text = (
            f"[Subagent \u201c{info.name}\u201d completed ({info.turn_count} turns, {elapsed:.0f}s)]\n\n{info.summary}"
        )
        self._start_agent(text)

    @staticmethod
    def _agent_info_from(info) -> AgentInfo:
        """Snapshot a manager record into the tree widget's view model.

        Shared by the event branch and the periodic running sweep so both
        carry the live activity feed. ``info.activity`` is appended by the
        worker thread while the Qt thread reads it; list append/truncate
        plus the join are atomic, so the worst case is a snapshot that
        misses the very newest entry.
        """
        from .agent_tree import AgentInfo

        elapsed = (info.completed_at or time.time()) - info.created_at
        return AgentInfo(
            agent_id=info.id,
            name=info.name,
            agent_type=info.agent_type,
            status=info.status.value.upper(),
            turns=info.turn_count,
            elapsed_seconds=elapsed,
            summary=info.summary,
            category=info.category,
            activity="\n".join(info.activity),
        )

    def _poll_tools_events(self) -> None:
        """Poll all tools subsystems for events."""
        if self._is_shutdown:
            return

        # Poll subagent manager events. Go through the accessor (not the raw
        # attribute) so a manager created by an agent loop's own fallback
        # is picked up, and so one registry serves the tab and the chat.
        mgr = self._get_or_create_subagent_manager()
        if mgr is not None:
            for _ in range(10):
                event = mgr.poll_event()
                if event is None:
                    break
                # Update agent tree
                if hasattr(self, "_agent_tree"):
                    meta = event.metadata or {}
                    agent_id = meta.get("agent_id", "")
                    info = mgr.get(agent_id)
                    if info is not None:
                        self._agent_tree.update_agent(self._agent_info_from(info))
                # Show in chat for spawned/completed/failed — but skip
                # bulk_rename agents to avoid polluting the conversation.
                if event.type in (
                    TurnEventType.SUBAGENT_SPAWNED,
                    TurnEventType.SUBAGENT_COMPLETED,
                    TurnEventType.SUBAGENT_FAILED,
                ):
                    is_bulk = info is not None and info.category == "bulk_rename"
                    if not is_bulk:
                        chat_view = self._active_chat_view()
                        if chat_view is not None:
                            chat_view.handle_event(event)

            # Refresh elapsed time for all RUNNING agents (~1 Hz, not every tick)
            now = time.time()
            last_sweep = getattr(self, "_last_agent_sweep", 0.0)
            if hasattr(self, "_agent_tree") and (now - last_sweep) >= 1.0:
                self._last_agent_sweep = now
                for info in mgr.list_all():
                    if info.status.value == "running":
                        snapshot = self._agent_info_from(info)
                        snapshot.elapsed_seconds = now - info.created_at
                        self._agent_tree.update_agent(snapshot)

    def _on_undo_requested(self, count: int) -> None:
        """Handle undo request from the mutation log panel.

        ``/undo`` goes through the normal agent path, which targets the
        ACTIVE tab — the same session the pane displays — so a tab can
        only ever reverse its own mutations.
        """
        if self._is_shutdown:
            return
        # Submit /undo command through the normal agent path
        self._start_agent(f"/undo {count}")

    # ------------------------------------------------------------------
    # Knowledge panel wiring
    # ------------------------------------------------------------------

    def _on_knowledge_show_changed(self, checked: bool) -> None:
        """Persist the *Show retrieved knowledge in chat* toggle."""
        try:
            self._config.knowledge_show_retrieved_in_chat = bool(checked)
            # Best-effort: don't crash UI if save() fails (e.g. read-only disk).
            try:
                self._config.save()
            except Exception as e:
                log_debug(f"config save after knowledge toggle failed: {e}")
        except Exception as e:
            log_debug(f"knowledge toggle persist failed: {e}")

    def _refresh_knowledge_panel(self) -> None:
        """Re-populate the Knowledge tab from the current IDB path.

        Prefers the SQLite repository exposed by
        ``SessionControllerBase.memory_service`` (Task 7) when the
        memory service is wired.  Falls back to the legacy JSONL
        ``make_store`` path when ``memory_service`` is ``None`` or the
        SQLite read raises; SQLite failures are logged at debug level
        so the panel degrades gracefully on a corrupted repository.
        """
        if self._is_shutdown:
            return
        panel = getattr(self, "_knowledge_panel", None)
        if panel is None:
            return

        if not bool(getattr(self._config, "knowledge_enabled", True)):
            panel.set_disabled_state(True)
            return

        idb_path = ""
        try:
            idb_path = self._ctrl.session.idb_path if self._ctrl and self._ctrl.session else ""
        except Exception:
            idb_path = ""

        if not idb_path:
            panel.set_disabled_state(False)
            panel.set_disabled_message("No IDB path is set. Open a binary to populate the knowledge store.")
            return

        # Prefer SQLite when the memory service is wired (Task 8).
        memory_service = getattr(self._ctrl, "memory_service", None)
        if memory_service is not None:
            try:
                from ..memory.notes import list_notes

                memories = memory_service.repository.list_memories()
                entities = memory_service.repository.list_entities()
                relations = memory_service.repository.list_relations()
                obs_count = memory_service.repository.count_observations()
                notes_dir = str(memory_service.paths.notes)
                panel.set_disabled_state(False)
                panel.set_counts(
                    {
                        "memories": len(memories),
                        "entities": len(entities),
                        "relations": len(relations),
                        "observations": obs_count,
                    }
                )
                panel.populate(
                    memories=memories,
                    entities=entities,
                    relations=relations,
                    notes=[f"{(n.title or '')}: {(n.body or '').strip()[:400]}" for n in list_notes(notes_dir)[:20]],
                )
                return
            except Exception as e:
                log_debug(f"knowledge panel SQLite refresh failed: {e}, falling back to JSONL")

        # Fallback: JSONL store.
        try:
            from ..memory.ingest import make_store
            from ..memory.notes import list_notes

            store, paths = make_store(idb_path)
            if store is None or paths is None:
                panel.set_disabled_state(False)
                panel.set_disabled_message("Could not initialize the knowledge store.")
                return

            # One read per file (was: four via counts() + three more
            # via the list_* calls = seven total).
            memories = store.list_memories()
            entities = store.list_entities()
            relations = store.list_relations()
            obs_count = store.count_observations()
            panel.set_disabled_state(False)
            panel.set_counts(
                {
                    "memories": len(memories),
                    "entities": len(entities),
                    "relations": len(relations),
                    "observations": obs_count,
                }
            )
            panel.populate(
                memories=memories,
                entities=entities,
                relations=relations,
                notes=[
                    f"{(n.title or os.path.basename(n.path or 'note'))}: {(n.body or '').strip()[:400]}"
                    for n in list_notes(paths.notes_dir)[:20]
                ],
            )
        except Exception as e:
            log_debug(f"knowledge panel refresh failed: {e}")
            panel.set_disabled_message(f"Failed to load knowledge: {e}")

    def _on_knowledge_event_refresh(self, event_type: str) -> None:
        """Hook for relevant events to nudge the panel to refresh.

        Coalesces bursty events (e.g. several ``EXPLORATION_FINDING``
        events emitted back-to-back) into a single refresh via a
        shared single-shot :class:`QTimer`.  ``.start()`` on a running
        single-shot timer resets its deadline, so only the LAST event
        in a burst ends up triggering the actual repaint.
        """
        del event_type  # unused — the timer coalesces regardless
        if getattr(self, "_is_shutdown", False):
            return
        timer = getattr(self, "_knowledge_refresh_timer", None)
        if timer is None:
            return
        try:
            timer.start()  # 50ms debounce window
        except Exception:
            pass

    def _set_running(self, running: bool) -> None:
        """Force the composer presentation (used by prefill and host code)."""
        self._sync_composer_state(force_running=running)

    def _apply_composer_metric_minima(self) -> None:
        """Re-derive the composer's font-dependent minima.

        The editor and its buttons learn a user font override through
        their QSS, which Qt only resolves while the style is being
        applied — so the minima are refreshed here rather than only at
        build time.  Both keep their contract floor at the default font.
        """
        editor = getattr(self, "_input_area", None)
        if editor is not None:
            editor.setMinimumHeight(_composer_min_height(editor))
        for button in (getattr(self, "_send_btn", None), getattr(self, "_cancel_btn", None)):
            if button is not None:
                button.setFixedWidth(_action_button_width(button))

    def _sync_composer_state(self, *, force_running: bool | None = None) -> None:
        """Apply the ACTIVE tab's composer state to the visible controls.

        Every piece the user sees — input lock, Send/Queue label, Stop
        visibility, status text — is derived here from the active tab's
        own gate plus that tab's run state.  Background tabs keep their
        gates and keep running without touching what is on screen.
        """
        running = force_running if force_running is not None else self._ctrl.is_tab_running(self._ctrl.active_tab_id)
        awaiting = self._active_tab_button_gate()
        # Keep input enabled so users can queue follow-up messages while
        # running — UNLESS this tab is waiting for a button-only approval.
        if awaiting:
            self._input_area.set_enabled(False)
            self._input_area.setPlaceholderText("Use the Approve/Reject buttons above to continue.")
        else:
            self._input_area.set_enabled(True)
            if running:
                self._input_area.setPlaceholderText(
                    "Luc Nhan is thinking... press Enter (or Queue) to queue a follow-up."
                )
            else:
                self._input_area.setPlaceholderText("Ask about this binary... (/ for skills, /modify to patch)")

        self._send_btn.setVisible(True)
        self._send_btn.setEnabled(not awaiting)
        self._send_btn.setText("Queue" if running else "Send")
        # Keep the accessible name in sync with the dynamic label so a
        # screen reader announces "Queue" while the agent is running.
        self._send_btn.setAccessibleName(
            "Queue — Queue a follow-up message" if running else "Send — Send the message (Enter)"
        )
        # Measure after changing Send/Queue: QSS hints use the current label.
        self._apply_composer_metric_minima()
        # Stop stays available while an approval is pending: the agent is still
        # blocked and the user needs a way out that does not depend on the
        # gate's buttons (GUI contract G3/G4).  Tying visibility to
        # ``running`` alone hid it at the exact moment it mattered.
        self._cancel_btn.setVisible(running or awaiting)
        # Picker busy follows the existing turn lifecycle — no timer, no
        # polling.  The controller guard still catches a race at commit.
        picker = getattr(self, "_picker", None)
        if picker is not None:
            try:
                picker.set_busy(bool(self._ctrl.is_agent_running))
            except Exception as exc:  # pragma: no cover - defensive
                log_debug(f"picker busy sync skipped: {exc}")
        # The trigger tooltip carries the busy reason (G6), so it has to
        # follow the same turn transition or it lies until the next resize.
        trigger = getattr(self, "_footer_trigger", None)
        if trigger is not None:
            trigger.setToolTip(self._footer_tooltip())
        # Header status carries the real agent state as *text* (never a
        # bare colour dot — GUI contract G1/G9).
        status = getattr(self, "_status_label", None)
        if status is not None:
            if awaiting:
                text = "Waiting for approval"
            elif running:
                text = "Running"
            else:
                text = "Ready"
            status.setText(text)
            status.setToolTip(text)
