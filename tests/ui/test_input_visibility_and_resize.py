"""Input visibility and resizing regression tests.

Pins the contract that:
  * ``build_input_area_stylesheet`` is non-empty in every theme mode
    (including host/IDA-native) so the editor foreground/background
    never inherit an unreadable palette.
  * ``InputArea.apply_palette`` sets the ``Base``, ``Text``, and
    ``PlaceholderText`` palette roles on the editor from the live
    tokens so typed text is visible in every theme.
  * ``LucNhanPanelCore.showEvent`` seeds the chat splitter default
    sizes on first show and leaves the splitter alone on subsequent
    shows so the user can drag the handle afterwards.

The editor's *geometry* (min 38 px, ``Expanding`` vertical policy, no
maximum cap) is a real-widget property, so it is covered by
``tests/ui/test_input_geometry_smoke.py`` instead of being pinned as
source text here.

The ``apply_palette`` check installs a tiny local fake receiver and the
splitter check uses ``MagicMock``, without changing the shared stub.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

# Defensive purge — sibling tests stub lucnhan.ui.* modules; see
# tests/tools/test_input_area.py for the canonical pattern.
from tests import purge_lucnhan_stubs

purge_lucnhan_stubs()

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()

# Force the real config module so a stub installed by a sibling test
# cannot mask ``validate()``/``hide_strings``-style guards.
sys.modules.pop("lucnhan.core.config", None)

from lucnhan.ui.styles import build_input_area_stylesheet
from lucnhan.ui.theme.palette_dark import DARK_TOKENS
from lucnhan.ui.theme.palette_light import LIGHT_TOKENS
from tests.qt_real import live_class


class _FakeColor:
    """Minimal QColor stand-in that round-trips the token name string."""

    def __init__(self, name: str) -> None:
        self._name = str(name).lower()

    def name(self) -> str:
        return self._name


class _FakePalette:
    """Minimal QPalette stand-in that stores ``QColor`` per role.

    Mirrors the small subset of PySide6 QPalette that ``InputArea``
    actually reads (``Base``, ``Text``, ``PlaceholderText``) so the
    palette contract is testable without depending on real PySide6
    or a broad shared stub.
    """

    def __init__(self) -> None:
        self._roles: dict = {}

    def setColor(self, role, color) -> None:
        # Accept enum-like and int roles by using the role's ``name``.
        key = getattr(role, "name", None) or str(role)
        self._roles[key] = color

    def color(self, role):
        key = getattr(role, "name", None) or str(role)
        return self._roles.get(key, _FakeColor("#000000"))


class _FakeReceiver:
    """Tiny widget stand-in: ``palette()`` returns a fresh fake
    palette; ``setPalette(p)`` records the assignment.  ``InputArea``
    only ever reads ``self.palette()`` and writes ``self.setPalette(p)``
    in ``apply_palette``, so this is the minimum surface needed.
    """

    def __init__(self) -> None:
        self._palette = _FakePalette()

    def palette(self):
        return self._palette

    def setPalette(self, palette) -> None:
        self._palette = palette


class TestInputQssLightTheme(unittest.TestCase):
    def setUp(self) -> None:
        import lucnhan.ui.styles as _styles

        self._orig_theme = _styles._current_theme
        _styles._current_theme = "light"
        self.addCleanup(_styles.__setattr__, "_current_theme", self._orig_theme)

    def test_light_input_qss_is_object_name_scoped(self) -> None:
        qss = build_input_area_stylesheet(LIGHT_TOKENS)
        self.assertTrue(qss, "light input QSS must not be empty")
        # Scoped to the editor's object name so the styles do not
        # bleed into other plain text editors in the host.
        self.assertTrue(
            qss.startswith("QPlainTextEdit#input_area"),
            f"QSS must be scoped to #input_area; got: {qss[:80]!r}",
        )


class TestInputQssDarkTheme(unittest.TestCase):
    def setUp(self) -> None:
        import lucnhan.ui.styles as _styles

        self._orig_theme = _styles._current_theme
        _styles._current_theme = "dark"
        self.addCleanup(_styles.__setattr__, "_current_theme", self._orig_theme)

    def test_dark_input_qss_includes_token_colors(self) -> None:
        qss = build_input_area_stylesheet(DARK_TOKENS)
        self.assertTrue(qss, "dark input QSS must not be empty")
        self.assertIn(DARK_TOKENS.base, qss)
        self.assertIn(DARK_TOKENS.highlight, qss)


class TestInputQssHostTheme(unittest.TestCase):
    """Host/IDA-native mode must still emit a non-empty QSS so the
    editor's selection colors stay token-driven. The
    foreground / background / placeholder text are handled by
    ``InputArea.apply_palette`` (QPalette roles), so the host palette
    never bleeds through to render typed text invisible.
    """

    def setUp(self) -> None:
        import lucnhan.ui.styles as _styles

        self._orig_theme = _styles._current_theme
        _styles._current_theme = "ida"
        self.addCleanup(_styles.__setattr__, "_current_theme", self._orig_theme)

    def test_input_qss_not_empty_in_host_mode(self) -> None:
        qss = build_input_area_stylesheet(LIGHT_TOKENS)
        self.assertTrue(qss, "input QSS must be non-empty in host mode")
        self.assertIn("QPlainTextEdit#input_area", qss)


class TestInputPalette(unittest.TestCase):
    """``InputArea.apply_palette`` sets ``Base`` / ``Text`` /
    ``PlaceholderText`` on the editor's ``QPalette`` from the live
    tokens so typed text is visible in every theme.

    The test patches a tiny ``QPalette`` / ``QColor`` into the module
    that OWNS the ``InputArea`` class under test, so the unbound
    ``apply_palette`` resolves to the fakes.  A ``ColorRole`` enum-like
    is attached to the fake palette so ``QPalette.ColorRole.Base/Text/...
    `` resolves inside the production code.
    """

    def test_palette_roles_match_tokens(self) -> None:
        # Resolve the class first and patch through ITS module: a
        # sibling test can leave ``lucnhan.ui.input_area`` swapped for a
        # stub (importable as an attribute of the ``lucnhan.ui``
        # package), so patching one module object while calling a class
        # from another silently patches nothing.
        input_area_cls = live_class("lucnhan.ui.input_area.InputArea")
        input_area_module = sys.modules[input_area_cls.__module__]

        # Attach ``ColorRole`` to the fake palette class so the
        # production code's ``QPalette.ColorRole.Base/Text/...``
        # resolves to the names our fake ``setColor`` stores under.
        _FakePalette.ColorRole = type(
            "_ColorRole",
            (),
            {
                "Base": "Base",
                "Text": "Text",
                "WindowText": "WindowText",
                "PlaceholderText": "PlaceholderText",
            },
        )

        receiver = _FakeReceiver()
        with patch.dict(
            vars(input_area_module),
            {"QPalette": _FakePalette, "QColor": _FakeColor},
        ):
            input_area_cls.apply_palette(receiver, LIGHT_TOKENS)

        self.assertEqual(
            receiver._palette._roles["Base"]._name,
            LIGHT_TOKENS.base.lower(),
        )
        self.assertEqual(
            receiver._palette._roles["Text"]._name,
            LIGHT_TOKENS.text.lower(),
        )
        self.assertEqual(
            receiver._palette._roles["WindowText"]._name,
            LIGHT_TOKENS.text.lower(),
        )
        self.assertEqual(
            receiver._palette._roles["PlaceholderText"]._name,
            LIGHT_TOKENS.muted_text.lower(),
        )
        # Drop the class-level ``ColorRole`` so other tests in the
        # same process do not observe the monkey-patched attribute.
        del _FakePalette.ColorRole


class TestChatSplitterShowEventSizes(unittest.TestCase):
    """``LucNhanPanelCore.showEvent`` seeds the chat splitter default
    sizes on first show, then leaves the splitter alone on subsequent
    shows so the user can drag the handle afterwards.  The test
    uses ``LucNhanPanelCore.__new__`` to bypass the heavy ``__init__``
    and installs ``MagicMock`` stand-ins for the splitter, so the
    test does not depend on real Qt construction.
    """

    def _make_panel(self, *, total_height: int):
        from lucnhan.ui.panel_core import LucNhanPanelCore

        panel = LucNhanPanelCore.__new__(LucNhanPanelCore)
        panel._chat_splitter = MagicMock()
        panel._chat_splitter.height.return_value = total_height
        return panel

    def test_first_show_seeds_composer_baseline(self) -> None:
        """The one-shot seed pins the composer to its GUI contract G2
        baseline (44 px editor + action row); the transcript above takes
        the remainder.  The user can drag from there.
        """
        panel = self._make_panel(total_height=400)
        panel._seed_chat_splitter()
        panel._chat_splitter.setSizes.assert_called_once()
        sizes = panel._chat_splitter.setSizes.call_args[0][0]
        self.assertEqual(sizes[1], 44 + 34)
        self.assertEqual(sizes[0], 400 - (44 + 34))

    def test_second_show_does_not_reset_sizes(self) -> None:
        """After the first seed, later seeds must NOT clobber the
        user-dragged sizes with the one-shot default.
        """
        panel = self._make_panel(total_height=400)
        panel._seed_chat_splitter()
        panel._seed_chat_splitter()
        panel._seed_chat_splitter()
        self.assertEqual(panel._chat_splitter.setSizes.call_count, 1)


if __name__ == "__main__":
    unittest.main()
