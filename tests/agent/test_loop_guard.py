"""Tests for the StreamLoopGuard bounded detector.

These tests prove the *external* contract: the guard must decide, from
incoming reasoning/visible deltas, whether to abort.  Internal state
(retained segment count, max segment length, partial-buffer length) is
also tested so the detector stays bounded for arbitrarily long streams.

Detector formula (per design spec):

    abort = T AND (H OR (R AND M AND V))

where:

* T = reasoning still active (no tool call started)
* H = estimated reasoning tokens >= ceiling_tokens (default 16,384)
* R = ratio of repeated fingerprints in evaluation window >= 60 %
* M = meta-intent segments in evaluation window >= 8
* V = visible answer has NOT yet reached the disable threshold (256)

Token estimate: ``(bytes + 2) // 3`` (rounds up at the boundary).

The generic detectors (verbatim tail, trigram similarity, progress
stall, header runaway) are profile-driven; the cases below construct
the guard with an explicit profile so each detector is exercised in
isolation.
"""

from __future__ import annotations

from lucnhan.agent.loop_guard import (
    _PROFILE_DEFAULT,
    _PROFILE_FULL,
    _PROFILE_OFF,
    EXACT_TAIL_WINDOW,
    LoopGuardProfile,
    LoopGuardSnapshot,
    StreamLoopGuard,
    resolve_loop_guard_profile,
)

# The exact repetition loop from the bug report ("Let me read. Let me
# go." after a read_bytes call), repeated 20 times (~4.9 KB).
# The report's range separator is an en dash; ``chr`` keeps the exact
# characters without an ambiguous-unicode literal.
_DASH = chr(0x2013)
_USER_LOOP = (
    "mm, 0x67B1C200 + 0x5000 = 0x67B21200. So the blob spans 0x67B1C200" + _DASH + "0x67B21200.\n"
    "Let me read the middle and end.\n"
    "Let me read 0x67B1D000 and 0x67B21000.\n"
    "Let me go.\n"
    "Let me read.\n"
    "Let me go.\n"
    "Let me read two spots.\n"
    "Let me go.\n"
    "Let me read.\n"
    "Let me go.\n"
) * 20

# 40 near-duplicate visible lines: identical apart from a trailing
# counter, so every adjacent pair stays above the 0.9 Jaccard
# threshold the visible trigram detector uses.
_USER_TRIGRAM_LINES = [
    f"Let me read the middle and the end of the blob and let me go, pass {i % 10}" for i in range(40)
]

# ---------------------------------------------------------------------------
# Step 1: hard ceiling boundary (token-unit API)
# ---------------------------------------------------------------------------


def test_hard_ceiling_flips_at_first_16384_estimated_token_byte() -> None:
    # An explicit profile without the verbatim detector: a 49 KB run of
    # "a" repeats itself, so the verbatim detector would otherwise fire
    # before the ceiling boundary.
    guard = StreamLoopGuard(
        exposed_tool_names=["read_bytes"],
        profile=LoopGuardProfile(ceiling=True, meta_intent=True),
    )

    guard.on_reasoning_delta("a" * 49_149)
    assert guard.should_abort() is False

    guard.on_reasoning_delta("a")
    assert guard.should_abort() is True
    assert guard.trigger == "reasoning_ceiling"


def test_constructor_rejects_non_positive_ceiling_tokens() -> None:
    import pytest

    with pytest.raises(ValueError, match="ceiling_tokens must be positive"):
        StreamLoopGuard(exposed_tool_names=[], ceiling_tokens=0)
    with pytest.raises(ValueError, match="ceiling_tokens must be positive"):
        StreamLoopGuard(exposed_tool_names=[], ceiling_tokens=-1)
    with pytest.raises(ValueError, match="visible_ceiling_tokens must be positive"):
        StreamLoopGuard(exposed_tool_names=[], visible_ceiling_tokens=0)
    with pytest.raises(ValueError, match="visible_ceiling_tokens must be positive"):
        StreamLoopGuard(exposed_tool_names=[], visible_ceiling_tokens=-1)


# ---------------------------------------------------------------------------
# Step 2: repetition detection, with false-positive guards
# ---------------------------------------------------------------------------


def test_31_segments_never_trigger_repetition() -> None:
    guard = StreamLoopGuard(exposed_tool_names=["read_bytes"])
    for _ in range(31):
        guard.on_reasoning_delta("outputting read_bytes tool now\n")
    assert guard.should_abort() is False


def test_full_window_requires_39_repeated_positions_and_8_meta_segments() -> None:
    guard = StreamLoopGuard(exposed_tool_names=["read_bytes"])
    repeated = "outputting read_bytes tool now"
    unique = [f"unique analysis line {i}" for i in range(25)]
    lines = [repeated] * 39 + unique

    guard.on_reasoning_delta("\n".join(lines))

    assert guard.should_abort() is True
    assert guard.trigger == "repetition_meta_intent"


def test_seven_meta_segments_with_repetition_do_not_trigger() -> None:
    """Exactly 7 meta-intent segments with R met must NOT trigger.

    The M threshold is 8, so 7 must fall short even when the
    repetition ratio crosses 60 %.
    """
    guard = StreamLoopGuard(exposed_tool_names=["read_bytes"])
    meta_seg = "outputting read_bytes tool now"  # action + tool name
    non_meta_repeat = "lorem ipsum dolor"  # no action, no tool term
    unique = [f"unique analysis {i}" for i in range(25)]
    # 7 meta + 32 repeated-non-meta + 25 unique = 64 segments
    # R: 7 + 32 = 39 repeated >= 39 threshold
    # M: 7 meta-intent < 8 → does NOT trigger
    lines = [meta_seg] * 7 + [non_meta_repeat] * 32 + unique

    guard.on_reasoning_delta("\n".join(lines))

    assert guard.should_abort() is False


def test_eight_meta_segments_with_repetition_triggers() -> None:
    """Exactly 8 meta-intent segments with R met MUST trigger."""
    guard = StreamLoopGuard(exposed_tool_names=["read_bytes"])
    meta_seg = "outputting read_bytes tool now"
    non_meta_repeat = "lorem ipsum dolor"
    unique = [f"unique analysis {i}" for i in range(24)]
    # 8 meta + 32 repeated-non-meta + 24 unique = 64 segments
    # R: 8 + 32 = 40 repeated >= 39
    # M: 8 meta-intent >= 8 → triggers
    lines = [meta_seg] * 8 + [non_meta_repeat] * 32 + unique

    guard.on_reasoning_delta("\n".join(lines))

    assert guard.should_abort() is True
    assert guard.trigger == "repetition_meta_intent"


# ---------------------------------------------------------------------------
# Visible-text threshold discrimination (< 256 keeps V open)
# ---------------------------------------------------------------------------


def test_one_visible_char_still_allows_repetition() -> None:
    """A single visible character must NOT silence the repetition path."""
    guard = StreamLoopGuard(exposed_tool_names=["read_bytes"])
    guard.on_visible_delta("v")  # 1 non-whitespace char → V still open
    guard.on_reasoning_delta("outputting read_bytes tool now\n" * 64)

    assert guard.should_abort() is True
    assert guard.trigger == "repetition_meta_intent"


def test_255_visible_chars_still_allows_repetition() -> None:
    """255 visible chars are still below the 256 disable threshold."""
    guard = StreamLoopGuard(exposed_tool_names=["read_bytes"])
    guard.on_visible_delta("v" * 255)
    guard.on_reasoning_delta("outputting read_bytes tool now\n" * 64)

    assert guard.should_abort() is True
    assert guard.trigger == "repetition_meta_intent"


def test_256_visible_chars_disables_meta_intent() -> None:
    """At exactly 256 visible chars, the meta-intent path is silenced.

    The generic repetition detectors and both ceilings stay active — a
    visible answer only means the model is no longer *narrating* a tool
    call.
    """
    guard = StreamLoopGuard(exposed_tool_names=["read_bytes"])
    guard.on_visible_delta("v" * 256)
    guard.on_reasoning_delta("outputting read_bytes tool now\n" * 64)
    assert guard.should_abort() is False

    # Ceiling path is independent of V.
    guard.on_reasoning_delta("x" * 49_150)
    assert guard.should_abort() is True


# ---------------------------------------------------------------------------
# Vocab: emit/output (action) cross invoke/parallel (tool)
# ---------------------------------------------------------------------------


def test_emit_with_invoke_recognized_as_meta_intent() -> None:
    """``emit`` is in the action vocab, ``invoke`` is in the tool vocab."""
    guard = StreamLoopGuard(exposed_tool_names=[])
    guard.on_reasoning_delta("emit data invoke now\n" * 64)
    assert guard.should_abort() is True
    assert guard.trigger == "repetition_meta_intent"


def test_output_with_parallel_recognized_as_meta_intent() -> None:
    """``output`` is in the action vocab, ``parallel`` is in the tool vocab."""
    guard = StreamLoopGuard(exposed_tool_names=[])
    guard.on_reasoning_delta("output data parallel call now\n" * 64)
    assert guard.should_abort() is True
    assert guard.trigger == "repetition_meta_intent"


def test_vocab_does_not_recognize_speculative_terms() -> None:
    """Removed terms (``running``, ``using``, ``let me``...) are NOT action terms.

    ``running the tool`` has a tool term (``tool``) but no current
    action term, so it is NOT meta-intent and the repetition path
    cannot fire even with a full window of repeats.
    """
    guard = StreamLoopGuard(exposed_tool_names=[])
    guard.on_reasoning_delta("running the tool now\n" * 64)
    # R fires (all 64 repeat), but M = 0 < 8 → no trigger.
    assert guard.should_abort() is False


# ---------------------------------------------------------------------------
# Tool start suppresses both paths
# ---------------------------------------------------------------------------


def test_tool_start_disables_guard() -> None:
    guard = StreamLoopGuard(exposed_tool_names=["read_bytes"])
    guard.on_tool_call_start()
    guard.on_reasoning_delta("x" * 60_000)
    assert guard.should_abort() is False


# ---------------------------------------------------------------------------
# Step 5: bounded internal state
# ---------------------------------------------------------------------------


def test_guard_state_stays_bounded() -> None:
    guard = StreamLoopGuard(exposed_tool_names=[])
    for i in range(10_000):
        guard.on_reasoning_delta(f"unique segment {i}\n")
        guard.on_visible_delta(f"unique visible segment {i}\n")

    assert guard.retained_segment_count <= 128
    assert guard.max_retained_segment_chars <= 240
    assert guard.visible_tail_chars <= 4096


def test_buffer_stays_bounded_under_one_huge_no_newline_delta() -> None:
    """A single huge no-newline delta must NOT let the buffer grow past 240."""
    guard = StreamLoopGuard(exposed_tool_names=[])
    guard.on_reasoning_delta("x" * 1_000_000)  # 1 MB, zero newlines

    # Partial buffer must be < MAX_SEGMENT_CHARS (everything else is
    # already flushed as 240-char chunks).
    assert guard.partial_buffer_chars < 240
    # Ring buffer + per-segment cap still hold.
    assert guard.retained_segment_count <= 128
    assert guard.max_retained_segment_chars <= 240


# ---------------------------------------------------------------------------
# Snapshot contract
# ---------------------------------------------------------------------------


def test_snapshot_reports_byte_count_token_estimate_and_repetition_ratio() -> None:
    guard = StreamLoopGuard(exposed_tool_names=["read_bytes"])
    guard.on_visible_delta("hi")
    guard.on_reasoning_delta("a" * 9)  # 9 UTF-8 bytes

    snap = guard.snapshot()
    assert isinstance(snap, LoopGuardSnapshot)
    assert snap.reasoning_utf8_bytes == 9
    # (bytes + 2) // 3 = (9 + 2) // 3 = 11 // 3 = 3
    assert snap.estimated_reasoning_tokens == 3
    assert snap.visible_non_whitespace_chars == 2
    assert snap.trigger == ""


# ---------------------------------------------------------------------------
# Generic detectors (profile-driven)
# ---------------------------------------------------------------------------


def test_verbatim_tail_requires_full_window() -> None:
    """The verbatim detector needs a full 4096-char tail window.

    A repeat inside a window that is not yet full must not fire, and a
    block's *first* appearance must not fire either — only the tail's
    last 180 chars repeating somewhere earlier inside the window.
    """
    # (a) The window is not full: the repeated block is inside the
    #     tail but the tail is shorter than the window.
    short_guard = StreamLoopGuard(exposed_tool_names=[], profile=_PROFILE_FULL)
    short = "".join(f"{i:05d} " for i in range(500))[:3000]
    short_guard.on_reasoning_delta(short + short[-200:])
    assert short_guard.should_abort() is False

    # (b) The window fills but the block is new.
    guard = StreamLoopGuard(exposed_tool_names=[], profile=_PROFILE_FULL)
    unique = "".join(f"{i:05d} " for i in range(700))[:4095]
    guard.on_reasoning_delta(unique)
    assert guard.should_abort() is False

    block = "".join(f"rep{i:05d} " for i in range(40))[:200]
    guard.on_reasoning_delta(block)
    assert guard.should_abort() is False

    # (c) The same block again: the tail's last 180 chars now repeat.
    guard.on_reasoning_delta(block)
    assert guard.should_abort() is True
    assert guard.trigger == "verbatim_tail"


def test_trigram_detector_fires_on_near_duplicates() -> None:
    """Near-duplicate segments trip the trigram detector.

    The control case (same stream, trigram detector disabled) must not
    trip, so the assertion really exercises the detector.
    """
    body = "the quick brown fox jumps over the lazy dog 0x401000 "
    segments = [f"s{i:03d} " + body * 2 for i in range(32)]

    guard = StreamLoopGuard(exposed_tool_names=[], profile=_PROFILE_FULL)
    guard.on_reasoning_delta("\n".join(segments) + "\n")
    assert guard.should_abort() is True
    assert guard.trigger == "trigram_jaccard"

    without_trigram = LoopGuardProfile(
        ceiling=True,
        meta_intent=True,
        verbatim_tail=True,
        trigram_jaccard=False,
        progress_stall=True,
        header_runaway=True,
    )
    control = StreamLoopGuard(exposed_tool_names=[], profile=without_trigram)
    control.on_reasoning_delta("\n".join(segments) + "\n")
    assert control.should_abort() is False


def test_progress_stall_needs_no_new_anchors() -> None:
    """A window that stops producing new information trips the stall detector.

    The same window with a concrete anchor (a hex address) in every
    segment means the model *is* pointing at something, so it must not
    trip.
    """
    segments = [f"analysis {i} " + "repeated filler text here " * 3 for i in range(32)]

    guard = StreamLoopGuard(exposed_tool_names=[], profile=_PROFILE_DEFAULT)
    guard.on_reasoning_delta("\n".join(segments) + "\n")
    assert guard.should_abort() is True
    assert guard.trigger == "progress_stall"

    anchored = [seg + "0x401000" for seg in segments]
    control = StreamLoopGuard(exposed_tool_names=[], profile=_PROFILE_DEFAULT)
    control.on_reasoning_delta("\n".join(anchored) + "\n")
    assert control.should_abort() is False


def test_header_runaway_fires_without_tool_call() -> None:
    """A stream of titled lines trips the header detector.

    Once a tool call starts the model has committed, so the detector
    must not fire.
    """
    lines = "\n".join(f"## Section {i}" for i in range(24))

    guard = StreamLoopGuard(exposed_tool_names=[], profile=_PROFILE_FULL)
    guard.on_reasoning_delta(lines + "\n")
    assert guard.should_abort() is True
    assert guard.trigger == "header_runaway"

    committed = StreamLoopGuard(exposed_tool_names=[], profile=_PROFILE_FULL)
    committed.on_tool_call_start()
    committed.on_reasoning_delta(lines + "\n")
    assert committed.should_abort() is False


def test_profile_off_disables_all() -> None:
    """The off profile disables every detector, including the ceiling."""
    guard = StreamLoopGuard(exposed_tool_names=[], profile=_PROFILE_OFF)
    guard.on_reasoning_delta("x" * 60_000)
    assert guard.should_abort() is False
    assert guard.trigger == ""


def test_resolver_maps_families() -> None:
    """``auto`` maps each model family to its documented profile."""
    default = resolve_loop_guard_profile("openai", "gpt-5", "auto")
    assert default.meta_intent is False
    assert default.verbatim_tail is True
    assert default.visible_verbatim is True
    assert default.visible_trigram is True
    assert default.visible_ceiling is True

    full = resolve_loop_guard_profile("deepseek", "deepseek-r1", "auto")
    assert full.trigram_jaccard is True
    assert full.meta_intent is True

    glm = resolve_loop_guard_profile("glm", "glm-5.2", "auto")
    assert glm.meta_intent is True
    assert glm.trigram_jaccard is False
    assert glm.visible_verbatim is True

    assert resolve_loop_guard_profile("openai", "gpt-5", "off").enabled is False
    assert resolve_loop_guard_profile("openai", "gpt-5", "off").visible_ceiling is False
    assert resolve_loop_guard_profile("openai", "gpt-5", "full").header_runaway is True
    assert resolve_loop_guard_profile("openai", "gpt-5", "glm").trigram_jaccard is False


# ---------------------------------------------------------------------------
# Visible-channel detectors (the reported loop lives in the visible text)
# ---------------------------------------------------------------------------


def test_visible_verbatim_tail_fires_on_reported_loop() -> None:
    """The loop from the bug report must trip the visible detector.

    Before the fix the 256 visible-character threshold silenced every
    repetition detector, so the loop streamed for tens of kilobytes.
    """
    guard = StreamLoopGuard(
        exposed_tool_names=[],
        profile=LoopGuardProfile(ceiling=False, meta_intent=False, visible_verbatim=True),
    )
    guard.on_visible_delta(_USER_LOOP)

    assert guard.should_abort() is True
    assert guard.trigger == "visible_verbatim_tail"
    # The old suppression threshold is exceeded — the detector is no
    # longer silenced by visible text.
    assert guard.snapshot().visible_non_whitespace_chars > 256


def test_visible_trigram_fires_on_near_duplicate_lines() -> None:
    """Near-duplicate visible lines trip the visible trigram detector."""
    guard = StreamLoopGuard(
        exposed_tool_names=[],
        profile=LoopGuardProfile(ceiling=False, meta_intent=False, visible_trigram=True),
    )
    for line in _USER_TRIGRAM_LINES:
        guard.on_visible_delta(line + "\n")

    assert guard.should_abort() is True
    assert guard.trigger == "visible_trigram_jaccard"


def test_visible_detectors_ignore_varied_answer() -> None:
    """A long, verbose but genuinely varied answer must not trip.

    Enumerated analysis steps share a template, so the near-duplicate
    detector must stay strict enough to ignore them.
    """
    guard = StreamLoopGuard(exposed_tool_names=[], profile=_PROFILE_FULL)
    for i in range(300):
        guard.on_visible_delta(
            f"Step {i}: analyze function sub_{i:04x} at 0x{0x401000 + i * 16:x}, "
            "inspect its callers and rename local variables accordingly.\n"
        )

    assert guard.should_abort() is False


def test_visible_ceiling_fires() -> None:
    """The visible channel has its own token ceiling."""
    guard = StreamLoopGuard(
        exposed_tool_names=[],
        profile=LoopGuardProfile(ceiling=False, meta_intent=False, visible_ceiling=True),
        visible_ceiling_tokens=1024,
    )
    guard.on_visible_delta("x" * 3072)

    assert guard.should_abort() is True
    assert guard.trigger == "visible_ceiling"


# ---------------------------------------------------------------------------
# Inline-``<think>`` routing
# ---------------------------------------------------------------------------


def test_on_text_delta_routes_think_block_to_reasoning() -> None:
    """A literal ``<think>`` span is reasoning, not visible answer text."""
    guard = StreamLoopGuard(exposed_tool_names=[], profile=_PROFILE_FULL)

    guard.on_text_delta("<thi")
    guard.on_text_delta("nk>")
    guard.on_text_delta(_USER_LOOP)
    guard.on_text_delta("</thi")
    guard.on_text_delta("nk>")

    snap = guard.snapshot()
    assert guard.trigger == "verbatim_tail"
    assert snap.visible_non_whitespace_chars == 0
    assert snap.reasoning_utf8_bytes > 4096


def test_on_text_delta_split_close_tag() -> None:
    """A close tag split across deltas still routes the text correctly."""
    guard = StreamLoopGuard(exposed_tool_names=[], profile=_PROFILE_FULL)

    guard.on_text_delta("<think>hello</thi")
    guard.on_text_delta("nk>world")

    snap = guard.snapshot()
    assert snap.reasoning_utf8_bytes == len("hello")
    assert snap.visible_non_whitespace_chars == len("world")


def test_flush_routes_unclosed_think_tail() -> None:
    """``flush()`` releases text held back for a possible tag."""
    guard = StreamLoopGuard(exposed_tool_names=[], profile=_PROFILE_FULL)

    guard.on_text_delta("<think>hello</thi")
    assert guard.snapshot().reasoning_utf8_bytes == len("hello")

    guard.flush()
    assert guard.snapshot().reasoning_utf8_bytes == len("hello</thi")


def test_default_profile_latches_short_four_line_cycle() -> None:
    """Regression: a custom OpenAI-compatible provider (unknown family →
    default profile) streaming a short 4-line thinking cycle must latch
    within a bounded amount of input instead of streaming forever."""
    profile = resolve_loop_guard_profile("custom-endpoint", "unknown-model", "auto")
    assert profile == _PROFILE_DEFAULT

    guard = StreamLoopGuard(exposed_tool_names=["read_bytes"], profile=profile)
    cycle = ["OK.\n", "Let me write.\n", "Writing.\n", "Go.\n"]
    fed = 0
    for _ in range(1000):
        for line in cycle:
            guard.on_reasoning_delta(line)
            fed += len(line.encode("utf-8"))
            if guard.should_abort():
                assert guard.trigger
                assert fed <= EXACT_TAIL_WINDOW, f"guard needed {fed} bytes (bound {EXACT_TAIL_WINDOW})"
                return
    raise AssertionError(f"guard did not latch after {fed} bytes of a {len(cycle)}-line cycle")
