"""External agent registry — discovers and manages A2A and subprocess agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ...core.logging import log_debug
from .client import A2AClient
from .subprocess_bridge import SubprocessBridge
from .types import ExternalAgentConfig

_ORCHESTRA_CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "orchestra.toml"


@dataclass
class ExternalAgentRegistry:
    """Registry of external agents (A2A and subprocess-based).

    Auto-discovers CLI agents on PATH and loads user-configured A2A
    agents from the orchestra.toml configuration file.
    """

    agents: list[ExternalAgentConfig] = field(default_factory=list)
    _bridge: SubprocessBridge = field(default_factory=SubprocessBridge, init=False)

    def discover(
        self,
        config_a2a_agents: list[dict[str, Any]] | None = None,
    ) -> list[ExternalAgentConfig]:
        """Discover all available external agents.

        Runs auto-discovery for CLI agents and loads A2A agents from
        config (TOML on disk, plus any explicit entries passed in via
        ``config_a2a_agents`` — typically from LucNhanConfig).
        """
        discovered: list[ExternalAgentConfig] = []

        # Auto-detect CLI agents on PATH
        discovered.extend(self._bridge.discover())

        # Load user-configured A2A agents: explicit list first, then
        # orchestra.toml. Either source can be empty; the discovery is
        # the union.
        if config_a2a_agents:
            discovered.extend(self._materialize_a2a_agents(config_a2a_agents))
        discovered.extend(self._load_a2a_agents())

        self.agents = discovered
        return discovered

    def _materialize_a2a_agents(self, specs: list[dict[str, Any]]) -> list[ExternalAgentConfig]:
        """Build ExternalAgentConfig objects from in-memory spec dicts.

        Each spec must include at minimum ``name`` and ``endpoint``.
        We attempt a live ``.well-known/agent.json`` lookup; if it
        fails we still register the agent because the user explicitly
        listed it (better to surface a runtime error than silently
        drop the config).
        """
        out: list[ExternalAgentConfig] = []
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            endpoint = spec.get("endpoint", "")
            if not endpoint:
                continue
            try:
                client = A2AClient(endpoint)
                cfg = client.discover(endpoint)
                if cfg is not None:
                    cfg.name = spec.get("name", cfg.name)
                    cfg.model = spec.get("model", cfg.model)
                    out.append(cfg)
                    continue
            except Exception as e:
                # Best-effort discovery: fall back to a manual config below.
                log_debug(f"a2a registry discover failed for {endpoint}: {e}")
            out.append(
                ExternalAgentConfig(
                    name=spec.get("name", "unknown"),
                    transport="a2a",
                    endpoint=endpoint,
                    capabilities=spec.get("capabilities", []),
                    model=spec.get("model", ""),
                )
            )
        return out

    def _load_a2a_agents(self) -> list[ExternalAgentConfig]:
        """Load A2A agents from orchestra.toml [[a2a.agents]] sections."""
        import tomllib

        agents: list[ExternalAgentConfig] = []
        if not _ORCHESTRA_CONFIG_PATH.exists():
            return agents

        try:
            with open(_ORCHESTRA_CONFIG_PATH, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            return agents

        a2a_config = data.get("a2a", {})
        if not a2a_config:
            return agents

        return self._materialize_a2a_agents(a2a_config.get("agents", []))

    def list_agents(self) -> list[ExternalAgentConfig]:
        """Return all discovered agents (runs discover if empty)."""
        if not self.agents:
            self.discover()
        return self.agents

    def get_by_name(self, name: str) -> ExternalAgentConfig | None:
        """Find an agent by exact name."""
        for agent in self.list_agents():
            if agent.name == name:
                return agent
        return None

    def get_by_transport(self, transport: Literal["a2a", "subprocess"]) -> list[ExternalAgentConfig]:
        """Return agents filtered by transport type."""
        return [a for a in self.list_agents() if a.transport == transport]
