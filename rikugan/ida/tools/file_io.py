"""Agent-callable text file read/write tools.

These tools give the LLM safe access to UTF-8 text files inside the
analyzed binary's parent folder. They are deliberately narrow:

* The root is fixed to ``os.path.dirname(get_database_path())`` — no
  caller-supplied absolute path, no env override, no symlink target.
* Reads are clamped to a bounded character window; writes are atomic
  and overwrite-protected. UTF-8 is required for both directions.
* ``write_file`` is marked ``mutating=True`` so the existing
  ``AgentLoop._execute_single_tool`` approval gate applies when
  ``config.approve_mutations`` is enabled.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Annotated

from ...core.atomic_io import atomic_replace
from ...core.host import get_database_path
from ...memory.storage_guard import StorageError, validate_regular_contained_path
from ...tools.base import tool

_READ_MAX_CHARS_LIMIT = 12000
_READ_MAX_BYTES = 1 * 1024 * 1024
_WRITE_MAX_BYTES = 2 * 1024 * 1024

_SEGMENT_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
_ABS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class _FileToolError(ValueError):
    """User-facing error string for an LLM-facing tool result."""


def _resolve_root() -> Path:
    db = get_database_path()
    if not db:
        raise _FileToolError(
            "no analyzed binary/IDB available; cannot resolve file root"
        )
    return Path(db).resolve().parent


def _normalize_relative_path(candidate: object) -> list[str]:
    """Validate a relative path and return its safe segments.

    Reject absolute paths, NUL bytes, and any ``..`` segment *before*
    sanitization. Each remaining segment is restricted to the safe
    character whitelist ``[A-Za-z0-9._-]``; anything else becomes ``_``
    so an LLM-supplied path can never carry a path separator, NUL,
    or control character.
    """
    if not isinstance(candidate, str):
        raise _FileToolError("path must be a string")
    if not candidate.strip():
        raise _FileToolError("path must not be empty")
    if "\x00" in candidate:
        raise _FileToolError("path contains a NUL byte")
    cleaned = candidate.replace("\\", "/")
    if cleaned.startswith("/"):
        raise _FileToolError("absolute paths are not permitted")
    if _ABS_DRIVE_RE.match(cleaned):
        raise _FileToolError("absolute paths are not permitted")
    raw_parts = [seg for seg in cleaned.split("/") if seg]
    if not raw_parts:
        raise _FileToolError("path must include at least one segment")
    if any(seg == ".." for seg in raw_parts):
        raise _FileToolError("path traversal is not permitted")
    parts = [_SEGMENT_SAFE_RE.sub("_", seg) or "_" for seg in raw_parts]
    return parts


def _resolve_under_root(parts: list[str]) -> Path:
    root = _resolve_root()
    candidate = root.joinpath(*parts)
    try:
        common = os.path.commonpath([str(root), str(candidate)])
    except ValueError as exc:
        raise _FileToolError(f"path is not within the binary folder: {exc}")
    if common != str(root):
        raise _FileToolError("path escapes the analyzed binary folder")
    return candidate


def _ensure_parent_no_symlink(parent: Path) -> None:
    """Refuse any parent path that traverses a symlink component.

    Walks the lexical ancestors of *parent* from the resolved root
    downward. Each existing component must not be a symlink;
    non-existent components are created one at a time with
    ``Path.mkdir`` so the result is always a regular directory. The
    final resolved path must remain inside the root so an attacker
    cannot redirect the write by replacing an intermediate link.
    """
    root = _resolve_root()
    try:
        common = os.path.commonpath([str(root), str(parent)])
    except ValueError as exc:
        raise _FileToolError(f"path is not within the binary folder: {exc}")
    if common != str(root):
        raise _FileToolError("path escapes the analyzed binary folder")

    cursor = root
    rel_segments = parent.relative_to(root).parts
    for seg in rel_segments:
        next_path = cursor / seg
        if next_path.is_symlink():
            raise _FileToolError(f"refusing symlink parent component: {next_path}")
        if next_path.exists():
            if not next_path.is_dir():
                raise _FileToolError(f"parent component is not a directory: {next_path}")
        else:
            try:
                next_path.mkdir(parents=False, exist_ok=False)
            except FileExistsError:
                if next_path.is_symlink():
                    raise _FileToolError(f"refusing symlink parent component: {next_path}")
            except OSError as exc:
                raise _FileToolError(f"failed to create parent directory: {exc}")
        cursor = next_path


def _format_path_error(exc: _FileToolError) -> str:
    return f"Error: {exc}"


@tool(category="file_io")
def read_file(
    path: Annotated[str, "Path relative to the analyzed binary folder"],
    max_chars: Annotated[int, "Maximum characters to return (1-12000)"] = 4000,
) -> str:
    """Read a UTF-8 text file under the analyzed binary folder."""
    try:
        parts = _normalize_relative_path(path)
        candidate = _resolve_under_root(parts)
    except _FileToolError as exc:
        return _format_path_error(exc)

    if not candidate.exists():
        return f"Error: file not found: {'/'.join(parts)}"
    if candidate.is_symlink():
        return f"Error: refusing symlink: {'/'.join(parts)}"
    if not candidate.is_file():
        return f"Error: not a regular file: {'/'.join(parts)}"

    root = _resolve_root()
    try:
        validate_regular_contained_path(candidate, root=root)
    except StorageError as exc:
        return f"Error: {exc}"

    size = candidate.stat().st_size
    if size > _READ_MAX_BYTES:
        return f"Error: file is too large to read ({size} bytes; max {_READ_MAX_BYTES})"

    try:
        with candidate.open("rb") as handle:
            raw = handle.read()
    except OSError as exc:
        return f"Error: failed to read file: {exc}"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return f"Error: file is not valid UTF-8 ({exc})"
    # Clamp max_chars whenever it is provided, including 0 and
    # non-integer values. A non-None value always sets the limit;
    # out-of-range inputs fall back to the maximum.
    if max_chars is not None:
        try:
            requested = int(max_chars)
        except (TypeError, ValueError):
            requested = _READ_MAX_CHARS_LIMIT
        limit = max(1, min(requested, _READ_MAX_CHARS_LIMIT))
    else:
        limit = _READ_MAX_CHARS_LIMIT
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n…(truncated)…"
    rel = "/".join(parts)
    return f"Read {rel} ({len(text)} chars, {size} bytes):\n{text}"


@tool(category="file_io", mutating=True)
def write_file(
    path: Annotated[str, "Path relative to the analyzed binary folder"],
    content: Annotated[str, "UTF-8 text to write"],
    overwrite: Annotated[bool, "Overwrite an existing file when True"] = False,
) -> str:
    """Write a UTF-8 text file under the analyzed binary folder.

    The write is atomic (tempfile + ``atomic_replace``) and never
    overwrites an existing file unless ``overwrite=True``. Requires
    user approval when ``config.approve_mutations`` is enabled.
    """
    if not isinstance(content, str):
        return "Error: content must be a string"
    try:
        parts = _normalize_relative_path(path)
        candidate = _resolve_under_root(parts)
    except _FileToolError as exc:
        return _format_path_error(exc)

    encoded = content.encode("utf-8")
    if len(encoded) > _WRITE_MAX_BYTES:
        return f"Error: content is too large ({len(encoded)} bytes; max {_WRITE_MAX_BYTES})"

    if candidate.exists():
        if candidate.is_symlink():
            return f"Error: refusing to overwrite symlink: {'/'.join(parts)}"
        if not candidate.is_file():
            return f"Error: refusing to overwrite non-regular file: {'/'.join(parts)}"
        if not overwrite:
            return (
                f"Error: file already exists; pass overwrite=true to replace: {'/'.join(parts)}"
            )

    parent = candidate.parent
    try:
        _ensure_parent_no_symlink(parent)
    except _FileToolError as exc:
        return _format_path_error(exc)

    os.makedirs(parent, exist_ok=True)
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".rikugan-write-", dir=str(parent), text=True)
    except OSError as exc:
        return f"Error: failed to create temp file: {exc}"
    try:
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return f"Error: write failed: {exc}"
        try:
            atomic_replace(tmp_path, str(candidate))
        except OSError as exc:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return f"Error: atomic replace failed: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return f"Error: write failed: {exc!r}"
    rel = "/".join(parts)
    return f"Wrote {rel} ({len(encoded)} bytes, {len(content)} chars)"


__all__ = ["read_file", "write_file"]
