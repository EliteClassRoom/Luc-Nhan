"""Independent hypothesis verification for ``/verify``.

The verifier spawns a fresh, tool-capable subagent that inspects each
pending hypothesis against the binary using only read-only IDA tools. The
subagent must return one JSON verdict per hypothesis ID in the exact
contract documented in :func:`_build_verifier_prompt`. Validation
rejects malformed JSON, missing/duplicate/unknown IDs, non-hypothesis
statuses, empty claims, empty citation lists, and any citation that
does not match ``function:``, ``address:``, or ``tool_result:`` prefixes.

A batch of mixed ``verified`` and ``wrong`` verdicts is acceptable as
long as every entry is valid; the commit is atomic at the command layer.
Up to ``max_attempts`` (default 3) are run; on full failure the
verdicts are not committed and the function returns ``passed=False``.

This module never mutates the analyzed IDA database and never auto-
approves mutating tool calls.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..core.errors import CancellationError
from ..core.logging import log_debug, log_error
from ..memory.schema import KnowledgeMemory
from .subagent import SubagentRunner

if TYPE_CHECKING:
    from .loop import AgentLoop


_CITATION_PREFIXES = ("function:", "address:", "tool_result:")
_CITATION_RE = re.compile(r"^(?:function|address|tool_result):.+$")
_VALID_STATUSES = {"verified", "wrong"}

# Match entity IDs of address-bearing types per the schema conventions
# in ``rikugan/memory.schema`` (func, string, global all carry a
# 0x-hex address; other types like import/struct/algo/capability/ioc/
# note/report do not). Anchored to the full ID so a stray segment
# (e.g. "func:0x401000:extra") does not slip through.
_ADDR_ENTITY_RE = re.compile(r"^(?:func|string|global):(0x[0-9a-fA-F]+)$")


@dataclass(frozen=True)
class HypothesisVerdict:
    """One verifier-issued verdict for a single hypothesis ID."""

    hypothesis_id: str
    status: str
    claim: str
    citations: tuple[str, ...]


@dataclass(frozen=True)
class HypothesisVerificationResult:
    """Outcome of the bounded verifier loop.

    Attributes:
        passed: True only when every expected ID has a valid verdict.
        verdicts: Per-id verdicts keyed by hypothesis id.
        unresolved: Map of hypothesis_id -> error string for any failure.
        attempts: Number of verifier attempts run (1..max_attempts).
    """

    passed: bool
    verdicts: dict[str, HypothesisVerdict] = field(default_factory=dict)
    unresolved: dict[str, str] = field(default_factory=dict)
    attempts: int = 0


def _drain(runner: SubagentRunner, prompt: str) -> tuple[str, str | None]:
    """Exhaust a runner generator and capture the final assistant text.

    Accepts either a generator whose ``return`` value carries the final
    text, or an iterable of text chunks whose joined value is the text.
    """
    final_text = ""
    collected: list[str] = []
    try:
        it = iter(runner.run_task(prompt, max_turns=8, silent=True))
        while True:
            try:
                piece = next(it)
            except StopIteration as stop:
                final_text = stop.value or ""
                break
            if isinstance(piece, str):
                collected.append(piece)
    except CancellationError:
        # Cancellation must propagate unchanged so run() converts it into
        # a CANCELLED event — never stringify it as a runner failure.
        raise
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc!r}"
    if not final_text:
        final_text = "".join(collected)
    return final_text, None


def _format_record(memory: KnowledgeMemory) -> str:
    addr = "n/a"
    for ref in memory.entity_refs or ():
        m = _ADDR_ENTITY_RE.match(ref)
        if m:
            addr = m.group(1).lower()
            break
    return (
        f"- id: {memory.id}\n"
        f"  title: {memory.title}\n"
        f"  content: {memory.content}\n"
        f"  address_hint: {addr}\n"
        f"  evidence: {memory.source_refs[0] if memory.source_refs else ''}\n"
        f"  entity_refs: {memory.entity_refs}"
    )


def _build_verifier_prompt(records: list[KnowledgeMemory]) -> str:
    serialized = "\n".join(_format_record(r) for r in records)
    return (
        "You are an independent hypothesis verifier. You have ZERO prior context about\n"
        "this binary other than the list of hypotheses below. Use ONLY the read-only\n"
        "tools available to you (decompile_function, read_disassembly, search_functions,\n"
        "list_imports, list_strings, xrefs_to, get_function_info, etc.). NEVER mutate\n"
        "the database, NEVER run scripts, NEVER execute the target.\n\n"
        "For EACH hypothesis, decide whether the claim is correct ('verified') or\n"
        "incorrect ('wrong'). You must produce a verdict for every id listed.\n\n"
        "Required JSON shape (one object only, no other text):\n"
        "{\n"
        '  "verdicts": [\n'
        '    {"id": "<hypothesis-id>", "status": "verified" or "wrong",\n'
        '     "claim": "<non-empty explanation of why the hypothesis is right or wrong>",\n'
        '     "citations": ["<citation>", ...] }\n'
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "- Every id listed below must appear exactly once in the verdicts array.\n"
        "- 'claim' must be a non-empty string describing what you found.\n"
        "- 'citations' must contain at least one entry.\n"
        "- Each citation MUST start with one of: 'function:', 'address:', or 'tool_result:'.\n"
        "- For function citations: 'function:<symbol-or-ida-name>'.\n"
        "- For address citations: 'address:0x<hex>'.\n"
        "- For tool citations: 'tool_result:<exact tool call id or short marker>'.\n"
        "- Do not invent or guess. If you cannot find tool-grounded evidence, return 'wrong'\n"
        "  with a claim explaining the missing evidence and cite what you tried.\n\n"
        f"Hypotheses to verify:\n\n{serialized}\n\n---\n\nReply with the JSON object only."
    )


def _parse_verifier_response(
    raw: str,
    expected_ids: set[str],
) -> tuple[dict[str, HypothesisVerdict], dict[str, str], str | None]:
    """Parse and validate the verifier JSON response.

    Returns ``(verdicts, unresolved, error)``. On success ``error`` is
    ``None`` and every expected id is present in ``verdicts``. On
    malformed input ``error`` carries a human-readable reason and all
    expected ids land in ``unresolved``.
    """
    if not raw or not raw.strip():
        return {}, {mid: "verifier returned no response" for mid in expected_ids}, "empty response"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Best effort: find the first JSON object substring.
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return {}, {mid: "verifier response was not valid JSON" for mid in expected_ids}, "invalid JSON"
        try:
            parsed = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return {}, {mid: "verifier response was not valid JSON" for mid in expected_ids}, "invalid JSON"
    if not isinstance(parsed, dict):
        return {}, {mid: "verifier response missing 'verdicts' list" for mid in expected_ids}, "missing verdicts"
    items = parsed.get("verdicts")
    if not isinstance(items, list):
        return {}, {mid: "verifier response missing 'verdicts' list" for mid in expected_ids}, "missing verdicts list"

    seen: set[str] = set()
    verdicts: dict[str, HypothesisVerdict] = {}
    unresolved: dict[str, str] = {}
    first_error: str | None = None
    for entry in items:
        if not isinstance(entry, dict):
            first_error = first_error or "verifier entry is not an object"
            continue
        rid = entry.get("id", "")
        if not isinstance(rid, str) or not rid:
            first_error = first_error or "verifier entry missing id"
            continue
        if rid in seen:
            first_error = first_error or f"duplicate id in verifier response: {rid}"
            continue
        if rid not in expected_ids:
            first_error = first_error or f"unknown id in verifier response: {rid}"
            continue
        seen.add(rid)
        status = entry.get("status")
        if status not in _VALID_STATUSES:
            first_error = first_error or f"invalid status for {rid}: {status!r}"
            continue
        claim = entry.get("claim", "")
        if not isinstance(claim, str) or not claim.strip():
            first_error = first_error or f"missing claim for {rid}"
            continue
        citations = entry.get("citations", [])
        if not isinstance(citations, list) or not citations:
            first_error = first_error or f"missing citations for {rid}"
            continue
        if not all(isinstance(c, str) and _CITATION_RE.match(c) for c in citations):
            first_error = first_error or f"malformed citation for {rid}"
            continue
        verdicts[rid] = HypothesisVerdict(
            hypothesis_id=rid,
            status=status,
            claim=claim.strip(),
            citations=tuple(citations),
        )

    missing = expected_ids - set(verdicts.keys())
    if missing:
        first_error = first_error or f"missing ids in verifier response: {sorted(missing)}"
        for mid in missing:
            unresolved[mid] = first_error
    for rid, _verdict in verdicts.items():
        unresolved.setdefault(rid, "")  # valid; empty marker
    return verdicts, unresolved, first_error


def _build_runner(loop: AgentLoop) -> SubagentRunner:
    """Build a verifier subagent that cannot mutate the analyzed binary.

    The verifier is given a read-only view of the parent's tool
    registry (tools whose ``mutating`` flag is set are filtered out).
    This is a hard contract rather than a prompt instruction: even
    if the subagent ignores the read-only directive in its system
    prompt, dispatching a mutating tool against the read-only
    registry will raise :class:`ToolNotFoundError`.
    """
    read_only = loop.tools.read_only_view()
    return SubagentRunner(
        provider=loop.provider,
        tool_registry=read_only,
        config=loop.config,
        host_name=loop.host_name,
        skill_registry=loop.skills,
        parent_loop=loop,
    )


def verify_hypotheses(
    loop: AgentLoop,
    hypotheses: list[KnowledgeMemory],
    *,
    max_attempts: int = 3,
    runner_factory: Callable[[], SubagentRunner] | None = None,
) -> HypothesisVerificationResult:
    """Run the bounded independent hypothesis verifier.

    The verifier is read-only with respect to the analyzed IDA database.
    Any citation that cannot be tied to a function, address, or tool
    result is rejected; the entire attempt is treated as failed and
    no record is mutated. A cancel observed between attempts raises
    ``CancellationError`` so ``run()`` converts it into a CANCELLED
    event.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if not hypotheses:
        return HypothesisVerificationResult(passed=True, attempts=0)
    expected = {h.id for h in hypotheses}
    factory = runner_factory or (lambda: _build_runner(loop))

    last_error: str | None = None
    last_unresolved: dict[str, str] = {}
    for attempt in range(1, max_attempts + 1):
        # Abort before starting another child run if the user cancelled.
        if loop._cancelled.is_set():
            raise CancellationError("verify_hypotheses cancelled")
        try:
            runner = factory()
        except Exception as exc:
            log_error(f"verify_hypotheses: runner factory failed: {exc}")
            return HypothesisVerificationResult(
                passed=False,
                unresolved={h.id: f"runner factory failed: {exc!r}" for h in hypotheses},
                attempts=attempt,
            )
        prompt = _build_verifier_prompt(hypotheses)
        raw, runner_err = _drain(runner, prompt)
        if runner_err is not None:
            log_error(f"verify_hypotheses: runner failed: {runner_err}")
            last_error = runner_err
            last_unresolved = {h.id: runner_err for h in hypotheses}
            continue
        verdicts, unresolved, err = _parse_verifier_response(raw or "", expected)
        if err is not None:
            log_debug(f"verify_hypotheses: attempt {attempt} invalid: {err}")
            last_error = err
            last_unresolved = unresolved
            continue
        if len(verdicts) == len(expected):
            return HypothesisVerificationResult(
                passed=True,
                verdicts=verdicts,
                unresolved={k: v for k, v in unresolved.items() if v},
                attempts=attempt,
            )
        last_error = "verifier did not return verdicts for every hypothesis"
        last_unresolved = {h.id: last_error for h in hypotheses if h.id not in verdicts}

    return HypothesisVerificationResult(
        passed=False,
        unresolved=last_unresolved or {h.id: (last_error or "verifier failed") for h in hypotheses},
        attempts=max_attempts,
    )


__all__ = [
    "HypothesisVerdict",
    "HypothesisVerificationResult",
    "verify_hypotheses",
]
