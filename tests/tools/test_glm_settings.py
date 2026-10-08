"""GLM Settings UI tests -- Task 13 of the GLM reasoning resilience plan.

These tests exercise the GLM settings controls (visibility, persistence)
and the one-time explicit ``api.z.ai`` migration prompt.

GLM controls must be visible only when the active provider has
``extra["dialect"] == "glm"``, and must persist the exact typed schema
from ``lucnhan.core.glm_config``.  The Z.AI migration is explicit-only:
it prompts when the hostname is exactly ``api.z.ai`` and the provider
has no dialect saved, and records a durable marker so decline is not
re-prompted.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

# Install the lightweight ``PySide6`` stubs BEFORE importing any
# lucnhan module.
from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()

import sys
import types
from unittest.mock import MagicMock


class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        m = MagicMock()
        object.__setattr__(self, name, m)
        return m


for _mod_name in [
    "lucnhan.core.host",
    "lucnhan.providers.anthropic_provider",
    "lucnhan.providers.auth_cache",
    "lucnhan.providers.ollama_provider",
    "lucnhan.providers.registry",
    "lucnhan.ui.styles",
    "lucnhan.ui.theme",
    "lucnhan.ui.theme.applicator",
    "lucnhan.ui.theme.manager",
    "lucnhan.ui.theme.tokens",
    "lucnhan.ui.theme.palette_dark",
    "lucnhan.ui.theme.palette_light",
    "lucnhan.ui.theme.palette_ida",
    "lucnhan.ui.message_widgets",
    "lucnhan.ui.input_area",
    "lucnhan.ui.context_bar",
    "lucnhan.ui.tool_widgets",
]:
    _stub = _StubModule(_mod_name)
    for _attr in [
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

_ollama_mod = sys.modules.get("lucnhan.providers.ollama_provider")
if _ollama_mod is not None and not isinstance(getattr(_ollama_mod, "DEFAULT_OLLAMA_URL", None), str):
    _ollama_mod.DEFAULT_OLLAMA_URL = "http://localhost:11434"

_ac_stub = sys.modules["lucnhan.providers.auth_cache"]
_ac_stub._cached_oauth = None
_ac_stub.resolve_anthropic_auth = MagicMock(return_value=("tok", "api_key"))
_ac_stub.invalidate_cache = MagicMock()
_ac_stub.set_keychain_consent = MagicMock()

from lucnhan.core.config import LucNhanConfig

# Stub tab service / tabs so _build_ui does not touch the filesystem.
_FakeService = type("_FakeService", (), {"__init__": lambda self, *a, **k: None})


def _make_fake_tab(*_a, **_k):
    mock = MagicMock()
    mock._build_ui = MagicMock()
    return mock


for _name, _cls_name in [
    ("settings_service", "SettingsService"),
    ("tabs.skills_tab", "SkillsTab"),
    ("tabs.mcp_tab", "MCPTab"),
    ("tabs.profiles_tab", "ProfilesTab"),
]:
    _mod = sys.modules.get(f"lucnhan.ui.{_name}")
    if _mod is None:
        _mod = types.ModuleType(f"lucnhan.ui.{_name}")
        sys.modules[f"lucnhan.ui.{_name}"] = _mod
    setattr(_mod, _cls_name, _make_fake_tab)

sys.modules["lucnhan.ui.settings_service"].SettingsService = _FakeService


def _ensure_qapplication():
    from lucnhan.ui.qt_compat import QApplication

    return QApplication.instance() or QApplication([])


_STUBBED_BY_THIS_MODULE = frozenset(
    [
        "lucnhan.core.host",
        "lucnhan.providers.anthropic_provider",
        "lucnhan.providers.auth_cache",
        "lucnhan.providers.ollama_provider",
        "lucnhan.providers.registry",
        "lucnhan.ui.styles",
        "lucnhan.ui.theme",
        "lucnhan.ui.theme.applicator",
        "lucnhan.ui.theme.manager",
        "lucnhan.ui.theme.tokens",
        "lucnhan.ui.theme.palette_dark",
        "lucnhan.ui.theme.palette_light",
        "lucnhan.ui.theme.palette_ida",
        "lucnhan.ui.message_widgets",
        "lucnhan.ui.input_area",
        "lucnhan.ui.context_bar",
        "lucnhan.ui.tool_widgets",
    ]
)


# ---------------------------------------------------------------------------
# Visibility tests
# ---------------------------------------------------------------------------


class TestGLMControlsVisibility(unittest.TestCase):
    """GLM group visibility follows extra['dialect'] == 'glm'."""

    def setUp(self) -> None:
        _ensure_qapplication()

    def _build_dialog(self, config=None):
        from lucnhan.ui.settings_dialog import SettingsDialog

        if config is None:
            config = LucNhanConfig()
        return SettingsDialog(config), config

    def test_glm_group_exists(self) -> None:
        dlg, _ = self._build_dialog()
        try:
            self.assertTrue(hasattr(dlg, "_glm_group"))
        finally:
            dlg.done(0)

    def test_glm_controls_hidden_when_not_glm_dialect(self) -> None:
        dlg, config = self._build_dialog()
        try:
            config.provider.extra = {}
            dlg._refresh_glm_controls()
            self.assertFalse(dlg._glm_group.isVisible())
        finally:
            dlg.done(0)

    def test_glm_controls_visible_for_glm_dialect(self) -> None:
        dlg, config = self._build_dialog()
        try:
            config.provider.extra = {"dialect": "glm"}
            dlg._refresh_glm_controls()
            self.assertTrue(dlg._glm_group.isVisible())
        finally:
            dlg.done(0)

    def test_glm_controls_hidden_for_non_glm_dialect(self) -> None:
        dlg, config = self._build_dialog()
        try:
            config.provider.extra = {"dialect": "openai"}
            dlg._refresh_glm_controls()
            self.assertFalse(dlg._glm_group.isVisible())
        finally:
            dlg.done(0)


# ---------------------------------------------------------------------------
# Persistence tests
# ---------------------------------------------------------------------------


class TestGLMControlsPersistence(unittest.TestCase):
    """_sync_config_from_ui() must persist the exact GLM extra schema.

    The thinking level is driven by the shared provider-neutral
    ``_thinking_combo`` in the Generation group; the GLM group keeps
    preserve / guard / endpoint only.
    """

    def setUp(self) -> None:
        _ensure_qapplication()

    def _build_dialog(self):
        from lucnhan.ui.settings_dialog import SettingsDialog

        config = LucNhanConfig()
        config.provider.extra = {"dialect": "glm"}
        return SettingsDialog(config), config

    @staticmethod
    def _select_level(dlg, level: str) -> None:
        """Select ``level`` in the shared Thinking combo."""
        idx = dlg._thinking_combo.findData(level)
        assert idx >= 0, f"level {level!r} not offered: {[dlg._thinking_combo.itemData(i) for i in range(dlg._thinking_combo.count())]}"
        dlg._thinking_combo.setCurrentIndex(idx)

    def test_sync_config_from_ui_persists_exact_extra(self) -> None:
        dlg, config = self._build_dialog()
        try:
            self._select_level(dlg, "none")
            dlg._glm_preserve_cb.setChecked(True)
            dlg._glm_guard_cb.setChecked(True)
            dlg._glm_ceiling_spin.setValue(16_384)
            dlg._glm_recovery_spin.setValue(16_384)

            dlg._sync_config_from_ui()

            self.assertEqual(
                config.provider.extra,
                {
                    "dialect": "glm",
                    "endpoint_type": "standard",
                    "thinking": {"enabled": False, "reasoning_effort": "none", "preserve": True},
                    "degeneration_guard": {
                        "enabled": True,
                        "reasoning_token_ceiling": 16_384,
                        "retry_without_thinking": True,
                        "recovery_max_tokens": 16_384,
                    },
                },
            )
        finally:
            dlg.done(0)

    def test_non_none_level_yields_enabled_true(self) -> None:
        dlg, config = self._build_dialog()
        try:
            self._select_level(dlg, "max")
            dlg._glm_preserve_cb.setChecked(True)
            dlg._glm_guard_cb.setChecked(True)
            dlg._glm_ceiling_spin.setValue(16_384)
            dlg._glm_recovery_spin.setValue(16_384)

            dlg._sync_config_from_ui()

            thinking = config.provider.extra.get("thinking", {})
            self.assertTrue(thinking.get("enabled"))
            self.assertEqual(thinking.get("reasoning_effort"), "max")
        finally:
            dlg.done(0)

    def test_load_glm_controls_from_config_round_trips(self) -> None:
        """Settings saved to extra must round-trip back to the UI controls."""
        dlg, config = self._build_dialog()
        try:
            config.provider.extra = {
                "dialect": "glm",
                "thinking": {"enabled": True, "reasoning_effort": "high", "preserve": False},
                "degeneration_guard": {
                    "enabled": False,
                    "reasoning_token_ceiling": 8_192,
                    "retry_without_thinking": False,
                    "recovery_max_tokens": 4_096,
                },
            }
            dlg._refresh_thinking_levels()
            dlg._load_thinking_controls_from_config()
            dlg._load_glm_controls_from_config()
            self.assertEqual(dlg._thinking_combo.currentData(), "high")
            self.assertFalse(dlg._glm_preserve_cb.isChecked())
            self.assertFalse(dlg._glm_guard_cb.isChecked())
            self.assertEqual(dlg._glm_ceiling_spin.value(), 8_192)
            self.assertEqual(dlg._glm_recovery_spin.value(), 4_096)
        finally:
            dlg.done(0)

    def test_glm_preserve_false_survives_the_shared_thinking_write(self) -> None:
        """GLM sync rebuilds the whole extra dict and runs first; the
        shared thinking write must then keep the preserve value the
        checkbox just wrote rather than resurrecting the old one."""
        dlg, config = self._build_dialog()
        try:
            self._select_level(dlg, "high")
            dlg._glm_preserve_cb.setChecked(False)

            dlg._sync_config_from_ui()

            self.assertEqual(
                config.provider.extra["thinking"],
                {"enabled": True, "reasoning_effort": "high", "preserve": False},
            )
        finally:
            dlg.done(0)

    def test_guard_disabled_writes_enabled_false(self) -> None:
        """When the guard checkbox is unchecked, the guard block is
        still written (with enabled=False) so the provider knows to
        skip degeneration detection entirely."""
        dlg, config = self._build_dialog()
        try:
            self._select_level(dlg, "max")
            dlg._glm_preserve_cb.setChecked(True)
            dlg._glm_guard_cb.setChecked(False)
            dlg._glm_ceiling_spin.setValue(16_384)
            dlg._glm_recovery_spin.setValue(16_384)

            dlg._sync_config_from_ui()

            guard = config.provider.extra.get("degeneration_guard", {})
            self.assertFalse(guard.get("enabled"))
        finally:
            dlg.done(0)


class TestGLMEndpointType(unittest.TestCase):
    """Endpoint type combo (Standard vs Coding Plan) persists and drives
    the API base URL.  The two Z.AI endpoints use non-interchangeable keys."""

    def setUp(self) -> None:
        _ensure_qapplication()

    def _build_dialog(self):
        from lucnhan.ui.settings_dialog import SettingsDialog

        config = LucNhanConfig()
        config.provider.extra = {"dialect": "glm"}
        return SettingsDialog(config), config

    def test_default_endpoint_is_standard(self) -> None:
        dlg, _ = self._build_dialog()
        try:
            self.assertEqual(dlg._glm_endpoint_combo.currentData(), "standard")
        finally:
            dlg.done(0)

    def test_endpoint_round_trips(self) -> None:
        """Endpoint saved to extra loads back into the combo."""
        from lucnhan.ui.settings_dialog import SettingsDialog

        config = LucNhanConfig()
        config.provider.extra = {"dialect": "glm", "endpoint_type": "coding_plan"}
        dlg = SettingsDialog(config)
        try:
            self.assertEqual(dlg._glm_endpoint_combo.currentData(), "coding_plan")
        finally:
            dlg.done(0)

    def test_endpoint_change_updates_base_url(self) -> None:
        """Switching to Coding Plan updates api_base from the standard URL
        to the coding-plan URL."""
        from lucnhan.core.glm_config import GLM_ENDPOINT_BASE_URLS
        from lucnhan.ui.settings_dialog import SettingsDialog

        config = LucNhanConfig()
        config.provider.extra = {"dialect": "glm"}
        config.provider.api_base = GLM_ENDPOINT_BASE_URLS["standard"]
        dlg = SettingsDialog(config)
        try:
            # Simulate user selecting Coding Plan.
            idx = dlg._glm_endpoint_combo.findData("coding_plan")
            dlg._glm_endpoint_combo.setCurrentIndex(idx)
            # _on_glm_endpoint_changed fires on currentIndexChanged.
            self.assertEqual(
                dlg._api_base_edit.text().strip(),
                GLM_ENDPOINT_BASE_URLS["coding_plan"],
            )
        finally:
            dlg.done(0)



# ---------------------------------------------------------------------------


class TestGLMZaiMigration(unittest.TestCase):
    """One-time explicit migration for api.z.ai custom providers.

    Migration must fire only when:
    - hostname is exactly ``api.z.ai``
    - provider is a custom / generic compat connection
    - no dialect is already saved
    - migration has not been previously prompted (marker durable)

    Accept sets ``extra.dialect="glm"`` and re-registers dialects.
    Decline leaves ``extra`` untouched and continues as OpenAI-compatible.
    """

    def setUp(self) -> None:
        _ensure_qapplication()

    def _build_dialog(self, config=None):
        from lucnhan.ui.settings_dialog import SettingsDialog

        if config is None:
            config = LucNhanConfig()
        return SettingsDialog(config), config

    def test_migration_accept_sets_dialect_glm(self) -> None:
        config = LucNhanConfig()
        config.add_custom_provider("zai-glm")
        config.provider.name = "zai-glm"
        config.provider.api_base = "https://api.z.ai/api/paas/v4/"
        config.provider.model = "glm-4.7"

        dlg, _ = self._build_dialog(config)
        try:
            with patch("lucnhan.ui.settings_dialog._prompt_zai_migration", return_value=True):
                dlg._maybe_prompt_zai_migration()

            self.assertEqual(config.provider.extra.get("dialect"), "glm")
            self.assertTrue(config.custom_providers["zai-glm"].get("glm_migration_prompted"))
        finally:
            dlg.done(0)

    def test_migration_decline_leaves_extra_untouched(self) -> None:
        config = LucNhanConfig()
        config.add_custom_provider("zai-glm")
        config.provider.name = "zai-glm"
        config.provider.api_base = "https://api.z.ai/api/paas/v4/"
        config.provider.model = "glm-4.7"

        dlg, _ = self._build_dialog(config)
        try:
            with patch("lucnhan.ui.settings_dialog._prompt_zai_migration", return_value=False):
                dlg._maybe_prompt_zai_migration()

            self.assertNotIn("dialect", config.provider.extra)
            self.assertTrue(config.custom_providers["zai-glm"].get("glm_migration_prompted"))
        finally:
            dlg.done(0)

    def test_migration_not_prompted_for_non_zai_host(self) -> None:
        config = LucNhanConfig()
        config.add_custom_provider("other")
        config.provider.name = "other"
        config.provider.api_base = "https://api.other.com/v1"
        config.provider.model = "some-model"

        dlg, _ = self._build_dialog(config)
        try:
            with patch("lucnhan.ui.settings_dialog._prompt_zai_migration", return_value=True) as m:
                dlg._maybe_prompt_zai_migration()
                m.assert_not_called()
            self.assertNotIn("dialect", config.provider.extra)
        finally:
            dlg.done(0)

    def test_migration_not_prompted_when_dialect_already_saved(self) -> None:
        config = LucNhanConfig()
        config.add_custom_provider("zai-glm")
        config.provider.name = "zai-glm"
        config.provider.api_base = "https://api.z.ai/api/paas/v4/"
        config.provider.model = "glm-4.7"
        config.provider.extra = {"dialect": "glm"}

        dlg, _ = self._build_dialog(config)
        try:
            with patch("lucnhan.ui.settings_dialog._prompt_zai_migration", return_value=True) as m:
                dlg._maybe_prompt_zai_migration()
                m.assert_not_called()
        finally:
            dlg.done(0)

    def test_migration_not_prompted_when_already_prompted(self) -> None:
        config = LucNhanConfig()
        config.add_custom_provider("zai-glm")
        config.custom_providers["zai-glm"]["glm_migration_prompted"] = True
        config.provider.name = "zai-glm"
        config.provider.api_base = "https://api.z.ai/api/paas/v4/"
        config.provider.model = "glm-4.7"

        dlg, _ = self._build_dialog(config)
        try:
            with patch("lucnhan.ui.settings_dialog._prompt_zai_migration", return_value=True) as m:
                dlg._maybe_prompt_zai_migration()
                m.assert_not_called()
        finally:
            dlg.done(0)

    def test_migration_not_prompted_for_builtin_provider(self) -> None:
        config = LucNhanConfig()
        config.provider.name = "anthropic"
        config.provider.api_base = "https://api.z.ai/"
        config.provider.model = "claude-3"

        dlg, _ = self._build_dialog(config)
        try:
            with patch("lucnhan.ui.settings_dialog._prompt_zai_migration", return_value=True) as m:
                dlg._maybe_prompt_zai_migration()
                m.assert_not_called()
        finally:
            dlg.done(0)

    # --- Marker durability across Cancel -------------------------------

    def test_decline_then_cancel_retains_marker_and_no_dialect(self) -> None:
        """Decline then Cancel must: keep the marker (no re-prompt on
        reopen) and leave no dialect on the active provider."""

        config = LucNhanConfig()
        config.add_custom_provider("zai-glm")
        config.provider.name = "zai-glm"
        config.provider.api_base = "https://api.z.ai/api/paas/v4/"
        config.provider.model = "glm-4.7"

        dlg, _ = self._build_dialog(config)
        try:
            with patch("lucnhan.ui.settings_dialog._prompt_zai_migration", return_value=False):
                dlg._maybe_prompt_zai_migration()

            # Before Cancel: marker set, no dialect.
            self.assertTrue(config.custom_providers["zai-glm"].get("glm_migration_prompted"))
            self.assertNotIn("dialect", config.provider.extra)

            # Cancel: _restore_config_from_snapshot replaces custom_providers
            # from the snapshot taken at construction.  The marker must
            # survive because we mirror it to the snapshot.
            dlg._restore_config_from_snapshot()

            # Marker survived Cancel.
            self.assertTrue(
                config.custom_providers.get("zai-glm", {}).get("glm_migration_prompted"),
                "Migration marker must survive Cancel so reopening does not re-prompt.",
            )
            # Dialect is still absent (decline path, reverted by Cancel).
            self.assertNotIn("dialect", config.provider.extra)
        finally:
            dlg.done(0)

    def test_accept_then_cancel_marker_survives_dialect_reverts(self) -> None:
        """Accept then Cancel must: keep the marker (durable) but revert
        the dialect change (normal Cancel semantics for config edits).

        The reviewer's chosen semantics: the migration prompt outcome
        (accept/decline) is recorded durably so it is never shown again.
        But the actual config edits (dialect="glm", registry
        re-registration) follow normal Cancel behavior and revert to the
        pre-dialog state.
        """

        config = LucNhanConfig()
        config.add_custom_provider("zai-glm")
        config.provider.name = "zai-glm"
        config.provider.api_base = "https://api.z.ai/api/paas/v4/"
        config.provider.model = "glm-4.7"

        dlg, _ = self._build_dialog(config)
        try:
            with patch("lucnhan.ui.settings_dialog._prompt_zai_migration", return_value=True):
                dlg._maybe_prompt_zai_migration()

            # Before Cancel: marker set, dialect=glm.
            self.assertTrue(config.custom_providers["zai-glm"].get("glm_migration_prompted"))
            self.assertEqual(config.provider.extra.get("dialect"), "glm")

            # Cancel reverts config edits but preserves the marker.
            dlg._restore_config_from_snapshot()

            # Marker survived Cancel.
            self.assertTrue(
                config.custom_providers.get("zai-glm", {}).get("glm_migration_prompted"),
                "Migration marker must survive Cancel even after accept.",
            )
            # Dialect reverted by Cancel (normal semantics).
            self.assertNotIn(
                "dialect",
                config.provider.extra,
                "Dialect change must revert on Cancel (normal config-edit semantics), "
                "only the migration marker is durable.",
            )
        finally:
            dlg.done(0)


class TestThinkingCombo(unittest.TestCase):
    """The provider-neutral Thinking combo: per-model level lists, and the
    provider-neutral storage schema written back into ``provider.extra``."""

    def setUp(self) -> None:
        _ensure_qapplication()

    def _build_dialog(self, model: str = "gpt-test", extra=None):
        from lucnhan.ui.settings_dialog import SettingsDialog

        config = LucNhanConfig()
        config.provider.name = "openai"
        config.provider.model = model
        if extra is not None:
            config.provider.extra = extra
        dlg = SettingsDialog(config)
        return dlg, config

    @staticmethod
    def _levels(dlg) -> list:
        return [dlg._thinking_combo.itemData(i) for i in range(dlg._thinking_combo.count())]

    @staticmethod
    def _select_model(dlg, model_id: str) -> None:
        dlg._model_combo.clear()
        dlg._model_combo.addItem(model_id, model_id)
        dlg._model_combo.setCurrentIndex(0)

    # --- Level list population -----------------------------------------

    def test_restricted_model_offers_only_its_own_levels(self) -> None:
        """GLM-5.3 supports high and max only — the combo must not offer
        levels the endpoint would reject."""
        dlg, _ = self._build_dialog()
        try:
            self._select_model(dlg, "glm-5.3")
            self.assertEqual(self._levels(dlg), ["high", "max"])
        finally:
            dlg.done(0)

    def test_known_model_defaults_to_high(self) -> None:
        """A model whose levels are known gets a sensible active default."""
        dlg, _ = self._build_dialog()
        try:
            self._select_model(dlg, "glm-5.3")
            self.assertEqual(dlg._thinking_combo.currentData(), "high")
        finally:
            dlg.done(0)

    def test_unknown_model_offers_the_full_default_range(self) -> None:
        from lucnhan.core.thinking import DEFAULT_THINKING_LEVELS

        dlg, _ = self._build_dialog()
        try:
            self._select_model(dlg, "some-unreleased-model")
            self.assertEqual(self._levels(dlg), list(DEFAULT_THINKING_LEVELS))
        finally:
            dlg.done(0)

    def test_unknown_model_defaults_to_none(self) -> None:
        """Opt-in default: sending reasoning_effort to a model that never
        advertised thinking makes the endpoint 400, so unknown models
        start with thinking off."""
        dlg, _ = self._build_dialog()
        try:
            self._select_model(dlg, "some-unreleased-model")
            self.assertEqual(dlg._thinking_combo.currentData(), "none")
        finally:
            dlg.done(0)

    def test_glm_5_2_offers_its_seven_levels(self) -> None:
        dlg, _ = self._build_dialog()
        try:
            self._select_model(dlg, "glm-5.2")
            self.assertEqual(
                self._levels(dlg),
                ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
            )
        finally:
            dlg.done(0)

    def test_selection_survives_a_model_switch_that_still_offers_it(self) -> None:
        """Switching between two models with overlapping levels must not
        reset the user's choice."""
        dlg, _ = self._build_dialog()
        try:
            self._select_model(dlg, "glm-5.2")
            dlg._thinking_combo.setCurrentIndex(dlg._thinking_combo.findData("xhigh"))
            self._select_model(dlg, "glm-5.3")
            # "xhigh" is not offered by glm-5.3 → fall back to its default.
            self.assertEqual(dlg._thinking_combo.currentData(), "high")

            dlg._thinking_combo.setCurrentIndex(dlg._thinking_combo.findData("max"))
            self._select_model(dlg, "glm-5.2")
            self.assertEqual(dlg._thinking_combo.currentData(), "max")
        finally:
            dlg.done(0)

    # --- Persistence ---------------------------------------------------

    def test_none_level_persists_enabled_false(self) -> None:
        dlg, config = self._build_dialog()
        try:
            dlg._thinking_combo.setCurrentIndex(dlg._thinking_combo.findData("none"))
            dlg._sync_config_from_ui()
            self.assertEqual(
                config.provider.extra["thinking"],
                {"enabled": False, "reasoning_effort": "none", "preserve": True},
            )
        finally:
            dlg.done(0)

    def test_active_level_persists_enabled_true(self) -> None:
        dlg, config = self._build_dialog()
        try:
            dlg._thinking_combo.setCurrentIndex(dlg._thinking_combo.findData("ultra"))
            dlg._sync_config_from_ui()
            self.assertEqual(
                config.provider.extra["thinking"],
                {"enabled": True, "reasoning_effort": "ultra", "preserve": True},
            )
        finally:
            dlg.done(0)

    def test_sync_preserves_sibling_extra_keys(self) -> None:
        """Custom-provider extras are opaque; the thinking write must merge
        into them rather than replace the dict."""
        dlg, config = self._build_dialog(extra={"some_option": {"a": 1}})
        try:
            dlg._thinking_combo.setCurrentIndex(dlg._thinking_combo.findData("high"))
            dlg._sync_config_from_ui()
            self.assertEqual(config.provider.extra["some_option"], {"a": 1})
            self.assertIn("thinking", config.provider.extra)
        finally:
            dlg.done(0)

    def test_sync_keeps_an_existing_preserve_flag(self) -> None:
        dlg, config = self._build_dialog(
            extra={"thinking": {"enabled": True, "reasoning_effort": "high", "preserve": False}}
        )
        try:
            dlg._thinking_combo.setCurrentIndex(dlg._thinking_combo.findData("max"))
            dlg._sync_config_from_ui()
            self.assertIs(config.provider.extra["thinking"]["preserve"], False)
        finally:
            dlg.done(0)

    def test_saved_level_is_restored_on_reopen(self) -> None:
        """A saved level round-trips back into the combo on the next open."""
        from lucnhan.ui.settings_dialog import SettingsDialog

        config = LucNhanConfig()
        config.provider.name = "openai"
        config.provider.model = "glm-5.3"
        config.provider.extra = {"thinking": {"enabled": True, "reasoning_effort": "max", "preserve": True}}
        dlg = SettingsDialog(config)
        try:
            self.assertEqual(dlg._thinking_combo.currentData(), "max")
        finally:
            dlg.done(0)

    def test_saved_level_unsupported_by_model_falls_back_instead_of_being_injected(self) -> None:
        """A stale saved level (e.g. "ultra" saved for another model) must
        not be spliced into the combo as an out-of-list item."""
        from lucnhan.ui.settings_dialog import SettingsDialog

        config = LucNhanConfig()
        config.provider.name = "openai"
        config.provider.model = "glm-5.3"
        config.provider.extra = {"thinking": {"enabled": True, "reasoning_effort": "ultra", "preserve": True}}
        dlg = SettingsDialog(config)
        try:
            self.assertEqual(self._levels(dlg), ["high", "max"])
            self.assertEqual(dlg._thinking_combo.currentData(), "high")
        finally:
            dlg.done(0)

    def test_thinking_is_written_for_non_glm_providers_too(self) -> None:
        """The setting is provider-neutral: an OpenAI-family config gets
        the same schema, which is what OpenAIProvider reads."""
        dlg, config = self._build_dialog()
        try:
            dlg._thinking_combo.setCurrentIndex(dlg._thinking_combo.findData("ultra"))
            dlg._on_accept()
            self.assertEqual(
                config.provider.extra["thinking"],
                {"enabled": True, "reasoning_effort": "ultra", "preserve": True},
            )
        finally:
            dlg.done(0)


def tearDownModule() -> None:
    """Remove the stub modules this test file installed."""
    for _name in _STUBBED_BY_THIS_MODULE:
        sys.modules.pop(_name, None)


if __name__ == "__main__":
    unittest.main()
