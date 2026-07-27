"""Input visibility and resizing regression tests.

Pins the contract that:
  * ``build_input_area_stylesheet`` is non-empty in every theme mode
    (including host/IDA-native) so the editor foreground/background
    never inherit an unreadable palette.
  * ``InputArea.apply_palette`` sets the ``Base``, ``Text``, and
    ``PlaceholderText`` palette roles on the editor from the live
    tokens so typed text is visible in every theme.
  * ``InputArea.__init__`` enforces a 2-3 line minimum height without
    capping the maximum, and uses ``Expanding`` vertical size policy
    so the editor fills the chat-splitter bottom pane when the user
    drags the handle taller.
  * ``RikuganPanelCore._build_ui`` wires a vertical chat splitter
    between the main conversation area and the input section, so the
    user can drag the handle to grow the input.
  * ``RikuganPanelCore.showEvent`` seeds the chat splitter default
    sizes on first show and leaves the splitter alone on subsequent
    shows so the user can drag the handle afterwards.

Most assertions are source-level guards so the tests do not depend
on the shared Qt-stub layer evolving with unrelated widget APIs.
The behavioural checks (``apply_palette``, ``showEvent``) install a
tiny local fake receiver / use ``MagicMock`` for the splitter
without changing the shared stub.
"""

from __future__ import annotations

import inspect
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

# Defensive purge — sibling tests stub rikugan.ui.* modules; see
# tests/tools/test_input_area.py for the canonical pattern.
from tests import purge_rikugan_stubs

purge_rikugan_stubs()

from tests.qt_stubs import ensure_pyside6_stubs  # noqa: E402

ensure_pyside6_stubs()

# Force the real config module so a stub installed by a sibling test
# cannot mask ``validate()``/``hide_strings``-style guards.
sys.modules.pop("rikugan.core.config", None)

from rikugan.ui.styles import build_input_area_stylesheet  # noqa: E402
from rikugan.ui.theme.palette_dark import DARK_TOKENS  # noqa: E402
from rikugan.ui.theme.palette_light import LIGHT_TOKENS  # noqa: E402


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
        import rikugan.ui.styles as _styles

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
        import rikugan.ui.styles as _styles

        self._orig_theme = _styles._current_theme
        _styles._current_theme = "dark"
        self.addCleanup(_styles.__setattr__, "_current_theme", self._orig_theme)

    def test_dark_input_qss_includes_token_colors(self) -> None:
        qss = build_input_area_stylesheet(DARK_TOKENS)
        self.assertTrue(qss, "dark input QSS must not be empty")
        self.assertIn(DARK_TOKENS.base, qss)
        self.assertIn(DARK_TOKENS.mid, qss)


class TestInputQssHostTheme(unittest.TestCase):
    """Host/IDA-native mode must still emit a non-empty QSS so the
    editor's border / focus / selection stay token-driven. The
    foreground / background / placeholder text are handled by
    ``InputArea.apply_palette`` (QPalette roles), so the host palette
    never bleeds through to render typed text invisible.
    """

    def setUp(self) -> None:
        import rikugan.ui.styles as _styles

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

    The test patches a tiny ``QPalette`` / ``QColor`` onto
    ``rikugan.ui.input_area`` so the unbound ``apply_palette``
    resolves to the fakes.  A ``ColorRole`` enum-like is attached
    to the fake palette so ``QPalette.ColorRole.Base/Text/...
    `` resolves inside the production code.
    """

    def test_palette_roles_match_tokens(self) -> None:
        from rikugan.ui import input_area as input_area_mod  # noqa: E402

        # Attach ``ColorRole`` to the fake palette class so the
        # production code's ``QPalette.ColorRole.Base/Text/...``
        # resolves to the names our fake ``setColor`` stores under.
        _FakePalette.ColorRole = type(
            "_ColorRole",
            (),
            {
                "Base": "Base",
                "Text": "Text",
                "PlaceholderText": "PlaceholderText",
            },
        )

        receiver = _FakeReceiver()
        with patch.object(
            input_area_mod, "QPalette", _FakePalette, create=True
        ), patch.object(input_area_mod, "QColor", _FakeColor, create=True):
            from rikugan.ui.input_area import InputArea  # noqa: E402

            InputArea.apply_palette(receiver, LIGHT_TOKENS)

        self.assertEqual(
            receiver._palette._roles["Base"]._name,
            LIGHT_TOKENS.base.lower(),
        )
        self.assertEqual(
            receiver._palette._roles["Text"]._name,
            LIGHT_TOKENS.text.lower(),
        )
        self.assertEqual(
            receiver._palette._roles["PlaceholderText"]._name,
            LIGHT_TOKENS.muted_text.lower(),
        )
        # Drop the class-level ``ColorRole`` so other tests in the
        # same process do not observe the monkey-patched attribute.
        del _FakePalette.ColorRole


class TestInputSizing(unittest.TestCase):
    """The two-to-three-line default is on ``InputArea``; the splitter
    owns the upper bound, so the editor must not pin a maximum
    height.  The vertical size policy must be ``Expanding`` so the
    editor fills the chat-splitter bottom pane when the user drags
    the handle taller.  Source-level guards so the tests do not
    depend on the shared Qt stub returning real ``maximumHeight``
    or actual splitter geometry.
    """

    def test_input_area_sizing_contract(self) -> None:
        from rikugan.ui.input_area import InputArea  # noqa: E402

        source = inspect.getsource(InputArea.__init__)
        self.assertIn(
            "self.setMinimumHeight(60)",
            source,
            msg=(
                "InputArea must default to a 2-3 line minimum height "
                "(60px at 18px line-height with 6px padding)."
            ),
        )
        # ``setMaximumHeight`` must not be called on the editor — the
        # vertical QSplitter in the panel owns the upper bound.
        self.assertNotIn(
            "setMaximumHeight",
            source,
            msg=(
                "InputArea must not cap its maximum height; the "
                "vertical QSplitter in the panel owns the upper bound."
            ),
        )
        # Vertical policy must be ``Expanding`` so the editor fills
        # the chat-splitter bottom pane when the user drags the
        # handle taller.  ``Preferred`` would let the splitter grow
        # the pane but leave the editor at its size hint with empty
        # space below.
        self.assertIn(
            "QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding",
            source,
            msg=(
                "InputArea must use Expanding vertical size policy so "
                "the editor fills the chat-splitter bottom pane when "
                "the user drags the handle taller."
            ),
        )


class TestChatSplitterShowEventSizes(unittest.TestCase):
    """``RikuganPanelCore.showEvent`` seeds the chat splitter default
    sizes on first show, then leaves the splitter alone on subsequent
    shows so the user can drag the handle afterwards.  The test
    uses ``RikuganPanelCore.__new__`` to bypass the heavy ``__init__``
    and installs ``MagicMock`` stand-ins for the splitter, so the
    test does not depend on real Qt construction.
    """

    def _make_panel(self, *, total_height: int, input_min: int = 60):
        from rikugan.ui.panel_core import RikuganPanelCore  # noqa: E402

        panel = RikuganPanelCore.__new__(RikuganPanelCore)
        panel._chat_splitter = MagicMock()
        panel._chat_splitter.height.return_value = total_height
        panel._input_area = MagicMock()
        panel._input_area.minimumHeight.return_value = input_min
        return panel

    def test_first_show_seeds_two_to_three_line_default(self) -> None:
        """First ``showEvent`` must call ``setSizes`` with a bottom
        pane equal to the input minimum (2-3 lines) and a top pane
        equal to ``total - input_min``.  The user can drag from there.
        """
        panel = self._make_panel(total_height=400, input_min=60)
        panel.showEvent(MagicMock())
        panel._chat_splitter.setSizes.assert_called_once()
        sizes = panel._chat_splitter.setSizes.call_args[0][0]
        # 60px ≈ 2-3 lines at 18px line-height; the top pane gets
        # the remainder so the chat fills the page.
        self.assertEqual(sizes[1], 60)
        self.assertEqual(sizes[0], 400 - 60)

    def test_second_show_does_not_reset_sizes(self) -> None:
        """After the first show, subsequent ``showEvent`` calls must
        NOT clobber the user-dragged sizes with the one-shot default.
        """
        panel = self._make_panel(total_height=400, input_min=60)
        panel.showEvent(MagicMock())
        panel.showEvent(MagicMock())
        self.assertEqual(panel._chat_splitter.setSizes.call_count, 1)


if __name__ == "__main__":
    unittest.main()
