"""SubAgent factory — creates specialized sub-agents from SubAgentSpec."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..subagent_manager import SubagentManager, SubagentStatus
from .orchestra_config import OrchestraConfig, SubAgentSpec

if TYPE_CHECKING:
    pass


class SubAgentFactory:
    """Factory for creating specialized sub-agents from SubAgentSpec."""

    def __init__(
        self,
        manager: SubagentManager,
        config: OrchestraConfig,
    ) -> None:
        self._manager = manager
        self._config = config

    def spawn(self, spec: SubAgentSpec) -> str:
        """Spawn a sub-agent from a SubAgentSpec.

        Returns the agent_id.

        If spec.mode is set to "exploration", "explore", "plan", or "research",
        the sub-agent will run in that mode instead of the normal agent loop.
        """
        return self._manager.spawn(
            name=spec.name,
            task=spec.instruction,
            agent_type="orchestra",
            parent_id=None,
            perks=[],  # Orchestra sub-agents use explicit tools, not perks
            max_turns=spec.max_steps,
            category="orchestra",
            mode=spec.mode,
            tools=spec.tools,
            model=spec.model,
        )

    def spawn_with_context(
        self,
        spec: SubAgentSpec,
        full_context: str,
    ) -> str:
        """Spawn a sub-agent with pre-built context.

        This wraps the SubAgentSpec instruction with the context,
        creating a complete task for the sub-agent.
        """
        complete_task = f"{spec.instruction}\n\n## Context\n{full_context}"

        return self._manager.spawn(
            name=spec.name,
            task=complete_task,
            agent_type="orchestra",
            parent_id=None,
            perks=[],
            max_turns=spec.max_steps,
            category="orchestra",
            tools=spec.tools,
            model=spec.model,
        )

    def register_external(self, spec: SubAgentSpec) -> str:
        """Register an externally-managed sub-agent for tracking.

        Use this for sub-agents that are spawned outside of SubagentManager's
        thread pool (e.g., in an external process).
        """
        return self._manager.register(
            name=spec.name,
            task=spec.instruction,
            agent_type="orchestra",
            parent_id=None,
            perks=[],
            category="orchestra",
        )

    def update_external(
        self,
        agent_id: str,
        status: SubagentStatus,
        summary: str = "",
        turn_count: int = 0,
    ) -> None:
        """Update state of an externally managed sub-agent."""
        self._manager.update_external(agent_id, status, summary, turn_count)
