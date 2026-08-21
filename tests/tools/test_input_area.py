"""Tests for rikugan.ui.input_area — pure logic helpers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

# Defensive: drop any ``_StubModule`` entries a sibling test file
# (e.g. ``tests/tools/test_settings_dialog.py`` or
# ``tests/tools/test_panel_core.py``) left in ``sys.modules`` before
# we import the real rikugan modules.  Without this purge, the
# input-area tests see a ``MagicMock`` ``InputArea`` class and fail
# with ``TypeError: object.__new__(X): X is not a type object`` on
# the first ``_make_input()`` call.  See
# ``tests/providers/test_providers.py`` for the canonical pattern.
from tests import purge_rikugan_stubs

purge_rikugan_stubs()

from tests.qt_stubs import ensure_pyside6_stubs  # noqa: E402

ensure_pyside6_stubs()

from rikugan.ui.input_area import InputArea, _SkillPopup  # noqa: E402

# ---------------------------------------------------------------------------
# _SkillPopup — pure list logic
# ---------------------------------------------------------------------------

def _make_popup() -> _SkillPopup:
    popup = object.__new__(_SkillPopup)
    popup._slugs = []
    popup._selected_idx = 0
    popup._items = []
    return popup


class TestSkillPopupCurrentSlug(unittest.TestCase):
    def test_empty_list_returns_none(self):
        popup = _make_popup()
        self.assertIsNone(popup.current_slug())

    def test_returns_first_item_by_default(self):
        popup = _make_popup()
        popup._slugs = ["plan", "modify", "explore"]
        self.assertEqual(popup.current_slug(), "plan")

    def test_returns_selected_item(self):
        popup = _make_popup()
        popup._slugs = ["a", "b", "c"]
        popup._selected_idx = 2
        self.assertEqual(popup.current_slug(), "c")

    def test_out_of_bounds_idx_returns_none(self):
        popup = _make_popup()
        popup._slugs = ["a"]
        popup._selected_idx = 5
        self.assertIsNone(popup.current_slug())


class TestSkillPopupIsEmpty(unittest.TestCase):
    def test_empty_list(self):
        popup = _make_popup()
        self.assertTrue(popup.is_empty())

    def test_non_empty_list(self):
        popup = _make_popup()
        popup._slugs = ["plan"]
        self.assertFalse(popup.is_empty())


class TestSkillPopupMoveSelection(unittest.TestCase):
    def test_wraps_forward(self):
        popup = _make_popup()
        popup._slugs = ["a", "b", "c"]
        popup._selected_idx = 2
        popup._update_highlight = MagicMock()
        popup.move_selection(1)
        self.assertEqual(popup._selected_idx, 0)

    def test_wraps_backward(self):
        popup = _make_popup()
        popup._slugs = ["a", "b", "c"]
        popup._selected_idx = 0
        popup._update_highlight = MagicMock()
        popup.move_selection(-1)
        self.assertEqual(popup._selected_idx, 2)

    def test_no_move_when_empty(self):
        popup = _make_popup()
        popup._selected_idx = 0
        popup._update_highlight = MagicMock()
        popup.move_selection(1)
        self.assertEqual(popup._selected_idx, 0)


# ---------------------------------------------------------------------------
# InputArea — pure logic methods
# ---------------------------------------------------------------------------

def _make_input() -> InputArea:
    area = object.__new__(InputArea)
    area._enabled = True
    area._skill_slugs = []
    area._popup = None
    area._submit_callback = None
    area._cancel_callback = None
    return area


class TestInputAreaSetCallbacks(unittest.TestCase):
    def test_set_submit_callback(self):
        area = _make_input()
        cb = MagicMock()
        area.set_submit_callback(cb)
        self.assertIs(area._submit_callback, cb)

    def test_set_cancel_callback(self):
        area = _make_input()
        cb = MagicMock()
        area.set_cancel_callback(cb)
        self.assertIs(area._cancel_callback, cb)


class TestInputAreaSetSkillSlugs(unittest.TestCase):
    def test_includes_builtin_commands(self):
        area = _make_input()
        # /report is a registered built-in skill (SkillRegistry); the
        # controller merges registry slugs into set_skill_slugs so the
        # input area surfaces it. Passing it explicitly here models
        # that contract and locks the migration in.
        area.set_skill_slugs(["report"])
        self.assertIn("plan", area._skill_slugs)
        self.assertIn("modify", area._skill_slugs)
        self.assertIn("explore", area._skill_slugs)
        self.assertIn("report", area._skill_slugs)
    def test_merges_custom_slugs(self):
        area = _make_input()
        area.set_skill_slugs(["decompile", "rename"])
        self.assertIn("decompile", area._skill_slugs)
        self.assertIn("rename", area._skill_slugs)

    def test_result_is_sorted(self):
        area = _make_input()
        area.set_skill_slugs(["z", "a", "m"])
        self.assertEqual(area._skill_slugs, sorted(area._skill_slugs))

    def test_no_duplicates(self):
        area = _make_input()
        area.set_skill_slugs(["plan", "plan", "explore"])
        self.assertEqual(area._skill_slugs.count("plan"), 1)


class TestInputAreaCheckAutocomplete(unittest.TestCase):
    def test_no_slash_dismisses_popup(self):
        area = _make_input()
        area._skill_slugs = ["plan"]
        area._dismiss_popup = MagicMock()
        area.toPlainText = MagicMock(return_value="hello")
        area._check_autocomplete()
        area._dismiss_popup.assert_called_once()

    def test_slash_with_space_dismisses_popup(self):
        area = _make_input()
        area._skill_slugs = ["plan"]
        area._dismiss_popup = MagicMock()
        area.toPlainText = MagicMock(return_value="/plan some text")
        area._check_autocomplete()
        area._dismiss_popup.assert_called_once()

    def test_partial_slug_shows_popup(self):
        area = _make_input()
        area._skill_slugs = ["plan", "modify"]
        area._show_popup = MagicMock()
        area._dismiss_popup = MagicMock()
        area.toPlainText = MagicMock(return_value="/pl")
        area._check_autocomplete()
        area._show_popup.assert_called_once_with(["plan"])
    def test_slash_after_setting_slugs_shows_suggestions(self):
        """Typing ``/`` after normal setup must open the full command list."""
        area = _make_input()
        area.set_skill_slugs(["decompile"])
        area._show_popup = MagicMock()
        area._dismiss_popup = MagicMock()
        area.toPlainText = MagicMock(return_value="/")

        area._check_autocomplete()

        matches = area._show_popup.call_args.args[0]
        self.assertIn("decompile", matches)
        self.assertIn("plan", matches)
        self.assertIn("verify", matches)

    def test_no_match_dismisses_popup(self):
        area = _make_input()
        area._skill_slugs = ["plan", "modify"]
        area._dismiss_popup = MagicMock()
        area.toPlainText = MagicMock(return_value="/xyz")
        area._check_autocomplete()
        area._dismiss_popup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
