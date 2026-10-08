"""A2A HTTP client for agent-to-agent communication over HTTPS + SSE.

Authentication model
--------------------
The A2A outbound HTTP client does **not** attach an ``Authorization`` header
by default. External agents are expected to be reachable over a network
path that provides its own authentication boundary:

* **Loopback / local network** — typical for Claude Code / Codex CLI
  subprocess transport; no auth needed.
* **Authenticated reverse proxy** — the operator fronts the agent endpoint
  with a TLS-terminating proxy that enforces bearer-token / mTLS auth
  before forwarding to the A2A server. The A2A server itself sees only
  authenticated traffic.
* **User-configured remote host** — operators are responsible for placing
  the endpoint behind appropriate network-level auth (VPN, IP allow-list,
  mTLS). Luc Nhan does not embed a token in outbound requests because
  ExternalAgentConfig does not currently expose an auth_token field.

If you need token-based auth directly from Luc Nhan to a remote A2A server
without an auth-fronting proxy, file an issue to extend
``ExternalAgentConfig`` with an ``auth_token`` field that propagates into
the request headers.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

from ...core.logging import log_debug, log_error
from .types import (
    A2AEvent,
    A2AEventType,
    A2ATask,
    A2ATaskStatus,
    ExternalAgentConfig,
)

# Default timeout for A2A requests (seconds)
_DEFAULT_TIMEOUT = 300


@dataclass
class A2AClientConfig:
    """Configuration for the A2A HTTP client."""

    timeout: int = _DEFAULT_TIMEOUT
    max_retries: int = 3
    retry_backoff: float = 1.0


class A2AClient:
    """HTTP client for A2A-compatible agents.

    Communicates via JSON-RPC over HTTPS with SSE streaming for task updates.
    """

    def __init__(self, config: A2AClientConfig | None = None) -> None:
        self._config = config or A2AClientConfig()
        self._sessions: dict[str, _A2ASession] = {}

    def discover(self, endpoint: str) -> ExternalAgentConfig | None:
        """Fetch agent metadata from an A2A agent card endpoint.

        Fetches ``endpoint/.well-known/agent.json`` to get agent capabilities,
        transport info, and default model.
        """
        agent_card_url = endpoint.rstrip("/") + "/.well-known/agent.json"
        try:
            req = urllib.request.Request(
                agent_card_url,
                headers={"User-Agent": "LucNhan-A2A/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return ExternalAgentConfig(
                name=data.get("name", "unknown"),
                transport="a2a",
                endpoint=endpoint,
                capabilities=data.get("capabilities", []),
                model=data.get("model", ""),
            )
        except Exception as e:
            log_error(f"A2A discover failed for {endpoint}: {e}")
            return None

    def send_task(
        self,
        agent: ExternalAgentConfig,
        prompt: str,
        context: str = "",
        event_callback: Any = None,
        cancel_event: threading.Event | None = None,
    ) -> A2ATask:
        """Send a task to an A2A agent and return immediately.

        The task is queued and monitored in a background thread. Events are
        delivered via ``event_callback`` if provided. Check task status via
        ``get_task()``.

        Args:
            cancel_event: Optional threading.Event. The session polls
                this between HTTP attempts; if it fires, the task is
                marked CANCELLED and no more requests are made.
        """
        task = A2ATask(
            id=uuid.uuid4().hex[:12],
            agent_name=agent.name,
            prompt=prompt,
            context=context,
        )
        task.created_at = time.time()

        session = _A2ASession(
            task=task,
            agent=agent,
            config=self._config,
            cancel_event=cancel_event,
            event_callback=event_callback,
        )
        self._sessions[task.id] = session
        session.start()
        return task

    def get_task(self, task_id: str) -> A2ATask | None:
        """Return the current state of a task."""
        session = self._sessions.get(task_id)
        return session.task if session else None

    def cancel_task(self, task_id: str) -> bool:
        """Request cancellation of a running task."""
        session = self._sessions.get(task_id)
        if session:
            session.cancel()
            return True
        return False

    def close(self) -> None:
        """Shut down all active sessions."""
        for session in list(self._sessions.values()):
            session.cancel()
        self._sessions.clear()


class _A2ASession:
    """Background session for monitoring an A2A task."""

    def __init__(
        self,
        task: A2ATask,
        agent: ExternalAgentConfig,
        config: A2AClientConfig,
        event_callback: Any = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.task = task
        self._agent = agent
        self._config = config
        self._callback = event_callback
        # External cancel event (if provided) is observed alongside the
        # internal cancel. We OR them so either source stops the session.
        self._cancel = cancel_event if cancel_event is not None else threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"a2a-session-{self.task.id[:6]}",
        )
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def _emit(self, event: A2AEvent) -> None:
        if self._callback:
            self._callback(event)

    def _run(self) -> None:
        """Execute the A2A task via JSON-RPC over HTTPS."""
        self._emit(
            A2AEvent(
                type=A2AEventType.TASK_STARTED,
                task_id=self.task.id,
                agent_name=self._agent.name,
            )
        )

        rpc_payload = {
            "jsonrpc": "2.0",
            "id": self.task.id,
            "method": "tasks.send",
            "params": {
                "id": self.task.id,
                "prompt": self.task.prompt,
            },
        }

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "LucNhan-A2A/1.0",
        }

        last_error = ""
        for attempt in range(self._config.max_retries):
            if self._cancel.is_set():
                self.task.status = A2ATaskStatus.CANCELLED
                self._emit(A2AEvent(type=A2AEventType.TASK_CANCELLED, task_id=self.task.id))
                return

            try:
                req = urllib.request.Request(
                    self._agent.endpoint,
                    data=json.dumps(rpc_payload).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self._config.timeout) as resp:
                    response_data = json.loads(resp.read().decode("utf-8"))

                if "result" in response_data:
                    result: Any = response_data["result"]
                    if isinstance(result, dict):
                        self.task.result = str(result.get("text", result.get("result", "")))
                        self.task.status = A2ATaskStatus.COMPLETED
                    else:
                        self.task.result = str(result)
                        self.task.status = A2ATaskStatus.COMPLETED
                elif "error" in response_data:
                    err = response_data["error"]
                    last_error = err.get("message", str(err))
                    self.task.error = last_error
                    self.task.status = A2ATaskStatus.FAILED
                    self._emit(
                        A2AEvent(
                            type=A2AEventType.TASK_FAILED,
                            task_id=self.task.id,
                            error=last_error,
                        )
                    )
                    return

                self.task.completed_at = time.time()
                elapsed = self.task.completed_at - self.task.created_at
                self._emit(
                    A2AEvent(
                        type=A2AEventType.TASK_COMPLETED,
                        task_id=self.task.id,
                        text=self.task.result,
                        metadata={"elapsed": elapsed},
                    )
                )
                return

            except Exception as e:
                last_error = str(e)
                log_debug(f"A2A attempt {attempt + 1} failed for task {self.task.id}: {e}")
                if attempt < self._config.max_retries - 1:
                    time.sleep(self._config.retry_backoff * (attempt + 1))

        # All retries exhausted
        self.task.status = A2ATaskStatus.FAILED
        self.task.error = f"Max retries exceeded: {last_error}"
        self._emit(
            A2AEvent(
                type=A2AEventType.TASK_FAILED,
                task_id=self.task.id,
                error=last_error,
            )
        )
