"""Tests for the GLMReasoningGuard bounded detector.

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
"""

from __future__ import annotations

from lucnhan.agent.glm_guard import GLMGuardSnapshot, GLMReasoningGuard

# ---------------------------------------------------------------------------
# Step 1: hard ceiling boundary (token-unit API)
# ---------------------------------------------------------------------------


def test_hard_ceiling_flips_at_first_16384_estimated_token_byte() -> None:
    guard = GLMReasoningGuard(exposed_tool_names=["read_bytes"])

    guard.on_reasoning_delta("a" * 49_149)
    assert guard.should_abort() is False

    guard.on_reasoning_delta("a")
    assert guard.should_abort() is True
    assert guard.trigger == "reasoning_ceiling"


def test_constructor_rejects_non_positive_ceiling_tokens() -> None:
    import pytest

    with pytest.raises(ValueError, match="ceiling_tokens must be positive"):
        GLMReasoningGuard(exposed_tool_names=[], ceiling_tokens=0)
    with pytest.raises(ValueError, match="ceiling_tokens must be positive"):
        GLMReasoningGuard(exposed_tool_names=[], ceiling_tokens=-1)


# ---------------------------------------------------------------------------
# Step 2: repetition detection, with false-positive guards
# ---------------------------------------------------------------------------


def test_31_segments_never_trigger_repetition() -> None:
    guard = GLMReasoningGuard(exposed_tool_names=["read_bytes"])
    for _ in range(31):
        guard.on_reasoning_delta("outputting read_bytes tool now\n")
    assert guard.should_abort() is False


def test_full_window_requires_39_repeated_positions_and_8_meta_segments() -> None:
    guard = GLMReasoningGuard(exposed_tool_names=["read_bytes"])
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
    guard = GLMReasoningGuard(exposed_tool_names=["read_bytes"])
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
    guard = GLMReasoningGuard(exposed_tool_names=["read_bytes"])
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
    guard = GLMReasoningGuard(exposed_tool_names=["read_bytes"])
    guard.on_visible_delta("v")  # 1 non-whitespace char → V still open
    guard.on_reasoning_delta("outputting read_bytes tool now\n" * 64)

    assert guard.should_abort() is True
    assert guard.trigger == "repetition_meta_intent"


def test_255_visible_chars_still_allows_repetition() -> None:
    """255 visible chars are still below the 256 disable threshold."""
    guard = GLMReasoningGuard(exposed_tool_names=["read_bytes"])
    guard.on_visible_delta("v" * 255)
    guard.on_reasoning_delta("outputting read_bytes tool now\n" * 64)

    assert guard.should_abort() is True
    assert guard.trigger == "repetition_meta_intent"


def test_256_visible_chars_disables_repetition() -> None:
    """At exactly 256 visible chars, the repetition path is silenced."""
    guard = GLMReasoningGuard(exposed_tool_names=["read_bytes"])
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
    guard = GLMReasoningGuard(exposed_tool_names=[])
    guard.on_reasoning_delta("emit data invoke now\n" * 64)
    assert guard.should_abort() is True
    assert guard.trigger == "repetition_meta_intent"


def test_output_with_parallel_recognized_as_meta_intent() -> None:
    """``output`` is in the action vocab, ``parallel`` is in the tool vocab."""
    guard = GLMReasoningGuard(exposed_tool_names=[])
    guard.on_reasoning_delta("output data parallel call now\n" * 64)
    assert guard.should_abort() is True
    assert guard.trigger == "repetition_meta_intent"


def test_vocab_does_not_recognize_speculative_terms() -> None:
    """Removed terms (``running``, ``using``, ``let me``...) are NOT action terms.

    ``running the tool`` has a tool term (``tool``) but no current
    action term, so it is NOT meta-intent and the repetition path
    cannot fire even with a full window of repeats.
    """
    guard = GLMReasoningGuard(exposed_tool_names=[])
    guard.on_reasoning_delta("running the tool now\n" * 64)
    # R fires (all 64 repeat), but M = 0 < 8 → no trigger.
    assert guard.should_abort() is False


# ---------------------------------------------------------------------------
# Tool start suppresses both paths
# ---------------------------------------------------------------------------


def test_tool_start_disables_guard() -> None:
    guard = GLMReasoningGuard(exposed_tool_names=["read_bytes"])
    guard.on_tool_call_start()
    guard.on_reasoning_delta("x" * 60_000)
    assert guard.should_abort() is False


# ---------------------------------------------------------------------------
# Step 5: bounded internal state
# ---------------------------------------------------------------------------


def test_guard_state_stays_bounded() -> None:
    guard = GLMReasoningGuard(exposed_tool_names=[])
    for i in range(10_000):
        guard.on_reasoning_delta(f"unique segment {i}\n")

    assert guard.retained_segment_count <= 128
    assert guard.max_retained_segment_chars <= 240


def test_buffer_stays_bounded_under_one_huge_no_newline_delta() -> None:
    """A single huge no-newline delta must NOT let the buffer grow past 240."""
    guard = GLMReasoningGuard(exposed_tool_names=[])
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
    guard = GLMReasoningGuard(exposed_tool_names=["read_bytes"])
    guard.on_visible_delta("hi")
    guard.on_reasoning_delta("a" * 9)  # 9 UTF-8 bytes

    snap = guard.snapshot()
    assert isinstance(snap, GLMGuardSnapshot)
    assert snap.reasoning_utf8_bytes == 9
    # (bytes + 2) // 3 = (9 + 2) // 3 = 11 // 3 = 3
    assert snap.estimated_reasoning_tokens == 3
    assert snap.visible_non_whitespace_chars == 2
    assert snap.trigger == ""
