"""Settings service: pre-loads discovery data and owns persistence for settings tabs.

Created once by SettingsDialog before tabs are constructed. Tabs read from
pre-loaded properties instead of performing I/O in their constructors.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.config import LucNhanConfig
from ..core.logging import log_error
from ..mcp.config import MCPServerConfig, load_mcp_config, save_mcp_config
from ..skills.loader import SkillDefinition


@dataclass
class DiscoveredSkills:
    """Pre-loaded skill discovery results."""

    lucnhan: list[SkillDefinition] = field(default_factory=list)
    external: dict[str, list[SkillDefinition]] = field(default_factory=dict)


@dataclass
class DiscoveredMCP:
    """Pre-loaded MCP server discovery results."""

    lucnhan: list[MCPServerConfig] = field(default_factory=list)
    external: dict[str, list[MCPServerConfig]] = field(default_factory=dict)


class SettingsService:
    """Owns discovery, tool introspection, and MCP persistence for settings tabs.

    Tabs receive this service instead of doing I/O directly, giving the settings
    surface a clean controller boundary.
    """

    def __init__(self, config: LucNhanConfig, tool_registry=None):
        self._config = config
        self._skills = self._discover_skills()
        self._mcp = self._discover_mcp()
        self._tools_by_category = self._build_tools_by_category(tool_registry)

    @property
    def skills(self) -> DiscoveredSkills:
        return self._skills

    @property
    def mcp(self) -> DiscoveredMCP:
        return self._mcp

    @property
    def tools_by_category(self) -> dict[str, list[str]]:
        return self._tools_by_category

    def save_mcp_servers(self, servers: list[MCPServerConfig]) -> None:
        """Persist Luc Nhan MCP server configs to disk."""
        try:
            save_mcp_config(servers, self._config.mcp_config_path)
        except Exception as e:
            log_error(f"Failed to save MCP config: {e}")

    # ------------------------------------------------------------------
    # Internal discovery helpers
    # ------------------------------------------------------------------

    def _discover_skills(self) -> DiscoveredSkills:
        result = DiscoveredSkills()
        try:
            from ..skills.registry import SkillRegistry

            registry = SkillRegistry(self._config.skills_dir)
            registry.discover()
            result.lucnhan = registry.list_skills()
        except Exception as e:
            log_error(f"Failed to discover Luc Nhan skills: {e}")

        try:
            from ..core.external_sources import discover_all_external_skills

            result.external = discover_all_external_skills()
        except Exception as e:
            log_error(f"Failed to discover external skills: {e}")

        return result

    def _discover_mcp(self) -> DiscoveredMCP:
        result = DiscoveredMCP()
        try:
            result.lucnhan = load_mcp_config(self._config.mcp_config_path)
        except Exception as e:
            log_error(f"Failed to load Luc Nhan MCP config: {e}")

        try:
            from ..core.external_sources import discover_all_external_mcp

            result.external = discover_all_external_mcp()
        except Exception as e:
            log_error(f"Failed to discover external MCP: {e}")

        return result

    @staticmethod
    def _build_tools_by_category(tool_registry) -> dict[str, list[str]]:
        by_cat: dict[str, list[str]] = {}
        if tool_registry is None:
            return by_cat
        for defn in tool_registry.list_tools():
            cat = (defn.category or "general").capitalize()
            by_cat.setdefault(cat, []).append(defn.name)
        for cat in by_cat:
            by_cat[cat].sort()
        return by_cat
