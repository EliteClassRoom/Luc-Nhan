"""Regression tests for ``ask_user`` option normalization.

The agent loop accepts ``options`` as a pseudo-tool argument derived from
LLM output — untrusted input whose exact JSON shape we cannot control.
The loop's only responsibility at the argument boundary is to normalize
whatever shape arrives into a ``list[str]`` of clean button labels, or
``[]`` for an open-ended question.

Two concrete defects motivated these tests:

* ``options="Yes"`` (a bare string) currently iterates character-by-
  character and produces ``['Y', 'e', 's']`` — three meaningless buttons
  the user cannot use.
* ``options=[{"label": "Yes"}, {"label": "No"}]`` (object form) is
  silently dropped to ``[]`` — the panel renders NO buttons and the
  user has nothing to click.

Both are user-visible bugs. The tests below pin the normalized output
on the observable ``USER_QUESTION`` event so a regression anywhere in
the loop→event path is caught.

These tests are Qt-free on purpose. ``UserQuestionWidget`` already
handles dict/``label`` options correctly, and many sibling tests pollute
``sys.modules`` with ``tests.qt_stubs.ensure_pyside6_stubs()``. Keeping
the normalization test at the loop layer avoids that whole class of
pollution and exercises the real contract: the metadata emitted on the
event stream that the UI consumes.
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.agent.loop import AgentLoop
from lucnhan.agent.turn import TurnEvent, TurnEventType
from lucnhan.core.config import LucNhanConfig
from lucnhan.core.types import ModelInfo, ProviderCapabilities, ToolCall
from lucnhan.providers.base import LLMProvider
from lucnhan.state.session import SessionState


class _NullProvider(LLMProvider):
    """Provider stub — these tests drive ``_handle_ask_user_tool`` directly."""

    def __init__(self) -> None:
        super().__init__(api_key="test", model="mock-model")

    @property
    def name(self) -> str:
        return "mock"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def _get_client(self) -> None:
        return None

    def _fetch_models_live(self) -> list[ModelInfo]:
        return [ModelInfo(id="mock-model", name="Mock", provider="mock")]

    @staticmethod
    def _builtin_models() -> list[ModelInfo]:
        return [ModelInfo(id="mock-model", name="Mock", provider="mock")]

    def _format_messages(self, messages: list) -> list:
        return messages

    def _build_request_kwargs(self, messages, tools, temperature, max_tokens, system, **kwargs):
        return {}

    def _call_api(self, client, kwargs):
        return None

    def _normalize_response(self, raw):
        return raw

    def _handle_api_error(self, e: Exception) -> None:
        raise e

    def _stream_chunks(self, client, kwargs, cancel_event=None):
        yield from ()


def _make_loop() -> AgentLoop:
    """Build a minimally-wired ``AgentLoop`` without running ``__init__``.

    We avoid the heavy ``__init__`` (which touches IDA, the registry, and
    threading) because the test only needs the fields that
    ``_handle_ask_user_tool`` reads: ``_unattended``, ``_user_answer_queue``,
    ``_cancelled``, and ``_wait_for_queue``. The queue is pre-loaded with
    an answer so the generator never blocks on ``_wait_for_queue``.
    """
    import queue as _queue
    import threading as _threading

    loop = AgentLoop.__new__(AgentLoop)
    loop._unattended = False
    loop._cancelled = _threading.Event()
    loop._user_answer_queue = _queue.Queue(maxsize=1)
    loop._user_answer_queue.put("")
    return loop


def _collect_event(loop: AgentLoop, arguments: dict[str, Any]) -> TurnEvent:
    """Drive ``_handle_ask_user_tool`` up to the first emitted event.

    Returns the ``USER_QUESTION`` event whose ``metadata["options"]``
    is the normalized, observable contract these tests pin.
    """
    tc = ToolCall(id="call_ask_user_regression", name="ask_user", arguments=arguments)
    gen = loop._handle_ask_user_tool(tc)
    event = next(gen)
    # Drain the rest of the generator cleanly so the queue item is consumed.
    try:
        while True:
            next(gen)
    except StopIteration:
        pass
    return event


class TestAskUserOptionsStringAndObjectShapes(unittest.TestCase):
    """Headline regressions: bare string and object options must survive."""

    def test_bare_string_options_preserved_as_single_choice(self) -> None:
        """``options="Yes"`` must normalize to ``["Yes"]`` — not ``['Y','e','s']``.

        Today the loop iterates the string character-by-character because
        ``isinstance("Yes", str)`` is True. The user's intended choice
        becomes three meaningless buttons and is unrecoverable.
        """
        loop = _make_loop()
        event = _collect_event(loop, {"question": "Proceed?", "options": "Yes"})
        self.assertEqual(event.type, TurnEventType.USER_QUESTION)
        self.assertEqual(event.metadata["options"], ["Yes"])

    def test_object_options_with_label_key_normalized(self) -> None:
        """``[{"label": "Yes"}, {"label": "No"}]`` must yield ``["Yes", "No"]``.

        Today the loop drops every non-string element, producing ``[]``.
        The panel renders zero buttons — the literal "doesn't display the
        option" symptom the user reported.
        """
        loop = _make_loop()
        event = _collect_event(
            loop,
            {"question": "Proceed?", "options": [{"label": "Yes"}, {"label": "No"}]},
        )
        self.assertEqual(event.type, TurnEventType.USER_QUESTION)
        self.assertEqual(event.metadata["options"], ["Yes", "No"])


class TestAskUserOptionsLabelFallbacks(unittest.TestCase):
    """Object options may use ``label``, ``text``, ``value``, or ``name``."""

    def test_label_key_takes_precedence(self) -> None:
        loop = _make_loop()
        event = _collect_event(
            loop,
            {"question": "Pick", "options": [{"label": "Apple", "text": "Banana", "value": "Cherry"}]},
        )
        self.assertEqual(event.metadata["options"], ["Apple"])

    def test_text_key_used_when_no_label(self) -> None:
        loop = _make_loop()
        event = _collect_event(
            loop,
            {"question": "Pick", "options": [{"text": "Banana"}]},
        )
        self.assertEqual(event.metadata["options"], ["Banana"])

    def test_value_key_used_when_no_label_or_text(self) -> None:
        loop = _make_loop()
        event = _collect_event(
            loop,
            {"question": "Pick", "options": [{"value": "Cherry"}]},
        )
        self.assertEqual(event.metadata["options"], ["Cherry"])

    def test_name_key_used_when_no_label_text_or_value(self) -> None:
        loop = _make_loop()
        event = _collect_event(
            loop,
            {"question": "Pick", "options": [{"name": "Durian"}]},
        )
        self.assertEqual(event.metadata["options"], ["Durian"])


class TestAskUserOptionsMixedShapes(unittest.TestCase):
    """Strings and objects coexist in the same list, preserving order."""

    def test_mixed_string_and_object_preserves_order(self) -> None:
        loop = _make_loop()
        event = _collect_event(
            loop,
            {"question": "Confirm?",
             "options": ["Yes", {"label": "No"}, {"text": "Maybe"}]},
        )
        self.assertEqual(event.metadata["options"], ["Yes", "No", "Maybe"])


class TestAskUserOptionsFiltering(unittest.TestCase):
    """None, empty strings, and whitespace-only strings are dropped.

    These tests pair each filtering invariant with at least one shape
    that the old ``isinstance(o, str) and o.strip()`` filter mishandled,
    so a regression in the normalisation logic is caught here even when
    the strict-shape portion happens to already produce the right answer.
    """

    def test_none_empty_whitespace_filtered_real_kept_and_stripped(self) -> None:
        """``[None, "", "  ", "  Real  "]`` → ``["Real"]``.

        Filters None / empty / whitespace-only entries AND strips
        surrounding whitespace from the kept label. The old code used
        ``o.strip()`` as a truthy filter but did not strip the kept
        element, so the broken result was ``['  Real  ']`` — a button
        with stray padding. The fix must both filter and strip.
        """
        loop = _make_loop()
        event = _collect_event(
            loop,
            {"question": "Pick", "options": [None, "", "  ", "  Real  "]},
        )
        self.assertEqual(event.metadata["options"], ["Real"])


class TestAskUserOptionsDeduplication(unittest.TestCase):
    """Duplicate labels must dedupe case-insensitively while preserving order.

    The point of dedupe is to avoid rendering two buttons that say
    effectively the same thing — a real LLM quirk where ``yes`` and
    ``Yes`` both appear because the model lost track.
    """

    def test_case_insensitive_dedupe_preserves_first(self) -> None:
        loop = _make_loop()
        event = _collect_event(
            loop,
            {"question": "Proceed?", "options": ["Yes", "yes", "YES"]},
        )
        self.assertEqual(event.metadata["options"], ["Yes"])

    def test_duplicate_distinct_entries_dedupe_order_preserved(self) -> None:
        loop = _make_loop()
        event = _collect_event(
            loop,
            {"question": "Pick", "options": ["A", "B", "A"]},
        )
        self.assertEqual(event.metadata["options"], ["A", "B"])


class TestAskUserOptionsNoRaiseRobustness(unittest.TestCase):
    """Untrusted shapes must normalize, never raise.

    The contract is concrete: every pathological shape below must produce
    a specific normalized list (not raise, not iterate a string). Asserting
    the exact result — not just "didn't raise" — is what makes these
    regression tests catch the real bug instead of just hiding it behind
    try/except. The normalisation rules chosen for these shapes:

    * a bare int ``42`` is stringified to a single button label ``"42"``;
    * a bare dict with a known key becomes one button labelled by that key;
    * a one-level nested list ``[["Yes", "No"]]`` is flattened so the
      user gets two buttons instead of zero.
    """

    def test_int_options_stringifies_to_single_button(self) -> None:
        """``options=42`` must become ``["42"]`` — one button, not zero."""
        loop = _make_loop()
        event = _collect_event(loop, {"question": "Pick", "options": 42})
        self.assertEqual(event.metadata["options"], ["42"])

    def test_nested_list_options_flattens_one_level(self) -> None:
        """``options=[["Yes", "No"]]`` must flatten to ``["Yes", "No"]``.

        Some tool-call serializers emit a one-level nested list when the
        model wraps its choices. Flattening saves the user from seeing
        no buttons at all.
        """
        loop = _make_loop()
        event = _collect_event(
            loop,
            {"question": "Pick", "options": [["Yes", "No"]]},
        )
        self.assertEqual(event.metadata["options"], ["Yes", "No"])

    def test_bare_dict_options_with_label_key_stringifies(self) -> None:
        """``options={"label": "Yes"}`` must yield ``["Yes"]``."""
        loop = _make_loop()
        event = _collect_event(
            loop,
            {"question": "Pick", "options": {"label": "Yes"}},
        )
        self.assertEqual(event.metadata["options"], ["Yes"])

    def test_very_long_string_options_preserved_as_single_choice(self) -> None:
        """A long string is one option, not 1000 single-character buttons.

        Today: 1000 buttons, panel unusable. After fix: one button,
        one click. This pins the exact case-by-case regression in
        observable form.
        """
        loop = _make_loop()
        long_label = "A" * 256
        event = _collect_event(
            loop,
            {"question": "Pick", "options": long_label},
        )
        self.assertEqual(event.metadata["options"], [long_label])


class TestAskUserOptionsWhitespaceStripping(unittest.TestCase):
    """Labels arrive with surrounding whitespace; strip before rendering."""

    def test_string_option_surrounding_whitespace_stripped(self) -> None:
        loop = _make_loop()
        event = _collect_event(
            loop,
            {"question": "Pick", "options": ["  Yes  "]},
        )
        self.assertEqual(event.metadata["options"], ["Yes"])

    def test_object_option_label_whitespace_stripped(self) -> None:
        loop = _make_loop()
        event = _collect_event(
            loop,
            {"question": "Pick", "options": [{"label": "  Yes  "}]},
        )
        self.assertEqual(event.metadata["options"], ["Yes"])


class TestAskUserOptionsOpenQuestionContract(unittest.TestCase):
    """``options=None`` must stay ``[]`` — that keeps the text input unlocked.

    ``UserQuestionWidget`` uses ``bool(options)`` to decide whether to lock
    the text input. Returning ``[]`` (not ``None``, not a non-empty
    falsy container) is the explicit contract other code depends on.

    The old implementation did ``for o in raw_options`` directly, so
    ``options=None`` raised ``TypeError: 'NoneType' object is not iterable``
    inside the generator and the user never saw a question event at all.
    """

    def test_options_none_yields_empty_list(self) -> None:
        loop = _make_loop()
        event = _collect_event(loop, {"question": "Thoughts?", "options": None})
        self.assertEqual(event.metadata["options"], [])


if __name__ == "__main__":
    unittest.main()