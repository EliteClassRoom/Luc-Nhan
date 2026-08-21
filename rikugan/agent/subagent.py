"""Subagent runner: spawn isolated AgentLoop instances with their own context."""

from __future__ import annotations

import copy
from collections.abc import Generator
from typing import Any

from ..core.config import RikuganConfig
from ..core.logging import log_debug, log_info
from ..providers.base import LLMProvider
from ..skills.registry import SkillRegistry
from ..state.session import SessionState
from ..tools.registry import ToolRegistry
from .exploration_mode import KnowledgeBase
from .turn import TurnEvent, TurnEventType

# Valid mode prefixes for mode-aware sub-agents
_MODE_PREFIXES = {
    "exploration": "/explore ",
    "explore": "/explore ",
    "plan": "/plan ",
    "research": "/research ",
    "normal": "",
}


class SubagentRunner:
    """Runs an isolated AgentLoop with its own SessionState.

    The subagent shares the same provider, tool registry, and config as
    the parent, but has a fresh session — its own message history and
    context window.  This keeps the parent's context clean from verbose
    tool output (decompilations, disassembly dumps, etc.).

    After the subagent finishes, the parent receives only a compact
    summary rather than the full exploration trace.

    ``cancel_event`` is forwarded to the inner AgentLoop so that
    ``SubagentManager.cancel(agent_id)`` (or any caller-supplied event)
    interrupts the provider stream and the loop's cancellation checks.
    ``model_override`` (when non-empty) is applied only to the child
    run via a shallow copy of the provider and config; the parent
    provider/config objects are never mutated.
    """

    def __init__(
        self,
        provider: LLMProvider,
        tool_registry: ToolRegistry,
        config: RikuganConfig,
        host_name: str = "IDA Pro",
        skill_registry: SkillRegistry | None = None,
        parent_loop: Any | None = None,
        cancel_event: Any | None = None,
        model_override: str = "",
    ):
        self.provider = provider
        self.tools = tool_registry
        self.config = config
        self.host_name = host_name
        self.skills = skill_registry
        self._parent_loop = parent_loop
        self._cancel_event = cancel_event
        self._model_override = model_override or ""
        self._last_session: SessionState | None = None

    def _resolve_child_provider_and_config(
        self,
    ) -> tuple[LLMProvider, RikuganConfig]:
        """Return (provider, config) for the child run, honoring ``_model_override``.

        When no override is configured, the original provider and config
        are returned unchanged so existing callers see the same object
        identity they had before this feature.

        When an override is configured, the provider is shallow-copied,
        the config is shallow-copied, and both `.model` attributes are
        set to the override model. The SDK client / base URL / credentials
        on the provider copy remain shared with the parent because the
        provider modules do not require a state reset per model. The
        config copy isolates the agent loop's effective ``provider.model``
        without leaking back to the parent config.
        """
        if not self._model_override:
            return self.provider, self.config
        child_provider = copy.copy(self.provider)
        child_provider.model = self._model_override
        child_config = copy.copy(self.config)
        child_provider_config = copy.copy(self.config.provider)
        child_provider_config.model = self._model_override
        child_config.provider = child_provider_config
        # Providers with model-dependent cached state (e.g. GLM) must
        # refresh their internal lookup for the override model. Keep this
        # generic by attempting the standard helpers when present; absent
        # helpers mean no per-model state exists and the provider copy is
        # already correct.
        if hasattr(child_provider, "_glm_metadata") and hasattr(child_provider, "_glm_config"):
            from ..core.glm_config import get_glm_model_metadata, parse_glm_extra

            extra = getattr(child_provider, "_provider_extra_raw", None) or {
                "dialect": getattr(child_provider, "_provider_name", "") or "glm"
            }
            child_provider._glm_metadata = get_glm_model_metadata(self._model_override)
            try:
                child_provider._glm_config = parse_glm_extra(extra, self._model_override)
            except ValueError:
                # The override model does not support a user-saved setting
                # (typically ``reasoning_effort != "max"`` on a model
                # that does not advertise reasoning-effort support). Strip
                # the unsupported sub-field and re-parse with the model's
                # own default. The user's saved value is preserved on the
                # parent provider so they can switch back without losing it.
                safe_extra = dict(extra)
                thinking = safe_extra.get("thinking")
                if isinstance(thinking, dict):
                    thinking.pop("reasoning_effort", None)
                child_provider._glm_config = parse_glm_extra(safe_extra, self._model_override)
        return child_provider, child_config

    def _build_loop(self, session: SessionState) -> Any:
        """Construct an AgentLoop for the child run with cancel/model wiring.

        Centralizes the three call sites in ``run_task`` / ``run_exploration``
        / ``run_mode`` so cancel and model overrides stay consistent.
        ``AgentLoop`` prefers the explicit ``cancel_event`` over
        ``parent_loop`` inheritance, which is the desired precedence for
        SubagentManager-owned cancellation tokens.
        """
        from .loop import AgentLoop  # deferred to avoid circular import

        child_provider, child_config = self._resolve_child_provider_and_config()
        return AgentLoop(
            provider=child_provider,
            tool_registry=self.tools,
            config=child_config,
            session=session,
            skill_registry=self.skills,
            host_name=self.host_name,
            parent_loop=self._parent_loop,
            cancel_event=self._cancel_event,
        )

    @property
    def last_session(self) -> SessionState | None:
        """The session from the most recent subagent run."""
        return self._last_session

    # Event types that must always be forwarded even in silent mode
    # (approval gates and user questions require UI interaction).
    _INTERACTIVE_EVENTS = frozenset(
        {
            TurnEventType.TOOL_APPROVAL_REQUEST,
            TurnEventType.USER_QUESTION,
        }
    )

    def run_task(
        self,
        task: str,
        max_turns: int = 20,
        system_addendum: str = "",
        silent: bool = False,
    ) -> Generator[TurnEvent, None, str]:
        """Run a general-purpose subagent task.

        Yields TurnEvents from the subagent so the UI can show progress.
        Returns the subagent's final assistant text as a string summary.

        When *silent* is True, only interactive events (tool approval,
        user questions) are forwarded — text, tool calls, and results
        are suppressed from the parent UI.

        The subagent gets a clean session and runs the task as a normal
        agent loop (not exploration mode, not plan mode).
        """
        session = SessionState()
        self._last_session = session
        loop = self._build_loop(session)

        log_info(f"Subagent started: task={task[:80]!r}, max_turns={max_turns}, silent={silent}")

        # Prefix the task with a turn limit instruction
        augmented_task = (
            f"[SUBAGENT TASK — max {max_turns} turns]\n"
            f"{task}\n\n"
            f"When done, provide a concise summary of your findings. "
            f"Focus on actionable results, not the process."
        )
        if system_addendum:
            augmented_task = f"{system_addendum}\n\n{augmented_task}"

        final_text = ""
        for event in loop.run(augmented_task):
            # In silent mode, only forward interactive events
            if silent:
                if event.type in self._INTERACTIVE_EVENTS:
                    yield event
            else:
                yield event

            # Capture the last text_done as the final output
            if event.type.value == "text_done" and event.text:
                final_text = event.text

        # Sync "always allow" flag back to parent
        if self._parent_loop and loop._always_allow_scripts:
            self._parent_loop._always_allow_scripts = True

        log_info(f"Subagent finished: {len(final_text)} chars output")
        return final_text

    def run_exploration(
        self,
        user_goal: str,
        max_turns: int = 30,
        idb_path: str = "",
    ) -> Generator[TurnEvent, None, KnowledgeBase]:
        """Run exploration Phase 1 as an isolated subagent.

        The subagent runs in explore-only mode, accumulating findings
        in its own KnowledgeBase.  When exploration is complete, the
        KnowledgeBase is returned to the parent — NOT the raw
        decompilation/disassembly output that filled the subagent's
        context window.

        Yields TurnEvents so the UI can track progress.
        Returns the populated KnowledgeBase.
        """
        session = SessionState()
        session.idb_path = idb_path
        self._last_session = session
        loop = self._build_loop(session)

        log_info(f"Subagent exploration started: goal={user_goal[:80]!r}, max_turns={max_turns}")

        # Run in explore-only mode via the /explore prefix
        yield from loop.run(f"/explore {user_goal}")

        # Extract the knowledge base.  _run_exploration_mode stores
        # it in _last_knowledge_base before clearing _exploration_state.
        kb = loop.last_knowledge_base
        if kb is None:
            kb = KnowledgeBase(user_goal=user_goal)
            log_debug("Subagent exploration: no knowledge base returned, using empty")

        # Sync "always allow" flag back to parent
        if self._parent_loop and loop._always_allow_scripts:
            self._parent_loop._always_allow_scripts = True

        log_info(
            f"Subagent exploration finished: "
            f"{len(kb.relevant_functions)} functions, "
            f"{len(kb.findings)} findings, "
            f"{len(kb.hypotheses)} hypotheses"
        )
        return kb

    def run_mode(
        self,
        task: str,
        mode: str = "normal",
        max_turns: int = 20,
        system_addendum: str = "",
        silent: bool = False,
    ) -> Generator[TurnEvent, None, str]:
        """Run a subagent in a specific mode (exploration, plan, research, normal).

        Yields TurnEvents from the subagent so the UI can show progress.
        Returns the subagent's final assistant text as a string summary.

        The *mode* determines how the agent loop processes the task:
        - "exploration" / "explore": run in explore-only mode
        - "plan": run in plan mode (generate plan, approve, execute)
        - "research": run in research mode (explore + write markdown notes)
        - "normal": run as a standard agent (default)

        When *silent* is True, only interactive events (tool approval,
        user questions) are forwarded to the parent UI.
        """
        session = SessionState()
        self._last_session = session
        loop = self._build_loop(session)

        mode_prefix = _MODE_PREFIXES.get(mode.lower(), "")
        augmented_task = f"{mode_prefix}{task}" if mode_prefix else task

        log_info(f"Subagent mode started: mode={mode}, task={task[:80]!r}, max_turns={max_turns}, silent={silent}")

        augmented_task = (
            f"[SUBAGENT TASK — mode={mode}, max {max_turns} turns]\n"
            f"{augmented_task}\n\n"
            f"When done, provide a concise summary of your findings. "
            f"Focus on actionable results, not the process."
        )
        if system_addendum:
            augmented_task = f"{system_addendum}\n\n{augmented_task}"

        final_text = ""
        for event in loop.run(augmented_task):
            if silent:
                if event.type in self._INTERACTIVE_EVENTS:
                    yield event
            else:
                yield event

            if event.type.value == "text_done" and event.text:
                final_text = event.text

        if self._parent_loop and loop._always_allow_scripts:
            self._parent_loop._always_allow_scripts = True

        log_info(f"Subagent mode finished: mode={mode}, {len(final_text)} chars output")
        return final_text
