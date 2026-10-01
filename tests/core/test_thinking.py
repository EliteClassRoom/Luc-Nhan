"""Tests for the provider-neutral thinking-level registry.

``rikugan.core.thinking`` is the single source of truth for which
thinking levels a model accepts.  Settings offers exactly the returned
list; ``glm_config`` validates saved levels against it; providers use
``has_model_thinking_levels`` to decide whether ``reasoning_effort`` may
go on the wire at all.
"""

from __future__ import annotations

import pytest

from rikugan.core.thinking import (
    ALL_THINKING_LEVELS,
    DEFAULT_THINKING_LEVELS,
    default_thinking_level,
    get_thinking_levels,
    has_model_thinking_levels,
)


def test_glm_5_3_has_restricted_levels():
    """GLM-5.3 accepts only high and max — the narrowest documented list."""
    assert get_thinking_levels("glm-5.3") == ("high", "max")
    assert has_model_thinking_levels("glm-5.3") is True


def test_glm_5_2_levels():
    assert get_thinking_levels("glm-5.2") == ("none", "minimal", "low", "medium", "high", "xhigh", "max")


@pytest.mark.parametrize("model_id", ["GLM-5.3", "GLM-5.3", "  glm-5.3  "])
def test_lookup_is_case_and_whitespace_insensitive(model_id: str):
    """Providers report IDs with inconsistent casing / padding."""
    assert get_thinking_levels(model_id) == ("high", "max")
    assert has_model_thinking_levels(model_id) is True


@pytest.mark.parametrize("model_id", ["", "   ", "gpt-4o", "llama3.1", "glm-9", "glm-5"])
def test_unknown_models_fall_back_to_the_default_list(model_id: str):
    assert get_thinking_levels(model_id) == DEFAULT_THINKING_LEVELS
    # Fallback means *unknown*, not "no thinking available" — providers
    # must not put reasoning_effort on the wire for these models.
    assert has_model_thinking_levels(model_id) is False


def test_default_levels_span_none_through_ultra():
    assert DEFAULT_THINKING_LEVELS[0] == "none"
    assert DEFAULT_THINKING_LEVELS[-1] == "ultra"


@pytest.mark.parametrize(
    ("levels", "expected"),
    [
        (DEFAULT_THINKING_LEVELS, "high"),
        (("high", "max"), "high"),
        (("none", "minimal", "low", "medium", "high", "xhigh", "max"), "high"),
        # No "high" member: take the highest remaining level.
        (("none", "low", "max"), "max"),
        (("minimal", "xhigh"), "xhigh"),
        # Nothing but "none".
        (("none",), "none"),
    ],
)
def test_default_thinking_level(levels, expected):
    assert default_thinking_level(levels) == expected


def test_all_levels_covers_every_table_entry():
    """Every level any model can accept must be a valid saved value, or
    ``glm_config`` would reject a level the Settings dialog just offered."""
    for model_id in ("glm-5.2", "glm-5.3"):
        assert set(get_thinking_levels(model_id)) <= ALL_THINKING_LEVELS
    assert set(DEFAULT_THINKING_LEVELS) <= ALL_THINKING_LEVELS


def test_all_levels_rejects_garbage():
    assert "turbo" not in ALL_THINKING_LEVELS
    assert "" not in ALL_THINKING_LEVELS
