"""Tool catalog formatting (categorized, markdown table).

Lives in the host-agnostic ``lucnhan.tools`` namespace so both
``ToolRegistry`` (which needs to cache the rendered string) and
``agent.system_prompt`` (which is the original caller) can import it
without creating a circular dependency.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.logging import log_debug

if TYPE_CHECKING:
    from .base import ToolDefinition


# Cap the per-tool description so the catalog table does not bloat the
# system prompt. The full description is still available via the
# provider tool schema; the catalog is for at-a-glance recall.
_CATALOG_DESC_MAX = 120
# Sentinel for tools whose description did not survive parsing (no
# docstring, decorator edge cases). Tells the LLM "this exists" without
# inventing a hint that may be wrong.
_UNKNOWN_DESC = "(no description)"


def format_tools_catalog(tools: list[ToolDefinition]) -> str:
    """Render a categorized, markdown-formatted catalog of available tools.

    The output groups tools by ``ToolDefinition.category`` (sorted
    alphabetically for stability) and lists each tool with a truncated
    one-line description. This is what we want in the system prompt's
    ``## Available Tools`` section — a comma-separated list of bare names
    gives the model no signal about which tool to reach for.

    Returns an empty string when *tools* is empty so callers can
    unconditionally include the result without a length check.
    """
    if not tools:
        return ""

    # Group by category. Sort categories and tool names within each
    # category for stable output (avoids spurious diffs in golden-file
    # tests and keeps the LLM's mental model consistent across runs).
    by_category: dict[str, list[ToolDefinition]] = {}
    for t in tools:
        by_category.setdefault(t.category, []).append(t)

    lines = ["## Available Tools", ""]
    for category in sorted(by_category):
        lines.append(f"### {category}")
        lines.append("")
        lines.append("| Tool | Description |")
        lines.append("| --- | --- |")
        for t in sorted(by_category[category], key=lambda x: x.name):
            desc = t.description.strip() if t.description else ""
            # Collapse internal newlines so each row stays a single Markdown line.
            desc = " ".join(desc.split())
            if len(desc) > _CATALOG_DESC_MAX:
                desc = desc[: _CATALOG_DESC_MAX - 1].rstrip() + "…"
            if not desc:
                desc = _UNKNOWN_DESC
            # Escape pipe characters so they do not break the table.
            safe_desc = desc.replace("|", "\\|")
            lines.append(f"| `{t.name}` | {safe_desc} |")
        lines.append("")
    out = "\n".join(lines).rstrip()
    log_debug(f"format_tools_catalog: {len(tools)} tools, {len(out)} chars")
    return out
