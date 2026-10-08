"""REAL-Qt regression tests for the Task 6 Settings layout (G7).

These must run against real PySide6, not ``qt_stubs``: the bugs they
pin are precisely the ones a stub cannot see.

* CI-1 — ``QFormLayout.removeRow(QWidget*)`` **deletes** the row's label
  and field.  The responsive reflow used it, so crossing the narrow
  breakpoint deleted the Temperature spin box and the Auto-context
  checkbox, and a later Save dereferenced a deleted C++ object.
* CI-2 — ``QStackedWidget.removeWidget`` of the current last page emits
  ``currentChanged``, which eagerly built a sibling lazy page (a
  ``SettingsService`` filesystem scan) the user never opened.
* IMP-3 — section titles were pinned to the 138/108 px form-label
  column and hard-clipped ("GLM reasoning resilience" is ~288 px).

``tests.qt_real.requires_real_qt`` skips the module when a sibling test
installed the stub bindings.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from tests.qt_real import requires_real_qt


def _ensure_qapplication():
    from lucnhan.ui.qt_compat import QApplication

    return QApplication.instance() or QApplication([])


def _dialog():
    from lucnhan.core.config import LucNhanConfig
    from lucnhan.ui.settings_dialog import SettingsDialog

    config = LucNhanConfig()
    config.provider.name = "ollama"
    config.provider.model = "llama3.1"
    return SettingsDialog(config)


@requires_real_qt
class TestResponsiveReflowKeepsWidgets(unittest.TestCase):
    """CI-1: crossing the breakpoint must not delete form widgets."""

    def test_narrow_crossing_keeps_widgets_alive(self) -> None:
        import shiboken6

        _ensure_qapplication()
        dlg = _dialog()
        try:
            dlg.resize(900, 700)
            dlg._apply_responsive_geometry()
            temp = dlg._temp_spin
            auto_ctx = dlg._auto_context_cb
            self.assertTrue(shiboken6.isValid(temp))
            self.assertTrue(shiboken6.isValid(auto_ctx))

            # First narrow crossing — this is where removeRow destroyed them.
            dlg.resize(460, 700)
            dlg._apply_responsive_geometry()
            self.assertTrue(shiboken6.isValid(temp), "Temperature spin deleted by reflow")
            self.assertTrue(shiboken6.isValid(auto_ctx), "Auto-context checkbox deleted by reflow")

            # And a round trip back to wide must not break it either.
            dlg.resize(900, 700)
            dlg._apply_responsive_geometry()
            self.assertTrue(shiboken6.isValid(temp))
            self.assertTrue(shiboken6.isValid(auto_ctx))
        finally:
            dlg.done(0)

    def test_large_font_crossing_keeps_widgets_alive(self) -> None:
        import shiboken6

        _ensure_qapplication()
        dlg = _dialog()
        try:
            dlg._font_size_spin.setValue(20)  # >= _SETTINGS_FONT_SIZE_LARGE_PT
            dlg._apply_responsive_geometry()
            self.assertTrue(shiboken6.isValid(dlg._temp_spin))
            self.assertTrue(shiboken6.isValid(dlg._thinking_combo))
            self.assertTrue(shiboken6.isValid(dlg._max_tokens_spin))
        finally:
            dlg.done(0)

    def test_save_completes_after_narrow_crossing(self) -> None:
        """A Save after the breakpoint must not hit a deleted widget."""
        _ensure_qapplication()
        dlg = _dialog()
        try:
            dlg.resize(900, 700)
            dlg._apply_responsive_geometry()
            dlg.resize(460, 700)
            dlg._apply_responsive_geometry()
            dlg._temp_spin.setValue(0.42)
            try:
                dlg._on_accept()
            except Exception as exc:  # pragma: no cover - failure detail
                self.fail(f"_on_accept raised {type(exc).__name__}: {exc}")
            self.assertAlmostEqual(dlg._config.provider.temperature, 0.42, places=3)
        finally:
            dlg.done(0)


@requires_real_qt
class TestLazyPagesStayLazy(unittest.TestCase):
    """CI-2: navigating to Profiles must not build Skills or MCP."""

    def test_selecting_profiles_does_not_build_siblings(self) -> None:
        _ensure_qapplication()
        dlg = _dialog()
        try:
            with (
                patch("lucnhan.ui.tabs.skills_tab.SkillsTab") as skills,
                patch("lucnhan.ui.tabs.mcp_tab.MCPTab") as mcp,
                patch("lucnhan.ui.tabs.profiles_tab.ProfilesTab") as profiles,
            ):
                dlg._select_page("Profiles")
            profiles.assert_called_once()
            skills.assert_not_called()
            mcp.assert_not_called()
        finally:
            dlg.done(0)

    def test_selecting_mcp_does_not_build_skills(self) -> None:
        _ensure_qapplication()
        dlg = _dialog()
        try:
            with (
                patch("lucnhan.ui.tabs.skills_tab.SkillsTab") as skills,
                patch("lucnhan.ui.tabs.mcp_tab.MCPTab") as mcp,
            ):
                dlg._select_page("MCP")
            mcp.assert_called_once()
            skills.assert_not_called()
        finally:
            dlg.done(0)

    def test_service_is_not_built_by_navigation_alone(self) -> None:
        _ensure_qapplication()
        dlg = _dialog()
        try:
            with patch("lucnhan.ui.settings_service.SettingsService") as service:
                for title in ("Provider", "Appearance", "Skills", "MCP", "Profiles"):
                    dlg._select_page(title)
                # Three pages were visited, so the service is built once —
                # but only for the pages the user actually opened.
                self.assertLessEqual(service.call_count, 1)
        finally:
            dlg.done(0)


@requires_real_qt
class TestSectionTitlesAreNotClamped(unittest.TestCase):
    """IMP-3: headings must not be pinned to the form-label column."""

    def test_section_titles_exceed_the_label_column(self) -> None:
        from lucnhan.ui.qt_compat import QLabel
        from lucnhan.ui.settings_dialog import _SETTINGS_LABEL_WIDTH_PX

        _ensure_qapplication()
        dlg = _dialog()
        try:
            titles = [
                w
                for w in dlg.findChildren(QLabel)
                if w.objectName() == "settings_section_title"
            ]
            self.assertTrue(titles, "no section titles found")
            for label in titles:
                self.assertLess(label.minimumWidth(), _SETTINGS_LABEL_WIDTH_PX)
                # A clamp would force maxWidth down to the column width.
                self.assertNotEqual(label.maximumWidth(), _SETTINGS_LABEL_WIDTH_PX, label.text())
        finally:
            dlg.done(0)

    def test_section_title_renders_without_clipping(self) -> None:
        """The widest real title must fit its natural width."""
        from lucnhan.ui.qt_compat import QLabel

        _ensure_qapplication()
        dlg = _dialog()
        try:
            titles = {
                w.text(): w
                for w in dlg.findChildren(QLabel)
                if w.objectName() == "settings_section_title"
            }
            self.assertIn("GLM reasoning resilience", titles)
            label = titles["GLM reasoning resilience"]
            dlg.resize(900, 700)
            dlg._apply_responsive_geometry()
            dlg._body_content.adjustSize()
            app = _ensure_qapplication()
            app.processEvents()
            self.assertGreaterEqual(label.width(), label.sizeHint().width() - 1)
        finally:
            dlg.done(0)


@requires_real_qt
class TestFormLabelsStillClamped(unittest.TestCase):
    """The label column itself must keep its G7 width after the fix."""

    def test_form_labels_follow_the_breakpoint(self) -> None:
        from lucnhan.ui.qt_compat import QLabel
        from lucnhan.ui.settings_dialog import (
            _SETTINGS_LABEL_WIDTH_NARROW_PX,
            _SETTINGS_LABEL_WIDTH_PX,
        )

        _ensure_qapplication()
        dlg = _dialog()
        try:
            labels = [
                w
                for w in dlg.findChildren(QLabel)
                if w.objectName() == "settings_form_label" and w.text()
            ]
            self.assertTrue(labels)
            dlg.resize(900, 700)
            dlg._apply_responsive_geometry()
            self.assertTrue(all(label.maximumWidth() == _SETTINGS_LABEL_WIDTH_PX for label in labels))
            dlg.resize(460, 700)
            dlg._apply_responsive_geometry()
            self.assertTrue(
                all(label.maximumWidth() == _SETTINGS_LABEL_WIDTH_NARROW_PX for label in labels)
            )
        finally:
            dlg.done(0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
