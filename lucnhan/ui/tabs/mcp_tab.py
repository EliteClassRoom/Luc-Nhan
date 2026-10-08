"""MCP settings tab: enable/disable Luc Nhan and external MCP servers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...core.config import LucNhanConfig
from ...core.logging import log_debug
from ...mcp.config import MCPServerConfig
from ..qt_compat import (
    QCheckBox,
    QGroupBox,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from ..settings_service import SettingsService


class MCPTab(QWidget):
    """Tab for managing MCP servers: Luc Nhan configured + external MCP."""

    def __init__(self, config: LucNhanConfig, service: SettingsService, parent: QWidget | None = None):
        super().__init__(parent)
        self._config = config
        self._service = service
        self._lucnhan_checks: dict[str, QCheckBox] = {}
        self._external_checks: dict[str, QCheckBox] = {}
        self._lucnhan_servers: list[MCPServerConfig] = list(service.mcp.lucnhan)
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        layout = QVBoxLayout(container)

        # Luc Nhan MCP servers (pre-loaded by service)
        lucnhan_group = self._build_lucnhan_group()
        layout.addWidget(lucnhan_group)

        # External MCP (pre-loaded by service)
        for source_key, servers in sorted(self._service.mcp.external.items()):
            group = self._build_external_group(source_key, servers)
            layout.addWidget(group)

        layout.addStretch()
        scroll.setWidget(container)
        outer.addWidget(scroll)

    def _build_lucnhan_group(self) -> QGroupBox:
        """Build the Luc Nhan MCP servers group box."""
        group = QGroupBox("Luc Nhan MCP Servers")
        layout = QVBoxLayout(group)

        if not self._lucnhan_servers:
            layout.addWidget(QLabel("No MCP servers configured"))
            return group

        for server in sorted(self._lucnhan_servers, key=lambda s: s.name):
            cb = QCheckBox(f"{server.name}  —  {server.command}")
            cb.setChecked(server.enabled)
            self._lucnhan_checks[server.name] = cb
            layout.addWidget(cb)

        return group

    def _build_external_group(self, source_key: str, servers: list[MCPServerConfig]) -> QGroupBox:
        """Build a group box for external MCP servers from one source."""
        if source_key == "claude":
            title = "Claude Code MCP Servers"
        elif source_key == "codex":
            title = "Codex MCP Servers"
        else:
            title = f"{source_key} MCP Servers"

        group = QGroupBox(title)
        layout = QVBoxLayout(group)

        if not servers:
            layout.addWidget(QLabel("No MCP servers found"))
            return group

        enabled_set = set(self._config.enabled_external_mcp)

        for server in sorted(servers, key=lambda s: s.name):
            ext_id = f"{source_key}:{server.name}"
            cb = QCheckBox(f"{server.name}  —  {server.command}")
            cb.setChecked(ext_id in enabled_set)
            self._external_checks[ext_id] = cb
            layout.addWidget(cb)

        return group

    def apply_to_config(self, config: LucNhanConfig) -> None:
        """Write checkbox state back to config fields."""
        # Update Luc Nhan MCP server enabled state
        for server in self._lucnhan_servers:
            cb = self._lucnhan_checks.get(server.name)
            if cb is not None:
                server.enabled = cb.isChecked()

        # Persist Luc Nhan MCP config changes via the service
        if self._lucnhan_servers:
            self._service.save_mcp_servers(self._lucnhan_servers)

        # Enabled external MCP (checked = enabled)
        config.enabled_external_mcp = [ext_id for ext_id, cb in self._external_checks.items() if cb.isChecked()]

        log_debug(f"MCP config: {len(config.enabled_external_mcp)} external enabled")
