"""Tests for the isolated, passive ``HistoryPanel`` widget.

Spec sections exercised:
- §6.3 (panel content + state copy)
- §6.4 (passive widget — no SessionHistory/config/thread/executor/I/O imports)
- §8.4 (search is case-insensitive title-only, no worker hit)
- §11.3 (titles are plain-text, never interpolated into QSS)
- §13 (Retry only on error; empty / search-empty / error copy exact)

The widget is intentionally passive: tests use the Qt stub injection
from ``tests.qt_stubs`` so no real PySide6 runtime is required. Signal
emission is asserted via direct call to ``clicked.emit()`` /
``mouseReleaseEvent(None)`` so the suite stays deterministic — no event
loop, no flaky pixel measurement.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()

# Ensure the real module is loaded even if a sibling test stubbed it.
sys.modules.pop("rikugan.ui.history_panel", None)

from rikugan.ui.history_panel import (
    HISTORY_PANEL_CONTENT_MAX_WIDTH,
    HistoryPanel,
    HistoryRowWidget,
)
from rikugan.ui.qt_compat import Qt


@dataclass(frozen=True)
class _EntryFactory:
    """Build a SessionHistoryEntry-compatible object without importing the
    full DTO module. The widget's signature only requires the named
    attributes the History row reads (``title``, ``session_id``,
    ``provider``, ``model``, ``message_count``, ``updated_at``); other
    fields are ignored.
    """

    session_id: str = "session-a"
    title: str = "Untitled chat"
    created_at: float = 0.0
    updated_at: float = 0.0
    provider: str = ""
    model: str = ""
    message_count: int = 0


def _entry(
    session_id: str = "session-a",
    title: str = "Untitled chat",
    *,
    provider: str = "",
    model: str = "",
    message_count: int = 0,
    updated_at: float = 0.0,
) -> _EntryFactory:
    """Construct a minimal history entry with sensible defaults.

    Tests can override any named field via keyword args; positional
    arguments cover the common ``session_id`` + ``title`` case the
    spec uses as its primary example.
    """
    return _EntryFactory(
        session_id=session_id,
        title=title,
        created_at=0.0,
        updated_at=updated_at,
        provider=provider,
        model=model,
        message_count=message_count,
    )


class TestEmptyStateCopy(unittest.TestCase):
    def test_no_entries_shows_idb_empty_message(self) -> None:
        panel = HistoryPanel()
        panel.set_entries([])
        self.assertEqual(
            panel._status_label.text(),
            "No saved chats for this IDB yet.",
        )
        # Retry must not appear when no entries have been delivered.
        self.assertFalse(panel._retry_btn.isVisible())

    def test_search_miss_shows_search_empty_message(self) -> None:
        panel = HistoryPanel()
        panel.set_entries([_entry("a", "Analyze Parser")])
        panel._search.setText("missing")
        self.assertEqual(
            panel._status_label.text(),
            "No chats match your search.",
        )
        self.assertFalse(panel._retry_btn.isVisible())

    def test_header_titles_are_exact_copy(self) -> None:
        panel = HistoryPanel()
        self.assertEqual(panel._title.text(), "Chat History")
        self.assertEqual(panel._scope_label.text(), "Current IDB")
        self.assertEqual(panel._search.placeholderText(), "Search conversations…")


class TestSearchFiltering(unittest.TestCase):
    def test_search_is_case_insensitive_title_only(self) -> None:
        panel = HistoryPanel()
        panel.set_entries(
            [
                _entry("a", "Analyze Parser"),
                _entry("b", "Map imports"),
            ]
        )
        panel._search.setText(" parser ")
        # Whitespace is stripped; ``casefold`` matches title substring.
        self.assertEqual(panel.visible_session_ids(), ["a"])

    def test_empty_query_returns_full_list(self) -> None:
        panel = HistoryPanel()
        panel.set_entries(
            [
                _entry("a", "Foo"),
                _entry("b", "Bar"),
            ]
        )
        panel._search.setText("foo")  # matches "Foo" only
        self.assertEqual(panel.visible_session_ids(), ["a"])
        panel._search.setText("")
        # Empty query restores the full set (cache-only, no worker hit).
        self.assertEqual(panel.visible_session_ids(), ["a", "b"])

    def test_search_never_inspects_other_fields(self) -> None:
        panel = HistoryPanel()
        panel.set_entries(
            [
                _entry("a", "Foo", provider="claude", model="sonnet"),
            ]
        )
        # Query that matches only a provider/model field must NOT match —
        # search is title-only per spec §8.4.
        panel._search.setText("claude")
        self.assertEqual(panel.visible_session_ids(), [])


class TestPlainTextTitles(unittest.TestCase):
    def test_row_title_is_plain_text(self) -> None:
        panel = HistoryPanel()
        panel.set_entries([_entry(title="<b>not rich</b>")])
        row = panel._row_widgets[0]
        self.assertEqual(row._title.textFormat(), Qt.TextFormat.PlainText)

    def test_row_meta_is_plain_text(self) -> None:
        panel = HistoryPanel()
        panel.set_entries([_entry(title="x")])
        row = panel._row_widgets[0]
        self.assertEqual(row._meta.textFormat(), Qt.TextFormat.PlainText)

    def test_status_label_is_plain_text(self) -> None:
        """The status label carries internal copy (empty / loading / error)
        and must never auto-promote to rich text — Qt's heuristic
        would otherwise allow a malformed copy to escape into HTML."""
        panel = HistoryPanel()
        self.assertEqual(panel._status_label.textFormat(), Qt.TextFormat.PlainText)

    def test_status_label_is_plain_text_after_error(self) -> None:
        """``set_error`` updates the label text; the format guard
        survives the swap."""
        panel = HistoryPanel()
        panel.set_error("Recent chats are still being saved.")
        self.assertEqual(panel._status_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(panel._status_label.text(), "Recent chats are still being saved.")


class TestNoHorizontalOverflow(unittest.TestCase):
    """320 px no-overflow is asserted via deterministic widget properties.

    The panel caps its content width via ``setMaximumWidth`` and forces
    every title / meta label to wrap and respect the same cap. These are
    observable widget state, not pixel measurements, so the test is
    deterministic.
    """

    def test_panel_maximum_width_is_capped_at_320(self) -> None:
        panel = HistoryPanel()
        self.assertLessEqual(panel.maximumWidth(), HISTORY_PANEL_CONTENT_MAX_WIDTH)
        self.assertEqual(HISTORY_PANEL_CONTENT_MAX_WIDTH, 320)

    def test_long_title_label_word_wraps_and_caps_width(self) -> None:
        panel = HistoryPanel()
        panel.set_entries([_entry(title="A" * 240)])
        row = panel._row_widgets[0]
        # Title is forced to wrap so a long string never overflows.
        self.assertTrue(row._title.wordWrap())
        # And it is capped to the content width.
        self.assertLessEqual(row._title.maximumWidth(), HISTORY_PANEL_CONTENT_MAX_WIDTH)

    def test_long_meta_label_word_wraps_and_caps_width(self) -> None:
        panel = HistoryPanel()
        panel.set_entries([_entry(title="x", provider="claude", model="opus-x" * 30, message_count=999)])
        row = panel._row_widgets[0]
        self.assertTrue(row._meta.wordWrap())
        self.assertLessEqual(row._meta.maximumWidth(), HISTORY_PANEL_CONTENT_MAX_WIDTH)

    def test_scroll_area_horizontal_policy_is_always_off(self) -> None:
        """The list scroll area must never show a horizontal scrollbar —
        the canonical ``setHorizontalScrollBarPolicy`` API is used so
        a row title cannot widen the panel past its 320 px cap."""
        panel = HistoryPanel()
        scroll = panel._list_scroll
        self.assertEqual(
            scroll.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )

    def test_scroll_area_keeps_border_and_background_qss(self) -> None:
        """The horizontal-scrollbar QSS rule was removed when the
        scrollbar API took over, but the border / background QSS for
        the scroll area is still applied via ``setStyleSheet`` so the
        host theme paints the area consistently."""
        panel = HistoryPanel()
        scroll = panel._list_scroll
        qss = scroll.styleSheet()
        self.assertIn("QScrollArea", qss)
        self.assertIn("border: none", qss)
        # The scrollbar height hack from the previous implementation is
        # gone — verifying the API-based fix took effect.
        self.assertNotIn("QScrollBar:horizontal", qss)


class TestRetryVisibility(unittest.TestCase):
    def test_retry_hidden_after_set_entries(self) -> None:
        panel = HistoryPanel()
        panel.set_error("boom", retry_visible=True)
        self.assertTrue(panel._retry_btn.isVisible())
        panel.set_entries([_entry("a", "Foo")])
        self.assertFalse(panel._retry_btn.isVisible())

    def test_retry_hidden_when_error_says_so(self) -> None:
        panel = HistoryPanel()
        panel.set_error("Recent chats are still being saved.", retry_visible=False)
        self.assertFalse(panel._retry_btn.isVisible())

    def test_retry_shown_only_after_set_error_with_retry(self) -> None:
        panel = HistoryPanel()
        # Default state: no retry.
        self.assertFalse(panel._retry_btn.isVisible())
        panel.set_loading()
        self.assertFalse(panel._retry_btn.isVisible())
        panel.set_error("Boom", retry_visible=True)
        self.assertTrue(panel._retry_btn.isVisible())


class TestSignals(unittest.TestCase):
    def test_close_button_emits_close_requested(self) -> None:
        panel = HistoryPanel()
        captured: list[str] = []
        panel.close_requested.connect(lambda: captured.append("close"))
        panel._close_btn.clicked.emit()
        self.assertEqual(captured, ["close"])

    def test_retry_button_emits_retry_requested(self) -> None:
        panel = HistoryPanel()
        panel.set_error("Boom", retry_visible=True)
        captured: list[str] = []
        panel.retry_requested.connect(lambda: captured.append("retry"))
        panel._retry_btn.clicked.emit()
        self.assertEqual(captured, ["retry"])

    def test_row_click_emits_session_open_requested_with_session_id(self) -> None:
        panel = HistoryPanel()
        panel.set_entries([_entry("session-42", "Analyze parser")])
        captured: list[str] = []
        panel.session_open_requested.connect(captured.append)
        # ``mouseReleaseEvent`` is a class method, so a synthetic
        # release on the row dispatches the open request.
        row = panel._row_widgets[0]
        row.mouseReleaseEvent(None)
        self.assertEqual(captured, ["session-42"])

    def test_mouse_release_event_is_a_class_method(self) -> None:
        """``mouseReleaseEvent`` must be a class method, not an instance
        attribute set in ``__init__``. Instance-assigned callables
        bypass PySide6's Shiboken dispatch path and can be silently
        masked by a class-level override in a future refactor."""
        from rikugan.ui.history_panel import HistoryRowWidget

        self.assertIn("mouseReleaseEvent", vars(HistoryRowWidget))
        # The descriptor is a function, not a per-instance bound method.
        self.assertTrue(callable(HistoryRowWidget.mouseReleaseEvent))

    def test_mouse_release_event_emits_with_correct_session_id(self) -> None:
        """Class-method form preserves the same behaviour as the
        previous instance-assignment: a click emits the bound entry's
        session_id through the row's signal."""
        panel = HistoryPanel()
        panel.set_entries(
            [
                _entry("session-a", "Foo"),
                _entry("session-b", "Bar"),
            ]
        )
        captured: list[str] = []
        panel.session_open_requested.connect(captured.append)
        # Click each row separately.
        for row in panel._row_widgets:
            row.mouseReleaseEvent(None)
        self.assertEqual(captured, ["session-a", "session-b"])


class TestDeleteAffordance(unittest.TestCase):
    """Task 4 — passive delete affordance on the row (presentation only).

    The delete button is a child QPushButton — its ``clicked`` signal
    must emit the entry's ``session_id`` and ``title`` through the
    ``session_delete_requested(str, str)`` row signal without bubbling
    up to ``mouseReleaseEvent`` (which would also fire
    ``session_open_requested``). The button is keyboard-focusable so a
    keyboard user can reach and activate it via Space/Return without
    firing an unintended row-open.
    """

    def test_delete_emits_id_and_title_without_opening_row(self) -> None:
        row = HistoryRowWidget(_entry("abc123", "Analyze parser"))
        deleted: list[tuple[str, str]] = []
        opened: list[str] = []
        row.session_delete_requested.connect(lambda session_id, title: deleted.append((session_id, title)))
        row.session_open_requested.connect(opened.append)

        row._delete_btn.clicked.emit()

        self.assertEqual(deleted, [("abc123", "Analyze parser")])
        self.assertEqual(opened, [])

    def test_delete_button_has_accessible_copy(self) -> None:
        row = HistoryRowWidget(_entry("abc123", "Analyze parser"))
        self.assertEqual(row._delete_btn.toolTip(), "Delete chat")
        self.assertEqual(
            row._delete_btn.accessibleName(),
            "Delete chat: Analyze parser",
        )


class TestDeletePanelState(unittest.TestCase):
    """Task 4 — passive panel API for delete coordination.

    The panel owns the row cache so ``remove_entry`` can preserve the
    current search query and clamp scroll position across a rerender.
    ``set_operation_pending`` disables the row's open/delete controls
    without disabling search. The notice is a dedicated row above the
    list — it never hides the cached rows.
    """

    def test_panel_forwards_delete_intent(self) -> None:
        panel = HistoryPanel()
        panel.set_entries([_entry("abc123", "Analyze parser")])
        deleted: list[tuple[str, str]] = []
        panel.session_delete_requested.connect(lambda session_id, title: deleted.append((session_id, title)))

        panel._row_widgets[0]._delete_btn.clicked.emit()

        self.assertEqual(deleted, [("abc123", "Analyze parser")])

    def test_remove_entry_preserves_query_and_remaining_rows(self) -> None:
        panel = HistoryPanel()
        panel.set_entries(
            [
                _entry("a", "Analyze parser"),
                _entry("b", "Analyze TLS"),
            ]
        )
        panel._search.setText("analyze")

        panel.remove_entry("a")

        self.assertEqual(panel._search.text(), "analyze")
        self.assertEqual(panel.visible_session_ids(), ["b"])

    def test_pending_disables_row_operations_but_not_search(self) -> None:
        panel = HistoryPanel()
        panel.set_entries([_entry("a", "Analyze parser")])

        panel.set_operation_pending("a")

        self.assertFalse(panel._row_widgets[0]._delete_btn.isEnabled())
        self.assertTrue(panel._search.isEnabled())
        panel.set_operation_pending(None)
        self.assertTrue(panel._row_widgets[0]._delete_btn.isEnabled())

    def test_notice_preserves_cached_rows_and_can_be_dismissed(self) -> None:
        panel = HistoryPanel()
        panel.set_entries([_entry("a", "Analyze parser")])
        dismissed: list[bool] = []
        panel.notice_dismissed.connect(lambda: dismissed.append(True))

        panel.show_notice("Could not delete this chat.", retry_visible=True)

        self.assertEqual(panel.visible_session_ids(), ["a"])
        self.assertEqual(panel._notice_label.text(), "Could not delete this chat.")
        self.assertTrue(panel._notice_frame.isVisible())
        self.assertTrue(panel._notice_retry_btn.isVisible())
        panel._notice_dismiss_btn.clicked.emit()
        self.assertEqual(dismissed, [True])
        self.assertEqual(panel.visible_session_ids(), ["a"])
        self.assertFalse(panel._notice_frame.isVisible())


class TestClearResetsCachedEntriesAndQuery(unittest.TestCase):
    def test_clear_resets_cached_entries(self) -> None:
        panel = HistoryPanel()
        panel.set_entries([_entry("a", "Foo"), _entry("b", "Bar")])
        self.assertEqual(len(panel._row_widgets), 2)
        panel.clear()
        # No rows remain.
        self.assertEqual(panel._row_widgets, [])
        # Status copy reverts to the IDB-empty message.
        self.assertEqual(
            panel._status_label.text(),
            "No saved chats for this IDB yet.",
        )

    def test_clear_resets_query(self) -> None:
        panel = HistoryPanel()
        panel.set_entries([_entry("a", "Foo")])
        panel._search.setText("foo")
        panel.clear()
        # Search box resets so the user starts a fresh query on reopen.
        self.assertEqual(panel._search.text(), "")
        # A subsequent ``set_entries`` shows the full list, not the
        # cached query-filtered subset.
        panel.set_entries([_entry("a", "Foo"), _entry("b", "Bar")])
        self.assertEqual(panel.visible_session_ids(), ["a", "b"])


class TestLoadingState(unittest.TestCase):
    def test_set_loading_does_not_drop_cached_rows(self) -> None:
        """Reopening history starts a refresh; cached rows survive until
        PanelCore delivers a new list. The widget must not clear rows
        when entering the loading state — that is PanelCore's job via
        ``set_entries`` / ``clear``."""
        panel = HistoryPanel()
        panel.set_entries([_entry("a", "Foo")])
        self.assertEqual(len(panel._row_widgets), 1)
        panel.set_loading()
        # Loading status copy is shown, but cached rows are preserved.
        self.assertEqual(len(panel._row_widgets), 1)
        self.assertEqual(panel._status_label.text(), "Loading chats…")
        self.assertFalse(panel._retry_btn.isVisible())

    def test_set_loading_preserves_search_query(self) -> None:
        panel = HistoryPanel()
        panel.set_entries([_entry("a", "Foo")])
        panel._search.setText("foo")
        panel.set_loading()
        self.assertEqual(panel._search.text(), "foo")


class TestShutdownIsIdempotent(unittest.TestCase):
    def test_shutdown_can_be_called_twice(self) -> None:
        panel = HistoryPanel()
        panel.set_entries([_entry("a", "Foo")])
        panel.shutdown()
        # No exception; rows still accessible for inspection.
        panel.shutdown()
        self.assertEqual(len(panel._row_widgets), 1)


class TestAccessibility(unittest.TestCase):
    """Task 4 a11y follow-ups — make the passive delete UI accessible.

    Four concrete gaps closed here (each verified end-to-end with a
    deterministic property assertion, not a brittle QSS substring):

    1. Delete button is at least 24x24 px so it meets WCAG 2.5.5 target
       size minimums — the value is asserted via the widget's own
       ``minimumSize`` getter, not by matching the stylesheet text.
    2. ``show_notice`` dispatches a ``QAccessibleAnnouncementEvent`` for
       the message so a screen reader announces it on display.
    3. Notice retry/dismiss + status retry buttons are explicitly
       ``Qt.FocusPolicy.StrongFocus`` so keyboard users can reach them.
    4. ``remove_entry`` restores keyboard focus to the next surviving
       delete button, the previous one if the deleted row was last,
       or the search box when the list is now empty.
    """

    def test_delete_button_minimum_size_is_at_least_24x24(self) -> None:
        """The delete affordance must meet the WCAG 2.5.5 24x24 minimum
        so a keyboard or motor-impaired user can hit it. The widget
        applies ``setMinimumSize(24, 24)`` so the size is pinned
        regardless of the token-driven QSS overrides.
        """
        row = HistoryRowWidget(_entry("a", "Title"))

        min_size = row._delete_btn.minimumSize()
        self.assertGreaterEqual(min_size.width(), 24)
        self.assertGreaterEqual(min_size.height(), 24)

    def test_show_notice_dispatches_accessibility_announcement(self) -> None:
        """``show_notice`` must publish the message through a
        ``QAccessibleAnnouncementEvent`` so screen readers announce
        it on display. The stub captures each announcement into a
        thread-local list so the assertion can inspect it.
        """
        from rikugan.ui.qt_compat import QAccessibleAnnouncementEvent

        panel = HistoryPanel()
        panel.set_entries([_entry("a", "Title")])

        announcements: list[tuple[object, str]] = []
        panel._capture_announcements(announcements.append)

        panel.show_notice("Could not delete this chat.")

        self.assertEqual(len(announcements), 1)
        _target, message = announcements[0]
        self.assertEqual(message, "Could not delete this chat.")
        # The event object must be a real ``QAccessibleAnnouncementEvent``,
        # not a duck-typed dict. Screen readers consume the event type
        # to decide what to do with the payload.
        self.assertIsInstance(announcements[0][0], QAccessibleAnnouncementEvent)

    def test_clear_notice_does_not_announce(self) -> None:
        """``clear_notice`` is a terminal-success path; it must not
        re-announce the same message (which would re-read the error
        to the user after they already acknowledged it via a fresh
        ``set_entries`` call).
        """
        panel = HistoryPanel()
        panel.set_entries([_entry("a", "Title")])

        announcements: list[tuple[object, str]] = []
        panel._capture_announcements(announcements.append)
        panel.show_notice("Could not delete this chat.")
        panel.clear_notice()

        self.assertEqual(len(announcements), 1)

    def test_notice_buttons_use_strong_focus(self) -> None:
        """Notice retry + dismiss buttons must be reachable via Tab so
        keyboard users can dismiss a delete error without reaching
        for the mouse.
        """
        panel = HistoryPanel()
        panel.set_entries([_entry("a", "Title")])
        panel.show_notice("Could not delete this chat.", retry_visible=True)

        self.assertEqual(panel._notice_retry_btn.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        self.assertEqual(panel._notice_dismiss_btn.focusPolicy(), Qt.FocusPolicy.StrongFocus)

    def test_status_retry_button_uses_strong_focus(self) -> None:
        """The status-frame retry button must also be ``StrongFocus``
        for the same reason — keyboard users must be able to Tab to
        it after a load failure.
        """
        panel = HistoryPanel()

        self.assertEqual(panel._retry_btn.focusPolicy(), Qt.FocusPolicy.StrongFocus)

    def test_remove_entry_restores_focus_to_next_delete_button(self) -> None:
        """Deleting the first row should move keyboard focus to the
        second row's delete button so the user keeps a delete-friendly
        target without an extra Tab press.
        """
        panel = HistoryPanel()
        panel.set_entries(
            [
                _entry("a", "Title A"),
                _entry("b", "Title B"),
                _entry("c", "Title C"),
            ]
        )
        panel._row_widgets[0]._delete_btn.setFocus()

        panel.remove_entry("a")

        self.assertTrue(panel._row_widgets[0]._delete_btn.hasFocus())
        self.assertEqual(panel._row_widgets[0].entry.session_id, "b")

    def test_remove_entry_restores_focus_to_previous_when_last_deleted(self) -> None:
        """If the deleted row was last, focus falls back to the new
        last row's delete button — otherwise the user lands on the
        search box with no row-level anchor.
        """
        panel = HistoryPanel()
        panel.set_entries(
            [
                _entry("a", "Title A"),
                _entry("b", "Title B"),
                _entry("c", "Title C"),
            ]
        )
        panel._row_widgets[2]._delete_btn.setFocus()

        panel.remove_entry("c")

        # Index 1 is now the last row after "c" is removed.
        self.assertTrue(panel._row_widgets[1]._delete_btn.hasFocus())
        self.assertEqual(panel._row_widgets[1].entry.session_id, "b")

    def test_remove_entry_restores_focus_to_search_when_list_empty(self) -> None:
        """When the deleted row was the only one, focus moves to the
        search box so the keyboard user retains a meaningful anchor.
        """
        panel = HistoryPanel()
        panel.set_entries([_entry("a", "Solo")])
        panel._row_widgets[0]._delete_btn.setFocus()

        panel.remove_entry("a")

        self.assertTrue(panel._search.hasFocus())

    def test_remove_entry_does_not_steal_focus_when_orphan(self) -> None:
        """If focus is NOT on the deleted row's delete button when
        ``remove_entry`` is called (programmatic path), focus must be
        left where the caller placed it. The restore logic only fires
        for keyboard-driven deletes.
        """
        panel = HistoryPanel()
        panel.set_entries(
            [
                _entry("a", "Title A"),
                _entry("b", "Title B"),
            ]
        )
        panel._search.setFocus()

        panel.remove_entry("a")

        self.assertTrue(panel._search.hasFocus())

    def test_apply_styles_refreshes_notice_widgets(self) -> None:
        """``_apply_styles`` must re-apply notice-frame styles so a
        theme change propagates to the transient notice row. The
        assertion is via the captured stylesheet text on each notice
        child, which is the same surface the row tests use.
        """
        panel = HistoryPanel()
        panel.set_entries([_entry("a", "Title")])
        panel.show_notice("hello", retry_visible=True)

        # Snapshot, re-apply, snapshot again. The values must remain
        # non-empty after re-application so a future token change
        # actually flows through.
        panel._apply_styles()
        self.assertNotEqual(panel._notice_label.styleSheet(), "")
        self.assertNotEqual(panel._notice_retry_btn.styleSheet(), "")
        self.assertNotEqual(panel._notice_dismiss_btn.styleSheet(), "")

    def test_remove_entry_clamps_scroll_against_new_maximum(self) -> None:
        """Direct scroll-clamp assertion: if the pre-delete scroll
        position exceeds the new maximum after a rerender, the
        restored value must clamp to the new maximum (not stay
        above it). This is the spec promise in ``remove_entry``.

        The precondition sets the raw value to ``100`` while the
        maximum is ``50`` — value > maximum on purpose. The stub
        allows raw values greater than maximum (it stores
        ``_value`` as a plain int without clamping), so this is a
        valid starting state. After the rerender the panel's
        ``min(old_scroll, scrollbar.maximum())`` clamp is the only
        thing bringing the value back down. Removing the ``min()``
        from ``remove_entry`` would leave ``value == 100`` while the
        new maximum is something smaller, and the assertion would
        fail.
        """
        panel = HistoryPanel()
        panel.set_entries(
            [
                _entry("a", "Title A"),
                _entry("b", "Title B"),
                _entry("c", "Title C"),
            ]
        )
        scrollbar = panel._list_scroll.verticalScrollBar()
        # Precondition: maximum=50, raw value=100 (intentionally
        # above the maximum — the stub does NOT clamp on setValue
        # so this is a legal starting state).
        scrollbar.setMaximum(50)
        scrollbar.setValue(100)
        self.assertEqual(scrollbar.value(), 100)

        panel.remove_entry("c")

        # After rerender the stub's maximum reflects the surviving
        # row count. The panel must have clamped the legacy 100
        # down to the new maximum via ``min(old_scroll, maximum)``;
        # without that clamp, ``value() == 100`` would persist and
        # the inequality would flip.
        self.assertLessEqual(scrollbar.value(), scrollbar.maximum())


class TestNoForbiddenImports(unittest.TestCase):
    """Passive widget invariant: it never imports SessionHistory, the
    config layer, threading primitives, executors, or I/O modules.

    Tasks 8-10 own PanelCore composition. If this test ever fails,
    the widget has grown a forbidden seam that must be removed before
    shipping.

    Substring matches like ``"SessionHistory"`` in ``"SessionHistoryEntry"``
    are allowed because the entry DTO is a frozen data contract the
    widget explicitly consumes — only SessionHistory (the persistence
    layer) and its public methods are forbidden.
    """

    def test_history_panel_does_not_import_io_or_threading(self) -> None:
        import pathlib
        import re

        source = (pathlib.Path(__file__).parents[2] / "rikugan/ui/history_panel.py").read_text(encoding="utf-8")
        # Word-boundary match so "SessionHistory" inside
        # "SessionHistoryEntry" or comments referring to the DTO class
        # does not trip the assertion. ``SessionHistory`` is the
        # persistence layer (forbidden); ``SessionHistoryEntry`` is
        # the immutable DTO the widget consumes (allowed).
        forbidden_patterns = (
            (r"\bSessionHistory\b", "SessionHistory (persistence layer)"),
            (r"\bsave_session\b", "save_session (storage seam)"),
            (r"\bload_session\b", "load_session (storage seam)"),
            (r"\blist_sessions\b", "list_sessions (storage seam)"),
            (r"\bThreadPoolExecutor\b", "ThreadPoolExecutor (threading)"),
            (r"^import threading\b", "threading module"),
            (r"^from threading\b", "threading module"),
            (r"\bsubprocess\b", "subprocess module"),
            (r"\bos\.system\b", "os.system (process spawning)"),
            (r"\bpanel_core\b", "panel_core (composition seam)"),
        )
        for pattern, label in forbidden_patterns:
            self.assertIsNone(
                re.search(pattern, source, re.MULTILINE),
                f"history_panel.py must not reference {label!r} (pattern: {pattern!r})",
            )


if __name__ == "__main__":
    unittest.main()
