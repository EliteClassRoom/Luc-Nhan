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


# ---------------------------------------------------------------------------
# Type-system builders — set_type, nop_microcode, modify_struct, modify_enum
# ---------------------------------------------------------------------------


def _parse_struct_info(raw: Any) -> dict[str, Any] | None:
    """Parse ``get_struct_info`` output into structured pre-state data.

    The getter returns a multi-line block with a ``Size:`` header and
    one line per member:

        Struct: Foo
        Size: 8 (0x8)
        Members: 2

          +0x0000  int                     x                        (4 bytes)
          +0x0004  int                     y                        (4 bytes)

    Returns:
        dict with keys ``name`` (str), ``size`` (int), ``fields``
        (list of ``{"name": str, "type": str, "offset": int, "size": int,
        "comment": str}``), or ``None`` when the getter failed or the
        payload is unparseable.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    if raw.startswith("Struct '") and "not found" in raw:
        return None

    out: dict[str, Any] = {"name": "", "size": 0, "fields": []}
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("Struct:"):
            out["name"] = stripped[len("Struct:") :].strip()
        elif stripped.startswith("Size:"):
            rest = stripped[len("Size:") :].strip()
            head = rest.split(" ", 1)[0]
            try:
                out["size"] = int(head, 0)
            except ValueError:
                pass
        elif stripped.startswith("+0x"):
            try:
                after_offset = stripped.split(None, 1)[1]
            except IndexError:
                continue
            comment = ""
            if ";" in after_offset:
                after_offset, _, comment = after_offset.partition(";")
            # Strip the trailing ``(N bytes)`` parenthesised size from the
            # line so tokenisation only sees ``<type> <name>`` plus any
            # inline comment.
            try:
                _type_part, _, tail = after_offset.rpartition("(")
                tail = tail.strip()
                if tail.endswith(")"):
                    tail = tail[:-1]
                size_b = int(tail.split()[0], 0)
                # ``after_offset`` now holds only ``<type> <name>`` (plus
                # optional comment already stripped above).
                body = _type_part.rstrip()
            except (ValueError, IndexError):
                continue
            tokens = body.split()
            if len(tokens) < 2:
                continue
            field_name = tokens[-1]
            field_type = " ".join(tokens[:-1])
            try:
                offset_b = int(stripped.split()[0], 16)
            except ValueError:
                continue
            out["fields"].append(
                {
                    "name": field_name,
                    "type": field_type,
                    "offset": offset_b,
                    "size": size_b,
                    "comment": comment.strip(),
                }
            )
    return out if out["name"] else None


def _parse_enum_info(raw: Any) -> dict[str, Any] | None:
    """Parse ``get_enum_info`` output into structured pre-state data.

    The getter returns a multi-line block with a header and one line
    per member:

        Enum: MyEnum

          X                                       = 0x5 (5)
          Y                                       = 0xa (10)

    When the enum is bitfield, the header carries `` (bitfield)``.

    Returns:
        dict with keys ``name`` (str), ``bitfield`` (bool), ``members``
        (list of ``{"name": str, "value": int}``), or ``None`` when the
        getter failed or the payload is unparseable.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    if raw.startswith("Enum '") and "not found" in raw:
        return None

    out: dict[str, Any] = {"name": "", "bitfield": False, "members": []}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("Enum:"):
            if stripped.startswith("Enum:"):
                rest = stripped[len("Enum:") :].strip()
                out["bitfield"] = rest.endswith("(bitfield)")
                out["name"] = rest.replace("(bitfield)", "").strip()
            continue
        if "=" not in stripped:
            continue
        lhs, _, rhs = stripped.partition("=")
        mname = lhs.strip()
        rhs = rhs.strip()
        value_str = rhs.split()[0] if rhs else ""
        try:
            value = int(value_str, 0)
        except ValueError:
            continue
        out["members"].append({"name": mname, "value": value})
    return out if out["name"] else None


def _reverse_set_type(args: dict[str, Any], pre: dict[str, Any]) -> MutationRecord:
    """Build reverse record for ``set_type``.

    Pre-state comes from ``get_function_prototype`` (the only available
    raw getter for a type attached to an address). An empty pre-state
    means the address had no prior type — nothing to restore, the
    record is non-reversible (the agent has no ``clear_type`` tool).
    """
    address = args.get("address", "")
    type_string = args.get("type_string", "")
    old_type = pre.get("old_type", "")
    if not (_has_value(address) and isinstance(old_type, str) and old_type):
        return _not_reversible(
            "set_type",
            args,
            f"Set type at {address} to {type_string} — no prior type captured; no clear_type tool to undo",
        )
    return MutationRecord(
        tool_name="set_type",
        arguments=args,
        reverse_tool="set_type",
        reverse_arguments={"address": address, "type_string": old_type},
        description=f"Set type at {address} to {type_string}",
    )


def _reverse_nop_microcode(args: dict[str, Any], pre: dict[str, Any]) -> MutationRecord:
    """Build reverse record for ``nop_microcode``.

    The tool carries its own pre-state in arguments: ``optimizer_name``
    is the key the runtime uses to track the installed ``NopOptimizer``
    in ``installed_optimizers``. Calling ``remove_microcode_optimizer``
    with the same name uninstalls the rule.
    """
    optimizer_name = args.get("optimizer_name", "")
    func_address = args.get("func_address", "")
    if not _has_value(optimizer_name):
        return _not_reversible(
            "nop_microcode",
            args,
            f"NOP microcode at {func_address} (no optimizer_name captured; cannot uninstall)",
        )
    return MutationRecord(
        tool_name="nop_microcode",
        arguments=args,
        reverse_tool="remove_microcode_optimizer",
        reverse_arguments={"name": optimizer_name},
        description=f"NOP microcode at {func_address} via optimizer {optimizer_name}",
    )


def _lookup_struct_field(parsed: dict[str, Any] | None, field_name: str) -> dict[str, Any] | None:
    if not parsed or not parsed.get("fields"):
        return None
    for f in parsed["fields"]:
        if f.get("name") == field_name:
            return f
    return None


def _lookup_enum_member(parsed: dict[str, Any] | None, member_name: str) -> dict[str, Any] | None:
    if not parsed or not parsed.get("members"):
        return None
    for m in parsed["members"]:
        if m.get("name") == member_name:
            return m
    return None


def _reverse_modify_struct(args: dict[str, Any], pre: dict[str, Any]) -> MutationRecord:
    """Build reverse record for ``modify_struct``.

    Action semantics:
    * ``rename_field`` — fully reversible from args (old/new swapped).
    * ``retype_field`` / ``set_field_comment`` — old type/comment read
      from ``get_struct_info`` pre-state.
    * ``add_field`` — reverses to ``remove_field``.
    * ``remove_field`` — reverses to ``add_field``; old offset/type
      captured from pre-state.
    * ``resize`` — reverses to ``resize`` with old size from pre-state.
    """
    name = args.get("name", "")
    action = args.get("action", "")
    field_name = args.get("field_name", "")

    if not (_has_value(name) and _has_value(action)):
        return _not_reversible(
            "modify_struct",
            args,
            f"Modify struct {name} (name/action missing, not reversible)",
        )

    parsed = pre.get("old_struct_info_parsed") if isinstance(pre.get("old_struct_info_parsed"), dict) else None

    if action == "rename_field":
        new_name = args.get("new_name", "")
        if _has_value(field_name) and _has_value(new_name):
            return MutationRecord(
                tool_name="modify_struct",
                arguments=args,
                reverse_tool="modify_struct",
                reverse_arguments={
                    "name": name,
                    "action": "rename_field",
                    "field_name": new_name,
                    "new_name": field_name,
                },
                description=f"Rename struct {name} field {field_name} \u2192 {new_name}",
            )
        return _not_reversible(
            "modify_struct",
            args,
            f"Modify struct {name} rename_field incomplete (not reversible)",
        )

    if action == "add_field":
        if _has_value(field_name):
            return MutationRecord(
                tool_name="modify_struct",
                arguments=args,
                reverse_tool="modify_struct",
                reverse_arguments={
                    "name": name,
                    "action": "remove_field",
                    "field_name": field_name,
                },
                description=f"Add field {field_name} to struct {name}",
            )
        return _not_reversible(
            "modify_struct",
            args,
            f"Modify struct {name} add_field missing field_name (not reversible)",
        )

    if action == "remove_field":
        if _has_value(field_name):
            old = _lookup_struct_field(parsed, field_name)
            if old is not None:
                return MutationRecord(
                    tool_name="modify_struct",
                    arguments=args,
                    reverse_tool="modify_struct",
                    reverse_arguments={
                        "name": name,
                        "action": "add_field",
                        "field_name": field_name,
                        "field_type": old.get("type", "int"),
                        "offset": old.get("offset", -1),
                        "comment": old.get("comment", ""),
                    },
                    description=f"Remove field {field_name} from struct {name}",
                )
        return _not_reversible(
            "modify_struct",
            args,
            f"Modify struct {name} remove_field {field_name} (pre-state unavailable, cannot recreate)",
        )

    if action == "retype_field":
        if _has_value(field_name):
            old = _lookup_struct_field(parsed, field_name)
            old_type = old.get("type", "") if old is not None else ""
            if old_type:
                return MutationRecord(
                    tool_name="modify_struct",
                    arguments=args,
                    reverse_tool="modify_struct",
                    reverse_arguments={
                        "name": name,
                        "action": "retype_field",
                        "field_name": field_name,
                        "field_type": old_type,
                    },
                    description=f"Retype struct {name} field {field_name}",
                )
        return _not_reversible(
            "modify_struct",
            args,
            f"Modify struct {name} retype_field {field_name} (old type unknown)",
        )

    if action == "set_field_comment":
        if _has_value(field_name):
            old = _lookup_struct_field(parsed, field_name)
            old_comment = old.get("comment", "") if old is not None else ""
            return MutationRecord(
                tool_name="modify_struct",
                arguments=args,
                reverse_tool="modify_struct",
                reverse_arguments={
                    "name": name,
                    "action": "set_field_comment",
                    "field_name": field_name,
                    "comment": old_comment,
                },
                description=f"Set comment on struct {name} field {field_name}",
            )
        return _not_reversible(
            "modify_struct",
            args,
            f"Modify struct {name} set_field_comment missing field_name (not reversible)",
        )

    if action == "resize":
        new_size = args.get("new_size", 0)
        old_size = parsed.get("size", 0) if parsed else 0
        if parsed is not None and old_size and old_size != new_size:
            return MutationRecord(
                tool_name="modify_struct",
                arguments=args,
                reverse_tool="modify_struct",
                reverse_arguments={
                    "name": name,
                    "action": "resize",
                    "new_size": old_size,
                },
                description=f"Resize struct {name} to {new_size}",
            )
        return _not_reversible(
            "modify_struct",
            args,
            f"Modify struct {name} resize to {new_size} (old size unavailable)",
        )

    return _not_reversible(
        "modify_struct",
        args,
        f"Modify struct {name} with unknown action {action!r} (not reversible)",
    )


def _reverse_modify_enum(args: dict[str, Any], pre: dict[str, Any]) -> MutationRecord:
    """Build reverse record for ``modify_enum``.

    Action semantics mirror ``_reverse_modify_struct``:
    * ``rename_member`` — fully reversible from args.
    * ``add_member`` — reverses to ``remove_member``.
    * ``remove_member`` — reverses to ``add_member`` with the captured
      old ``value`` from ``get_enum_info`` pre-state.
    """
    name = args.get("name", "")
    action = args.get("action", "")
    member_name = args.get("member_name", "")

    if not (_has_value(name) and _has_value(action)):
        return _not_reversible(
            "modify_enum",
            args,
            f"Modify enum {name} (name/action missing, not reversible)",
        )

    parsed = pre.get("old_enum_info_parsed") if isinstance(pre.get("old_enum_info_parsed"), dict) else None

    if action == "rename_member":
        new_name = args.get("new_name", "")
        if _has_value(member_name) and _has_value(new_name):
            return MutationRecord(
                tool_name="modify_enum",
                arguments=args,
                reverse_tool="modify_enum",
                reverse_arguments={
                    "name": name,
                    "action": "rename_member",
                    "member_name": new_name,
                    "new_name": member_name,
                },
                description=f"Rename enum {name} member {member_name} \u2192 {new_name}",
            )
        return _not_reversible(
            "modify_enum",
            args,
            f"Modify enum {name} rename_member incomplete (not reversible)",
        )

    if action == "add_member":
        if _has_value(member_name):
            return MutationRecord(
                tool_name="modify_enum",
                arguments=args,
                reverse_tool="modify_enum",
                reverse_arguments={
                    "name": name,
                    "action": "remove_member",
                    "member_name": member_name,
                },
                description=f"Add enum {name} member {member_name}",
            )
        return _not_reversible(
            "modify_enum",
            args,
            f"Modify enum {name} add_member missing member_name (not reversible)",
        )

    if action == "remove_member":
        if _has_value(member_name):
            old = _lookup_enum_member(parsed, member_name)
            if old is not None:
                return MutationRecord(
                    tool_name="modify_enum",
                    arguments=args,
                    reverse_tool="modify_enum",
                    reverse_arguments={
                        "name": name,
                        "action": "add_member",
                        "member_name": member_name,
                        "value": old.get("value", 0),
                    },
                    description=f"Remove enum {name} member {member_name}",
                )
        return _not_reversible(
            "modify_enum",
            args,
            f"Modify enum {name} remove_member {member_name} (pre-state unavailable)",
        )

    return _not_reversible(
        "modify_enum",
        args,
        f"Modify enum {name} with unknown action {action!r} (not reversible)",
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
    "set_type": _reverse_set_type,
    "nop_microcode": _reverse_nop_microcode,
    "modify_struct": _reverse_modify_struct,
    "modify_enum": _reverse_modify_enum,
}


# Mutating tools whose effect cannot be reversed faithfully. Each tool listed
# here MUST carry an inline reason — the test
# ``test_intentionally_non_reversible_is_frozenset_with_reasons`` enforces
# that the reason is a non-empty string. Adding a tool here without a reason
# fails the drift-proof guard.
_INTENTIONALLY_NON_REVERSIBLE: frozenset[tuple[str, str]] = frozenset(
    {
        # ``execute_python`` runs arbitrary user-supplied IDAPython; the
        # effects can include renaming functions, defining types, etc.
        # without going through other mutating tools, so the agent has no
        # observable pre-state to roll back to.
        (
            "execute_python",
            "Arbitrary IDAPython: effects can mutate the IDB in any way "
            "outside the typed tool surface, so no faithful pre-state "
            "captures the changes.",
        ),
        # ``create_struct`` defines a new named type. There is no
        # ``delete_struct`` tool in the agent surface, so the user must
        # remove the type manually if /undo is needed.
        (
            "create_struct",
            "No delete_struct tool: the new named type persists in the TIL until the user deletes it manually.",
        ),
        # ``create_enum`` — same rationale as ``create_struct``.
        (
            "create_enum",
            "No delete_enum tool: the new named enum persists in the TIL until the user deletes it manually.",
        ),
        # ``create_typedef`` — same rationale as ``create_struct``.
        (
            "create_typedef",
            "No delete_typedef tool: the new named typedef persists in the TIL until the user deletes it manually.",
        ),
        # ``import_c_header`` parses a header that can declare any
        # combination of structs/enums/typedefs. Reconstructing the
        # pre-state (which types did NOT exist) requires per-type
        # snapshots the agent does not capture.
        (
            "import_c_header",
            "Header may declare any mix of struct/enum/typedef — "
            "recreating pre-state (which types did not exist) is not "
            "feasible from a single parse call.",
        ),
        # ``import_type_from_library`` adds a single named type from a
        # TIL. Like ``create_typedef`` the type persists in the local
        # TIL and cannot be deleted through the typed tool surface.
        (
            "import_type_from_library",
            "Imported type persists in the local TIL; no typed tool removes a single imported type definition.",
        ),
        # ``install_microcode_optimizer`` registers a user-supplied
        # optimizer. The reverse (remove_microcode_optimizer) is
        # idempotent so it is captured as a real builder for nop
        # microcode, but the user-supplied optimizer code itself has no
        # pre-state — the agent cannot reconstruct the previous optimizer
        # set.
        (
            "install_microcode_optimizer",
            "User-supplied optimizer code; the agent cannot reconstruct the previous optimizer registry.",
        ),
        # ``apply_struct_to_address`` changes a data address's type to
        # a struct. There is no typed tool to read or restore the
        # previous type of an arbitrary data address
        # (get_function_prototype only covers functions).
        (
            "apply_struct_to_address",
            "No raw getter returns the prior data type at an arbitrary "
            "address — get_function_prototype only covers functions.",
        ),
        # ``propagate_type`` re-runs the IDA type-propagation engine;
        # its side effects are global and IDA-internal — the agent has
        # no pre-state to reconstruct.
        (
            "propagate_type",
            "Re-runs IDA's internal type propagation: side effects are "
            "global and IDA-managed, no observable pre-state to revert.",
        ),
        # ``remove_microcode_optimizer`` deletes an entry from the
        # optimizer registry; once removed the original code/description
        # cannot be recovered, so reverse = install with the same args
        # is not faithful. Pair with ``install_microcode_optimizer`` for
        # the symmetric rationale.
        (
            "remove_microcode_optimizer",
            "Removes optimizer from registry — original python_code/"
            "description cannot be recovered after removal, so install "
            "would not faithfully undo.",
        ),
    }
)


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
        elif tool_name == "set_type":
            # Capture the prior type at the address (function prototype
            # for function addresses; empty string when no type or
            # ``get_function_prototype`` failed).
            address = arguments.get("address", "")
            if _has_value(address):
                pre["old_type"] = tool_executor("get_function_prototype", {"address": address})
        elif tool_name == "nop_microcode":
            # All state needed for reversal lives in ``arguments``
            # (the optimizer_name + the target instruction addresses).
            # No external getter call is required.
            pre["optimizer_name"] = arguments.get("optimizer_name", "")
        elif tool_name == "modify_struct":
            # Pull the struct layout so the builder can revert add/
            # remove/retype_field/resize actions. ``get_struct_info``
            # returns formatted text; we parse it into structured data
            # for the builder.
            name = arguments.get("name", "")
            if _has_value(name):
                raw = tool_executor("get_struct_info", {"name": name})
                parsed = _parse_struct_info(raw)
                if parsed is not None:
                    pre["old_struct_info_parsed"] = parsed
        elif tool_name == "modify_enum":
            name = arguments.get("name", "")
            if _has_value(name):
                raw = tool_executor("get_enum_info", {"name": name})
                parsed = _parse_enum_info(raw)
                if parsed is not None:
                    pre["old_enum_info_parsed"] = parsed
    except Exception as e:
        log_debug(f"capture_pre_state failed for {tool_name}: {e}")

    return pre
