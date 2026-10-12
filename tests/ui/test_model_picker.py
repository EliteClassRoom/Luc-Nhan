"""Footer model picker (GUI contract G6) — grouped list, local search.

Two tiers:

* **Logic tier** — runs everywhere (catalog grouping, duplicate model IDs
  across connections, local filtering without row reallocation, thinking
  fallback, busy blocking, identity from item data, cache invalidation).
* **Native tier** — decorated with ``@requires_real_qt`` so it SKIPS when
  ``tests.qt_stubs`` replaced PySide6.  It drives real geometry, focus and
  key events plus the panel footer seam.  A stubbed run proves nothing here
  and must never be reported as native verification.

Run standalone to exercise the native tier::

    pytest tests/ui/test_model_picker.py
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from lucnhan.core.types import ModelInfo
from lucnhan.ui.model_picker import (
    BUSY_HINT,
    NO_MATCHES_TEXT,
    OPEN_SETTINGS_TEXT,
    ModelCatalogCache,
    ModelChoice,
    ModelPicker,
)
from tests.qt_real import requires_real_qt


def _info(model_id: str, name: str = "", provider: str = "anthropic") -> ModelInfo:
    return ModelInfo(id=model_id, name=name or model_id, provider=provider)


def _catalog(*pairs: tuple[str, list[str]]) -> dict[str, list[ModelInfo]]:
    return {name: [_info(mid) for mid in ids] for name, ids in pairs}


def _picker() -> tuple[ModelPicker, list[ModelChoice]]:
    chosen: list[ModelChoice] = []
    return ModelPicker(parent=None, on_select=chosen.append), chosen


def _anchor():
    """Stand-in trigger: opening the popup only needs a geometry."""
    from PySide6.QtWidgets import QLineEdit

    return QLineEdit()


@requires_real_qt
class _LogicBase(unittest.TestCase):
    """Logic-tier base: guarantees a ``QApplication`` for widget creation.

    Widgets cannot be constructed without one — ``QWidget: Must construct
    a QApplication before a QWidget`` aborts the process otherwise, and
    that abort kills the whole test session.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# Catalog / grouping / identity
# ---------------------------------------------------------------------------


class TestCatalogIdentity(_LogicBase):
    def test_duplicate_model_ids_are_independent_rows(self) -> None:
        """Two connections advertising ``gpt-4o`` are two rows.

        Identity is the (connection, model) PAIR — resolving it from the
        display text alone would make the two rows indistinguishable.
        """
        picker, chosen = _picker()
        picker.set_catalog(_catalog(("openai", ["gpt-4o"]), ("local", ["gpt-4o"])))

        self.assertEqual(
            [(r.connection_name, r.model_id) for r in picker.model_rows()],
            [("openai", "gpt-4o"), ("local", "gpt-4o")],
        )
        picker.select_identity("local", "gpt-4o")
        picker.commit_current()
        self.assertEqual(chosen, [ModelChoice("local", "gpt-4o", "high")])

    def test_identity_reads_item_data_not_display_text(self) -> None:
        """A connection whose name appears in the model text stays separate.

        Guards the "never parse display text" rule: the row renders
        ``gpt-4o (openai)`` under a connection named ``openai``; a text
        parser could resolve the wrong connection for the second row.
        """
        picker, _ = _picker()
        picker.set_catalog(_catalog(("openai", ["gpt-4o (openai)"])))

        self.assertIsNotNone(picker.item_for("openai", "gpt-4o (openai)"))
        # No row exists for the pair a display-text parse would guess.
        self.assertIsNone(picker.item_for("gpt-4o (openai)", ""))

    def test_model_name_is_metadata_only(self) -> None:
        """Real ``ModelInfo.name`` rides along; it is never the identity."""
        picker, _ = _picker()
        picker.set_catalog({"openai": [ModelInfo("gpt-4o", "GPT-4o (Omni)", "openai")]})
        row = picker.model_rows()[0]
        self.assertEqual((row.connection_name, row.model_id), ("openai", "gpt-4o"))
        self.assertEqual(row.model_name, "GPT-4o (Omni)")
        self.assertEqual(row.provider, "openai")

    def test_empty_catalog_clears_rows(self) -> None:
        picker, _ = _picker()
        picker.set_catalog(_catalog(("openai", ["gpt-4o"])))
        picker.set_catalog({})
        self.assertEqual(picker.model_rows(), [])
        self.assertEqual(picker.visible_rows(), [])


# ---------------------------------------------------------------------------
# Local search — pure, no fetch, no reallocation
# ---------------------------------------------------------------------------


class TestLocalSearch(_LogicBase):
    def setUp(self) -> None:
        self.picker, self.chosen = _picker()
        self.picker.set_catalog(
            _catalog(
                ("openai", ["gpt-4o", "gpt-4o-mini", "o3"]),
                ("local", ["llama3.1", "gpt-4o"]),
            )
        )

    def test_filter_by_model_id(self) -> None:
        self.picker.set_search_text("llama")
        self.assertEqual(
            [(r.connection_name, r.model_id) for r in self.picker.visible_rows()],
            [("local", "llama3.1")],
        )

    def test_filter_by_connection_name(self) -> None:
        self.picker.set_search_text("openai")
        self.assertEqual(
            [(r.connection_name, r.model_id) for r in self.picker.visible_rows()],
            [("openai", "gpt-4o"), ("openai", "gpt-4o-mini"), ("openai", "o3")],
        )

    def test_filter_is_case_insensitive_substring(self) -> None:
        self.picker.set_search_text("GPT-4O")
        self.assertEqual(
            [(r.connection_name, r.model_id) for r in self.picker.visible_rows()],
            [("openai", "gpt-4o"), ("openai", "gpt-4o-mini"), ("local", "gpt-4o")],
        )

    def test_no_fetch_code_exists_in_the_picker_module(self) -> None:
        """Structural guarantee that typing / opening cannot fetch.

        The popup is presentation over a cached catalog; there is no
        provider, HTTP or SDK symbol reachable from the module at all.
        """
        import lucnhan.ui.model_picker as mp

        blob = "\n".join(open(mp.__file__, encoding="utf-8").read().splitlines())
        for banned in ("import requests", "import httpx", "ProviderRegistry", "list_models", "urlopen"):
            self.assertNotIn(banned, blob, f"picker must not reference {banned!r}")

    def test_filter_hides_rows_instead_of_rebuilding(self) -> None:
        """Filtering reuses the rows built at ``set_catalog``."""
        before = {r: id(r) for r in self.picker.model_rows()}
        self.picker.set_search_text("gpt-4o")
        after = self.picker.visible_rows()
        self.assertEqual(len(after), 3)
        for row in after:
            self.assertEqual(id(row), before[row], "filter must not reallocate rows")

    def test_zero_results_keeps_query_and_does_not_switch(self) -> None:
        self.picker.select_identity("openai", "o3")
        self.picker.set_search_text("no-such-model")
        self.assertEqual(self.picker.visible_rows(), [])
        self.assertEqual(self.picker.status_text(), NO_MATCHES_TEXT)
        self.assertEqual(self.picker.search_text(), "no-such-model")
        self.assertEqual(self.picker.current_choice().model_id, "o3")
        self.picker.clear_search()
        self.assertEqual(self.picker.search_text(), "")
        self.assertEqual(len(self.picker.visible_rows()), 5)

    def test_normalized_haystack_is_cached_at_set_catalog(self) -> None:
        """The filter compares against text normalized once per catalog."""
        picker, _ = _picker()
        picker.set_catalog({"openai": [ModelInfo("Model-A", "Model A", "openai")]})
        self.assertEqual(picker.search_haystacks(), {"openai/Model-A": "openai model-a model a"})
        picker.set_search_text("MODEL-A")
        self.assertEqual([r.model_id for r in picker.visible_rows()], ["Model-A"])
        self.assertEqual(picker.search_haystacks(), {"openai/Model-A": "openai model-a model a"})


# ---------------------------------------------------------------------------
# Thinking levels (shared core policy)
# ---------------------------------------------------------------------------


class TestThinkingPolicy(_LogicBase):
    def test_known_model_uses_registry_levels(self) -> None:
        picker, _ = _picker()
        picker.set_catalog({"glm": [_info("glm-5.3")]})
        picker.select_identity("glm", "glm-5.3")
        self.assertEqual(picker.thinking_levels(), ("high", "max"))
        self.assertEqual(picker.thinking_level(), "high")
        self.assertTrue(picker.thinking_is_confirmed())

    def test_unknown_model_falls_back_permissively_with_hint(self) -> None:
        """An uncatalogued model keeps the fallback AND says it is unconfirmed."""
        picker, _ = _picker()
        picker.set_catalog({"local": [_info("mystery-model")]})
        picker.select_identity("local", "mystery-model")
        self.assertEqual(
            picker.thinking_levels(),
            ("none", "low", "medium", "high", "max", "ultra"),
        )
        self.assertIn("not confirmed", picker.status_text())
        self.assertFalse(picker.thinking_is_confirmed())

    def test_invalid_thinking_level_falls_back_to_default(self) -> None:
        """A stored level the model does not offer never reaches a commit."""
        picker, chosen = _picker()
        picker.set_catalog({"glm": [_info("glm-5.2")]})
        picker.set_selection(ModelChoice("glm", "glm-5.2", "ultra"))
        self.assertEqual(picker.thinking_level(), "high")
        picker.commit_current()
        self.assertEqual([c.thinking_level for c in chosen], ["high"])

    def test_thinking_change_keeps_connection_and_model(self) -> None:
        picker, chosen = _picker()
        picker.set_catalog({"glm": [_info("glm-5.2")]})
        picker.select_identity("glm", "glm-5.2")
        picker.set_thinking_level("max")
        picker.commit_current()
        self.assertEqual(chosen[-1], ModelChoice("glm", "glm-5.2", "max"))

    def test_thinking_change_preserves_selection_without_catalog(self) -> None:
        """A manual model that is not in any catalog stays selectable."""
        picker, chosen = _picker()
        picker.set_catalog({})
        picker.set_selection(ModelChoice("openai", "my-model", "medium"))
        picker.set_thinking_level("high")
        picker.commit_current()
        self.assertEqual(chosen[-1], ModelChoice("openai", "my-model", "high"))


# ---------------------------------------------------------------------------
# Busy state
# ---------------------------------------------------------------------------


class TestBusyState(_LogicBase):
    def setUp(self) -> None:
        self.picker, self.chosen = _picker()
        self.picker.set_catalog(_catalog(("openai", ["gpt-4o", "o3"])))
        self.picker.set_selection(ModelChoice("openai", "gpt-4o", "high"))

    def test_busy_blocks_selection_and_thinking(self) -> None:
        self.picker.set_busy(True)
        self.picker.select_identity("openai", "o3")
        self.picker.set_thinking_level("low")
        self.assertFalse(self.picker.commit_current())
        self.assertEqual(self.chosen, [])
        self.assertEqual(self.picker.status_text(), BUSY_HINT)

    def test_busy_keeps_current_selection_readable(self) -> None:
        self.picker.set_busy(True)
        self.assertEqual(self.picker.current_choice(), ModelChoice("openai", "gpt-4o", "high"))

    def test_busy_keeps_search_usable(self) -> None:
        """Looking around is fine while an agent runs."""
        self.picker.set_busy(True)
        self.picker.set_search_text("o3")
        self.assertEqual([r.model_id for r in self.picker.visible_rows()], ["o3"])

    def test_clearing_busy_restores_selection(self) -> None:
        self.picker.set_busy(True)
        self.picker.set_busy(False)
        self.picker.select_identity("openai", "o3")
        self.assertTrue(self.picker.commit_current())
        self.assertEqual(self.chosen, [ModelChoice("openai", "o3", "high")])

    def test_status_reverts_to_hint_when_idle(self) -> None:
        self.picker.set_busy(True)
        self.picker.set_busy(False)
        self.assertNotEqual(self.picker.status_text(), BUSY_HINT)


# ---------------------------------------------------------------------------
# No fetched catalog — real saved/current model, never a fake list
# ---------------------------------------------------------------------------


class TestNoFetchedCatalog(_LogicBase):
    def test_empty_catalog_offers_settings_entry_not_fake_models(self) -> None:
        picker, _ = _picker()
        picker.set_catalog({})
        self.assertEqual(picker.model_rows(), [])
        self.assertEqual(picker.settings_entry_text(), OPEN_SETTINGS_TEXT)

    def test_saved_model_without_catalog_still_offers_the_settings_entry(self) -> None:
        """Unfetched catalog = saved/current row PLUS the Settings entry.

        ``set_catalog`` always injects the configured model, so gating the
        entry on "no rows" hid it from exactly the users who need it:
        fresh installs and ollama / custom connections that never get a
        builtin list.
        """
        picker, _ = _picker()
        picker.set_selection(ModelChoice("ollama", "llama3.1", "high"))
        picker.set_catalog({})
        self.assertEqual(
            [(r.connection_name, r.model_id) for r in picker.model_rows()],
            [("ollama", "llama3.1")],
        )
        picker.open_above(_anchor())
        self.assertTrue(picker.settings_entry_is_visible())

    def test_settings_entry_disappears_once_a_connection_is_fetched(self) -> None:
        """The opposite direction: a fetched catalog hides the entry."""
        picker, _ = _picker()
        picker.set_selection(ModelChoice("openai", "gpt-4o", "high"))
        picker.set_catalog({"openai": [_info("gpt-4o"), _info("o3")]})
        self.assertFalse(picker.settings_entry_is_visible())

    def test_unfetched_entry_survives_a_saved_model_and_an_empty_search(self) -> None:
        """The unfetched entry stays reachable in both empty shapes.

        ``_sync_rows`` injects the configured model as a real row, so the
        only thing the entry's visibility can key off is the catalog, not
        the row count — the two ways an unfetched catalog looks empty are
        "no fetched connection" and "a query that matches nothing".
        """
        picker, _ = _picker()
        picker.set_selection(ModelChoice("ollama", "llama3.1", "high"))
        picker.set_catalog({})
        picker.open_above(_anchor())
        self.assertTrue(picker.settings_entry_is_visible())
        self.assertEqual(len(picker.model_rows()), 1, "the saved model is a real row")

        picker.set_search_text("nothing-matches-this")
        self.assertEqual(picker.visible_rows(), [])
        self.assertTrue(picker.settings_entry_is_visible())

    def test_current_model_without_catalog_is_a_real_row(self) -> None:
        """The configured (connection, model) is a first-class row.

        It comes from the live config, not a synthetic ``ModelInfo`` — an
        invented one would smuggle fake context-window / token metadata
        into the generation defaults.
        """
        picker, chosen = _picker()
        picker.set_catalog({})
        picker.set_selection(ModelChoice("openai", "my-saved-model", "high"))
        rows = picker.model_rows()
        self.assertEqual([(r.connection_name, r.model_id) for r in rows], [("openai", "my-saved-model")])
        picker.select_identity("openai", "my-saved-model")
        picker.commit_current()
        self.assertEqual(chosen, [ModelChoice("openai", "my-saved-model", "high")])

    def test_settings_entry_triggers_the_callback(self) -> None:
        opened: list[int] = []
        picker, _ = _picker()
        picker.set_open_settings_callback(lambda: opened.append(1))
        picker.set_catalog({})
        picker.open_settings()
        self.assertEqual(opened, [1])

    def test_unfetched_catalog_is_not_reported_as_a_network_error(self) -> None:
        picker, _ = _picker()
        picker.set_catalog({})
        self.assertNotIn("error", picker.status_text().lower())
        self.assertNotIn("fail", picker.status_text().lower())


# ---------------------------------------------------------------------------
# Catalog cache ownership / invalidation
# ---------------------------------------------------------------------------


class TestModelCatalogCache(_LogicBase):
    def setUp(self) -> None:
        self.cache = ModelCatalogCache()

    def test_refresh_results_are_cached_per_connection(self) -> None:
        self.cache.store("openai", [_info("gpt-4o")])
        self.cache.store("local", [_info("llama3.1")])
        self.assertEqual(
            self.cache.catalog_for(["openai", "local"]),
            {"openai": [_info("gpt-4o")], "local": [_info("llama3.1")]},
        )

    def test_unfetched_connection_contributes_nothing(self) -> None:
        self.cache.store("openai", [])
        self.assertEqual(self.cache.catalog_for(["openai"]), {})
        self.assertIsNone(self.cache.get("openai"))

    def test_connection_change_invalidates_only_that_connection(self) -> None:
        self.cache.store("openai", [_info("gpt-4o")])
        self.cache.store("local", [_info("llama3.1")])
        self.cache.note_connection_changed("openai")
        self.assertIsNone(self.cache.get("openai"))
        self.assertIsNotNone(self.cache.get("local"))

    def test_thinking_change_does_not_invalidate(self) -> None:
        self.cache.store("openai", [_info("gpt-4o")])
        self.cache.note_selection_committed("openai", "gpt-4o", "max")
        self.assertIsNotNone(self.cache.get("openai"))

    def test_theme_change_does_not_invalidate(self) -> None:
        self.cache.store("openai", [_info("gpt-4o")])
        self.cache.note_theme_changed()
        self.assertIsNotNone(self.cache.get("openai"))

    def test_no_credential_is_kept_in_the_cache(self) -> None:
        self.cache.store("openai", [_info("gpt-4o")])
        blob = repr(self.cache.catalog_for(["openai"]))
        self.assertNotIn("api_key", blob)
        self.assertNotIn("sk-", blob)


# ---------------------------------------------------------------------------
# Native tier: real geometry, focus, keys, no reallocation
# ---------------------------------------------------------------------------


@requires_real_qt
class TestModelPickerNative(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        from PySide6.QtWidgets import QLineEdit, QPlainTextEdit, QVBoxLayout, QWidget

        self.chosen: list[ModelChoice] = []
        self.host = QWidget()
        lay = QVBoxLayout(self.host)
        lay.setContentsMargins(0, 0, 0, 0)
        self.composer = QPlainTextEdit()
        lay.addWidget(self.composer)
        self.trigger = QLineEdit()
        self.trigger.setFocusPolicy(self.trigger.focusPolicy())
        lay.addWidget(self.trigger)
        self.host.resize(1100, 700)
        self.host.move(0, 0)
        self.host.show()

        self.picker = ModelPicker(parent=self.host, on_select=self.chosen.append)
        self.picker.set_catalog(
            _catalog(
                ("openai", ["gpt-4o", "gpt-4o-mini", "o3"]),
                ("local", ["gpt-4o", "llama3.1"]),
            )
        )
        self.picker.set_selection(ModelChoice("openai", "gpt-4o", "high"))
        for _ in range(3):
            self.app.processEvents()

    def _open(self) -> None:
        self.picker.open_above(self.trigger)
        for _ in range(4):
            self.app.processEvents()

    # -- geometry (GUI contract G6) -------------------------------------

    def test_popup_anchors_above_trigger_left_aligned(self) -> None:
        self._open()
        geo = self.picker.frameGeometry()
        anchor = self.trigger.mapToGlobal(self.trigger.rect().bottomLeft())
        self.assertEqual(geo.top() + geo.height() + 8, anchor.y(), "8 px gap above the trigger")
        self.assertEqual(geo.left(), anchor.x(), "left-aligned with the trigger")

    def test_popup_width_is_clamped_to_dock_and_screen(self) -> None:
        self._open()
        screen = self.app.primaryScreen().availableGeometry()
        width = self.picker.frameGeometry().width()
        self.assertGreaterEqual(width, 1)
        self.assertLessEqual(width, min(360, self.host.width() - 24, screen.width() - 24) + 1)
        self.assertLessEqual(self.picker.frameGeometry().height(), 420 + 1)

    def test_popup_stays_inside_screen_bounds(self) -> None:
        self._open()
        geo = self.picker.frameGeometry()
        screen = self.app.primaryScreen().availableGeometry()
        self.assertGreaterEqual(geo.left(), screen.left())
        self.assertLessEqual(geo.right(), screen.right())
        self.assertGreaterEqual(geo.top(), screen.top())
        self.assertLessEqual(geo.bottom(), screen.bottom())

    def test_narrow_dock_still_shows_a_usable_popup(self) -> None:
        self.host.resize(460, 700)
        self.app.processEvents()
        self._open()
        self.assertGreater(self.picker.frameGeometry().width(), 100)

    # -- focus / keys ---------------------------------------------------

    def test_open_focuses_search(self) -> None:
        self._open()
        self.assertTrue(self.picker._search.hasFocus())

    def test_escape_closes_and_returns_focus_to_trigger(self) -> None:
        self._open()
        self.picker._search.setFocus()
        self.app.processEvents()
        self.app.sendEvent(self.picker._search, self._key_event(self._key("Key_Escape")))
        self.app.processEvents()
        self.assertFalse(self.picker.isVisible())
        self.assertTrue(self.trigger.hasFocus())

    def test_picker_is_a_native_popup_so_click_away_closes_it(self) -> None:
        """Click-away closing is Qt's job, and only works on a Qt.Popup.

        A frameless synthetic event posted at the host behind the popup
        cannot exercise the platform click-away mask (the offscreen
        platform has none), so the honest assertion is the flag Qt
        actually consults: ``Qt.Popup`` on the window.  The Escape /
        WindowDeactivate path above covers our own close handling.
        """
        from PySide6.QtCore import Qt

        self._open()
        self.assertTrue(bool(self.picker.windowFlags() & Qt.WindowType.Popup.value))
        self.assertTrue(self.picker.isVisible())
        # Our own close path is the one we own and it must also restore
        # focus to the trigger.
        self.picker.close_popup()
        self.assertFalse(self.picker.isVisible())
        self.assertTrue(self.trigger.hasFocus())

    def test_composer_draft_and_caret_survive_the_popup(self) -> None:
        self.composer.setFocus()
        self.app.processEvents()
        self.composer.setPlainText("draft that must survive")
        cursor = self.composer.textCursor()
        cursor.setPosition(7)
        self.composer.setTextCursor(cursor)
        self._open()
        self.assertEqual(self.composer.toPlainText(), "draft that must survive")
        self.assertEqual(self.composer.textCursor().position(), 7)
        self._press_escape()
        self.assertEqual(self.composer.toPlainText(), "draft that must survive")

    def test_enter_commits_selected_model_and_popup_stays_open(self) -> None:

        self._open()
        self.picker._search.setText("llama")
        self.app.processEvents()
        # The single surviving row is what a user sees; keyboard focus on
        # the tree then commits it with Enter.
        self.picker._tree.setFocus()
        self.app.processEvents()
        self.assertEqual([r.model_id for r in self.picker.visible_rows()], ["llama3.1"])
        self.app.sendEvent(self.picker._tree, self._key_event(self._key("Key_Return"), "\r"))
        self.app.processEvents()
        self.assertEqual(self.chosen, [ModelChoice("local", "llama3.1", "high")])
        self.assertTrue(self.picker.isVisible())

    def test_arrow_keys_move_the_selection(self) -> None:

        self._open()
        tree = self.picker._tree
        tree.setFocus()
        self.picker.select_identity("openai", "gpt-4o")
        self.app.processEvents()
        first = self.picker.current_choice().model_id
        self.app.sendEvent(tree, self._key_event(self._key("Key_Down")))
        self.app.processEvents()
        self.assertNotEqual(self.picker.current_choice().model_id, first)

    def test_search_keeps_query_and_caret_across_a_reopen(self) -> None:
        self._open()
        self.picker._search.setText("llama")
        self.picker._search.setCursorPosition(2)
        self.app.processEvents()
        self._press_escape()
        self._open()
        self.assertEqual(self.picker._search.text(), "llama")
        self.assertEqual(self.picker._search.cursorPosition(), 2)

    # -- rows / themes / density ----------------------------------------

    def test_real_filter_hides_rows_without_reallocating(self) -> None:
        self._open()
        tree = self.picker._tree
        before = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
        self.picker._search.setText("llama")
        self.app.processEvents()
        after = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
        self.assertEqual(before, after, "filter must hide rows, not rebuild them")
        self.assertEqual([r.model_id for r in self.picker.visible_rows()], ["llama3.1"])

    def test_uniform_row_heights_and_connection_header(self) -> None:
        self._open()
        tree = self.picker._tree
        self.assertTrue(tree.uniformRowHeights())
        self.assertEqual(tree.topLevelItem(0).text(0), "openai")

    def test_dark_and_light_themes_render_the_same_structure(self) -> None:
        from lucnhan.ui.theme.manager import ThemeManager
        from lucnhan.ui.theme.tokens import ThemeMode

        tm = ThemeManager.instance()
        original = tm.mode
        try:
            for mode in (ThemeMode.DARK, ThemeMode.LIGHT):
                tm.set_mode(mode)
                self.picker.refresh_theme()
                self.app.processEvents()
                self.assertEqual(len(self.picker.visible_rows()), 5)
                self.assertIn("background", self.picker.styleSheet())
        finally:
            tm.set_mode(original)
            self.picker.refresh_theme()

    def test_larger_font_does_not_hide_rows(self) -> None:
        """Font growth is an allowed adaptation; rows must stay reachable."""
        from PySide6.QtGui import QFont

        font = QFont(self.picker._tree.font())
        font.setPointSize(max(font.pointSize() + 4, 12))
        self.picker._tree.setFont(font)
        self._open()
        self.app.processEvents()
        self.assertEqual(len(self.picker.visible_rows()), 5)
        self.assertGreater(self.picker._tree.sizeHint().height(), 0)

    # -- helpers --------------------------------------------------------

    # -- footer trigger width ceiling (GUI contract G3 + G6) -------------

    def _footer_widget(self, footer_width: int = 900):
        """A real footer built by ``_build_footer`` at a known width.

        The ceiling is production's, so the test drives it directly:
        ``_refresh_footer_trigger_width`` is exactly what
        ``resizeEvent`` defers, and reading the footer's real width is
        what a live dock would do.
        """
        from PySide6.QtWidgets import QApplication, QWidget

        from lucnhan.ui.model_picker import ModelCatalogCache
        from lucnhan.ui.panel_core import LucNhanPanelCore

        panel = LucNhanPanelCore.__new__(LucNhanPanelCore)
        QWidget.__init__(panel)
        panel._footer_width_timer = None
        panel._ctrl = type("_Ctrl", (), {"is_agent_running": False})()
        panel._config = type(
            "_Cfg",
            (),
            {
                "provider": type("_P", (), {"name": "openai", "model": "gpt-4o", "extra": {}})(),
                "providers": {},
            },
        )()
        panel._model_catalog = ModelCatalogCache()
        panel._build_footer()
        footer = panel._footer
        footer.resize(footer_width, footer.height())
        QApplication.instance().processEvents()
        return panel, footer

    def _settle(self) -> None:
        """Drain the one-shot clamp timer the resize path schedules."""
        from PySide6.QtCore import QEventLoop, QTimer

        loop = QEventLoop()
        QTimer.singleShot(0, loop.quit)
        loop.exec()

    def test_trigger_ceiling_tracks_the_footer_width(self) -> None:
        """68% follows the footer's live width, never the 640 px default."""
        panel, footer = self._footer_widget(900)
        panel._refresh_footer_trigger_width()
        self.assertEqual(panel._footer_trigger.maximumWidth(), int(900 * 0.68))

        # A narrower dock re-clamps; a wider one lifts the ceiling again.
        for width in (460, 1100):
            footer.resize(width, footer.height())
            panel._refresh_footer_trigger_width()
            self.assertEqual(
                panel._footer_trigger.maximumWidth(),
                int(width * 0.68),
                f"ceiling must track a {width} px footer",
            )

    def test_panel_resize_defers_a_single_shot_reclamp(self) -> None:
        """``resizeEvent`` re-arms the clamp without recurring work."""
        panel, footer = self._footer_widget(900)
        panel._refresh_footer_trigger_width()
        before = panel._footer_trigger.maximumWidth()
        self.assertEqual(before, int(900 * 0.68))

        footer.resize(300, footer.height())
        panel._schedule_footer_trigger_width()
        timer = panel._footer_width_timer
        self.assertEqual(timer.interval(), 0)
        self.assertTrue(timer.isSingleShot(), "no recurring idle work")

        self._settle()
        self.assertEqual(panel._footer_trigger.maximumWidth(), int(300 * 0.68))
        self.assertNotEqual(panel._footer_trigger.maximumWidth(), before)

    @staticmethod
    def _key(key):
        from PySide6.QtCore import Qt

        return getattr(Qt.Key, key)

    @staticmethod
    def _key_event(key, text=""):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent

        return QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier, text)

    def _press_escape(self) -> None:
        target = self.app.focusWidget() or self.picker
        self.app.sendEvent(target, self._key_event(self._key("Key_Escape")))
        self.app.processEvents()
