"""Bridge MCP tools into Luc Nhan ToolRegistry."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..constants import MCP_TOOL_PREFIX
from ..core.logging import log_info
from ..core.sanitize import strip_injection_markers
from ..tools.base import ParameterSchema, ToolDefinition

if TYPE_CHECKING:
    from ..tools.registry import ToolRegistry
    from .client import MCPClient
else:
    ToolRegistry = Any
    MCPClient = Any


def _mcp_schema_to_parameters(input_schema: dict[str, Any]) -> list[ParameterSchema]:
    """Convert a JSON Schema object to a list of ParameterSchema."""
    params: list[ParameterSchema] = []

    properties = input_schema.get("properties", {})
    required = set(input_schema.get("required", []))

    for name, prop in properties.items():
        json_type = prop.get("type", "string")
        # Normalize array types
        if isinstance(json_type, list):
            json_type = json_type[0] if json_type else "string"

        ps = ParameterSchema(
            name=name,
            type=json_type,
            description=prop.get("description", ""),
            required=name in required,
            default=prop.get("default"),
            enum=prop.get("enum"),
            items=prop.get("items"),
        )
        params.append(ps)

    return params


# Underscores only: MCP names and server names both use "-" and ".", and the
# rest of the codebase already normalizes those to underscores.
_UNSAFE_NAME_CHARS_RE = re.compile(r"[^a-zA-Z0-9_]")
# Provider function-name limit (OpenAI and Anthropic both enforce 64).
_MAX_TOOL_NAME = 64
# Used when a tool name is entirely non-ASCII and sanitizes to nothing.
_TOOL_NAME_PLACEHOLDER = "tool"
# Longest readable part of a server-derived tool prefix.
_MAX_SERVER_NAME = 24


def safe_server_name(name: str) -> str:
    """Return the provider-safe, unique prefix segment for a server name.

    Two servers may normalize to the same readable part (``a-b`` and ``a.b``
    both become ``a_b``); the hash suffix keeps their tool prefixes distinct,
    so one server's cleanup can never unregister the other's tools.  The
    manager and this module must use this single function — a second
    normalizer is what produced the mismatch this replaces.
    """
    safe = _UNSAFE_NAME_CHARS_RE.sub("_", name)[:_MAX_SERVER_NAME].strip("_") or "server"
    return f"{safe}_{hashlib.sha1(name.encode('utf-8')).hexdigest()[:6]}"


def _make_mcp_handler(client: MCPClient, tool_name: str, registered_name: str) -> Callable:
    """Create a closure that calls client.call_tool() for the given tool."""

    def handler(**kwargs: Any) -> str:
        return client.call_tool(tool_name, kwargs)

    handler.__name__ = registered_name
    handler.__doc__ = f"MCP tool: {tool_name} (server: {client.name})"
    return handler


def _registered_tool_name(prefix: str, tool_name: str, registry: ToolRegistry) -> str:
    """Return a provider-safe, collision-free ``<prefix><tool_name>``.

    MCP servers pick their own tool names and may use characters providers
    reject (a space, a dot) or names long enough that the prefixed result
    exceeds the 64-character function-name limit, which fails the whole
    request. Sanitize, cap, and de-duplicate instead.
    """
    safe_prefix = _UNSAFE_NAME_CHARS_RE.sub("_", prefix)[: _MAX_TOOL_NAME - 1]
    budget = _MAX_TOOL_NAME - len(safe_prefix)
    tool_part = _UNSAFE_NAME_CHARS_RE.sub("_", tool_name)[:budget].strip("_")
    if not tool_part:
        tool_part = _TOOL_NAME_PLACEHOLDER[:budget]
    base = safe_prefix + tool_part
    name = base
    counter = 2
    while registry.get(name) is not None:
        suffix = f"_{counter}"
        name = base[: _MAX_TOOL_NAME - len(suffix)] + suffix
        counter += 1
    return name


def register_mcp_tools(client: MCPClient, registry: ToolRegistry, prefix: str = "") -> int:
    """Register all tools from an MCP client into the Luc Nhan ToolRegistry.

    Returns the number of tools registered.
    """
    if not prefix:
        prefix = f"{MCP_TOOL_PREFIX}{safe_server_name(client.name)}_"

    tools = client.get_tools()
    count = 0

    for mcp_tool in tools:
        lucnhan_name = _registered_tool_name(prefix, mcp_tool.name, registry)
        # Server-supplied text: strip injection markers before it rides along
        # in every request's tool schema. Capped because it is repeated per call.
        clean_desc = " ".join(strip_injection_markers(mcp_tool.description).split())
        description = f"[MCP:{client.name}] {clean_desc}"[:73]
        parameters = _mcp_schema_to_parameters(mcp_tool.input_schema)
        handler = _make_mcp_handler(client, mcp_tool.name, lucnhan_name)
        # An MCP server is untrusted: its ``annotations`` are the only signal
        # about what a tool does. Honour them, but default to gated — a tool
        # only escapes approval when the server says it is read-only and not
        # destructive. ``mutating`` also takes the registry mutation lock and
        # drops the result cache, so it is not set for declared-read-only
        # tools (which keeps subagents and headless from stalling).
        gated = (not mcp_tool.read_only_hint) or mcp_tool.destructive_hint

        defn = ToolDefinition(
            name=lucnhan_name,
            description=description,
            parameters=parameters,
            category=f"mcp:{client.name}",
            handler=handler,
            mutating=gated,
            requires_approval=gated,
        )
        registry.register(defn)
        count += 1

    log_info(f"Registered {count} MCP tools from {client.name} (prefix={prefix})")
    return count
