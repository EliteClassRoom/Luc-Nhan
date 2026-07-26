"""Settings dialog for provider, model, API key, and temperature configuration."""

from __future__ import annotations

import copy
import queue
import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from ..constants import GLM_DEFAULT_MODEL
from ..core.config import RikuganConfig
from ..core.glm_config import (
    GLM_DIALECT,
    GLM_ENDPOINT_BASE_URLS,
    GLM_ENDPOINT_CODING_PLAN,
    GLM_ENDPOINT_STANDARD,
    REASONING_EFFORT_VALUES,
    REASONING_TOKEN_CEILING_DEFAULT,
    REASONING_TOKEN_CEILING_MAX,
    REASONING_TOKEN_CEILING_MIN,
    RECOVERY_MAX_TOKENS_DEFAULT,
    RECOVERY_MAX_TOKENS_MAX,
    RECOVERY_MAX_TOKENS_MIN,
    get_glm_model_metadata,
)
from ..core.log_sinks import set_host_log_level
from ..core.logging import log_debug, log_error
from ..core.types import ModelInfo
from ..providers.auth_cache import resolve_auth_cached
from ..providers.ollama_provider import DEFAULT_OLLAMA_URL
from ..providers.registry import ProviderRegistry
from .qt_compat import (
    OK_CANCEL_BUTTONS,
    YES_NO_BUTTONS,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    Qt,
    QTabWidget,
    QTimer,
    QVBoxLayout,
    QWidget,
)
from .styles import (
    build_settings_dialog_stylesheet,
    get_err_status_style,
    get_error_label_style,
    get_hint_status_style,
    get_ok_status_style,
    get_settings_btn_style,
)
from .theme.applicator import disconnect_theme
from .theme.manager import ThemeManager

_DEFAULT_MINIMAX_URL = "https://api.minimax.io/anthropic"
_CUSTOM_PROVIDER_URL_PLACEHOLDER = "https://api.example.com/v1"

# Generous upper bound for the Max Output Tokens spin box when the
# selected model has no metadata (manual / custom models).  Matches the
# built-in ``QSpinBox`` range so the spin box never silently truncates a
# legitimate value the user typed.
_MANUAL_MAX_TOKENS = 2_000_000

# Known default API base URLs per provider — used to auto-clear on switch
_PROVIDER_BASES = {
    "ollama": DEFAULT_OLLAMA_URL,
    "minimax": _DEFAULT_MINIMAX_URL,
    "glm": "https://api.z.ai/api/paas/v4",
}

# Z.AI's official Chat Completions endpoint for the GLM family.
_ZAI_API_BASE = "https://api.z.ai/api/paas/v4"

# GLM endpoint-type → base URL.  Mirror of GLM_ENDPOINT_BASE_URLS in
# glm_config, kept here as a module-level constant so the settings dialog
# can resolve base URLs without re-importing the core module in the handler.
_PROVIDER_BASES_GLM: dict[str, str] = {
    GLM_ENDPOINT_STANDARD: GLM_ENDPOINT_BASE_URLS[GLM_ENDPOINT_STANDARD],
    GLM_ENDPOINT_CODING_PLAN: GLM_ENDPOINT_BASE_URLS[GLM_ENDPOINT_CODING_PLAN],
}

# Placeholder/default keys that should be cleared on provider switch
_PROVIDER_DEFAULT_KEYS = {"ollama"}

# Backwards-compatible alias (tests and external code may reference the old name)
_resolve_auth_cached = resolve_auth_cached

#: Hostname of Z.AI's API endpoint.  The one-time migration prompt
#: fires only when ``urlparse(api_base).hostname`` is exactly this value.
_ZAI_HOSTNAME = "api.z.ai"


def _prompt_zai_migration(parent: Any = None) -> bool:
    """Show a one-time modal asking the user to opt into the GLM dialect.

    Returns ``True`` on accept (user wants GLM dialect), ``False`` on
    decline or dismiss.  The dialog text explicitly says the current
    connection will be treated as OpenAI-compatible until the user opts in.
    """
    from .qt_compat import QMessageBox

    result = QMessageBox.question(
        parent,
        "Enable GLM Dialect?",
        (
            "This connection targets api.z.ai, which serves GLM-4.7 / GLM-5.x models.\n\n"
            "Enable the GLM dialect for reasoning-content support, thinking\n"
            "preservation, and degeneration-guard recovery?\n\n"
            "If you decline, the connection continues as a generic\n"
            "OpenAI-compatible endpoint."
        ),
        YES_NO_BUTTONS,
        QMessageBox.StandardButton.No,
    )
    return result == QMessageBox.StandardButton.Yes


class _ModelFetcher:
    """Fetches models in a background thread. Results collected via queue.

    This is a plain Python class — no QObject, no Qt signals.
    Results are polled from the main thread via a QTimer, eliminating
    all cross-thread Shiboken/PySide6 signal delivery crashes.
    """

    def __init__(self, registry: ProviderRegistry):
        self._registry = registry
        self._queue: queue.Queue = queue.Queue()
        self._alive = True

    def shutdown(self) -> None:
        self._alive = False
        # Drain the queue to unblock any pending puts
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def fetch(self, provider_name: str, api_key: str, api_base: str) -> None:
        """Fetch models for the given provider configuration.

        Always pre-initialises the provider on the MAIN thread
        (``ensure_ready``), because Python 3.14 crashes when heavy
        C-extension SDK packages (httpx, h2, ssl, ...) are first
        imported from a background thread.  The provider object is then
        reused inside the worker thread to call ``list_models()``.

        The previous revision supported an ``ensure_ready=False`` flag
        for "background-thread SDK import" — that path was unsafe and
        has been removed.  All model fetches go through this safe
        path; provider/key-change handlers no longer auto-fetch live
        models (see ``_on_provider_changed`` / ``_on_key_edited``) and
        only the explicit Refresh button calls ``fetch`` at all.
        """
        try:
            provider = self._registry.new_instance(
                provider_name,
                api_key=api_key,
                api_base=api_base,
            )
            provider.ensure_ready()
        except Exception as e:
            if self._alive:
                self._queue.put(("error", provider_name, str(e)))
            return

        def _run():
            try:
                models = provider.list_models()
                if self._alive:
                    self._queue.put(("models", provider_name, models))
            except Exception as e:
                if self._alive:
                    self._queue.put(("error", provider_name, str(e)))

        threading.Thread(target=_run, daemon=True).start()

    def poll(self) -> tuple | None:
        """Non-blocking poll. Returns ('models'|'error', provider_name, payload) or None."""
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None


_BUILTIN_PROVIDERS = [
    "anthropic",
    "openai",
    "glm",
    "gemini",
    "ollama",
    "minimax",
    "openai_compat",
]


class _AddProviderDialog(QDialog):
    """Mini-dialog to create a new custom OpenAI-compatible connection."""

    def __init__(self, existing_names: list, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Add Custom Connection")
        self.setMinimumWidth(400)
        self._existing = {n.lower() for n in existing_names}

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. minimax, deepseek, local-vllm")
        form.addRow("Connection Name:", self._name_edit)

        self._base_edit = QLineEdit()
        self._base_edit.setPlaceholderText(_CUSTOM_PROVIDER_URL_PLACEHOLDER)
        form.addRow("API Base URL:", self._base_edit)

        layout.addLayout(form)

        self._error_label = QLabel()
        self._error_label.setStyleSheet(get_error_label_style())
        self._error_label.hide()
        layout.addWidget(self._error_label)

        buttons = QDialogButtonBox(OK_CANCEL_BUTTONS)
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate(self) -> None:
        name = self._name_edit.text().strip().lower().replace(" ", "-")
        if not name:
            self._error_label.setText("Name is required")
            self._error_label.show()
            return
        if name in self._existing:
            self._error_label.setText(f"'{name}' already exists")
            self._error_label.show()
            return
        base = self._base_edit.text().strip()
        if not base:
            self._error_label.setText("API Base URL is required")
            self._error_label.show()
            return
        self._name_edit.setText(name)
        self.accept()

    def provider_name(self) -> str:
        return self._name_edit.text().strip()

    def api_base(self) -> str:
        return self._base_edit.text().strip()


class SettingsDialog(QDialog):
    """Configuration dialog for Rikugan."""

    def __init__(
        self,
        config: RikuganConfig,
        registry: ProviderRegistry | None = None,
        tool_registry: Any | None = None,
        is_running_callback: Callable[[], bool] | None = None,
        parent: QWidget | None = None,
    ):
        # Use None parent to avoid lifecycle coupling with IDA PluginForm widgets
        super().__init__(None)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        # Object name is used by ``build_settings_dialog_stylesheet`` to
        # scope the QSS so it does not bleed into the host UI.
        self.setObjectName("rikugan_settings")
        self._config = config
        # Snapshot the config at construction so ``done(Rejected)`` can
        # restore it. Provider switching, custom-provider add/remove and
        # UI→config syncs mutate the *live* config object eagerly (before
        # the dialog is accepted), so without this snapshot a Cancel
        # would silently persist those edits — losing e.g. the previous
        # provider's API key. Accepted dialogs keep the live edits.
        self._config_snapshot = copy.deepcopy(config)
        self._tool_registry = tool_registry
        self._registry = registry or ProviderRegistry()
        self._registry.register_custom_providers(list(self._config.custom_providers.keys()))
        # Optional callback the panel uses to know whether the agent is
        # currently running. The dialog uses it to disable inputs that
        # would race the live runner (e.g. provider switches mid-prompt).
        self._is_running_callback = is_running_callback or (lambda: False)
        self._fetcher = _ModelFetcher(self._registry)
        self._fetched_models: list[ModelInfo] = []
        self._resolved_token: str = ""
        self._model_restore_hint: str = self._config.provider.model.strip()
        self._shown = False
        self._closed = False
        self.encryption_password: str = ""
        self.setWindowTitle("Luc Nhan Settings")
        screen = QApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            self.resize(min(int(avail.width() * 0.45), 900), min(int(avail.height() * 0.7), 800))
        else:
            self.resize(700, 600)
        self.setMinimumWidth(400)
        self._build_ui()
        self._remove_provider_btn.setEnabled(self._config.is_custom_provider(self._config.provider.name))

        # Subscribe to theme changes immediately so a user who
        # opens the dialog, leaves it open while changing the theme
        # in another panel, or switches themes via the settings
        # combo sees the dialog repaint against the new palette
        # without needing a close-and-reopen cycle.
        #
        # The previous revision wired the subscription inside
        # ``showEvent`` behind a ``_theme_signal_connected`` flag.
        # That left a window where the dialog was constructed but
        # not yet shown, during which a theme change would not
        # update the visible stylesheet (e.g. tests that build a
        # dialog but never show it).  Connecting here also matches
        # the new ``bind_theme`` pattern used by every other
        # themed widget so the disconnect path in ``done`` is
        # symmetric.
        try:
            ThemeManager.instance().themeChanged.connect(self._apply_theme_styles)
        except Exception:
            # Subscription is best-effort; if the manager has been
            # torn down already (rare test path), the dialog will
            # still work, just without live theme refresh.
            pass
        # Apply the current theme immediately so the dialog body
        # paints against the live palette even when constructed in
        # a hidden state (dockable forms, tests).
        self._apply_theme_styles()

        # Poll timer for fetcher results — NO cross-thread signals
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_fetcher)
        self._poll_timer.start(150)

        # Deferred init timer — parented to self, safe if dialog closes instantly.
        # We use a *non-zero* interval (200 ms) so the dialog can paint
        # before any heavy work (auth resolution, provider construction,
        # SDK imports) starts.  A zero-interval timer can fire on the
        # same event-loop tick that paints the dialog, defeating the
        # point of deferring.
        self._init_timer = QTimer(self)
        self._init_timer.setSingleShot(True)
        self._init_timer.setInterval(200)
        self._init_timer.timeout.connect(self._deferred_init)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        self._tabs = QTabWidget()

        # Tab 0: Provider (existing 3 group boxes + GLM group)
        provider_tab = QWidget()
        playout = QVBoxLayout(provider_tab)
        self._provider_group = self._build_provider_group()
        playout.addWidget(self._provider_group)
        self._generation_group = self._build_generation_group()
        playout.addWidget(self._generation_group)
        self._behavior_group = self._build_behavior_group()
        playout.addWidget(self._behavior_group)
        self._glm_group = self._build_glm_group()
        playout.addWidget(self._glm_group)
        playout.addStretch()
        self._tabs.addTab(provider_tab, "Provider")

        # Initialise GLM controls from config and set visibility.
        self._load_glm_controls_from_config()
        self._refresh_glm_controls()

        # Tab: Appearance
        appearance_tab = QWidget()
        appearance_layout = QVBoxLayout(appearance_tab)
        self._appearance_group = self._build_appearance_group()
        appearance_layout.addWidget(self._appearance_group)
        appearance_layout.addStretch()
        self._tabs.addTab(appearance_tab, "Appearance")

        # Tab 1-3: Skills, MCP, Profiles — LAZILY constructed on first tab
        # switch. The SettingsService (which scans Rikugan / external
        # skills and MCP configs) is heavy and previously ran synchronously
        # in __init__, blocking first paint.  We add lightweight
        # placeholders and build the real tabs on demand.
        self._service: Any = None
        self._skills_tab = None
        self._mcp_tab = None
        self._profiles_tab = None
        for tab_title, _attr_name in (
            ("Skills", "_skills_tab"),
            ("MCP", "_mcp_tab"),
            ("Profiles", "_profiles_tab"),
        ):
            placeholder = QWidget()
            ph_layout = QVBoxLayout(placeholder)
            ph_label = QLabel(f"{tab_title} not loaded.\nClick to load.")
            ph_label.setStyleSheet(get_hint_status_style())
            ph_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            ph_layout.addWidget(ph_label)
            self._tabs.addTab(placeholder, tab_title)
        # Connect tab change handler to lazy-load on first selection.
        self._tabs.currentChanged.connect(self._on_tab_changed_lazy)

        layout.addWidget(self._tabs)

        self._button_box = QDialogButtonBox(OK_CANCEL_BUTTONS)
        self._button_box.accepted.connect(self._on_accept)
        self._button_box.rejected.connect(self.reject)
        layout.addWidget(self._button_box)

        # Connect provider/key change signals AFTER everything is built
        self._provider_combo.currentTextChanged.connect(self._on_provider_changed)
        self._api_key_edit.editingFinished.connect(self._on_key_edited)
        # Updating the selected model should immediately refresh the
        # Max Output Tokens range / value and Context Window.  The slot
        # is idempotent (it just reads the current combo state) so the
        # programmatic ``setCurrentIndex`` calls inside
        # ``_on_models_ready`` are safe — they trigger a re-application
        # that is a no-op when the model matches the saved config.
        self._model_combo.currentIndexChanged.connect(lambda _idx: self._update_generation_defaults())

    def _on_tab_changed_lazy(self, index: int) -> None:
        """Lazy-construct Skills / MCP / Profiles tabs on first selection."""
        if index < 0 or index >= self._tabs.count():
            return
        title = self._tabs.tabText(index)
        widget = self._tabs.widget(index)
        if widget is None:
            return
        # If the widget at this index has been replaced with a real tab,
        # ``objectName`` will have been set on the real tab.  In that
        # case there is nothing to do.
        if widget.objectName() in {"skills_tab", "mcp_tab", "profiles_tab"}:
            return
        if title == "Skills":
            self._load_skills_tab()
        elif title == "MCP":
            self._load_mcp_tab()
        elif title == "Profiles":
            self._load_profiles_tab()

    def _ensure_service(self) -> Any:
        """Build the SettingsService on first use."""
        if self._service is None:
            from .settings_service import SettingsService

            self._service = SettingsService(self._config, tool_registry=self._tool_registry)
        return self._service

    def _load_skills_tab(self) -> None:
        if self._skills_tab is not None:
            return
        from .tabs.skills_tab import SkillsTab

        idx = self._index_of_tab("Skills")
        if idx < 0:
            return
        self._skills_tab = SkillsTab(self._config, service=self._ensure_service())
        self._skills_tab.setObjectName("skills_tab")
        self._tabs.removeTab(idx)
        self._tabs.insertTab(idx, self._skills_tab, "Skills")
        self._tabs.setCurrentIndex(idx)

    def _load_mcp_tab(self) -> None:
        if self._mcp_tab is not None:
            return
        from .tabs.mcp_tab import MCPTab

        idx = self._index_of_tab("MCP")
        if idx < 0:
            return
        self._mcp_tab = MCPTab(self._config, service=self._ensure_service())
        self._mcp_tab.setObjectName("mcp_tab")
        self._tabs.removeTab(idx)
        self._tabs.insertTab(idx, self._mcp_tab, "MCP")
        self._tabs.setCurrentIndex(idx)

    def _load_profiles_tab(self) -> None:
        if self._profiles_tab is not None:
            return
        from .tabs.profiles_tab import ProfilesTab

        idx = self._index_of_tab("Profiles")
        if idx < 0:
            return
        self._profiles_tab = ProfilesTab(self._config, service=self._ensure_service())
        self._profiles_tab.setObjectName("profiles_tab")
        self._tabs.removeTab(idx)
        self._tabs.insertTab(idx, self._profiles_tab, "Profiles")
        self._tabs.setCurrentIndex(idx)

    def _index_of_tab(self, title: str) -> int:
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i) == title:
                return i
        return -1

    def _build_provider_group(self) -> QGroupBox:
        """Build the LLM Provider settings group box."""
        provider_group = QGroupBox("LLM Provider")
        provider_form = QFormLayout(provider_group)

        provider_form.addRow("Provider:", self._build_provider_row())

        # API key — only show explicit user keys, NOT auto-resolved OAuth tokens
        key_layout = QHBoxLayout()
        self._api_key_edit = QLineEdit(self._config.provider.api_key)
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_edit.setPlaceholderText("sk-... or leave empty for auto-detect")
        key_layout.addWidget(self._api_key_edit, 1)
        self._auth_status = QLabel()
        key_layout.addWidget(self._auth_status)
        provider_form.addRow("API Key:", key_layout)

        # OAuth checkbox — controls keychain autoload
        self._oauth_cb = QCheckBox("Use OAuth from Claude Code (macOS Keychain)")
        self._oauth_cb.setChecked(self._config.oauth_consent_accepted)
        self._oauth_cb.setVisible(self._config.provider.name == "anthropic")
        self._oauth_cb.setToolTip(
            "Auto-load your Claude Code OAuth token from the macOS Keychain.\n"
            "Requires accepting Anthropic's credential use policy."
        )
        self._oauth_cb.toggled.connect(self._on_oauth_toggled)
        provider_form.addRow("", self._oauth_cb)

        self._api_base_edit = QLineEdit(self._config.provider.api_base)
        self._api_base_edit.setPlaceholderText("Custom endpoint URL (optional)")
        provider_form.addRow("API Base:", self._api_base_edit)

        provider_form.addRow("Model:", self._build_model_row())

        return provider_group

    def _build_provider_row(self) -> QHBoxLayout:
        """Build the provider combo + add/remove buttons row."""
        row = QHBoxLayout()
        self._provider_combo = QComboBox()
        self._populate_provider_combo()
        idx = self._provider_combo.findText(self._config.provider.name)
        if idx >= 0:
            self._provider_combo.setCurrentIndex(idx)
        row.addWidget(self._provider_combo, 1)

        self._add_provider_btn = QPushButton("+")
        self._add_provider_btn.setFixedSize(28, 28)
        self._add_provider_btn.setToolTip("Add custom OpenAI-compatible connection")
        self._add_provider_btn.setStyleSheet(get_settings_btn_style())
        self._add_provider_btn.clicked.connect(self._on_add_custom_provider)
        row.addWidget(self._add_provider_btn)

        self._remove_provider_btn = QPushButton("\u2212")  # minus sign
        self._remove_provider_btn.setFixedSize(28, 28)
        self._remove_provider_btn.setToolTip("Remove custom connection")
        self._remove_provider_btn.setStyleSheet(get_settings_btn_style())
        self._remove_provider_btn.clicked.connect(self._on_remove_custom_provider)
        row.addWidget(self._remove_provider_btn)

        return row  # connected AFTER group is built (in _build_ui)

    def _build_model_row(self) -> QHBoxLayout:
        """Build the model combo + refresh button + status row."""
        model_layout = QHBoxLayout()
        self._model_combo = QComboBox()
        self._model_combo.setEditable(True)
        self._model_combo.setMinimumWidth(300)
        self._model_combo.setCurrentText(self._config.provider.model)
        model_layout.addWidget(self._model_combo, 1)

        self._fetch_btn = QPushButton("Refresh")
        self._fetch_btn.setFixedWidth(70)
        self._fetch_btn.setStyleSheet(get_settings_btn_style())
        self._fetch_btn.clicked.connect(lambda: self._fetch_models(explicit=True))
        model_layout.addWidget(self._fetch_btn)

        self._model_status = QLabel()
        self._model_status.setStyleSheet(get_hint_status_style())
        self._model_status.setWordWrap(True)
        model_layout.addWidget(self._model_status)
        return model_layout

    def _build_generation_group(self) -> QGroupBox:
        """Build the Generation settings group box."""
        gen_group = QGroupBox("Generation")
        gen_form = QFormLayout(gen_group)

        self._temp_spin = QDoubleSpinBox()
        self._temp_spin.setRange(0.0, 2.0)
        self._temp_spin.setSingleStep(0.05)
        self._temp_spin.setDecimals(2)
        self._temp_spin.setValue(self._config.provider.temperature)
        gen_form.addRow("Temperature:", self._temp_spin)

        self._max_tokens_spin = QSpinBox()
        # Generous initial range. ``_update_generation_defaults()`` refines
        # the upper bound to the selected model's ``max_output_tokens`` once
        # the model combo is populated.  Lower bound of 1 matches the
        # provider/API minimum and ``RikuganConfig.validate()`` which only
        # requires positivity.
        self._max_tokens_spin.setRange(1, 2_000_000)
        self._max_tokens_spin.setSingleStep(1024)
        self._max_tokens_spin.setValue(self._config.provider.max_tokens)
        gen_form.addRow("Max Output Tokens:", self._max_tokens_spin)

        self._context_spin = QSpinBox()
        self._context_spin.setRange(4096, 2000000)
        self._context_spin.setSingleStep(10000)
        self._context_spin.setValue(self._config.provider.context_window)
        gen_form.addRow("Context Window:", self._context_spin)

        return gen_group

    def _build_behavior_group(self) -> QGroupBox:
        """Build the Behavior settings group box."""
        behavior_group = QGroupBox("Behavior")
        behavior_form = QFormLayout(behavior_group)

        self._auto_context_cb = QCheckBox("Auto-inject binary context into system prompt")
        self._auto_context_cb.setChecked(self._config.auto_context)
        behavior_form.addRow(self._auto_context_cb)

        self._auto_save_cb = QCheckBox("Auto-save sessions")
        self._auto_save_cb.setChecked(self._config.checkpoint_auto_save)
        behavior_form.addRow(self._auto_save_cb)

        self._explore_turns_spin = QSpinBox()
        self._explore_turns_spin.setRange(5, 200)
        self._explore_turns_spin.setValue(self._config.exploration_turn_limit)
        self._explore_turns_spin.setToolTip(
            "Maximum turns the agent spends in the exploration phase before "
            "forcing a transition (or reporting an error if findings are insufficient)."
        )
        behavior_form.addRow("Exploration turn limit:", self._explore_turns_spin)

        # --- Rate-limit handling ---
        self._max_retries_spin = QSpinBox()
        self._max_retries_spin.setRange(1, 10)
        self._max_retries_spin.setValue(self._config.max_retries)
        self._max_retries_spin.setToolTip(
            "Number of retry attempts when the API returns a rate-limit or transient error."
        )
        behavior_form.addRow("API retry attempts:", self._max_retries_spin)

        self._silent_retry_cb = QCheckBox("Show loading indicator instead of error messages during retries")
        self._silent_retry_cb.setChecked(self._config.silent_retry_mode)
        self._silent_retry_cb.setToolTip(
            "When enabled, rate-limit retries show a subtle text indicator instead of red error messages."
        )
        behavior_form.addRow(self._silent_retry_cb)

        # --- Context preservation ---
        self._preserve_context_cb = QCheckBox("Preserve full context (disable tool result truncation)")
        self._preserve_context_cb.setChecked(self._config.preserve_context)
        self._preserve_context_cb.setToolTip(
            "Disables tool result truncation and message trimming. "
            "Enable for deep RE sessions where losing decompilation context is worse than higher token cost."
        )
        behavior_form.addRow(self._preserve_context_cb)

        # --- Raw knowledge memory ---
        self._knowledge_enabled_cb = QCheckBox("Enable raw knowledge memory")
        self._knowledge_enabled_cb.setChecked(bool(getattr(self._config, "knowledge_enabled", True)))
        self._knowledge_enabled_cb.setToolTip(
            "When enabled, Luc Nhan writes structured analysis knowledge to\n"
            "``.rikugan-kb/`` (memories, entities, relations, observations) and\n"
            "report Markdown under ``notes/reports/`` next to the IDB. Disable\n"
            "to skip writes entirely and hide the Knowledge tab banner."
        )
        behavior_form.addRow(self._knowledge_enabled_cb)

        # --- API key encryption ---
        from ..core.crypto import is_available as crypto_available

        self._encrypt_keys_cb = QCheckBox("Encrypt API keys with password")
        self._encrypt_keys_cb.setChecked(self._config.encrypt_api_keys)
        self._encrypt_keys_cb.setEnabled(crypto_available())
        self._encrypt_keys_cb.setToolTip(
            "Encrypt all stored API keys with a password.\nYou must enter this password each time Luc Nhan starts."
            if crypto_available()
            else "Requires the 'cryptography' package (pip install cryptography)."
        )
        behavior_form.addRow(self._encrypt_keys_cb)

        # --- IDAPython docs-review gate (post-error) ---
        self._docs_review_mode_cb = QComboBox()
        self._docs_review_mode_cb.addItem("Review on runtime error (recommended)", "on_error")
        self._docs_review_mode_cb.addItem("Off (no docs review)", "off")
        current_mode = getattr(self._config, "docs_review_mode", "on_error")
        idx = self._docs_review_mode_cb.findData(current_mode)
        self._docs_review_mode_cb.setCurrentIndex(max(0, idx))
        self._docs_review_mode_cb.setToolTip(
            "Controls when the IDA docs-reviewer subagent runs for execute_python:\n"
            "• On runtime error: reviewer diagnoses only when a script fails with "
            "an API-shaped exception (AttributeError, ImportError, NameError). "
            "The reviewer auto-injects the relevant module reference so the agent "
            "can fix the script. This is faster than reviewing every complex script.\n"
            "• Off: no reviewer — you handle all script errors yourself."
        )
        behavior_form.addRow("IDA docs review mode:", self._docs_review_mode_cb)

        # --- IDA Output window verbosity ---
        # Controls which log records appear in IDA's Output window.
        # Routine INFO/DEBUG chatter is suppressed by default; file and
        # JSONL logs continue to receive everything (see
        # ``rikugan_debug.log``).
        from ..core.log_sinks import LOG_LEVEL_LABELS, LOG_LEVEL_VALUE_TO_LABEL

        self._ida_output_log_combo = QComboBox()
        self._ida_output_log_combo.setEditable(False)
        self._ida_output_log_combo.addItems(LOG_LEVEL_LABELS)
        current_label = LOG_LEVEL_VALUE_TO_LABEL.get(self._config.ida_output_log_level, "Warning")
        idx = self._ida_output_log_combo.findText(current_label)
        if idx >= 0:
            self._ida_output_log_combo.setCurrentIndex(idx)
        self._ida_output_log_combo.setToolTip(
            "Minimum severity shown in IDA's Output window.\n"
            "'Off' silences Luc Nhan entirely; full DEBUG output is always "
            "available in 'rikugan_debug.log' under the Luc Nhan config "
            "directory regardless of this setting."
        )
        behavior_form.addRow("IDA Output verbosity:", self._ida_output_log_combo)

        # --- Multi-tab parallel agent ---
        self._parallel_agent_cb = QCheckBox("Run agents in parallel across tabs")
        self._parallel_agent_cb.setChecked(self._config.parallel_agent_enabled)
        self._parallel_agent_cb.setToolTip(
            "When enabled, multiple chat tabs can run agents concurrently;\n"
            "switching tabs does not cancel a running agent. Disable for the\n"
            "legacy single-agent behavior (tab switch cancels)."
        )
        behavior_form.addRow(self._parallel_agent_cb)

        self._parallel_max_spin = QSpinBox()
        self._parallel_max_spin.setRange(1, 20)
        self._parallel_max_spin.setValue(self._config.parallel_agent_max_concurrent)
        self._parallel_max_spin.setToolTip(
            "Maximum number of agents that may run simultaneously across all tabs.\n"
            "Extra messages queue and start as slots free up."
        )
        behavior_form.addRow("Max concurrent agents:", self._parallel_max_spin)

        return behavior_group

    def _build_appearance_group(self) -> QGroupBox:
        """Build the Appearance settings group box."""
        appearance_group = QGroupBox("Font")
        appearance_form = QFormLayout(appearance_group)

        self._font_family_combo = QComboBox()
        self._font_family_combo.setEditable(False)
        self._font_family_combo.addItems(
            [
                "(Inherit from IDA)",
                "Consolas",
                "Courier New",
                "Lucida Console",
                "Monaco",
                "Source Code Pro",
                "Segoe UI",
            ]
        )
        current_family = self._config.font_family
        if current_family:
            idx = self._font_family_combo.findText(current_family)
            if idx >= 0:
                self._font_family_combo.setCurrentIndex(idx)
            else:
                self._font_family_combo.insertItem(1, current_family)
                self._font_family_combo.setCurrentIndex(1)
        self._font_family_combo.setToolTip(
            "Font family for chat messages and code blocks. "
            "Leave at 'Inherit from IDA' to use IDA Pro's configured font."
        )
        appearance_form.addRow("Font family:", self._font_family_combo)

        self._font_size_spin = QSpinBox()
        self._font_size_spin.setRange(0, 72)
        self._font_size_spin.setValue(self._config.font_size_override)
        self._font_size_spin.setSuffix(" pt")
        self._font_size_spin.setToolTip("Font size in points. Set to 0 or leave at default to inherit from IDA Pro.")
        appearance_form.addRow("Font size:", self._font_size_spin)

        # Theme selector — wired to ThemeManager. The combo has 4
        # entries (auto / dark / light / ida) and updates the manager
        # in real time so the user can preview the change before
        # closing the dialog.
        self._theme_combo = QComboBox()
        from .theme.tokens import ThemeMode

        for label, mode in (
            ("Auto (follow host)", ThemeMode.AUTO),
            ("Dark", ThemeMode.DARK),
            ("Light", ThemeMode.LIGHT),
            ("IDA native", ThemeMode.IDA_NATIVE),
        ):
            self._theme_combo.addItem(label, mode.value)
        # Reflect current config — read from ``config.theme`` (not the
        # legacy ``theme_mode`` field, which never existed in the
        # RikuganConfig dataclass).
        current = getattr(self._config, "theme", ThemeMode.AUTO.value) or ThemeMode.AUTO.value
        for i in range(self._theme_combo.count()):
            if self._theme_combo.itemData(i) == current:
                self._theme_combo.setCurrentIndex(i)
                break
        self._theme_combo.setToolTip(
            "Select the colour theme. Auto derives from the host palette; "
            "Dark/Light use Luc Nhan's bundled palettes; IDA native follows "
            "IDA's current Qt palette (no effect when not running in IDA)."
        )
        self._theme_combo.currentIndexChanged.connect(lambda _idx: self._on_theme_changed())
        appearance_form.addRow("Theme:", self._theme_combo)

        return appearance_group

    # --- GLM Settings (Task 13) -------------------------------------------

    def _build_glm_group(self) -> QGroupBox:
        """Build the GLM reasoning resilience controls group.

        Visible only when the active provider has
        ``extra["dialect"] == "glm"``.  Exposes product-level controls
        only (thinking on/off/effort, preserve, guard enable/ceiling/
        recovery).  Repetition/window/meta thresholds are not exposed.
        """
        glm_group = QGroupBox("GLM Reasoning Resilience")
        glm_form = QFormLayout(glm_group)

        # Thinking mode: Adaptive (enabled) or Disabled
        self._glm_thinking_combo = QComboBox()
        self._glm_thinking_combo.addItem("Adaptive", True)
        self._glm_thinking_combo.addItem("Disabled", False)
        self._glm_thinking_combo.setToolTip(
            "Adaptive: GLM reasoning/thinking is enabled for requests.\nDisabled: thinking is turned off entirely."
        )
        glm_form.addRow("Thinking:", self._glm_thinking_combo)

        # Reasoning effort — only meaningful for GLM-5.2, but we always
        # show the combo and disable it for models that don't advertise
        # ``reasoning_effort`` support.
        self._glm_effort_combo = QComboBox()
        for effort in sorted(REASONING_EFFORT_VALUES):
            self._glm_effort_combo.addItem(effort, effort)
        self._glm_effort_combo.setToolTip(
            "Reasoning effort sent to the GLM endpoint.\n"
            "GLM-5.2 supports: max, xhigh, high, medium, low, minimal, none.\n"
            "Other GLM models use the default 'max' and cannot change it."
        )
        glm_form.addRow("Reasoning effort:", self._glm_effort_combo)

        # Preserve thinking context across turns
        self._glm_preserve_cb = QCheckBox("Preserve reasoning context across turns")
        self._glm_preserve_cb.setToolTip(
            "When checked, prior reasoning_content is replayed in\nmulti-turn conversations so GLM maintains context."
        )
        glm_form.addRow(self._glm_preserve_cb)

        # Degeneration guard
        self._glm_guard_cb = QCheckBox("Enable degeneration guard")
        self._glm_guard_cb.setToolTip(
            "When checked, detects reasoning loops and triggers\n"
            "one-shot recovery (retry without thinking, then with\n"
            "a capped recovery budget)."
        )
        glm_form.addRow(self._glm_guard_cb)

        # Reasoning token ceiling
        self._glm_ceiling_spin = QSpinBox()
        self._glm_ceiling_spin.setRange(REASONING_TOKEN_CEILING_MIN, REASONING_TOKEN_CEILING_MAX)
        self._glm_ceiling_spin.setValue(REASONING_TOKEN_CEILING_DEFAULT)
        self._glm_ceiling_spin.setSuffix(" tokens")
        self._glm_ceiling_spin.setToolTip(
            "Estimated reasoning-token hard limit. Aborts a turn before\nit consumes the full max_tokens budget."
        )
        glm_form.addRow("Reasoning token ceiling:", self._glm_ceiling_spin)

        # Recovery max tokens
        self._glm_recovery_spin = QSpinBox()
        self._glm_recovery_spin.setRange(RECOVERY_MAX_TOKENS_MIN, RECOVERY_MAX_TOKENS_MAX)
        self._glm_recovery_spin.setValue(RECOVERY_MAX_TOKENS_DEFAULT)
        self._glm_recovery_spin.setSuffix(" tokens")
        self._glm_recovery_spin.setToolTip(
            "Total output-token cap for the one-shot recovery request.\n"
            "Clamped to the selected model's max output tokens at dispatch."
        )
        glm_form.addRow("Recovery token cap:", self._glm_recovery_spin)

        # Endpoint type: Standard API vs Coding Plan.
        # The two Z.AI endpoints use non-interchangeable API keys and
        # different base URLs, so this must match the key the user pastes.
        self._glm_endpoint_combo = QComboBox()
        self._glm_endpoint_combo.addItem("Standard API", "standard")
        self._glm_endpoint_combo.addItem("Coding Plan", "coding_plan")
        self._glm_endpoint_combo.setToolTip(
            "Standard API: general-purpose Z.AI endpoint (per-token billing).\n"
            "Coding Plan: Z.AI Coding Plan subscription ($18/mo+); requires a\n"
            "Plan API key, which is NOT interchangeable with a standard key.\n"
            "Changing this updates the API Base URL automatically."
        )
        self._glm_endpoint_combo.currentIndexChanged.connect(self._on_glm_endpoint_changed)
        glm_form.addRow("Endpoint:", self._glm_endpoint_combo)

        # Hidden by default until a GLM-dialect provider is active.
        glm_group.setVisible(False)
        return glm_group

    def _load_glm_controls_from_config(self) -> None:
        """Populate GLM UI controls from ``config.provider.extra``.

        Called on initial build and whenever the provider changes.  If
        the extra dict lacks GLM keys (or is not a GLM dialect), the
        controls are left at their defaults.
        """
        extra = self._config.provider.extra
        if not isinstance(extra, dict) or extra.get("dialect") != GLM_DIALECT:
            return

        thinking = extra.get("thinking") or {}
        if thinking.get("enabled", True):
            self._glm_thinking_combo.setCurrentIndex(0)  # Adaptive
        else:
            self._glm_thinking_combo.setCurrentIndex(1)  # Disabled

        effort = thinking.get("reasoning_effort", "max")
        idx = self._glm_effort_combo.findData(effort)
        if idx >= 0:
            self._glm_effort_combo.setCurrentIndex(idx)

        self._glm_preserve_cb.setChecked(thinking.get("preserve", True))

        guard = extra.get("degeneration_guard") or {}
        self._glm_guard_cb.setChecked(guard.get("enabled", True))
        self._glm_ceiling_spin.setValue(guard.get("reasoning_token_ceiling", REASONING_TOKEN_CEILING_DEFAULT))
        self._glm_recovery_spin.setValue(guard.get("recovery_max_tokens", RECOVERY_MAX_TOKENS_DEFAULT))

        # Endpoint type (Standard vs Coding Plan).  Default to standard.
        endpoint_type = extra.get("endpoint_type", "standard")
        endpoint_idx = self._glm_endpoint_combo.findData(endpoint_type)
        if endpoint_idx >= 0:
            self._glm_endpoint_combo.blockSignals(True)
            self._glm_endpoint_combo.setCurrentIndex(endpoint_idx)
            self._glm_endpoint_combo.blockSignals(False)

        # Clamp recovery spin to the selected model's max_output_tokens.
        self._clamp_glm_recovery_to_model()

    def _sync_glm_controls_to_config(self) -> None:
        """Write GLM UI control values back into ``config.provider.extra``.

        Called from ``_sync_config_from_ui`` and ``_on_accept``.  Only
        fires when the active provider has ``dialect == "glm"``.
        """
        extra = self._config.provider.extra
        if not isinstance(extra, dict) or extra.get("dialect") != GLM_DIALECT:
            return

        # Read thinking combo (index 0 = Adaptive/enabled, 1 = Disabled)
        thinking_enabled = self._glm_thinking_combo.currentData()
        if thinking_enabled is None:
            thinking_enabled = True

        effort = self._glm_effort_combo.currentData() or "max"

        preserve = self._glm_preserve_cb.isChecked()
        guard_enabled = self._glm_guard_cb.isChecked()
        ceiling = self._glm_ceiling_spin.value()
        recovery = self._glm_recovery_spin.value()

        # Build a fresh extra dict with the exact GLM schema.  We keep
        # the dialect key and replace thinking/guard sub-dicts entirely
        # so stale keys from a previous config version cannot survive.
        endpoint_type = self._glm_endpoint_combo.currentData() or "standard"
        self._config.provider.extra = {
            "dialect": GLM_DIALECT,
            "endpoint_type": str(endpoint_type),
            "thinking": {
                "enabled": bool(thinking_enabled),
                "reasoning_effort": str(effort),
                "preserve": bool(preserve),
            },
            "degeneration_guard": {
                "enabled": bool(guard_enabled),
                "reasoning_token_ceiling": int(ceiling),
                "retry_without_thinking": True,
                "recovery_max_tokens": int(recovery),
            },
        }

    def _on_glm_endpoint_changed(self, _index: int | None = None) -> None:
        """Update API base URL when the GLM endpoint type changes.

        The two Z.AI endpoints (standard vs coding_plan) use distinct base
        URLs and non-interchangeable API keys.  When the user switches the
        endpoint combo, we update both the UI field and the live config so
        the next provider instance builds against the correct URL.  If the
        user has set a custom non-Z.AI base URL, we do not clobber it.
        """
        endpoint_type = self._glm_endpoint_combo.currentData()
        from .qt_compat import QMessageBox

        base_url = _PROVIDER_BASES_GLM.get(endpoint_type)
        if base_url is None:
            return
        current_base = self._api_base_edit.text().strip()
        # Only update if the current base is empty or matches one of the
        # known Z.AI endpoints — don't clobber a custom endpoint.
        known_zai = set(_PROVIDER_BASES_GLM.values())
        if current_base in known_zai or not current_base:
            self._api_base_edit.setText(base_url)
            self._config.provider.api_base = base_url
            # Clear the key field — the endpoint change means the old key
            # is almost certainly wrong for the new endpoint.
            if self._api_key_edit.text().strip():
                reply = QMessageBox.question(
                    self,
                    "Clear API Key?",
                    "Switching GLM endpoint type requires a different API key.\n\nClear the current key?",
                    YES_NO_BUTTONS,
                    QMessageBox.StandardButton.Yes,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    self._api_key_edit.clear()

    def _refresh_glm_controls(self) -> None:
        """Show/hide the GLM group based on the active dialect.

        Called on initial build, provider changes, and model changes.
        """
        extra = self._config.provider.extra
        is_glm = isinstance(extra, dict) and extra.get("dialect") == GLM_DIALECT
        self._glm_group.setVisible(is_glm)
        if is_glm:
            # Update effort combo enabled state based on model metadata.
            model_id = self._config.provider.model
            metadata = get_glm_model_metadata(model_id)
            self._glm_effort_combo.setEnabled(metadata.reasoning_effort)
            self._clamp_glm_recovery_to_model()

    def _clamp_glm_recovery_to_model(self) -> None:
        """Clamp the recovery spin box maximum to the selected model's
        ``max_output_tokens`` (if known).  Falls back to the parser's
        hard limit when model metadata is unavailable."""
        model_id = self._config.provider.model
        metadata = get_glm_model_metadata(model_id)
        # The recovery cap cannot exceed the model's max output tokens.
        effective_max = min(metadata.max_output_tokens, RECOVERY_MAX_TOKENS_MAX)
        self._glm_recovery_spin.setMaximum(effective_max)
        if self._glm_recovery_spin.value() > effective_max:
            self._glm_recovery_spin.setValue(effective_max)

    def _maybe_prompt_zai_migration(self) -> None:
        """One-time explicit migration for ``api.z.ai`` custom providers.

        Fires only when ALL of:
        - hostname of ``api_base`` is exactly ``api.z.ai``
        - provider is a custom (non-builtin) connection
        - no dialect is already saved in ``provider.extra``
        - migration has not been previously prompted for this provider

        Accept sets ``extra.dialect = "glm"`` and re-registers the
        provider's dialect in the registry.  Decline (or dismiss) leaves
        ``extra`` untouched so the provider continues as
        OpenAI-compatible.

        The ``glm_migration_prompted`` marker is durable: it survives
        Cancel by being written to both the live config and the
        construction-time snapshot.  The dialect change itself follows
        normal Cancel semantics (reverted by
        ``_restore_config_from_snapshot``).
        """
        provider_name = self._config.provider.name
        # Only for custom (non-builtin) providers.
        if not self._config.is_custom_provider(provider_name):
            return
        # Already has a dialect — don't prompt.
        extra = self._config.provider.extra
        if isinstance(extra, dict) and extra.get("dialect"):
            return
        # Already prompted — don't re-prompt.
        cp = self._config.custom_providers.get(provider_name, {})
        if cp.get("glm_migration_prompted"):
            return
        # Check hostname.
        api_base = self._config.provider.api_base or ""
        try:
            hostname = urlparse(api_base).hostname
        except Exception:
            hostname = ""
        if hostname != _ZAI_HOSTNAME:
            return

        # Prompt the user.
        accepted = _prompt_zai_migration(parent=self)

        # Record the durable marker regardless of accept/decline.
        # Write to BOTH the live config and the construction snapshot so
        # the marker survives Cancel (_restore_config_from_snapshot
        # replaces custom_providers from the snapshot).  The dialect
        # change (if accepted) follows normal Cancel semantics and will
        # be reverted by the snapshot restore.
        self._config.custom_providers.setdefault(provider_name, {})
        self._config.custom_providers[provider_name]["glm_migration_prompted"] = True
        # Mirror to the snapshot so Cancel does not undo the marker.
        snap_cp = self._config_snapshot.custom_providers.get(provider_name)
        if snap_cp is None:
            self._config_snapshot.custom_providers[provider_name] = {"glm_migration_prompted": True}
        else:
            snap_cp["glm_migration_prompted"] = True

        if accepted:
            # Set the GLM dialect on the active provider.
            new_extra = dict(extra) if isinstance(extra, dict) else {}
            new_extra["dialect"] = GLM_DIALECT
            self._config.provider.extra = new_extra

            # Re-register provider dialects in the registry so the GLM
            # adapter is used for this custom connection.
            dialects = {}
            for name, _cp_data in self._config.custom_providers.items():
                saved = self._config.providers.get(name, {})
                saved_extra = saved.get("extra", {})
                if isinstance(saved_extra, dict) and saved_extra.get("dialect") == GLM_DIALECT:
                    dialects[name] = GLM_DIALECT
                elif name == provider_name:
                    dialects[name] = GLM_DIALECT
            self._registry.register_custom_providers(
                list(self._config.custom_providers.keys()),
                dialects=dialects,
            )

            # Load GLM controls from the newly-set config and refresh.
            self._load_glm_controls_from_config()
            self._refresh_glm_controls()

    def _on_theme_changed(self) -> None:
        """Apply the selected theme to the live ThemeManager and persist it."""
        try:
            from .theme.manager import ThemeManager
            from .theme.tokens import ThemeMode

            data = self._theme_combo.currentData() if hasattr(self, "_theme_combo") else None
            if not data:
                return
            try:
                mode = ThemeMode(data)
            except ValueError:
                return
            ThemeManager.instance().set_mode(mode)
            # Sync the legacy ``styles._current_theme`` helper so
            # ``is_host_theme()`` and ``is_dark_theme()`` — read by the
            # theme-aware style getters in ``rikugan.ui.styles`` —
            # also reflect the new selection.  Without this the new
            # ``"auto"`` and ``"ida"`` modes would leave the legacy
            # selectors stale (still claiming ``light``), so inline
            # styles built from the helper palette would not update.
            # ``"ida"`` is mapped to the legacy ``"ida"`` value so
            # ``is_host_theme()`` returns True; ``"auto"`` is mapped
            # to ``"ida"`` too because the legacy helpers do not
            # distinguish the two and the *effective* palette is
            # decided by the live QApplication.
            from .styles import set_current_theme

            legacy_value = "ida" if mode in (ThemeMode.IDA_NATIVE, ThemeMode.AUTO) else mode.value
            set_current_theme(legacy_value)
            # Persist on ``config.theme`` — the canonical RikuganConfig
            # field.  ``theme_mode`` was never declared and writing it
            # would have silently vanished on the next save.
            self._config.theme = mode.value
        except Exception as e:  # settings are best-effort
            log_debug(f"SettingsDialog theme change error: {e}")

    def _apply_theme_styles(self) -> None:
        """Re-apply the QSS for the currently selected theme.

        The stylesheet is generated from the live ``ThemeTokens`` via
        :func:`build_settings_dialog_stylesheet`.  It targets only
        widgets under ``#rikugan_settings`` (the dialog's object name)
        so it never bleeds into the host application.  Calling it
        after the user picks a new theme in the combo box, or after
        ``ThemeManager.themeChanged`` fires, refreshes every label /
        line edit / spin box in the dialog with the new palette — so
        Rikugan Light never shows the previous dark QSS as a
        half-rendered black background.
        """
        try:
            tokens = ThemeManager.instance().tokens()
        except Exception as e:
            # ThemeManager failing (transient IPC, corrupt cache) should
            # never leave the dialog unstyled and silent. Log so operators
            # have visibility; the previously-applied stylesheet remains,
            # so the dialog stays visible with the last known palette.
            log_error(f"SettingsDialog._apply_theme_styles: tokens() failed: {e}")
            return
        try:
            self.setStyleSheet(build_settings_dialog_stylesheet(tokens))
        except Exception as e:
            log_debug(f"SettingsDialog._apply_theme_styles error: {e}")

    # --- Show event: defer all non-widget work to here ---

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not getattr(self, "_shown", False):
            self._shown = True
            # Defer auth resolution and model fetch to AFTER the dialog is painted.
            # This avoids subprocess.run() and background threads during construction.
            self._init_timer.start()
        # Re-apply the QSS for the current theme on every show so a
        # light/dark switch performed while the dialog was hidden
        # becomes visible immediately, and so the dialog is
        # theme-consistent after restoration from a saved layout.
        # The theme subscription itself is wired in ``__init__`` so
        # it is in place even when the dialog is constructed but
        # never shown (tests, dockable forms).
        self._apply_theme_styles()

    def _deferred_init(self) -> None:
        """Runs after the dialog is fully painted. Safe for subprocesses/threads.

        We do NOT auto-fetch live models on first open — instead we
        populate the model combo with the provider's built-in / cached
        model list so the dialog is immediately usable.  Live network
        fetches are triggered ONLY by the explicit Refresh button;
        switching provider or editing the API key no longer triggers
        a live fetch (see ``_on_provider_changed`` / ``_on_key_edited``).
        """
        if self._closed:
            return
        try:
            self._update_auth_status()
            self._model_restore_hint = self._config.provider.model.strip()
            self._populate_builtin_models()
        except Exception as e:
            log_error(f"SettingsDialog deferred init error: {e}")

    def _populate_builtin_models(self) -> None:
        """Populate the model combo with the built-in / current model list.

        This is called from the deferred-init timer (after the dialog
        is painted), from ``_on_provider_changed``, and from
        ``_on_key_edited``.  It does NOT trigger a network call and
        does NOT pre-import provider SDKs.

        For providers with a safe static ``_builtin_models()`` list
        (anthropic / openai / gemini / minimax) we use that list.  For
        providers whose ``_builtin_models`` is inherited from
        OpenAIProvider (ollama / openai_compat / custom connections),
        the static list contains OpenAI-only entries like "gpt-4o"
        that have nothing to do with the user's actual model.  In that
        case we only show the preserved manual model — or nothing
        at all, leaving the user's typed text in the editable combo.

        The "preserved manual model" is the first non-empty value of:
        ``_model_restore_hint`` (captured by the key-edit / provider-
        switch handlers before this method runs), the saved
        ``config.provider.model``, or whatever the user has typed into
        the editable combo.  Preserving the typed text on every refresh
        prevents the key-edit handler from clobbering a freshly typed
        model for a brand-new provider with no saved model.
        """
        provider_name = self._provider_combo.currentText()
        current_model = (self._config.provider.model or "").strip()
        restore_model = (self._model_restore_hint or "").strip()
        typed_model = self._model_combo.currentText().strip()
        manual_model = restore_model or current_model or typed_model
        models: list = []
        try:
            if self._is_local_compat_provider(provider_name):
                # Don't fall back to OpenAI's static list — that would
                # silently replace the user's Ollama / custom model
                # with "gpt-4o".  And don't synthesize a bare ``ModelInfo``
                # here either: a ``ModelInfo`` constructed without
                # ``max_output_tokens`` would default the dataclass
                # field to ``4096``, and ``_update_generation_defaults``
                # would then clamp the spin box to that bogus 4096
                # limit.  Treat the manual model as unknown metadata —
                # let ``_update_generation_defaults`` use the
                # ``_MANUAL_MAX_TOKENS`` fallback instead.
                self._set_manual_model_text(manual_model)
                self._fetched_models = []
                self._update_generation_defaults()
                self._model_status.setText("Click Refresh to fetch live models.")
                self._model_status.setStyleSheet(get_hint_status_style())
                self._model_restore_hint = ""
                return
            else:
                provider = self._registry.new_instance(
                    provider_name,
                    api_key=self._api_key_edit.text().strip(),
                    api_base=self._api_base_edit.text().strip(),
                )
                models = provider._builtin_models()
        except Exception as e:
            log_debug(f"Could not load built-in models: {e}")
            models = []
        if models:
            # During the first-paint built-in pass, never replace the
            # typed combo text with an unrelated first model.  Pass
            # ``preserve_unmatched=True`` so the user keeps their
            # current entry even if it isn't in the populated list.
            # ``_on_models_ready`` clears ``_model_restore_hint`` on
            # its way out, so we do not need to clear it here.
            self._on_models_ready(models, preserve_unmatched=True)
        else:
            # No static built-ins (e.g. a fresh OpenAI-compatible /
            # custom connection with no saved model).  Preserve the
            # manual model — falling back to ``""`` would silently
            # discard whatever the user typed before pressing the API
            # key.  ``_set_manual_model_text`` also forces
            # ``currentIndex`` to ``-1`` so the previously-selected
            # item's ``itemData`` cannot leak into the save path.
            self._set_manual_model_text(manual_model)
            self._model_status.setText("Click Refresh to fetch live models.")
            self._model_status.setStyleSheet(get_hint_status_style())
            # The hint has been (or would have been) consumed by this
            # pass; clear it so it cannot influence a later live fetch.
            self._model_restore_hint = ""

    def _is_local_compat_provider(self, provider_name: str) -> bool:
        """True for ollama, openai_compat, and user-added custom connections.

        These providers' static ``_builtin_models()`` lists come from
        the OpenAI base class and have nothing to do with the user's
        actual model — using them in the initial pop would silently
        overwrite the configured model.
        """
        if provider_name in ("ollama", "openai_compat"):
            return True
        # Custom connections registered via the registry.
        return self._registry._is_compat_name(provider_name) and provider_name != "openai_compat"

    # --- Cleanup ---

    def done(self, result: int) -> None:
        self._closed = True
        try:
            self._init_timer.stop()
            self._poll_timer.stop()
        except RuntimeError as e:
            log_debug(f"SettingsDialog.done timer cleanup: {e}")
        self._fetcher.shutdown()
        # Detach the theme subscription so the singleton doesn't
        # keep a dangling reference to a closed dialog.  The
        # ``disconnect_theme`` helper swallows the broad set of
        # disconnect-time errors PySide6 can raise during teardown.
        disconnect_theme(self)
        # Cancel: the user discarded the dialog, so roll the live config
        # back to its pre-dialog state. Without this the eager UI→config
        # syncs (provider switch, key edits) would persist despite Cancel.
        if result == QDialog.DialogCode.Rejected:
            self._restore_config_from_snapshot()
        super().done(result)

    def _restore_config_from_snapshot(self) -> None:
        """Restore the live config to the snapshot taken at construction.

        Copies field-by-field rather than swapping the object reference
        so callers that hold ``config`` (the panel, the agent runner)
        see the reverted values without us having to propagate a new
        object up the call stack.
        """
        snap = self._config_snapshot
        self._config.provider.name = snap.provider.name
        self._config.provider.api_key = snap.provider.api_key
        self._config.provider.api_base = snap.provider.api_base
        self._config.provider.model = snap.provider.model
        self._config.provider.temperature = snap.provider.temperature
        self._config.provider.max_tokens = snap.provider.max_tokens
        self._config.provider.context_window = snap.provider.context_window
        # Restore extra (GLM dialect settings) via deep-copy so the
        # restored dict cannot alias the snapshot's nested structures.
        self._config.provider.extra = copy.deepcopy(snap.provider.extra)
        self._config.providers = copy.deepcopy(snap.providers)
        self._config.custom_providers = copy.deepcopy(snap.custom_providers)
        self._config.active_profile = snap.active_profile
        self._config.theme = snap.theme
        self._config.disabled_skills = list(snap.disabled_skills)
        self._config.enabled_external_skills = list(snap.enabled_external_skills)
        self._config.enabled_external_mcp = list(snap.enabled_external_mcp)

    # --- Fetcher polling (main thread only, no cross-thread signals) ---

    def _poll_fetcher(self) -> None:
        """Poll the fetcher queue from the main thread. Safe for Shiboken."""
        if self._closed:
            return
        result = self._fetcher.poll()
        if result is None:
            return
        try:
            kind, provider_name, data = result
            # Ignore stale results from previous provider selections.
            if provider_name != self._provider_combo.currentText():
                return
            if kind == "models":
                self._on_models_ready(data)
            elif kind == "error":
                self._on_fetch_error(data)
        except (ValueError, TypeError) as e:
            log_debug(f"Malformed fetcher result: {e}")

    # --- Provider switching ---

    def _on_provider_changed(self, provider: str) -> None:
        # Persist edits from the previous provider before switching.
        # Skip sync if switch_provider was already called externally (e.g. _on_add_custom_provider)
        # to avoid corrupting the new provider's config with stale UI values.
        if self._config.provider.name != provider:
            self._sync_config_from_ui()

        # Use config.switch_provider() to snapshot current & restore saved
        self._config.switch_provider(provider)

        # Enable remove button only for custom providers
        is_custom = self._config.is_custom_provider(provider)
        self._remove_provider_btn.setEnabled(is_custom)

        # Update UI fields from the (possibly restored) config
        self._api_key_edit.setText(self._config.provider.api_key)
        self._api_base_edit.setText(self._config.provider.api_base)
        self._set_manual_model_text(self._config.provider.model)
        self._temp_spin.setValue(self._config.provider.temperature)
        self._max_tokens_spin.setValue(self._config.provider.max_tokens)
        self._context_spin.setValue(self._config.provider.context_window)
        self._model_restore_hint = self._config.provider.model.strip()

        # Auto-fill API base for providers that need it
        if provider == "ollama" and not self._api_base_edit.text().strip():
            self._api_base_edit.setText(_PROVIDER_BASES["ollama"])

        # Built-in GLM: auto-set dialect + Z.AI base URL + default model so
        # GLM controls appear and the provider is immediately usable without
        # the user having to know the dialect/base/model details.
        # Default endpoint is "standard"; user can switch to "coding_plan"
        # in the GLM controls group, which updates the base URL.
        if provider == "glm":
            if not self._config.provider.extra.get("dialect"):
                self._config.provider.extra = {
                    "dialect": GLM_DIALECT,
                    "endpoint_type": GLM_ENDPOINT_STANDARD,
                }
            if not self._api_base_edit.text().strip():
                self._api_base_edit.setText(_ZAI_API_BASE)
                self._config.provider.api_base = _ZAI_API_BASE
            if not self._config.provider.model.strip():
                self._set_manual_model_text(GLM_DEFAULT_MODEL)
                self._config.provider.model = GLM_DEFAULT_MODEL

        # OAuth checkbox only visible for Anthropic
        self._oauth_cb.setVisible(provider == "anthropic")

        # Update placeholder
        if provider == "anthropic":
            self._api_key_edit.setPlaceholderText("sk-... or leave empty for OAuth auto-detect")
        elif provider == "ollama":
            self._api_key_edit.setPlaceholderText("Not required for local Ollama")
        elif provider in ("openai_compat",) or is_custom:
            self._api_key_edit.setPlaceholderText("API key for the endpoint")
        else:
            self._api_key_edit.setPlaceholderText("API key")

        self._update_auth_status()
        # Refreshing the local built-in list is cheap and safe; do
        # NOT kick off a live network fetch here.  Live fetches are
        # triggered only by the explicit Refresh button.
        self._populate_builtin_models()
        self._model_status.setText("Click Refresh to fetch live models.")
        self._model_status.setStyleSheet(get_hint_status_style())

        # Load GLM controls from the (possibly restored) config and
        # refresh visibility based on the new provider's dialect.
        self._load_glm_controls_from_config()
        self._refresh_glm_controls()
        # Check for one-time Z.AI migration prompt.
        self._maybe_prompt_zai_migration()

    def _on_key_edited(self) -> None:
        # Capture the user's currently-selected / typed model BEFORE we
        # refresh the built-in list, so the refresh can preserve a
        # manually typed model for fresh providers with no saved model.
        self._model_restore_hint = self._get_selected_model_id()
        self._update_auth_status()
        # Edit finished on the API key — refresh the local built-in
        # model list.  This is a local-only refresh; it does NOT trigger
        # a live network fetch.  Live fetches are only triggered by the
        # explicit Refresh button.
        self._populate_builtin_models()
        self._model_status.setText("Click Refresh to fetch live models.")
        self._model_status.setStyleSheet(get_hint_status_style())

    def _on_oauth_toggled(self, checked: bool) -> None:
        """Handle the OAuth checkbox toggle."""
        if checked and not self._config.oauth_consent_accepted:
            from .oauth_consent import show_oauth_consent

            choice = show_oauth_consent(parent=self)
            if choice != "accept":
                # User declined — uncheck without recursion
                self._oauth_cb.blockSignals(True)
                self._oauth_cb.setChecked(False)
                self._oauth_cb.blockSignals(False)
                return
        # Update consent and refresh auth status
        from ..providers.auth_cache import invalidate_cache, set_keychain_consent

        set_keychain_consent(checked)
        invalidate_cache()
        self._update_auth_status()

    # --- Auth status ---

    def _update_auth_status(self) -> None:
        provider_name = self._provider_combo.currentText()
        explicit_key = self._api_key_edit.text().strip()
        base = self._api_base_edit.text().strip()

        try:
            provider = self._registry.new_instance(provider_name, api_key=explicit_key, api_base=base)
            label, status_type = provider.auth_status()
            self._resolved_token = provider.api_key
        except Exception as e:
            log_debug(f"Auth status check failed for {provider_name}: {e}")
            label, status_type = "", "none"
            self._resolved_token = ""

        if status_type == "ok":
            self._auth_status.setText(label)
            self._auth_status.setStyleSheet(get_ok_status_style())
        elif status_type == "error":
            if provider_name == "anthropic":
                self._auth_status.setText("run claude setup-token to acquire your oauth")
                self._auth_status.setStyleSheet(get_hint_status_style())
            else:
                self._auth_status.setText(label)
                self._auth_status.setStyleSheet(get_err_status_style())
        else:
            self._auth_status.setText("")
            self._auth_status.setStyleSheet("")

    # --- Model fetching ---

    def _fetch_models(self, explicit: bool = False) -> None:
        """Refresh the model list for the current provider.

        ``explicit=True`` is used when the user clicks the Refresh button.
        This is now the ONLY live-fetch trigger — provider / key change
        handlers no longer auto-fetch.

        The fetcher always runs ``ensure_ready`` on the main thread
        before launching the worker, so SDK imports never happen on a
        background thread (Python 3.14 + C-extension UAF).
        """
        provider = self._provider_combo.currentText()
        key = self._api_key_edit.text().strip()
        base = self._api_base_edit.text().strip()

        # For providers with auto-detect auth, use resolved token if no explicit key
        if not key and self._resolved_token:
            key = self._resolved_token

        self._model_status.setText("Fetching..." if explicit else "Refreshing...")
        self._fetch_btn.setEnabled(False)
        self._fetcher.fetch(provider, key, base)

    def _on_models_ready(self, models: list, preserve_unmatched: bool = False) -> None:
        self._fetch_btn.setEnabled(True)
        self._fetched_models = models

        preferred_id = (self._model_restore_hint or "").strip()
        current_id = preferred_id or self._get_selected_model_id()
        previous_text = self._model_combo.currentText().strip()
        self._model_combo.clear()
        for m in models:
            label = f"{m.name}  ({m.id})" if m.name != m.id else m.id
            self._model_combo.addItem(label, m.id)

        # Restore previous selection by model ID
        matched = False
        for i in range(self._model_combo.count()):
            if self._model_combo.itemData(i) == current_id:
                self._model_combo.setCurrentIndex(i)
                matched = True
                break
        if not matched and models and not preserve_unmatched:
            # Live fetch result — the fetched list is authoritative,
            # so fall back to the first item if the current model is
            # not in the list (e.g. provider rotated model lineup).
            self._model_combo.setCurrentIndex(0)
        elif not matched and preserve_unmatched:
            # Initial / built-in population — keep the user's typed
            # model as editable text so we never silently overwrite
            # "llama3.1" with "gpt-4o" just because the combo is
            # populated from an unrelated static list.
            #
            # We also insert/select a *custom* combo item whose
            # ``itemData`` equals the preserved model id.  An editable
            # ``QComboBox`` can otherwise keep ``currentIndex() == 0``
            # while visually displaying the typed text, and
            # ``_get_selected_model_id()`` prefers ``itemData(idx)``
            # whenever the index is valid — that mismatch used to cause
            # the dialog to display "llama3.1" but save "gpt-4o".
            preserved_id = (current_id or previous_text or "").strip()
            if preserved_id:
                self._model_combo.addItem(preserved_id, preserved_id)
                self._model_combo.setCurrentIndex(self._model_combo.count() - 1)
            else:
                self._model_combo.setCurrentText("")
        self._model_restore_hint = ""

        if models:
            self._model_status.setText(f"{len(models)} models")
            self._model_status.setStyleSheet(get_ok_status_style())
        else:
            self._model_status.setText("Type model name manually")
            self._model_status.setStyleSheet(get_hint_status_style())

        # Auto-fill generation defaults based on selected model
        self._update_generation_defaults()

    def _on_fetch_error(self, error: str) -> None:
        self._fetch_btn.setEnabled(True)
        self._model_status.setText(error)
        self._model_status.setStyleSheet(get_err_status_style())
        self._model_restore_hint = ""

    def _update_generation_defaults(self) -> None:
        """Apply model-driven generation defaults to the spin boxes.

        Behavior:

        * The Max Output Tokens spin box upper bound is always updated to
          the selected model's ``max_output_tokens`` (or a generous
          ``_MANUAL_MAX_TOKENS`` fallback for unknown / custom models).
        * If the user picked a *different* model from the combo, Max
          Output Tokens is set to that model's ``max_output_tokens`` and
          Context Window is set to ``model.context_window``.
        * If the model matches the saved config, the user's custom
          ``max_tokens`` value is preserved — we only clamp it down if it
          exceeds the newly discovered model limit (e.g. the user typed
          a high value and the new provider metadata has a lower cap).
        * Context Window is left alone on same-model population so a user
          who has manually tuned it is not silently overridden.
        """
        model_id = self._get_selected_model_id()
        info = self._find_model_info(model_id)
        if info is None:
            # Manual / custom model with no metadata.  Use a generous
            # fallback upper bound but keep the current value.
            self._max_tokens_spin.setMaximum(_MANUAL_MAX_TOKENS)
            return

        self._max_tokens_spin.setMaximum(info.max_output_tokens)

        if model_id != self._config.provider.model:
            # User picked a different model — auto-fill from metadata.
            self._context_spin.setValue(info.context_window)
            self._max_tokens_spin.setValue(info.max_output_tokens)
            return

        # Same model as the saved config: only clamp if the saved value
        # exceeds the model's advertised limit.
        if self._max_tokens_spin.value() > info.max_output_tokens:
            self._max_tokens_spin.setValue(info.max_output_tokens)

    def _find_model_info(self, model_id: str) -> ModelInfo | None:
        """Return ``ModelInfo`` for *model_id* in the current fetched list."""
        if not model_id:
            return None
        for m in self._fetched_models:
            if m.id == model_id:
                return m
        return None

    def _get_selected_model_id(self) -> str:
        idx = self._model_combo.currentIndex()
        data = self._model_combo.itemData(idx) if idx >= 0 else None
        if data:
            return data
        return self._model_combo.currentText().strip()

    def _set_manual_model_text(self, model_id: str) -> None:
        """Set the editable model combo text without leaving stale
        ``itemData`` from a previous provider selected.

        An editable ``QComboBox`` can keep ``currentIndex()`` pointing at
        a stale item whose ``itemData`` belongs to a different provider,
        even while the line edit visually shows the intended value.
        Because ``_get_selected_model_id()`` prefers ``itemData(idx)``
        whenever the index is valid, pressing OK in that state would save
        the previous provider's model instead of the intended one.  By
        forcing ``currentIndex`` to ``-1`` first, the accessor falls back
        to the visible text and the stale itemData can never reach the
        save path.
        """
        # Blocking signals during the multi-step combo mutation prevents
        # the ordering-changed/currentTextChanged feedback loop from
        # re-selecting an item while we are trying to clear the index.
        blocker = self._model_combo.blockSignals(True)
        try:
            self._model_combo.setCurrentIndex(-1)
            self._model_combo.setEditText(model_id)
            self._model_combo.setCurrentText(model_id)
        finally:
            self._model_combo.blockSignals(blocker)

    # --- Custom provider management ---

    def _populate_provider_combo(self) -> None:
        """Fill the provider combo with builtins + custom connections."""
        self._provider_combo.clear()
        self._provider_combo.addItems(_BUILTIN_PROVIDERS)
        custom = sorted(self._config.custom_providers.keys())
        if custom:
            self._provider_combo.insertSeparator(len(_BUILTIN_PROVIDERS))
            self._provider_combo.addItems(custom)

    def _on_add_custom_provider(self) -> None:
        all_names = _BUILTIN_PROVIDERS + list(self._config.custom_providers.keys())
        dlg = _AddProviderDialog(all_names, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.provider_name()
        api_base = dlg.api_base()
        # Snapshot current provider settings before switching
        self._sync_config_from_ui()
        # Register in config and registry
        self._config.add_custom_provider(name)
        self._registry.register_custom_providers(list(self._config.custom_providers.keys()))
        # Initialize settings for the new provider
        self._config.switch_provider(name)
        self._config.provider.api_base = api_base
        # Rebuild combo and select the new provider
        self._provider_combo.currentTextChanged.disconnect(self._on_provider_changed)
        self._populate_provider_combo()
        idx = self._provider_combo.findText(name)
        if idx >= 0:
            self._provider_combo.setCurrentIndex(idx)
        self._provider_combo.currentTextChanged.connect(self._on_provider_changed)
        self._on_provider_changed(name)

    def _on_remove_custom_provider(self) -> None:
        name = self._provider_combo.currentText()
        if not self._config.is_custom_provider(name):
            return
        self._config.remove_custom_provider(name)
        self._provider_combo.currentTextChanged.disconnect(self._on_provider_changed)
        self._populate_provider_combo()
        self._provider_combo.setCurrentIndex(0)
        self._provider_combo.currentTextChanged.connect(self._on_provider_changed)
        self._on_provider_changed(self._provider_combo.currentText())

    def _sync_config_from_ui(self) -> None:
        """Copy current UI values into config (without accepting the dialog)."""
        self._config.provider.model = self._get_selected_model_id()
        self._config.provider.api_key = self._api_key_edit.text().strip()
        self._config.provider.api_base = self._api_base_edit.text().strip()
        self._config.provider.temperature = self._temp_spin.value()
        self._config.provider.max_tokens = self._max_tokens_spin.value()
        self._config.provider.context_window = self._context_spin.value()
        # Sync GLM controls back to extra if the active provider is GLM.
        self._sync_glm_controls_to_config()

    # --- Accept ---

    def _prompt_password(self, title: str, confirm: bool = False) -> str:
        """Show a modal password dialog. Returns empty string on cancel."""
        from .qt_compat import QMessageBox

        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumWidth(320)
        layout = QVBoxLayout(dlg)

        pw_edit = QLineEdit()
        pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        pw_edit.setPlaceholderText("Password")
        layout.addWidget(pw_edit)

        pw_confirm: QLineEdit | None = None
        if confirm:
            pw_confirm = QLineEdit()
            pw_confirm.setEchoMode(QLineEdit.EchoMode.Password)
            pw_confirm.setPlaceholderText("Confirm password")
            layout.addWidget(pw_confirm)

        from .qt_compat import QDialogButtonBox

        buttons = QDialogButtonBox(
            OK_CANCEL_BUTTONS,
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec_() != QDialog.DialogCode.Accepted:
            return ""

        password = pw_edit.text()
        if not password:
            QMessageBox.warning(self, title, "Password cannot be empty.")
            return ""
        if confirm and pw_confirm and pw_confirm.text() != password:
            QMessageBox.warning(self, title, "Passwords do not match.")
            return ""
        return password

    def _on_accept(self) -> None:
        api_key = self._api_key_edit.text().strip()

        # If the user pasted an OAuth token with the checkbox unchecked,
        # show the consent dialog.  Use parent=None to avoid nesting a
        # modal inside this already-modal settings dialog.
        if api_key.startswith("sk-ant-oat") and not self._oauth_cb.isChecked():
            from .oauth_consent import show_oauth_consent

            choice = show_oauth_consent(parent=None)
            if choice == "accept":
                self._oauth_cb.blockSignals(True)
                self._oauth_cb.setChecked(True)
                self._oauth_cb.blockSignals(False)
            else:
                self._api_key_edit.clear()
                return

        self._config.provider.name = self._provider_combo.currentText()
        self._config.provider.model = self._get_selected_model_id()
        # ONLY save what the user explicitly typed — never save auto-resolved OAuth tokens
        self._config.provider.api_key = self._api_key_edit.text().strip()
        self._config.provider.api_base = self._api_base_edit.text().strip()
        self._config.provider.temperature = self._temp_spin.value()
        self._config.provider.max_tokens = self._max_tokens_spin.value()
        self._config.provider.context_window = self._context_spin.value()
        # Sync GLM controls back to extra before persisting.
        self._sync_glm_controls_to_config()
        self._config.auto_context = self._auto_context_cb.isChecked()
        self._config.checkpoint_auto_save = self._auto_save_cb.isChecked()
        self._config.exploration_turn_limit = self._explore_turns_spin.value()
        self._config.max_retries = self._max_retries_spin.value()
        self._config.silent_retry_mode = self._silent_retry_cb.isChecked()
        font_family_text = self._font_family_combo.currentText()
        self._config.font_family = "" if font_family_text == "(Inherit from IDA)" else font_family_text
        self._config.font_size_override = self._font_size_spin.value()
        self._config.preserve_context = self._preserve_context_cb.isChecked()
        self._config.oauth_consent_accepted = self._oauth_cb.isChecked()
        if hasattr(self, "_knowledge_enabled_cb"):
            self._config.knowledge_enabled = self._knowledge_enabled_cb.isChecked()
        if hasattr(self, "_docs_review_mode_cb"):
            self._config.docs_review_mode = self._docs_review_mode_cb.currentData()
        if hasattr(self, "_parallel_agent_cb"):
            self._config.parallel_agent_enabled = self._parallel_agent_cb.isChecked()
        if hasattr(self, "_parallel_max_spin"):
            self._config.parallel_agent_max_concurrent = self._parallel_max_spin.value()
        # Persist the selected theme.  ``_on_theme_changed`` already
        # wrote it when the user changed the combo, but we re-write
        # here so even users who accepted the dialog without touching
        # the combo get the current combo selection saved.
        if hasattr(self, "_theme_combo"):
            theme_data = self._theme_combo.currentData()
            if theme_data:
                self._config.theme = str(theme_data)

        # --- API key encryption handling ---
        wants_encrypt = self._encrypt_keys_cb.isChecked()
        password = ""
        if wants_encrypt:
            if self._config.encrypt_api_keys:
                # Already encrypted — need current password to re-encrypt
                password = self._prompt_password("Enter encryption password", confirm=False)
            else:
                # Newly enabling — prompt for new password with confirmation
                password = self._prompt_password("Set encryption password", confirm=True)
            if not password:
                return  # user cancelled
        elif self._config.encrypt_api_keys:
            # Disabling encryption — need current password to verify ownership
            password = self._prompt_password("Enter current password to disable encryption", confirm=False)
            if not password:
                return
            # Verify the password is correct before disabling
            if self._config.has_encrypted_keys():
                if not self._config.decrypt_stored_keys(password):
                    from .qt_compat import QMessageBox

                    QMessageBox.warning(self, "Wrong Password", "Incorrect password.")
                    return
            password = ""  # save unencrypted

        self._config.encrypt_api_keys = wants_encrypt
        self.encryption_password = password  # consumed by caller's save()

        # --- IDA Output verbosity ---
        # Apply the live setting so the change takes effect without an
        # IDA restart.  Config file persistence happens via the caller
        # (``save()``) after this method returns.
        if hasattr(self, "_ida_output_log_combo"):
            from ..core.log_sinks import LOG_LEVEL_LABEL_TO_VALUE

            self._config.ida_output_log_level = LOG_LEVEL_LABEL_TO_VALUE.get(
                self._ida_output_log_combo.currentText(), "warning"
            )
            try:
                set_host_log_level(self._config.ida_output_log_level)
            except Exception as e:
                log_debug(f"set_host_log_level failed: {e}")

        # Apply new tab settings — but only for tabs that were
        # actually loaded. The Skills / MCP / Profiles tabs are
        # lazy-constructed on first tab switch (to keep first paint
        # fast). If the user opens Settings and immediately presses OK
        # without visiting those tabs, they remain ``None`` and we
        # MUST NOT force-load them just to save — that would defeat
        # the lazy-load latency work and block the UI thread on
        # SkillsService / MCP config scanning.
        for tab in (
            getattr(self, "_skills_tab", None),
            getattr(self, "_mcp_tab", None),
            getattr(self, "_profiles_tab", None),
        ):
            if tab is not None:
                try:
                    tab.apply_to_config(self._config)
                except Exception as e:
                    log_error(f"Settings apply_to_config failed for {type(tab).__name__}: {e}")

        self.accept()
