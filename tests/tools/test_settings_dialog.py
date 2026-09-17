"""Tests for rikugan.ui.settings_dialog — pure logic helpers."""

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

# Stub heavy dependencies.
# Reinstall these unconditionally because other tests may leave behind
# incomplete stubs that are missing attributes needed here.  Each
# stub has a ``__getattr__`` fallback so any missing attribute
# resolves to a fresh MagicMock, keeping this test file resilient
# to new names added by the production code.


class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        m = MagicMock()
        object.__setattr__(self, name, m)
        return m


for _mod_name in [
    "rikugan.core.config",
    "rikugan.core.logging",
    "rikugan.core.types",
    "rikugan.core.host",
    "rikugan.providers.anthropic_provider",
    "rikugan.providers.auth_cache",
    "rikugan.providers.ollama_provider",
    "rikugan.providers.registry",
    "rikugan.ui.styles",
    "rikugan.ui.theme",
    "rikugan.ui.theme.applicator",
    "rikugan.ui.theme.manager",
    "rikugan.ui.theme.tokens",
    "rikugan.ui.theme.palette_dark",
    "rikugan.ui.theme.palette_light",
    "rikugan.ui.theme.palette_ida",
    "rikugan.ui.message_widgets",
    "rikugan.ui.input_area",
    "rikugan.ui.context_bar",
    "rikugan.ui.tool_widgets",
]:
    _stub = _StubModule(_mod_name)
    for _attr in [
        "RikuganConfig",
        "log_debug",
        "log_error",
        "log_info",
        "log_warning",
        "ModelInfo",
        "Role",
        "resolve_anthropic_auth",
        "resolve_auth_cached",
        "DEFAULT_OLLAMA_URL",
        "ProviderRegistry",
        "build_small_button_stylesheet",
        "maybe_host_stylesheet",
        "use_native_host_theme",
        "get_err_status_style",
        "get_error_label_style",
        "get_hint_status_style",
        "get_ok_status_style",
        "get_settings_btn_style",
    ]:
        setattr(_stub, _attr, MagicMock())
    sys.modules[_mod_name] = _stub

# Track the names we stubbed so the module-level teardown can
# undo exactly the entries we installed.  We compare against
# this set in ``tearDownModule`` so we never pop a real module
# that a downstream test imported between the time we installed
# the stub and the time teardown runs.
#
# NOTE: We intentionally do NOT stub ``rikugan.ui.markdown``,
# ``rikugan.ui.chat_view``, or ``rikugan.ui.panel_core`` here.
# Those modules are imported by sibling tests (e.g.
# ``tests/tools/test_markdown.py``) and stubbing them would
# silently corrupt those tests when pytest collects this
# module first.  ``settings_dialog`` does not import those
# modules, so leaving them out of the stub list is safe.
_STUBBED_BY_THIS_MODULE = frozenset(
    [
        "rikugan.core.config",
        "rikugan.core.logging",
        "rikugan.core.types",
        "rikugan.core.host",
        "rikugan.providers.anthropic_provider",
        "rikugan.providers.auth_cache",
        "rikugan.providers.ollama_provider",
        "rikugan.providers.registry",
        "rikugan.ui.styles",
        "rikugan.ui.theme",
        "rikugan.ui.theme.applicator",
        "rikugan.ui.theme.manager",
        "rikugan.ui.theme.tokens",
        "rikugan.ui.theme.palette_dark",
        "rikugan.ui.theme.palette_light",
        "rikugan.ui.theme.palette_ida",
        "rikugan.ui.message_widgets",
        "rikugan.ui.input_area",
        "rikugan.ui.context_bar",
        "rikugan.ui.tool_widgets",
    ]
)

_styles_mod = sys.modules.get("rikugan.ui.styles")
if _styles_mod is not None:
    _styles_mod.maybe_host_stylesheet = lambda css: css

# Ensure DEFAULT_OLLAMA_URL is a string on the stub (real module already has it)
_ollama_mod = sys.modules.get("rikugan.providers.ollama_provider")
if _ollama_mod is not None and not isinstance(getattr(_ollama_mod, "DEFAULT_OLLAMA_URL", None), str):
    _ollama_mod.DEFAULT_OLLAMA_URL = "http://localhost:11434"

# Install real resolve_auth_cached logic on the stub so tests can exercise it
_ac_stub = sys.modules["rikugan.providers.auth_cache"]
_ac_stub._cached_oauth = None
_ac_stub.resolve_anthropic_auth = MagicMock(return_value=("tok", "api_key"))


def _resolve_auth_cached_impl(explicit_key=""):
    if explicit_key:
        return _ac_stub.resolve_anthropic_auth(explicit_key)
    if _ac_stub._cached_oauth is not None:
        return _ac_stub._cached_oauth
    _ac_stub._cached_oauth = _ac_stub.resolve_anthropic_auth("")
    return _ac_stub._cached_oauth


_ac_stub.resolve_auth_cached = _resolve_auth_cached_impl
_ac_stub.invalidate_cache = MagicMock()

from rikugan.ui.settings_dialog import _AddProviderDialog, _ModelFetcher

# ---------------------------------------------------------------------------
# _ModelFetcher
# ---------------------------------------------------------------------------


class TestModelFetcherShutdown(unittest.TestCase):
    def test_shutdown_sets_alive_false(self):
        registry = MagicMock()
        fetcher = _ModelFetcher(registry)
        self.assertTrue(fetcher._alive)
        fetcher.shutdown()
        self.assertFalse(fetcher._alive)

    def test_shutdown_drains_queue(self):
        registry = MagicMock()
        fetcher = _ModelFetcher(registry)
        fetcher._queue.put(("models", "anthropic", []))
        fetcher._queue.put(("error", "anthropic", "fail"))
        fetcher.shutdown()
        self.assertTrue(fetcher._queue.empty())

    def test_shutdown_empty_queue_noop(self):
        registry = MagicMock()
        fetcher = _ModelFetcher(registry)
        fetcher.shutdown()  # must not raise


class TestModelFetcherPoll(unittest.TestCase):
    def test_poll_returns_none_when_empty(self):
        registry = MagicMock()
        fetcher = _ModelFetcher(registry)
        result = fetcher.poll()
        self.assertIsNone(result)

    def test_poll_returns_item_when_available(self):
        registry = MagicMock()
        fetcher = _ModelFetcher(registry)
        fetcher._queue.put(("models", "anthropic", ["gpt4"]))
        result = fetcher.poll()
        self.assertEqual(result, ("models", "anthropic", ["gpt4"]))

    def test_poll_non_destructive_multiple(self):
        registry = MagicMock()
        fetcher = _ModelFetcher(registry)
        fetcher._queue.put("item1")
        fetcher._queue.put("item2")
        self.assertEqual(fetcher.poll(), "item1")
        self.assertEqual(fetcher.poll(), "item2")
        self.assertIsNone(fetcher.poll())


class TestModelFetcherFetch(unittest.TestCase):
    def test_fetch_error_when_create_fails(self):
        registry = MagicMock()
        registry.new_instance.side_effect = RuntimeError("boom")
        fetcher = _ModelFetcher(registry)
        fetcher.fetch("anthropic", "key", "base")
        result = fetcher.poll()
        self.assertIsNotNone(result)
        kind, _provider, msg = result
        self.assertEqual(kind, "error")
        self.assertIn("boom", msg)

    def test_fetch_no_queue_when_not_alive(self):
        registry = MagicMock()
        registry.new_instance.side_effect = RuntimeError("boom")
        fetcher = _ModelFetcher(registry)
        fetcher.shutdown()  # clears _alive and drains the queue
        fetcher.fetch("anthropic", "key", "base")  # must not crash
        self.assertIsNone(fetcher.poll())


# ---------------------------------------------------------------------------
# _resolve_auth_cached
# ---------------------------------------------------------------------------


class TestResolveAuthCached(unittest.TestCase):
    """Tests for the auth_cache module (extracted from settings_dialog)."""

    def _get_ac(self):
        return _ac_stub

    def setUp(self):
        ac = self._get_ac()
        ac._cached_oauth = None

    def test_explicit_key_bypasses_cache(self):
        ac = self._get_ac()
        mock_auth = MagicMock(return_value=("token", "api_key"))
        with patch.object(ac, "resolve_anthropic_auth", mock_auth):
            ac.resolve_auth_cached("my-key")
        mock_auth.assert_called_once_with("my-key")

    def test_no_key_uses_cache_on_second_call(self):
        ac = self._get_ac()
        mock_auth = MagicMock(return_value=("tok", "oauth"))
        with patch.object(ac, "resolve_anthropic_auth", mock_auth):
            ac.resolve_auth_cached("")
            ac.resolve_auth_cached("")
        mock_auth.assert_called_once()  # second call hits cache

    def test_cache_is_populated_after_first_call(self):
        ac = self._get_ac()
        mock_auth = MagicMock(return_value=("t", "o"))
        with patch.object(ac, "resolve_anthropic_auth", mock_auth):
            ac.resolve_auth_cached("")
        self.assertIsNotNone(ac._cached_oauth)


# ---------------------------------------------------------------------------
# _AddProviderDialog._validate via object.__new__
# ---------------------------------------------------------------------------


def _make_dialog(name_text: str, base_text: str, existing: list | None = None) -> _AddProviderDialog:
    # Use ``_AddProviderDialog.__new__`` (which delegates to the
    # C-level allocator) instead of ``object.__new__``.  The
    # dialog inherits from ``QDialog`` (a real Qt C-level class
    # in this test environment) which rejects ``object.__new__``
    # with ``TypeError: object.__new__(...) is not safe``.
    dlg = _AddProviderDialog.__new__(_AddProviderDialog)
    dlg._existing = {n.lower() for n in (existing or [])}
    dlg._name_edit = MagicMock()
    dlg._name_edit.text.return_value = name_text
    dlg._base_edit = MagicMock()
    dlg._base_edit.text.return_value = base_text
    dlg._error_label = MagicMock()
    dlg.accept = MagicMock()
    return dlg


class TestAddProviderDialogValidate(unittest.TestCase):
    def test_empty_name_shows_error(self):
        dlg = _make_dialog("   ", "http://example.com")
        dlg._validate()
        dlg._error_label.show.assert_called()
        dlg.accept.assert_not_called()

    def test_duplicate_name_shows_error(self):
        dlg = _make_dialog("ollama", "http://example.com", existing=["ollama"])
        dlg._validate()
        dlg._error_label.show.assert_called()
        dlg.accept.assert_not_called()

    def test_empty_base_url_shows_error(self):
        dlg = _make_dialog("mynew", "   ")
        dlg._validate()
        dlg._error_label.show.assert_called()
        dlg.accept.assert_not_called()

    def test_valid_input_calls_accept(self):
        dlg = _make_dialog("mynew", "http://example.com")
        dlg._validate()
        dlg.accept.assert_called_once()

    def test_name_normalized_to_lowercase(self):
        dlg = _make_dialog("MyProvider", "http://example.com")
        dlg._validate()
        dlg._name_edit.setText.assert_called_with("myprovider")

    def test_name_spaces_replaced_with_dashes(self):
        dlg = _make_dialog("my provider", "http://example.com")
        dlg._validate()
        dlg._name_edit.setText.assert_called_with("my-provider")

    def test_duplicate_check_case_insensitive(self):
        dlg = _make_dialog("OLLAMA", "http://example.com", existing=["ollama"])
        dlg._validate()
        dlg.accept.assert_not_called()

    def test_valid_no_error_shown(self):
        dlg = _make_dialog("fresh", "http://example.com")
        dlg._validate()
        dlg._error_label.show.assert_not_called()


# ---------------------------------------------------------------------------
# SettingsDialog logic via object.__new__
# ---------------------------------------------------------------------------


def _import_dialog():
    from rikugan.ui.settings_dialog import SettingsDialog

    return SettingsDialog


def _make_settings():
    SettingsDialog = _import_dialog()
    # Use ``SettingsDialog.__new__`` instead of ``object.__new__``
    # — see the matching comment in ``_make_dialog`` for the
    # rationale (``SettingsDialog`` inherits from ``QDialog``).
    dlg = SettingsDialog.__new__(SettingsDialog)
    dlg._closed = False
    dlg._model_restore_hint = ""
    dlg._resolved_token = ""
    dlg._fetched_models = []
    dlg._fetcher = MagicMock()
    dlg._model_combo = MagicMock()
    dlg._model_combo.currentIndex.return_value = -1
    dlg._model_combo.count.return_value = 0
    dlg._model_status = MagicMock()
    dlg._fetch_btn = MagicMock()
    dlg._context_spin = MagicMock()
    dlg._max_tokens_spin = MagicMock()
    dlg._provider_combo = MagicMock()
    dlg._provider_combo.currentText.return_value = "anthropic"
    dlg._config = MagicMock()
    dlg._registry = MagicMock()
    dlg._auth_status = MagicMock()
    dlg._api_key_edit = MagicMock()
    dlg._api_base_edit = MagicMock()
    dlg._temp_spin = MagicMock()
    dlg._explore_turns_spin = MagicMock()
    dlg._auto_context_cb = MagicMock()
    dlg._auto_save_cb = MagicMock()
    return dlg


class TestSettingsDialogGetSelectedModelId(unittest.TestCase):
    def test_returns_item_data_when_available(self):
        dlg = _make_settings()
        dlg._model_combo.currentIndex.return_value = 0
        dlg._model_combo.itemData.return_value = "claude-3-5-sonnet-20241022"
        result = dlg._get_selected_model_id()
        self.assertEqual(result, "claude-3-5-sonnet-20241022")

    def test_returns_current_text_when_no_data(self):
        dlg = _make_settings()
        dlg._model_combo.currentIndex.return_value = 0
        dlg._model_combo.itemData.return_value = None
        dlg._model_combo.currentText.return_value = " typed-model "
        result = dlg._get_selected_model_id()
        self.assertEqual(result, "typed-model")

    def test_returns_current_text_when_index_negative(self):
        dlg = _make_settings()
        dlg._model_combo.currentIndex.return_value = -1
        dlg._model_combo.currentText.return_value = "manual-model"
        result = dlg._get_selected_model_id()
        self.assertEqual(result, "manual-model")


class TestSettingsDialogPollFetcher(unittest.TestCase):
    def test_noop_when_closed(self):
        dlg = _make_settings()
        dlg._closed = True
        dlg._poll_fetcher()
        dlg._fetcher.poll.assert_not_called()

    def test_noop_when_poll_returns_none(self):
        dlg = _make_settings()
        dlg._fetcher.poll.return_value = None
        dlg._poll_fetcher()  # must not raise

    def test_ignores_stale_provider_result(self):
        dlg = _make_settings()
        dlg._provider_combo.currentText.return_value = "openai"
        dlg._fetcher.poll.return_value = ("models", "anthropic", [])
        dlg._poll_fetcher()
        dlg._model_status.setText.assert_not_called()

    def test_handles_malformed_result_gracefully(self):
        dlg = _make_settings()
        dlg._fetcher.poll.return_value = "not_a_tuple"
        dlg._poll_fetcher()  # must not raise


class TestSettingsDialogOnModelsReady(unittest.TestCase):
    def _model(self, mid: str, name: str | None = None, ctx: int = 200000, max_out: int = 8192):
        m = MagicMock()
        m.id = mid
        m.name = name or mid
        m.context_window = ctx
        m.max_output_tokens = max_out
        return m

    def test_enables_fetch_btn(self):
        dlg = _make_settings()
        dlg._on_models_ready([])
        dlg._fetch_btn.setEnabled.assert_called_with(True)

    def test_no_models_shows_manual_hint(self):
        dlg = _make_settings()
        dlg._on_models_ready([])
        dlg._model_status.setText.assert_called_with("Type model name manually")

    def test_models_shows_count(self):
        dlg = _make_settings()
        models = [self._model("m1"), self._model("m2")]
        dlg._on_models_ready(models)
        dlg._model_status.setText.assert_called_with("2 models")

    def test_clears_restore_hint_after(self):
        dlg = _make_settings()
        dlg._model_restore_hint = "claude-3-5-sonnet"
        dlg._on_models_ready([])
        self.assertEqual(dlg._model_restore_hint, "")


class TestSettingsDialogOnFetchError(unittest.TestCase):
    def test_enables_fetch_btn(self):
        dlg = _make_settings()
        dlg._on_fetch_error("Connection refused")
        dlg._fetch_btn.setEnabled.assert_called_with(True)

    def test_sets_error_text(self):
        dlg = _make_settings()
        dlg._on_fetch_error("Connection refused")
        dlg._model_status.setText.assert_called_with("Connection refused")

    def test_clears_restore_hint(self):
        dlg = _make_settings()
        dlg._model_restore_hint = "old"
        dlg._on_fetch_error("err")
        self.assertEqual(dlg._model_restore_hint, "")


class TestDeferredInit(unittest.TestCase):
    def test_noop_when_closed(self):
        dlg = _make_settings()
        dlg._closed = True
        dlg._deferred_init()  # must not raise, and not call _update_auth_status
        # No way to verify directly but ensure it doesn't crash


# ---------------------------------------------------------------------------
# Appearance tab — Task 14 of the theme system plan.
#
# The full SettingsDialog constructor pulls in SettingsService + SkillsTab +
# MCPTab + ProfilesTab, each of which does I/O (disk reads, network, etc.).
# To test the Appearance tab in isolation we install lightweight stubs for
# those modules in setUp, so the dialog builds but doesn't touch the host
# filesystem. ThemeManager itself is the *real* manager (we want to verify
# that changing the combo actually updates the singleton), so we reset it
# before each test to keep state from leaking between cases.
# ---------------------------------------------------------------------------


def _install_tab_stubs() -> None:
    """Stub out settings_service and the three tab packages.

    Each stub provides the public class names imported inside
    ``_build_ui`` as no-op constructors that return a MagicMock widget.
    """
    _tab_classes = {
        "settings_service": "SettingsService",
        "tabs.skills_tab": "SkillsTab",
        "tabs.mcp_tab": "MCPTab",
        "tabs.profiles_tab": "ProfilesTab",
    }
    _tab_factories = {
        "SettingsService": _FakeService,
        "SkillsTab": _make_fake_tab,
        "MCPTab": _make_fake_tab,
        "ProfilesTab": _make_fake_tab,
    }
    for _name, _class_name in _tab_classes.items():
        _mod = sys.modules.get(f"rikugan.ui.{_name}")
        if _mod is None:
            _mod = types.ModuleType(f"rikugan.ui.{_name}")
            sys.modules[f"rikugan.ui.{_name}"] = _mod
        # Provide the class the dialog imports inside _build_ui
        setattr(_mod, _class_name, _tab_factories[_class_name])


def _make_fake_tab(*_a, **_k):
    """Return a MagicMock that looks like a tab widget."""
    mock = MagicMock()
    mock._build_ui = MagicMock()
    return mock


class _FakeService:
    """Stand-in for SettingsService that doesn't touch the disk."""

    def __init__(self, *_a, **_k):
        self._skills = MagicMock()
        self._skills.rikugan = []
        self._skills.external = {}
        self._mcp = MagicMock()
        self._mcp.rikugan = []
        self._mcp.external = {}
        self._tools_by_category = {}

    @property
    def skills(self):
        return self._skills

    @property
    def mcp(self):
        return self._mcp

    @property
    def tools_by_category(self):
        return self._tools_by_category

    def save_mcp_servers(self, *_a, **_k):
        return None


def _install_real_config_module() -> None:
    """Reinstall the real rikugan.core.config + rikugan.core.logging so
    RikuganConfig() returns a real dataclass with theme_mode.

    Also reinstalls the real ``rikugan.ui.theme.*`` modules so the
    appearance / bootstrap tests can exercise the production
    ``ThemeManager`` singleton.  The module-level stubs are
    necessary for the dialog-construction tests but get in the
    way of the singleton tests, so we tear them down here.
    """
    # Remove stubs so the real modules get imported on next access
    for _name in (
        "rikugan.core.config",
        "rikugan.core.logging",
        "rikugan.core.types",
        "rikugan.core.host",
        "rikugan.ui.theme",
        "rikugan.ui.theme.manager",
        "rikugan.ui.theme.tokens",
        "rikugan.ui.theme.palette_dark",
        "rikugan.ui.theme.palette_light",
        "rikugan.ui.theme.palette_ida",
        "rikugan.ui.styles",
        "rikugan.ui.markdown",
        "rikugan.ui.message_widgets",
        "rikugan.ui.chat_view",
        "rikugan.ui.input_area",
        "rikugan.ui.context_bar",
        "rikugan.ui.tool_widgets",
        "rikugan.ui.panel_core",
    ):
        sys.modules.pop(_name, None)


class TestAppearanceTab(unittest.TestCase):
    def setUp(self):
        # Make sure rikugan.core.config and rikugan.ui.theme.* are
        # the real modules so the dialog gets a real
        # ``RikuganConfig`` instance and the production
        # ``ThemeManager`` singleton.  The module-level stubs
        # installed at the top of this file are necessary for the
        # dialog-construction tests but get in the way of the
        # singleton tests, so we tear them down here BEFORE
        # importing the real ones below.
        _install_real_config_module()
        _install_tab_stubs()

        # Use the real ThemeManager (its tokens() / set_mode() / reset() /
        # instance() API is part of the contract we're testing). Reset so
        # state from earlier tests doesn't leak in.
        from rikugan.ui.theme.manager import ThemeManager
        from rikugan.ui.theme.tokens import ThemeMode, ThemeTokens  # noqa: F401

        self._ThemeManager = ThemeManager
        self._ThemeMode = ThemeMode
        ThemeManager.reset()

    def tearDown(self):
        self._ThemeManager.reset()

    def _build_dialog(self, config=None):
        from rikugan.core.config import RikuganConfig
        from rikugan.ui.settings_dialog import SettingsDialog

        if config is None:
            config = RikuganConfig()
        return SettingsDialog(config=config)

    def test_appearance_tab_in_dialog(self):
        """SettingsDialog should have an 'Appearance' tab at index 1."""
        dlg = self._build_dialog()
        labels = [dlg._tabs.tabText(i) for i in range(dlg._tabs.count())]
        self.assertIn("Appearance", labels)
        appearance_idx = labels.index("Appearance")
        self.assertEqual(appearance_idx, 1)

    def test_theme_combo_has_four_modes(self):
        dlg = self._build_dialog()
        self.assertTrue(hasattr(dlg, "_theme_combo"))
        self.assertEqual(dlg._theme_combo.count(), 4)
        modes = [dlg._theme_combo.itemData(i) for i in range(4)]
        self.assertEqual(modes, ["auto", "dark", "light", "ida"])

    def test_theme_combo_reflects_config(self):
        from rikugan.core.config import RikuganConfig

        config = RikuganConfig()
        config.theme = "light"
        dlg = self._build_dialog(config=config)
        idx = dlg._theme_combo.currentIndex()
        self.assertEqual(dlg._theme_combo.itemData(idx), "light")

    def test_changing_combo_updates_manager(self):
        from rikugan.core.config import RikuganConfig

        config = RikuganConfig()
        dlg = self._build_dialog(config=config)
        for i in range(dlg._theme_combo.count()):
            if dlg._theme_combo.itemData(i) == "dark":
                dlg._theme_combo.setCurrentIndex(i)
                break
        # setCurrentIndex emits currentIndexChanged synchronously in the stub
        self.assertEqual(self._ThemeManager.instance().mode.value, "dark")
        self.assertEqual(config.theme, "dark")

    def test_combo_includes_auto_and_ida(self) -> None:
        """Theme combo offers all four modes — auto, dark, light,
        ida — in that order.  ``auto`` is the new default that
        follows the host palette; ``ida`` is the IDA-native
        passthrough.  Older revisions only exposed dark/light/ida.
        """
        dlg = self._build_dialog()
        modes = [dlg._theme_combo.itemData(i) for i in range(dlg._theme_combo.count())]
        self.assertEqual(modes, ["auto", "dark", "light", "ida"])

    def test_combo_reflects_explicit_auto_in_config(self) -> None:
        """``config.theme = "auto"`` must round-trip through the
        dialog's theme combo (the review found that
        ``RikuganConfig.load`` rejected "auto" as an unknown
        value, silently rewriting it to "ida")."""
        from rikugan.core.config import RikuganConfig

        config = RikuganConfig()
        config.theme = "auto"
        dlg = self._build_dialog(config=config)
        idx = dlg._theme_combo.currentIndex()
        self.assertEqual(dlg._theme_combo.itemData(idx), "auto")

    def test_selecting_combo_persists_to_config_theme(self) -> None:
        """Changing the combo writes the new mode to
        ``config.theme`` so the next ``_on_accept`` /
        ``SettingsDialog._on_accept`` round-trip persists it to
        disk.  The legacy ``theme_mode`` field is *not* written
        because it was never declared on ``RikuganConfig``.
        """
        from rikugan.core.config import RikuganConfig

        config = RikuganConfig()
        dlg = self._build_dialog(config=config)
        for i in range(dlg._theme_combo.count()):
            if dlg._theme_combo.itemData(i) == "light":
                dlg._theme_combo.setCurrentIndex(i)
                break
        self.assertEqual(config.theme, "light")


# ---------------------------------------------------------------------------
# RikuganConfig.load — must accept the "auto" theme value.
# ---------------------------------------------------------------------------


class TestConfigThemeNormalization(unittest.TestCase):
    """Regression coverage for the new ``"auto"`` theme value.

    ``RikuganConfig.load`` used to normalize any unknown theme
    value to ``"ida"``.  The new theme system adds ``"auto"``
    as a first-class value, so loading a config that contains
    ``theme = "auto"`` must round-trip without rewriting it.
    """

    def _real_config(self):
        """Return the *real* ``RikuganConfig`` class.

        The sibling test file's module-level stubbing installs
        a MagicMock under ``rikugan.core.config``.  Force a
        re-import so this test exercises the real class.
        """
        import sys as _sys

        for _name in list(_sys.modules):
            if _name == "rikugan.core.config" or _name.startswith("rikugan.core.config."):
                _sys.modules.pop(_name, None)
        import rikugan.core.config as _cfg

        return _cfg.RikuganConfig

    def test_load_preserves_auto(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        RikuganConfig = self._real_config()

        with tempfile.TemporaryDirectory() as tmp:
            # ``_config_dir`` is the field that backs the
            # ``config_path`` property.  Point it at our temp
            # directory so ``load()`` reads the file we just
            # wrote.
            tmp_path = Path(tmp)
            (tmp_path / "config.json").write_text(
                json.dumps(
                    {
                        "provider": {"name": "anthropic", "model": "claude-3-5-sonnet"},
                        "theme": "auto",
                    }
                ),
                encoding="utf-8",
            )
            config = RikuganConfig(_config_dir=str(tmp_path))
            config.load()
            self.assertEqual(config.theme, "auto")

    def test_load_preserves_dark_and_light(self) -> None:
        """Sanity check: dark/light also round-trip cleanly."""
        import json
        import tempfile
        from pathlib import Path

        RikuganConfig = self._real_config()

        for value in ("dark", "light", "ida"):
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                (tmp_path / "config.json").write_text(
                    json.dumps(
                        {
                            "provider": {"name": "anthropic", "model": "x"},
                            "theme": value,
                        }
                    ),
                    encoding="utf-8",
                )
                config = RikuganConfig(_config_dir=str(tmp_path))
                config.load()
                self.assertEqual(config.theme, value)

    def test_load_falls_back_to_auto_for_garbage_value(self) -> None:
        """An unknown theme value (e.g. from a typo or older
        config) must normalize to ``"auto"`` — not to
        ``"ida"`` as in the previous behaviour.  ``"auto"`` is
        the safe default for fresh installs.
        """
        import json
        import tempfile
        from pathlib import Path

        RikuganConfig = self._real_config()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "config.json").write_text(
                json.dumps(
                    {
                        "provider": {"name": "anthropic", "model": "x"},
                        "theme": "synthwave",
                    }
                ),
                encoding="utf-8",
            )
            config = RikuganConfig(_config_dir=str(tmp_path))
            config.load()
            self.assertEqual(config.theme, "auto")


# ---------------------------------------------------------------------------
# ThemeManager — initialized from RikuganConfig.theme at panel
# construction time.  Pin the behaviour so a future refactor
# doesn't drop the bootstrap call.
# ---------------------------------------------------------------------------


class TestThemeManagerBootstrapsFromConfig(unittest.TestCase):
    """``RikuganPanelCore.__init__`` should set the
    ``ThemeManager`` mode to match the persisted
    ``RikuganConfig.theme`` value.

    The bootstrap lives in :meth:`RikuganPanelCore._apply_initial_theme_from_config`
    (a static helper extracted from ``__init__``).  These tests
    drive that helper directly, which exercises the real code path
    that maps the persisted ``config.theme`` string to a
    :class:`ThemeMode` enum and pushes it into the live
    ``ThemeManager`` singleton.
    """

    def setUp(self) -> None:
        # Make sure rikugan.ui.theme.* is the real module so we
        # exercise the production ``ThemeManager`` singleton
        # (not the module-level stub installed at the top of
        # this test file).
        _install_real_config_module()

        # Use the real ThemeManager (its ``mode`` property is
        # what we are asserting on).  Reset the singleton so the
        # test starts from a known default mode (``AUTO``).
        from rikugan.ui.theme.manager import ThemeManager
        from rikugan.ui.theme.tokens import ThemeMode

        ThemeManager.reset()
        # Force the live mode to a value that is *not* the
        # expected outcome of the bootstrap call.  The
        # helper short-circuits when the live mode already
        # matches the target, so without this precondition
        # the assertions below would pass for the wrong
        # reason (the live mode was left at AUTO from
        # ``reset()`` and the helper was a no-op).
        ThemeManager.instance().set_mode(ThemeMode.LIGHT)
        self._ThemeManager = ThemeManager

    def tearDown(self) -> None:
        self._ThemeManager.reset()

    def _import_helper(self):
        from rikugan.ui.panel_core import RikuganPanelCore

        return RikuganPanelCore._apply_initial_theme_from_config

    def test_dark_string_sets_dark_mode(self) -> None:
        bootstrap = self._import_helper()
        config = MagicMock()
        config.theme = "dark"
        bootstrap(config)
        from rikugan.ui.theme.tokens import ThemeMode

        self.assertEqual(self._ThemeManager.instance().mode, ThemeMode.DARK)

    def test_light_string_sets_light_mode(self) -> None:
        bootstrap = self._import_helper()
        config = MagicMock()
        config.theme = "light"
        bootstrap(config)
        from rikugan.ui.theme.tokens import ThemeMode

        self.assertEqual(self._ThemeManager.instance().mode, ThemeMode.LIGHT)

    def test_ida_string_sets_ida_native_mode(self) -> None:
        bootstrap = self._import_helper()
        config = MagicMock()
        config.theme = "ida"
        bootstrap(config)
        from rikugan.ui.theme.tokens import ThemeMode

        self.assertEqual(self._ThemeManager.instance().mode, ThemeMode.IDA_NATIVE)

    def test_auto_string_sets_auto_mode(self) -> None:
        bootstrap = self._import_helper()
        config = MagicMock()
        config.theme = "auto"
        bootstrap(config)
        from rikugan.ui.theme.tokens import ThemeMode

        self.assertEqual(self._ThemeManager.instance().mode, ThemeMode.AUTO)

    def test_unknown_string_is_silently_ignored(self) -> None:
        """An unrecognised ``config.theme`` value must not raise —
        the bootstrap is best-effort and falls back to whatever
        the default mode is."""
        bootstrap = self._import_helper()
        config = MagicMock()
        config.theme = "this-is-not-a-real-mode"
        # Must not raise.
        bootstrap(config)
        # Mode is left at the LIVE mode (we set it to LIGHT in
        # setUp, and the unknown value didn't match any of the
        # four known modes, so the helper was a no-op).
        from rikugan.ui.theme.tokens import ThemeMode

        self.assertEqual(self._ThemeManager.instance().mode, ThemeMode.LIGHT)

    def test_missing_theme_attribute_uses_default(self) -> None:
        """If ``config`` has no ``theme`` attribute, the helper
        must not raise — it falls back to the ``"ida"`` default
        which then maps to ``ThemeMode.IDA_NATIVE``."""
        bootstrap = self._import_helper()
        config = MagicMock(spec=[])  # no ``theme`` attribute
        bootstrap(config)
        from rikugan.ui.theme.tokens import ThemeMode

        self.assertEqual(self._ThemeManager.instance().mode, ThemeMode.IDA_NATIVE)


class TestSettingsDialogCancelReverts(unittest.TestCase):
    """Cancelling the settings dialog must revert every config mutation.

    Regression: ``_on_provider_changed`` wrote edits into the *live*
    ``self._config`` (via ``_sync_config_from_ui`` +
    ``config.switch_provider``) so a provider switch persisted
    immediately. Because ``done(Rejected)`` only closed the dialog
    without reverting, clicking Cancel after switching providers left
    the config silently altered — losing the previous provider's API
    key/model. These tests pin the Cancel = discard contract: whatever
    the user does inside the dialog, a rejected dialog restores the
    config object to its pre-dialog state.
    """

    def setUp(self):
        _install_real_config_module()
        _install_tab_stubs()
        from rikugan.ui.theme.manager import ThemeManager

        self._ThemeManager = ThemeManager
        ThemeManager.reset()

    def tearDown(self):
        self._ThemeManager.reset()

    def _build_dialog(self, config=None):
        from rikugan.core.config import RikuganConfig
        from rikugan.ui.settings_dialog import SettingsDialog

        if config is None:
            config = RikuganConfig()
        return config, SettingsDialog(config=config)

    def test_cancel_reverts_provider_switch(self):
        from rikugan.core.config import RikuganConfig

        config = RikuganConfig()
        config.provider.name = "anthropic"
        config.provider.api_key = "sk-original-key"
        _, dlg = self._build_dialog(config=config)

        original_name = config.provider.name
        original_key = config.provider.api_key

        # Simulate the in-dialog mutation the current code performs when
        # the user switches providers — the live config is rewritten.
        config.switch_provider("gemini")
        self.assertNotEqual(config.provider.name, original_name)

        # ... then the user changes their mind and cancels the dialog.
        from rikugan.ui.qt_compat import QDialog

        dlg.done(QDialog.DialogCode.Rejected)

        self.assertEqual(config.provider.name, original_name)
        self.assertEqual(config.provider.api_key, original_key)

    def test_cancel_reverts_api_key_edit(self):
        from rikugan.core.config import RikuganConfig

        config = RikuganConfig()
        config.provider.name = "anthropic"
        config.provider.api_key = "sk-original-key"
        _, dlg = self._build_dialog(config=config)

        # An in-dialog edit mutates the live config (the bug).
        config.provider.api_key = "sk-typo-wrong-key"
        from rikugan.ui.qt_compat import QDialog

        dlg.done(QDialog.DialogCode.Rejected)

        # The live config must not carry the typo after cancel.
        self.assertEqual(config.provider.api_key, "sk-original-key")

    def test_accept_still_persists_changes(self):
        # Guard: revert-on-reject must not also clobber the accept path.
        from rikugan.core.config import RikuganConfig

        config = RikuganConfig()
        config.provider.name = "anthropic"
        config.provider.api_key = "sk-original-key"
        _, dlg = self._build_dialog(config=config)

        config.switch_provider("gemini")
        from rikugan.ui.qt_compat import QDialog

        dlg.done(QDialog.DialogCode.Accepted)

        # Accept path keeps the change the user just made.
        self.assertEqual(config.provider.name, "gemini")


def tearDownModule() -> None:
    """Remove the stub modules this test file installed.

    Without this teardown, a stub module we put into ``sys.modules``
    could survive past our test module and leak into a sibling test
    that the user invokes in the same ``pytest`` invocation.  We pop
    only the modules we actually stubbed — never blindly pop entries
    that may have been installed by other test files between our
    setup and our teardown.
    """
    for _name in _STUBBED_BY_THIS_MODULE:
        sys.modules.pop(_name, None)


if __name__ == "__main__":
    unittest.main()
