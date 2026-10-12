"""Consumer-visible regressions for the Settings profile/theme transactions
and the narrow-width Connection row.

Two findings from the native UI review are pinned here:

* **Delete then Save resurrected the deleted profile.**  Deleting a
  custom profile dropped it from the working copy but left the form
  fields and ``_displayed_profile`` naming it, so the next profile
  switch or ``apply_to_config`` re-created it from the stale fields.
  The tests drive the real ``ProfilesTab`` handlers and assert what the
  config ends up holding — never how the fix is written.
* **The theme combo's live preview survived Cancel.**  ``config.theme``
  was restored but the live ``ThemeManager`` singleton and the legacy
  ``styles`` helper were not, so the panel kept painting the rejected
  theme.  The three stores can legitimately hold different values, and
  Cancel must put each one back exactly as it was.

The third block drives the real ``SettingsDialog`` and asserts the layout
contract that failed at 400/460 px: the Connection section's minimum width
fits the scroll viewport, so the body never grows a horizontal scrollbar,
Refresh stays on screen and keeps its label at a large font.
``requires_real_qt`` skips the module when a sibling test installed the
stub bindings.

Isolation: no provider instance is ever built, deferred init (auth probe
+ model population) is inert, and ``LucNhanConfig.save`` is patched so a
Save can never touch the real configuration directory.  Real PySide6 only
— the IDA bundle has no ``PySide6.QtTest``.
"""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from tests.qt_real import requires_real_qt


class _InertRegistry:
    """Provider registry stand-in that refuses to build anything.

    The dialog calls ``register_custom_providers`` at construction and
    ``new_instance`` only from the deferred auth probe; both are
    neutralised here so no test reaches a provider SDK, a credential or
    a network call.
    """

    def register_custom_providers(self, *_a, **_k) -> None:
        return None

    def _is_compat_name(self, _name: str) -> bool:
        return False

    def _is_glm_name(self, _name: str) -> bool:
        return False

    def new_instance(self, *_a, **_k):
        raise AssertionError("tests must not build a provider instance")


def _ensure_qapplication():
    from lucnhan.ui.qt_compat import QApplication

    return QApplication.instance() or QApplication([])


class _SettingsDialogCase(unittest.TestCase):
    """Real-Qt settings dialog with every side effect switched off."""

    def setUp(self) -> None:
        _ensure_qapplication()
        from lucnhan.core.config import LucNhanConfig

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.config = LucNhanConfig()
        # Never persist: Save writes the real config directory otherwise.
        self.config.save = lambda *_a, **_k: None
        self.stack.enter_context(patch("lucnhan.ui.settings_dialog.SettingsDialog._deferred_init", lambda _self: None))
        self.addCleanup(self._drain_events)
        self.dialog = None

    @staticmethod
    def _drain_events() -> None:
        from lucnhan.ui.qt_compat import QApplication

        app = QApplication.instance()
        if app is not None:
            app.processEvents()

    def tearDown(self) -> None:
        if self.dialog is not None:
            self.dialog.done(0)
            self.dialog.deleteLater()
            self.dialog = None
        self._drain_events()

    def open_dialog(self):
        from lucnhan.ui.settings_dialog import SettingsDialog

        self.dialog = SettingsDialog(config=self.config, registry=_InertRegistry())
        self.dialog.show()
        self._drain_events()
        return self.dialog

    def preview_theme(self, mode: str) -> None:
        """Pick ``mode`` in the theme combo the way the user does."""
        combo = self.dialog._theme_combo
        for i in range(combo.count()):
            if combo.itemData(i) == mode:
                combo.setCurrentIndex(i)
                return
        self.fail(f"theme combo has no {mode!r} entry")


def _custom_profile(name: str, description: str) -> dict:
    """Build a real custom-profile record the way the config stores one."""
    from lucnhan.core.profile import AnalysisProfile

    return AnalysisProfile(name=name, description=description).to_dict()


def _confirm(reply: str):
    """Patch the delete confirmation to the given Yes/No answer."""
    from lucnhan.ui.qt_compat import QMessageBox

    return patch(
        "lucnhan.ui.tabs.profiles_tab.QMessageBox.question",
        return_value=getattr(QMessageBox.StandardButton, reply),
    )


@requires_real_qt
class TestDeleteProfileSurvivesApply(unittest.TestCase):
    """A deleted custom profile must stay deleted through Save."""

    def setUp(self) -> None:
        _ensure_qapplication()
        from lucnhan.core.config import LucNhanConfig

        self.config = LucNhanConfig()
        self.config.custom_profiles = {
            "keep-me": _custom_profile("keep-me", "kept profile"),
            "drop-me": _custom_profile("drop-me", "dropped profile"),
        }
        self.config.active_profile = "drop-me"
        self.addCleanup(self._drain_events)

    @staticmethod
    def _drain_events() -> None:
        from lucnhan.ui.qt_compat import QApplication

        app = QApplication.instance()
        if app is not None:
            app.processEvents()

    def _tab(self):
        from lucnhan.ui.tabs.profiles_tab import ProfilesTab

        tab = ProfilesTab(self.config, service=SimpleNamespace(tools_by_category={}))
        self.addCleanup(tab.deleteLater)
        return tab

    def test_apply_after_delete_keeps_profile_deleted(self) -> None:
        tab = self._tab()
        with _confirm("Yes"):
            tab._delete_btn.click()
        tab.apply_to_config(self.config)
        self.assertNotIn("drop-me", self.config.custom_profiles)
        self.assertIn("keep-me", self.config.custom_profiles)

    def test_apply_after_delete_loads_the_remaining_selection(self) -> None:
        """Save must activate the profile the combo actually shows."""
        tab = self._tab()
        with _confirm("Yes"):
            tab._delete_btn.click()
        tab.apply_to_config(self.config)
        self.assertEqual(self.config.active_profile, tab._profile_combo.currentText())
        self.assertNotEqual(self.config.active_profile, "drop-me")

    def test_switching_after_delete_does_not_reinsert_the_profile(self) -> None:
        """A later profile switch must not write the deleted one back."""
        tab = self._tab()
        with _confirm("Yes"):
            tab._delete_btn.click()
        for name in ("default", "private", "keep-me"):
            idx = tab._profile_combo.findText(name)
            if idx >= 0:
                tab._profile_combo.setCurrentIndex(idx)
        tab.apply_to_config(self.config)
        self.assertNotIn("drop-me", self.config.custom_profiles)

    def test_declining_the_confirmation_deletes_nothing(self) -> None:
        tab = self._tab()
        with _confirm("No"):
            tab._delete_btn.click()
        tab.apply_to_config(self.config)
        self.assertIn("drop-me", self.config.custom_profiles)


@requires_real_qt
class TestThemePreviewCancelRestoresLiveTheme(_SettingsDialogCase):
    """Reject must revert config, ThemeManager and the legacy helper."""

    def setUp(self) -> None:
        from lucnhan.ui.theme.manager import ThemeManager

        ThemeManager.reset()
        self.addCleanup(ThemeManager.reset)
        super().setUp()
        self.config.theme = "light"

    @staticmethod
    def _applied_theme() -> tuple[str, str]:
        """Return the two *painted* theme stores as plain strings."""
        from lucnhan.ui.styles import get_current_theme
        from lucnhan.ui.theme.manager import ThemeManager

        return (ThemeManager.instance().mode.value, get_current_theme())

    def _apply_live_theme(self, mode: str, legacy: str) -> None:
        """Put the live manager + legacy helper into a known state."""
        from lucnhan.ui.styles import set_current_theme
        from lucnhan.ui.theme.manager import ThemeManager
        from lucnhan.ui.theme.tokens import ThemeMode

        ThemeManager.instance().set_mode(ThemeMode(mode))
        set_current_theme(legacy)

    def test_preview_is_live_before_cancel(self) -> None:
        """Positive control: the combo really does repaint live."""
        self.open_dialog()
        before = self._applied_theme()
        self.preview_theme("dark")
        self.assertEqual(self.config.theme, "dark")
        self.assertNotEqual(self._applied_theme(), before)

    def test_cancel_restores_each_store_to_its_own_original(self) -> None:
        """Config, manager and legacy disagree: Cancel keeps each one."""
        self._apply_live_theme("dark", "ida")
        original_config = self.config.theme
        dialog = self.open_dialog()
        self.preview_theme("ida")
        self.assertNotEqual(self._applied_theme(), ("dark", "ida"))
        dialog.reject()
        self.assertEqual(self.config.theme, original_config)
        self.assertEqual(self._applied_theme(), ("dark", "ida"))

    def test_back_to_chat_restores_the_live_theme(self) -> None:
        dialog = self.open_dialog()
        applied = self._applied_theme()
        self.preview_theme("dark")
        dialog._back_to_chat_btn.click()
        self.assertEqual(self._applied_theme(), applied)

    def test_done_rejected_restores_the_live_theme(self) -> None:
        """Escape and any programmatic close land on ``done(Rejected)``."""
        dialog = self.open_dialog()
        applied = self._applied_theme()
        self.preview_theme("dark")
        dialog.done(dialog.DialogCode.Rejected)
        self.assertEqual(self._applied_theme(), applied)

    def test_save_keeps_the_chosen_theme(self) -> None:
        dialog = self.open_dialog()
        applied = self._applied_theme()
        self.preview_theme("dark")
        dialog._on_accept()
        self.assertEqual(self._applied_theme(), ("dark", "dark"))
        self.assertNotEqual(self._applied_theme(), applied)

    def test_cancel_does_not_recolor_the_qapplication(self) -> None:
        """The theme restore must never repaint the whole application."""
        from lucnhan.ui.qt_compat import QPalette

        app = _ensure_qapplication()

        def window_color():
            return app.palette().color(QPalette.ColorRole.Window).name()

        dialog = self.open_dialog()
        before = window_color()
        self.preview_theme("dark")
        dialog.reject()
        self.assertEqual(window_color(), before)


@requires_real_qt
class TestNarrowConnectionRowFits(_SettingsDialogCase):
    """The Connection section must fit a 400/460 px body with Refresh shown."""

    def setUp(self) -> None:
        super().setUp()
        self.config.provider.name = "ollama"
        self.config.provider.model = "llama3.1"
        self.dialog = self.open_dialog()

    def _settle(self, width: int, font_size: int = 0) -> None:
        """Resize, re-flow and let Qt re-lay-out, optionally at a large font."""
        from lucnhan.ui.qt_compat import QFont

        if font_size:
            self.dialog._fetch_btn.setFont(QFont(self.dialog.font().family(), font_size))
        self.dialog._font_size_spin.setValue(font_size)
        self.dialog.resize(width, 700)
        self.dialog._apply_responsive_geometry()
        self._drain_events()

    def test_footer_actions_have_matching_geometry(self) -> None:
        for width in (400, 460, 900):
            with self.subTest(width=width):
                self._settle(width)
                cancel = self.dialog._cancel_btn
                save = self.dialog._save_btn
                self.assertEqual(cancel.size(), save.size())
                self.assertEqual(cancel.geometry().top(), save.geometry().top())
                for button in (cancel, save):
                    self.assertTrue(button.isVisible())
                    self.assertGreaterEqual(button.width(), button.fontMetrics().horizontalAdvance(button.text()))

    def test_connection_section_fits_narrow_viewport(self) -> None:
        """Qt's own minimum width must fit — no horizontal body scroll."""
        for width in (400, 460):
            with self.subTest(width=width):
                self._settle(width)
                viewport = self.dialog._body.viewport().width()
                self.assertGreater(viewport, 0)
                self.assertLessEqual(self.dialog._provider_group.minimumSizeHint().width(), viewport)

    def test_body_needs_no_horizontal_scroll_at_default_font(self) -> None:
        """Every page must fit the 400 px minimum width, incl. lazy pages.

        ``_page_host`` is a ``QStackedWidget``: it takes the largest
        minimum of *all* its pages, including the Appearance and lazy
        Skills / MCP / Profiles pages the user has not opened yet.  One
        unshrinkable label on a hidden page is therefore enough to put
        the whole body into a horizontal scrollbar.
        """
        for width in (400, 460):
            for page in ("Provider", "Appearance", "Skills", "MCP", "Profiles"):
                with self.subTest(width=width, page=page):
                    self._settle(width)
                    self.dialog._select_page(page)
                    self._drain_events()
                    bar = self.dialog._body.horizontalScrollBar()
                    self.assertEqual(bar.maximum(), 0)
                    self.assertFalse(bar.isVisible())
            self.dialog._select_page("Provider")
            self._drain_events()

    def test_refresh_stays_visible_at_narrow_width(self) -> None:
        self._settle(400)
        button = self.dialog._fetch_btn
        self.assertTrue(button.isVisible())
        self.assertGreaterEqual(button.width(), button.minimumWidth())
        body = self.dialog._body
        right_edge = button.mapTo(body.viewport(), button.rect().topRight()).x()
        self.assertLessEqual(right_edge, body.viewport().width())

    def test_model_combo_stays_editable_and_usable_at_narrow_width(self) -> None:
        """Shrinking the combo must not make it unusable or read-only."""
        self._settle(400)
        combo = self.dialog._model_combo
        self.assertTrue(combo.isEditable())
        self.assertGreaterEqual(combo.width(), 40)
        self.assertTrue(combo.lineEdit().isVisible())

    def test_refresh_label_fits_a_large_font(self) -> None:
        """A real large font must widen Refresh, not elide its label."""
        from lucnhan.ui.settings_dialog import _SETTINGS_FONT_SIZE_LARGE_PT

        self._settle(460, font_size=_SETTINGS_FONT_SIZE_LARGE_PT)
        button = self.dialog._fetch_btn
        metrics = button.fontMetrics()
        self.assertGreaterEqual(button.width(), metrics.horizontalAdvance(button.text()))

    def test_manual_model_entry_still_round_trips(self) -> None:
        """Shrinking the combo must not break a typed model ID."""
        self.dialog._set_manual_model_text("my-custom-model-id")
        self.dialog._on_accept()
        self.assertEqual(self.config.provider.model, "my-custom-model-id")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
