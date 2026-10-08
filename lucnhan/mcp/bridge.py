"""Bridge MCP tools into Luc Nhan ToolRegistry."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..constants import MCP_TOOL_PREFIX
from ..core.logging import log_info
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


def _make_mcp_handler(client: MCPClient, tool_name: str) -> Callable:
    """Create a closure that calls client.call_tool() for the given tool."""

    def handler(**kwargs: Any) -> str:
        return client.call_tool(tool_name, kwargs)

    handler.__name__ = f"mcp_{client.name}_{tool_name}"
    handler.__doc__ = f"MCP tool: {tool_name} (server: {client.name})"
    return handler


def register_mcp_tools(client: MCPClient, registry: ToolRegistry, prefix: str = "") -> int:
    """Register all tools from an MCP client into the Luc Nhan ToolRegistry.

    Returns the number of tools registered.
    """
    if not prefix:
        # Sanitize server name for use in tool names
        safe_name = client.name.replace("-", "_").replace(".", "_")
        prefix = f"{MCP_TOOL_PREFIX}{safe_name}_"

    tools = client.get_tools()
    count = 0

    for mcp_tool in tools:
        lucnhan_name = f"{prefix}{mcp_tool.name}"
        description = f"[MCP:{client.name}] {mcp_tool.description}"
        parameters = _mcp_schema_to_parameters(mcp_tool.input_schema)
        handler = _make_mcp_handler(client, mcp_tool.name)

        defn = ToolDefinition(
            name=lucnhan_name,
            description=description,
            parameters=parameters,
            category=f"mcp:{client.name}",
            handler=handler,
        )
        registry.register(defn)
        count += 1

    log_info(f"Registered {count} MCP tools from {client.name} (prefix={prefix})")
    return count
