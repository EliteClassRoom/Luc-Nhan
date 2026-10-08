"""/case command parsing and dispatch.

Supported commands:
    /case create <display-name>
    /case list
    /case use <case_id>
    /case use none
    /case add-binary <memory_id>
    /case remove-binary <memory_id>
    /case delete <case_id>
    /case compare <memory_id-a> <memory_id-b>
    /case promote <source-record-id>

All commands require authority from the main controller.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedCaseCommand:
    """Parsed /case command."""

    action: str
    args: tuple[str, ...]


_MAX_ARGS = 8
_MAX_ARG_LEN = 256


def parse_case_command(text: str) -> ParsedCaseCommand | None:
    """Parse a /case command line.

    Returns ``None`` if the text is not a valid /case command.
    """
    text = text.strip()
    if not text.startswith("/case"):
        return None

    rest = text[len("/case") :].strip()
    if not rest:
        return ParsedCaseCommand(action="list", args=())

    try:
        parts = shlex.split(rest)
    except ValueError:
        return None

    if len(parts) > _MAX_ARGS:
        parts = parts[:_MAX_ARGS]
    parts = tuple(p[:_MAX_ARG_LEN] for p in parts)

    action = parts[0]
    args = parts[1:]
    return ParsedCaseCommand(action=action, args=args)


def dispatch_case_command(
    cmd: ParsedCaseCommand,
    *,
    case_repository,
    case_service,
    manager,
    authority,
    context,
) -> str:
    """Dispatch a parsed /case command and return a user-facing result string."""
    if cmd.action == "create":
        if not cmd.args:
            return "Usage: /case create <display-name>"
        name = " ".join(cmd.args)
        case = case_repository.create_case(name)
        # Auto-add current binary
        case_repository.add_member(case.case_id, context.binary_memory_id, expected_case_revision=case.revision)
        manager.set_active_case(case.case_id)
        return f"Created case '{name}' (ID: {case.case_id[:12]}) and set as active."

    if cmd.action == "list":
        cases = case_repository.list_cases()
        if not cases:
            return "No cases found."
        lines = []
        for c in cases:
            active = " *" if c.case_id == manager.active_case_id else ""
            lines.append(f"  {c.case_id[:12]}  {c.name}  [{c.state}]{active}")
        return "Cases:\n" + "\n".join(lines)

    if cmd.action == "use":
        if not cmd.args:
            return "Usage: /case use <case_id|none>"
        if cmd.args[0] == "none":
            manager.clear_active_case()
            return "Cleared active case."
        # Accept short ID prefix
        case_id = _resolve_case_id(cmd.args[0], case_repository)
        if case_id is None:
            return f"Case not found: {cmd.args[0]}"
        manager.set_active_case(case_id)
        return f"Active case set to {case_id[:12]}."

    if cmd.action == "add-binary":
        if len(cmd.args) < 1:
            return "Usage: /case add-binary <memory_id>"
        return f"add-binary requires UI confirmation for member {cmd.args[0][:12]}"

    if cmd.action == "promote":
        if len(cmd.args) < 1:
            return "Usage: /case promote <source-record-id>"
        if not manager.active_case_id:
            return "No active case. Use /case use <case_id> first."
        try:
            promotion = case_service.promote(authority, context, manager.active_case_id, cmd.args[0])
            return f"Promoted fact {promotion.source.source_record_id[:12]} into case {promotion.case_id[:12]}"
        except Exception as e:
            return f"Promotion failed: {e}"

    if cmd.action == "compare":
        if len(cmd.args) < 2:
            return "Usage: /case compare <memory_id-a> <memory_id-b>"
        return "Compare requires active case and both binaries as members."

    if cmd.action == "delete":
        if len(cmd.args) < 1:
            return "Usage: /case delete <case_id>"
        case_id = _resolve_case_id(cmd.args[0], case_repository)
        if case_id is None:
            return f"Case not found: {cmd.args[0]}"
        case = case_repository.get_case(case_id)
        case_repository.soft_delete_case(case_id, expected_case_revision=case.revision)
        if manager.active_case_id == case_id:
            manager.clear_active_case()
        return f"Case {case_id[:12]} deleted."

    return f"Unknown /case action: {cmd.action}"


def _resolve_case_id(selector: str, case_repository) -> str | None:
    """Resolve a case selector (full ID or unique prefix) to a case_id."""
    # Full match
    case = case_repository.get_case(selector)
    if case is not None:
        return case.case_id
    # Prefix match
    matches = [c for c in case_repository.list_cases(include_deleted=True) if c.case_id.startswith(selector)]
    if len(matches) == 1:
        return matches[0].case_id
    return None
