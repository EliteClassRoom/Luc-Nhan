"""MCP server configuration: load and save mcp.json from the Luc Nhan config directory."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from ..constants import CONFIG_DIR_NAME, MCP_CONFIG_FILE
from ..core.host import get_user_config_base_dir
from ..core.logging import log_debug, log_error


def _default_mcp_config_path() -> str:
    """Compute the default MCP config path without instantiating LucNhanConfig."""
    return os.path.join(get_user_config_base_dir(), CONFIG_DIR_NAME, MCP_CONFIG_FILE)


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout: float = 30.0

    def resolve_timeout(self, fallback: float) -> float:
        """Return the configured timeout, or *fallback* when unset/invalid (<= 0)."""
        return self.timeout if self.timeout > 0 else fallback


def load_mcp_config(path: str = "") -> list[MCPServerConfig]:
    """Load MCP server configurations from the Luc Nhan config directory.

    When *path* is not given, computes the default from the host config
    base directory (e.g. ``~/.idapro/lucnhan/mcp.json``).

    Returns an empty list if the file doesn't exist (graceful no-op).
    """
    if not path:
        path = _default_mcp_config_path()

    if not os.path.isfile(path):
        log_debug(f"MCP config not found: {path}")
        return []

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log_error(f"Failed to load MCP config: {e}")
        return []

    servers_dict = data.get("mcpServers", {})
    servers: list[MCPServerConfig] = []

    for name, cfg in servers_dict.items():
        if not isinstance(cfg, dict):
            continue
        server = MCPServerConfig(
            name=name,
            command=cfg.get("command", ""),
            args=cfg.get("args", []),
            env=cfg.get("env", {}),
            enabled=cfg.get("enabled", True),
            timeout=float(cfg.get("timeout", 30.0)),
        )
        if server.command:
            servers.append(server)
            log_debug(f"MCP server config: {name} cmd={server.command}")
        else:
            log_error(f"MCP server {name}: missing 'command', skipping")

    return servers


def save_mcp_config(servers: list[MCPServerConfig], path: str = "") -> None:
    """Save MCP server configurations back to disk."""
    if not path:
        path = _default_mcp_config_path()

    servers_dict: dict[str, dict] = {}
    for s in servers:
        entry: dict = {
            "command": s.command,
            "args": s.args,
            "env": s.env,
            "enabled": s.enabled,
        }
        if s.timeout != 30.0:
            entry["timeout"] = s.timeout
        servers_dict[s.name] = entry

    data = {"mcpServers": servers_dict}

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
