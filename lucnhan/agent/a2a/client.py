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
import urllib.error
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

# Cap on a response body. The endpoint is external and untrusted, and
# ``resp.read()`` without a size reads whatever it is given.
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# Statuses worth another attempt: 429/5xx mean the server did not complete the
# work, 4xx means it rejected the request outright. ``tasks.send`` is not
# idempotent, so a retry re-runs the task under the same id — accepted cost.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _may_retry(error: Exception) -> bool:
    """True when *error* is worth another attempt."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code in _RETRYABLE_STATUS
    return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))


def _read_json(resp: Any) -> Any:
    """Decode a JSON response body, refusing an oversized one."""
    body = resp.read(_MAX_RESPONSE_BYTES + 1)
    if len(body) > _MAX_RESPONSE_BYTES:
        raise ValueError(f"A2A response exceeds {_MAX_RESPONSE_BYTES} bytes")
    return json.loads(body.decode("utf-8"))


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
        # A bare endpoint string is a frequent mistake: it satisfies the
        # signature at runtime but every ``main_retries`` read then raises.
        if isinstance(config, str):
            raise TypeError(f"A2AClient expects A2AClientConfig, got str {config!r}")
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
                data = _read_json(resp)
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

    def _pick_response(self, data: Any) -> dict[str, Any] | None:
        """Return the JSON-RPC envelope that answers this task, if any.

        A server may answer with a batch (a list) and, since the same task id
        is resent on every attempt, may answer with an envelope for an older
        attempt. Only a matching id — or an envelope carrying no id at all —
        is accepted; anything else must not be reported as this task's result.
        """
        envelope: dict[str, Any] | None
        if isinstance(data, dict):
            envelope = data
        elif isinstance(data, list):
            envelope = None
            for item in data:
                if isinstance(item, dict) and item.get("id") == self.task.id:
                    envelope = item
                    break
            if envelope is None:
                return None
        else:
            return None
        response_id = envelope.get("id")
        if response_id is not None and response_id != self.task.id:
            return None
        return envelope

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
                    response_data = _read_json(resp)

                envelope = self._pick_response(response_data)
                if envelope is None:
                    last_error = f"Response did not answer task {self.task.id}"
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

                if "result" in envelope:
                    result: Any = envelope["result"]
                    if isinstance(result, dict):
                        self.task.result = str(result.get("text", result.get("result", "")))
                    else:
                        self.task.result = str(result)
                    self.task.status = A2ATaskStatus.COMPLETED
                elif "error" in envelope:
                    err = envelope["error"]
                    last_error = str(err.get("message") or str(err) or type(err).__name__)
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
                else:
                    # A reply that is neither result nor error must not be
                    # reported as a success — the task never finished.
                    last_error = "Response carried neither result nor error"
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
                last_error = str(e) or type(e).__name__
                log_debug(f"A2A attempt {attempt + 1} failed for task {self.task.id}: {e}")
                if attempt >= self._config.max_retries - 1 or not _may_retry(e):
                    break
                # ``wait`` returns early on cancel, so a stop during the
                # backoff is not delayed by a full sleep.
                # ponytail: an idempotency risk stays — a request that reached
                # the server re-runs the task under the same id. Only
                # transport failures and explicit "retry me" statuses qualify.
                self._cancel.wait(self._config.retry_backoff * (attempt + 1))

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
