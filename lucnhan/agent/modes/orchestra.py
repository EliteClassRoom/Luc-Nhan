"""Orchestra mode runner: agent orchestration with four-tuple φ = ⟨I, C, T, M⟩ specialization."""

from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING

from ...core.logging import log_info
from ..orchestra import OrchestraConfig, OrchestraMainAgent
from ..turn import TurnEvent

if TYPE_CHECKING:
    from ..loop import AgentLoop


def run_orchestra_mode(
    loop: AgentLoop,
    user_message: str,
    system_prompt: str,
    tools_schema: list,
) -> Generator[TurnEvent, None, None]:
    """Run the agent in orchestra mode: delegate tasks to specialized sub-agents."""
    orchestra_config = OrchestraConfig.load()

    orch = OrchestraMainAgent(
        provider=loop.provider,
        tool_registry=loop.tools,
        config=loop.config,
        session=loop.session,
        orchestra_config=orchestra_config,
        skill_registry=loop.skills,
        host_name=loop.host_name,
        parent_loop=loop,
    )

    log_info(f"Orchestra mode started: main_model={orchestra_config.main_model}")

    yield from orch.run(user_message)
