"""Exploration mode runner: explore -> plan -> patch -> save."""

from __future__ import annotations

import re
from collections.abc import Generator
from typing import TYPE_CHECKING

from ... import constants
from ...core.errors import ToolError
from ...core.logging import log_debug, log_error, log_info
from ...core.types import (
    Message,
    Role,
    UserDecision,
    parse_approval,
    parse_save_decision,
)
from ..exploration_mode import (
    EXECUTE_STEP_PROMPT,
    EXPLORATION_SYSTEM_ADDENDUM,
    PLAN_SYNTHESIS_PROMPT,
    ExplorationPhase,
    ExplorationState,
    ModificationPlan,
    PatchSummary,
    PlannedChange,
)
from ..plan_mode import parse_plan as _parse_plan_impl
from ..subagent import SubagentRunner
from ..turn import TurnEvent
from .phase_tracker import ModePhaseTracker
from .turn_helpers import execute_single_turn


def _id_for_pair(finding) -> str:
    from ...memory.ingest import _stable_hash

    cat = (finding.category or "general").strip().lower()
    addr_part = (
        f"0x{int(finding.address):x}" if finding.address is not None else "noaddr"
    )
    return f"mem:explore:{cat}:{addr_part}:{_stable_hash(cat, finding.summary, finding.address)}"

def _build_central_index(
    goal: str,
    review,
    persisted_count: int,
    finding_by_id: dict[str, Any] | None = None,
) -> str:
    """Build a bounded sanitized verified-only index for central memory.

    The plan forbids unverified hypotheses; only corrected records
    from a fully-passed review contribute. Every field is sanitized
    with ``strip_injection_markers`` and capped in length; the
    total record count is bounded so a large finding set cannot
    blow out the prompt injection envelope. ``finding_by_id`` lets
    the index surface the original ``Finding.evidence`` and address
    alongside each verified record.
    """
    from ...core.sanitize import strip_injection_markers
    from ..report_review import ReviewResult

    def _safe(value: object, limit: int = 200) -> str:
        text = strip_injection_markers(str(value or "")).strip()
        if len(text) > limit:
            text = text[: limit - 1].rstrip() + "…"
        return text

    max_records = 50
    effective_count = (
        len(review.records) if isinstance(review, ReviewResult) and persisted_count == 0 else persisted_count
    )
    parts = [
        f"goal: {_safe(goal, 400)}",
        f"verified_finding_count: {int(effective_count)}",
        f"review_cycles: {int(review.cycles) if isinstance(review, ReviewResult) else 0}",
        "",
        "verified_findings:",
    ]
    if isinstance(review, ReviewResult):
        for mem in review.records[:max_records]:
            finding = (finding_by_id or {}).get(mem.id) if finding_by_id else None
            addr = ""
            evidence = ""
            if finding is not None:
                if finding.address is not None:
                    addr = f"0x{int(finding.address):x}"
                evidence = finding.evidence or ""
            elif mem.entity_refs:
                addr = mem.entity_refs[0]
            content = (mem.content or "").strip().replace("\n", " ")
            parts.append(
                f"- [{_safe(mem.type, 32)}] id={_safe(mem.id, 80)} "
                f"addr={_safe(addr, 40)} title={_safe(mem.title, 80)}: "
                f"{_safe(content, 200)} evidence={_safe(evidence, 200)}"
            )
        if len(review.records) > max_records:
            parts.append(
                f"… ({len(review.records) - max_records} more verified findings truncated)"
            )
    return "\n".join(parts).strip()


def finalize_explore_memory(
    loop: AgentLoop, state: ExplorationState
) -> tuple[bool, str]:
    """Synchronous wrapper around :func:`_finalize_explore_memory`.

    Drains the event generator and returns ``(persisted, message)``
    so callers/tests can use the public contract specified in the
    plan. ``persisted`` is True only when at least one verified
    finding made it to the raw store or the central index.
    """
    persisted = False
    last_message = ""
    for event in _finalize_explore_memory(loop, state):
        text = getattr(event, "text", "") or ""
        if text:
            last_message = text
        if getattr(event, "type", None) and getattr(event.type, "value", "") == "text_done":
            lowered = text.lower()
            if "saved" in lowered or "central index only" in lowered:
                persisted = True
            if "persisted" in text.lower():
                persisted = True
    return persisted, last_message


def _finalize_explore_memory(loop, state: ExplorationState) -> Generator[TurnEvent, None, None]:
    """Review and persist verified findings at the end of /explore.

    Only runs for ``/explore`` (inline mode). Reviews every finding
    with the same independent verifier used by ``/report``. Persists
    verified findings to the raw store; saves a compact central fact
    when the controller has bound a memory authority. A failed review
    keeps the in-memory summary but surfaces a warning to the user.
    """
    findings = list(state.knowledge_base.findings)
    if not findings:
        return
    from ...memory.ingest import (
        _stable_hash,
        ingest_exploration_finding,
        make_store,
    )
    from ...memory.paths import function_entity_id
    from ...memory.schema import KnowledgeMemory
    from ..report_review import review_memories

    candidates: list[KnowledgeMemory] = []
    for f in findings:
        cat = (f.category or "general").strip().lower()
        addr_part = (
            f"0x{int(f.address):x}" if f.address is not None else "noaddr"
        )
        mem_id = f"mem:explore:{cat}:{addr_part}:{_stable_hash(cat, f.summary, f.address)}"
        entity_refs: list[str] = []
        if f.address is not None:
            entity_refs.append(function_entity_id(f.address))
        candidates.append(
            KnowledgeMemory(
                id=mem_id,
                binary_id="",
                type=cat,
                title=(f.summary or mem_id).splitlines()[0][:80],
                content=f.summary or "",
                entity_refs=entity_refs,
                relation_refs=[],
                source_refs=[f"exploration_report:{mem_id}"],
                tags=[cat, "exploration"],
                confidence=0.6,
                importance=0.5,
                verified=False,
            )
        )

    store, paths = make_store(loop.session.idb_path)
    raw_store_available = store is not None and paths is not None
    if not raw_store_available:
        log_debug("explore memory finalizer: raw store unavailable; ingest will be skipped")
    for mem in candidates:
        if paths is not None:
            mem.binary_id = paths.binary_id

    yield TurnEvent.text_done(
        f"Verifying {len(candidates)} finding(s) before persisting exploration memory…"
    )
    review = review_memories(loop, candidates, max_cycles=3)
    if not review.passed:
        ids = ", ".join(sorted(review.unresolved.keys())) or "(none)"
        yield TurnEvent.error_event(
            f"Exploration memory not persisted: {len(review.unresolved)} finding(s) "
            f"unverified after {review.cycles} cycle(s). IDs: {ids}."
        )
        return
    finding_by_id: dict[str, Any] = {
        mem.id: finding for mem, finding in zip(candidates, findings)
    }
    persisted = 0
    raw_errors: list[str] = []
    if raw_store_available:
        for mem, finding in zip(review.records, findings):
            corrected_summary = mem.content or finding.summary
            try:
                ingest_exploration_finding(
                    store,
                    paths,
                    category=mem.type,
                    summary=corrected_summary,
                    address=finding.address,
                    relevance="high",
                    evidence=finding.evidence,
                    function_name=(
                        finding.summary[:40]
                        if finding.category == "function_purpose"
                        else ""
                    ),
                    # Prebuilt id keeps the raw store and the
                    # central index referencing the same record
                    # after content correction; explicit
                    # title/confidence persist reviewer-corrected
                    # fields (Plan §5.43).
                    memory_id=mem.id,
                    title=mem.title,
                    confidence=mem.confidence,
                )
                persisted += 1
            except Exception as exc:
                raw_errors.append(f"{mem.id}: {exc!r}")
                log_debug(f"explore memory finalizer ingest failed: {exc!r}")
    else:
        raw_errors.append("raw store: unavailable")
    central_saved = False
    if loop.memory_service is not None and loop._memory_authority is not None:
        try:
            index_fact = _build_central_index(
                state.knowledge_base.user_goal,
                review,
                persisted,
                finding_by_id,
            )
            loop.memory_service.save_fact(
                loop._memory_authority,
                category="exploration_index",
                fact=index_fact,
                source="explore_verified",
            )
            central_saved = True
        except Exception as exc:
            raw_errors.append(f"central memory: {exc!r}")
            log_debug(f"explore memory finalizer central save failed: {exc!r}")

    verified_summary = _build_central_index(
        state.knowledge_base.user_goal,
        review,
        persisted,
        finding_by_id,
    )
    if persisted == 0 and not central_saved:
        joined = "; ".join(raw_errors) or "no persistence path succeeded"
        yield TurnEvent.error_event(
            f"Exploration memory not persisted: {joined}"
        )
        return
    if central_saved and persisted > 0:
        status_msg = (
            f"Verified exploration memory saved to raw store and "
            f"central index ({persisted} finding(s))."
        )
        body = (
            f"[SYSTEM] Verified exploration memory saved to raw store and "
            f"central index ({persisted} finding(s)).\n\n{verified_summary}"
        )
    elif central_saved:
        status_msg = (
            f"Verified exploration memory saved to central index only "
            f"(raw store unavailable)."
        )
        body = (
            f"[SYSTEM] Verified exploration memory saved to central index only "
            f"(raw store unavailable).\n\n{verified_summary}"
        )
    else:
        status_msg = (
            f"Verified exploration memory saved to raw store "
            f"({persisted} finding(s))."
        )
        body = (
            f"[SYSTEM] Verified exploration memory saved to raw store "
            f"({persisted} finding(s)).\n\n{verified_summary}"
        )
    loop.session.add_message(Message(role=Role.USER, content=body))
    yield TurnEvent.text_done(status_msg)
    if raw_errors:
        yield TurnEvent.error_event(
            "Exploration memory persistence had partial failures: "
            + "; ".join(raw_errors)
        )


def _run_phase1_subagent(
    loop: AgentLoop,
    state: ExplorationState,
    user_message: str,
    exploration_system: str,
) -> Generator[TurnEvent, None, None]:
    """Run exploration Phase 1 as an isolated subagent."""
    runner = SubagentRunner(
        provider=loop.provider,
        tool_registry=loop.tools,
        config=loop.config,
        host_name=loop.host_name,
        skill_registry=loop.skills,
        parent_loop=loop,
    )

    log_info("Phase 1 running as subagent (isolated context)")
    kb = yield from runner.run_exploration(
        user_goal=user_message,
        max_turns=state.max_explore_turns,
        idb_path=loop.session.idb_path or "",
    )

    # Store exploration subagent messages for export
    if runner.last_session and runner.last_session.messages:
        log_id = f"exploration_{state.total_turns}"
        loop.session.subagent_logs[log_id] = list(runner.last_session.messages)

    # Merge subagent knowledge base into parent state
    state.knowledge_base = kb
    state.knowledge_base.user_goal = user_message

    # Inject summary into parent context (compact, not raw output)
    summary = kb.to_summary()
    if summary:
        summary_msg = Message(
            role=Role.USER,
            content=("[SYSTEM] Subagent exploration complete. Summary:\n\n" + summary),
        )
        loop.session.add_message(summary_msg)

    # Transition to plan if ready
    if kb.has_minimum_for_planning:
        state.transition_to(ExplorationPhase.PLAN)
        yield TurnEvent.exploration_phase_change(
            "explore",
            "plan",
            "Subagent exploration complete. Moving to planning.",
        )
    else:
        yield TurnEvent.error_event(
            "Subagent exploration finished without sufficient findings. "
            f"Gap: {kb.planning_gap_description}. "
            "Try a more specific request."
        )
        loop._clear_exploration_state()


def run_phase1_inline(
    loop: AgentLoop,
    state: ExplorationState,
    exploration_system: str,
    tools_schema: list,
    explore_only: bool,
) -> Generator[TurnEvent, None, None]:
    """Run exploration Phase 1 inline (in the parent's context)."""
    while state.phase == ExplorationPhase.EXPLORE:
        loop._check_cancelled()
        state.explore_turns += 1
        state.total_turns += 1

        if state.explore_turns > state.max_explore_turns:
            if state.knowledge_base.has_minimum_for_planning and not explore_only:
                state.transition_to(ExplorationPhase.PLAN)
                yield TurnEvent.exploration_phase_change(
                    "explore",
                    "plan",
                    f"Exploration turn limit reached ({state.max_explore_turns}). "
                    "Moving to planning with current findings.",
                )
                break
            else:
                yield TurnEvent.error_event(
                    f"Exploration turn limit reached ({state.max_explore_turns}) "
                    "without sufficient findings for planning. "
                    "Try a more specific request."
                )
                loop._clear_exploration_state()
                return

        yield TurnEvent.turn_start(state.total_turns)

        result = yield from execute_single_turn(loop, exploration_system, tools_schema)

        if not result.ok:
            loop._clear_exploration_state()
            return

        if not result.has_tool_calls:
            yield TurnEvent.turn_end(state.total_turns)
            if explore_only:
                break
            if state.knowledge_base.has_minimum_for_planning:
                state.transition_to(ExplorationPhase.PLAN)
                yield TurnEvent.exploration_phase_change(
                    "explore",
                    "plan",
                    "Agent finished exploration. Moving to planning.",
                )
            break

        loop._maybe_inject_error_hint()

        yield TurnEvent.turn_end(state.total_turns)


def _run_phase2_plan(
    loop: AgentLoop,
    state: ExplorationState,
    exploration_system: str,
    user_goal: str,
) -> Generator[TurnEvent, None, list[str] | None]:
    """Phase 2: synthesize a modification plan from gathered findings.

    Returns the parsed step list, or None if planning failed/was rejected.
    """
    knowledge_summary = state.knowledge_base.to_summary()
    plan_prompt = PLAN_SYNTHESIS_PROMPT.format(knowledge_summary=knowledge_summary)
    loop.session.add_message(Message(role=Role.USER, content=plan_prompt))

    def _generate_plan_turn() -> Generator[TurnEvent, None, str | None]:
        """Run one text-only plan generation turn."""
        state.total_turns += 1
        yield TurnEvent.turn_start(state.total_turns)
        result = yield from execute_single_turn(loop, exploration_system, None)
        yield TurnEvent.turn_end(state.total_turns)
        if not result.ok:
            return None
        return result.text

    def _build_mod_plan(plan_text: str, steps: list[str]) -> None:
        changes: list[PlannedChange] = []
        for i, step in enumerate(steps):
            addr_match = re.search(r"0x([0-9a-fA-F]+)", step)
            addr = int(addr_match.group(1), 16) if addr_match else 0
            changes.append(
                PlannedChange(
                    index=i,
                    target_address=addr,
                    current_behavior="",
                    proposed_behavior=step,
                    patch_strategy=step,
                )
            )
        state.modification_plan = ModificationPlan(changes=changes, rationale=plan_text)

    plan_text = yield from _generate_plan_turn()
    if plan_text is None:
        return None

    steps = _parse_plan(plan_text)
    if not steps:
        yield TurnEvent.error_event("Failed to generate a valid modification plan from exploration findings.")
        return None

    yield TurnEvent.plan_generated(steps)
    _build_mod_plan(plan_text, steps)

    # User approval gate
    decision = parse_approval(loop._wait_for_queue(loop._user_answer_queue))
    while decision.decision != UserDecision.APPROVE:
        loop._check_cancelled()
        yield TurnEvent.user_question(
            "Modification plan rejected. Would you like to regenerate it, or type feedback for a revised plan?",
            ["Regenerate", "Cancel"],
            tool_call_id="plan_reject",
            allow_text=True,
        )
        decision = parse_approval(loop._wait_for_queue(loop._user_answer_queue))
        if decision.decision == UserDecision.CANCEL:
            yield TurnEvent.error_event("Modification plan cancelled by user.")
            return None
        regen_prompt = "The user rejected the previous modification plan."
        if decision.feedback:
            regen_prompt += f" Their feedback: {decision.feedback}"
        regen_prompt += "\n\nPlease generate a revised modification plan."
        plan_prompt = PLAN_SYNTHESIS_PROMPT.format(knowledge_summary=knowledge_summary)
        loop.session.add_message(Message(role=Role.USER, content=regen_prompt + "\n\n" + plan_prompt))

        plan_text = yield from _generate_plan_turn()
        if plan_text is None:
            return None

        steps = _parse_plan(plan_text)
        if not steps:
            yield TurnEvent.error_event("Failed to generate a valid modification plan.")
            return None

        _build_mod_plan(plan_text, steps)
        yield TurnEvent.plan_generated(steps)
        decision = parse_approval(loop._wait_for_queue(loop._user_answer_queue))

    state.transition_to(ExplorationPhase.EXECUTE)
    yield TurnEvent.exploration_phase_change("plan", "execute", "Plan approved. Executing patches.")
    from .plan import persist_plan

    persist_plan(loop, user_goal, steps)
    return steps


def _run_phase3_execute(
    loop: AgentLoop,
    state: ExplorationState,
    steps: list[str],
    exploration_system: str,
    tools_schema: list,
) -> Generator[TurnEvent, None, bool]:
    """Phase 3: execute each planned patch step. Returns True if completed."""
    for i, step_desc in enumerate(steps):
        loop._check_cancelled()
        state.execute_turns += 1
        if state.execute_turns > state.max_execute_turns:
            yield TurnEvent.error_event(
                f"Execute turn limit reached ({state.max_execute_turns}). Some patches may not have been applied."
            )
            return False

        yield TurnEvent.plan_step_start(i, step_desc)
        loop.session.add_message(
            Message(
                role=Role.USER,
                content=EXECUTE_STEP_PROMPT.format(
                    index=i + 1,
                    total=len(steps),
                    description=step_desc,
                ),
            )
        )

        # Mini agent loop for this step
        for _st in range(10):
            loop._check_cancelled()
            state.total_turns += 1
            yield TurnEvent.turn_start(state.total_turns)

            result = yield from execute_single_turn(loop, exploration_system, tools_schema)

            if not result.ok:
                return False

            if not result.has_tool_calls:
                yield TurnEvent.turn_end(state.total_turns)
                break

            yield TurnEvent.turn_end(state.total_turns)

        yield TurnEvent.plan_step_done(i, "completed")
    return True


def _run_phase4_save(
    loop: AgentLoop,
    state: ExplorationState,
) -> Generator[TurnEvent, None, None]:
    """Phase 4: prompt the user to save or discard applied patches."""
    state.transition_to(ExplorationPhase.SAVE)
    yield TurnEvent.exploration_phase_change("execute", "save", "All patches applied. Awaiting save decision.")

    summary = PatchSummary(patches=list(state.patches_applied))
    summary.compute()
    patches_detail = [
        {
            "address": f"0x{p.address:x}",
            "description": p.description,
            "original": p.original_bytes.hex() if p.original_bytes else "",
            "new": p.new_bytes.hex() if p.new_bytes else "",
            "verified": p.verified,
        }
        for p in state.patches_applied
    ]
    yield TurnEvent.save_approval_request(
        patch_count=len(state.patches_applied),
        total_bytes=summary.total_bytes_modified,
        all_verified=summary.all_verified,
        patches_detail=patches_detail,
    )

    save_decision = parse_save_decision(loop._wait_for_queue(loop._user_answer_queue))
    if save_decision.decision == UserDecision.SAVE:
        loop.session.add_message(
            Message(
                role=Role.USER,
                content=(
                    "[SYSTEM] Patches are saved in the analysis database. "
                    "To create a patched binary:\n"
                    "- **IDA Pro**: File → Produce file → Create patched file\n"
                    "- **IDA Pro**: File → Save Database"
                ),
            )
        )
        yield TurnEvent.save_completed(len(state.patches_applied), summary.total_bytes_modified)
        log_info("Exploration mode: patches saved")
    else:
        rolled_back = False
        if state.patches_applied:
            rollback_parts = [
                (
                    f"import ida_bytes; ida_bytes.patch_bytes(0x{p.address:x}, {bytes(p.original_bytes)!r})"
                    if loop.host_name == "IDA Pro"
                    else f"bv.write(0x{p.address:x}, {bytes(p.original_bytes)!r})"
                )
                for p in reversed(state.patches_applied)
                if p.original_bytes
            ]
            if rollback_parts:
                try:
                    loop.tools.execute(constants.EXECUTE_PYTHON_TOOL_NAME, {"code": "; ".join(rollback_parts)})
                    rolled_back = True
                    log_info("Exploration mode: patches rolled back via execute_python")
                except ToolError as e:
                    log_error(f"Exploration mode: rollback failed: {e}")

        discard_msg = (
            "[SYSTEM] Patches discarded. Original bytes have been restored."
            if rolled_back
            else "[SYSTEM] Patches discarded. The in-memory changes persist "
            "until the analysis database is reloaded without saving."
        )
        loop.session.add_message(Message(role=Role.USER, content=discard_msg))
        yield TurnEvent.save_discarded(len(state.patches_applied), rolled_back)
        log_info(f"Exploration mode: patches discarded by user (rolled_back={rolled_back})")


def run_exploration_mode(
    loop: AgentLoop,
    user_message: str,
    system_prompt: str,
    tools_schema: list,
    explore_only: bool = False,
) -> Generator[TurnEvent, None, None]:
    """Run the agent in exploration mode: explore -> plan -> patch -> save.

    Uses :class:`ModePhaseTracker` so that on cancel + resume the pipeline
    skips to the phase that was interrupted.  The conversation history has
    all prior tool calls/results — the LLM picks up where it left off.
    """
    phases = ["explore"] if explore_only else ["explore", "plan", "execute", "save"]
    tracker = ModePhaseTracker(loop, phases=phases)

    state = ExplorationState(explore_only=explore_only)
    state.max_explore_turns = loop.config.exploration_turn_limit
    state.knowledge_base.user_goal = user_message
    loop._exploration_state = state

    exploration_system = system_prompt + EXPLORATION_SYSTEM_ADDENDUM
    log_info(
        f"Exploration mode started: goal={user_message[:80]!r}, explore_only={explore_only}, resuming={tracker.is_resuming}"
    )

    # ------------------------------------------------------------------
    # Phase 1: EXPLORE
    # ------------------------------------------------------------------
    if tracker.should_run("explore"):
        tracker.enter("explore")
        if not tracker.is_continuing("explore"):
            yield TurnEvent.exploration_phase_change("", "explore", f"Starting exploration: {user_message[:60]}")

        if not explore_only:
            yield from _run_phase1_subagent(loop, state, user_message, exploration_system)
        else:
            yield from run_phase1_inline(loop, state, exploration_system, tools_schema, explore_only)

        if explore_only:
            yield from _finalize_explore_memory(loop, state)
            log_info("Exploration mode finished (explore-only)")
            loop._clear_exploration_state()
            tracker.complete()
            return
    else:
        # Resuming past explore — advance the state machine so downstream
        # phase checks pass.  The conversation history has the prior findings.
        # For execute/save we fall back to plan since plan steps are lost.
        if tracker.resume_phase in ("execute", "save"):
            state.phase = ExplorationPhase.PLAN
        else:
            state.phase = ExplorationPhase.PLAN

    # ------------------------------------------------------------------
    # Phase 2: PLAN
    # ------------------------------------------------------------------
    if state.phase == ExplorationPhase.PLAN:
        tracker.enter("plan")
        steps = yield from _run_phase2_plan(loop, state, exploration_system, user_message)
        if steps is None:
            loop._clear_exploration_state()
            tracker.complete()
            return
    else:
        steps = []

    # ------------------------------------------------------------------
    # Phase 3: EXECUTE
    # ------------------------------------------------------------------
    if state.phase == ExplorationPhase.EXECUTE:
        tracker.enter("execute")
        ok = yield from _run_phase3_execute(
            loop,
            state,
            steps,
            exploration_system,
            tools_schema,
        )
        if not ok:
            loop._clear_exploration_state()
            tracker.complete()
            return
        # Phase 4: SAVE
        tracker.enter("save")
        yield from _run_phase4_save(loop, state)

    log_info("Exploration mode finished")
    tracker.complete()
    loop._clear_exploration_state()
