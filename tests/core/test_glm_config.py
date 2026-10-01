"""Tests for typed GLM dialect configuration and model metadata.

Task 4 of the GLM reasoning resilience plan.  The parser is the single source
of truth for GLM configuration validation: it rejects unknown nested keys,
validates boolean/enum/range values, and reports exact field paths so users
see *which* GLM setting is wrong.

Effort levels come from the provider-neutral table in
:mod:`rikugan.core.thinking`: a wholly unknown level raises, while a level
the selected model does not accept (a stale saved value) normalizes to that
model's default instead of bricking provider construction.

Model metadata is data-only and lives next to the parser so call sites can
ask ``get_glm_model_metadata("glm-5.2")`` instead of hard-coding capability
flags.
"""

from __future__ import annotations

import pytest

from rikugan.core.glm_config import get_glm_model_metadata, parse_glm_extra


def test_default_glm_config_is_guarded_and_preserved():
    parsed = parse_glm_extra({"dialect": "glm"}, "glm-5.2")

    assert parsed.thinking.enabled is True
    assert parsed.thinking.reasoning_effort == "max"
    assert parsed.thinking.preserve is True
    assert parsed.guard.enabled is True
    assert parsed.guard.reasoning_token_ceiling == 16_384
    assert parsed.guard.recovery_max_tokens == 16_384


def test_glm_config_rejects_unknown_nested_key_with_field_path():
    with pytest.raises(ValueError, match=r"provider.extra.thinking.unknown"):
        parse_glm_extra(
            {"dialect": "glm", "thinking": {"unknown": True}},
            "glm-5.2",
        )


def test_glm_config_validates_ranges():
    with pytest.raises(ValueError, match=r"reasoning_token_ceiling"):
        parse_glm_extra(
            {
                "dialect": "glm",
                "degeneration_guard": {"reasoning_token_ceiling": 1023},
            },
            "glm-5.2",
        )


def test_unknown_glm_model_disables_tool_stream_and_effort():
    metadata = get_glm_model_metadata("glm-experimental")

    assert metadata.reasoning_content is True
    assert metadata.streaming_tool_calls is False
    assert metadata.reasoning_effort is False


def test_glm_5_2_context_window_is_one_million():
    """Z.AI lists GLM-5.2 with a 1,000,000-token context window and a
    131,072-token output limit."""
    metadata = get_glm_model_metadata("glm-5.2")

    assert metadata.context_window == 1_000_000
    assert metadata.max_output_tokens == 131_072


@pytest.mark.parametrize(
    "model_id",
    ["glm-5.1", "glm-5", "glm-4.7"],
)
def test_pre_glm_5_2_context_window_is_200k(model_id: str):
    """Z.AI lists GLM-5.1, GLM-5, and GLM-4.7 with a 200,000-token
    context window and a 131,072-token output limit.  These three
    must NOT inherit the 1M context window from GLM-5.2."""
    metadata = get_glm_model_metadata(model_id)

    assert metadata.context_window == 200_000
    assert metadata.max_output_tokens == 131_072


def test_unknown_glm_model_context_window_falls_back_to_200k():
    """Unknown GLM model IDs inherit the conservative 200K context
    window rather than the 1M ceiling, so request payloads are
    clamped against a realistic upper bound."""
    metadata = get_glm_model_metadata("glm-experimental")

    assert metadata.context_window == 200_000
    assert metadata.max_output_tokens == 131_072


def test_glm_5_3_uses_conservative_family_defaults():
    """GLM-5.3 joins the known-model table with the conservative
    200K / 131,072 family limits and full reasoning flags."""
    metadata = get_glm_model_metadata("glm-5.3")

    assert metadata.context_window == 200_000
    assert metadata.max_output_tokens == 131_072
    assert metadata.reasoning_content is True
    assert metadata.streaming_tool_calls is True
    assert metadata.reasoning_effort is True


# ---------------------------------------------------------------------------
# Thinking-effort validation
# ---------------------------------------------------------------------------


def test_effort_level_accepted_by_the_selected_model():
    parsed = parse_glm_extra(
        {"dialect": "glm", "thinking": {"enabled": True, "reasoning_effort": "xhigh"}},
        "glm-5.2",
    )

    assert parsed.thinking.reasoning_effort == "xhigh"


@pytest.mark.parametrize("level", ["high", "max"])
def test_glm_5_3_accepts_only_high_and_max(level: str):
    parsed = parse_glm_extra(
        {"dialect": "glm", "thinking": {"enabled": True, "reasoning_effort": level}},
        "glm-5.3",
    )

    assert parsed.thinking.reasoning_effort == level


@pytest.mark.parametrize("stale_level", ["ultra", "low", "medium", "xhigh", "minimal", "none"])
def test_stale_level_for_glm_5_3_normalizes_to_the_model_default(stale_level: str):
    """A level saved against another model (e.g. ``ultra`` after switching
    to GLM-5.3) must normalize instead of raising — a hard failure here
    would make GLMProvider.__init__ throw on every session start."""
    parsed = parse_glm_extra(
        {"dialect": "glm", "thinking": {"enabled": True, "reasoning_effort": stale_level}},
        "glm-5.3",
    )

    assert parsed.thinking.reasoning_effort == "high"


def test_unknown_effort_string_still_raises():
    """Hand-edited garbage is not a stale level — it is a config error."""
    with pytest.raises(ValueError, match=r"provider\.extra\.thinking\.reasoning_effort"):
        parse_glm_extra(
            {"dialect": "glm", "thinking": {"enabled": True, "reasoning_effort": "turbo"}},
            "glm-5.2",
        )


def test_non_string_effort_raises():
    with pytest.raises(ValueError, match=r"must be a string"):
        parse_glm_extra(
            {"dialect": "glm", "thinking": {"enabled": True, "reasoning_effort": 3}},
            "glm-5.2",
        )


def test_unknown_glm_model_accepts_any_known_level():
    """Unknown GLM IDs have no authoritative list; the level is passed
    through and GLMProvider omits the wire field entirely."""
    parsed = parse_glm_extra(
        {"dialect": "glm", "thinking": {"enabled": True, "reasoning_effort": "ultra"}},
        "glm-experimental",
    )

    assert parsed.thinking.reasoning_effort == "ultra"


def test_legacy_reasoning_effort_values_is_the_level_union():
    """The exported flat enum stays in sync with the level table."""
    from rikugan.core.glm_config import REASONING_EFFORT_VALUES
    from rikugan.core.thinking import ALL_THINKING_LEVELS

    assert REASONING_EFFORT_VALUES == frozenset(ALL_THINKING_LEVELS)
