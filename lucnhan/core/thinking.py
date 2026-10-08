"""Provider-neutral thinking-level registry.

The Settings dialog exposes a single **Thinking** control for every
provider.  Which levels a given model accepts is answered by a local
per-model table — no provider API in this codebase advertises thinking
capability (the model-list endpoints return IDs only), so the table *is*
the capability query.

Adding support for a newly released model is one line here: add its
exact (lowercased) ID to :data:`_MODEL_THINKING_LEVELS` with the ordered
levels the endpoint accepts.  Models absent from the table fall back to
:data:`DEFAULT_THINKING_LEVELS`, which spans the full range from "none"
(thinking off) through the highest documented level; the chosen value is
then sent as-is and any endpoint rejection surfaces through normal
provider error handling.

Related: :mod:`lucnhan.core.glm_config` validates the persisted level
against this table, and ``GLMProvider`` uses
:func:`has_model_thinking_levels` to decide whether to put
``reasoning_effort`` on the wire at all.
"""

from __future__ import annotations

#: Levels offered for models with no table entry — every level Luc Nhan
#: knows about, from "none" (thinking disabled) to the highest one.
DEFAULT_THINKING_LEVELS: tuple[str, ...] = ("none", "low", "medium", "high", "max", "ultra")

#: Exact model ID (lowercased) -> ordered list of levels the endpoint
#: accepts.  Models absent here fall back to DEFAULT_THINKING_LEVELS.
_MODEL_THINKING_LEVELS: dict[str, tuple[str, ...]] = {
    "glm-5.2": ("none", "minimal", "low", "medium", "high", "xhigh", "max"),
    "glm-5.3": ("high", "max"),
}

#: Every level string that may appear in either the default list or a
#: per-model list.  Used by the GLM config parser to reject hand-edited
#: garbage without having to know the selected model's list.
ALL_THINKING_LEVELS: frozenset[str] = frozenset(DEFAULT_THINKING_LEVELS) | frozenset(
    level for levels in _MODEL_THINKING_LEVELS.values() for level in levels
)


def get_thinking_levels(model_id: str) -> tuple[str, ...]:
    """Return the ordered thinking levels supported by ``model_id``.

    Exact match on the lowercased / stripped ID; unknown or empty IDs
    return :data:`DEFAULT_THINKING_LEVELS`.
    """
    return _MODEL_THINKING_LEVELS.get((model_id or "").strip().lower(), DEFAULT_THINKING_LEVELS)


def has_model_thinking_levels(model_id: str) -> bool:
    """True iff ``model_id`` has an explicit table entry.

    The capability is therefore *known*: the level list is authoritative
    rather than a permissive fallback, and the wire may safely carry
    ``reasoning_effort``.
    """
    return (model_id or "").strip().lower() in _MODEL_THINKING_LEVELS


def default_thinking_level(levels: tuple[str, ...]) -> str:
    """Return the level to preselect for ``levels``.

    ``"high"`` when the model supports it (a sane mid-to-high default),
    otherwise the last non-``"none"`` entry of the ordered list.  Returns
    ``"none"`` when the list offers nothing but "none".
    """
    if "high" in levels:
        return "high"
    for level in reversed(levels):
        if level != "none":
            return level
    return "none"
