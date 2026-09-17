"""System prompt builder with binary context awareness."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.logging import log_debug
from ..core.profile import IOC_FILTER_CATEGORIES
from ..core.sanitize import MEMORY_MAX_CHARS, quote_untrusted, sanitize_binary_context, sanitize_memory
from ..tools.catalog import format_tools_catalog as _format_tools_catalog  # re-export
from .prompts.ida import IDA_BASE_PROMPT

_BASE_PROMPT = IDA_BASE_PROMPT  # backward compat alias

if TYPE_CHECKING:
    from ..core.profile import AnalysisProfile


# Re-export so callers that still import ``format_tools_catalog`` from
# ``agent.system_prompt`` keep working. ``ToolRegistry`` now reads it
# directly from ``rikugan.tools.catalog`` to avoid the
# agent <-> tools round-trip during tool registration.
format_tools_catalog = _format_tools_catalog


def build_system_prompt(
    host_name: str = "IDA Pro",
    binary_info: str | None = None,
    current_function: str | None = None,
    current_address: str | None = None,
    extra_context: str | None = None,
    active_goal: str | None = None,
    tool_names: list[str] | None = None,
    skill_summary: str | None = None,
    profile: AnalysisProfile | None = None,
    tools_table: str | None = None,
    structured_memory: str = "",
    manual_memory_notes: str = "",
    hide_strings: bool = False,
) -> str:
    """Build the full system prompt with optional binary context."""
    base_prompt = IDA_BASE_PROMPT
    parts = [base_prompt]

    # Central memory: structured facts from SQLite + manual notes from
    # MEMORY.md unmanaged region. Both supplied by BinaryMemoryService.
    if structured_memory:
        parts.append("\n" + quote_untrusted(structured_memory, "structured_memory", max_length=MEMORY_MAX_CHARS))
    if manual_memory_notes:
        parts.append(f"\n## Manual Notes\n{sanitize_memory(manual_memory_notes)}")

    if active_goal:
        parts.append(
            "\n## Active Goal\n"
            "Use this as the standing analysis objective for the current session.\n"
            + quote_untrusted(active_goal, "active_goal", max_length=1000)
        )

    # Binary context is untrusted — function names, strings, and metadata
    # originate from the analyzed binary and could contain adversarial content.
    # When profile.hide_binary_metadata is set, skip binary context entirely.
    if profile and profile.hide_binary_metadata:
        log_debug("Profile: hiding binary metadata from system prompt")
    else:
        if binary_info:
            parts.append(f"\n## Current Binary\n{sanitize_binary_context(binary_info, 'binary_info')}")

        if current_address:
            parts.append(
                f"\n## Current Position\nAddress: {sanitize_binary_context(current_address, 'cursor_address')}"
            )
            if current_function:
                parts.append(f"Function: {sanitize_binary_context(current_function, 'cursor_function')}")

    if tools_table:
        # Prefer the categorized catalog — it gives the LLM category
        # grouping + one-line description hints so it can pick the right
        # tool without scanning the full provider schema.
        parts.append(f"\n{tools_table}")
    elif tool_names:
        # Fallback: comma-separated names only. Less useful but still
        # better than nothing for hosts that do not pre-compute a table.
        parts.append(f"\n## Available Tools\n{', '.join(tool_names)}")

    if skill_summary:
        parts.append("\n## Skills\n" + quote_untrusted(skill_summary, "skill_summary"))

    if extra_context:
        parts.append(f"\n## Additional Context\n{extra_context}")

    # Profile-driven prompt additions
    if profile:
        if profile.singular_analysis:
            parts.append(
                "\n## Analysis Constraint\n"
                "You are operating in singular analysis mode. "
                "Focus only on the specific question asked. "
                "Do not reference or cross-correlate with other binaries, "
                "samples, or external threat intelligence."
            )
        if profile.custom_filters:
            parts.append("\n## Profile Instructions\n" + "\n".join(profile.custom_filters))
        if profile.denied_functions:
            parts.append(
                "\n## Restricted Functions\n"
                "Do NOT call or reference the following functions in your analysis:\n"
                + "\n".join(f"- {fn}" for fn in profile.denied_functions)
            )

        # Profile awareness — tell the agent about the active profile
        if profile.name != "default":
            section = f"\n## Active Profile: {profile.name}\n"
            if profile.description:
                section += f"{profile.description}\n\n"
            section += (
                "You are operating under this analysis profile. "
                "The user has configured specific constraints and data filters. "
                "Respect these constraints in your analysis and output.\n"
            )
            if profile.has_any_ioc_filter:
                active = [
                    IOC_FILTER_CATEGORIES[k] for k, v in profile.ioc_filters.items() if v and k in IOC_FILTER_CATEGORIES
                ]
                if active:
                    section += (
                        "\nIOC filtering is active — the following are automatically redacted:\n"
                        + "\n".join(f"- {f}" for f in active)
                        + "\n\nIMPORTANT CONSTRAINTS:\n"
                        "- Do NOT attempt to reconstruct or reference original values "
                        "behind redaction markers.\n"
                        "- Hex-encoded data (hexdumps, raw bytes) is also sanitized — "
                        "do NOT decode hex bytes to recover filtered IOC data.\n"
                        "- Do NOT use read_bytes or memory dumps to circumvent IOC filters.\n"
                        "- If a value has been redacted, treat it as permanently unavailable.\n"
                    )
            parts.append(section)
    # Optional: emit a hide-strings constraint when the user disabled
    # string-list/search tools. The rule names the exact tools and the
    # disassembly/decompiler fallbacks so the agent does not waste a
    # turn trying the hidden tools.
    if hide_strings:
        parts.append(
            "\n## String Analysis Constraint\n"
            "String analysis is disabled by configuration. "
            "Do not call list_strings or search_strings; analyze behavior with "
            "read_function_disassembly, read_disassembly, decompile_function, "
            "or get_pseudocode instead. If decompilation fails or is unavailable, "
            "fall back to disassembly."
        )

    return "\n".join(parts)
