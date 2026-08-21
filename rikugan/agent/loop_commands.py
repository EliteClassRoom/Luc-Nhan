"""Slash-command handlers for the agent loop.

These generators were extracted verbatim from ``rikugan.agent.loop`` so
that ``AgentLoop`` only contains the turn orchestration logic, while
standalone commands (/goal, /memory, /undo, /mcp, /doctor) live here.

Each function receives the :class:`AgentLoop` instance as ``loop`` and
yields :class:`TurnEvent` objects exactly like the original methods did.
No command logic was changed.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from typing import TYPE_CHECKING

from ..core.errors import ToolError
from ..core.logging import log_debug, log_error, log_info
from ..core.sanitize import strip_injection_markers
from .turn import TurnEvent

if TYPE_CHECKING:
    from ..memory.paths import KnowledgePaths
    from ..memory.raw_store import KnowledgeRawStore
    from .loop import AgentLoop


def _truncate_report_preview(body: str, cap: int = 1500) -> str:
    """Truncate a report body for the chat draft without leaving
    Markdown broken open.

    Two failure modes the chat bubble must not show:

    1. A character-cut that lands inside an opening triple-backtick
       code fence.  The bubble then renders a ``<pre>`` block that
       has no closing fence in the visible window, so the trailing
       prose collapses into the code block — the user sees a
       heading, a long source-code dump, and no body.
    2. A character-cut that lands in the middle of a bullet,
       heading, or paragraph — the user sees an unfinished sentence
       with no period.

    The fix cuts on a structural boundary (paragraph break, fence
    close, or fence open = strip the trailing partial block) and
    always balances fences so the rendered HTML closes every
    ``<pre>`` it opens.
    """
    if len(body) <= cap:
        return body
    head = body[:cap]
    # Split into lines for structural decisions.
    lines = head.split("\n")
    # Track fence balance.
    fence_open = False
    fence_marker_at: list[int] = []
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            if not fence_open:
                fence_open = True
                fence_marker_at.append(idx)
            else:
                # Closing fence for the current block.
                fence_open = False
    truncated_lines: list[str] = []
    fence_open = False
    kept_idx = 0
    for idx, line in enumerate(lines):
        kept_idx = idx
        stripped = line.lstrip()
        if stripped.startswith("```"):
            if fence_open:
                fence_open = False
            else:
                fence_open = True
            truncated_lines.append(line)
            continue
        truncated_lines.append(line)
        if idx + 1 >= len(lines):
            break
        # If the next line is well past the cap, stop here on a
        # paragraph-break / blank-line boundary so we don't end on
        # a half sentence.
        consumed = sum(len(x) + 1 for x in truncated_lines)
        next_line = lines[idx + 1]
        next_is_paragraph_break = next_line.strip() == ""
        next_is_fence = next_line.lstrip().startswith("```")
        is_inside_open_fence = fence_open
        if consumed >= cap * 0.85 and not is_inside_open_fence and (next_is_paragraph_break or next_is_fence):
            kept_idx = idx
            break
    # ``kept_idx`` is consumed; rebuild the truncated body.
    out = "\n".join(truncated_lines[: kept_idx + 1])
    # If we cut while a fence was still open, drop everything from
    # the trailing open fence onward so the rendered HTML is balanced.
    if fence_open:
        last_open = max(fence_marker_at)
        out = "\n".join(truncated_lines[:last_open]).rstrip()
    out = out.rstrip()
    return out + "\n\n…(truncated; write the full report to view the rest)…"


def _report_draft_fingerprint(body: str) -> str:
    """12-char SHA-256 prefix of the synthesized report body.

    Diagnostic-only: lets a developer correlate "draft was empty in
    the UI" with what the LLM actually returned, without writing
    report contents (which may carry sensitive analysis) to the
    persistent debug log.
    """
    if not body:
        return ""
    import hashlib

    return hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()[:12]


_MAX_GOAL_CHARS = 1000
ACTIVE_GOAL_METADATA_KEY = "active_goal"


def normalize_goal(raw_goal: str) -> str:
    """Sanitize, trim, and cap a raw goal string.

    Used by both the state-only `/goal` direct command and the parser
    branch that converts `/goal <objective>` into a normal run. Strips
    injection markers and caps length so the active goal is safe to
    inject into the system prompt via ``quote_untrusted`` later.
    """
    goal = strip_injection_markers(raw_goal.strip())
    if len(goal) > _MAX_GOAL_CHARS:
        goal = goal[:_MAX_GOAL_CHARS].rstrip() + "..."
    return goal


# ---------------------------------------------------------------------------
# Shared guards for knowledge-store-backed slash commands (/knowledge, /report)
# ---------------------------------------------------------------------------


def _open_knowledge_store(loop: AgentLoop) -> tuple[KnowledgeRawStore | None, KnowledgePaths | None, TurnEvent | None]:
    """Centralize the "is the knowledge store usable?" guard.

    ``/knowledge`` and ``/report`` both need: a real :class:`AgentLoop`
    config with ``knowledge_enabled=True``, an IDB path, and a
    successfully-constructed :class:`KnowledgeRawStore`.  Sharing the
    guard here keeps the user-facing messages consistent and avoids
    the duplicate four-step boilerplate that previously lived in both
    handlers.

    Returns ``(store, paths, None)`` on success and
    ``(None, None, event)`` on failure.  Callers should ``yield`` the
    event when present and ``return`` to short-circuit the command.
    """
    if not getattr(loop.config, "knowledge_enabled", True):
        return (
            None,
            None,
            TurnEvent.text_done(
                "Raw knowledge memory is disabled in settings "
                "(`knowledge_enabled = False`). Re-enable it in "
                "Settings → Behavior or `RikuganConfig` to use the "
                "knowledge store."
            ),
        )

    idb_path = loop.session.idb_path or ""
    if not idb_path:
        return (
            None,
            None,
            TurnEvent.text_done("No IDB path is set, so the raw knowledge store is not available."),
        )

    from ..memory.ingest import make_store

    store, paths = make_store(idb_path)
    if store is None or paths is None:
        return (
            None,
            None,
            TurnEvent.text_done("Could not initialize the knowledge store."),
        )
    return store, paths, None


def _handle_memory_command(loop: AgentLoop) -> Generator[TurnEvent, None, None]:
    """Show current memory contents in chat.

    Reads from BinaryMemoryService (SQLite structured facts + unmanaged
    MEMORY.md notes). When memory_service is None (identity resolution
    failed), reports central memory unavailable.
    """
    if loop.memory_service is None:
        yield TurnEvent.text_done("Central memory is not available for this binary.")
        return

    try:
        structured = loop.memory_service.structured_context()
        manual = loop.memory_service.manual_notes_context()
        parts = []
        if structured:
            parts.append(structured)
        if manual:
            parts.append(f"\n## Manual Notes\n{manual}")
        if not parts:
            yield TurnEvent.text_done("No memory saved yet. Use `save_memory` to persist facts.")
        else:
            yield TurnEvent.text_done("**Memory**:\n\n" + "\n".join(parts))
    except Exception as e:
        yield TurnEvent.error_event(f"Failed to read central memory: {e}")


def _handle_goal_command(loop: AgentLoop, raw_goal: str) -> Generator[TurnEvent, None, None]:
    goal = normalize_goal(raw_goal)
    if not goal:
        current = loop.session.metadata.get(ACTIVE_GOAL_METADATA_KEY, "").strip()
        if current:
            yield TurnEvent.text_done(f"**Active Goal**\n\n{current}")
        else:
            yield TurnEvent.text_done("No active goal set. Use `/goal <objective>` to set one.")
        return

    if goal.lower() in {"clear", "reset", "unset"}:
        loop.session.metadata.pop(ACTIVE_GOAL_METADATA_KEY, None)
        yield TurnEvent.text_done("Active goal cleared.")
        return

    loop.session.metadata[ACTIVE_GOAL_METADATA_KEY] = goal
    yield TurnEvent.text_done(f"Active goal set:\n\n{goal}")


def _handle_undo_command(loop: AgentLoop, raw_cmd: str) -> Generator[TurnEvent, None, None]:
    """Undo the last N mutations."""
    # Parse count from "/undo" or "/undo N"
    parts = raw_cmd.strip().split()
    count = 1
    if len(parts) > 1:
        try:
            count = int(parts[1])
        except ValueError:
            yield TurnEvent.error_event(f"Invalid undo count: {parts[1]}. Usage: /undo [N]")
            return

    if not loop._mutation_log:
        yield TurnEvent.text_done("Nothing to undo — mutation log is empty.")
        return

    count = min(count, len(loop._mutation_log))
    undone = 0
    errors = []
    for _ in range(count):
        record = loop._mutation_log.pop()
        if not record.reversible:
            errors.append(f"Cannot undo: {record.description} (not reversible)")
            continue
        try:
            loop.tools.execute(record.reverse_tool, record.reverse_arguments)
            undone += 1
            log_info(f"Undo: {record.description}")
        except ToolError as e:
            errors.append(f"Failed to undo {record.description}: {e}")
            log_error(f"Undo failed: {record.description}: {e}")

    parts_out = []
    if undone:
        parts_out.append(f"Undid {undone} mutation(s).")
    if errors:
        parts_out.append("\n".join(errors))
    yield TurnEvent.text_done("\n".join(parts_out) if parts_out else "Nothing undone.")


def _handle_mcp_command(loop: AgentLoop) -> Generator[TurnEvent, None, None]:
    """Show MCP server health and status."""
    # Access the MCP manager via the tool registry's registered tools
    # We check for MCP-prefixed tools and try to reach the manager
    mcp_tools = [n for n in loop.tools.list_names() if n.startswith("mcp_")]
    if not mcp_tools:
        yield TurnEvent.text_done("No MCP servers configured or connected.")
        return

    lines = ["**MCP Server Status**\n"]
    # Group tools by server prefix
    servers: dict[str, list[str]] = {}
    for name in mcp_tools:
        # MCP tools are named mcp_<server>_<tool>
        parts = name.split("_", 2)
        server = parts[1] if len(parts) >= 3 else "unknown"
        servers.setdefault(server, []).append(name)

    for server, tools in sorted(servers.items()):
        lines.append(f"- **{server}**: {len(tools)} tools registered")

    lines.append(f"\n**Total**: {len(mcp_tools)} MCP tools available")
    yield TurnEvent.text_done("\n".join(lines))


def _handle_report_command(loop: AgentLoop, raw_scope: str) -> Generator[TurnEvent, None, None]:
    """Generate a Markdown report from stored knowledge.

    Usage: ``/report`` (default scope: ``full``) or ``/report <scope>``
    where scope is one of: ``full``, ``executive``, ``technical``,
    ``iocs``, ``network``.
    """
    from ..memory.report import (
        SUPPORTED_SCOPES,
        build_report_context,
        save_report,
        synthesize_report,
        verified_memories,
    )

    store, paths, err_event = _open_knowledge_store(loop)
    if err_event is not None:
        yield err_event
        return

    scope = (raw_scope or "full").strip().lower() or "full"
    if scope not in SUPPORTED_SCOPES:
        yield TurnEvent.text_done(f"Unknown report scope: `{scope}`. Supported: {', '.join(SUPPORTED_SCOPES)}.")
        return

    try:
        ctx = build_report_context(store, paths, scope=scope)
    except Exception as e:
        yield TurnEvent.error_event(f"Failed to assemble report context: {e}")
        return
    if ctx.is_empty():
        yield TurnEvent.text_done(
            "No stored knowledge to report. Try running `/research <goal>`, "
            "`save_memory`, or `exploration_report` first."
        )
        return

    provider = getattr(loop, "provider", None)
    if provider is None:
        yield TurnEvent.text_done("No LLM provider is configured — cannot synthesize the report.")
        return

    # Static binary evidence: decompile (or disassemble) the addresses
    # cited by verified memories so the draft is grounded in real code.
    # ``getattr(loop, "tools", None)`` keeps fake-loop tests working —
    # a missing registry yields empty evidence and the report still
    # generates (the writer then omits `### Evidence` subsections).
    from ..agent.report_evidence import collect_binary_evidence, fetch_binary_info

    registry = getattr(loop, "tools", None)
    memories = verified_memories(store)
    evidence = collect_binary_evidence(memories, registry, scope=scope)
    binary_info = fetch_binary_info(registry)

    try:
        ctx, report_md = synthesize_report(
            store,
            paths,
            scope=scope,
            provider=provider,
            config=loop.config,
            # Pass the current chat history so the writer can use it
            # for narrative and ordering. The report helper sanitizes
            # and bounds this input; verified facts come only from the
            # knowledge store.
            conversation_context=getattr(loop.session, "messages", ()),
            evidence=evidence,
            binary_info=binary_info,
        )
    except Exception as e:
        yield TurnEvent.error_event(f"Report generation failed: {e}")
        return
    draft = (report_md or "").strip()
    if not draft:
        yield TurnEvent.error_event(
            "Report generation returned an empty draft. The LLM did not produce a body — nothing to write."
        )
        return
    preview = _truncate_report_preview(report_md)

    draft_event_text = (
        f"**Report draft** — review the verified report below before choosing whether to write it.\n\n{preview}"
    )
    # Diagnostic: log lengths (not bodies — reports may carry sensitive
    # analysis) plus a SHA-256 prefix for cross-run correlation. This
    # tells us whether the text the user "saw" as empty was actually
    # emitted to ChatView in the first place.
    log_debug(
        f"REPORT_DRAFT: synthesized={len(report_md)} preview={len(preview)} "
        f"event={len(draft_event_text)} sha={_report_draft_fingerprint(report_md)}"
    )
    yield TurnEvent.text_done(draft_event_text)
    yield TurnEvent.user_question(
        "Write the verified report to disk?",
        ["Write report", "Cancel"],
        tool_call_id="report_write",
        allow_text=True,
    )
    answer = loop._wait_for_queue(loop._user_answer_queue).strip().lower()
    if answer not in {"write report", "write", "yes", "save", "1"}:
        yield TurnEvent.text_done(
            "**Report discarded** — no file was written.\n\n"
            f"Scope: `{scope}` · Counts: {ctx.counts['memories']} memories · "
            f"{ctx.counts['entities']} entities · {ctx.counts['relations']} relations · "
            f"{ctx.counts['notes']} notes"
        )
        return

    try:
        result = save_report(store, paths, report_md, scope=scope)
    except Exception as e:
        yield TurnEvent.error_event(f"Failed to write report file: {e}")
        return
    if not result.ingested:
        yield TurnEvent.error_event(
            f"Report file written to `{result.file_path}` but ingestion failed: {result.ingest_error}"
        )
        return
    yield TurnEvent.text_done(
        f"**Report saved** — `{result.file_path}`\n\nScope: `{scope}` · "
        f"Counts: {ctx.counts['memories']} memories · "
        f"{ctx.counts['entities']} entities · "
        f"{ctx.counts['relations']} relations · "
        f"{ctx.counts['notes']} notes\n\n{preview}"
    )


_HYPOTHESIS_STATUS_MARKER = {
    "verified": "✓ verified",
    "wrong": "✗ wrong",
    "unverified": "◌ unverified",
}


def _hypothesis_status_marker(memory) -> str:
    """Return a short, explicit status flag for ``/knowledge`` output.

    Hypothesis records use the three explicit status markers; other
    memory types keep the legacy single-check rendering.
    """
    if memory.type == "hypothesis":
        marker = _HYPOTHESIS_STATUS_MARKER.get(memory.status, f"? {memory.status}")
        return f" {marker}"
    return " ✓" if memory.verified else ""


def _verdict_evidence_lines(memory, *, indent: str = "  ") -> list[str]:
    """Return indented lines showing the verdict claim + citations.

    Empty list when the memory is not a checked hypothesis. Used by
    the ``/knowledge`` command so the user can see *why* a claim was
    judged verified or wrong, not just the verdict status.
    """
    if memory.type != "hypothesis" or memory.status not in {"verified", "wrong"}:
        return []
    out: list[str] = []
    if memory.verdict_claim:
        claim = memory.verdict_claim
        if len(claim) > 240:
            claim = claim[:239].rstrip() + "…"
        out.append(f"{indent}verdict: {claim}")
    if memory.verification_citations:
        cites = ", ".join(memory.verification_citations[:6])
        if len(cites) > 240:
            cites = cites[:239].rstrip() + "…"
        out.append(f"{indent}citations: {cites}")
    return out


def _handle_knowledge_command(loop: AgentLoop, raw_query: str) -> Generator[TurnEvent, None, None]:
    """Show knowledge counts or search stored knowledge.

    ``/knowledge`` → counts + most-recent items.
    ``/knowledge <query>`` → ranked search across memories/entities/relations/notes.
    """
    from ..memory.retrieve import search_all

    store, paths, err_event = _open_knowledge_store(loop)
    if err_event is not None:
        yield err_event
        return

    query = (raw_query or "").strip()
    counts = store.counts()

    # No query: dump counts + a few of the newest records.
    if not query:
        recent = store.list_memories()[-5:][::-1]
        lines = ["**Knowledge Memory — Overview**", ""]
        lines.append(
            f"Counts: {counts['memories']} memories · "
            f"{counts['entities']} entities · "
            f"{counts['relations']} relations · "
            f"{counts['observations']} observations"
        )
        lines.append(f"Storage: `{paths.kb_dir}`")
        if recent:
            lines.append("")
            lines.append("Recent memories:")
            for m in recent:
                flag = _hypothesis_status_marker(m)
                lines.append(f"- `{m.id}`{flag} — {m.title}")
                lines.extend(_verdict_evidence_lines(m))
        else:
            lines.append("")
            lines.append("No memories yet. Use `/research`, `save_memory`, or `exploration_report` to populate.")
        yield TurnEvent.text_done("\n".join(lines))
        return

    # Search path
    try:
        result = search_all(store, query, max_results=20)
    except Exception as e:
        yield TurnEvent.error_event(f"Knowledge search failed: {e}")
        return
    lines = [f"**Knowledge Search — `{query}`**", ""]
    lines.append(
        f"Matched: {len(result['memories'])} memories, "
        f"{len(result['entities'])} entities, "
        f"{len(result['relations'])} relations, "
        f"{len(result['notes'])} note excerpts"
    )
    if result["memories"]:
        lines.append("")
        lines.append("### Memories")
        for m in result["memories"][:10]:
            flag = _hypothesis_status_marker(m)
            snippet = (m.content or "").splitlines()[0] if m.content else ""
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            lines.append(f"- `{m.id}`{flag} — {m.title}")
            if snippet:
                lines.append(f"  {snippet}")
            lines.extend(_verdict_evidence_lines(m))
        lines.append("### Entities")
        for e in result["entities"][:10]:
            addr = f" @ {e.address}" if e.address else ""
            lines.append(f"- `{e.id}` ({e.type}){addr} — {e.name}")

    if result["relations"]:
        lines.append("")
        lines.append("### Relations")
        for r in result["relations"][:10]:
            lines.append(f"- `{r.src}` → *{r.predicate}* → `{r.dst}`")

    if result["notes"]:
        lines.append("")
        lines.append("### Note excerpts")
        for n in result["notes"][:3]:
            excerpt = (n or "").strip()
            if len(excerpt) > 400:
                excerpt = excerpt[:400] + "…"
            lines.append(f"```\n{excerpt}\n```")

    if not any([result["memories"], result["entities"], result["relations"], result["notes"]]):
        lines.append("")
        lines.append("No matches. Try a hex address (`0x401000`), a function name, a tag, or a free-text term.")

    yield TurnEvent.text_done("\n".join(lines))


def _handle_doctor_command(loop: AgentLoop) -> Generator[TurnEvent, None, None]:
    """Diagnose common setup issues."""
    issues: list[str] = []
    ok: list[str] = []

    # Check provider
    if loop.provider:
        ok.append(f"Provider: {loop.config.provider.name} ({loop.config.provider.model})")
    else:
        issues.append("No LLM provider configured")

    # Check API key
    if loop.config.provider.api_key:
        ok.append("API key: configured")
    else:
        env_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if env_key:
            ok.append("API key: from environment variable")
        else:
            issues.append("No API key configured (set in config or environment)")

    # Check tools
    tool_count = len(loop.tools.list_names())
    if tool_count > 0:
        ok.append(f"Tools: {tool_count} registered")
    else:
        issues.append("No tools registered — check plugin initialization")

    # Check skills
    if loop.skills:
        slugs = loop.skills.list_slugs()
        ok.append(f"Skills: {len(slugs)} loaded")
    else:
        issues.append("No skill registry — skills won't be available")

    # Check context window
    from .loop import _MIN_CONTEXT_WINDOW_TOKENS

    ctx = loop.config.provider.context_window
    if ctx >= _MIN_CONTEXT_WINDOW_TOKENS:
        ok.append(f"Context window: {ctx:,} tokens")
    else:
        issues.append(f"Context window very small: {ctx} tokens")

    # Check config validation
    config_errors = loop.config.validate()
    if config_errors:
        issues.extend(f"Config: {e}" for e in config_errors)
    else:
        ok.append("Config: valid")

    # Check IDB path for persistent memory
    if loop.session.idb_path:
        ok.append(f"IDB: {loop.session.idb_path}")
    else:
        issues.append("No IDB path — persistent memory disabled")

    # Surface missing optional Python deps so users know which
    # provider features are unavailable. We don't treat these as
    # "issues" because the plugin can still run; they're warnings.
    try:
        from ...core.dependencies import get_missing_dependency_warnings

        for warning in get_missing_dependency_warnings():
            issues.append(warning)
    except Exception:
        pass

    # Format output
    lines = ["**Luc Nhan Doctor**\n"]
    if ok:
        lines.append("**OK:**")
        for item in ok:
            lines.append(f"  - {item}")
    if issues:
        lines.append("\n**Issues:**")
        for item in issues:
            lines.append(f"  - {item}")
    else:
        lines.append("\nNo issues found.")
    yield TurnEvent.text_done("\n".join(lines))


def _handle_verify_command(loop: AgentLoop, raw_id: str) -> Generator[TurnEvent, None, None]:
    """Independent hypothesis verification.

    ``/verify`` with no argument selects every hypothesis record whose
    status is ``unverified``. ``/verify <id>`` selects exactly that
    hypothesis. The selected records are passed to a fresh, read-only
    verifier subagent that returns a JSON verdict per id. On a fully
    valid batch the verdict fields are committed atomically to both
    stores and a ``HYPOTHESIS_VERDICT`` event is emitted for each
    record; otherwise a terminal error event is emitted and no record
    is mutated.
    """
    import uuid

    from ..memory.ingest import _now_iso
    from ..memory.schema import KnowledgeMemory, KnowledgeObservation
    from .hypothesis_verification import verify_hypotheses

    store, paths, err_event = _open_knowledge_store(loop)
    if err_event is not None:
        yield err_event
        return
    if paths is None or store is None:
        yield TurnEvent.error_event("Knowledge store is unavailable; cannot run /verify.")
        return

    try:
        all_mems = store.list_memories()
    except Exception as e:
        yield TurnEvent.error_event(f"Knowledge read failed: {e}")
        return
    pending = [m for m in all_mems if m.type == "hypothesis" and m.status == "unverified"]
    pending.sort(key=lambda m: m.id)
    raw = (raw_id or "").strip()
    if raw:
        target = [m for m in all_mems if m.id == raw]
        if not target:
            yield TurnEvent.text_done(f"No memory found with id `{raw}`.")
            return
        mem = target[0]
        if mem.type != "hypothesis":
            yield TurnEvent.text_done(
                f"`{raw}` is not a hypothesis (type={mem.type}); /verify only handles hypotheses."
            )
            return
        if mem.status != "unverified":
            yield TurnEvent.text_done(f"`{raw}` is already {mem.status}; nothing to verify.")
            return
        candidates = [mem]
    else:
        candidates = pending
    if not candidates:
        yield TurnEvent.text_done("No unverified hypotheses pending /verify.")
        return

    yield TurnEvent.text_done(f"Verifying {len(candidates)} hypothesis(es) with an independent agent (read-only)…")
    result = verify_hypotheses(loop, candidates, max_attempts=3)
    if not result.passed:
        unresolved = result.unresolved or {h.id: "verifier failed" for h in candidates}
        joined = "; ".join(f"{mid}: {reason}" for mid, reason in sorted(unresolved.items()))
        yield TurnEvent.error_event(
            f"/verify did not produce a valid verdict batch after {result.attempts} attempt(s): {joined}"
        )
        return

    updated_memories: dict[str, KnowledgeMemory] = {}
    new_observations: list[KnowledgeObservation] = []
    for hid, verdict in result.verdicts.items():
        original = next((m for m in candidates if m.id == hid), None)
        if original is None:
            continue
        updated = KnowledgeMemory(
            id=original.id,
            binary_id=original.binary_id,
            type=original.type,
            title=original.title,
            content=original.content,
            entity_refs=list(original.entity_refs),
            relation_refs=list(original.relation_refs),
            source_refs=list(original.source_refs),
            tags=list(original.tags),
            confidence=original.confidence,
            importance=original.importance,
            verified=(verdict.status == "verified"),
            status=verdict.status,
            verdict_claim=verdict.claim,
            verification_citations=list(verdict.citations),
        )
        updated_memories[hid] = updated
        new_observations.append(
            KnowledgeObservation(
                id=f"obs:{uuid.uuid4().hex[:12]}",
                binary_id=updated.binary_id,
                ts=_now_iso(),
                kind="hypothesis_verified",
                payload={
                    "memory_id": updated.id,
                    "status": updated.status,
                    "claim": updated.verdict_claim[:200],
                    "citations": list(updated.verification_citations),
                },
            )
        )

    try:
        store.commit_hypothesis_verdicts(updated_memories, new_observations)
    except Exception as e:
        yield TurnEvent.error_event(f"Failed to persist verdict batch: {e}")
        return

    committed = [
        updated_memories[hid] for hid in result.verdicts if hid in updated_memories
    ]
    for updated in committed:
        yield TurnEvent.hypothesis_verdict(
            updated.id, updated.status, updated.verdict_claim, list(updated.verification_citations)
        )

    summary_counts: dict[str, int] = {"verified": 0, "wrong": 0}
    for updated in committed:
        summary_counts[updated.status] = summary_counts.get(updated.status, 0) + 1
    summary = (
        f"/verify committed {len(committed)} verdict(s): "
        f"{summary_counts['verified']} verified, {summary_counts['wrong']} wrong."
    )
    yield TurnEvent.text_done(summary)
