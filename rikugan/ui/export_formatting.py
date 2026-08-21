"""Markdown export formatting helpers for panel content.

Extracted from ``panel_core.py`` so the panel module stays focused on
layout and control flow. These helpers format tool calls, tool results,
and subagent logs into markdown for the export view.
"""

from __future__ import annotations

import re

from .. import constants
from ..core.types import Role

# Truncate exported tool-result previews so the exported transcript stays bounded.
_TOOL_RESULT_TRUNCATE_CHARS = 2000

# Strip the sanitization wrappers the LLM sees so exports show clean content.
_SANITIZER_TAG_RE = re.compile(
    r"^\[The following is (?:a tool execution result|output from an EXTERNAL MCP server)"
    r"[^\]]*\]\n?",
    re.MULTILINE,
)
_SANITIZER_WRAP_RE = re.compile(
    r"<(?:tool_result|mcp_result|binary_info|cursor_address|cursor_function|persistent_memory|retrieved_knowledge|conversation_context|binary_evidence|structured_memory|skill_summary|active_goal|skill)\b[^>]*>\n?"
    r"|</(?:tool_result|mcp_result|binary_info|cursor_address|cursor_function|persistent_memory|retrieved_knowledge|conversation_context|binary_evidence|structured_memory|skill_summary|active_goal|skill)>\n?",
)

_TOOL_LANG_MAP = {
    constants.EXECUTE_PYTHON_TOOL_NAME: "python",
    "decompile_function": "c",
    "get_il": "c",
    "declare_c_type": "c",
    "define_types": "c",
    "set_function_prototype": "c",
    "fetch_disassembly": "x86asm",
}


def _strip_sanitizer_tags(text: str) -> str:
    """Remove sanitization wrappers added for the LLM from exported content."""
    text = _SANITIZER_TAG_RE.sub("", text)
    text = _SANITIZER_WRAP_RE.sub("", text)
    return text.strip()


def _export_detect_lang(text: str, tool_name: str = "", arg_key: str = "") -> str:
    """Detect markdown language hint from content heuristics and tool/arg context."""
    if arg_key in ("code", "python"):
        return "python"
    if arg_key in ("c_code", "c_declaration", "prototype"):
        return "c"
    if tool_name in _TOOL_LANG_MAP:
        return _TOOL_LANG_MAP[tool_name]

    sample = text[:_TOOL_RESULT_TRUNCATE_CHARS]
    if re.search(r"^[0-9a-fA-F]{8,16}\s+([0-9a-fA-F]{2}\s+){4,}", sample, re.M):
        return "text"

    asm_pat = r"(?:mov|lea|push|pop|call|ret|jmp|je|jne|jz|jnz|cmp|test|xor|add|sub|nop|int)\s"
    if re.search(asm_pat, sample, re.I) and re.search(r"0x[0-9a-fA-F]+", sample):
        return "x86asm"

    c_indicators = 0
    if re.search(r"\b(void|int|char|uint\d+_t|int\d+_t|struct|enum|typedef)\b", sample):
        c_indicators += 1
    if re.search(r"[{};]", sample):
        c_indicators += 1
    if re.search(r"\b(if|while|for|return|switch)\s*\(", sample):
        c_indicators += 1
    if c_indicators >= 2:
        return "c"

    if re.search(r"^(def |class |import |from .+ import |print\()", sample, re.M):
        return "python"

    return ""


def _export_format_tool_args(tc) -> str:
    """Format tool call arguments as markdown with per-argument code blocks."""
    parts = []
    for k, v in tc.arguments.items():
        if isinstance(v, str) and ("\n" in v or len(v) > 80):
            lang = _export_detect_lang(v, tc.name, k)
            parts.append(f"  - `{k}`:\n\n```{lang}\n{v}\n```\n")
        else:
            parts.append(f"  - `{k}`: `{v!r}`")
    return "\n".join(parts)


def _export_format_tool_result(tr) -> str:
    """Format tool result content as a markdown code block."""
    content = _strip_sanitizer_tags(tr.content)
    if len(content) > _TOOL_RESULT_TRUNCATE_CHARS:
        content = content[:_TOOL_RESULT_TRUNCATE_CHARS] + "\n... (truncated)"
    lang = _export_detect_lang(content, tr.name)
    return f"```{lang}\n{content}\n```"


def _export_format_subagent_log(messages) -> str:
    """Format a subagent's message log as a collapsible markdown section."""
    tool_count = sum(len(m.tool_calls) for m in messages if m.role == Role.ASSISTANT)
    parts = [
        f"<details>\n<summary>Subagent Log ({tool_count} tool calls)</summary>\n",
    ]
    for msg in messages:
        if msg.role == Role.USER:
            parts.append(f"> **Task**: {msg.content}\n")
        elif msg.role == Role.ASSISTANT:
            if msg.content:
                parts.append(f"> **Subagent**:\n> {msg.content}\n")
            for tc in msg.tool_calls:
                parts.append(f"> **Tool call**: `{tc.name}`\n")
                parts.append(f"> {_export_format_tool_args(tc)}\n")
        elif msg.role == Role.TOOL:
            for tr in msg.tool_results:
                status = "Error" if tr.is_error else "Result"
                parts.append(f"> **{status}** (`{tr.name}`):\n")
                parts.append(f"> {_export_format_tool_result(tr)}\n")
    parts.append("</details>\n")
    return "\n".join(parts)
