"""A2A mode runner: thin wrapper that routes ``/a2a <agent> <message>``
slash commands through the same ``A2ADispatcher`` the
``delegate_external_task`` pseudo-tool uses.

Why a thin wrapper instead of duplicate logic:
- The pseudo-tool and the slash command share a transport stack
  (SubprocessBridge, A2AClient) and a translation layer
  (A2ADispatcher). Two implementations would drift.
- One fix point for cancel-event wiring, output truncation, and
  error handling.
- Easier to test: the dispatcher has its own integration tests;
  this mode just splits the slash command's body into (agent, task).

Why this is separate from orchestra's ``delegate_task``:
- Orchestra's delegate_task is LLM-mediated — the orchestrator LLM
  decides when to delegate and to which agent.
- ``/a2a`` is user-mediated — the user explicitly chooses the
  agent and the task. No LLM interpretation, no cost surprises.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING

from ..a2a import A2ADispatcher
from ...core.errors import CancellationError
from ..turn import TurnEvent, TurnEventType
if TYPE_CHECKING:
    from ..loop import AgentLoop


def run_a2a_mode(
    loop: AgentLoop,
    user_message: str,
    system_prompt: str,
    tools_schema: list,
) -> Generator[TurnEvent, None, None]:
    """Run the agent in /a2a mode: delegate to an external agent.

    Parses the slash command body as ``<agent> <message...>``:
      /a2a claude "summarize the binary"
      /a2a codex what does main do?

    The first whitespace-separated token is the agent name; the
    rest is the task text. Surfaces a clear error event when
    either piece is missing.

    Events yielded: ``TEXT_DELTA`` (one per dispatcher event),
    ``TEXT_DONE`` at the end with the final aggregated output,
    ``ERROR`` for validation/transport failures, and (via the outer
    ``AgentLoop.run`` re-raise) ``CANCELLED`` when the user cancels
    mid-dispatch. We bypass the regular turn cycle entirely — /a2a
    is a one-shot, non-LLM-mediated path.
    """
    parts = user_message.split(maxsplit=1)
    if len(parts) < 2:
        yield TurnEvent.error_event('Usage: /a2a <agent> <message>\nExample: /a2a claude "summarize the main function"')
        yield TurnEvent.text_done('Usage: /a2a <agent> <message>\nExample: /a2a claude "summarize the main function"')
        return

    agent_name, task_text = parts[0].strip(), parts[1].strip()
    if not agent_name or not task_text:
        yield TurnEvent.error_event("Both agent name and message are required for /a2a.")
        yield TurnEvent.text_done("Both agent name and message are required for /a2a.")
        return

    # Build dispatcher using the same config the pseudo-tool
    # consumes. ``a2a_agents`` is a list of dicts (raw TOML
    # deserialization); the dispatcher forwards it to the
    # registry on first discover().
    dispatcher = A2ADispatcher(
        auto_discover=getattr(loop.config, "a2a_auto_discover", True),
        a2a_agents=getattr(loop.config, "a2a_agents", None),
    )

    # Stream dispatcher events straight to the chat. We don't
    # wrap in a tool call because /a2a is direct user action,
    # not an LLM-driven tool invocation.
    yield TurnEvent(
        type=TurnEventType.TEXT_DELTA,
        text=f"[A2A] Delegating to {agent_name}...\n",
    )

    collected_text = ""
    try:
        for event in dispatcher.run_task(
            agent_name,
            task_text,
            cancel_event=loop._cancelled,
        ):
            if event.type == TurnEventType.TEXT_DELTA:
                collected_text += event.text or ""
                yield event
            elif event.type == TurnEventType.ERROR:
                # The dispatcher surfaces user cancellation as an
                # ERROR event with a "cancelled by user" message
                # rather than raising. Detect the underlying cancel
                # signal and re-raise ``CancellationError`` so the
                # outer ``AgentLoop.run`` converts it into a
                # ``CANCELLED`` event for the UI instead of a
                # generic ``ERROR``.
                if loop._cancelled is not None and loop._cancelled.is_set():
                    raise CancellationError("Agent run cancelled")
                collected_text += event.error or "External agent error"
                yield event
    except CancellationError:
        # Propagate unchanged so the outer ``AgentLoop.run``
        # converts it into a CANCELLED event.
        raise
    except Exception as e:
        collected_text += f"\nDispatcher exception: {e}"
        yield TurnEvent.error_event(f"Dispatcher exception: {e}")

    # Final aggregated output. The chat view treats this as the
    # end of the assistant's turn, so the next user message
    # arrives cleanly.
    final = collected_text.strip() or "(no output)"
    yield TurnEvent.text_done(final)
