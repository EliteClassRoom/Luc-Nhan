"""A2A (Agent-to-Agent) subsystem for external agent integration.

Provides:
- A2AClient: HTTP client for A2A-compatible agents
- SubprocessBridge: Bridge to CLI agents (Claude Code, Codex)
- ExternalAgentRegistry: Auto-discovery and management of external agents
- A2ADispatcher: High-level facade used by the tool/UI/slash entry points
"""

from __future__ import annotations

from .client import A2AClient, A2AClientConfig
from .dispatcher import A2ADispatcher
from .registry import ExternalAgentRegistry
from .subprocess_bridge import SubprocessBridge
from .types import (
    A2AEvent,
    A2AEventType,
    A2ATask,
    A2ATaskStatus,
    ExternalAgentConfig,
)

__all__ = [
    "A2AClient",
    "A2AClientConfig",
    "A2ADispatcher",
    "A2AEvent",
    "A2AEventType",
    "A2ATask",
    "A2ATaskStatus",
    "ExternalAgentConfig",
    "ExternalAgentRegistry",
    "SubprocessBridge",
]
