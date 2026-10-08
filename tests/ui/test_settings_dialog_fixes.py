"""Regression tests for the settings-dialog and provider-normalization
fixes that landed alongside the lazy-loading / dark-theme rewrite.

These tests are pure Python where possible; the SettingsDialog and the
``_AddButtonTabBar`` tests require a ``QApplication`` and use the shared
``qapp`` fixture from ``tests/conftest.py``.

The fixes covered here:

A. SettingsDialog OK with lazy tabs unopened.
B. ``_ModelFetcher`` no longer accepts ``ensure_ready=False``.
C. Opening settings does not corrupt Ollama / custom model selection.
D. Anthropic ``message_delta`` output_tokens is coercion-safe.
E. OpenAI streaming yields one cumulative usage, not additive deltas.
F. OpenAI retryable transient error mapping.
G. ``_AddButtonTabBar`` ``+`` button uses theme-aware stylesheet.
"""

from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from rikugan.core.types import Message, Role
from rikugan.providers.openai_provider import OpenAIProvider
from tests.qt_real import requires_real_qt

# ----------------------------------------------------------------------------
# Test-local fakes for Anthropic streaming and provider-fetcher ordering.
# These intentionally live in this regression-test module and are prefixed
# with ``_`` to signal they are not part of the production surface.
# ----------------------------------------------------------------------------


class _FakeAnthropicStream:
    """Minimal stand-in for ``anthropic.Anthropic.messages.stream(...)`` context manager."""

    def __init__(self, events):
        self._events = events

    def __enter__(self):
        return iter(self._events)

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeAnthropicMessages:
    def __init__(self, events):
        self._events = events

    def stream(self, **_kwargs):
        return _FakeAnthropicStream(self._events)


class _FakeAnthropicClient:
    def __init__(self, events):
        self.messages = _FakeAnthropicMessages(events)


def _ensure_qapplication():
    """Return the active ``QApplication``, creating one if necessary.

    SettingsDialog and other widgets in this file require a
    ``QApplication`` to exist.  Running an individual test in isolation
    crashes on Windows Qt (exit code ``-1073740791``) if no
    ``QApplication`` is present, so every widget-touching test helper
    must call this first.
    """
    from rikugan.ui.qt_compat import QApplication

    return QApplication.instance() or QApplication([])


# ----------------------------------------------------------------------------
# A. SettingsDialog OK with lazy tabs unopened
# ----------------------------------------------------------------------------


@requires_real_qt
class TestSettingsDialogLazyOK(unittest.TestCase):
    """Pressing OK on a fresh SettingsDialog must not blow up when the
    Skills / MCP / Profiles tabs have not been opened.

    Without the fix, ``_on_accept()`` unconditionally dereferences
    ``self._skills_tab.apply_to_config(...)`` and raises AttributeError
    because the lazy-loaded tabs are still ``None``.
    """

    def test_accept_with_unopened_lazy_tabs_does_not_raise(self) -> None:
        from rikugan.core.config import RikuganConfig
        from rikugan.ui.settings_dialog import SettingsDialog

        _ensure_qapplication()

        config = RikuganConfig()
        config.provider.name = "ollama"
        config.provider.model = "llama3.1"

        dlg = SettingsDialog(config)
        try:
            # Sanity: lazy tabs start out as None.
            self.assertIsNone(dlg._skills_tab)
            self.assertIsNone(dlg._mcp_tab)
            self.assertIsNone(dlg._profiles_tab)

            # The actual OK path mutates encryption state via prompts.  We
            # call ``_on_accept`` directly but stub out ``_prompt_password``
            # so it does not show a modal.  Encryption is disabled by
            # default, so the password prompt is only triggered when the
            # user toggles the checkbox.  We also avoid the OAuth consent
            # path by leaving the API key field empty.
            try:
                dlg._on_accept()
            except Exception as e:
                self.fail(f"_on_accept raised {type(e).__name__}: {e}")

            # Provider and model must be preserved when OK is pressed
            # without opening the heavy tabs.
            self.assertEqual(config.provider.name, "ollama")
            self.assertEqual(config.provider.model, "llama3.1")

            # The OK path must NOT force-load the lazy tabs.  Loading
            # them would defeat the lazy-load latency work and block the
            # UI thread on SkillsService / MCP config scanning.
            self.assertIsNone(dlg._skills_tab)
            self.assertIsNone(dlg._mcp_tab)
            self.assertIsNone(dlg._profiles_tab)
        finally:
            dlg.done(0)

    def test_accept_applies_loaded_lazy_tabs(self) -> None:
        """If the user DID open a lazy tab, ``_on_accept()`` must call
        its ``apply_to_config()`` exactly once.  Otherwise the user's
        edits in the Skills / MCP / Profiles tabs would be silently
        dropped on save.
        """
        from rikugan.core.config import RikuganConfig
        from rikugan.ui.settings_dialog import SettingsDialog

        _ensure_qapplication()

        config = RikuganConfig()
        dlg = SettingsDialog(config)
        try:
            # Inject mocks as if the user had already opened the tabs.
            dlg._skills_tab = MagicMock()
            dlg._mcp_tab = MagicMock()
            dlg._profiles_tab = MagicMock()

            dlg._on_accept()

            dlg._skills_tab.apply_to_config.assert_called_once_with(config)
            dlg._mcp_tab.apply_to_config.assert_called_once_with(config)
            dlg._profiles_tab.apply_to_config.assert_called_once_with(config)
        finally:
            dlg.done(0)


# ----------------------------------------------------------------------------
# B. _ModelFetcher safety
# ----------------------------------------------------------------------------


@requires_real_qt
class TestModelFetcherSafety(unittest.TestCase):
    """The fetcher must not create the provider on a background thread.

    The previous revision accepted an ``ensure_ready=False`` flag for the
    automatic on-open path; that created a provider entirely in a worker
    thread, which is unsafe for Python 3.14 + heavy C-extension SDKs.
    The flag has been removed and provider / key-change handlers no
    longer auto-fetch.
    """

    def test_fetch_signature_no_longer_accepts_ensure_ready(self) -> None:
        from rikugan.providers.registry import ProviderRegistry
        from rikugan.ui.settings_dialog import _ModelFetcher

        fetcher = _ModelFetcher(ProviderRegistry())
        try:
            import inspect

            sig = inspect.signature(fetcher.fetch)
            self.assertNotIn(
                "ensure_ready",
                sig.parameters,
                "_ModelFetcher.fetch must not accept ensure_ready — "
                "the unsafe background-thread SDK import path has been removed.",
            )
        finally:
            fetcher.shutdown()

    def test_provider_changed_does_not_call_fetcher(self) -> None:
        """Switching the provider combo must NOT trigger a live fetch."""
        from rikugan.core.config import RikuganConfig
        from rikugan.ui.settings_dialog import SettingsDialog

        _ensure_qapplication()

        config = RikuganConfig()
        config.provider.name = "anthropic"
        config.provider.model = "claude-sonnet-4-20250514"
        dlg = SettingsDialog(config)
        try:
            with patch.object(dlg._fetcher, "fetch", autospec=True) as mock_fetch:
                dlg._on_provider_changed("ollama")
                mock_fetch.assert_not_called()
        finally:
            dlg.done(0)

    def test_key_edited_does_not_call_fetcher(self) -> None:
        from rikugan.core.config import RikuganConfig
        from rikugan.ui.settings_dialog import SettingsDialog

        _ensure_qapplication()

        config = RikuganConfig()
        config.provider.name = "openai"
        config.provider.model = "gpt-4o"
        config.provider.api_key = ""
        dlg = SettingsDialog(config)
        try:
            with patch.object(dlg._fetcher, "fetch", autospec=True) as mock_fetch:
                dlg._api_key_edit.setText("sk-test")
                dlg._on_key_edited()
                mock_fetch.assert_not_called()
        finally:
            dlg.done(0)

    def test_explicit_refresh_calls_fetcher(self) -> None:
        """The Refresh button is the only live-fetch trigger."""
        from rikugan.core.config import RikuganConfig
        from rikugan.ui.settings_dialog import SettingsDialog

        _ensure_qapplication()

        config = RikuganConfig()
        config.provider.name = "anthropic"
        config.provider.model = "claude-sonnet-4-20250514"
        dlg = SettingsDialog(config)
        try:
            with patch.object(dlg._fetcher, "fetch", autospec=True) as mock_fetch:
                dlg._fetch_models(explicit=True)
                mock_fetch.assert_called_once()
                # The new fetch() signature only takes the three
                # credential strings; ensure_ready is gone.
                _args, kwargs = mock_fetch.call_args
                self.assertNotIn("ensure_ready", kwargs)
        finally:
            dlg.done(0)

    def test_key_edited_preserves_manual_model_for_empty_openai_compat(self) -> None:
        """Typing a manual model into a fresh ``openai_compat`` provider
        must survive the API-key edit path.

        Reproduces the regression: with ``config.provider.model == ""``,
        ``_on_key_edited()`` re-populated the built-in list and the
        empty-models branch fell back to ``current_model`` (also
        empty), clobbering the user's typed text.  The user then
        pressed OK and the empty string got saved.
        """
        from rikugan.core.config import RikuganConfig
        from rikugan.ui.settings_dialog import SettingsDialog

        _ensure_qapplication()

        config = RikuganConfig()
        config.provider.name = "openai_compat"
        config.provider.model = ""
        config.provider.api_key = ""
        dlg = SettingsDialog(config)
        try:
            idx = dlg._provider_combo.findText("openai_compat")
            self.assertGreaterEqual(idx, 0, "openai_compat must be a built-in provider option.")
            dlg._provider_combo.setCurrentIndex(idx)
            dlg._set_manual_model_text("manual-model")

            with patch.object(dlg._fetcher, "fetch", autospec=True) as mock_fetch:
                dlg._api_key_edit.setText("sk-test")
                dlg._on_key_edited()
                # Key edits must NOT trigger a live fetch.
                mock_fetch.assert_not_called()

            # The manually typed model must survive the key-edit refresh.
            self.assertEqual(
                dlg._model_combo.currentText().strip(),
                "manual-model",
                "Editing the API key must not clobber a manually typed model for a fresh provider.",
            )
            self.assertEqual(
                dlg._get_selected_model_id(),
                "manual-model",
                "_get_selected_model_id() must return the manually typed model after a key edit.",
            )

            # Save path: pressing OK must persist the preserved model,
            # not "" and not a stale itemData from a previous provider.
            dlg._on_accept()
            self.assertEqual(config.provider.name, "openai_compat")
            self.assertEqual(
                config.provider.model,
                "manual-model",
                "_on_accept() must save the preserved manual model, not '' and not stale built-in itemData.",
            )
        finally:
            dlg.done(0)

    def test_key_edited_preserves_manual_model_for_fresh_custom_provider(self) -> None:
        """Same regression as ``test_key_edited_preserves_manual_model_for_empty_openai_compat``
        but for a user-added custom OpenAI-compatible connection.
        """
        from rikugan.core.config import RikuganConfig
        from rikugan.ui.settings_dialog import SettingsDialog

        _ensure_qapplication()

        config = RikuganConfig()
        config.add_custom_provider("deepseek")
        config.provider.name = "deepseek"
        config.provider.model = ""
        dlg = SettingsDialog(config)
        try:
            idx = dlg._provider_combo.findText("deepseek")
            self.assertGreaterEqual(idx, 0, "Custom provider must appear in the provider combo.")
            dlg._provider_combo.setCurrentIndex(idx)
            dlg._set_manual_model_text("deepseek-chat")

            with patch.object(dlg._fetcher, "fetch", autospec=True) as mock_fetch:
                dlg._api_key_edit.setText("sk-test")
                dlg._on_key_edited()
                mock_fetch.assert_not_called()

            self.assertEqual(
                dlg._model_combo.currentText().strip(),
                "deepseek-chat",
                "Editing the API key must not clobber a manually typed model for a fresh custom provider.",
            )
            self.assertEqual(
                dlg._get_selected_model_id(),
                "deepseek-chat",
                "_get_selected_model_id() must return the manually typed model for a fresh custom provider.",
            )
            dlg._on_accept()
            self.assertEqual(config.provider.name, "deepseek")
            self.assertEqual(
                config.provider.model,
                "deepseek-chat",
                "_on_accept() must save the preserved manual model for a fresh custom provider.",
            )
        finally:
            dlg.done(0)


# ----------------------------------------------------------------------------
# C. Initial / built-in model population preserves Ollama / custom model
# ----------------------------------------------------------------------------


@requires_real_qt
class TestBuiltinModelPopulation(unittest.TestCase):
    """Ollama / openai_compat / custom providers MUST NOT silently
    overwrite the configured model with the OpenAI base class's
    ``_builtin_models()`` list (which contains ``gpt-4o``).
    """

    def _build_dialog(self, provider_name: str, model: str):
        from rikugan.core.config import RikuganConfig
        from rikugan.ui.settings_dialog import SettingsDialog

        _ensure_qapplication()

        config = RikuganConfig()
        config.provider.name = provider_name
        config.provider.model = model
        dlg = SettingsDialog(config)
        return dlg, config

    def test_ollama_built_in_keeps_llama(self) -> None:
        dlg, _ = self._build_dialog("ollama", "llama3.1")
        try:
            dlg._populate_builtin_models()
            self.assertEqual(
                dlg._model_combo.currentText().strip(),
                "llama3.1",
                "Ollama built-in population must not overwrite the configured model with an OpenAI default.",
            )
        finally:
            dlg.done(0)

    def test_openai_compat_keeps_custom_model(self) -> None:
        dlg, _ = self._build_dialog("openai_compat", "my-model")
        try:
            dlg._populate_builtin_models()
            self.assertEqual(dlg._model_combo.currentText().strip(), "my-model")
        finally:
            dlg.done(0)

    def test_accept_preserves_ollama_model(self) -> None:
        dlg, config = self._build_dialog("ollama", "llama3.1")
        try:
            dlg._populate_builtin_models()
            dlg._on_accept()
            self.assertEqual(config.provider.name, "ollama")
            self.assertEqual(config.provider.model, "llama3.1")
        finally:
            dlg.done(0)

    def test_is_local_compat_provider(self) -> None:
        from rikugan.core.config import RikuganConfig
        from rikugan.providers.registry import ProviderRegistry
        from rikugan.ui.settings_dialog import SettingsDialog

        _ensure_qapplication()

        dlg = SettingsDialog(RikuganConfig())
        try:
            # The dialog needs _registry; assign a registry manually.
            dlg._registry = ProviderRegistry()
            self.assertTrue(dlg._is_local_compat_provider("ollama"))
            self.assertTrue(dlg._is_local_compat_provider("openai_compat"))
            self.assertFalse(dlg._is_local_compat_provider("anthropic"))
            self.assertFalse(dlg._is_local_compat_provider("openai"))
        finally:
            try:
                dlg.done(0)
            except Exception:
                pass

    def test_preserve_unmatched_inserts_custom_item_with_id(self) -> None:
        """When ``_on_models_ready(..., preserve_unmatched=True)`` cannot
        find the configured model in the incoming list, the dialog must
        insert/select a *custom* combo item whose ``itemData`` equals the
        preserved model id.  Previously the dialog kept ``currentIndex=0``
        while visually displaying the typed text, causing
        ``_get_selected_model_id()`` to return the first built-in model
        (e.g. ``gpt-4o``) instead of the configured custom model
        (e.g. ``custom-model-x``).
        """
        from rikugan.core.types import ModelInfo

        dlg, _ = self._build_dialog("ollama", "custom-model-x")
        try:
            builtins = [
                ModelInfo(id="gpt-4o", name="GPT-4o", provider="openai"),
                ModelInfo(id="gpt-4o-mini", name="GPT-4o Mini", provider="openai"),
            ]
            dlg._on_models_ready(builtins, preserve_unmatched=True)
            # The visible text must be the configured model, not the
            # first built-in.
            self.assertEqual(
                dlg._model_combo.currentText().strip(),
                "custom-model-x",
                "Combo must display the preserved model id, not the first built-in.",
            )
            # And the dialog's accessor must return the same id so the
            # value is the one that gets saved on OK.
            self.assertEqual(
                dlg._get_selected_model_id(),
                "custom-model-x",
                "_get_selected_model_id() must return the preserved model, not the first built-in model.",
            )
        finally:
            dlg.done(0)

    def test_accept_preserves_unmatched_custom_model(self) -> None:
        """Save-path test for the model-preservation bug.

        After ``_populate_builtin_models()`` runs with a built-in list
        that does NOT contain the configured model,
        ``_on_accept()`` must save the configured model, not the first
        built-in model.
        """
        from rikugan.core.types import ModelInfo

        dlg, config = self._build_dialog("openai_compat", "custom-model-x")
        try:
            builtins = [
                ModelInfo(id="gpt-4o", name="GPT-4o", provider="openai"),
                ModelInfo(id="gpt-4o-mini", name="GPT-4o Mini", provider="openai"),
            ]
            dlg._on_models_ready(builtins, preserve_unmatched=True)
            dlg._on_accept()
            self.assertEqual(config.provider.name, "openai_compat")
            self.assertEqual(
                config.provider.model,
                "custom-model-x",
                "Saving must persist the preserved unmatched model, not the first built-in model.",
            )
        finally:
            dlg.done(0)

    def test_provider_switch_to_empty_model_does_not_save_stale_combo_item(self) -> None:
        """Switching to a provider with no saved model must not save a
        stale ``itemData`` from the previous provider's built-ins.

        Scenario:
          1. Start with ``openai`` + ``gpt-4o`` — combo has OpenAI built-ins
             with itemData 'gpt-4o', 'gpt-4o-mini', 'o3-mini'.
          2. User switches to ``openai_compat`` (a provider with no saved
             model on the new connection).
          3. ``_on_provider_changed`` restores an empty model, and
             ``_populate_builtin_models()`` runs with no built-ins.

        Before the fix, the editable ``QComboBox`` could keep
        ``currentIndex=0`` (still pointing at ``gpt-4o``) even though
        the line edit visually showed empty text, causing
        ``_get_selected_model_id()`` to return ``'gpt-4o'`` and
        ``_on_accept()`` to persist the previous provider's model.
        """
        from rikugan.core.config import RikuganConfig
        from rikugan.core.types import ModelInfo
        from rikugan.ui.settings_dialog import SettingsDialog

        _ensure_qapplication()

        config = RikuganConfig()
        config.provider.name = "openai"
        config.provider.model = "gpt-4o"
        dlg = SettingsDialog(config)
        try:
            # Populate the OpenAI built-ins so currentIndex/itemData
            # are meaningful before the switch.
            openai_builtins = [
                ModelInfo(id="gpt-4o", name="GPT-4o", provider="openai"),
                ModelInfo(id="gpt-4o-mini", name="GPT-4o Mini", provider="openai"),
                ModelInfo(id="o3-mini", name="o3-mini", provider="openai"),
            ]
            dlg._on_models_ready(openai_builtins, preserve_unmatched=False)

            # Sanity: before the switch, the combo's current index maps
            # to a real built-in model.
            self.assertEqual(dlg._model_combo.itemData(dlg._model_combo.currentIndex()), "gpt-4o")

            # Switch to a fresh openai_compat connection.  In the real UI
            # the user changes the provider combo, which fires
            # ``currentTextChanged`` and calls ``_on_provider_changed``.
            # Replicate that flow by setting the combo text directly.
            dlg._provider_combo.setCurrentText("openai_compat")

            # The new provider has no saved model — ``switch_provider``
            # cleared ``config.provider.model``.  The combo must NOT
            # still be pointing at the previous provider's first model.
            self.assertEqual(
                config.provider.model,
                "",
                "switch_provider to a fresh provider must clear the saved model.",
            )
            self.assertEqual(
                dlg._get_selected_model_id(),
                "",
                "_get_selected_model_id() must return the empty/manual "
                "value, not the previous provider's first built-in model.",
            )
            self.assertEqual(
                dlg._model_combo.currentText().strip(),
                "",
                "The combo line edit must show the empty/manual value, not stale text from the previous provider.",
            )

            # Save path — pressing OK must persist the empty model,
            # NOT a stale built-in id from the previous provider.
            dlg._on_accept()
            self.assertEqual(config.provider.name, "openai_compat")
            self.assertEqual(
                config.provider.model,
                "",
                "_on_accept() must save the empty model after switching "
                "to a fresh provider, not the previous provider's stale built-in model.",
            )
        finally:
            dlg.done(0)

    def test_populate_builtin_models_with_empty_list_clears_stale_selection(self) -> None:
        """``_populate_builtin_models()`` with ``models == []`` and an
        empty ``current_model`` must leave the combo in a state where
        ``_get_selected_model_id()`` returns ``''`` — never a stale
        ``itemData`` from a previous provider.
        """
        # First populate with some OpenAI built-ins so the combo has
        # a non-empty itemData set.
        from rikugan.core.config import RikuganConfig
        from rikugan.core.types import ModelInfo
        from rikugan.ui.settings_dialog import SettingsDialog

        _ensure_qapplication()

        config = RikuganConfig()
        config.provider.name = "openai"
        config.provider.model = "gpt-4o"
        dlg = SettingsDialog(config)
        try:
            dlg._on_models_ready(
                [
                    ModelInfo(id="gpt-4o", name="GPT-4o", provider="openai"),
                    ModelInfo(id="gpt-4o-mini", name="GPT-4o Mini", provider="openai"),
                ],
                preserve_unmatched=False,
            )

            # Simulate a provider switch to a fresh local-compat
            # provider with no saved model: clear the config model,
            # then re-run the built-in population which will yield
            # ``models == []`` (no OpenAI fallthrough for compat
            # providers, and no current_model to seed the list).
            config.provider.model = ""
            config.provider.name = "ollama"
            dlg._provider_combo.setCurrentText("ollama")
            dlg._populate_builtin_models()

            # The accessor must return the empty/manual value, never
            # a stale itemData from the previous provider.
            self.assertEqual(
                dlg._get_selected_model_id(),
                "",
                "_populate_builtin_models() with no models and an empty "
                "current model must clear the stale combo itemData.",
            )
        finally:
            dlg.done(0)

    def test_custom_provider_keeps_custom_model(self) -> None:
        """A user-added custom provider registered via
        ``config.custom_providers`` must preserve its configured model
        through ``_populate_builtin_models()`` and ``_on_accept()``.
        """
        from rikugan.core.config import RikuganConfig
        from rikugan.ui.settings_dialog import SettingsDialog

        _ensure_qapplication()

        config = RikuganConfig()
        config.add_custom_provider("deepseek")
        config.provider.name = "deepseek"
        config.provider.model = "deepseek-chat"
        dlg = SettingsDialog(config)
        try:
            dlg._populate_builtin_models()
            self.assertEqual(
                dlg._model_combo.currentText().strip(),
                "deepseek-chat",
                "Custom provider built-in population must keep the configured model visible in the combo.",
            )
            self.assertEqual(
                dlg._get_selected_model_id(),
                "deepseek-chat",
                "_get_selected_model_id() must return the configured custom-provider model, not a built-in fallback.",
            )
            dlg._on_accept()
            self.assertEqual(config.provider.name, "deepseek")
            self.assertEqual(
                config.provider.model,
                "deepseek-chat",
                "Saving must persist the configured custom-provider model.",
            )
        finally:
            dlg.done(0)


# ----------------------------------------------------------------------------
# D. Anthropic message_delta output_tokens coercion
# ----------------------------------------------------------------------------


class TestAnthropicMessageDeltaCoercion(unittest.TestCase):
    """The Anthropic streaming code must coerce ``output_tokens`` so a
    string value (e.g. ``"12"``) or ``None`` does not raise, and must
    emit (or suppress) the resulting usage chunk correctly.

    These tests drive ``AnthropicProvider._stream_chunks()`` directly
    with a fake streaming client, so a regression in the production
    ``message_delta`` handling path is caught even if the helper
    ``coerce_token_count`` is intact.
    """

    def test_anthropic_message_delta_string_output_tokens_emits_usage(self) -> None:
        from rikugan.providers.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(api_key="sk-test", model="claude-sonnet-4-6")
        events = [
            SimpleNamespace(
                type="message_delta",
                delta=SimpleNamespace(stop_reason=None),
                usage=SimpleNamespace(output_tokens="12"),
            )
        ]
        chunks = list(provider._stream_chunks(_FakeAnthropicClient(events), {}))
        usage_chunks = [c for c in chunks if c.usage is not None]
        self.assertEqual(
            len(usage_chunks),
            1,
            f"Expected exactly one usage chunk for output_tokens='12', "
            f"got {len(usage_chunks)}: {[c.usage for c in usage_chunks]}",
        )
        self.assertEqual(usage_chunks[0].usage.completion_tokens, 12)

    def test_anthropic_message_delta_none_output_tokens_does_not_raise(self) -> None:
        from rikugan.providers.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(api_key="sk-test", model="claude-sonnet-4-6")
        events = [
            SimpleNamespace(
                type="message_delta",
                delta=SimpleNamespace(stop_reason=None),
                usage=SimpleNamespace(output_tokens=None),
            )
        ]
        # Must not raise (e.g. TypeError from `None > 0`).
        chunks = list(provider._stream_chunks(_FakeAnthropicClient(events), {}))
        usage_chunks = [c for c in chunks if c.usage is not None]
        # ``None`` is coerced to 0; the production code only emits a
        # usage chunk for positive token counts.
        self.assertEqual(usage_chunks, [])


class TestModelFetcherOrdering(unittest.TestCase):
    """``_ModelFetcher.fetch()`` MUST:

    1. create the provider on the calling (main) thread,
    2. call ``ensure_ready()`` on the main thread,
    3. THEN hand the provider to a worker thread for ``list_models()``.

    The previous unsafe path created the provider entirely inside the
    background thread, which crashes on Python 3.14 when heavy C-extension
    SDK packages are first imported there.  These tests record the call
    order via fakes so a regression moves the test back to failure.
    """

    def _build_fakes(self):
        calls: list[str] = []
        thread_calls: list[tuple[str, int]] = []

        class _FakeProvider:
            def ensure_ready(self):
                calls.append("ensure_ready")
                thread_calls.append(("ensure_ready", threading.get_ident()))

            def list_models(self):
                calls.append("list_models")
                thread_calls.append(("list_models", threading.get_ident()))
                return []

        class _FakeRegistry:
            def new_instance(self, provider_name, api_key="", api_base="", **_kwargs):
                calls.append("new_instance")
                thread_calls.append(("new_instance", threading.get_ident()))
                return _FakeProvider()

        return calls, thread_calls, _FakeRegistry

    def test_fetch_thread_invariants_and_call_order(self) -> None:
        """Combined ordering + thread + result assertion.

        Verifies the full safety contract for ``_ModelFetcher.fetch()``:

        * ``new_instance`` runs on the caller thread,
        * ``ensure_ready()`` runs on the caller thread,
        * ``list_models()`` runs on a different (worker) thread,
        * call order is ``[new_instance, ensure_ready, list_models]``,
        * the polled result is a successful ``("models", "fake", [])``
          tuple (no error tuple).
        """
        from rikugan.ui.settings_dialog import _ModelFetcher

        calls, thread_calls, FakeRegistry = self._build_fakes()
        fetcher = _ModelFetcher(FakeRegistry())
        try:
            caller_ident = threading.get_ident()
            fetcher.fetch("fake", "key", "base")

            # Poll the result queue deterministically (no long sleeps).
            deadline = time.time() + 2.0
            result = None
            while time.time() < deadline:
                result = fetcher.poll()
                if result is not None:
                    break
                time.sleep(0.01)

            self.assertIsNotNone(result, "_ModelFetcher never produced a result")
            self.assertEqual(
                result,
                ("models", "fake", []),
                "Polled fetcher result must be a successful models tuple, not an error.",
            )
            self.assertEqual(
                calls,
                ["new_instance", "ensure_ready", "list_models"],
                "Provider must be created and ensure_ready() must run on the "
                "main thread BEFORE the worker thread invokes list_models().",
            )

            # Thread assertions: provider creation and ensure_ready must
            # run on the caller/main thread; list_models must NOT.
            thread_by_call = dict(thread_calls)
            self.assertEqual(
                thread_by_call.get("new_instance"),
                caller_ident,
                "Provider creation (new_instance) must run on the caller thread.",
            )
            self.assertEqual(
                thread_by_call.get("ensure_ready"),
                caller_ident,
                "ensure_ready() must run on the caller thread to keep C-extension SDK imports off the worker thread.",
            )
            self.assertNotEqual(
                thread_by_call.get("list_models"),
                caller_ident,
                "list_models() must run on the worker thread, not the caller thread.",
            )
        finally:
            fetcher.shutdown()


# ----------------------------------------------------------------------------
# E. OpenAI streaming usage is yielded once, not overcounted
# ----------------------------------------------------------------------------


class TestOpenAIStreamingCumulativeUsage(unittest.TestCase):
    """The OpenAI streaming implementation must yield a single
    ``StreamChunk(usage=...)`` for cumulative usage — not one per chunk.
    The agent loop's accumulator treats each chunk as a delta; multiple
    chunks with cumulative usage would overcount by N times.
    """

    def test_cumulative_usage_on_multiple_chunks_produces_one_chunk(self) -> None:
        provider = OpenAIProvider(api_key="x", model="gpt-4o")

        # Three content chunks, each carrying a populated ``usage``
        # field (as some OpenAI-compatible proxies do).  The values
        # are *cumulative*: each chunk's completion_tokens is the
        # total so far, not a delta.  The implementation must yield
        # the most recent cumulative usage exactly once after the
        # stream.
        chunk1 = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="hi", reasoning_content=None, tool_calls=None),
                    finish_reason=None,
                )
            ],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=3, total_tokens=103),
        )
        chunk2 = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=" ", reasoning_content=None, tool_calls=None),
                    finish_reason=None,
                )
            ],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=7, total_tokens=107),
        )
        chunk3 = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="there", reasoning_content=None, tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10, total_tokens=110),
        )

        def fake_create(**_kwargs):
            return iter([chunk1, chunk2, chunk3])

        fake_client = MagicMock()
        fake_client.chat.completions.create = fake_create

        kwargs = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16,
            "temperature": 0.0,
        }
        chunks = list(provider._stream_chunks(fake_client, kwargs))
        usage_chunks = [c for c in chunks if c.usage is not None]
        self.assertEqual(
            len(usage_chunks),
            1,
            f"Expected exactly one usage chunk for cumulative usage, "
            f"got {len(usage_chunks)}: {[c.usage for c in usage_chunks]}",
        )
        # The values must be the latest cumulative ones
        # (100/10/110), not additive sums or intermediate snapshots.
        self.assertEqual(usage_chunks[0].usage.prompt_tokens, 100)
        self.assertEqual(usage_chunks[0].usage.completion_tokens, 10)
        self.assertEqual(usage_chunks[0].usage.total_tokens, 110)

    def test_duplicate_final_usage_only_chunks_emit_one_usage_chunk(self) -> None:
        """Some OpenAI-compatible proxies emit the final usage-only
        chunk more than once.  The streaming code must not yield more
        than one usage ``StreamChunk`` — the agent loop accumulator
        would otherwise sum the duplicates and overcount.
        """
        provider = OpenAIProvider(api_key="x", model="gpt-4o")
        content_chunk = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="hi", reasoning_content=None, tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=5, total_tokens=105),
        )
        usage_chunk_a = SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=5, total_tokens=105),
        )
        usage_chunk_b = SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=5, total_tokens=105),
        )

        def fake_create(**_kwargs):
            return iter([content_chunk, usage_chunk_a, usage_chunk_b])

        fake_client = MagicMock()
        fake_client.chat.completions.create = fake_create

        kwargs = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16,
            "temperature": 0.0,
        }
        chunks = list(provider._stream_chunks(fake_client, kwargs))
        usage_chunks = [c for c in chunks if c.usage is not None]
        self.assertEqual(
            len(usage_chunks),
            1,
            f"Duplicate usage-only chunks must collapse to a single "
            f"usage StreamChunk, got {len(usage_chunks)}: "
            f"{[c.usage for c in usage_chunks]}",
        )
        self.assertEqual(usage_chunks[0].usage.prompt_tokens, 100)
        self.assertEqual(usage_chunks[0].usage.completion_tokens, 5)
        self.assertEqual(usage_chunks[0].usage.total_tokens, 105)

    def test_final_usage_only_chunk_is_yielded(self) -> None:
        # Sanity check that the official final usage-only chunk is
        # also handled.
        provider = OpenAIProvider(api_key="x", model="gpt-4o")
        content_chunk = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="hello", reasoning_content=None, tool_calls=None),
                    finish_reason=None,
                )
            ],
            usage=None,
        )
        usage_chunk = SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=7, total_tokens=19),
        )

        def fake_create(**_kwargs):
            return iter([content_chunk, usage_chunk])

        fake_client = MagicMock()
        fake_client.chat.completions.create = fake_create

        kwargs = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16,
            "temperature": 0.0,
        }
        chunks = list(provider._stream_chunks(fake_client, kwargs))
        usage_chunks = [c for c in chunks if c.usage is not None]
        self.assertEqual(len(usage_chunks), 1)
        self.assertEqual(usage_chunks[0].usage.prompt_tokens, 12)


# ----------------------------------------------------------------------------
# F. OpenAI retryable transient error mapping
# ----------------------------------------------------------------------------


class TestOpenAIRetryableErrorMapping(unittest.TestCase):
    """``_handle_api_error`` must raise ``ProviderError(retryable=True)``
    for transient errors (connection, timeout, 5xx) and
    ``AuthenticationError`` / ``RateLimitError`` / ``ContextLengthError``
    for the well-known cases.
    """

    def _provider(self) -> OpenAIProvider:
        return OpenAIProvider(api_key="x", model="gpt-4o")

    def test_api_connection_error_is_retryable(self) -> None:
        from rikugan.core.errors import ProviderError

        provider = self._provider()
        fake_openai = SimpleNamespace(
            AuthenticationError=type("AuthErr", (Exception,), {}),
            RateLimitError=type("RLE", (Exception,), {}),
            BadRequestError=type("BRE", (Exception,), {}),
            APIConnectionError=type("ACE", (Exception,), {}),
            APITimeoutError=type("ATE", (Exception,), {}),
            APIStatusError=type("ASE", (Exception,), {}),
        )
        err = fake_openai.APIConnectionError("conn refused")
        with patch.dict("sys.modules", {"openai": fake_openai}):
            with self.assertRaises(ProviderError) as ctx:
                provider._handle_api_error(err)
        self.assertTrue(ctx.exception.retryable)

    def test_api_timeout_error_is_retryable(self) -> None:
        from rikugan.core.errors import ProviderError

        provider = self._provider()
        fake_openai = SimpleNamespace(
            AuthenticationError=type("AuthErr", (Exception,), {}),
            RateLimitError=type("RLE", (Exception,), {}),
            BadRequestError=type("BRE", (Exception,), {}),
            APIConnectionError=type("ACE", (Exception,), {}),
            APITimeoutError=type("ATE", (Exception,), {}),
            APIStatusError=type("ASE", (Exception,), {}),
        )
        err = fake_openai.APITimeoutError("timeout")
        with patch.dict("sys.modules", {"openai": fake_openai}):
            with self.assertRaises(ProviderError) as ctx:
                provider._handle_api_error(err)
        self.assertTrue(ctx.exception.retryable)

    def test_5xx_status_error_is_retryable(self) -> None:
        from rikugan.core.errors import ProviderError

        provider = self._provider()
        fake_openai = SimpleNamespace(
            AuthenticationError=type("AuthErr", (Exception,), {}),
            RateLimitError=type("RLE", (Exception,), {}),
            BadRequestError=type("BRE", (Exception,), {}),
            APIConnectionError=type("ACE", (Exception,), {}),
            APITimeoutError=type("ATE", (Exception,), {}),
            APIStatusError=type("ASE", (Exception,), {}),
        )

        class SrvErr(fake_openai.APIStatusError):
            def __init__(self):
                self.status_code = 502
                super().__init__("bad gateway")

        with patch.dict("sys.modules", {"openai": fake_openai}):
            with self.assertRaises(ProviderError) as ctx:
                provider._handle_api_error(SrvErr())
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(ctx.exception.status_code, 502)

    def test_4xx_status_error_is_not_retryable(self) -> None:
        from rikugan.core.errors import ProviderError

        provider = self._provider()
        fake_openai = SimpleNamespace(
            AuthenticationError=type("AuthErr", (Exception,), {}),
            RateLimitError=type("RLE", (Exception,), {}),
            BadRequestError=type("BRE", (Exception,), {}),
            APIConnectionError=type("ACE", (Exception,), {}),
            APITimeoutError=type("ATE", (Exception,), {}),
            APIStatusError=type("ASE", (Exception,), {}),
        )

        class CliErr(fake_openai.APIStatusError):
            def __init__(self):
                self.status_code = 400
                super().__init__("bad request")

        with patch.dict("sys.modules", {"openai": fake_openai}):
            with self.assertRaises(ProviderError) as ctx:
                provider._handle_api_error(CliErr())
        self.assertFalse(ctx.exception.retryable)

    def test_authentication_error_passthrough(self) -> None:
        from rikugan.core.errors import AuthenticationError

        provider = self._provider()
        fake_openai = SimpleNamespace(
            AuthenticationError=type("AuthErr", (Exception,), {}),
            RateLimitError=type("RLE", (Exception,), {}),
            BadRequestError=type("BRE", (Exception,), {}),
            APIConnectionError=type("ACE", (Exception,), {}),
            APITimeoutError=type("ATE", (Exception,), {}),
            APIStatusError=type("ASE", (Exception,), {}),
        )
        err = fake_openai.AuthenticationError("bad key")
        with patch.dict("sys.modules", {"openai": fake_openai}):
            with self.assertRaises(AuthenticationError):
                provider._handle_api_error(err)

    def test_rate_limit_error_passthrough(self) -> None:
        from rikugan.core.errors import RateLimitError

        provider = self._provider()
        fake_openai = SimpleNamespace(
            AuthenticationError=type("AuthErr", (Exception,), {}),
            RateLimitError=type("RLE", (Exception,), {}),
            BadRequestError=type("BRE", (Exception,), {}),
            APIConnectionError=type("ACE", (Exception,), {}),
            APITimeoutError=type("ATE", (Exception,), {}),
            APIStatusError=type("ASE", (Exception,), {}),
        )
        err = fake_openai.RateLimitError("rate limited")
        with patch.dict("sys.modules", {"openai": fake_openai}):
            with self.assertRaises(RateLimitError):
                provider._handle_api_error(err)

    def test_api_timeout_error_subclass_of_connection_error_uses_timeout_message(self) -> None:
        """The real OpenAI SDK defines ``APITimeoutError`` as a subclass
        of ``APIConnectionError``.  The handler must classify such
        errors as timeouts (not generic connection errors) so the
        user sees the more precise timeout-specific message.
        """
        from rikugan.core.errors import ProviderError

        provider = self._provider()
        # Build the class graph the way the real SDK does.
        APIConnectionError = type("ACE", (Exception,), {})
        APITimeoutError = type("ATE", (APIConnectionError,), {})
        fake_openai = SimpleNamespace(
            AuthenticationError=type("AuthErr", (Exception,), {}),
            RateLimitError=type("RLE", (Exception,), {}),
            BadRequestError=type("BRE", (Exception,), {}),
            APIConnectionError=APIConnectionError,
            APITimeoutError=APITimeoutError,
            APIStatusError=type("ASE", (Exception,), {}),
        )
        err = APITimeoutError("socket timeout")
        with patch.dict("sys.modules", {"openai": fake_openai}):
            with self.assertRaises(ProviderError) as ctx:
                provider._handle_api_error(err)
        self.assertTrue(ctx.exception.retryable)
        self.assertIn(
            "timed out",
            str(ctx.exception),
            "APITimeoutError must produce the timeout-specific message, not the generic 'Connection error' branch.",
        )


# ----------------------------------------------------------------------------
# G. _AddButtonTabBar uses theme-aware stylesheet
# ----------------------------------------------------------------------------


@requires_real_qt
class TestAddButtonTabBarTheme(unittest.TestCase):
    """The ``+`` button on the tab bar must reflect the current theme
    palette (light or dark) by re-applying its inline stylesheet from
    ``ThemeManager.tokens()`` rather than a hard-coded dark stylesheet.
    """

    def test_uses_add_tab_btn_style_for_current_theme(self) -> None:
        from rikugan.ui import styles
        from rikugan.ui.panel_core import _AddButtonTabBar
        from rikugan.ui.theme.manager import ThemeManager
        from rikugan.ui.theme.tokens import ThemeMode

        _ensure_qapplication()

        # Snapshot the global theme state so the test does not leak
        # its intermediate "light" / "dark" flips to later tests.  There
        # is no public getter, so read the module attributes directly.
        old_current = styles._current_theme
        old_effective = styles._effective_theme
        old_mgr_mode = ThemeManager.instance().mode
        try:
            # Switch to light theme and confirm the add button picks up
            # the light palette tokens.
            styles.set_current_theme("light")
            ThemeManager.instance().set_mode(ThemeMode.LIGHT)
            bar = _AddButtonTabBar()
            try:
                bar.refresh_inline_styles()
                light_tokens = ThemeManager.instance().tokens()
                self.assertIn(
                    light_tokens.text,
                    bar._add_btn.styleSheet(),
                    "Light theme should inject the dark-text color into the add button.",
                )
            finally:
                bar.deleteLater()
                styles.set_current_theme("dark")
                ThemeManager.instance().set_mode(ThemeMode.DARK)
                bar2 = _AddButtonTabBar()
            try:
                bar2.refresh_inline_styles()
                dark_tokens = ThemeManager.instance().tokens()
                self.assertIn(
                    dark_tokens.text,
                    bar2._add_btn.styleSheet(),
                    "Dark theme should inject the light-text color into the add button.",
                )
            finally:
                bar2.deleteLater()
        finally:
            # Restore the theme that was in effect before the test ran,
            # regardless of whether the assertions above passed or failed.
            styles.set_current_theme(old_current, old_effective)
            ThemeManager.instance().set_mode(old_mgr_mode)


# ----------------------------------------------------------------------------
# H. OpenAI duplicate tool_call id regression
# ----------------------------------------------------------------------------


class TestOpenAIDuplicateToolCallIdStreaming(unittest.TestCase):
    """OpenAI's streaming code must never emit more than one
    ``is_tool_call_end`` event for the same tool-call id, even when
    the upstream proxy re-emits the same final tool-call state.

    Background: a recent runtime failure surfaced as
    ``openai: Error code: 400 - invalid params, duplicate tool_call
    id: call_function_...``.  The duplicate id came from a persisted
    assistant message that had two ``ToolCall`` entries with the
    same id — a direct consequence of the streaming code yielding
    a duplicate ``is_tool_call_end`` for the same id, which the
    agent loop then appended as a duplicate ``ToolCall`` to
    ``tool_calls``.  Fixing the bug at the source keeps the
    session history clean.
    """

    def test_duplicate_finish_chunks_emit_one_tool_call_end(self) -> None:
        """A stream that re-emits the same final tool-call state
        twice (e.g. a proxy that flushes twice on keep-alive
        boundaries) must yield exactly one
        ``StreamChunk(is_tool_call_end=True)`` per tool-call id."""
        provider = OpenAIProvider(api_key="x", model="gpt-4o")

        # First final chunk: start + args + finish.
        start_chunk = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call_function_nc4eo99zfk3n_1",
                                function=SimpleNamespace(
                                    name="do_thing",
                                    arguments='{"x":',
                                ),
                            )
                        ],
                    ),
                    finish_reason=None,
                )
            ],
            usage=None,
        )
        args_chunk = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id=None,
                                function=SimpleNamespace(
                                    name=None,
                                    arguments="1}",
                                ),
                            )
                        ],
                    ),
                    finish_reason=None,
                )
            ],
            usage=None,
        )
        finish_chunk = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content=None,
                        tool_calls=None,
                    ),
                    finish_reason="tool_calls",
                )
            ],
            usage=None,
        )
        # Duplicate final chunk: same finish_reason, no new
        # content.  This is the regression case.
        duplicate_finish_chunk = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content=None,
                        tool_calls=None,
                    ),
                    finish_reason="tool_calls",
                )
            ],
            usage=None,
        )
        usage_chunk = SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

        def fake_create(**_kwargs):
            return iter(
                [
                    start_chunk,
                    args_chunk,
                    finish_chunk,
                    duplicate_finish_chunk,
                    usage_chunk,
                ]
            )

        fake_client = MagicMock()
        fake_client.chat.completions.create = fake_create

        kwargs = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16,
            "temperature": 0.0,
        }
        chunks = list(provider._stream_chunks(fake_client, kwargs))
        end_chunks = [c for c in chunks if c.is_tool_call_end]
        self.assertEqual(
            len(end_chunks),
            1,
            f"Duplicate finish chunks must collapse to one end event, "
            f"got {len(end_chunks)}: {[c.tool_call_id for c in end_chunks]}",
        )
        self.assertEqual(end_chunks[0].tool_call_id, "call_function_nc4eo99zfk3n_1")
        # Start event still emitted once.
        start_chunks = [c for c in chunks if c.is_tool_call_start]
        self.assertEqual(len(start_chunks), 1)

    def test_finish_reason_duplicate_emitted_only_once(self) -> None:
        """The ``finish_reason`` must also not be re-emitted on a
        duplicate final chunk."""
        provider = OpenAIProvider(api_key="x", model="gpt-4o")
        start_chunk = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call_a",
                                function=SimpleNamespace(name="f", arguments="{}"),
                            )
                        ],
                    ),
                    finish_reason=None,
                )
            ],
            usage=None,
        )
        finish_a = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content=None,
                        tool_calls=None,
                    ),
                    finish_reason="tool_calls",
                )
            ],
            usage=None,
        )
        finish_b = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content=None,
                        tool_calls=None,
                    ),
                    finish_reason="tool_calls",
                )
            ],
            usage=None,
        )

        def fake_create(**_kwargs):
            return iter([start_chunk, finish_a, finish_b])

        fake_client = MagicMock()
        fake_client.chat.completions.create = fake_create

        kwargs = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16,
            "temperature": 0.0,
        }
        chunks = list(provider._stream_chunks(fake_client, kwargs))
        finish_chunks = [c for c in chunks if c.finish_reason]
        self.assertEqual(
            len(finish_chunks),
            1,
            "Duplicate finish_reason chunks must collapse to one StreamChunk.",
        )

    def test_empty_tool_call_id_does_not_emit_end(self) -> None:
        """If a tool call is somehow left with an empty id (e.g.
        a broken proxy never supplies one), the end event must
        be skipped — emitting an end for ``""`` would cause the
        agent loop to append a ``ToolCall`` with ``id=""`` and
        OpenAI would reject the next request."""
        provider = OpenAIProvider(api_key="x", model="gpt-4o")
        # Start chunk with no id (only name) — some proxies
        # split id and name across two deltas.
        start_chunk = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id=None,  # no id on first delta
                                function=SimpleNamespace(name="f", arguments=None),
                            )
                        ],
                    ),
                    finish_reason=None,
                )
            ],
            usage=None,
        )
        args_chunk = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call_x",
                                function=SimpleNamespace(name=None, arguments="{}"),
                            )
                        ],
                    ),
                    finish_reason=None,
                )
            ],
            usage=None,
        )
        finish_chunk = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content=None,
                        tool_calls=None,
                    ),
                    finish_reason="tool_calls",
                )
            ],
            usage=None,
        )

        def fake_create(**_kwargs):
            return iter([start_chunk, args_chunk, finish_chunk])

        fake_client = MagicMock()
        fake_client.chat.completions.create = fake_create

        kwargs = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16,
            "temperature": 0.0,
        }
        chunks = list(provider._stream_chunks(fake_client, kwargs))
        end_chunks = [c for c in chunks if c.is_tool_call_end]
        self.assertEqual(
            len(end_chunks),
            1,
            "Empty-id end events must be skipped, otherwise the agent loop persists a ToolCall with id=''.",
        )
        self.assertEqual(end_chunks[0].tool_call_id, "call_x")


class TestOpenAIFormatMessagesRepair(unittest.TestCase):
    """``_format_messages`` must repair duplicate / missing
    ``tool_calls[].id`` values before sending the request to
    OpenAI, because the API rejects duplicate ids with HTTP 400
    (``invalid params, duplicate tool_call id``).
    """

    def test_duplicate_ids_in_one_assistant_message_are_rewritten(self) -> None:
        provider = OpenAIProvider(api_key="x", model="gpt-4o")
        from rikugan.core.types import ToolCall, ToolResult

        # Assistant message with two tool calls that share the
        # same id (regression case from a corrupt session).
        msgs = [
            Message(role=Role.USER, content="hi"),
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[
                    ToolCall(id="dup", name="f1", arguments={}),
                    ToolCall(id="dup", name="f2", arguments={}),
                ],
            ),
            Message(
                role=Role.TOOL,
                tool_results=[
                    ToolResult(tool_call_id="dup", name="f1", content="r1"),
                    ToolResult(tool_call_id="dup", name="f2", content="r2"),
                ],
            ),
        ]
        formatted = provider._format_messages(msgs)
        # Two assistant tool_calls, both rewritten to unique ids.
        assistant = next(m for m in formatted if m["role"] == "assistant")
        ids = [tc["id"] for tc in assistant["tool_calls"]]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2, "All formatted assistant tool_call ids must be unique.")
        # The first id is unique within the request, so it is
        # preserved.  The second collides, so it must be replaced
        # with a synthesized id.
        self.assertEqual(ids[0], "dup")
        self.assertNotIn("dup", ids[1:], "Duplicate id must be replaced with a unique synthesized id.")

        tool_msgs = [m for m in formatted if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 2)
        for tm in tool_msgs:
            self.assertIn(
                tm["tool_call_id"],
                ids,
                f"Tool result tool_call_id={tm['tool_call_id']!r} must reference a formatted assistant id.",
            )

    def test_missing_id_is_generated(self) -> None:
        provider = OpenAIProvider(api_key="x", model="gpt-4o")
        from rikugan.core.types import ToolCall, ToolResult

        msgs = [
            Message(role=Role.USER, content="hi"),
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="", name="f", arguments={})],
            ),
            Message(
                role=Role.TOOL,
                tool_results=[ToolResult(tool_call_id="", name="f", content="r")],
            ),
        ]
        formatted = provider._format_messages(msgs)
        assistant = next(m for m in formatted if m["role"] == "assistant")
        self.assertEqual(len(assistant["tool_calls"]), 1)
        new_id = assistant["tool_calls"][0]["id"]
        self.assertTrue(new_id, "Missing tool_call id must be replaced with a non-empty value.")
        # The corresponding tool result must reference the same id.
        tool_msg = next(m for m in formatted if m["role"] == "tool")
        self.assertEqual(tool_msg["tool_call_id"], new_id)

    def test_duplicate_ids_across_turns_are_repaired(self) -> None:
        """A restored session that reuses the same tool_call id
        across multiple turns (e.g. a tool that always uses
        ``call_X``) must not produce a request with duplicate
        assistant tool_calls[].id values."""
        provider = OpenAIProvider(api_key="x", model="gpt-4o")
        from rikugan.core.types import ToolCall, ToolResult

        msgs = [
            Message(role=Role.USER, content="hi"),
            # First turn: id="X" used.
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="X", name="f", arguments={})],
            ),
            Message(
                role=Role.TOOL,
                tool_results=[ToolResult(tool_call_id="X", name="f", content="ok")],
            ),
            Message(role=Role.USER, content="again"),
            # Second turn: same id "X" — the request-formatter
            # must repair this duplicate.
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="X", name="f", arguments={})],
            ),
            Message(
                role=Role.TOOL,
                tool_results=[ToolResult(tool_call_id="X", name="f", content="ok2")],
            ),
        ]
        formatted = provider._format_messages(msgs)
        all_assistant_ids = [tc["id"] for m in formatted if m["role"] == "assistant" for tc in m.get("tool_calls", [])]
        self.assertEqual(len(all_assistant_ids), 2)
        self.assertEqual(
            len(set(all_assistant_ids)),
            2,
            f"Duplicate id reused across turns must be repaired; got {all_assistant_ids}",
        )
        # Every tool result still references a real assistant id.
        assistant_id_set = set(all_assistant_ids)
        for tm in (m for m in formatted if m["role"] == "tool"):
            self.assertIn(
                tm["tool_call_id"],
                assistant_id_set,
                f"Tool result id={tm['tool_call_id']!r} must reference a valid assistant id.",
            )

    def test_valid_unique_history_is_unchanged(self) -> None:
        """A well-formed history with unique tool_call ids must
        pass through ``_format_messages`` unchanged (no
        rewriting, no synthesized ids)."""
        provider = OpenAIProvider(api_key="x", model="gpt-4o")
        from rikugan.core.types import ToolCall, ToolResult

        msgs = [
            Message(role=Role.USER, content="hi"),
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[
                    ToolCall(id="call_a", name="f1", arguments={"x": 1}),
                    ToolCall(id="call_b", name="f2", arguments={"y": 2}),
                ],
            ),
            Message(
                role=Role.TOOL,
                tool_results=[
                    ToolResult(tool_call_id="call_a", name="f1", content="r1"),
                    ToolResult(tool_call_id="call_b", name="f2", content="r2"),
                ],
            ),
        ]
        formatted = provider._format_messages(msgs)
        assistant = next(m for m in formatted if m["role"] == "assistant")
        ids = sorted(tc["id"] for tc in assistant["tool_calls"])
        self.assertEqual(ids, ["call_a", "call_b"])
        # Tool result ids must also be unchanged.
        tool_ids = sorted(m["tool_call_id"] for m in formatted if m["role"] == "tool")
        self.assertEqual(tool_ids, ["call_a", "call_b"])

    def test_format_messages_does_not_mutate_input(self) -> None:
        """The repair logic must produce new dicts; the original
        ``Message.tool_calls`` and ``Message.tool_results`` lists
        must keep their original ids intact."""
        provider = OpenAIProvider(api_key="x", model="gpt-4o")
        from rikugan.core.types import ToolCall, ToolResult

        tc1 = ToolCall(id="dup", name="f1", arguments={})
        tc2 = ToolCall(id="dup", name="f2", arguments={})
        tr1 = ToolResult(tool_call_id="dup", name="f1", content="r1")
        tr2 = ToolResult(tool_call_id="dup", name="f2", content="r2")
        assistant = Message(role=Role.ASSISTANT, content="", tool_calls=[tc1, tc2])
        tool = Message(role=Role.TOOL, tool_results=[tr1, tr2])
        msgs = [assistant, tool]
        provider._format_messages(msgs)
        self.assertEqual([tc.id for tc in assistant.tool_calls], ["dup", "dup"])
        self.assertEqual([tr.tool_call_id for tr in tool.tool_results], ["dup", "dup"])


class TestAgentLoopDuplicateToolCallIdGuard(unittest.TestCase):
    """The agent loop must guard against duplicate tool-call-end
    events: only one ``ToolCall`` may be appended per id, and only
    one ``tool_call_done`` event may be emitted.  This keeps the
    session history clean even if a provider adapter re-emits
    the same final state.

    The dedup logic now lives in :meth:`AgentLoop._is_duplicate_tool_call_end`
    so the test exercises production code directly.  Re-implementing
    the dedup branch in the test body would silently diverge from
    production if the helper ever changes.
    """

    def test_duplicate_end_chunks_return_one_tool_call(self) -> None:
        """Drive the agent loop's tool-call-end guard directly by
        feeding it duplicate ``is_tool_call_end`` chunks for the
        same id and asserting the persisted tool list contains
        exactly one entry for that id."""
        from rikugan.agent.loop import AgentLoop
        from rikugan.core.types import ToolCall

        completed_ids: set[str] = set()
        tool_calls: list[ToolCall] = []

        def _process(tc_id: str) -> None:
            """Mirror the production dedup branch exactly."""
            if AgentLoop._is_duplicate_tool_call_end(tc_id, completed_ids):
                return
            completed_ids.add(tc_id)
            tool_calls.append(ToolCall(id=tc_id, name="f", arguments={}))

        # First end: must be recorded.
        _process("call_x")
        # Duplicate end: must be ignored by the production guard.
        _process("call_x")
        # Different id: must be recorded.
        _process("call_y")

        self.assertEqual(
            len(tool_calls),
            2,
            f"Expected 2 tool calls (one per unique id); got {len(tool_calls)}: {tool_calls!r}",
        )
        self.assertEqual(tool_calls[0].id, "call_x")
        self.assertEqual(tool_calls[1].id, "call_y")
        # The completed_ids set should mirror the tool_calls list
        # — the production helper must NOT add a duplicate id to
        # the set (a regression in the helper could break that
        # contract and silently still dedupe the next chunk).
        self.assertEqual(completed_ids, {"call_x", "call_y"})

    def test_helper_is_pure_check(self) -> None:
        """The helper is a no-side-effect check; mutating the set
        is the caller's responsibility.  Pin that contract so a
        future refactor cannot silently add the duplicate to the
        set after returning True."""
        from rikugan.agent.loop import AgentLoop

        seen: set[str] = set()
        # First call returns False and does not mutate.
        self.assertFalse(AgentLoop._is_duplicate_tool_call_end("c1", seen))
        self.assertEqual(seen, set())
        # Caller adds; helper returns False again.
        seen.add("c1")
        self.assertFalse(AgentLoop._is_duplicate_tool_call_end("c2", seen))
        # Helper now reports duplicate without mutating.
        self.assertTrue(AgentLoop._is_duplicate_tool_call_end("c1", seen))
        self.assertEqual(seen, {"c1"})


class TestKnowledgeEnabledSetting(unittest.TestCase):
    """``knowledge_enabled`` must be discoverable in Settings and persist."""

    def test_default_is_true(self) -> None:
        from rikugan.core.config import RikuganConfig

        cfg = RikuganConfig()
        self.assertTrue(cfg.knowledge_enabled)

    def test_checkbox_default_round_trip(self) -> None:
        from rikugan.core.config import RikuganConfig
        from rikugan.ui.settings_dialog import SettingsDialog

        _ensure_qapplication()
        config = RikuganConfig()
        dlg = SettingsDialog(config)
        try:
            # Checkbox exists and mirrors the config default.
            self.assertTrue(hasattr(dlg, "_knowledge_enabled_cb"))
            self.assertEqual(
                dlg._knowledge_enabled_cb.isChecked(),
                config.knowledge_enabled,
            )
        finally:
            dlg.done(0)

    def test_toggle_persists_via_on_accept(self) -> None:
        from rikugan.core.config import RikuganConfig
        from rikugan.ui.settings_dialog import SettingsDialog

        _ensure_qapplication()
        config = RikuganConfig()
        dlg = SettingsDialog(config)
        try:
            dlg._knowledge_enabled_cb.setChecked(False)
            dlg._on_accept()
            self.assertFalse(config.knowledge_enabled)
            dlg._knowledge_enabled_cb.setChecked(True)
            dlg._on_accept()
            self.assertTrue(config.knowledge_enabled)
        finally:
            dlg.done(0)


# ----------------------------------------------------------------------------
# H. Max Output Tokens spin box follows selected model metadata
# ----------------------------------------------------------------------------


@requires_real_qt
class TestMaxOutputTokensModelDrivenRange(unittest.TestCase):
    """The ``Max Output Tokens`` spin box must follow the selected model.

    Previously the spin box was hard-capped at 65536 and clamped to
    ``min(m.max_output_tokens, 16384)`` regardless of the model's
    advertised limit.  This regressed models with large output budgets
    (e.g. MiniMax-M3's 524288) and silently truncated the configured
    value.  The fix drives the upper bound from ``ModelInfo.max_output_tokens``
    and removes the 16384 clamp.
    """

    def _make_dialog(self):
        from rikugan.core.config import RikuganConfig
        from rikugan.ui.settings_dialog import SettingsDialog

        _ensure_qapplication()
        config = RikuganConfig()
        config.provider.name = "anthropic"
        config.provider.model = "claude-sonnet-4-20250514"
        dlg = SettingsDialog(config)
        return dlg, config

    def test_initial_spin_box_range_is_generous(self) -> None:
        dlg, _cfg = self._make_dialog()
        try:
            minimum = dlg._max_tokens_spin.minimum()
            maximum = dlg._max_tokens_spin.maximum()
            # Lower bound of 1 matches provider/API minimums and the
            # RikuganConfig.validate() contract (positivity only).
            self.assertEqual(minimum, 1)
            # Upper bound is generous until a model with known metadata
            # is selected.  This avoids the old hard 65536 cap.
            self.assertGreaterEqual(maximum, 1_000_000)
        finally:
            dlg.done(0)

    def test_model_with_high_max_output_tokens_sets_range(self) -> None:
        dlg, _cfg = self._make_dialog()
        try:
            from rikugan.core.types import ModelInfo

            dlg._fetched_models = [
                ModelInfo(
                    id="big-model",
                    name="Big Model",
                    provider="anthropic",
                    context_window=200000,
                    max_output_tokens=524_288,
                    supports_tools=True,
                )
            ]
            dlg._model_combo.addItem("Big Model  (big-model)", "big-model")
            dlg._model_combo.setCurrentIndex(0)
            # On model change, value snaps to the model limit (not clamped).
            self.assertEqual(dlg._max_tokens_spin.value(), 524_288)
            self.assertEqual(dlg._max_tokens_spin.maximum(), 524_288)
            self.assertEqual(dlg._context_spin.value(), 200000)
        finally:
            dlg.done(0)

    def test_update_generation_defaults_no_longer_clamps_to_16384(self) -> None:
        """``_update_generation_defaults`` must NOT clamp to 16384.

        The previous implementation did
        ``min(m.max_output_tokens, 16384)`` which silently truncated
        models with larger output budgets.
        """
        dlg, _cfg = self._make_dialog()
        try:
            from rikugan.core.types import ModelInfo

            dlg._fetched_models = [
                ModelInfo(
                    id="huge",
                    name="Huge",
                    provider="anthropic",
                    context_window=200000,
                    max_output_tokens=128_000,
                    supports_tools=True,
                )
            ]
            dlg._model_combo.addItem("Huge  (huge)", "huge")
            dlg._model_combo.setCurrentIndex(0)
            # Ensure the "model change" branch ran (model_id != saved).
            self.assertEqual(dlg._max_tokens_spin.value(), 128_000)
            self.assertNotEqual(
                dlg._max_tokens_spin.value(),
                16_384,
                "Must not clamp to 16384.",
            )
        finally:
            dlg.done(0)

    def test_same_model_preserves_saved_custom_max_tokens(self) -> None:
        """When the selected model matches the saved config, the
        user's custom ``max_tokens`` value is preserved (only clamped
        if it exceeds the model limit)."""
        dlg, cfg = self._make_dialog()
        try:
            from rikugan.core.types import ModelInfo

            cfg.provider.model = "claude-sonnet-4-20250514"
            cfg.provider.max_tokens = 8192
            dlg._max_tokens_spin.setValue(8192)
            dlg._fetched_models = [
                ModelInfo(
                    id="claude-sonnet-4-20250514",
                    name="Claude Sonnet 4",
                    provider="anthropic",
                    context_window=200000,
                    max_output_tokens=8192,
                    supports_tools=True,
                )
            ]
            dlg._model_combo.addItem(
                "Claude Sonnet 4  (claude-sonnet-4-20250514)",
                "claude-sonnet-4-20250514",
            )
            dlg._model_combo.setCurrentIndex(0)
            # Same model as saved — custom value preserved.
            self.assertEqual(dlg._max_tokens_spin.value(), 8192)
        finally:
            dlg.done(0)

    def test_saved_value_clamped_when_exceeds_model_limit(self) -> None:
        """If the saved value exceeds the model's limit, it is clamped down."""
        dlg, cfg = self._make_dialog()
        try:
            from rikugan.core.types import ModelInfo

            cfg.provider.model = "claude-sonnet-4-20250514"
            cfg.provider.max_tokens = 999_999  # absurdly high
            dlg._max_tokens_spin.setValue(999_999)
            dlg._fetched_models = [
                ModelInfo(
                    id="claude-sonnet-4-20250514",
                    name="Claude Sonnet 4",
                    provider="anthropic",
                    context_window=200000,
                    max_output_tokens=8192,
                    supports_tools=True,
                )
            ]
            dlg._model_combo.addItem(
                "Claude Sonnet 4  (claude-sonnet-4-20250514)",
                "claude-sonnet-4-20250514",
            )
            dlg._model_combo.setCurrentIndex(0)
            # Clamped to model limit.
            self.assertEqual(dlg._max_tokens_spin.value(), 8192)
        finally:
            dlg.done(0)

    def test_unknown_model_uses_generous_fallback(self) -> None:
        """A manually typed model with no metadata uses a generous
        fallback upper bound (>= 1_000_000) and keeps the current value."""
        dlg, _cfg = self._make_dialog()
        try:
            dlg._fetched_models = []  # no metadata
            dlg._model_combo.addItem("my-custom-model", "my-custom-model")
            dlg._model_combo.setCurrentIndex(0)
            dlg._max_tokens_spin.setValue(50000)
            dlg._update_generation_defaults()
            self.assertGreaterEqual(dlg._max_tokens_spin.maximum(), 1_000_000)
            # Current value preserved when model is unknown.
            self.assertEqual(dlg._max_tokens_spin.value(), 50000)
        finally:
            dlg.done(0)

    def test_populate_builtin_models_local_compat_uses_generous_max(self) -> None:
        """``_populate_builtin_models`` must not synthesize a bare
        ``ModelInfo`` for local / custom OpenAI-compatible providers —
        a bare ``ModelInfo`` defaults ``max_output_tokens`` to ``4096``
        which would clamp the spin box to a bogus 4096 limit.  The
        local-compat path should leave ``_fetched_models`` empty and
        fall through to the ``_MANUAL_MAX_TOKENS`` upper bound."""
        dlg, cfg = self._make_dialog()
        try:
            from rikugan.providers.minimax_provider import MiniMaxProvider

            # Pre-populate stale metadata to verify it gets cleared by
            # the local-compat path — otherwise a previous provider's
            # metadata could keep leaking into the spin box maximum.
            cfg.provider.name = "openai_compat"
            cfg.provider.model = "my-llama"
            dlg._fetched_models = [
                MiniMaxProvider._builtin_models()[0],
            ]

            dlg._provider_combo.setCurrentText("openai_compat")
            dlg._populate_builtin_models()

            # Stale metadata cleared — manual path uses the generous fallback.
            self.assertEqual(dlg._fetched_models, [])
            self.assertGreaterEqual(dlg._max_tokens_spin.maximum(), 1_000_000)
            # Sanity: the openai_compat provider's static _builtin_models
            # would have had default 4096 limits if we had used them.
            self.assertNotEqual(dlg._max_tokens_spin.maximum(), 4096)
        finally:
            dlg.done(0)

    def test_populate_builtin_models_ollama_uses_generous_max(self) -> None:
        """Same regression for the Ollama provider."""
        dlg, cfg = self._make_dialog()
        try:
            cfg.provider.name = "ollama"
            cfg.provider.model = "llama3.1:70b"
            dlg._provider_combo.setCurrentText("ollama")
            dlg._populate_builtin_models()

            self.assertEqual(dlg._fetched_models, [])
            self.assertGreaterEqual(dlg._max_tokens_spin.maximum(), 1_000_000)
            self.assertNotEqual(dlg._max_tokens_spin.maximum(), 4096)
        finally:
            dlg.done(0)

    def test_minimax_m3_settings_spin_max_is_524288(self) -> None:
        """The Settings dialog must surface MiniMax-M3's documented
        ``524288`` upper bound for ``Max Output Tokens``."""
        dlg, cfg = self._make_dialog()
        try:
            cfg.provider.name = "minimax"
            cfg.provider.model = "MiniMax-M3"
            dlg._provider_combo.setCurrentText("minimax")
            dlg._populate_builtin_models()

            # Built-in models populated M3 metadata into ``_fetched_models``.
            info = dlg._find_model_info("MiniMax-M3")
            self.assertIsNotNone(info)
            assert info is not None  # type-narrow for the assert below
            self.assertEqual(info.max_output_tokens, 524_288)
            # Spin upper bound is driven by the model's metadata, not a
            # dataclass default or a hard-coded 4096.
            self.assertEqual(dlg._max_tokens_spin.maximum(), 524_288)
            self.assertNotEqual(dlg._max_tokens_spin.maximum(), 4096)
        finally:
            dlg.done(0)


# ----------------------------------------------------------------------------
# J. Anthropic-compatible raw content block preservation
# ----------------------------------------------------------------------------


class TestAnthropicRawPartsPreservation(unittest.TestCase):
    """Raw Anthropic content blocks (thinking signatures, text, tool_use)
    are collected during streaming and replayed by ``_format_messages``."""

    def _stream_chunks(self, events):
        from rikugan.providers.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(api_key="sk-test", model="claude-sonnet-4-20250514")
        return list(provider._stream_chunks(_FakeAnthropicClient(events), {}))

    def test_stream_emits_final_raw_parts_with_signature(self) -> None:
        events = [
            SimpleNamespace(
                type="content_block_start",
                content_block=SimpleNamespace(type="thinking", thinking=""),
            ),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="thinking_delta", thinking="hmm"),
            ),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="signature_delta", signature="sig-xyz"),
            ),
            SimpleNamespace(type="content_block_stop"),
            SimpleNamespace(
                type="content_block_start",
                content_block=SimpleNamespace(type="text", text=""),
            ),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="text_delta", text="hi"),
            ),
            SimpleNamespace(type="content_block_stop"),
        ]
        chunks = self._stream_chunks(events)
        # The visible UI text chunks are unchanged: <think>\n, hmm, \n</think>\n, hi.
        text_chunks = [c.text for c in chunks if c.text]
        self.assertIn("<think>\n", text_chunks)
        self.assertIn("hmm", text_chunks)
        self.assertIn("\n</think>\n", text_chunks)
        self.assertIn("hi", text_chunks)

        # The last chunk carries the complete raw block list.
        last_with_raw = [c for c in chunks if c.raw_parts is not None]
        self.assertEqual(len(last_with_raw), 1)
        raw = last_with_raw[0].raw_parts
        self.assertEqual(len(raw), 2)
        self.assertEqual(raw[0]["type"], "thinking")
        self.assertEqual(raw[0]["thinking"], "hmm")
        self.assertEqual(raw[0]["signature"], "sig-xyz")
        self.assertEqual(raw[1]["type"], "text")
        self.assertEqual(raw[1]["text"], "hi")

    def test_stream_collects_tool_use_raw_block(self) -> None:
        events = [
            SimpleNamespace(
                type="content_block_start",
                content_block=SimpleNamespace(type="tool_use", id="t1", name="do_thing", input=None),
            ),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="input_json_delta", partial_json='{"a":'),
            ),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="input_json_delta", partial_json="1}"),
            ),
            SimpleNamespace(type="content_block_stop"),
        ]
        chunks = self._stream_chunks(events)
        last_with_raw = [c for c in chunks if c.raw_parts is not None]
        self.assertEqual(len(last_with_raw), 1)
        raw = last_with_raw[0].raw_parts
        self.assertEqual(len(raw), 1)
        self.assertEqual(raw[0]["type"], "tool_use")
        self.assertEqual(raw[0]["id"], "t1")
        self.assertEqual(raw[0]["name"], "do_thing")
        self.assertEqual(raw[0]["input"], {"a": 1})

    def test_stream_malformed_tool_use_json_degrades_gracefully(self) -> None:
        """Malformed partial JSON must not raise; the raw block falls
        back to an empty input dict so downstream replay still works."""
        events = [
            SimpleNamespace(
                type="content_block_start",
                content_block=SimpleNamespace(type="tool_use", id="t1", name="broken", input=None),
            ),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="input_json_delta", partial_json='{"a": '),
            ),
            SimpleNamespace(type="content_block_stop"),
        ]
        chunks = self._stream_chunks(events)
        last_with_raw = [c for c in chunks if c.raw_parts is not None]
        self.assertEqual(len(last_with_raw), 1)
        raw = last_with_raw[0].raw_parts
        self.assertEqual(raw[0]["input"], {})

    def test_format_messages_replays_raw_parts(self) -> None:
        """When an assistant message carries Anthropic-shaped ``_raw_parts``,
        ``_format_messages`` must replay them verbatim instead of
        reconstructing from ``content`` + ``tool_calls``."""
        from rikugan.core.types import Message, Role
        from rikugan.providers.anthropic_provider import AnthropicProvider

        raw = [
            {"type": "thinking", "thinking": "reasoning", "signature": "abc"},
            {"type": "text", "text": "answer"},
            {"type": "tool_use", "id": "t1", "name": "do_thing", "input": {"x": 1}},
        ]
        msg = Message(role=Role.ASSISTANT, content="answer")
        msg._raw_parts = raw
        provider = AnthropicProvider(api_key="sk-test")
        formatted = provider._format_messages([msg])
        self.assertEqual(formatted[0]["content"], raw)

    def test_format_messages_falls_back_without_raw_parts(self) -> None:
        """Without ``_raw_parts``, the existing reconstruction path
        is used (text + tool_use dicts)."""
        from rikugan.core.types import Message, Role, ToolCall
        from rikugan.providers.anthropic_provider import AnthropicProvider

        msg = Message(
            role=Role.ASSISTANT,
            content="hi",
            tool_calls=[ToolCall(id="t1", name="do", arguments={"a": 1})],
        )
        provider = AnthropicProvider(api_key="sk-test")
        formatted = provider._format_messages([msg])
        content = formatted[0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "hi"})
        self.assertEqual(
            content[1],
            {"type": "tool_use", "id": "t1", "name": "do", "input": {"a": 1}},
        )

    def test_format_messages_rejects_gemini_shaped_raw_parts(self) -> None:
        """Non-dict raw parts (e.g. Gemini ``Part`` objects) must
        not be forwarded as Anthropic content blocks."""
        from rikugan.core.types import Message, Role
        from rikugan.providers.anthropic_provider import AnthropicProvider

        # Simulate a Gemini-shaped raw part (SDK object with attributes).
        class _FakeGeminiPart:
            type = "text"
            text = "hello"

        msg = Message(role=Role.ASSISTANT, content="hello")
        msg._raw_parts = [_FakeGeminiPart()]
        provider = AnthropicProvider(api_key="sk-test")
        formatted = provider._format_messages([msg])
        # Falls back to reconstruction (text block with content).
        self.assertEqual(formatted[0]["content"][0], {"type": "text", "text": "hello"})


@requires_real_qt
class TestRefreshKeepsUnlistedModel(unittest.TestCase):
    """Clicking Refresh must not rewrite the configured model.

    ``/models`` listings lag behind what an account can actually run (new
    previews, Token-Plan-only ids, account-scoped rollouts), so a model that
    works today is routinely absent from the fetched list.  The Refresh
    handler used to jump to index 0 in that case, silently switching the user
    to a different model.
    """

    def _make_dialog(self, model: str):
        from rikugan.core.config import RikuganConfig
        from rikugan.ui.settings_dialog import SettingsDialog

        _ensure_qapplication()
        config = RikuganConfig()
        config.provider.name = "minimax-token-plan"
        config.provider.model = model
        return SettingsDialog(config), config

    def _refresh_with(self, dlg, advertised_ids: list[str]) -> str:
        from rikugan.core.types import ModelInfo

        dlg._set_manual_model_text(dlg._config.provider.model)
        dlg._on_models_ready([ModelInfo(id=i, name=i, provider="minimax-token-plan") for i in advertised_ids])
        return dlg._get_selected_model_id()

    def test_refresh_keeps_model_missing_from_server_list(self) -> None:
        dlg, _cfg = self._make_dialog("MiniMax-M3.1-Flash-Preview")
        try:
            selected = self._refresh_with(
                dlg,
                ["MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.5", "MiniMax-M2.1", "MiniMax-M2"],
            )
            self.assertEqual(selected, "MiniMax-M3.1-Flash-Preview")
        finally:
            dlg.done(0)

    def test_refresh_still_selects_advertised_model(self) -> None:
        """The fix must not break the normal case: a model present in the
        fetched list is still matched and selected by id."""
        dlg, _cfg = self._make_dialog("MiniMax-M2.7")
        try:
            selected = self._refresh_with(dlg, ["MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.5"])
            self.assertEqual(selected, "MiniMax-M2.7")
        finally:
            dlg.done(0)


if __name__ == "__main__":
    unittest.main()
