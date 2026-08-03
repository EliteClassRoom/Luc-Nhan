"""Mutation tracking for reversible tool calls."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..core.logging import log_debug
from ..tools.coercion import coerce_bool


@dataclass
class MutationRecord:
    """Records a single mutation for undo capability."""

    tool_name: str
    arguments: dict[str, Any]
    reverse_tool: str
    reverse_arguments: dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    description: str = ""
    reversible: bool = True


def _has_value(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _parse_pseudocode_comment_state(raw_state: Any) -> str | None:
    """Parse a ``get_pseudocode_comment_state`` result into ``old_comment``.

    Returns:
        * ``str`` — the captured old comment (including ``""`` when it was
          genuinely empty and the decompile call succeeded).
        * ``None`` — pre-state is unavailable because the decompile call
          failed, the JSON is malformed, the decoded value is not a dict,
          or the ``comment`` field is not a string.
    """
    try:
        state = json.loads(raw_state) if isinstance(raw_state, str) else raw_state
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(state, dict):
        return None
    # ok=true is required; ``is not True`` ensures falsy/absent are treated
    # as failure (the dict key "ok" may use Python's True, not a string).
    if state.get("ok") is not True:
        return None
    comment = state.get("comment", "")
    return comment if isinstance(comment, str) else None


def _not_reversible(tool_name: str, args: dict[str, Any], description: str) -> MutationRecord:
    return MutationRecord(
        tool_name=tool_name,
        arguments=args,
        reverse_tool="",
        reverse_arguments={},
        description=description,
        reversible=False,
    )


# ---------------------------------------------------------------------------
# Per-tool reverse-record builders
# ---------------------------------------------------------------------------


def _reverse_rename_function(args: dict[str, Any], pre: dict[str, Any]) -> MutationRecord:
    old_name = pre.get("old_name", "")
    new_name = args.get("new_name", "")
    address = args.get("address", "")
    if not (_has_value(address) and _has_value(old_name) and _has_value(new_name)):
        return _not_reversible(
            "rename_function",
            args,
            f"Rename function to {new_name} (arguments incomplete, not reversible)",
        )
    return MutationRecord(
        tool_name="rename_function",
        arguments=args,
        reverse_tool="rename_function",
        reverse_arguments={"address": address, "new_name": old_name},
        description=f"Rename function {old_name} → {new_name}",
    )


def _reverse_rename_variable(tool_name: str, args: dict[str, Any], pre: dict[str, Any]) -> MutationRecord:
    func = args.get("func_address", "")
    old_var = args.get("old_name", "")
    new_var = args.get("new_name", "")
    if not (_has_value(func) and _has_value(old_var) and _has_value(new_var)):
        return _not_reversible(
            tool_name,
            args,
            f"Rename variable to {new_var} (arguments incomplete, not reversible)",
        )
    return MutationRecord(
        tool_name=tool_name,
        arguments=args,
        reverse_tool=tool_name,
        reverse_arguments={
            "func_address": func,
            "old_name": new_var,
            "new_name": old_var,
        },
        description=f"Rename variable {old_var} → {new_var} in {func}",
    )


def _reverse_comment(
    tool_name: str,
    key: str,
    args: dict[str, Any],
    pre: dict[str, Any],
) -> MutationRecord:
    """Build reverse record for comment-setting tools.

    The pre-state key ``old_comment`` can be in three states:

    * **Missing** (``"old_comment" not in pre``) — pre-state capture did not
      run or returned nothing.  The record is **not reversible**.
    * **None** (``pre["old_comment"] is None``) — the getter tool returned
      ``None``, meaning it failed (e.g. decompile error).  The record is
      **not reversible**.
    * **Explicit empty string** (``pre["old_comment"] == ""``) — the old
      comment genuinely was empty.  The record **is reversible** and will
      restore the empty comment on undo.

    Non-string values for ``old_comment`` are treated as non-reversible.
    """
    target = args.get(key, "")
    repeatable = coerce_bool(args.get("repeatable", False))

    # Missing pre-state → not reversible
    if not _has_value(target) or "old_comment" not in pre:
        return _not_reversible(
            tool_name,
            args,
            f"Set comment on {target} (target or pre-state missing, not reversible)",
        )

    old_comment = pre.get("old_comment")

    # Getter failed → not reversible
    if old_comment is None:
        return _not_reversible(
            tool_name,
            args,
            f"Set comment on {target} (pre-state is None, not reversible)",
        )

    # Non-string values → not reversible
    if not isinstance(old_comment, str):
        return _not_reversible(
            tool_name,
            args,
            f"Set comment on {target} (non-string pre-state, not reversible)",
        )

    return MutationRecord(
        tool_name=tool_name,
        arguments=args,
        reverse_tool=tool_name,
        reverse_arguments={key: target, "comment": old_comment, "repeatable": repeatable},
        description=f"Set comment on {target}",
    )


def _reverse_set_comment(args: dict[str, Any], pre: dict[str, Any]) -> MutationRecord:
    return _reverse_comment("set_comment", "address", args, pre)


def _reverse_set_function_comment(args: dict[str, Any], pre: dict[str, Any]) -> MutationRecord:
    return _reverse_comment("set_function_comment", "address", args, pre)


def _reverse_set_pseudocode_comment(args: dict[str, Any], pre: dict[str, Any]) -> MutationRecord:
    """Build reverse record for set_pseudocode_comment.

    Uses the same pre-state validity rules as _reverse_comment():

    * Missing or None old_comment → not reversible.
    * Explicit empty string old_comment → reversible (restores empty comment).
    * Non-string old_comment → not reversible.
    """
    func_addr = args.get("func_address", "")
    target_addr = args.get("target_address", "")
    if not (_has_value(func_addr) and _has_value(target_addr)) or "old_comment" not in pre:
        return _not_reversible(
            "set_pseudocode_comment",
            args,
            f"Set pseudocode comment at {target_addr} (target or pre-state missing, not reversible)",
        )

    old_comment = pre.get("old_comment")

    if old_comment is None:
        return _not_reversible(
            "set_pseudocode_comment",
            args,
            f"Set pseudocode comment at {target_addr} (pre-state is None, not reversible)",
        )

    if not isinstance(old_comment, str):
        return _not_reversible(
            "set_pseudocode_comment",
            args,
            f"Set pseudocode comment at {target_addr} (non-string pre-state, not reversible)",
        )

    return MutationRecord(
        tool_name="set_pseudocode_comment",
        arguments=args,
        reverse_tool="set_pseudocode_comment",
        reverse_arguments={
            "func_address": func_addr,
            "target_address": target_addr,
            "comment": old_comment,
        },
        description=f"Set pseudocode comment at {target_addr}",
    )


def _reverse_rename_address(args: dict[str, Any], pre: dict[str, Any]) -> MutationRecord:
    address = args.get("address", "")
    old_name = pre.get("old_name", "")
    new_name = args.get("new_name", "")
    if _has_value(address) and _has_value(old_name) and _has_value(new_name):
        return MutationRecord(
            tool_name="rename_address",
            arguments=args,
            reverse_tool="rename_address",
            reverse_arguments={"address": address, "new_name": old_name},
            description=f"Rename data at {address} → {new_name}",
        )
    return _not_reversible(
        "rename_address",
        args,
        f"Rename data at {address} → {new_name} (arguments incomplete, not reversible)",
    )


def _reverse_set_function_prototype(args: dict[str, Any], pre: dict[str, Any]) -> MutationRecord:
    target = args.get("address", "")
    old_proto = pre.get("old_prototype", "")
    if _has_value(target) and _has_value(old_proto):
        return MutationRecord(
            tool_name="set_function_prototype",
            arguments=args,
            reverse_tool="set_function_prototype",
            reverse_arguments={"address": target, "prototype": old_proto},
            description=f"Set prototype for {target}",
        )
    return _not_reversible(
        "set_function_prototype",
        args,
        f"Set prototype for {target} (pre-state unknown, not reversible)",
    )


def _reverse_apply_type_to_variable(args: dict[str, Any], pre: dict[str, Any]) -> MutationRecord:
    func = args.get("func_address", "")
    var = args.get("var_name", "")
    old_type = pre.get("old_type", "")
    if _has_value(func) and _has_value(var) and _has_value(old_type):
        return MutationRecord(
            tool_name="apply_type_to_variable",
            arguments=args,
            reverse_tool="apply_type_to_variable",
            reverse_arguments={
                "func_address": func,
                "var_name": var,
                "type_str": old_type,
            },
            description=f"Retype {var} in {func}",
        )
    return _not_reversible(
        "apply_type_to_variable",
        args,
        f"Retype {var} in {func} (arguments incomplete, not reversible)",
    )


def _parse_read_file_output(raw: Any) -> tuple[bool, str]:
    """Parse a ``read_file`` tool result into ``(existed, content)``.

    ``read_file`` returns a formatted string ``"Read <rel> (N chars, M
    bytes):\\n<text>"`` on success, or an ``"Error: ..."`` line on
    failure (including file-not-found). Returns:

    * ``(True, <text>)``  — file exists; ``<text>`` is the decoded body.
    * ``(False, "")``     — file does not exist or read failed; the
      reverse record must be non-reversible since there is nothing to
      restore.
    """
    if not isinstance(raw, str) or not raw:
        return False, ""
    # ``read_file`` emits ``"Error: ..."`` for any failure path
    # (missing file, non-UTF-8, too large, path rejection). Any such
    # result means there is no prior content to restore.
    error_prefixes = (
        "Error: file not found",
        "Error: not a regular file",
        "Error: refusing symlink",
        "Error: file is too large",
        "Error: file is not valid UTF-8",
        "Error: failed to read file",
    )
    if raw.startswith(error_prefixes):
        return False, ""
    marker = ":\n"
    idx = raw.find(marker)
    if idx < 0:
        # No header/body separator — treat as no content to restore.
        return True, ""
    return True, raw[idx + len(marker) :]


def _reverse_write_file(args: dict[str, Any], pre: dict[str, Any]) -> MutationRecord:
    """Build reverse record for ``write_file``.

    ``write_file`` has two semantically distinct cases:

    1. **Overwrite** (``existed=True`` and ``old_content`` captured):
       reversible — replay ``write_file`` with ``overwrite=True`` and
       the captured body.
    2. **Create new file** (``existed=False``) or **unknown pre-state**
       (capture failed): non-reversible. The agent has no
       ``delete_file`` tool, so ``/undo`` cannot remove the created
       file. The description names the path so the user can clean up
       by hand.
    """
    path = args.get("path", "")
    existed = pre.get("existed", False)
    old_content = pre.get("old_content")
    if existed and isinstance(old_content, str):
        return MutationRecord(
            tool_name="write_file",
            arguments=args,
            reverse_tool="write_file",
            reverse_arguments={
                "path": path,
                "content": old_content,
                "overwrite": True,
            },
            description=f"Overwrite {path} back to pre-write content",
        )
    reason = "file did not exist before write" if not existed else "pre-write content unavailable (getter failed)"
    return _not_reversible(
        "write_file",
        args,
        f"Write {path} — not reversible: {reason}; delete the file manually to undo",
    )


# Dispatch table: tool_name → handler(args, pre) -> MutationRecord
# Only contains tools that actually exist in the IDA registry.
# Unknown tools fall through to build_reverse_record() non-reversible default.
_REVERSE_BUILDERS: dict[str, Any] = {
    "rename_function": _reverse_rename_function,
    "rename_variable": lambda a, p: _reverse_rename_variable("rename_variable", a, p),
    "rename_address": _reverse_rename_address,
    "set_comment": _reverse_set_comment,
    "set_function_comment": _reverse_set_function_comment,
    "set_pseudocode_comment": _reverse_set_pseudocode_comment,
    "set_function_prototype": _reverse_set_function_prototype,
    "apply_type_to_variable": _reverse_apply_type_to_variable,
    "write_file": _reverse_write_file,
}


def build_reverse_record(
    tool_name: str,
    arguments: dict[str, Any],
    pre_state: dict[str, Any] | None = None,
) -> MutationRecord:
    """Build a MutationRecord with reverse operation for a mutating tool call.

    Returns a non-reversible MutationRecord if the tool cannot be undone.
    All registered reverse builders are guaranteed to return a MutationRecord
    (never ``None``), so the return value is always usable.
    """
    pre = pre_state or {}
    builder = _REVERSE_BUILDERS.get(tool_name)
    if builder is not None:
        return builder(arguments, pre)

    # For tools we don't know how to reverse (execute_python, etc.)
    return _not_reversible(tool_name, arguments, f"Call {tool_name}")


def capture_pre_state(
    tool_name: str,
    arguments: dict[str, Any],
    tool_executor: Callable[[str, dict[str, Any]], str],
) -> dict[str, Any]:
    """Capture pre-mutation state needed for undo.

    Calls getter tools where needed to record the current state
    before a mutation is applied.
    """
    pre: dict[str, Any] = {}

    try:
        if tool_name == "rename_function":
            address = arguments.get("address", "")
            if _has_value(address):
                pre["old_name"] = tool_executor("get_function_name", {"address": address})

        elif tool_name == "rename_address":
            address = arguments.get("address", "")
            if _has_value(address):
                pre["old_name"] = tool_executor("get_address_name", {"address": address})

        elif tool_name == "set_comment":
            address = arguments.get("address", "")
            repeatable = coerce_bool(arguments.get("repeatable", False))
            if _has_value(address):
                pre["old_comment"] = tool_executor("get_comment", {"address": address, "repeatable": repeatable})
        elif tool_name == "set_function_comment":
            address = arguments.get("address", "")
            repeatable = coerce_bool(arguments.get("repeatable", False))
            if _has_value(address):
                pre["old_comment"] = tool_executor(
                    "get_function_comment", {"address": address, "repeatable": repeatable}
                )
        elif tool_name == "set_pseudocode_comment":
            func_addr = arguments.get("func_address", "")
            target_addr = arguments.get("target_address", "")
            if _has_value(func_addr) and _has_value(target_addr):
                raw_state = tool_executor(
                    "get_pseudocode_comment_state",
                    {"func_address": func_addr, "target_address": target_addr},
                )
                pre["old_comment"] = _parse_pseudocode_comment_state(raw_state)
        elif tool_name == "set_function_prototype":
            target = arguments.get("address", "")
            if _has_value(target):
                pre["old_prototype"] = tool_executor("get_function_prototype", {"address": target})
        elif tool_name == "apply_type_to_variable":
            func = arguments.get("func_address", "")
            var = arguments.get("var_name", "")
            if _has_value(func) and _has_value(var):
                pre["old_type"] = tool_executor("get_variable_type", {"func_address": func, "var_name": var})
        elif tool_name == "write_file":
            # Read prior content via the ``read_file`` getter so the
            # reverse record can replay an overwrite. ``read_file``
            # returns an ``"Error: ..."`` string when the file does not
            # exist (or is unreadable); ``_parse_read_file_output``
            # distinguishes the two cases.
            path = arguments.get("path", "")
            if _has_value(path):
                raw = tool_executor("read_file", {"path": path})
                existed, old_content = _parse_read_file_output(raw)
                pre["existed"] = existed
                if existed:
                    pre["old_content"] = old_content
    except Exception as e:
        log_debug(f"capture_pre_state failed for {tool_name}: {e}")

    return pre
