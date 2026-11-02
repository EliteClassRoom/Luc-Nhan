"""Bounded streaming loop guard.

A reasoning model can loop at the *stream* level: the chain-of-thought
balloons, repeats itself, or degenerates into a stream of headings
without ever emitting a tool call or a visible answer.  This module
implements a pure, isolated detector that watches the reasoning/visible
deltas of a single agent turn and reports whether the loop has crossed
the line.

The detector is profile-driven.  Each profile enables a subset of the
independent detectors below; a profile with every detector disabled is
inert.

1. **Hard ceiling** — estimated reasoning tokens >= the configured
   ceiling (default 16,384 tokens, where the token estimate uses
   ``(bytes + 2) // 3`` over the cumulative UTF-8 byte count of
   reasoning).
2. **Repetition + meta intent** — at least 60 % of the segments in the
   most recent ``min(64, total)`` evaluation window share a fingerprint
   *and* at least 8 of those segments mention both an action term and
   a tool term.  GLM-family specific: the "narrating a tool call"
   degeneration.
3. **Verbatim tail** — the last 180 characters of the reasoning tail
   already occurred earlier inside the 4,096-character tail window.
4. **Trigram similarity** — consecutive normalized segments in the last
   16-segment window are near-duplicates (Jaccard similarity of their
   trigram sets >= 0.8) for at least 80 % of the adjacent pairs.
5. **Progress stall** — the last 8 segments contribute almost no new
   trigrams (novelty <= 0.2) and contain no concrete anchor (hex
   address, call expression, or source path), i.e. the model is
   elaborating without moving.
6. **Header runaway** — 24 or more titled lines (``## heading`` or
   ``**bold heading**``) with no tool call.
7. **Visible verbatim tail** — the last 180 characters of the *visible*
   tail occur at least three times inside the 4,096-character visible
   window: the answer channel itself is looping.
8. **Visible trigram similarity** — the last 16 visible lines are
   near-duplicates (Jaccard similarity of their trigram sets >= 0.9)
   for at least 90 % of the adjacent pairs.
9. **Visible ceiling** — estimated visible tokens >= the configured
   visible ceiling (default 32,768 tokens).

Text that arrives on the provider's *text* channel is routed with
:meth:`StreamLoopGuard.on_text_delta`: a literal ``<think>...</think>``
span counts as reasoning, everything else counts as visible answer
text.  A tool call start suppresses every detector.

A visible answer (>= ``VISIBLE_DISABLE_THRESHOLD`` non-whitespace
chars) suppresses only the meta-intent path: once visible text has
crossed the threshold the model is answering, so the "narrating a tool
call" degeneration no longer applies.  The generic repetition
detectors and both ceilings keep running — a model can loop in the
answer channel just as easily as in the reasoning channel.

All state is bounded: the partial-line buffer is flushed in
:data:`MAX_SEGMENT_CHARS` chunks even without a newline so the buffer
never exceeds that size, plus at most 128 normalized segments are
retained, plus a 4,096-character verbatim tail, plus a 4,096-character
visible tail.  This guarantees O(1) memory regardless of how long the
reasoning stream runs.
"""

from __future__ import annotations

import math
import re
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise

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

#: Trigger name returned when the reasoning tail repeats verbatim.
TRIGGER_VERBATIM: str = "verbatim_tail"

#: Trigger name returned when consecutive segments are near-duplicates.
TRIGGER_TRIGRAM: str = "trigram_jaccard"

#: Trigger name returned when the segment window stops making progress.
TRIGGER_STALL: str = "progress_stall"

#: Trigger name returned when the model emits a runaway stream of
#: titled lines without committing to a tool call.
TRIGGER_HEADER: str = "header_runaway"

#: Trigger name returned when the *visible* (answer) tail repeats
#: verbatim.  Catches a model that loops in the channel the user reads.
TRIGGER_VISIBLE_VERBATIM: str = "visible_verbatim_tail"

#: Trigger name returned when consecutive visible lines are
#: near-duplicates.
TRIGGER_VISIBLE_TRIGRAM: str = "visible_trigram_jaccard"

#: Trigger name returned when the visible channel blows through its own
#: token ceiling.
TRIGGER_VISIBLE_CEILING: str = "visible_ceiling"

#: Character window of the reasoning tail inspected by the verbatim
#: detector.
EXACT_TAIL_WINDOW: int = 4096

#: Minimum length of the repeated block compared by the verbatim
#: detector.
EXACT_REPEAT_CHARS: int = 180

#: Number of trailing segments inspected by the trigram detector.
TRIGRAM_WINDOW: int = 16

#: Minimum Jaccard similarity of the trigram sets of two adjacent
#: segments for the pair to count as a near-duplicate.
TRIGRAM_JACCARD: float = 0.8

#: Minimum fraction of adjacent pairs that must be near-duplicates for
#: the trigram detector to fire.
TRIGRAM_PAIR_RATIO: float = 0.8

#: Number of trailing segments inspected by the progress-stall
#: detector.
STALL_WINDOW: int = 8

#: Maximum share of new trigrams a segment may contribute and still
#: count as "no progress".
STALL_NOVELTY: float = 0.2

#: Number of titled lines after which the header-runaway detector
#: fires.
HEADER_RUNAWAY_THRESHOLD: int = 24

#: Hard ceiling on estimated *visible* tokens per turn.  A backstop for
#: runs where the visible channel never settles into a recognisable
#: repeat pattern (e.g. a slowly drifting paraphrase loop).
DEFAULT_VISIBLE_CEILING_TOKENS: int = 32_768

#: Minimum number of occurrences of the repeated visible block that
#: makes the visible verbatim detector fire.
VISIBLE_EXACT_MIN_OCCURRENCES: int = 3

#: Number of trailing visible lines inspected by the visible trigram
#: detector.
VISIBLE_TRIGRAM_WINDOW: int = 16

#: Minimum Jaccard similarity of the trigram sets of two adjacent visible
#: lines for the pair to count as a near-duplicate.  Deliberately strict:
#: legitimate verbose output (enumerated steps that share a template)
#: must not be mistaken for a loop.
VISIBLE_TRIGRAM_JACCARD: float = 0.9

#: Minimum fraction of adjacent visible pairs that must be near-duplicates
#: for the visible trigram detector to fire.
VISIBLE_TRIGRAM_PAIR_RATIO: float = 0.9

#: Minimum number of new visible characters before the visible trigram
#: detector re-evaluates.  Keeps the detector O(1) per character.
VISIBLE_TRIGRAM_MIN_GROWTH: int = 256

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

#: A "titled line": a markdown heading or a standalone bold caption.
_HEADER_LINE_RE: re.Pattern[str] = re.compile(r"^\s*(?:#{1,6}\s+\S|\*\*[^*]{1,80}\*\*\s*:?\s*$)")

#: A concrete anchor: a hex address, a call expression, or a source
#: path.  Their presence means the model is pointing at something
#: specific, i.e. it is making progress even if the prose repeats.
_ANCHOR_RE: re.Pattern[str] = re.compile(r"0x[0-9a-fA-F]+|\b\w+\s*\(|[\w./\\-]+\.(?:c|cc|cpp|h|hpp|py|rs|go|js|ts)\b")

#: Tag routing: a literal ``<think>`` span inside provider *text* is
#: reasoning, not user-visible answer text.  Matching is
#: case-insensitive; the emitted text keeps its original case.  A regex
#: is used (instead of ``casefold().find()``) because ``casefold`` can
#: change the string length for non-ASCII input, which would misalign
#: the index into the pending buffer.
_THINK_OPEN_RE: re.Pattern[str] = re.compile(r"<think>", re.IGNORECASE)
_THINK_CLOSE_RE: re.Pattern[str] = re.compile(r"</think>", re.IGNORECASE)


def _partial_tag_suffix(text: str, tag: str) -> str:
    """Return the trailing fragment of *text* that may start *tag*.

    A streamed ``<think>`` can arrive split across deltas ("<thi" then
    "nk>").  The fragment is held back until the rest of the tag shows
    up, so a literal tag is never leaked into the visible channel.
    """
    limit = min(len(text), len(tag) - 1)
    for n in range(limit, 0, -1):
        if text[-n:].casefold() == tag[:n].casefold():
            return text[-n:]
    return ""


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoopGuardProfile:
    """Which detectors a :class:`StreamLoopGuard` runs.

    Attributes
    ----------
    ceiling:
        Hard ceiling on estimated reasoning tokens.
    meta_intent:
        Repetition + meta-intent detector (GLM "narrating a tool call"
        degeneration).
    verbatim_tail:
        Verbatim repeated-tail detector.
    trigram_jaccard:
        Near-duplicate segment detector.
    progress_stall:
        No-new-information detector.
    header_runaway:
        Runaway titled-line detector.
    visible_verbatim:
        Verbatim repeated-tail detector for the visible channel.
    visible_trigram:
        Near-duplicate line detector for the visible channel.
    visible_ceiling:
        Hard ceiling on estimated visible tokens.
    """

    ceiling: bool = True
    meta_intent: bool = True
    verbatim_tail: bool = False
    trigram_jaccard: bool = False
    progress_stall: bool = False
    header_runaway: bool = False
    visible_verbatim: bool = False
    visible_trigram: bool = False
    visible_ceiling: bool = False

    @property
    def enabled(self) -> bool:
        """True when at least one detector is active."""
        return any(
            (
                self.ceiling,
                self.meta_intent,
                self.verbatim_tail,
                self.trigram_jaccard,
                self.progress_stall,
                self.header_runaway,
                self.visible_verbatim,
                self.visible_trigram,
                self.visible_ceiling,
            )
        )


#: Every detector disabled.
_PROFILE_OFF = LoopGuardProfile(False, False)

#: GLM parity: ceiling + the meta-intent repetition detector + the
#: generic visible-channel detectors.
_PROFILE_GLM = LoopGuardProfile(
    ceiling=True,
    meta_intent=True,
    verbatim_tail=True,
    visible_verbatim=True,
    visible_trigram=True,
    visible_ceiling=True,
)

#: Default for unlisted model families: ceiling + the generic detectors
#: that need no model-specific tuning.
_PROFILE_DEFAULT = LoopGuardProfile(
    ceiling=True,
    meta_intent=False,
    verbatim_tail=True,
    progress_stall=True,
    visible_verbatim=True,
    visible_trigram=True,
    visible_ceiling=True,
)

#: Every detector.
_PROFILE_FULL = LoopGuardProfile(
    ceiling=True,
    meta_intent=True,
    verbatim_tail=True,
    trigram_jaccard=True,
    progress_stall=True,
    header_runaway=True,
    visible_verbatim=True,
    visible_trigram=True,
    visible_ceiling=True,
)

#: Model families known to need the full detector set.
_FAMILY_FULL: tuple[str, ...] = ("gemini", "deepseek", "grok", "qwen", "kimi", "minimax")


def resolve_loop_guard_profile(provider_name: str, model_id: str, setting: str) -> LoopGuardProfile:
    """Resolve the detector profile for a provider/model pair.

    *setting* is one of ``"auto"`` (family-based), ``"full"``, ``"glm"``
    or ``"off"``.  ``"auto"`` maps GLM-family models to the GLM parity
    profile, the families in :data:`_FAMILY_FULL` to the full profile,
    and everything else to the generic default.
    """
    if setting == "off":
        return _PROFILE_OFF
    if setting == "glm":
        return _PROFILE_GLM
    if setting == "full":
        return _PROFILE_FULL
    tag = f"{provider_name} {model_id}".casefold()
    if "glm" in tag:
        return _PROFILE_GLM
    if any(family in tag for family in _FAMILY_FULL):
        return _PROFILE_FULL
    return _PROFILE_DEFAULT


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoopGuardSnapshot:
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
        Empty string if the guard has not aborted; otherwise the name
        of the detector that fired.
    verbatim_tail_repeats:
        Number of times the verbatim-tail detector observed a repeated
        block.
    trigram_pairs_matched:
        Number of adjacent segment pairs in the trigram window that are
        near-duplicates.
    stall_segments:
        Number of trailing segments that contributed no new
        information.
    header_lines:
        Number of titled lines observed in the reasoning stream.
    estimated_visible_tokens:
        Token estimate of the visible channel using ``(bytes + 2) // 3``.
    visible_utf8_bytes:
        Cumulative UTF-8 byte count of all visible deltas.
    visible_tail_chars:
        Number of characters retained in the bounded visible tail window.
    visible_verbatim_repeats:
        Number of times the visible verbatim detector observed a repeated
        block.
    visible_trigram_pairs:
        Number of adjacent visible line pairs in the trigram window that
        are near-duplicates.
    """

    estimated_reasoning_tokens: int
    reasoning_utf8_bytes: int
    visible_non_whitespace_chars: int
    total_segments: int
    repeated_positions: int
    meta_intent_segments: int
    repetition_ratio_millis: int
    trigger: str
    verbatim_tail_repeats: int = 0
    trigram_pairs_matched: int = 0
    stall_segments: int = 0
    header_lines: int = 0
    estimated_visible_tokens: int = 0
    visible_utf8_bytes: int = 0
    visible_tail_chars: int = 0
    visible_verbatim_repeats: int = 0
    visible_trigram_pairs: int = 0


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


class StreamLoopGuard:
    """Pure, isolated detector for streaming reasoning degeneration.

    The detector is intentionally host-agnostic: it consumes raw delta
    strings (``on_reasoning_delta`` / ``on_visible_delta`` /
    ``on_text_delta``) plus a single tool-call-start notification
    (``on_tool_call_start``) and answers ``should_abort()``.  No I/O,
    no globals, no threads.

    ``on_text_delta`` is the entry point for providers that deliver the
    answer as *text*: it routes literal ``<think>...</think>`` spans to
    the reasoning channel and everything else to the visible channel.
    ``flush()`` must be called at the end of the stream so a trailing
    partial tag is not lost.

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
    profile:
        Detector profile.  Defaults to the GLM parity profile so a
        bare ``StreamLoopGuard(...)`` keeps the historical GLM
        behaviour.
    visible_ceiling_tokens:
        Hard ceiling on **estimated visible tokens**
        (``(bytes + 2) // 3``).  Defaults to
        :data:`DEFAULT_VISIBLE_CEILING_TOKENS` (32,768).
    """

    __slots__ = (
        "_buffer",
        "_bytes",
        "_ceiling_tokens",
        "_header_lines",
        "_last_meta",
        "_last_ratio_millis",
        "_last_repeated",
        "_last_stall_segments",
        "_last_trigram_pairs",
        "_last_visible_trigram_pairs",
        "_last_window_size",
        "_max_segment_chars",
        "_profile",
        "_segments",
        "_tail",
        "_tail_growth",
        "_think_mode",
        "_think_pending",
        "_tool_names_cf",
        "_tool_started",
        "_total_segments",
        "_trigger",
        "_verbatim_repeats",
        "_visible_bytes",
        "_visible_ceiling_tokens",
        "_visible_nw",
        "_visible_tail",
        "_visible_tail_growth",
        "_visible_trigram_growth",
        "_visible_verbatim_repeats",
    )

    def __init__(
        self,
        exposed_tool_names: Iterable[str],
        ceiling_tokens: int = DEFAULT_CEILING_TOKENS,
        profile: LoopGuardProfile | None = None,
        visible_ceiling_tokens: int = DEFAULT_VISIBLE_CEILING_TOKENS,
    ) -> None:
        if ceiling_tokens <= 0:
            raise ValueError(f"ceiling_tokens must be positive, got {ceiling_tokens}")
        if visible_ceiling_tokens <= 0:
            raise ValueError(f"visible_ceiling_tokens must be positive, got {visible_ceiling_tokens}")
        self._ceiling_tokens: int = ceiling_tokens
        self._visible_ceiling_tokens: int = visible_ceiling_tokens
        self._profile: LoopGuardProfile = profile or _PROFILE_GLM
        # Case-fold tool names once so we can do cheap substring lookups
        # against the already-normalized segment text.
        self._tool_names_cf: tuple[str, ...] = tuple(n.casefold() for n in exposed_tool_names if n)

        # Streaming state.
        self._bytes: int = 0
        self._visible_nw: int = 0
        self._visible_bytes: int = 0
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

        # Detector-local state.
        self._tail: str = ""
        self._tail_growth: int = 0
        self._verbatim_repeats: int = 0
        self._header_lines: int = 0
        self._last_trigram_pairs: int = 0
        self._last_stall_segments: int = 0

        # Visible-channel detector state.
        self._visible_tail: str = ""
        self._visible_tail_growth: int = 0
        self._visible_trigram_growth: int = 0
        self._visible_verbatim_repeats: int = 0
        self._last_visible_trigram_pairs: int = 0

        # Inline-``<think>`` routing state.
        self._think_mode: bool = False
        self._think_pending: str = ""

    # ------------------------------------------------------------------
    # Public properties (read-only diagnostics)
    # ------------------------------------------------------------------

    @property
    def trigger(self) -> str:
        """The latched trigger string (empty if the guard has not aborted)."""
        return self._trigger

    @property
    def profile(self) -> LoopGuardProfile:
        """The active detector profile."""
        return self._profile

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

    @property
    def visible_tail_chars(self) -> int:
        """Number of characters retained in the bounded visible tail.

        Always ``<= EXACT_TAIL_WINDOW``.  Exposed for the bounded-state
        test.
        """
        return len(self._visible_tail)

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

        # Verbatim-tail bookkeeping: keep a bounded window of the most
        # recent reasoning text so an exactly repeated block can be
        # spotted without retaining the whole stream.
        if self._profile.verbatim_tail:
            self._tail = (self._tail + text)[-EXACT_TAIL_WINDOW:]
            self._tail_growth += len(text)

        # Re-evaluate on every delta so the trigger latches as soon as
        # we cross the line, not on the next event.
        self._maybe_latch_trigger()

    def on_visible_delta(self, text: str) -> None:
        """Ingest a chunk of *visible* (assistant content) text.

        Counts the non-whitespace characters and the UTF-8 bytes, and
        keeps a bounded tail window so the visible-channel detectors
        (verbatim repeat, near-duplicate lines, visible ceiling) can
        spot a loop in the channel the user actually reads.

        Reaching :data:`VISIBLE_DISABLE_THRESHOLD` non-whitespace
        characters only suppresses the *meta-intent* path (the
        "narrating a tool call" degeneration).  The generic visible
        detectors and the visible ceiling keep running regardless.
        """
        if not text:
            return
        self._visible_nw += sum(1 for ch in text if not ch.isspace())
        self._visible_bytes += len(text.encode("utf-8"))
        if self._profile.visible_verbatim or self._profile.visible_trigram:
            # Bounded window: the tail never exceeds EXACT_TAIL_WINDOW.
            self._visible_tail = (self._visible_tail + text)[-EXACT_TAIL_WINDOW:]
            self._visible_tail_growth += len(text)
            self._visible_trigram_growth += len(text)
        self._maybe_latch_trigger()

    def on_text_delta(self, text: str) -> None:
        """Ingest a chunk of provider *text* (assistant content).

        Providers that do not own a reasoning channel inline their
        thinking trace into the text channel as ``<think>...</think>``.
        This method routes such spans to the reasoning channel so the
        loop detectors see the reasoning where they belong, and the
        visible channel only carries the actual answer.
        """
        if not text:
            return
        self._think_pending += text
        self._drain_think_pending(final=False)

    def flush(self) -> None:
        """Flush the pending text buffer at the end of the stream.

        A trailing partial ``<think>`` / ``</think>`` tag (or any text
        held back while a tag might still be completed) is routed to the
        channel it belongs to instead of being dropped.
        """
        self._drain_think_pending(final=True)

    def _drain_think_pending(self, *, final: bool) -> None:
        """Route buffered text to the reasoning / visible channel.

        While ``_think_mode`` is False the buffer is scanned for an
        opening ``<think>`` tag; while it is True the buffer is scanned
        for the closing ``</think>`` tag.  Tag text itself is never
        emitted to either channel.  A trailing fragment that could still
        become a tag is held back until the next delta (or until
        :meth:`flush` at the end of the stream).
        """
        while self._think_pending:
            tag_re = _THINK_CLOSE_RE if self._think_mode else _THINK_OPEN_RE
            match = tag_re.search(self._think_pending)
            if match is not None:
                before = self._think_pending[: match.start()]
                if before:
                    self._emit_channel(before)
                self._think_pending = self._think_pending[match.end() :]
                self._think_mode = not self._think_mode
                continue
            if final:
                self._emit_channel(self._think_pending)
                self._think_pending = ""
                return
            hold = _partial_tag_suffix(self._think_pending, "</think>" if self._think_mode else "<think>")
            emit_len = len(self._think_pending) - len(hold)
            if emit_len:
                self._emit_channel(self._think_pending[:emit_len])
            self._think_pending = hold
            return

    def _emit_channel(self, text: str) -> None:
        """Send *text* to the reasoning or the visible channel."""
        if not text:
            return
        if self._think_mode:
            self.on_reasoning_delta(text)
        else:
            self.on_visible_delta(text)

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

    def snapshot(self) -> LoopGuardSnapshot:
        """Return a frozen :class:`LoopGuardSnapshot` of the current state."""
        return LoopGuardSnapshot(
            estimated_reasoning_tokens=(self._bytes + 2) // 3,
            reasoning_utf8_bytes=self._bytes,
            visible_non_whitespace_chars=self._visible_nw,
            total_segments=self._total_segments,
            repeated_positions=self._last_repeated,
            meta_intent_segments=self._last_meta,
            repetition_ratio_millis=self._last_ratio_millis,
            trigger=self._trigger,
            verbatim_tail_repeats=self._verbatim_repeats,
            trigram_pairs_matched=self._last_trigram_pairs,
            stall_segments=self._last_stall_segments,
            header_lines=self._header_lines,
            estimated_visible_tokens=(self._visible_bytes + 2) // 3,
            visible_utf8_bytes=self._visible_bytes,
            visible_tail_chars=len(self._visible_tail),
            visible_verbatim_repeats=self._visible_verbatim_repeats,
            visible_trigram_pairs=self._last_visible_trigram_pairs,
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
        # A titled line is counted before the chunk split so the
        # header-runaway detector sees the logical line, not fragments.
        if self._profile.header_runaway and _HEADER_LINE_RE.match(line):
            self._header_lines += 1
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
        if self._profile.ceiling and estimated_tokens >= self._ceiling_tokens:
            self._trigger = TRIGGER_CEILING
            return

        # Visible ceiling: the answer channel blew through its own
        # budget.  Independent of the reasoning ceiling.
        visible_tokens = (self._visible_bytes + 2) // 3
        if self._profile.visible_ceiling and visible_tokens >= self._visible_ceiling_tokens:
            self._trigger = TRIGGER_VISIBLE_CEILING
            return

        # Header runaway: the model emits a stream of titled sections
        # without ever committing to a tool call.
        if self._profile.header_runaway and self._header_lines >= HEADER_RUNAWAY_THRESHOLD:
            self._trigger = TRIGGER_HEADER
            return

        # Visible verbatim repeat: the answer channel itself loops.
        if self._profile.visible_verbatim and self._visible_repeated():
            self._trigger = TRIGGER_VISIBLE_VERBATIM
            return

        # Visible near-duplicate lines: the answer channel drifts into a
        # template it never leaves.
        if self._profile.visible_trigram and self._visible_trigram_similar():
            self._trigger = TRIGGER_VISIBLE_TRIGRAM
            return

        # Verbatim repeated tail (reasoning channel).
        if self._profile.verbatim_tail and self._verbatim_repeated():
            self._trigger = TRIGGER_VERBATIM
            return

        total = self._total_segments
        if total < REPETITION_EVAL_THRESHOLD:
            return  # not enough signal yet

        # Progress stall: the window stopped contributing new information.
        if self._profile.progress_stall and self._progress_stalled():
            self._trigger = TRIGGER_STALL
            return

        # Near-duplicate segments (reasoning channel).
        if self._profile.trigram_jaccard and self._trigram_similar():
            self._trigger = TRIGGER_TRIGRAM
            return

        # V = visible answer has NOT yet crossed the disable threshold.
        # Only the meta-intent path ("narrating a tool call") is
        # silenced by a visible answer; the generic detectors above run
        # regardless.
        if self._visible_nw >= VISIBLE_DISABLE_THRESHOLD:
            return

        if not self._profile.meta_intent:
            return

        # R, M, V path — only evaluate once enough segments have arrived
        # to distinguish a real loop from a normal multi-step plan.
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

        if repeated >= threshold and meta >= META_INTENT_MIN_COUNT:
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

    # ------------------------------------------------------------------
    # Generic detectors
    # ------------------------------------------------------------------

    @staticmethod
    def _trigrams(segment: str) -> set[str]:
        """Return the set of character trigrams in *segment*."""
        return {segment[i : i + 3] for i in range(len(segment) - 2)}

    @staticmethod
    def _jaccard(a: set[str], b: set[str]) -> float:
        """Jaccard similarity of two trigram sets."""
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    def _verbatim_repeated(self) -> bool:
        """True when the tail's last block already occurred earlier.

        The comparison is exact: the final ``EXACT_REPEAT_CHARS``
        characters of the tail must appear verbatim somewhere in the
        preceding part of the tail window.
        """
        if len(self._tail) < EXACT_TAIL_WINDOW or self._tail_growth < EXACT_REPEAT_CHARS:
            return False
        self._tail_growth = 0
        block = self._tail[-EXACT_REPEAT_CHARS:]
        if block in self._tail[:-EXACT_REPEAT_CHARS]:
            self._verbatim_repeats += 1
            return True
        return False

    def _trigram_similar(self) -> bool:
        """True when most adjacent segments in the window are near-duplicates."""
        window = list(self._segments)[-TRIGRAM_WINDOW:]
        if len(window) < TRIGRAM_WINDOW:
            return False
        sets = [self._trigrams(seg) for seg in window]
        matched = sum(1 for a, b in pairwise(sets) if self._jaccard(a, b) >= TRIGRAM_JACCARD)
        self._last_trigram_pairs = matched
        return matched >= math.ceil(TRIGRAM_PAIR_RATIO * (len(sets) - 1))

    def _visible_repeated(self) -> bool:
        """True when the visible tail repeats a block verbatim.

        Same shape as :meth:`_verbatim_repeated`, but on the visible
        channel: the last ``EXACT_REPEAT_CHARS`` characters of the
        visible tail must occur at least
        :data:`VISIBLE_EXACT_MIN_OCCURRENCES` times inside the tail
        window.
        """
        if len(self._visible_tail) < EXACT_TAIL_WINDOW:
            return False
        if self._visible_tail_growth < EXACT_REPEAT_CHARS:
            return False
        self._visible_tail_growth = 0
        block = self._visible_tail[-EXACT_REPEAT_CHARS:]
        if self._visible_tail.count(block) >= VISIBLE_EXACT_MIN_OCCURRENCES:
            self._visible_verbatim_repeats += 1
            return True
        return False

    def _visible_trigram_similar(self) -> bool:
        """True when the trailing visible lines are near-duplicates.

        The visible channel is line-oriented: the detector compares the
        trigram sets of the last :data:`VISIBLE_TRIGRAM_WINDOW`
        non-empty lines and fires when at least
        :data:`VISIBLE_TRIGRAM_PAIR_RATIO` of the adjacent pairs are
        near-duplicates.
        """
        if self._visible_trigram_growth < VISIBLE_TRIGRAM_MIN_GROWTH:
            return False
        self._visible_trigram_growth = 0
        lines = [ln for ln in self._visible_tail.splitlines() if ln.strip()]
        window = [self._normalize(ln) for ln in lines[-VISIBLE_TRIGRAM_WINDOW:]]
        window = [seg for seg in window if seg]
        if len(window) < VISIBLE_TRIGRAM_WINDOW:
            return False
        sets = [self._trigrams(seg) for seg in window]
        matched = sum(1 for a, b in pairwise(sets) if self._jaccard(a, b) >= VISIBLE_TRIGRAM_JACCARD)
        self._last_visible_trigram_pairs = matched
        return matched >= math.ceil(VISIBLE_TRIGRAM_PAIR_RATIO * (len(sets) - 1))

    def _progress_stalled(self) -> bool:
        """True when the trailing window stopped producing new information.

        "New information" is measured by trigram novelty: the share of a
        segment's trigrams that were never seen earlier in the window.
        A window where nearly every segment is a near-duplicate of what
        came before, and where no segment names a concrete anchor (hex
        address, call expression, source path), means the model is
        elaborating without moving.
        """
        window = list(self._segments)[-STALL_WINDOW:]
        if len(window) < STALL_WINDOW:
            return False
        known: set[str] = set()
        novelty: list[float] = []
        for seg in window:
            tri = self._trigrams(seg)
            novelty.append(len(tri - known) / max(1, len(tri)))
            known |= tri
        self._last_stall_segments = sum(1 for n in novelty[1:] if n <= STALL_NOVELTY)
        if self._last_stall_segments < STALL_WINDOW - 1:
            return False
        return not any(_ANCHOR_RE.search(seg) for seg in window[1:])
