"""Turn event types emitted by the agent loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core.types import TokenUsage


class TurnEventType(str, Enum):
    TEXT_DELTA = "text_delta"
    TEXT_DONE = "text_done"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_ARGS_DELTA = "tool_call_args_delta"
    TOOL_CALL_DONE = "tool_call_done"
    TOOL_RESULT = "tool_result"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    ERROR = "error"
    CANCELLED = "cancelled"
    USAGE_UPDATE = "usage_update"
    USER_QUESTION = "user_question"
    PLAN_GENERATED = "plan_generated"
    PLAN_STEP_START = "plan_step_start"
    PLAN_STEP_DONE = "plan_step_done"
    TOOL_APPROVAL_REQUEST = "tool_approval_request"
    EXPLORATION_PHASE_CHANGE = "exploration_phase_change"
    EXPLORATION_FINDING = "exploration_finding"
    PATCH_APPLIED = "patch_applied"
    PATCH_VERIFIED = "patch_verified"
    SAVE_APPROVAL_REQUEST = "save_approval_request"
    SAVE_COMPLETED = "save_completed"
    SAVE_DISCARDED = "save_discarded"
    MUTATION_RECORDED = "mutation_recorded"
    RESEARCH_NOTE_SAVED = "research_note_saved"
    RESEARCH_NOTE_REVIEWED = "research_note_reviewed"
    SUBAGENT_SPAWNED = "subagent_spawned"
    SUBAGENT_PROGRESS = "subagent_progress"
    SUBAGENT_COMPLETED = "subagent_completed"
    SUBAGENT_FAILED = "subagent_failed"
    COMMAND_ORCHESTRA = "command_orchestra"
    KNOWLEDGE_RETRIEVED = "knowledge_retrieved"
    DOCS_GATE_STATUS = "docs_gate_status"
    MEMORY_SAVED = "memory_saved"
    # GLM reasoning-resilience events (consumed by stream/UI/recovery tasks).
    REASONING_DELTA = "reasoning_delta"
    RECOVERY_START = "recovery_start"
    TOOL_CALL_DISCARDED = "tool_call_discarded"


@dataclass
class TurnEvent:
    type: TurnEventType
    text: str = ""
    # Provider reasoning/thinking channel, distinct from user-visible ``text``.
    # Populated by REASONING_DELTA events; the same value can also carry
    # reasoning payloads from other resilience events.
    reasoning: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    tool_args: str = ""
    tool_result: str = ""
    tool_is_error: bool = False
    error: str | None = None
    usage: TokenUsage | None = None
    turn_number: int = 0
    plan_steps: list[str] | None = None
    plan_step_index: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def text_delta(text: str) -> TurnEvent:
        return TurnEvent(type=TurnEventType.TEXT_DELTA, text=text)

    @staticmethod
    def text_done(full_text: str) -> TurnEvent:
        return TurnEvent(type=TurnEventType.TEXT_DONE, text=full_text)

    @staticmethod
    def tool_call_start(tool_call_id: str, tool_name: str) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.TOOL_CALL_START,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )

    @staticmethod
    def tool_call_args_delta(tool_call_id: str, delta: str) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.TOOL_CALL_ARGS_DELTA,
            tool_call_id=tool_call_id,
            tool_args=delta,
        )

    @staticmethod
    def tool_call_done(tool_call_id: str, tool_name: str, args: str) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.TOOL_CALL_DONE,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_args=args,
        )

    @staticmethod
    def tool_result_event(
        tool_call_id: str,
        tool_name: str,
        result: str,
        is_error: bool = False,
    ) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.TOOL_RESULT,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_result=result,
            tool_is_error=is_error,
        )

    @staticmethod
    def turn_start(turn_number: int) -> TurnEvent:
        return TurnEvent(type=TurnEventType.TURN_START, turn_number=turn_number)

    @staticmethod
    def turn_end(turn_number: int) -> TurnEvent:
        return TurnEvent(type=TurnEventType.TURN_END, turn_number=turn_number)

    @staticmethod
    def error_event(error: str) -> TurnEvent:
        return TurnEvent(type=TurnEventType.ERROR, error=error)

    @staticmethod
    def cancelled_event() -> TurnEvent:
        return TurnEvent(type=TurnEventType.CANCELLED)

    @staticmethod
    def usage_update(usage: TokenUsage) -> TurnEvent:
        return TurnEvent(type=TurnEventType.USAGE_UPDATE, usage=usage)

    @staticmethod
    def user_question(
        question: str,
        options: list[str] | None,
        tool_call_id: str,
        allow_text: bool = False,
    ) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.USER_QUESTION,
            text=question,
            tool_call_id=tool_call_id,
            metadata={"options": options or [], "allow_text": allow_text},
        )

    @staticmethod
    def plan_generated(steps: list[str]) -> TurnEvent:
        return TurnEvent(type=TurnEventType.PLAN_GENERATED, plan_steps=steps)

    @staticmethod
    def plan_step_start(index: int, description: str) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.PLAN_STEP_START,
            plan_step_index=index,
            text=description,
        )

    @staticmethod
    def plan_step_done(index: int, result: str) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.PLAN_STEP_DONE,
            plan_step_index=index,
            text=result,
        )

    @staticmethod
    def tool_approval_request(
        tool_call_id: str,
        tool_name: str,
        args: str,
        description: str,
    ) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.TOOL_APPROVAL_REQUEST,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_args=args,
            text=description,
        )

    @staticmethod
    def docs_gate_status(
        tool_call_id: str,
        state: str,
        reasons: tuple[str, ...] = (),
        summary: str = "",
    ) -> TurnEvent:
        """Emit a docs-review gate status update for an execute_python call.

        ``state`` is one of ``running`` | ``approved`` | ``blocked`` | ``failed``.
        This is a UI-only signal — it is NOT serialized into assistant text
        or history.  ``reasons`` are the complexity reasons (for ``running``);
        ``summary`` is the reviewer summary (for ``blocked`` / ``failed``).
        """
        return TurnEvent(
            type=TurnEventType.DOCS_GATE_STATUS,
            tool_call_id=tool_call_id,
            metadata={
                "docs_gate_state": state,
                "docs_gate_reasons": list(reasons),
                "docs_gate_summary": summary,
            },
        )

    @staticmethod
    def exploration_phase_change(
        from_phase: str,
        to_phase: str,
        reason: str = "",
    ) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.EXPLORATION_PHASE_CHANGE,
            text=reason,
            metadata={"from_phase": from_phase, "to_phase": to_phase},
        )

    @staticmethod
    def exploration_finding(
        category: str,
        summary: str,
        address: int | None = None,
        relevance: str = "medium",
    ) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.EXPLORATION_FINDING,
            text=summary,
            metadata={
                "category": category,
                "address": f"0x{address:x}" if address is not None else None,
                "relevance": relevance,
            },
        )

    @staticmethod
    def patch_applied(
        address: int,
        description: str,
        original_hex: str,
        new_hex: str,
    ) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.PATCH_APPLIED,
            text=description,
            metadata={
                "address": f"0x{address:x}",
                "original": original_hex,
                "new": new_hex,
            },
        )

    @staticmethod
    def patch_verified(address: int, success: bool, result: str) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.PATCH_VERIFIED,
            text=result,
            metadata={
                "address": f"0x{address:x}",
                "success": success,
            },
        )

    @staticmethod
    def save_approval_request(
        patch_count: int,
        total_bytes: int,
        all_verified: bool,
        patches_detail: list[dict[str, Any]] | None = None,
    ) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.SAVE_APPROVAL_REQUEST,
            text=f"{patch_count} patches ready ({total_bytes} bytes modified)",
            metadata={
                "patch_count": patch_count,
                "total_bytes": total_bytes,
                "all_verified": all_verified,
                "patches": patches_detail or [],
            },
        )

    @staticmethod
    def save_completed(patch_count: int, total_bytes: int) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.SAVE_COMPLETED,
            text=f"Saved {patch_count} patches ({total_bytes} bytes)",
            metadata={"patch_count": patch_count, "total_bytes": total_bytes},
        )

    @staticmethod
    def save_discarded(patch_count: int, rolled_back: bool) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.SAVE_DISCARDED,
            text=f"Discarded {patch_count} patches"
            + (" (original bytes restored)" if rolled_back else " (in-memory changes persist)"),
            metadata={"patch_count": patch_count, "rolled_back": rolled_back},
        )

    @staticmethod
    def research_note_saved(
        title: str,
        genre: str,
        path: str,
        preview: str = "",
        review_passed: bool = True,
    ) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.RESEARCH_NOTE_SAVED,
            text=title,
            metadata={
                "genre": genre,
                "path": path,
                "preview": preview,
                "review_passed": review_passed,
            },
        )

    @staticmethod
    def research_note_reviewed(
        title: str,
        passed: bool,
        feedback: str = "",
    ) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.RESEARCH_NOTE_REVIEWED,
            text=title,
            metadata={
                "passed": passed,
                "feedback": feedback,
            },
        )

    @staticmethod
    def mutation_recorded(
        tool_name: str,
        description: str,
        reversible: bool,
        reverse_tool: str = "",
        reverse_args: dict[str, Any] | None = None,
    ) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.MUTATION_RECORDED,
            tool_name=tool_name,
            text=description,
            metadata={
                "reversible": reversible,
                "reverse_tool": reverse_tool,
                "reverse_args": reverse_args or {},
            },
        )

    @staticmethod
    def subagent_spawned(
        agent_id: str,
        name: str,
        agent_type: str,
        task: str,
    ) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.SUBAGENT_SPAWNED,
            text=name,
            metadata={
                "agent_id": agent_id,
                "agent_type": agent_type,
                "task": task,
            },
        )

    @staticmethod
    def subagent_progress(
        agent_id: str,
        turn_count: int,
        text: str = "",
    ) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.SUBAGENT_PROGRESS,
            text=text,
            metadata={
                "agent_id": agent_id,
                "turn_count": turn_count,
            },
        )

    @staticmethod
    def subagent_completed(
        agent_id: str,
        name: str,
        summary: str,
        turn_count: int = 0,
        elapsed: float = 0.0,
    ) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.SUBAGENT_COMPLETED,
            text=summary,
            metadata={
                "agent_id": agent_id,
                "name": name,
                "turn_count": turn_count,
                "elapsed": elapsed,
            },
        )

    @staticmethod
    def subagent_failed(
        agent_id: str,
        name: str,
        error: str,
    ) -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.SUBAGENT_FAILED,
            error=error,
            metadata={
                "agent_id": agent_id,
                "name": name,
            },
        )

    @staticmethod
    def command_orchestra(message: str = "") -> TurnEvent:
        return TurnEvent(
            type=TurnEventType.COMMAND_ORCHESTRA,
            text=message,
        )

    @staticmethod
    def knowledge_retrieved(
        summary: str,
        counts: dict,
        items: list[dict] | None = None,
    ) -> TurnEvent:
        """Compact notification that retrieval happened this turn.

        ``summary`` is a one-line human-readable description of the
        retrieved slice (e.g. "5 memories, 2 entities, 4 relations for
        0x401000"). ``counts`` is a dict the chat widget uses for its
        badge; ``items`` is a short list of representative titles/ids.
        """
        return TurnEvent(
            type=TurnEventType.KNOWLEDGE_RETRIEVED,
            text=summary,
            metadata={"counts": dict(counts or {}), "items": list(items or [])},
        )

    @staticmethod
    def memory_saved(tool_call_id: str) -> TurnEvent:
        """Emit a write-side signal that the knowledge store changed.

        Fires after a successful ``save_memory`` tool call (on both
        ``created`` and ``deduplicated`` outcomes). The Knowledge panel
        listens for this event to refresh its view.
        """
        return TurnEvent(
            type=TurnEventType.MEMORY_SAVED,
            tool_call_id=tool_call_id,
        )

    # ------------------------------------------------------------------
    # GLM reasoning-resilience factories
    # ------------------------------------------------------------------

    @staticmethod
    def reasoning_event(text: str) -> TurnEvent:
        """Emit an incremental reasoning/thinking delta from the provider.

        Carries the private reasoning channel separately from the
        user-visible ``text`` channel so the UI and recovery logic can
        distinguish them.
        """
        return TurnEvent(type=TurnEventType.REASONING_DELTA, reasoning=text)

    @staticmethod
    def recovery_start(
        *,
        attempt: int,
        reason: str,
        discard_transient_reasoning: bool,
    ) -> TurnEvent:
        """Mark the boundary where a reasoning-recovery retry begins.

        ``attempt`` is the 1-based retry number. ``reason`` is the
        canonical trigger (e.g. ``reasoning_degenerated``,
        ``truncated_partial_tool_use``). ``discard_transient_reasoning``
        records whether the transient reasoning trace from the failed
        attempt is dropped before retrying.
        """
        return TurnEvent(
            type=TurnEventType.RECOVERY_START,
            metadata={
                "attempt": attempt,
                "reason": reason,
                "discard_transient_reasoning": discard_transient_reasoning,
            },
        )

    @staticmethod
    def tool_call_discarded(tool_call_id: str, tool_name: str, reason: str) -> TurnEvent:
        """Close out a started-but-discarded tool call.

        Emitted when a partial tool call (truncated args, degenerated
        JSON, etc.) is dropped instead of executed. Pairs with a prior
        ``TOOL_CALL_START`` so the UI can render a closed lifecycle.
        """
        return TurnEvent(
            type=TurnEventType.TOOL_CALL_DISCARDED,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            metadata={"reason": reason},
        )

    # ------------------------------------------------------------------
    # Serialization — stable dict format for headless / control-server
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize this TurnEvent to a stable JSON-compatible dict.

        Headless integrations (runner, control server, CLI) consume
        serialized TurnEvents rather than scraping human-readable UI
        output.
        """
        d: dict[str, Any] = {"type": self.type.value}

        if self.text:
            d["text"] = self.text
        if self.reasoning:
            d["reasoning"] = self.reasoning
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_name:
            d["tool_name"] = self.tool_name
        if self.tool_args:
            d["tool_args"] = self.tool_args
        if self.tool_result:
            d["tool_result"] = self.tool_result
        if self.tool_is_error:
            d["tool_is_error"] = True
        if self.error:
            d["error"] = self.error
        d["turn_number"] = self.turn_number
        if self.plan_steps is not None:
            d["plan_steps"] = self.plan_steps
        d["plan_step_index"] = self.plan_step_index
        if self.usage is not None:
            d["usage"] = {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "total_tokens": self.usage.total_tokens,
                "cache_read_tokens": self.usage.cache_read_tokens,
                "cache_creation_tokens": self.usage.cache_creation_tokens,
            }
        if self.metadata:
            d["metadata"] = self.metadata

        return d
