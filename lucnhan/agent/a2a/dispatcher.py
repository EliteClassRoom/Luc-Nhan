"""A2A dispatcher — single entry point for delegating to external agents.

The dispatcher is consumed by three different entry points:
  1. ``delegate_external_task`` pseudo-tool in ``agent/loop.py``
  2. ``A2ABridgeWidget`` in ``ui/a2a_widget.py``
  3. ``/a2a`` slash command in ``agent/loop.py``

It abstracts over the two supported transports (``subprocess`` for
local CLIs like Claude Code / Codex, ``a2a`` for the JSON-RPC over
HTTPS protocol) and yields ``TurnEvent`` objects that the existing UI
machinery already knows how to render. Cancellation is wired through
``threading.Event`` so a user-cancel cleanly terminates subprocesses
and aborts HTTP retry loops.
"""

from __future__ import annotations

import threading
from collections.abc import Generator
from typing import Any

from ..turn import TurnEvent, TurnEventType
from .client import A2AClient, A2AClientConfig
from .registry import ExternalAgentRegistry
from .subprocess_bridge import SubprocessBridge
from .types import A2AEvent, A2ATaskStatus, ExternalAgentConfig

# Default cap on how many chars of stdout/event payload to surface to
# the LLM as a tool result. The full text is stored in session metadata
# (via the ``text`` field on the final ``completed`` event), but
# returning the entire transcript to the model would blow the context
# budget on long-running tasks.
_MAX_RESULT_CHARS = 8000


#: Statuses that mean "no more polling needed" — the task is in a
#: terminal state and the loop should exit regardless of the elapsed
#: timer.
_TERMINAL_TASK_STATUSES = frozenset(
    {
        A2ATaskStatus.COMPLETED,
        A2ATaskStatus.FAILED,
        A2ATaskStatus.CANCELLED,
        A2ATaskStatus.TIMEOUT,
    }
)


class A2ADispatcher:
    """High-level facade for delegating to external agents.

    Lifecycle is intentionally simple: instantiate once per session
    controller, share across UI/tool/CLI entry points. The registry is
    cached lazily on first ``discover()`` call.

    Threading: the registry and bridges are safe to call from any
    thread; the dispatcher does not maintain its own state beyond the
    cached registry instance. ``run_task`` does NOT spawn its own
    thread for subprocess runs — the SubprocessBridge generator
    cooperates with the caller's event loop so cancellation is
    responsive.
    """

    def __init__(
        self,
        *,
        auto_discover: bool = True,
        a2a_agents: list[dict[str, Any]] | None = None,
        timeout: int = 300,
    ) -> None:
        self._auto_discover = auto_discover
        # Config-declared A2A agents get merged with auto-discovered
        # ones in ``discover()``. We don't validate them here — the
        # registry's ``_load_a2a_agents`` does the .well-known lookup
        # to confirm the endpoint is real.
        self._config_a2a_agents = a2a_agents or []
        self._timeout = timeout
        self._registry: ExternalAgentRegistry | None = None
        # Lazy: only constructed when a 'subprocess' transport runs.
        # Avoids touching shutil/imports for callers that only call
        # ``discover()``.
        self._subprocess_bridge: SubprocessBridge | None = None
        # Lazy: only constructed when an 'a2a' transport runs.
        self._a2a_client: A2AClient | None = None

    # -- Lazy accessors -----------------------------------------------------

    def _get_subprocess_bridge(self) -> SubprocessBridge:
        if self._subprocess_bridge is None:
            self._subprocess_bridge = SubprocessBridge()
        return self._subprocess_bridge

    def _get_a2a_client(self) -> A2AClient:
        if self._a2a_client is None:
            self._a2a_client = A2AClient(A2AClientConfig(timeout=self._timeout))
        return self._a2a_client

    def _find_agent(self, name: str) -> ExternalAgentConfig | None:
        return next(
            (a for a in self.discover() if a.name == name),
            None,
        )

    # -- Discovery ----------------------------------------------------------

    def discover(self) -> list[ExternalAgentConfig]:
        """Return the list of available external agents (cached after first call).

        On first call, runs auto-discovery (PATH check for ``claude`` /
        ``codex``) plus loads any user-configured A2A agents from
        ``orchestra.toml`` and the in-memory ``a2a_agents`` list passed
        at construction. Subsequent calls return the cached list.
        """
        if self._registry is None:
            self._registry = ExternalAgentRegistry()
            if self._auto_discover:
                # Pass config-declared A2A agents so the registry
                # materializes them alongside TOML entries and
                # auto-discovered CLI agents.
                self._registry.discover(config_a2a_agents=self._config_a2a_agents)
        return list(self._registry.agents)

    # -- Delegation ---------------------------------------------------------

    def run_task(
        self,
        agent_name: str,
        task: str,
        *,
        cancel_event: threading.Event | None = None,
        include_context: str = "",
    ) -> Generator[TurnEvent, None, str]:
        """Run ``task`` on the named agent. Yields TurnEvents; returns final result.

        The caller is responsible for the event loop: events go to the
        UI's queue, the user sees streaming output, and a cancel
        forwards here via ``cancel_event`` to terminate subprocesses
        or skip HTTP retry attempts.

        Args:
            agent_name: Must be in ``discover()`` results.
            task: User/LLM-controlled text. Validated against argv
                injection for subprocess transport; passed through
                unchanged for A2A transport (HTTP body is JSON-encoded
                so shell quoting is not a concern there).
            cancel_event: Set to terminate early. Both the subprocess
                read loop and the A2A HTTP retry loop poll this.
            include_context: Optional context string (e.g. binary
                info) to prepend to the task. Useful for the widget's
                "include current context" option.

        Returns:
            The aggregated result string. For A2A transport this is
            the ``result.text`` field from the JSON-RPC response; for
            subprocess transport this is the last JSON line's
            ``content``/``result`` field (or the raw text if no JSON).
        """
        # Locate the agent.
        agents = self.discover()
        agent = self._find_agent(agent_name)
        if agent is None:
            yield TurnEvent.error_event(
                f"External agent '{agent_name}' not found. Available: {', '.join(a.name for a in agents) or '(none)'}"
            )
            return ""

        # Prepend optional context.
        full_task = task
        if include_context:
            full_task = f"{include_context}\n\n{task}"

        # Dispatch by transport. ``yield from`` propagates the inner
        # generator's events AND its return value (the aggregated
        # result text), so the outer ``run_task`` returns whatever
        # the inner call produced.
        if agent.transport == "subprocess":
            return (yield from self._run_subprocess(agent, full_task, cancel_event))
        if agent.transport == "a2a":
            return (yield from self._run_a2a(agent, full_task, cancel_event))

        yield TurnEvent.error_event(f"Agent '{agent.name}' has unknown transport: {agent.transport!r}")
        return ""

    # -- Transport implementations ------------------------------------------

    def _run_subprocess(
        self,
        agent: ExternalAgentConfig,
        task: str,
        cancel_event: threading.Event | None,
    ) -> Generator[TurnEvent, None, str]:
        """Run a subprocess transport, yielding TurnEvents from the bridge."""
        yield TurnEvent(
            type=TurnEventType.TEXT_DELTA,
            text=f"Delegating to {agent.name}...\n",
            tool_name="delegate_external_task",
            metadata={"agent": agent.name, "transport": agent.transport},
        )

        last_text = ""
        try:
            for event in self._get_subprocess_bridge().run_task(
                agent, task, timeout=self._timeout, cancel_event=cancel_event
            ):
                # Terminal error events from the bridge — surface as a
                # single TurnEvent.error_event and stop. We must NOT
                # fall through to ``_translate_subprocess_event`` and
                # silently keep the previous ``last_text`` as the
                # return value, because that path was the original bug
                # that hid subprocess failures from the caller.
                if event.type == "error":
                    yield TurnEvent.error_event(event.text or "External agent error")
                    return ""
                if event.type == "cancelled":
                    msg = event.text or "Task cancelled by user"
                    yield TurnEvent.error_event(msg)
                    return ""

                translated = self._translate_subprocess_event(agent, event)
                if translated is None:
                    # Unknown event type — ignore it but keep the loop
                    # running so we still observe the final ``completed``
                    # marker from the bridge.
                    continue
                last_text = translated
                yield TurnEvent(
                    type=TurnEventType.TEXT_DELTA,
                    text=translated,
                    tool_name="delegate_external_task",
                    metadata={
                        "agent": agent.name,
                        "transport": agent.transport,
                        "event_type": event.type,
                    },
                )
        except ValueError as e:
            # _validate_task in _build_command raises ValueError on
            # argv-injection-shaped tasks. Surface as a clear error
            # event so the LLM can rephrase.
            yield TurnEvent.error_event(str(e))
            return ""

        # ``last_text`` is the most recent line, but SubprocessBridge
        # yields a final ``completed`` event whose ``text`` is the
        # JSON-parsed result. Apply the same cap we use for the A2A
        # path so a runaway subprocess can't blow the LLM's context.
        return self._truncate(last_text)

    def _run_a2a(
        self,
        agent: ExternalAgentConfig,
        task: str,
        cancel_event: threading.Event | None,
    ) -> Generator[TurnEvent, None, str]:
        """Run an A2A transport via the JSON-RPC client."""
        # The A2AClient.send_task returns immediately and runs in a
        # background thread. The event callback (if provided) would
        # surface partial results, but for the LLM tool surface we
        # just want the final text. Wait for completion via polling.
        yield TurnEvent(
            type=TurnEventType.TEXT_DELTA,
            text=f"Delegating to {agent.name} (A2A)...\n",
            tool_name="delegate_external_task",
            metadata={"agent": agent.name, "transport": "a2a"},
        )

        a2a_client = self._get_a2a_client()
        a2a_task = a2a_client.send_task(
            agent,
            task,
            cancel_event=cancel_event,
        )

        # Poll task status until completion, cancellation, or timeout.
        # Using ``cancel_event.wait(poll_interval)`` instead of a fresh
        # ``threading.Event().wait()`` is intentional: if the user
        # cancels mid-wait, the call returns True and we exit the
        # loop on the next check. This is what makes cancellation
        # responsive (the previous version created a throwaway Event
        # that ignored the cancel signal entirely).
        poll_interval = 0.25
        elapsed = 0.0
        wait_event = cancel_event if cancel_event is not None else threading.Event()
        while True:
            if wait_event.is_set():
                a2a_client.cancel_task(a2a_task.id)
                yield TurnEvent.error_event(f"A2A task {a2a_task.id} cancelled by user")
                return ""
            if a2a_task.status in _TERMINAL_TASK_STATUSES:
                break
            if elapsed >= self._timeout:
                a2a_client.cancel_task(a2a_task.id)
                yield TurnEvent.error_event(f"A2A task {a2a_task.id} timed out after {self._timeout}s")
                return ""
            # wait_event.wait() returns True if set during the wait,
            # False on timeout. Either way we re-check status.
            wait_event.wait(poll_interval)
            elapsed += poll_interval

        # Translate final state to a TurnEvent + return value.
        if a2a_task.status == A2ATaskStatus.COMPLETED:
            text = a2a_task.result or "(no result)"
            yield TurnEvent(
                type=TurnEventType.TEXT_DELTA,
                text=text,
                tool_name="delegate_external_task",
                metadata={"agent": agent.name, "transport": "a2a"},
            )
            return self._truncate(text)
        err = a2a_task.error or f"Agent {agent.name} failed (no error message)"
        yield TurnEvent.error_event(err)
        return ""

    # -- Helpers ------------------------------------------------------------

    @staticmethod
    def _translate_subprocess_event(agent: ExternalAgentConfig, event: A2AEvent) -> str | None:
        """Map a SubprocessBridge stdout/completed event to user-facing text.

        Returns ``None`` for event types that the caller does not want
        surfaced as a ``TEXT_DELTA`` (currently just unknown types).
        ``"error"`` and ``"cancelled"`` events are intercepted directly
        in ``_run_subprocess`` and converted to ``TurnEvent.error_event``
        calls — they never reach this helper, so this function does
        not need a special case for them anymore.
        """
        if event.type == "stdout":
            return event.text
        if event.type == "completed":
            return event.text  # JSON-parsed result line
        # Unknown event type — let the caller decide what to do.
        return None

    @staticmethod
    def _truncate(text: str) -> str:
        """Cap returned text to keep the LLM context window healthy."""
        if len(text) <= _MAX_RESULT_CHARS:
            return text
        return text[:_MAX_RESULT_CHARS] + f"\n\n[...{len(text) - _MAX_RESULT_CHARS} chars truncated...]"


__all__ = ["A2ADispatcher"]
