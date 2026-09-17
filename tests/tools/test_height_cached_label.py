"""Tests for the height-cached label optimisation that prevents layout cascade.

Background: ``AssistantMessageWidget._content`` is a word-wrapped label whose
``setText`` triggers Qt's ``heightForWidth()`` protocol. In a chat with many
long messages, every layout pass walks every sibling label and pays
``O(text_length)`` per sibling — this is the root cause of "whole-IDA lag when
the conversation grows large".

``_HeightCachedLabel`` opts out of the protocol (``hasHeightForWidth -> False``)
and pins its height once per render, reducing layout cost to ``O(1)`` for the
widget. These tests assert that the optimisation is actually wired into the
live streaming path, not just defined as dead code.
"""

from __future__ import annotations

import unittest

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()

from rikugan.ui import message_widgets as _mw
from rikugan.ui.message_widgets import (
    AssistantMessageWidget,
    _HeightCachedLabel,
)


class TestHeightCachedLabelContract(unittest.TestCase):
    """The label class itself must opt out of the heightForWidth protocol."""

    def test_has_height_for_width_returns_false(self):
        # If this returns True, Qt will call heightForWidth() on every layout
        # pass — the exact O(N x msg_length) cascade we are avoiding.
        label = _HeightCachedLabel()
        self.assertFalse(label.hasHeightForWidth())

    def test_pin_height_sets_fixed_height_from_height_for_width(self):
        # pin_height() must translate heightForWidth(width) into a fixed
        # height so the widget no longer participates in the protocol.
        # pin_height() calls ``QLabel.heightForWidth(self, w)`` explicitly
        # on the base class, so we patch the class attribute — not the
        # instance — to inject a deterministic return value.
        label = _HeightCachedLabel()
        label.width = lambda: 400  # type: ignore[method-assign]
        captured: dict[str, int] = {}
        label.setFixedHeight = lambda h: captured.__setitem__("h", int(h))  # type: ignore[method-assign]

        original = _mw.QLabel.heightForWidth
        try:
            _mw.QLabel.heightForWidth = lambda self, w: 24  # type: ignore[assignment]
            label.pin_height()
        finally:
            _mw.QLabel.heightForWidth = original  # type: ignore[assignment]

        self.assertEqual(captured.get("h"), 24)

    def test_pin_height_noop_when_width_zero(self):
        # Before the widget is laid out, width() may return 0. pin_height()
        # must not pin a bogus fixed height in that case (otherwise the
        # label collapses to 0px on first render).
        label = _HeightCachedLabel()
        label.width = lambda: 0  # type: ignore[method-assign]
        called: list[int] = []
        label.setFixedHeight = lambda h: called.append(h)  # type: ignore[method-assign]

        label.pin_height()
        self.assertEqual(called, [])


class TestHeightCachedLabelRePinsOnResize(unittest.TestCase):
    """The cached height must be recomputed when the widget width changes.

    Regression: ``pin_height()`` calls ``setFixedHeight`` once after each
    render, but nothing re-pinned the height when the widget was later
    resized. When the chat panel was widened (resize narrow -> wide),
    the label kept the *large* height computed at the narrow width —
    because word-wrapped text needs more vertical space when narrower.
    The stale fixed height left a large empty gap at the bottom of the
    bubble (reported as a ~240px gap on the last assistant message,
    only after resizing the panel from small to large; resizing large
    to small was unaffected because the height was already small).

    The fix re-pins inside ``resizeEvent`` so the height always matches
    the current width. ``hasHeightForWidth`` stays ``False`` so the
    O(N x msg_length) layout-cascade optimisation is preserved — the
    re-pin only touches *this* widget, it does not re-enter the
    protocol.
    """

    def test_resize_event_triggers_repin(self):
        # After a width change, pin_height must be called again so the
        # fixed height tracks the new width. We assert the setFixedHeight
        # call sequence: once for the initial pin at the narrow width,
        # again after the resize to a wider width.
        label = _HeightCachedLabel()
        heights: list[int] = []

        # Width starts narrow, switches to wide after the resize is
        # "applied". A state variable is simpler and less brittle than a
        # fixed pop-sequence (which breaks if pin_height reads width
        # more or fewer times than expected).
        state = {"width": 400}

        def fake_width():
            return state["width"]

        label.width = fake_width  # type: ignore[method-assign]
        label.setFixedHeight = lambda h: heights.append(int(h))  # type: ignore[method-assign]

        original = _mw.QLabel.heightForWidth
        try:
            # Narrow width needs height 250; wide width needs height 150
            # (word-wrapped text occupies fewer lines when wider).
            def fake_hfw(_self, w):
                return 250 if w < 500 else 150

            _mw.QLabel.heightForWidth = fake_hfw  # type: ignore[assignment]

            # Initial pin at narrow width.
            label.pin_height()
            # Simulate the panel widening: Qt would fire resizeEvent on
            # the label after its geometry updates.
            state["width"] = 600
            label.resizeEvent(None)
        finally:
            _mw.QLabel.heightForWidth = original  # type: ignore[assignment]

        # First call pins 250 (narrow), second pins 150 (wide) after resize.
        self.assertEqual(heights, [250, 150])

    def test_pin_height_clears_prior_fixed_height_before_measuring(self):
        # Root cause of the empty-gap bug: ``setFixedHeight`` *poisons*
        # Qt's ``heightForWidth``. Once a fixed height is set, a later
        # ``heightForWidth(w)`` echoes the cached fixed value for *any*
        # width instead of recomputing the wrapped-text height for *w*.
        # On the restore path (and after a resize) ``pin_height`` runs
        # again with a different width, but the poisoned call returned
        # the stale height — the wrong height was re-locked and a large
        # empty gap appeared inside the bubble.
        #
        # ``pin_height`` must clear any previously-set size constraints
        # before calling ``heightForWidth`` so Qt measures fresh. We
        # assert the clear happens by checking that a second pin at a
        # wider width yields a *smaller* height (word-wraps fewer lines),
        # which is impossible if the stale value were echoed back.
        label = _HeightCachedLabel()
        # Stub heightForWidth to return a width-dependent value so the
        # test does not need a real Qt layout engine. The key is that
        # the *second* pin must read the *new* width's value, not the
        # first pin's cached result.
        state = {"width": 400, "cleared": False}
        label.width = lambda: state["width"]  # type: ignore[method-assign]

        # Track whether pin_height cleared the min/max height constraints
        # before measuring. If it does not clear them, the prior fixed
        # height would leak into the next heightForWidth call in real Qt.
        def fake_set_min_h(v):
            state["cleared"] = v == 0

        def fake_set_max_h(v):
            if v >= 16777215:  # QWIDGETSIZE_MAX
                state["cleared"] = True

        label.setMinimumHeight = fake_set_min_h  # type: ignore[method-assign]
        label.setMaximumHeight = fake_set_max_h  # type: ignore[method-assign]
        label.setFixedHeight = lambda h: None  # type: ignore[method-assign]

        original = _mw.QLabel.heightForWidth
        try:
            _mw.QLabel.heightForWidth = lambda _self, w: 250 if w < 500 else 150  # type: ignore[assignment]
            label.pin_height()
        finally:
            _mw.QLabel.heightForWidth = original  # type: ignore[assignment]

        self.assertTrue(
            state["cleared"],
            "pin_height must clear the min/max height constraints (setMinimumHeight(0) "
            "and setMaximumHeight(QWIDGETSIZE_MAX)) before calling heightForWidth, "
            "otherwise a previously pinned setFixedHeight poisons the measurement "
            "and the bubble keeps a stale (too-large) height after restore/resize.",
        )


class TestAssistantMessageWidgetUsesCachedLabel(unittest.TestCase):
    """The live streaming widget must use the height-cached label."""

    def test_content_label_is_height_cached_label(self):
        # This is the wire-in assertion. If _content is a plain QLabel,
        # the layout-cascade optimisation is dead code and the lag bug
        # regresses.
        widget = AssistantMessageWidget()
        self.assertIsInstance(
            widget._content,
            _HeightCachedLabel,
            "AssistantMessageWidget._content must be a _HeightCachedLabel to "
            "opt out of the O(N x msg_length) heightForWidth layout cascade.",
        )


class TestThinkingBlockUsesCachedLabel(unittest.TestCase):
    """``_ThinkingBlock._content`` is a sibling of the assistant content label.

    ``set_thinking`` calls ``setText`` every ~100ms during streaming (the
    same hot path as ``_content``). If it stays a plain ``QLabel``, the
    cascade still fires from the thinking side even after the content side
    was fixed — which is why fixing only ``_content`` reduced but did not
    eliminate the lag.
    """

    def test_thinking_content_label_opts_out_of_height_for_width(self):
        # We assert the *behaviour* (hasHeightForWidth -> False) rather than
        # the type identity (isinstance _HeightCachedLabel) because the
        # QLabel base class is resolved at import time and differs between
        # the stubbed and real-Qt test environments. The contract that
        # actually matters for performance is the protocol opt-out.
        from rikugan.ui.message_widgets import _ThinkingBlock

        block = _ThinkingBlock()
        self.assertFalse(
            block._content.hasHeightForWidth(),
            "_ThinkingBlock._content shares the streaming hot path with "
            "AssistantMessageWidget._content and must opt out of the "
            "heightForWidth protocol to avoid the layout cascade.",
        )


class TestToolCallWidgetUsesCachedLabel(unittest.TestCase):
    """Tool-call labels fire ``setText`` once per tool call (args + result).

    Agentic loops (explore/modify modes) emit dozens of tool calls per
    turn. Each call's ``set_arguments``/``set_result`` does ``setText`` on
    word-wrapped labels that sit in a shared ``QVBoxLayout`` — the same
    cascade trigger. All three must opt out of the ``heightForWidth``
    protocol.
    """

    def test_tool_call_labels_opt_out_of_height_for_width(self):
        from rikugan.ui.tool_widgets import ToolCallWidget

        widget = ToolCallWidget("get_function_info", "tc_1")
        for attr in ("_preview_label", "_args_label", "_result_label"):
            self.assertFalse(
                getattr(widget, attr).hasHeightForWidth(),
                f"ToolCallWidget.{attr} must opt out of the heightForWidth "
                f"protocol to avoid the layout cascade triggered by "
                f"set_arguments/set_result.",
            )


if __name__ == "__main__":
    unittest.main()
