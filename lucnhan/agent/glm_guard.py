"""Bounded GLM reasoning degeneration guard.

Some GLM-family reasoning models occasionally enter a degenerate loop in
which the chain-of-thought balloons with the same handful of
"outputting <tool> tool now"-style sentences, never finishing the
reasoning block and never emitting a tool call.  This module implements
a pure, isolated detector that watches the reasoning/visible deltas of a
single agent turn and reports whether the loop has crossed the line.

Two orthogonal abort paths exist:

1. **Hard ceiling** — estimated reasoning tokens >= the configured
   ceiling (default 16,384 tokens, where the token estimate uses
   ``(bytes + 2) // 3`` over the cumulative UTF-8 byte count of
   reasoning).
2. **Repetition + meta intent** — at least 60 % of the segments in the
   most recent ``min(64, total)`` evaluation window share a fingerprint
   *and* at least 8 of those segments mention both an action term and
   a tool term.

A visible answer (>= ``VISIBLE_DISABLE_THRESHOLD`` non-whitespace chars)
or a tool call start suppresses the repetition path:

* Once visible text has crossed the threshold, repetition is no longer
  a degeneration signal — the model is now answering, not stuck
  reasoning.
* Once a tool call starts, the model has committed; further reasoning
  is irrelevant and the guard must not interfere.

The detector formula is therefore:

    abort = T AND (H OR (R AND M AND V))

where ``V`` is the *absence* of visible text beyond
:data:`VISIBLE_DISABLE_THRESHOLD`.

All state is bounded: the partial-line buffer is flushed in
:data:`MAX_SEGMENT_CHARS` chunks even without a newline so the buffer
never exceeds that size, plus at most 128 normalized segments are
retained.  This guarantees O(1) memory regardless of how long the
reasoning stream runs.
"""

from __future__ import annotations

import math
import re
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Hard ceiling on estimated reasoning tokens per turn.  Picked so the
#: first byte to cross 16,384 tokens lands on exactly 49,150 UTF-8
#: bytes (``(49_150 + 2) // 3 == 16_384``).
DEFAULT_CEILING_TOKENS: int = 16_384

#: Number of non-whitespace visible characters at which the repetition
#: path is suppressed.  Below this the model is still considered to be
#: in pure reasoning mode.
VISIBLE_DISABLE_THRESHOLD: int = 256

#: Minimum count of meta-intent segments in the evaluation window
#: before the repetition path can fire.
META_INTENT_MIN_COUNT: int = 8

#: Maximum normalized segment count retained in the ring buffer.
SEGMENT_BUFFER_CAPACITY: int = 128

#: Minimum segment count before repetition is evaluated.  Below this we
#: cannot distinguish a degenerate loop from a normal multi-step plan.
REPETITION_EVAL_THRESHOLD: int = 32

#: Maximum window size used for repetition evaluation.
REPETITION_WINDOW_CAP: int = 64

#: Repetition ratio (out of 1.0) required to trigger the repetition
#: path.  Combined with the segment cap above this yields a threshold
#: of ``ceil(0.60 * 64) = 39`` segments in a full window.
REPETITION_RATIO: float = 0.60

#: Maximum length (raw characters) of any single normalized segment.
#: Longer lines — and the partial-line buffer with no newline — are
#: split into chunks of this size before normalization.
MAX_SEGMENT_CHARS: int = 240

#: Trigger name returned for the hard-ceiling abort path.
TRIGGER_CEILING: str = "reasoning_ceiling"

#: Trigger name returned for the repetition + meta-intent abort path.
TRIGGER_REPETITION: str = "repetition_meta_intent"

#: Action verbs that, combined with a tool term, indicate the model is
#: *narrating* an upcoming tool call (the degeneration pattern we want
#: to catch).  Lowercased via ``casefold`` on the segment side.
_ACTION_TERMS: frozenset[str] = frozenset(
    {
        "call",
        "calling",
        "execute",
        "executing",
        "emit",
        "output",
        "outputting",
    }
)

#: Generic "tool" / "function" vocabulary.  When a segment contains one
#: of these words AND an action term, it counts as meta-intent even if
#: it does not name an exposed tool (e.g. "calling the tool now").
_TOOL_TERMS: frozenset[str] = frozenset(
    {
        "tool",
        "invoke",
        "function",
        "parallel",
    }
)

#: Regex used to collapse runs of punctuation into a single character.
#: Word characters (``\\w``) and whitespace are preserved so that tool
#: names (``read_bytes``), numbers, and addresses (``0x401000``) survive
#: the normalization round-trip.
_PUNCT_RUN_RE: re.Pattern[str] = re.compile(r"([^\w\s])\1+")

#: Regex used to collapse runs of whitespace into a single space.
_WS_RUN_RE: re.Pattern[str] = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GLMGuardSnapshot:
    """Read-only view of the guard's current state.

    Attributes
    ----------
    estimated_reasoning_tokens:
        Token estimate using ``(bytes + 2) // 3`` (rounds up at the
        boundary).
    reasoning_utf8_bytes:
        Cumulative UTF-8 byte count of all reasoning deltas.
    visible_non_whitespace_chars:
        Cumulative count of non-whitespace characters seen in visible
        (assistant content) deltas.
    total_segments:
        Lifetime count of normalized segments emitted (monotonic; not
        bounded by ``SEGMENT_BUFFER_CAPACITY``).
    repeated_positions:
        Number of segment positions in the current evaluation window
        whose fingerprint appears more than once.
    meta_intent_segments:
        Number of segments in the evaluation window that contain both
        an action term and a tool term.
    repetition_ratio_millis:
        ``repeated_positions / window_size * 1000`` as an integer.
        ``0`` if no evaluation has occurred yet.
    trigger:
        Empty string if the guard has not aborted; otherwise one of
        :data:`TRIGGER_CEILING` or :data:`TRIGGER_REPETITION`.
    """

    estimated_reasoning_tokens: int
    reasoning_utf8_bytes: int
    visible_non_whitespace_chars: int
    total_segments: int
    repeated_positions: int
    meta_intent_segments: int
    repetition_ratio_millis: int
    trigger: str


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


class GLMReasoningGuard:
    """Pure, isolated detector for GLM reasoning degeneration.

    The detector is intentionally host-agnostic: it consumes raw delta
    strings (``on_reasoning_delta`` / ``on_visible_delta``) plus a
    single tool-call-start notification (``on_tool_call_start``) and
    answers ``should_abort()``.  No I/O, no globals, no threads.

    Parameters
    ----------
    exposed_tool_names:
        Iterable of tool names exposed to the model.  Each entry is
        case-folded once at construction time and matched against
        normalized segments to count as a "tool term".
    ceiling_tokens:
        Hard ceiling on **estimated tokens** (``(bytes + 2) // 3``).
        Defaults to :data:`DEFAULT_CEILING_TOKENS` (16,384), which
        yields the 49,150-byte boundary targeted by the spec.  Wire
        this from ``GLMGuardConfig.ceiling_tokens`` when integrating
        with the loop.
    """

    __slots__ = (
        "_buffer",
        "_bytes",
        "_ceiling_tokens",
        "_last_meta",
        "_last_ratio_millis",
        "_last_repeated",
        "_last_window_size",
        "_max_segment_chars",
        "_segments",
        "_tool_names_cf",
        "_tool_started",
        "_total_segments",
        "_trigger",
        "_visible_nw",
    )

    def __init__(
        self,
        exposed_tool_names: Iterable[str],
        ceiling_tokens: int = DEFAULT_CEILING_TOKENS,
    ) -> None:
        if ceiling_tokens <= 0:
            raise ValueError(f"ceiling_tokens must be positive, got {ceiling_tokens}")
        self._ceiling_tokens: int = ceiling_tokens
        # Case-fold tool names once so we can do cheap substring lookups
        # against the already-normalized segment text.
        self._tool_names_cf: tuple[str, ...] = tuple(n.casefold() for n in exposed_tool_names if n)

        # Streaming state.
        self._bytes: int = 0
        self._visible_nw: int = 0
        self._tool_started: bool = False
        self._buffer: str = ""
        self._segments: deque[str] = deque(maxlen=SEGMENT_BUFFER_CAPACITY)
        self._total_segments: int = 0
        self._max_segment_chars: int = 0

        # Latched trigger — once set, stays set for the lifetime of the
        # guard.  Prevents a flapping decision.
        self._trigger: str = ""

        # Last-evaluation metrics (for the snapshot).
        self._last_window_size: int = 0
        self._last_repeated: int = 0
        self._last_meta: int = 0
        self._last_ratio_millis: int = 0

    # ------------------------------------------------------------------
    # Public properties (read-only diagnostics)
    # ------------------------------------------------------------------

    @property
    def trigger(self) -> str:
        """The latched trigger string (empty if the guard has not aborted)."""
        return self._trigger

    @property
    def retained_segment_count(self) -> int:
        """Number of normalized segments currently in the ring buffer.

        Always ``<= SEGMENT_BUFFER_CAPACITY``.  Exposed for the
        bounded-state test.
        """
        return len(self._segments)

    @property
    def max_retained_segment_chars(self) -> int:
        """Length (in characters) of the longest retained normalized segment.

        Always ``<= MAX_SEGMENT_CHARS``.  Exposed for the bounded-state
        test.
        """
        return self._max_segment_chars

    @property
    def partial_buffer_chars(self) -> int:
        """Number of characters currently buffered without a newline.

        Always ``< MAX_SEGMENT_CHARS``: the buffer is flushed in
        :data:`MAX_SEGMENT_CHARS`-sized chunks even without a newline.
        Exposed for the no-newline stress test.
        """
        return len(self._buffer)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def on_reasoning_delta(self, text: str) -> None:
        """Ingest a chunk of model reasoning text.

        Performs byte counting, partial-buffer flushing (with or without
        newlines), segment splitting, normalization, and (re-)evaluates
        the abort decision.
        """
        if not text:
            return

        # Increment UTF-8 byte count *before* splitting so even an
        # oversized chunk that we drop on the floor still trips the
        # hard ceiling.
        self._bytes += len(text.encode("utf-8"))

        # Append to the partial-line buffer and immediately flush any
        # completed lines / oversized chunks so the buffer stays
        # bounded regardless of whether newlines ever appear.
        self._buffer += text
        self._flush_buffer()

        # Re-evaluate on every delta so the trigger latches as soon as
        # we cross the line, not on the next event.
        self._maybe_latch_trigger()

    def on_visible_delta(self, text: str) -> None:
        """Ingest a chunk of *visible* (assistant content) text.

        Increments the non-whitespace visible character counter.  Once
        the counter reaches :data:`VISIBLE_DISABLE_THRESHOLD`, the
        repetition path is suppressed (the model is no longer stuck —
        it is answering).  Does not affect the hard ceiling, which
        remains a hard backstop.
        """
        if not text:
            return
        self._visible_nw += sum(1 for ch in text if not ch.isspace())
        # A visible delta may also flip the trigger latching: if the
        # ceiling was already crossed but V has just become False, the
        # ceiling path is unaffected; if R is the current pending path
        # it now cannot fire.
        self._maybe_latch_trigger()

    def on_tool_call_start(self) -> None:
        """Notify the guard that the model has started a tool call.

        Once a tool call begins, the model has committed — further
        reasoning is no longer a degeneration concern.  This suppresses
        *both* abort paths.
        """
        self._tool_started = True
        # No trigger latching needed: T=False guarantees no abort
        # regardless of H, R, M, V.  Still call the evaluator so the
        # snapshot reflects the latest metrics.
        self._maybe_latch_trigger()

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------

    def should_abort(self) -> bool:
        """Return ``True`` iff the guard has latched an abort trigger.

        Latching is one-way: once ``True``, always ``True`` for the
        lifetime of the guard (caller should construct a fresh guard
        per turn).
        """
        return self._trigger != ""

    def snapshot(self) -> GLMGuardSnapshot:
        """Return a frozen :class:`GLMGuardSnapshot` of the current state."""
        return GLMGuardSnapshot(
            estimated_reasoning_tokens=(self._bytes + 2) // 3,
            reasoning_utf8_bytes=self._bytes,
            visible_non_whitespace_chars=self._visible_nw,
            total_segments=self._total_segments,
            repeated_positions=self._last_repeated,
            meta_intent_segments=self._last_meta,
            repetition_ratio_millis=self._last_ratio_millis,
            trigger=self._trigger,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _flush_buffer(self) -> None:
        """Drain the partial-line buffer into normalized segments.

        Two flush rules, in order, keep the buffer bounded:

        1. Newline-terminated prefixes become completed "lines" and
           pass through :meth:`_ingest_line` (which itself chunks at
           :data:`MAX_SEGMENT_CHARS`).
        2. Any residual buffer (no newline) of length >=
           :data:`MAX_SEGMENT_CHARS` is split off in 240-char chunks
           so a single huge no-newline delta cannot blow up the
           buffer.
        """
        # Rule 1: flush completed lines.
        while True:
            nl_pos = self._buffer.find("\n")
            if nl_pos < 0:
                break
            line = self._buffer[:nl_pos]
            self._buffer = self._buffer[nl_pos + 1 :]
            self._ingest_line(line)

        # Rule 2: flush 240-char prefixes with no newline so the buffer
        # never grows past ``MAX_SEGMENT_CHARS``.
        while len(self._buffer) >= MAX_SEGMENT_CHARS:
            chunk = self._buffer[:MAX_SEGMENT_CHARS]
            self._buffer = self._buffer[MAX_SEGMENT_CHARS:]
            self._ingest_line(chunk)

    def _ingest_line(self, line: str) -> None:
        """Normalize one logical line (or 240-char chunk), splitting if needed."""
        if not line:
            return  # empty line yields no segment
        # Split into raw 240-character chunks BEFORE normalization so
        # the cap holds even if normalization would shrink the string.
        for start in range(0, len(line), MAX_SEGMENT_CHARS):
            chunk = line[start : start + MAX_SEGMENT_CHARS]
            normalized = self._normalize(chunk)
            if not normalized:
                continue
            self._segments.append(normalized)
            self._total_segments += 1
            if len(normalized) > self._max_segment_chars:
                self._max_segment_chars = len(normalized)

    def _normalize(self, text: str) -> str:
        r"""Apply casefold + whitespace/punctuation collapse.

        Tool names (``read_bytes``), numbers, and addresses
        (``0x401000``) survive: ``\w`` matches letters, digits, and
        underscore; only the surrounding whitespace and repeated
        non-word punctuation are touched.
        """
        cf = text.casefold()
        cf = _WS_RUN_RE.sub(" ", cf)
        cf = _PUNCT_RUN_RE.sub(r"\1", cf)
        return cf.strip()

    def _maybe_latch_trigger(self) -> None:
        """Recompute the abort decision and latch the trigger if it fires."""
        if self._trigger:
            return  # already latched

        # T = reasoning still active.
        if self._tool_started:
            return  # T = False → no abort possible.

        # H = hard ceiling crossed (regardless of V).
        estimated_tokens = (self._bytes + 2) // 3
        if estimated_tokens >= self._ceiling_tokens:
            self._trigger = TRIGGER_CEILING
            return

        # V = visible answer has NOT yet crossed the disable threshold.
        v_open = self._visible_nw < VISIBLE_DISABLE_THRESHOLD

        # R, M, V path — only evaluate once enough segments have arrived
        # to distinguish a real loop from a normal multi-step plan.
        total = self._total_segments
        if total < REPETITION_EVAL_THRESHOLD:
            return  # not enough signal yet

        window_size = min(REPETITION_WINDOW_CAP, total)
        # Take the last ``window_size`` segments out of the ring.
        # ``list(self._segments)[-window_size:]`` is O(window_size) but
        # window_size is bounded (≤ 64), so this is O(1) in practice.
        window = list(self._segments)[-window_size:]
        freq: Counter[str] = Counter(window)

        repeated = sum(1 for seg in window if freq[seg] >= 2)
        meta = sum(1 for seg in window if self._is_meta_intent(seg))

        threshold = math.ceil(REPETITION_RATIO * window_size)
        ratio_millis = (repeated * 1000) // window_size if window_size else 0

        # Stash for snapshot.
        self._last_window_size = window_size
        self._last_repeated = repeated
        self._last_meta = meta
        self._last_ratio_millis = ratio_millis

        if v_open and repeated >= threshold and meta >= META_INTENT_MIN_COUNT:
            self._trigger = TRIGGER_REPETITION

    def _is_meta_intent(self, segment: str) -> bool:
        """Return True iff the segment contains BOTH an action and a tool term.

        "Tool term" = an exposed tool name (case-folded) OR a generic
        tool/function vocabulary word from :data:`_TOOL_TERMS`.  "Action
        term" comes from :data:`_ACTION_TERMS`.
        """
        has_action = False
        for term in _ACTION_TERMS:
            if term in segment:
                has_action = True
                break
        if not has_action:
            return False

        for name in self._tool_names_cf:
            if name in segment:
                return True
        for term in _TOOL_TERMS:
            if term in segment:
                return True
        return False
