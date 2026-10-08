"""Context management for sub-agent context passing."""

from __future__ import annotations

from typing import Any

from .orchestra_config import SubAgentSpec

CHAR_TO_TOKEN_ESTIMATE = 4


def sanitize_context(context: str) -> str:
    """Sanitize context by stripping injection markers."""
    from ..core.sanitize import strip_injection_markers

    return strip_injection_markers(context)


def build_subagent_context(
    main_context: str,
    subtask_history: list[dict[str, Any]],
    max_chars: int = 100,
    enable_sharing: bool = True,
) -> str:
    """Build curated context for a sub-agent, respecting context window limits.

    Args:
        main_context: Current binary context (binary info, position, etc.)
        subtask_history: List of completed subtask results
        max_chars: Maximum characters to include
        enable_sharing: Whether to include completed subtask results
    """
    if not enable_sharing:
        return main_context[:max_chars]

    parts: list[str] = []

    if main_context:
        parts.append(main_context)

    completed_summaries: list[str] = []
    for entry in subtask_history[-5:]:
        name = entry.get("name", "?")
        result = entry.get("result", "")
        status = entry.get("status", "")
        if result:
            truncated = result[:200] + "..." if len(result) > 200 else result
            completed_summaries.append(f"[{name}] {status}: {truncated}")

    if completed_summaries:
        parts.append("\n\n## Completed Subtasks\n" + "\n".join(completed_summaries))

    context = "\n\n".join(parts)

    if len(context) > max_chars * 2:
        context = context[:max_chars] + f"\n\n...(truncated, total {len(context)} chars)"

    return sanitize_context(context)


def format_delegation_for_display(spec: SubAgentSpec) -> dict[str, Any]:
    """Format a SubAgentSpec for UI display."""
    tools_str = ", ".join(spec.tools) if spec.tools else "all available tools"
    context_preview = spec.context[:200] + "..." if len(spec.context) > 200 else spec.context

    return {
        "name": spec.name,
        "instruction": spec.instruction[:500] + "..." if len(spec.instruction) > 500 else spec.instruction,
        "model": spec.model or "default",
        "tools": tools_str,
        "tools_count": len(spec.tools),
        "context_preview": context_preview[:200] + "..." if len(context_preview) > 200 else context_preview,
        "max_steps": spec.max_steps,
    }
