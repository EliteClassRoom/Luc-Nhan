"""AOrchestra: agent orchestration with four-tuple φ = ⟨I, C, T, M⟩ specialization."""

from __future__ import annotations

from .context import build_subagent_context, sanitize_context
from .main_agent import OrchestraMainAgent
from .orchestra_config import OrchestraConfig, SubAgentSpec
from .subagent_factory import SubAgentFactory

__all__ = [
    "OrchestraConfig",
    "OrchestraMainAgent",
    "SubAgentFactory",
    "SubAgentSpec",
    "build_subagent_context",
    "sanitize_context",
]
