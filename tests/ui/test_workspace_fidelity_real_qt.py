"""REAL-Qt regression for the workspace chrome / theme repair.

Every check here pins a *painted* or *measured* property — a resolved
``QPalette`` role, a grabbed pixel, real geometry, real tab/close
behaviour — because the defects it covers were invisible to the Qt
stubs: stylesheets lost on reparent, a 0 px status label, a transcript
painted from the application palette, and chrome that was never
re-applied on a theme switch.

Isolation is the other half of the job.  ``LucNhanPanelCore``'s real
constructor loads the user's config from disk, runs the OAuth consent
restore and spawns the provider auth warm-up, so every external edge is
patched for the panel's whole lifetime: an in-memory
``LucNhanConfig`` (no ``load()``), no consent prompt, no auth thread, no
provider-registry scan and no startup history probe (which would spawn a
worker that reads the session store).  The panel's own
``shutdown()``/``deleteLater()`` path runs afterwards, then the
``ThemeManager`` singleton is restored.

``tests.qt_real.requires_real_qt`` skips the module when a sibling test
installed the stub bindings.  Run standalone::

    pytest tests/ui/test_workspace_fidelity_real_qt.py
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Import the real Qt classes through the seam below, but only when this
# session really has real PySide6.  In a full-suite collection a sibling
# test installs ``tests.qt_stubs`` first, which replaces the PySide6
# modules; importing against those stubs raises at import time and takes
# the whole collection down with it.  pytest treats a module-level
# SkipTest as a skip, and a standalone real-Qt run is unaffected.
from tests.qt_real import real_qt_available, requires_real_qt

if not real_qt_available():
    raise unittest.SkipTest(
        "real PySide6 is unavailable: a sibling test installed tests.qt_stubs; run this file on its own to exercise it"
    )

from lucnhan.state.session import SessionState
from lucnhan.ui.qt_compat import (
    QApplication,
    QEvent,
    QEventLoop,
    QKeyEvent,
    QPalette,
    Qt,
    QTabBar,
    QTimer,
    QWidget,
)

# GUI contract G3: the only narrow breakpoint, in logical px.
_NARROW_PX = 460
_WIDE_PX = 1100
_HEIGHT_PX = 800


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv)


def _press_key(widget: QWidget, key) -> None:
    """Deliver a real key press/release pair to *widget*.

    IDA's bundled PySide6 ships no ``QtTest``, so events go through
    ``QApplication.sendEvent`` with the real ``QKeyEvent`` — the same
    path the native smoke uses.  ``InputArea.keyPressEvent`` is reached
    through the normal Qt dispatch, so the test exercises the shipped
    keyboard handling rather than calling it directly.
    """
    app = _app()
    for event_type in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
        app.sendEvent(widget, QKeyEvent(event_type, key, Qt.KeyboardModifier.NoModifier))


def _settle(ms: int = 400) -> None:
    """Drain the real event loop past the 50 ms theme debounce.

    The earlier smoke settled ``_pump`` loops only and labelled Dark /
    Light from styles that had not been rebuilt yet; every switch below
    runs through a real ``QEventLoop`` so the paint actually happened.
    """
    app = _app()
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    app.processEvents()


def _set_theme(panel, mode: str) -> None:
    from lucnhan.ui.theme.manager import ThemeManager
    from lucnhan.ui.theme.tokens import ThemeMode

    panel.set_theme(mode)
    ThemeManager.instance().set_mode(ThemeMode(mode))
    _settle()


def _resolved(widget, role) -> str:
    """The palette color a widget *actually* resolves for *role*.

    ``QWidget.palette()`` walks the widget/parent/application chain, so
    this is what the painter uses — not what any stylesheet says.
    """
    return widget.palette().color(role).name()


def _pixel(widget, point) -> str:
    """Grab the widget and read one painted pixel.

    ``point`` is in **logical** px relative to the widget's top-left; the
    grab image is in device pixels, so it is scaled by ``devicePixelRatioF``
    before the lookup (the native smoke runs at 1.5x, where an unscaled
    sample lands on the wrong pixel and a rounded corner reads as an
    antialiased blend).
    """
    image = widget.grab().toImage()
    ratio = widget.devicePixelRatioF() or 1.0
    x = min(max(round(point[0] * ratio), 0), image.width() - 1)
    y = min(max(round(point[1] * ratio), 0), image.height() - 1)
    return image.pixelColor(x, y).name()


def _border_pixel(widget) -> str:
    """One pixel from the middle of a widget's top border.

    A corner is rounded and antialiased, so it blends border and surface
    colors; the middle of a straight edge does not.
    """
    return _pixel(widget, (widget.width() // 2, 0))


def _painted(widget) -> str:
    """The dominant color actually painted on the widget.

    Reads a grid of interior pixels and returns the most common one, so a
    label or icon occupying one sample cannot decide the answer.  This is
    the consumer-visible check: it reports what a user sees, not what a
    palette object claims.
    """
    from collections import Counter

    image = widget.grab().toImage()
    ratio = widget.devicePixelRatioF() or 1.0
    colors = Counter()
    for fx in (0.2, 0.35, 0.5, 0.65, 0.8):
        for fy in (0.25, 0.5, 0.75):
            x = min(max(round(widget.width() * fx * ratio), 0), image.width() - 1)
            y = min(max(round(widget.height() * fy * ratio), 0), image.height() - 1)
            colors[image.pixelColor(x, y).name().lower()] += 1
    return colors.most_common(1)[0][0]


class _StubController:
    """The controller *seams* the panel touches, and nothing else.

    Every method here either returns inert data or raises if the panel
    reaches for something that would hit disk / the network, so an
    accidental new dependency fails loudly instead of touching the
    user's config or credentials.
    """

    def __init__(self) -> None:
        self.active_tab_id = "tab-0"
        self.is_agent_running = False
        self.runtime_ready = True
        self.skill_slugs: list[str] = []
        # ``_on_new_tab`` reads ``session`` before it creates anything; a
        # fresh session has no messages, so the panel takes the plain
        # "create a new tab" path.
        self.session = SessionState()
        self._next = 0
        self._tabs = ["tab-0"]
        self._idb_path = ""
        # Per-tab undo stacks, addressed by the panel's active-session
        # scoped Mutations pane (``SessionControllerBase.mutation_records``
        # returns a private copy of exactly this, records a successful
        # /undo removed excluded).
        self.mutations: dict[str, list] = {}
        # Per-tab run state, so a test can model "tab A is still
        # streaming while tab B is idle" the way the panel sees it.
        self.running: set[str] = set()

    def mutation_records(self, tab_id: str | None = None) -> list:
        """Inert, real snapshot: a copy of the tab's own records."""
        tid = tab_id if tab_id is not None else self.active_tab_id
        return list(self.mutations.get(tid, []))

    def create_tab(self) -> str:
        self._next += 1
        tab_id = f"tab-{self._next}"
        self._tabs.append(tab_id)
        return tab_id

    def close_tab(self, tab_id: str) -> None:
        """Real removal, so a close cannot silently no-op in the fixture."""
        if tab_id in self._tabs:
            self._tabs.remove(tab_id)

    def new_chat(self) -> None:
        self.mutations.pop(self.active_tab_id, None)
        self.running.discard(self.active_tab_id)
        self.is_agent_running = bool(self.running)
        self.session = SessionState()

    def reset_for_new_file(self, path: str) -> None:
        self._idb_path = path
        self._tabs.clear()
        self.mutations.clear()
        self.running.clear()
        self.is_agent_running = False
        self.active_tab_id = self.create_tab()
        self.session = SessionState()

    def cancel(self, tab_id: str | None = None) -> None:
        """Stop one tab only — the real controller cancels per tab."""
        self.running.discard(tab_id if tab_id is not None else self.active_tab_id)
        self.is_agent_running = bool(self.running)

    def get_runner(self, tab_id: str):
        """Inert runner — the panel only forwards the decision to it."""
        return None

    def is_tab_running(self, tab_id: str) -> bool:
        return tab_id in self.running

    def switch_tab(self, tab_id: str) -> None:
        self.active_tab_id = tab_id

    def tab_label(self, tab_id: str) -> str:
        return f"Session {tab_id}"

    def shutdown(self) -> None:
        """Release the inert run state when the real panel tears down."""
        self.running.clear()
        self.is_agent_running = False

    def __getattr__(self, name):
        def _forbidden(*_args, **_kwargs):
            raise AssertionError(f"controller seam {name!r} must not run in this test")

        return _forbidden


class _WorkspacePanelTest(unittest.TestCase):
    """Shared harness: one isolated panel, torn down deterministically.

    Not decorated and not named ``Test*`` — pytest must not collect it;
    the real cases are the subclasses below.
    """

    def setUp(self) -> None:
        _app()
        from lucnhan.core.config import LucNhanConfig
        from lucnhan.ui.panel_core import LucNhanPanelCore
        from lucnhan.ui.theme.manager import ThemeManager

        # A fresh manager for this test: the AUTO/IDA palette source is a
        # one-shot registration, so a leaked source from a previous test
        # would silently change what the host branch derives from.
        ThemeManager.reset()
        self._manager = ThemeManager.instance()
        self._mode = self._manager.mode
        self.addCleanup(self._restore_manager)

        config = LucNhanConfig()
        config.theme = "dark"
        controller = _StubController()
        # Kept so a test can build a second panel (reopen) with the same
        # isolated seams instead of re-patching.
        self._controller = controller
        self._config = config
        self._patches = [
            patch.object(LucNhanConfig, "load_or_create", classmethod(lambda cls: config)),
            patch.object(LucNhanPanelCore, "_check_oauth_consent", lambda self: None),
            patch.object(LucNhanPanelCore, "_resolve_dependency_warnings", lambda self: None),
            patch.object(LucNhanPanelCore, "_arm_startup_restore_if_idle", lambda self: None),
            patch("lucnhan.ui.panel_core.resolve_auth_cached", lambda: None),
        ]
        for started in self._patches:
            started.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

        self._panel = LucNhanPanelCore(controller_factory=lambda _cfg: controller)
        self.addCleanup(self._teardown_panel)
        self._panel.resize(_WIDE_PX, _HEIGHT_PX)
        self._panel.show()
        _settle()

    def _retire(self, widget) -> None:
        """Run the real shutdown on a panel the test built itself.

        Tolerant of a plain widget (the font test retires the form's root),
        which has no panel shutdown to run.
        """
        from lucnhan.ui.panel_core import LucNhanPanelCore

        if widget is None:
            return
        if hasattr(widget, "_is_shutdown"):
            LucNhanPanelCore.shutdown(widget)
        widget.close()
        widget.deleteLater()
        _app().processEvents()

    def _teardown_panel(self) -> None:
        """Run the real shutdown, then drop the C++ object."""
        panel, self._panel = self._panel, None
        if panel is None:
            return
        # The panel must not survive the test: its theme subscription and
        # timers outlive the QObject otherwise.
        from lucnhan.ui.panel_core import LucNhanPanelCore

        LucNhanPanelCore.shutdown(panel)
        panel.close()
        panel.deleteLater()
        _app().processEvents()

    def _restore_manager(self) -> None:
        from lucnhan.ui.theme.manager import ThemeManager

        self._manager.set_mode(self._mode)
        ThemeManager.reset()


@requires_real_qt
class TestThemeSurfaces(_WorkspacePanelTest):
    """The panel root and transcript must paint from the theme tokens.

    Before the repair both resolved through ``QApplication``'s Window
    role, which in a host is the platform default — so the transcript
    stayed the host's ``#282a36`` under Light while every other region
    followed the tokens.
    """

    def test_surfaces_paint_the_selected_theme(self) -> None:
        """What the user actually sees on the panel and the transcript.

        Checked on painted pixels rather than the resolved QPalette: the
        role objects can report a stale value while the paint is correct
        (and vice versa), so the consumer check is the paint.
        """
        from lucnhan.ui.theme.palette_dark import DARK_TOKENS
        from lucnhan.ui.theme.palette_light import LIGHT_TOKENS

        for mode, tokens in (("dark", DARK_TOKENS), ("light", LIGHT_TOKENS)):
            with self.subTest(mode=mode):
                _set_theme(self._panel, mode)
                view = self._panel._chat_views[self._panel._ctrl.active_tab_id]
                for surface in (self._panel, self._panel._content_area, view):
                    with self.subTest(surface=surface.objectName()):
                        self.assertEqual(
                            _painted(surface).lower(),
                            tokens.window.lower(),
                            f"{surface.objectName()} must paint the {mode} surface",
                        )

    def test_transcript_pixels_follow_the_theme(self) -> None:
        """The transcript itself, not just the palette bookkeeping."""
        from lucnhan.ui.theme.palette_dark import DARK_TOKENS
        from lucnhan.ui.theme.palette_light import LIGHT_TOKENS

        for mode, tokens in (("dark", DARK_TOKENS), ("light", LIGHT_TOKENS)):
            with self.subTest(mode=mode):
                _set_theme(self._panel, mode)
                view = self._panel._chat_views[self._panel._ctrl.active_tab_id]
                painted = _pixel(view, (4, 4))
                self.assertEqual(painted.lower(), tokens.window.lower())

    def _host_window(self) -> str:
        """The host's Window color, measured on a widget we never touch.

        A fresh parentless widget that Luc Nhan has no reference to and
        has never styled: whatever the host resolves for it is the host
        palette.  This is the independent reference the AUTO/IDA tokens
        must match — deriving from the panel instead would just read the
        plugin's own colors back.
        """
        from lucnhan.ui.qt_compat import QWidget

        probe = QWidget()
        try:
            probe.ensurePolished()
            return probe.palette().color(QPalette.ColorRole.Window).name().lower()
        finally:
            probe.deleteLater()
            _app().processEvents()

    def _as_ida(self):
        """Make the session look like an IDA host for AUTO/IDA derives.

        ``ThemeManager._compute_tokens`` asks ``core.host.is_ida()`` at call
        time and falls back to the Dark palette when it is False, so a host
        test has to make that call answer True.  Patching the single
        function is enough: no ``ida_kernwin`` / ``idaapi`` module is faked
        and ``QApplication`` is never touched.
        """
        patcher = patch("lucnhan.core.host.is_ida", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def _set_host_theme(self, mode: str = "ida") -> None:
        """Switch mode *and* refresh, so the derive happens under the patch."""
        from lucnhan.ui.theme.manager import ThemeManager
        from lucnhan.ui.theme.tokens import ThemeMode

        ThemeManager.instance().set_mode(ThemeMode(mode))
        ThemeManager.instance().refresh_from_host()
        _settle()

    def test_host_mode_matches_an_independent_host_palette(self) -> None:
        self._as_ida()
        app = _app()
        before = app.palette().color(QPalette.ColorRole.Window).name()
        before_style = app.styleSheet()
        host_window = self._host_window()

        self._set_host_theme("ida")

        self.assertEqual(
            app.palette().color(QPalette.ColorRole.Window).name(),
            before,
            "AUTO/IDA must not recolor QApplication",
        )
        self.assertEqual(app.styleSheet(), before_style)
        from lucnhan.ui.theme.manager import ThemeManager

        self.assertEqual(
            ThemeManager.instance().tokens().window.lower(),
            host_window,
            "AUTO/IDA tokens must come from the host, not the app default",
        )
        self.assertEqual(
            _resolved(self._panel, QPalette.ColorRole.Window).lower(),
            host_window,
        )

    def test_composer_divider_survives_host_theme_switches(self) -> None:
        from lucnhan.ui.theme.manager import ThemeManager

        self._as_ida()
        for mode in ("auto", "dark", "light", "ida", "auto"):
            with self.subTest(mode=mode):
                _set_theme(self._panel, mode)
                tokens = ThemeManager.instance().tokens()
                composer = self._panel._input_container
                border = _border_pixel(composer).lower()
                self.assertEqual(border, tokens.mid.lower())
                self.assertNotEqual(
                    border,
                    _pixel(composer, (composer.width() // 2, 4)).lower(),
                    "the composer must have a visible divider above the input",
                )

    def test_footer_selector_stays_readable_across_host_theme_switches(self) -> None:
        from lucnhan.ui.theme.manager import ThemeManager
        from lucnhan.ui.theme.tokens import hex_luminance

        self._as_ida()
        self._panel.activateWindow()
        _settle(50)
        for mode in ("auto", "dark", "light", "ida", "auto"):
            _set_theme(self._panel, mode)
            tokens = ThemeManager.instance().tokens()
            trigger = self._panel._footer_trigger
            for focused in (False, True):
                with self.subTest(mode=mode, focused=focused):
                    (trigger if focused else self._panel._input_area).setFocus()
                    _settle(50)
                    self.assertEqual(trigger.hasFocus(), focused)
                    origin = trigger.mapTo(self._panel._footer, trigger.rect().topLeft())
                    # Grab the parent: a transparent button's own grab has no
                    # composited background and reports black instead of its fill.
                    background = _pixel(
                        self._panel._footer,
                        (origin.x() + 4, origin.y() + trigger.height() // 2),
                    ).lower()
                    foreground = _resolved(trigger, QPalette.ColorRole.ButtonText)
                    first, second = sorted((hex_luminance(background), hex_luminance(foreground)))
                    ratio = (second + 0.05) / (first + 0.05)
                    self.assertGreaterEqual(
                        ratio,
                        4.5,
                        f"footer {foreground} on {background} = {ratio:.2f}:1",
                    )
                    self.assertEqual(
                        background,
                        (tokens.alt_base if focused else tokens.window).lower(),
                        "the footer selector must stay flat on the active theme surface",
                    )

    def test_host_code_surface_contrasts_with_its_text(self) -> None:
        """The host code inset must meet AA against the host's own text.

        A native host resolves Base ``#282a36`` and AlternateBase
        ``#f5f5f5``; deriving the code background from AlternateBase
        painted ``#f8f8f2`` text on a near-white inset (white on white).
        The check is the WCAG 2.x contrast ratio of the *actual derived
        pair*, not just that ``code_bg`` equals ``base`` — the equality
        alone would pass with a light Base too.  Nothing here mutates
        ``QApplication``.
        """
        from lucnhan.ui.qt_compat import QWidget
        from lucnhan.ui.theme.palette_ida import derive_ida_tokens

        def _luminance(hex_color: str) -> float:
            h = hex_color.lstrip("#")
            channels = [int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]
            linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        host = QWidget()
        try:
            host.ensurePolished()
            pal = host.palette()
            self.assertNotEqual(
                pal.color(QPalette.ColorRole.Base).name(),
                pal.color(QPalette.ColorRole.AlternateBase).name(),
                "fixture host must separate Base from AlternateBase",
            )
            tokens = derive_ida_tokens(host)

            first, second = sorted((_luminance(tokens.code_text), _luminance(tokens.code_bg)), reverse=True)
            ratio = (first + 0.05) / (second + 0.05)
            self.assertGreaterEqual(
                ratio,
                4.5,
                f"code {tokens.code_text} on {tokens.code_bg} = {ratio:.2f}:1 (must be >= 4.5:1)",
            )
        finally:
            host.deleteLater()
            _app().processEvents()

    def test_transition_to_host_reverts_to_host_paint(self) -> None:
        """Dark -> Light -> host: the painted surfaces end up on the host.

        Checked on the paint of the actual surfaces (panel, content area
        and the transcript viewport) rather than on the outer root's
        QPalette, which is too broad a claim: it can read a host value
        while what is drawn follows the theme.
        """
        from lucnhan.ui.styles import is_host_theme

        self._as_ida()
        panel = self._panel
        _set_theme(panel, "light")
        view = panel._chat_views[panel._ctrl.active_tab_id]
        for surface in (panel, panel._content_area, view.viewport()):
            with self.subTest(mode="light", surface=surface.objectName()):
                self.assertEqual(_painted(surface).lower(), "#ffffff")

        self._set_host_theme("ida")
        self.assertTrue(is_host_theme())
        host_window = self._host_window()
        for surface in (panel, panel._content_area, view.viewport()):
            with self.subTest(mode="ida", surface=surface.objectName()):
                self.assertEqual(
                    _painted(surface).lower(),
                    host_window,
                    f"{surface.objectName()} must paint the host surface",
                )

    def test_no_reserved_tab_bar_band_above_the_transcript(self) -> None:
        """The gap between the context row and the transcript is not host-colored.

        Samples the actual strip of pixels the panel paints between the
        bottom of the context row and the top of the transcript.  A band
        the tab widget reserves for a tab bar it does not own is exactly
        where a host-colored gap shows up; this asserts on paint, not on
        any assumption about what reserves it.
        """
        from lucnhan.ui.theme.palette_light import LIGHT_TOKENS

        panel = self._panel
        _set_theme(panel, "light")
        strip = panel._session_strip
        page = panel._tab_widget
        view = panel._chat_views[panel._ctrl.active_tab_id]
        # The transcript starts where the page content starts; anything the
        # page reserves above that is the band under investigation.
        page_origin = page.mapTo(panel, page.rect().topLeft())
        top = page_origin.y()
        bottom = view.mapTo(panel, view.rect().topLeft()).y()
        image = panel.grab().toImage()
        ratio = panel.devicePixelRatioF() or 1.0
        samples = {}
        for dy in (0, 1, 2, 3, 10):
            for fx in (0.02, 0.2, 0.5, 0.8, 0.98):
                sx = min(int(image.width() * fx), image.width() - 1)
                sy = min(int((top + dy) * ratio), image.height() - 1)
                child = panel.childAt(int(image.width() * fx), top + dy)
                samples[(dy, fx)] = (
                    image.pixelColor(sx, sy).name(),
                    type(child).__name__,
                    child.objectName() if child is not None else "?",
                )
        px = image.pixelColor(min(int(40 * ratio), image.width() - 1), min(int(top * ratio), image.height() - 1))
        self.assertEqual(
            px.name().lower(),
            LIGHT_TOKENS.window.lower(),
            f"pageTop={top} pageGeom={page.geometry()} viewTop={bottom} "
            f"stripGeom={strip.geometry()} pixel={px.name()} "
            f"samples={ {k: v for k, v in list(samples.items())[:6]} }",
        )

        image = panel.grab().toImage()
        ratio = panel.devicePixelRatioF() or 1.0
        expected = LIGHT_TOKENS.window.lower()
        occupants: dict[str, int] = {}
        offenders: list[str] = []
        for y in range(top, bottom):
            for x in range(int(20 * ratio), int(image.width() - 20 * ratio), 8):
                px = min(x, image.width() - 1)
                py = min(int(y * ratio), image.height() - 1)
                owner = panel.childAt(int(x), y)
                name = type(owner).__name__
                occupants[name] = occupants.get(name, 0) + 1
                if image.pixelColor(px, py).name().lower() != expected:
                    offenders.append(
                        f"y={y} x={int(x)} owner={name} "
                        f"geom={owner.geometry()} pageGeom={page.geometry()} stripGeom={strip.geometry()}"
                    )
        self.assertEqual(
            offenders[:5],
            [],
            f"host-colored pixels above the transcript; band={bottom - top}px occupants={occupants}",
        )

    def test_reopened_panel_still_follows_the_host(self) -> None:
        """A closed panel must not be what host mode keeps deriving from.

        The manager owns the AUTO/IDA probe, so retiring a panel cannot
        leave it pinned to a destroyed widget.  ``isValid`` is checked
        after the deferred delete has actually been processed — before
        that the C++ object legitimately still exists, which is why the
        earlier assertion saw a live panel.
        """
        import shiboken6

        from lucnhan.ui.panel_core import LucNhanPanelCore
        from lucnhan.ui.qt_compat import QEventLoop
        from lucnhan.ui.theme.manager import ThemeManager
        from lucnhan.ui.theme.tokens import ThemeMode

        self._as_ida()
        self._set_host_theme("ida")
        first = self._panel
        self._panel = None
        LucNhanPanelCore.shutdown(first)
        first.close()
        first.deleteLater()
        # Drain the deferred deletion: a plain sendPostedEvents() does NOT flush
        # DeferredDelete, which is why the earlier check saw a live panel.
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        loop = QEventLoop()
        QTimer.singleShot(0, loop.quit)
        loop.exec()
        _app().processEvents()
        self.assertFalse(shiboken6.isValid(first), "the old panel must really be gone")

        ThemeManager.instance().set_mode(ThemeMode.LIGHT)
        _settle()
        reopened = LucNhanPanelCore(controller_factory=lambda _cfg: self._controller)
        self.addCleanup(self._retire, reopened)
        reopened.resize(_WIDE_PX, _HEIGHT_PX)
        reopened.show()
        reopened.set_theme("ida")
        ThemeManager.instance().refresh_from_host()
        _settle()
        self.assertEqual(
            _resolved(reopened, QPalette.ColorRole.Window).lower(),
            self._host_window(),
        )


@requires_real_qt
class TestSessionStripAndStatus(_WorkspacePanelTest):
    """Session tabs and the header status must survive real layout."""

    def setUp(self) -> None:
        super().setUp()
        # Two extra sessions, created through the panel's own path, so the
        # strip holds real tab content.
        self._panel._create_tab("tab-1", "Header analysis")
        self._panel._create_tab("tab-2", "Image loading")
        _settle()

    def test_bar_lives_in_the_strip_and_paints_its_tabs(self) -> None:
        bar = self._panel._tab_bar
        strip = self._panel._session_strip
        self.assertIs(bar.parent(), strip)
        # The bar is the one that owns the tab chrome: it has to be tall
        # enough for its label and its 2 px active underline, not Qt's
        # 16 px unstyled default.
        self.assertGreaterEqual(bar.height(), 24)

    def test_every_tab_is_reachable_at_wide_and_narrow(self) -> None:
        panel, bar = self._panel, self._panel._tab_bar

        panel.resize(_WIDE_PX, _HEIGHT_PX)
        _settle()
        self.assertEqual(bar.count(), 3)
        for index in range(bar.count()):
            with self.subTest(width=_WIDE_PX, tab=index):
                rect = bar.tabRect(index)
                self.assertTrue(rect.isValid())
                self.assertGreater(rect.height(), 0)

        panel.resize(_NARROW_PX, _HEIGHT_PX)
        _settle()
        # G3: narrow must not hide inactive tabs — every one is still
        # there and carries a label, reachable through the bar's native
        # scrolling.
        self.assertEqual(bar.count(), 3)
        for index in range(bar.count()):
            with self.subTest(width=_NARROW_PX, tab=index):
                self.assertFalse(bar.tabText(index) == "")
        self.assertTrue(panel._tab_bar._add_btn.isVisible())

    def test_tab_close_button_closes_only_that_tab(self) -> None:
        """G3: the native close affordance removes one tab, not the strip.

        Driven by clicking the ``QTabBar`` close button itself — the
        control the user actually presses — so the widget's
        ``tabCloseRequested`` wiring is what is under test.  The platform
        places that button on either side, so both are probed.
        """
        panel = self._panel
        bar = panel._tab_bar
        before = bar.count()
        labels_before = [bar.tabText(i) for i in range(before)]
        close_btn = None
        for position in (QTabBar.ButtonPosition.RightSide, QTabBar.ButtonPosition.LeftSide):
            candidate = bar.tabButton(1, position)
            if candidate is not None:
                close_btn = candidate
                break
        # The affordance itself is the contract: a closable tab must
        # actually carry a button on this platform.
        self.assertIsNotNone(
            close_btn,
            "closable tabs must expose a close button (neither side returned one)",
        )
        close_btn.click()
        _settle(50)
        self.assertEqual(bar.count(), before - 1)
        self.assertEqual(bar.tabText(0), labels_before[0], "sibling tab must survive")
        self.assertEqual(bar.tabText(1), labels_before[2], "the wrong tab was closed")

    def test_strip_highlight_tracks_the_selected_tab(self) -> None:
        """The strip's highlight and the shown page must never disagree.

        Programmatic selections (opening a tab, focusing an older one,
        rebuilding a restored session, closing the current tab) and a user
        click must all leave the bar on the page that is actually shown.
        """
        panel = self._panel
        bar = panel._tab_bar
        pages = panel._tab_widget

        def in_step(where: str) -> None:
            self.assertEqual(
                bar.currentIndex(),
                pages.currentIndex(),
                f"{where}: strip highlight and selected page disagree",
            )

        in_step("initial")
        panel._create_tab("tab-new", "Newest")
        # _create_tab selects explicitly, so the newly opened tab is both
        # the shown page and the highlighted one.
        self.assertEqual(pages.currentIndex(), pages.count() - 1)
        in_step("after creating a tab")

        panel._focus_tab("tab-0")
        _settle(50)
        self.assertEqual(pages.currentIndex(), 0)
        in_step("after focusing an older tab")

        bar.setCurrentIndex(pages.count() - 1)
        _settle(50)
        in_step("after clicking a tab")

        pages.removeTab(pages.currentIndex())
        _settle(50)
        in_step("after closing the current tab")

    def test_new_tab_button_creates_a_session(self) -> None:
        panel = self._panel
        before = panel._tab_bar.count()
        panel._tab_bar._add_btn.click()
        _settle(50)
        self.assertEqual(panel._tab_bar.count(), before + 1)

    def test_strip_grows_for_a_larger_font(self) -> None:
        """G2: a larger user font must grow the row, not clip the tabs."""
        strip = self._panel._session_strip
        baseline = strip.sizeHint().height()
        font = strip.font()
        font.setPointSize(font.pointSize() + 6)
        strip.setFont(font)
        self._panel.resize(_WIDE_PX, _HEIGHT_PX)
        _settle()
        self.assertGreaterEqual(strip.sizeHint().height(), baseline)
        self.assertGreaterEqual(self._panel._tab_bar.height(), 24)

    def test_status_is_legible_at_both_widths(self) -> None:
        status = self._panel._status_label
        self._panel.resize(_WIDE_PX, _HEIGHT_PX)
        _settle()
        wide_width = status.width()
        self.assertGreater(wide_width, 0, "status must not collapse to 0 px when wide")
        self.assertTrue(status.text())

        self._panel.resize(_NARROW_PX, _HEIGHT_PX)
        _settle()
        self.assertGreater(status.width(), 0, "status must stay reachable when narrow")
        self.assertTrue(status.toolTip())


@requires_real_qt
class TestActionEmphasis(_WorkspacePanelTest):
    """Nav, Send and the composer must read as the contract's hierarchy."""

    def setUp(self) -> None:
        super().setUp()
        _set_theme(self._panel, "dark")

    def _sample(self, widget, points=((3, 3), (6, 3))):
        return {_pixel(widget, p).lower() for p in points}

    def test_active_nav_is_visually_distinct(self) -> None:
        """The checked nav item must paint differently from a neutral one."""
        self._panel._chat_btn.setChecked(True)
        self._panel._tools_btn.setChecked(False)
        _settle(50)
        active = self._sample(self._panel._chat_btn)
        neutral = self._sample(self._panel._tools_btn)
        self.assertTrue(active)
        self.assertTrue(neutral)
        self.assertNotEqual(
            active,
            neutral,
            "the active nav item must not paint like a neutral one",
        )

    def test_send_paints_the_accent_fill(self) -> None:
        """G1: the primary action is a filled accent button.

        Sampled inside the button but *off* the label: the centre of a
        short button is glyph, and antialiased glyph edges blend the fill
        with the label color (a centre sample returned #514521 instead of
        the accent).  The right inset quarter is fill.
        """
        from lucnhan.ui.theme.palette_dark import DARK_TOKENS

        send = self._panel._send_btn
        painted = _pixel(send, (send.width() * 3 // 4, send.height() // 2))
        self.assertEqual(painted.lower(), DARK_TOKENS.highlight.lower())

    def test_send_label_contrasts_with_its_fill(self) -> None:
        """``highlight_text`` on the accent fill, not ``button_text``."""
        from lucnhan.ui.theme.palette_dark import DARK_TOKENS

        send = self._panel._send_btn
        self.assertNotEqual(DARK_TOKENS.highlight_text, DARK_TOKENS.button_text)
        # The label color reaches the widget through the resolved
        # ButtonText role once the primary QSS is applied.
        text = send.palette().color(QPalette.ColorRole.ButtonText).name().lower()
        self.assertEqual(text, DARK_TOKENS.highlight_text.lower())

    def test_stop_keeps_its_danger_appearance(self) -> None:
        from lucnhan.ui.theme.palette_dark import DARK_TOKENS

        stop = self._panel._cancel_btn
        stop.setVisible(True)
        _settle(50)
        self.assertNotEqual(
            _pixel(stop, (stop.width() // 2, 4)).lower(),
            DARK_TOKENS.highlight.lower(),
            "Stop must not paint as the primary fill",
        )

    def test_composer_frame_wraps_editor_and_action_row(self) -> None:
        """G3: one frame around the editor *and* the commands/send row.

        Checked on pixels: the editor's own background is the inset, the
        wrapper's surface surrounds it, and the wrapper's border is a
        single stroke around both.
        """
        from lucnhan.ui.theme.palette_dark import DARK_TOKENS

        composer = self._panel._input_container
        self.assertEqual(_border_pixel(composer).lower(), DARK_TOKENS.mid.lower())

        editor = self._panel._input_area
        inside = _pixel(editor, (editor.width() - 2, editor.height() - 2)).lower()
        self.assertEqual(inside, DARK_TOKENS.base.lower(), "editor inset surface")
        # The editor contributes no second frame of its own.
        self.assertEqual(
            _border_pixel(editor).lower(),
            DARK_TOKENS.base.lower(),
            "editor must not draw its own border inside the composer frame",
        )
        # ...and the action row sits inside the same frame: the wrapper's
        # surface shows to the right of the Send button, so the frame wraps
        # the action row rather than stopping at the editor.
        send = self._panel._send_btn
        self.assertGreater(send.width(), 0)
        gutter = send.mapTo(composer, send.rect().topRight()).x() + 1
        self.assertLess(gutter, composer.width() - 1)
        self.assertEqual(
            _pixel(composer, (gutter, composer.height() - 2)).lower(),
            DARK_TOKENS.base.lower(),
            "composer surface must continue under the action row",
        )


@requires_real_qt
class TestSwitchIsPresentationOnly(_WorkspacePanelTest):
    """A theme switch must not touch the user's work."""

    def test_draft_and_selection_survive_every_switch(self) -> None:
        editor = self._panel._input_area
        draft = "draft: inspect the next function."
        editor.setPlainText(draft)
        cursor = editor.textCursor()
        cursor.setPosition(6)
        cursor.setPosition(14, cursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        selected = editor.textCursor().selectedText()
        self.assertTrue(selected)
        for mode in ("light", "dark", "ida", "light"):
            with self.subTest(mode=mode):
                _set_theme(self._panel, mode)
                self.assertEqual(editor.toPlainText(), draft)
                self.assertEqual(editor.textCursor().selectedText(), selected)

    def test_selected_tab_and_running_state_survive_switches(self) -> None:
        panel = self._panel
        panel._create_tab("tab-1", "Header analysis")
        panel._tab_widget.setCurrentIndex(1)
        _set_theme(panel, "light")
        self.assertEqual(panel._tab_widget.currentIndex(), 1)
        panel._set_running(True)
        _set_theme(panel, "ida")
        self.assertEqual(panel._tab_widget.currentIndex(), 1)
        self.assertTrue(panel._cancel_btn.isVisible())
        self.assertEqual(panel._status_label.text(), "Running")
        panel._set_running(False)
        self.assertEqual(panel._status_label.text(), "Ready")


@requires_real_qt
class TestFontOverride(_WorkspacePanelTest):
    """G2: a user font override must grow the text and the rows, and it
    has to survive a theme switch."""

    def test_font_family_reaches_the_panel_widgets(self) -> None:
        """A configured family must land on the panel, and clear cleanly.

        Driven through the real wrapper (``LucNhanPanel._apply_font_override``),
        which is where the family is applied — a bare panel core never sees
        it.  Checks resolved families on real widgets, so a rule that only
        *reads* right cannot pass.
        """
        from tests.mocks.ida_mock import install_ida_mocks

        install_ida_mocks()

        from lucnhan.ida.ui.panel import LucNhanPanel

        form = LucNhanPanel()
        root = QWidget()
        # The form's family rule lives on ``_root`` and cascades to
        # descendants, so the panel has to actually be inside it — the
        # real PluginForm builds exactly this tree.
        self._panel.setParent(root)
        form._root = root
        form._core = self._panel
        self.addCleanup(self._retire, root)
        self._config.font_family = "Consolas"
        try:
            form._apply_font_override()
            _settle()
            # Measured natively: the family lands on the form root and on
            # the panel core, which is where the override is applied; a
            # child widget that carries its own stylesheet keeps the font
            # Qt resolves for it, and the panel/application set no
            # ``font-family`` rule anywhere.
            # The family must reach the TEXT, i.e. widgets that carry
            # their own stylesheet (measured: a styled child kept the host
            # family while root/core took the override).
            self.assertEqual(self._panel._status_label.font().family(), "Consolas")
            self.assertEqual(self._panel._context_bar._context_title.font().family(), "Consolas")
            self.assertIn("Consolas", root.styleSheet())
        finally:
            self._config.font_family = ""
            form._apply_font_override()
            _settle()
        # Clearing restores the inherited default rather than freezing it.
        self.assertEqual(root.styleSheet(), "")
        self.assertNotEqual(root.font().family(), "Consolas")
        self.assertNotEqual(self._panel.font().family(), "Consolas")

    def test_injected_font_family_is_not_applied(self) -> None:
        """A configured name that is not installed never reaches the UI.

        The family is validated against the host's installed families, so
        a value carrying QSS (``Consolas'; font-size: 100pt``) matches
        nothing and leaves no rule behind.
        """
        from lucnhan.ida.ui.panel import LucNhanPanel
        from tests.mocks.ida_mock import install_ida_mocks

        install_ida_mocks()
        form = LucNhanPanel()
        root = QWidget()
        # The form's family rule lives on ``_root`` and cascades to
        # descendants, so the panel has to actually be inside it — the
        # real PluginForm builds exactly this tree.
        self._panel.setParent(root)
        form._root = root
        form._core = self._panel
        self.addCleanup(self._retire, root)

        injection = "Consolas'; font-size: 100pt; font-family: 'Consolas"
        self._config.font_family = injection
        try:
            form._apply_font_override()
            _settle()
            self.assertEqual(root.styleSheet(), "")
            self.assertNotIn("font-size: 100pt", self._panel.styleSheet())
            self.assertNotIn(
                "100pt",
                self._panel._status_label.font().family() + self._panel._status_label.styleSheet(),
                "the injected value must not reach the UI",
            )
        finally:
            self._config.font_family = ""
            form._apply_font_override()
            _settle()

    def test_configured_override_grows_roles_and_regions(self) -> None:
        panel = self._panel
        _set_theme(panel, "dark")

        # Measure the widget the contract's 11 px role is applied to — the
        # CONTEXT label itself, not the bar frame that holds it.
        label = panel._context_bar._context_title

        def apply_and_measure():
            # The manager only learns the configured size when the real
            # apply path runs, so drive that rather than assuming an
            # attribute write is picked up.
            panel._apply_font_override()
            _settle()
            return (
                label.fontMetrics().height(),
                panel._input_area.fontMetrics().height(),
                panel._session_strip.sizeHint().height(),
                panel._context_bar.sizeHint().height(),
            )

        default_label, default_body, default_strip, default_bar = apply_and_measure()

        self._config.font_size_override = 16
        grown_label, grown_body, grown_strip, grown_bar = apply_and_measure()

        from lucnhan.ui.theme.manager import ThemeManager

        self.assertEqual(ThemeManager.instance().font_size_override, 16)
        self.assertGreater(grown_label, default_label, "context label text must grow")
        self.assertGreater(grown_body, default_body, "body text must grow")
        self.assertGreater(grown_strip, default_strip, "session strip must grow")
        self.assertGreater(grown_bar, default_bar, "context row must grow")

        # The override survives a theme switch (roles rebuild from the
        # manager on every change).
        _set_theme(panel, "light")
        self.assertEqual(ThemeManager.instance().font_size_override, 16)
        self.assertGreater(label.fontMetrics().height(), default_label)
        self.assertGreater(panel._input_area.fontMetrics().height(), default_body)
        self._config.font_size_override = 0


@requires_real_qt
class TestApprovalGate(_WorkspacePanelTest):
    """G3/G4: a script approval locks the composer until it is decided.

    Driven through the real event path — a ``TOOL_APPROVAL_REQUEST``
    ``TurnEvent`` and then the panel's decision slot — because the defect
    was in the event dispatch, not in any terminal flag.
    """

    def _request_script_approval(self, tool_call_id: str = "gate-1") -> None:
        """Drive the real execute_python approval route into the transcript.

        A script approval only reaches the unified gate when the widget
        already exists: ``TOOL_CALL_START`` creates the
        ``ExecutePythonWidget`` for the tool, ``TOOL_CALL_DONE`` hands it
        the code, and the ``TOOL_APPROVAL_REQUEST`` with the same id then
        shows its buttons.  A bare approval event renders a different
        widget entirely, so the whole sequence goes through
        ``panel._on_event``.
        """
        from lucnhan import constants
        from lucnhan.agent.turn import TurnEvent, TurnEventType

        panel = self._panel
        tab_id = panel._ctrl.active_tab_id
        panel._on_event(
            TurnEvent.tool_call_start(
                tool_call_id,
                constants.EXECUTE_PYTHON_TOOL_NAME,
            ),
            tab_id,
        )
        panel._on_event(
            TurnEvent.tool_call_done(
                tool_call_id,
                constants.EXECUTE_PYTHON_TOOL_NAME,
                json.dumps({"code": "def parse_header(data):\n    return len(data) >= 32"}),
            ),
            tab_id,
        )
        panel._on_event(
            TurnEvent(
                type=TurnEventType.TOOL_APPROVAL_REQUEST,
                tool_call_id=tool_call_id,
                tool_name=constants.EXECUTE_PYTHON_TOOL_NAME,
            ),
            tab_id,
        )
        _settle(50)

    def test_tool_approval_locks_the_composer_and_keeps_stop(self) -> None:
        panel = self._panel
        _set_theme(panel, "dark")
        panel._set_running(True)
        self._request_script_approval()

        self.assertTrue(panel._input_area.isReadOnly(), "composer must be locked")
        self.assertFalse(panel._send_btn.isEnabled(), "Send must be disabled")
        self.assertTrue(panel._cancel_btn.isVisible(), "Stop must stay reachable")
        self.assertEqual(panel._status_label.text(), "Waiting for approval")

    def _decide(self, decision: str) -> None:
        """Click the decision button on the gate the transcript rendered.

        Finds the gate by its real class (``ExecutePythonWidget`` for a
        script approval) and clicks that widget's own ``_allow_btn`` /
        ``_deny_btn``, so the approval path is exercised end to end:
        event -> rendered gate -> button click.  No production API is
        renamed or added to make this selectable.
        """
        from lucnhan.ui.tool_widgets import ExecutePythonWidget

        panel = self._panel
        view = panel._chat_views[panel._ctrl.active_tab_id]
        gates = view.findChildren(ExecutePythonWidget)
        self.assertTrue(gates, "the transcript must render the script approval gate")
        button = gates[0]._deny_btn if decision == "deny" else gates[0]._allow_btn
        self.assertTrue(button.isEnabled(), f"{decision} must be offered")
        button.click()
        _settle(50)

    def test_allow_once_releases_the_composer(self) -> None:
        panel = self._panel
        _set_theme(panel, "dark")
        panel._set_running(True)
        self._request_script_approval()

        self._decide("allow_once")

        self.assertFalse(panel._input_area.isReadOnly(), "Allow once must release input")
        self.assertTrue(panel._send_btn.isEnabled(), "Send must come back")

    def test_deny_releases_the_composer(self) -> None:
        panel = self._panel
        _set_theme(panel, "dark")
        panel._set_running(True)
        self._request_script_approval()

        self._decide("deny")

        self.assertFalse(panel._input_area.isReadOnly(), "Deny must release input")

    def test_stop_cancels_the_approval_and_unlocks(self) -> None:
        panel = self._panel
        _set_theme(panel, "dark")
        panel._set_running(True)
        self._request_script_approval()

        panel._cancel_btn.click()
        _settle(50)

        self.assertFalse(panel._input_area.isReadOnly(), "Stop must unlock the composer")
        self.assertFalse(panel._cancel_btn.isVisible(), "Stop hides once the gate clears")
        self.assertEqual(panel._status_label.text(), "Ready")


@requires_real_qt
class TestConcurrentGatesArePerTab(_WorkspacePanelTest):
    """A gate belongs to the tab that asked — two tabs wait independently.

    Before the repair the panel kept ONE panel-wide approval flag plus the
    id of the tab that set it.  Sending from a second tab therefore looked
    like the answer to the first tab's gate, and switching back showed an
    unlocked composer with Stop hidden and a "Ready" status while the agent
    was still blocked.
    """

    def _new_tab(self, panel) -> str:
        """Create and focus a second chat tab through the real path."""
        panel._tab_bar._add_btn.click()
        _settle(50)
        return panel._ctrl.active_tab_id

    def _request_script_approval(self, tab_id: str, tool_call_id: str) -> None:
        from lucnhan import constants
        from lucnhan.agent.turn import TurnEvent

        # A script approval only reaches the unified gate when the widget
        # already exists, so drive the real tool sequence first.
        for event in (
            TurnEvent.tool_call_start(tool_call_id, constants.EXECUTE_PYTHON_TOOL_NAME),
            TurnEvent.tool_call_done(
                tool_call_id,
                constants.EXECUTE_PYTHON_TOOL_NAME,
                json.dumps({"code": "print(1)"}),
            ),
            TurnEvent.tool_approval_request(
                tool_call_id,
                constants.EXECUTE_PYTHON_TOOL_NAME,
                '{"code":"print(1)"}',
                "Synthetic gate; the script is never executed",
            ),
        ):
            self._panel._on_event(event, tab_id)
        _settle(50)

    def test_idle_second_tab_sends_while_first_waits(self) -> None:
        panel = self._panel
        tab_a = panel._ctrl.active_tab_id
        self._request_script_approval(tab_a, "gate-A")
        tab_b = self._new_tab(panel)

        self.assertNotEqual(tab_b, tab_a)
        self.assertFalse(
            panel._input_area.isReadOnly(),
            "a background gate must not lock another tab's composer",
        )
        self.assertTrue(panel._send_btn.isEnabled(), "an idle tab must be able to send")
        self.assertEqual(panel._status_label.text(), "Ready")

        # The message leaves tab B: it is neither answered into tab A's
        # gate nor queued behind it.
        sent: list[tuple[str, str]] = []
        panel._input_area.setPlainText("Question for tab B")
        with patch.object(type(panel), "_start_agent_for_tab", lambda self, tid, text: sent.append((tid, text))):
            panel._send_btn.click()
        self.assertEqual(sent, [(tab_b, "Question for tab B")])

    def test_switching_back_shows_the_pending_gate_and_stop(self) -> None:
        panel = self._panel
        tab_a = panel._ctrl.active_tab_id
        self._request_script_approval(tab_a, "gate-A")
        tab_b = self._new_tab(panel)
        panel._input_area.setPlainText("Typed into tab B")
        self._request_script_approval(tab_b, "gate-B")

        # Both tabs wait; tab B is on screen, so tab B's gate is shown.
        self.assertTrue(panel._input_area.isReadOnly())
        panel._tab_widget.setCurrentIndex(0)
        _settle(50)

        self.assertEqual(panel._ctrl.active_tab_id, tab_a)
        self.assertTrue(panel._input_area.isReadOnly(), "switching back must show tab A's gate")
        self.assertFalse(panel._send_btn.isEnabled(), "Send must be disabled for tab A")
        self.assertTrue(panel._cancel_btn.isVisible(), "Stop must stay reachable for tab A")
        self.assertEqual(panel._status_label.text(), "Waiting for approval")

    def test_stop_on_one_tab_leaves_the_other_gate_locked(self) -> None:
        panel = self._panel
        tab_a = panel._ctrl.active_tab_id
        self._request_script_approval(tab_a, "gate-A")
        tab_b = self._new_tab(panel)
        self._request_script_approval(tab_b, "gate-B")

        panel._cancel_btn.click()
        _settle(50)

        self.assertFalse(panel._input_area.isReadOnly(), "Stop must unlock the tab it targets")
        panel._tab_widget.setCurrentIndex(0)
        _settle(50)
        self.assertEqual(panel._ctrl.active_tab_id, tab_a)
        self.assertTrue(panel._input_area.isReadOnly(), "Stop on tab B must not clear tab A's gate")
        self.assertTrue(panel._cancel_btn.isVisible(), "tab A must keep Stop")
        self.assertIn("approval", panel._status_label.text().lower(), panel._status_label.text())

    def test_deciding_one_gate_leaves_the_other_locked(self) -> None:
        panel = self._panel
        tab_a = panel._ctrl.active_tab_id
        self._request_script_approval(tab_a, "gate-A")
        tab_b = self._new_tab(panel)
        self._request_script_approval(tab_b, "gate-B")

        # Answer tab B through its own gate widget.
        from lucnhan.ui.tool_widgets import ExecutePythonWidget

        gates = panel._chat_views[tab_b].findChildren(ExecutePythonWidget)
        self.assertTrue(gates, "tab B's transcript must render its gate")
        gates[-1]._allow_btn.click()
        _settle(50)
        self.assertFalse(panel._input_area.isReadOnly(), "deciding tab B releases tab B")

        panel._tab_widget.setCurrentIndex(0)
        _settle(50)
        self.assertTrue(panel._input_area.isReadOnly(), "tab A must still be waiting")

    def test_refused_send_and_enter_keep_the_draft(self) -> None:
        panel = self._panel
        tab_b = self._new_tab(panel)
        self._request_script_approval(tab_b, "gate-B")
        draft = "Draft I refuse to lose"
        panel._input_area.setPlainText(draft)
        self.assertTrue(panel._input_area.isReadOnly(), "the gate must be up before sending")

        # The Send button path.
        panel._send_btn.click()
        self.assertEqual(panel._input_area.toPlainText(), draft, "a refused Send must keep the draft")

        # The Enter path, which clears the editor only on acceptance.
        _press_key(panel._input_area, Qt.Key.Key_Return)
        self.assertEqual(panel._input_area.toPlainText(), draft, "a refused Enter must keep the draft")


@requires_real_qt
class TestPerTabDrafts(_WorkspacePanelTest):
    """Each tab owns its composer text, caret and selection.

    The composer is a single widget shared by every tab, so without a
    per-tab store a draft typed in one tab is overwritten (or inherited)
    by the next.
    """

    def _new_tab(self, panel) -> str:
        panel._tab_bar._add_btn.click()
        _settle(50)
        return panel._ctrl.active_tab_id

    @staticmethod
    def _select(editor, anchor: int, position: int) -> None:
        cursor = editor.textCursor()
        cursor.setPosition(anchor)
        cursor.setPosition(position, cursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)

    def test_new_tab_starts_empty_and_returns_the_first_draft(self) -> None:
        panel = self._panel
        editor = panel._input_area
        tab_a = panel._ctrl.active_tab_id
        editor.setPlainText("Draft A only")
        self._select(editor, 2, 7)

        tab_b = self._new_tab(panel)
        self.assertNotEqual(tab_b, tab_a)
        self.assertEqual(editor.toPlainText(), "", "a new tab must not inherit A's draft")

        editor.setPlainText("Draft B only")
        panel._tab_widget.setCurrentIndex(0)
        _settle(50)
        self.assertEqual(editor.toPlainText(), "Draft A only", "A must get its own draft back")
        cursor = editor.textCursor()
        self.assertEqual((cursor.anchor(), cursor.position()), (2, 7), "A must get its selection back")

        panel._tab_widget.setCurrentIndex(1)
        _settle(50)
        self.assertEqual(editor.toPlainText(), "Draft B only", "B must get its own draft back")

    def test_survives_repeated_switches_without_drift(self) -> None:
        panel = self._panel
        editor = panel._input_area
        editor.setPlainText("stable draft")
        self._select(editor, 0, 6)
        self._new_tab(panel)

        for _ in range(3):
            panel._tab_widget.setCurrentIndex(0)
            _settle(50)
            self.assertEqual(editor.toPlainText(), "stable draft")
            self.assertEqual(editor.textCursor().selectedText(), "stable")
            panel._tab_widget.setCurrentIndex(1)
            _settle(50)

    def test_closing_a_tab_releases_only_that_draft(self) -> None:
        panel = self._panel
        editor = panel._input_area
        panel._input_area.setPlainText("kept draft")
        self._new_tab(panel)
        editor.setPlainText("doomed draft")

        # Close tab B (index 1) and confirm tab A's draft is intact.
        panel._on_close_tab(1)
        _settle(50)
        self.assertEqual(panel._ctrl.active_tab_id, "tab-0")
        self.assertEqual(editor.toPlainText(), "kept draft", "closing B must not touch A's draft")

    def test_database_switch_never_resurrects_the_previous_binary_draft(self) -> None:
        panel = self._panel
        panel._input_area.setPlainText("Only meaningful for the previous binary")
        panel.on_database_changed("synthetic-next-binary.i64")
        self.assertEqual(panel._input_area.toPlainText(), "")
        self._new_tab(panel)
        panel._tab_widget.setCurrentIndex(0)
        _settle(50)
        self.assertEqual(panel._input_area.toPlainText(), "")


@requires_real_qt
class TestComposerFitsLargeFont(_WorkspacePanelTest):
    """The composer and its buttons must fit the text at any font size.

    ``setFixedWidth(64)`` clipped ``Send`` / ``Stop`` / ``Queue`` and the
    fixed 38 px editor floor cut the first typed line at a 24 pt override.
    Both minima are now metric-derived; the default font is unchanged.
    """

    def _fit_report(self) -> dict:
        panel = self._panel
        editor = panel._input_area
        return {
            "editor_height": editor.height(),
            "font_height": editor.fontMetrics().height(),
            "buttons": {
                btn.text(): (btn.width(), btn.fontMetrics().horizontalAdvance(btn.text()))
                for btn in (panel._send_btn, panel._cancel_btn)
            },
        }

    def test_default_font_keeps_the_compact_baseline(self) -> None:
        _set_theme(self._panel, "dark")
        report = self._fit_report()
        self.assertGreaterEqual(report["editor_height"], 38, "the default composer floor is 38 px")
        for label, (width, text_width) in report["buttons"].items():
            self.assertGreaterEqual(width, text_width + 8, f"{label} must fit its label at the default font")

    def test_large_font_shows_a_full_line_and_unclipped_buttons(self) -> None:
        panel = self._panel
        _set_theme(panel, "dark")
        try:
            self._config.font_size_override = 24
            panel._apply_font_override()
            _settle()
            report = self._fit_report()
            self.assertGreaterEqual(
                report["editor_height"],
                report["font_height"] + 4,
                "the composer must show a full text line at 24 pt",
            )
            for label, (width, text_width) in report["buttons"].items():
                self.assertGreaterEqual(width, text_width + 8, f"{label} must fit its label at 24 pt")
        finally:
            self._config.font_size_override = 0
            panel._apply_font_override()
            _settle()

    def test_large_font_queue_transition_fits_the_current_label(self) -> None:
        panel = self._panel
        self._config.font_size_override = 24
        panel._apply_font_override()
        _settle()
        panel._ctrl.running.add(panel._ctrl.active_tab_id)
        panel._ctrl.is_agent_running = True
        panel._sync_composer_state()
        _settle()
        self.assertEqual(panel._send_btn.text(), "Queue")
        self.assertGreaterEqual(panel._send_btn.width(), panel._send_btn.sizeHint().width())


@requires_real_qt
class TestMutationPaneIsSessionScoped(_WorkspacePanelTest):
    """The Mutations pane lists the ACTIVE session's undo stack only."""

    def _record(self, description: str):
        from lucnhan.agent.mutation import MutationRecord

        return MutationRecord(
            tool_name="patch_binary",
            arguments={},
            reverse_tool="restore_patch",
            reverse_arguments={},
            description=description,
            reversible=True,
        )

    def _add(self, panel, tab_id: str, count: int) -> None:
        panel._ctrl.mutations.setdefault(tab_id, []).extend(self._record(f"{tab_id} change {i}") for i in range(count))
        panel._refresh_mutation_pane()

    def test_pane_follows_the_active_tab_only(self) -> None:
        panel = self._panel
        tab_a = panel._ctrl.active_tab_id
        self._add(panel, tab_a, 2)
        self.assertEqual(panel._mutation_panel.entry_count(), 2)

        panel._tab_bar._add_btn.click()
        _settle(50)
        tab_b = panel._ctrl.active_tab_id
        self._add(panel, tab_b, 1)
        self.assertEqual(panel._mutation_panel.entry_count(), 1, "the pane must show tab B's own stack")

        panel._tab_widget.setCurrentIndex(0)
        _settle(50)
        self.assertEqual(panel._mutation_panel.entry_count(), 2, "the pane must follow back to tab A")

    def test_successful_undo_drops_the_record(self) -> None:
        from lucnhan.agent.turn import TurnEvent

        panel = self._panel
        tab_id = panel._ctrl.active_tab_id
        self._add(panel, tab_id, 2)

        # The /undo run reversed the newest record; the controller's
        # stack is the source of truth for the pane.
        panel._ctrl.mutations[tab_id].pop()
        panel._on_event(TurnEvent.mutation_undone(1), tab_id)

        self.assertEqual(panel._mutation_panel.entry_count(), 1, "a successful undo removes the record")

    def test_background_undo_does_not_touch_the_visible_pane(self) -> None:
        from lucnhan.agent.turn import TurnEvent

        panel = self._panel
        tab_a = panel._ctrl.active_tab_id
        self._add(panel, tab_a, 2)
        panel._tab_bar._add_btn.click()
        _settle(50)
        tab_b = panel._ctrl.active_tab_id
        self._add(panel, tab_b, 1)
        panel._tab_widget.setCurrentIndex(0)
        _settle(50)
        before = panel._mutation_panel.entry_count()

        # Tab B (in the background) undoes its only record.
        panel._ctrl.mutations[tab_b].clear()
        panel._on_event(TurnEvent.mutation_undone(1), tab_b)

        self.assertEqual(
            panel._mutation_panel.entry_count(),
            before,
            "a background undo must not re-scope or alter the active pane",
        )

    def test_clearing_current_chat_drops_visible_mutations(self) -> None:
        from types import SimpleNamespace

        panel = self._panel
        self._add(panel, panel._ctrl.active_tab_id, 1)
        self.assertEqual(panel._mutation_panel.entry_count(), 1)
        panel._ctrl.session = SimpleNamespace(messages=[object()], last_prompt_tokens=0)
        with patch.object(panel, "_show_new_chat_dialog", return_value="clear"):
            panel._tab_bar._add_btn.click()
        self.assertEqual(panel._ctrl.mutation_records(), [])
        self.assertEqual(panel._mutation_panel.entry_count(), 0)


if __name__ == "__main__":
    unittest.main()
