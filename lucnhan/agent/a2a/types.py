"""Data types for agent-to-agent (A2A) integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class A2ATaskStatus(str, Enum):
    """Status of an A2A task dispatched to an external agent."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


@dataclass
class ExternalAgentConfig:
    """Configuration for an external agent discovered or registered."""

    name: str
    transport: Literal["a2a", "subprocess"]
    endpoint: str = ""
    capabilities: list[str] = field(default_factory=list)
    model: str = ""
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class A2ATask:
    """A task dispatched to an external agent."""

    id: str
    agent_name: str
    prompt: str
    context: str = ""
    status: A2ATaskStatus = A2ATaskStatus.PENDING
    result: str = ""
    error: str = ""
    created_at: float = 0.0
    completed_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class A2AEventType(str, Enum):
    """Event types emitted by the A2A subsystem."""

    TASK_STARTED = "task_started"
    TASK_OUTPUT = "task_output"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    AGENT_DISCOVERED = "agent_discovered"
    AGENT_LOST = "agent_lost"


@dataclass
class A2AEvent:
    """Event emitted during A2A communication.

    ``type`` is typed as ``str`` (not ``A2AEventType``) because the
    ``SubprocessBridge`` transport yields informal values
    (``"stdout"``, ``"completed"``, ``"error"``, ``"cancelled"``)
    while the ``A2AClient`` transport yields ``A2AEventType`` enum
    members (which are also ``str`` since the enum subclasses ``str``).
    Both paths flow through the same dispatcher and ``type`` field.
    """

    type: str
    task_id: str = ""
    agent_name: str = ""
    text: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    done: bool = False  # terminal flag — last event of a stream
