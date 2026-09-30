"""Typed GLM dialect configuration and exact model metadata.

Task 4 of the GLM reasoning resilience plan.  This module is the single
source of truth for:

- the ``dialect == "glm"`` namespace inside ``ProviderConfig.extra``;
- the per-model capability flags (``reasoning_content``,
  ``streaming_tool_calls``, ``reasoning_effort``) and hard output limits
  that ``GLMProvider`` consults when building requests;
- validation of user-supplied GLM options against the GLM namespace only —
  unknown keys on non-GLM ``extra`` dicts are intentionally untouched.

The parser validates **saved intent**.  Range/range checks fail fast with a
field-path error so the Settings UI can highlight the exact control that
needs to change.  The effective recovery cap is *not* clamped at parse
time — that happens when a request is built, because the active model's
declared output limit may differ between selection and dispatch.

Spec reference: ``docs/superpowers/specs/2026-07-22-glm-reasoning-resilience-design.md``
sections 6.7, 7.1, 12.1, 12.2, 12.4.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .thinking import ALL_THINKING_LEVELS, default_thinking_level, get_thinking_levels, has_model_thinking_levels

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: GLM dialect identifier — must match the string Settings writes under
#: ``provider.extra["dialect"]`` when the user picks the GLM dialect.
GLM_DIALECT = "glm"

#: Hard reasoning-token ceiling used as the default guard limit.  Range
#: permitted by ``parse_glm_extra``: 1,024 to 65,536 estimated reasoning
#: tokens (spec §12.2).
REASONING_TOKEN_CEILING_DEFAULT = 16_384
REASONING_TOKEN_CEILING_MIN = 1_024
REASONING_TOKEN_CEILING_MAX = 65_536

#: Default total output-token cap for the one-shot recovery request.
#: Range permitted by ``parse_glm_extra``: 1,024 to 131,072.  The
#: effective recovery cap is further clamped against the active model's
#: declared ``max_output_tokens`` at request-build time (spec §9.2.8).
RECOVERY_MAX_TOKENS_DEFAULT = 16_384
RECOVERY_MAX_TOKENS_MIN = 1_024
RECOVERY_MAX_TOKENS_MAX = 131_072

#: Z.AI documented maximum output token count for the GLM-5.x and GLM-4.7
#: families.  Unknown GLM model IDs also inherit this conservative ceiling.
KNOWN_GLM_MAX_OUTPUT_TOKENS = 131_072

#: Z.AI documented context window for GLM-5.2 (1M tokens).  Per Z.AI's
#: official model pages, GLM-5.2 is the only GLM-5.x member with a 1M
#: context window; GLM-5.1 / GLM-5 / GLM-4.7 declare 200K.
KNOWN_GLM_5_2_CONTEXT_WINDOW = 1_000_000

#: Z.AI documented context window for GLM-5.1, GLM-5, and GLM-4.7
#: (200K tokens each).  Unknown GLM model IDs also inherit this conservative
#: ceiling so the provider can clamp request payloads before they exceed the
#: upstream endpoint's actual limit.
KNOWN_GLM_CONTEXT_WINDOW = 200_000

#: Legacy flat enum of ``reasoning_effort`` values.  The authoritative
#: per-model lists now live in :mod:`rikugan.core.thinking`; this set is
#: kept as the union of those lists for callers that only need a coarse
#: membership test.
REASONING_EFFORT_VALUES: frozenset[str] = frozenset(ALL_THINKING_LEVELS)

#: Z.AI endpoint types.  The "standard" endpoint serves the general-purpose
#: PaaS API (per-token billing); the "coding_plan" endpoint serves Z.AI's
#: Coding Plan subscription ($18/mo+) and requires a non-interchangeable
#: Plan API key.  The two base URLs are distinct per Z.AI docs:
#:   standard:     https://api.z.ai/api/paas/v4
#:   coding_plan:  https://api.z.ai/api/coding/paas/v4
#: Plan keys are rejected by the standard endpoint and vice-versa, so this
#: must be stored alongside the GLM config so the provider sends requests
#: to the correct base URL.
GLM_ENDPOINT_STANDARD = "standard"
GLM_ENDPOINT_CODING_PLAN = "coding_plan"
GLM_ENDPOINT_VALUES: frozenset[str] = frozenset({GLM_ENDPOINT_STANDARD, GLM_ENDPOINT_CODING_PLAN})

#: Default base URLs per endpoint type.  ``GLMProvider._get_client`` falls
#: back to these when the user has not set an explicit ``api_base``.
GLM_ENDPOINT_BASE_URLS: dict[str, str] = {
    GLM_ENDPOINT_STANDARD: "https://api.z.ai/api/paas/v4",
    GLM_ENDPOINT_CODING_PLAN: "https://api.z.ai/api/coding/paas/v4",
}

#: Exact key sets so we can reject unknown nested keys with the field path
#: in the error string.
_THINKING_KEYS: frozenset[str] = frozenset({"enabled", "reasoning_effort", "preserve"})
_GUARD_KEYS: frozenset[str] = frozenset(
    {"enabled", "reasoning_token_ceiling", "retry_without_thinking", "recovery_max_tokens"}
)
_EXTRA_KEYS: frozenset[str] = frozenset({"dialect", "thinking", "degeneration_guard", "endpoint_type"})


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GLMModelMetadata:
    """Per-model capability flags and hard limits for a GLM model ID.

    Unknown GLM model IDs inherit the conservative defaults: reasoning
    content still arrives (the user picked GLM), but streamed tool
    arguments and the ``reasoning_effort`` field are unavailable, so
    ``GLMProvider._build_request_kwargs()`` must omit them.
    """

    context_window: int
    max_output_tokens: int
    reasoning_content: bool
    streaming_tool_calls: bool
    reasoning_effort: bool


@dataclass(frozen=True)
class GLMThinkingConfig:
    """User-facing thinking controls that map to Z.AI's preserved-thinking protocol."""

    enabled: bool = True
    reasoning_effort: str = "max"
    preserve: bool = True


@dataclass(frozen=True)
class GLMGuardConfig:
    """Reasoning-degeneration guard controls.

    ``reasoning_token_ceiling`` is the estimated-reasoning-token hard limit
    that aborts a turn before it consumes the configured ``max_tokens``;
    range 1,024-65,536.  ``recovery_max_tokens`` is the *user-saved* total
    output-token cap for the one-shot recovery attempt (range
    1,024-131,072); the effective recovery cap is further clamped against
    the active model's declared output limit at request-build time.
    """

    enabled: bool = True
    reasoning_token_ceiling: int = REASONING_TOKEN_CEILING_DEFAULT
    retry_without_thinking: bool = True
    recovery_max_tokens: int = RECOVERY_MAX_TOKENS_DEFAULT


@dataclass(frozen=True)
class GLMConfig:
    """Validated GLM dialect configuration."""

    thinking: GLMThinkingConfig
    guard: GLMGuardConfig
    endpoint_type: str = GLM_ENDPOINT_STANDARD

    @property
    def base_url(self) -> str:
        """The default API base URL for the configured endpoint type."""
        return GLM_ENDPOINT_BASE_URLS.get(self.endpoint_type, GLM_ENDPOINT_BASE_URLS[GLM_ENDPOINT_STANDARD])


# ---------------------------------------------------------------------------
# Model metadata lookup
# ---------------------------------------------------------------------------

#: Known GLM model entries.  Capabilities follow the Z.AI contract:
#: ``reasoning_content`` starts with the GLM-4.5-series-and-newer models,
#: ``streaming_tool_calls`` starts with GLM-4.6-and-newer, and
#: ``reasoning_effort`` is sent for the models that advertise it
#: (spec §12.4).  Per-model effort *levels* live in
#: :mod:`rikugan.core.thinking`.
_KNOWN_GLM_MODELS: dict[str, GLMModelMetadata] = {
    "glm-5.3": GLMModelMetadata(
        context_window=KNOWN_GLM_CONTEXT_WINDOW,
        max_output_tokens=KNOWN_GLM_MAX_OUTPUT_TOKENS,
        reasoning_content=True,
        streaming_tool_calls=True,
        reasoning_effort=True,
    ),
    "glm-5.2": GLMModelMetadata(
        context_window=KNOWN_GLM_5_2_CONTEXT_WINDOW,
        max_output_tokens=KNOWN_GLM_MAX_OUTPUT_TOKENS,
        reasoning_content=True,
        streaming_tool_calls=True,
        reasoning_effort=True,
    ),
    "glm-5.1": GLMModelMetadata(
        context_window=KNOWN_GLM_CONTEXT_WINDOW,
        max_output_tokens=KNOWN_GLM_MAX_OUTPUT_TOKENS,
        reasoning_content=True,
        streaming_tool_calls=True,
        reasoning_effort=False,
    ),
    "glm-5": GLMModelMetadata(
        context_window=KNOWN_GLM_CONTEXT_WINDOW,
        max_output_tokens=KNOWN_GLM_MAX_OUTPUT_TOKENS,
        reasoning_content=True,
        streaming_tool_calls=True,
        reasoning_effort=False,
    ),
    "glm-4.7": GLMModelMetadata(
        context_window=KNOWN_GLM_CONTEXT_WINDOW,
        max_output_tokens=KNOWN_GLM_MAX_OUTPUT_TOKENS,
        reasoning_content=True,
        streaming_tool_calls=True,
        reasoning_effort=False,
    ),
}

#: Conservative defaults for unknown GLM model IDs — reasoning content is
#: assumed (the user picked GLM), but streamed tool arguments and
#: ``reasoning_effort`` are omitted to avoid unsupported-parameter failures.
_UNKNOWN_GLM_METADATA: GLMModelMetadata = GLMModelMetadata(
    context_window=KNOWN_GLM_CONTEXT_WINDOW,
    max_output_tokens=KNOWN_GLM_MAX_OUTPUT_TOKENS,
    reasoning_content=True,
    streaming_tool_calls=False,
    reasoning_effort=False,
)


def get_glm_model_metadata(model_id: str) -> GLMModelMetadata:
    """Return the documented capabilities for ``model_id``.

    Unknown GLM model IDs return conservative defaults so the provider can
    still build a safe request without silently adding fields the upstream
    endpoint may reject.
    """
    return _KNOWN_GLM_MODELS.get(model_id, _UNKNOWN_GLM_METADATA)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _validate_field_path(extra: Mapping[str, Any], key: str, allowed: frozenset[str]) -> None:
    """Reject unknown nested keys under ``provider.extra.<key>``.

    Validation is GLM-namespace-only and only fires when the sub-dict is
    actually present.  A missing sub-dict means "use the GLM defaults",
    which the dataclass ``field`` defaults encode.  Non-GLM ``extra``
    dictionaries never reach this code path.
    """
    sub = extra.get(key)
    if sub is None:
        return
    if not isinstance(sub, Mapping):
        raise ValueError(f"provider.extra.{key} must be a dict, got {type(sub).__name__}")
    unknown = set(sub.keys()) - allowed
    if unknown:
        raise ValueError(f"provider.extra.{key}.{sorted(unknown)[0]}: unknown key")


def _require_bool(extra: Mapping[str, Any], field_path: str) -> bool:
    value = extra.get(field_path.split(".")[-1])
    if not isinstance(value, bool):
        raise ValueError(f"{field_path} must be a boolean, got {type(value).__name__}")
    return value


def _parse_thinking(extra: Mapping[str, Any], model_id: str) -> GLMThinkingConfig:
    """Parse the ``provider.extra.thinking`` sub-dict.

    Effort validation is provider-neutral and driven by
    :mod:`rikugan.core.thinking`:

    * a non-string or wholly unknown level string is a hand-edit error
      and raises with the exact field path;
    * a level that exists but is not in the selected model's list (e.g.
      a saved ``ultra`` after switching to GLM-5.3) is *normalized* to
      that model's default rather than raised — a stale saved level must
      never brick ``GLMProvider.__init__``.

    Unknown model IDs have no authoritative list, so any known level is
    accepted and passed through; ``GLMProvider`` omits the wire field for
    them entirely.
    """
    _validate_field_path(extra, "thinking", _THINKING_KEYS)
    thinking = extra.get("thinking") or {}
    enabled = _require_bool(thinking, "provider.extra.thinking.enabled") if "enabled" in thinking else True
    preserve = _require_bool(thinking, "provider.extra.thinking.preserve") if "preserve" in thinking else True
    effort = thinking.get("reasoning_effort", "max")
    if not isinstance(effort, str):
        raise ValueError(f"provider.extra.thinking.reasoning_effort must be a string, got {type(effort).__name__}")
    if effort not in ALL_THINKING_LEVELS:
        raise ValueError(
            f"provider.extra.thinking.reasoning_effort must be one of {sorted(ALL_THINKING_LEVELS)}, got {effort!r}"
        )
    if has_model_thinking_levels(model_id) and effort not in get_thinking_levels(model_id):
        effort = default_thinking_level(get_thinking_levels(model_id))
    return GLMThinkingConfig(enabled=enabled, reasoning_effort=effort, preserve=preserve)


def _parse_guard(extra: Mapping[str, Any]) -> GLMGuardConfig:
    _validate_field_path(extra, "degeneration_guard", _GUARD_KEYS)
    guard = extra.get("degeneration_guard") or {}
    enabled = _require_bool(guard, "provider.extra.degeneration_guard.enabled") if "enabled" in guard else True
    retry = (
        _require_bool(guard, "provider.extra.degeneration_guard.retry_without_thinking")
        if "retry_without_thinking" in guard
        else True
    )

    ceiling = guard.get("reasoning_token_ceiling", REASONING_TOKEN_CEILING_DEFAULT)
    if not isinstance(ceiling, int) or isinstance(ceiling, bool):
        raise ValueError(
            f"provider.extra.degeneration_guard.reasoning_token_ceiling must be an integer, "
            f"got {type(ceiling).__name__}"
        )
    if not (REASONING_TOKEN_CEILING_MIN <= ceiling <= REASONING_TOKEN_CEILING_MAX):
        raise ValueError(
            f"provider.extra.degeneration_guard.reasoning_token_ceiling={ceiling} "
            f"must be in [{REASONING_TOKEN_CEILING_MIN}, {REASONING_TOKEN_CEILING_MAX}]"
        )

    recovery = guard.get("recovery_max_tokens", RECOVERY_MAX_TOKENS_DEFAULT)
    if not isinstance(recovery, int) or isinstance(recovery, bool):
        raise ValueError(
            f"provider.extra.degeneration_guard.recovery_max_tokens must be an integer, got {type(recovery).__name__}"
        )
    if not (RECOVERY_MAX_TOKENS_MIN <= recovery <= RECOVERY_MAX_TOKENS_MAX):
        raise ValueError(
            f"provider.extra.degeneration_guard.recovery_max_tokens={recovery} "
            f"must be in [{RECOVERY_MAX_TOKENS_MIN}, {RECOVERY_MAX_TOKENS_MAX}]"
        )

    return GLMGuardConfig(
        enabled=enabled,
        reasoning_token_ceiling=ceiling,
        retry_without_thinking=retry,
        recovery_max_tokens=recovery,
    )


def parse_glm_extra(extra: Mapping[str, Any], model_id: str) -> GLMConfig:
    """Validate and normalize a ``provider.extra`` dict for the GLM dialect.

    Non-GLM ``extra`` dicts are not the parser's responsibility — callers
    gate on ``extra.get("dialect") == GLM_DIALECT`` first.  This keeps
    custom-provider extras opaque to the parser, per spec §12.2.

    The parser validates **saved intent** only.  The effective recovery
    cap (``min(recovery_max_tokens, model_max_output_tokens)``) is computed
    later at request-build time, so the user-saved value remains the source
    of truth when the active model changes.

    Raises ``ValueError`` with an exact field path on any unknown nested
    key, non-boolean boolean field, out-of-range numeric, or unknown
    ``reasoning_effort`` level.
    """
    if not isinstance(extra, Mapping):
        raise ValueError(f"provider.extra must be a dict, got {type(extra).__name__}")
    unknown = set(extra.keys()) - _EXTRA_KEYS
    if unknown:
        raise ValueError(f"provider.extra.{sorted(unknown)[0]}: unknown key")
    dialect = extra.get("dialect")
    if dialect != GLM_DIALECT:
        raise ValueError(f"provider.extra.dialect must be {GLM_DIALECT!r}, got {dialect!r}")

    thinking = _parse_thinking(extra, model_id)
    guard = _parse_guard(extra)

    endpoint_type = extra.get("endpoint_type", GLM_ENDPOINT_STANDARD)
    if not isinstance(endpoint_type, str):
        raise ValueError(f"provider.extra.endpoint_type must be a string, got {type(endpoint_type).__name__}")
    if endpoint_type not in GLM_ENDPOINT_VALUES:
        raise ValueError(
            f"provider.extra.endpoint_type must be one of {sorted(GLM_ENDPOINT_VALUES)}, got {endpoint_type!r}"
        )

    return GLMConfig(thinking=thinking, guard=guard, endpoint_type=endpoint_type)
