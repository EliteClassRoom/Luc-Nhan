"""Verified-only review pipeline for stored findings.

Independent tool-backed review used by both ``/report`` and the
post-explore memory finalizer. Every candidate record is run through
a fresh, tool-capable reviewer that must return a parsed JSON status
for each finding; failures are corrected by a separate agent and
re-verified, up to ``max_cycles`` (default 3) attempts.

The module never writes files, never mutates the knowledge store, and
never produces a partial passed result. :func:`review_memories`
exhausts the runner generators internally and returns a
:class:`ReviewResult` directly.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.errors import CancellationError
from ..core.logging import log_debug, log_error
from ..memory.schema import KnowledgeMemory
from .subagent import SubagentRunner


def _drain(generator: Generator[Any, None, str]) -> tuple[str, str | None]:
    """Exhaust a runner generator and capture its returned summary.

    Returns ``(final_text, error)``. ``error`` is populated only when
    the runner itself raised while iterating; cancellation errors are
    propagated to the caller.
    """
    final_text = ""
    it = iter(generator)
    try:
        while True:
            try:
                next(it)
            except StopIteration as stop:
                final_text = stop.value or ""
                break
    except CancellationError:
        # Cancellation must propagate unchanged so run() converts it into
        # a CANCELLED event — never stringify it as a runner failure.
        raise
    except Exception as exc:  # pragma: no cover - defensive
        return "", f"{type(exc).__name__}: {exc!r}"
    return final_text, None


@dataclass(frozen=True)
class ReviewResult:
    """Outcome of an independent verification cycle.

    Attributes:
        passed: True only when every input record passed review.
        records: Final in-memory copy of reviewed records. Caller may
            persist when ``passed`` is True.
        unresolved: Map of memory_id -> latest evidence/error when a
            review cycle could not confirm a record.
        cycles: Number of correction/reverification cycles used.
    """

    passed: bool
    records: list[KnowledgeMemory] = field(default_factory=list)
    unresolved: dict[str, str] = field(default_factory=dict)
    cycles: int = 0


def _format_record(memory: KnowledgeMemory) -> str:
    body = memory.content or ""
    return (
        f"- id: {memory.id}\n"
        f"  type: {memory.type}\n"
        f"  title: {memory.title}\n"
        f"  entity_refs: {memory.entity_refs}\n"
        f"  source_refs: {memory.source_refs}\n"
        f"  confidence: {memory.confidence}\n"
        f"  verified: {memory.verified}\n"
        f"  content: {body}"
    )


def _build_reviewer_prompt(records: list[KnowledgeMemory]) -> str:
    serialized = "\n".join(_format_record(r) for r in records)
    return (
        "You are an independent reviewer. You have ZERO prior context about "
        "this binary — the ONLY information you have is the list of findings "
        "below. Your job is to fact-check each finding against the actual "
        "binary using the available analysis tools.\n\n"
        "**USE YOUR TOOLS** to verify every claim:\n"
        "- decompile_function / read_disassembly to confirm functions at the "
        "addressed location actually exist and do what the finding claims\n"
        "- search_functions to verify function names\n"
        "- list_imports / list_strings to verify import/string claims\n"
        "- xrefs_to to verify cross-reference claims\n\n"
        "Do NOT trust the findings at face value. Verify, then respond.\n\n"
        "Return a single JSON object in EXACTLY this shape (no other text):\n"
        "{\n"
        '  "findings": [\n'
        '    {"id": "<id>", "status": "pass" or "fail", "evidence": "<tool-grounded evidence>", '
        '"corrected_content": "<required when fail>", "corrected_title": "<optional>", '
        '"confidence": <number between 0.0 and 1.0>}\n'
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "- Every input id must appear exactly once.\n"
        "- Use status='pass' only when tools confirm the finding.\n"
        "- Use status='fail' when any claim is wrong or unsupported; provide "
        "corrected_content with the corrected text.\n"
        "- evidence must be a non-empty string describing what tools you used "
        "and what they returned.\n"
        "- confidence must be a number between 0.0 and 1.0.\n\n"
        f"---\n\nFindings to review:\n\n{serialized}\n\n---\n\n"
        "Reply with the JSON object only."
    )


def _build_correction_prompt(records: list[KnowledgeMemory], feedback: dict[str, str]) -> str:
    lines: list[str] = []
    for r in records:
        lines.append(_format_record(r))
        lines.append(f"  reviewer_feedback: {feedback.get(r.id, '')}")
    serialized = "\n".join(lines)
    return (
        "You are a corrector. A reviewer verified the findings below and "
        "rejected the ones whose feedback you see. **USE YOUR TOOLS** "
        "(decompile_function, read_disassembly, search_functions, "
        "list_imports, xrefs_to, etc.) to look up the correct addresses, "
        "function names, and behavior. Produce corrected_title and "
        "corrected_content for every record whose feedback is non-empty. "
        "Preserve the record id verbatim.\n\n"
        "Return a single JSON object in EXACTLY this shape (no other text):\n"
        "{\n"
        '  "findings": [\n'
        '    {"id": "<id>", "status": "fail", "evidence": "<tool-grounded evidence>", '
        '"corrected_content": "<corrected text>", "corrected_title": "<optional>", '
        '"confidence": <number between 0.0 and 1.0>}\n'
        "  ]\n"
        "}\n\n"
        "Only include findings you actually corrected. Re-emit each id whose "
        "feedback you could not address so the reviewer sees a fail.\n\n"
        f"---\n\nRecords:\n\n{serialized}\n\n---"
    )


_FINDING_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(raw: str) -> dict[str, Any] | None:
    """Extract the first JSON object from a possibly noisy LLM response."""
    if not raw:
        return None
    match = _FINDING_RE.search(raw)
    candidate = match.group(0) if match else raw.strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_review_response(
    raw: str,
    expected_ids: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, str], str | None]:
    """Parse and validate a reviewer response.

    Returns ``(findings, unresolved, error)``. On success ``error`` is
    None and ``unresolved`` only contains failures. On malformed input
    ``error`` carries a human-readable reason and ``unresolved`` mirrors
    it for every expected id.
    """
    parsed = _extract_json(raw)
    if parsed is None:
        return {}, {}, "reviewer response was not valid JSON"
    findings_in = parsed.get("findings")
    if not isinstance(findings_in, list):
        return {}, {}, "reviewer response missing 'findings' list"

    seen: set[str] = set()
    result: dict[str, dict[str, Any]] = {}
    unresolved: dict[str, str] = {}
    error: str | None = None

    for entry in findings_in:
        if not isinstance(entry, dict):
            error = "reviewer entry is not an object"
            continue
        rid = entry.get("id", "")
        if not isinstance(rid, str) or not rid:
            error = "reviewer entry missing id"
            continue
        if rid in seen:
            error = f"duplicate id in reviewer response: {rid}"
            continue
        if rid not in expected_ids:
            error = f"unknown id in reviewer response: {rid}"
            continue
        seen.add(rid)
        status = entry.get("status")
        if status not in ("pass", "fail"):
            error = f"invalid status for {rid}: {status!r}"
            continue
        evidence = entry.get("evidence", "")
        if not isinstance(evidence, str) or not evidence.strip():
            error = f"missing evidence for {rid}"
            continue
        confidence = entry.get("confidence", 0.0)
        if not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
            error = f"confidence out of range for {rid}: {confidence!r}"
            continue
        if status == "fail":
            corrected = entry.get("corrected_content", "")
            if not isinstance(corrected, str) or not corrected.strip():
                error = f"missing corrected_content for {rid}"
                continue
        result[rid] = entry

    missing = expected_ids - set(result.keys())
    if missing:
        error = error or f"missing ids in reviewer response: {sorted(missing)}"
        for mid in missing:
            result[mid] = {"status": "fail", "evidence": error, "corrected_content": ""}

    for rid, entry in result.items():
        if entry.get("status") == "fail":
            unresolved[rid] = entry.get("evidence", "") or "no evidence"

    return result, unresolved, error


def _build_runner(loop: AgentLoop) -> SubagentRunner:
    return SubagentRunner(
        provider=loop.provider,
        tool_registry=loop.tools,
        config=loop.config,
        host_name=loop.host_name,
        skill_registry=loop.skills,
        parent_loop=loop,
    )


def review_memories(
    loop: AgentLoop,
    memories: list[KnowledgeMemory],
    *,
    max_cycles: int = 3,
    runner_factory: Callable[[], SubagentRunner] | None = None,
) -> ReviewResult:
    """Run independent verification + correction over *memories*.

    Each cycle performs a fresh tool-backed reviewer pass. If any
    record fails the cycle, a corrector is run over the failed
    records only and the loop starts a new reviewer pass with the
    corrected content. The loop stops on the first all-pass cycle,
    after ``max_cycles`` cycles, or when the reviewer/corrector
    errors out. A cancel observed between child runs raises
    ``CancellationError`` so ``run()`` converts it into a CANCELLED
    event.
    """
    if max_cycles < 1:
        raise ValueError("max_cycles must be >= 1")
    candidates: list[KnowledgeMemory] = [copy.copy(m) for m in memories]
    if not candidates:
        return ReviewResult(passed=True, records=[], unresolved={}, cycles=0)

    last_unresolved: dict[str, str] = {}
    last_error: str | None = None
    factory = runner_factory or (lambda: _build_runner(loop))

    for cycle in range(1, max_cycles + 1):
        # Abort before starting another child run if the user cancelled.
        if loop._cancelled.is_set():
            raise CancellationError("review_memories cancelled")
        reviewer = factory()
        review_prompt = _build_reviewer_prompt(candidates)
        raw, runner_err = _drain(reviewer.run_task(review_prompt, max_turns=8, silent=True))
        if runner_err is not None:
            log_error(f"review_memories: reviewer runner failed: {runner_err}")
            last_error = runner_err
            last_unresolved = {m.id: runner_err for m in candidates}
            continue

        parsed, unresolved, err = _parse_review_response(raw or "", {m.id for m in candidates})
        # If the response was malformed in any way, treat the whole
        # cycle as failed: a single err is enough to invalidate a
        # review, even if some records parsed as pass. No partial pass.
        if err is not None:
            last_error = err
            last_unresolved = {m.id: err for m in candidates}
            log_debug(f"review_memories: cycle {cycle} errored: {err}")
            continue
        last_unresolved = unresolved
        failed_ids = [m.id for m in candidates if parsed.get(m.id, {}).get("status") == "fail"]
        if not failed_ids:
            return ReviewResult(
                passed=True,
                records=candidates,
                unresolved={},
                cycles=cycle,
            )

        if cycle == max_cycles:
            break

        # Run corrector over failed records.
        feedback = {rid: parsed[rid].get("evidence", "") for rid in failed_ids}
        if loop._cancelled.is_set():
            raise CancellationError("review_memories cancelled")

        failed_records = [next(m for m in candidates if m.id == rid) for rid in failed_ids]
        corrector = factory()
        correct_prompt = _build_correction_prompt(failed_records, feedback)
        raw_corr, corr_runner_err = _drain(corrector.run_task(correct_prompt, max_turns=8, silent=True))
        if corr_runner_err is not None:
            log_error(f"review_memories: corrector runner failed: {corr_runner_err}")
            last_error = corr_runner_err
            last_unresolved = {rid: corr_runner_err for rid in failed_ids}
            continue
        corrected, _, corr_err = _parse_review_response(
            raw_corr or "",
            set(failed_ids),
        )
        if corr_err is not None:
            # Any malformed corrector output invalidates the cycle.
            last_error = corr_err
            last_unresolved = {rid: corr_err for rid in failed_ids}
            log_debug(f"review_memories: cycle {cycle} correction errored: {corr_err}")
            continue
        for rid in failed_ids:
            target = next(m for m in candidates if m.id == rid)
            entry = corrected.get(rid)
            if not entry or entry.get("status") != "fail":
                # Corrector did not address this id; mark as evidence.
                fallback = (
                    entry.get("evidence", "correction did not address finding")
                    if entry
                    else last_unresolved.get(rid, "correction missing")
                )
                last_unresolved[rid] = fallback
                continue
            new_title = entry.get("corrected_title") or target.title
            new_content = entry.get("corrected_content") or target.content
            try:
                target.title = new_title
                target.content = new_content
                target.confidence = float(entry.get("confidence", target.confidence))
            except (TypeError, ValueError) as exc:
                last_error = f"invalid corrected value for {rid}: {exc!r}"
                last_unresolved[rid] = last_error

    return ReviewResult(
        passed=False,
        records=candidates,
        unresolved=last_unresolved,
        cycles=max_cycles,
    )


def persist_reviewed_memories(
    store: Any,
    result: ReviewResult,
) -> int:
    """Upsert a fully-passed review set, marking every record verified.

    Returns the number of records persisted. Raises ``ValueError`` if
    ``result.passed`` is False; callers must gate on the verdict.
    """
    if not result.passed:
        raise ValueError("refusing to persist unverified review result")
    persisted = 0
    for memory in result.records:
        if memory.type == "report":
            continue
        memory.verified = True
        if memory.type == "hypothesis":
            memory.status = "verified"
        # Ensure updated_at is current while preserving created_at.
        from ..memory.ingest import _now_iso  # local import to avoid cycle

        memory.updated_at = _now_iso()
        store.upsert_memory(memory)
        persisted += 1
    return persisted


def empty_review_result() -> ReviewResult:
    """Return a placeholder :class:`ReviewResult` for callers that
    never invoked the reviewer (e.g. when an exploration batch
    contains only hypotheses). Used by the explore finalizer to
    feed ``_build_central_index`` without crashing on
    ``review.records`` access.
    """
    return ReviewResult(passed=True, records=[], unresolved={}, cycles=0)


__all__ = [
    "ReviewResult",
    "empty_review_result",
    "persist_reviewed_memories",
    "review_memories",
]
