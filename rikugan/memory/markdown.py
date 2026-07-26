"""MEMORY.md managed-region parser, deterministic renderer, and locked projector.

The managed region is the projection of current structured facts from SQLite.
Content outside the managed region is user-authored free-form Markdown that
is preserved byte-for-byte by projection.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..constants import MEMORY_LOCK_TIMEOUT_SECONDS, MEMORY_MARKDOWN_MAX_BYTES
from ..core.atomic_io import atomic_replace
from .workspace import WorkspacePaths
from .workspace_store import WorkspaceStore

MANAGED_START = "<!-- rikugan:managed:start -->"
MANAGED_END = "<!-- rikugan:managed:end -->"

_RECORD_RE = re.compile(r"<!-- rikugan:record id=([A-Za-z0-9._:-]+) rev=([1-9][0-9]*) -->")


class ManagedRegionError(RuntimeError):
    """Raised when managed-region delimiters are invalid."""


class ProjectionConflictError(RuntimeError):
    """Raised when MEMORY.md changed between projection read and write."""


@dataclass(frozen=True)
class ManagedEntry:
    """One fact entry in the managed region."""

    fact_id: str
    fact_type: str
    title: str
    content: str
    revision: int


@dataclass(frozen=True)
class MemoryDocument:
    """Parsed MEMORY.md with separated managed and unmanaged regions."""

    prefix: str
    managed: str
    suffix: str
    managed_hash: str
    unmanaged_hash: str


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_memory_document(content: str) -> MemoryDocument:
    """Parse a MEMORY.md document into managed and unmanaged regions.

    Raises ``ManagedRegionError`` if the managed delimiters are missing,
    duplicated, or reversed.
    """
    starts = [m.start() for m in re.finditer(re.escape(MANAGED_START), content)]
    ends = [m.start() for m in re.finditer(re.escape(MANAGED_END), content)]

    if not starts and not ends:
        digest = _sha256(content)
        return MemoryDocument(
            prefix=content,
            managed="",
            suffix="",
            managed_hash=_sha256(""),
            unmanaged_hash=digest,
        )

    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ManagedRegionError("invalid managed-region delimiters")

    start_body = starts[0] + len(MANAGED_START)
    prefix = content[: starts[0]]
    managed = content[start_body : ends[0]]
    suffix = content[ends[0] + len(MANAGED_END) :]

    return MemoryDocument(
        prefix=prefix,
        managed=managed,
        suffix=suffix,
        managed_hash=_sha256(managed),
        unmanaged_hash=_sha256(prefix + suffix),
    )


def render_memory_document(
    doc: MemoryDocument,
    managed_block: str = "",
    entries: list[ManagedEntry] | None = None,
) -> str:
    """Render a complete MEMORY.md from a parsed document and managed entries.

    Sorts entries by ``(fact_type, title, fact_id)`` for deterministic output.
    No timestamps, no provider/IDA/MCP calls. Preserves ``prefix + suffix``
    byte-for-byte.
    """
    if entries is None:
        entries = []

    lines: list[str] = []
    for entry in sorted(entries, key=lambda e: (e.fact_type, e.title, e.fact_id)):
        escaped_title = _escape_marker_text(entry.title)
        escaped_content = _escape_marker_text(entry.content)
        lines.append(f"<!-- rikugan:record id={entry.fact_id} rev={entry.revision} -->")
        lines.append(f"- [{entry.fact_type}] {escaped_title}: {escaped_content}")

    managed_body = managed_block
    if lines:
        if managed_body and not managed_body.endswith("\n"):
            managed_body += "\n"
        managed_body += "\n".join(lines) + "\n"

    # If no managed region existed before but we have entries, create one.
    # If one existed, replace its body.
    if doc.managed == "" and not managed_body:
        # No managed region and nothing to add
        return doc.prefix + doc.suffix

    result = doc.prefix + MANAGED_START + "\n" + managed_body + MANAGED_END + doc.suffix
    return result


def _escape_marker_text(text: str) -> str:
    """Escape hidden marker syntax so user content can't inject record markers."""
    # Collapse the marker prefix so injected markers are inert.
    text = text.replace("<!-- rikugan:", "<!-- rikugan_")
    # Collapse to single line for clean list rendering
    text = text.replace("\n", " ").replace("\r", " ")
    return text


def extract_unmanaged_markdown(content: str) -> str:
    """Return ``prefix + suffix`` from a MEMORY.md, never the managed block.

    Strips all managed-region delimiters and hidden record markers so the
    returned text is safe to include as ``manual_notes`` in a prompt.
    """
    try:
        doc = parse_memory_document(content)
    except ManagedRegionError:
        # If markers are malformed, return the whole content minus markers
        cleaned = content.replace(MANAGED_START, "").replace(MANAGED_END, "")
        return _strip_record_markers(cleaned)
    unmanaged = doc.prefix + doc.suffix
    return _strip_record_markers(unmanaged)


def _strip_record_markers(text: str) -> str:
    """Remove hidden ``<!-- rikugan:record ... -->`` markers from text."""
    return _RECORD_RE.sub("", text)


def _read_bounded_regular_utf8(path: Path, *, default: str = "# Memory\n") -> str:
    """Read a regular file with bounded size, rejecting symlinks/reparse points."""
    if not path.exists():
        return default
    if path.is_symlink():
        raise ManagedRegionError(f"refusing to follow symlink: {path}")
    stat = path.stat()
    if stat.st_size > MEMORY_MARKDOWN_MAX_BYTES:
        raise ManagedRegionError(f"MEMORY.md exceeds {MEMORY_MARKDOWN_MAX_BYTES} bytes: {stat.st_size}")
    return path.read_text(encoding="utf-8")


def _atomic_replace_regular_file(path: Path, content: str) -> None:
    """Write content to a temp file in the same directory, then atomically replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class MemoryProjector:
    """Deterministic locked projector that regenerates the managed region."""

    def __init__(self, *, lock_timeout: float = MEMORY_LOCK_TIMEOUT_SECONDS) -> None:
        self._lock_timeout = lock_timeout

    def project(self, paths: WorkspacePaths, store: WorkspaceStore) -> None:
        """Regenerate the managed region of ``paths.markdown`` from SQLite facts.

        Uses a portable cross-process lock (``portalocker``) to coordinate
        concurrent projectors. If the unmanaged region changed between read
        and write (detected by hash), marks ``projection_conflict`` and
        raises ``ProjectionConflictError``.
        """
        import portalocker

        latest_facts = store.list_facts()
        entries = [
            ManagedEntry(
                fact_id=f.fact_id,
                fact_type=f.fact_type,
                title=f.title,
                content=f.content,
                revision=f.revision,
            )
            for f in latest_facts
        ]

        try:
            with portalocker.Lock(str(paths.lock), mode="a", timeout=self._lock_timeout):
                before = _read_bounded_regular_utf8(paths.markdown)
                document = parse_memory_document(before)
                rendered = render_memory_document(document, entries=entries)

                current = _read_bounded_regular_utf8(paths.markdown)
                if _sha256(current) != _sha256(before):
                    store.mark_projection_conflict()
                    raise ProjectionConflictError("MEMORY.md changed during projection")

                _atomic_replace_regular_file(paths.markdown, rendered)

                new_doc = parse_memory_document(rendered)
                store.mark_projection_clean(
                    managed_hash=new_doc.managed_hash,
                    unmanaged_hash=new_doc.unmanaged_hash,
                    projected_revision=max((e.revision for e in entries), default=0),
                )
        except portalocker.exceptions.LockError:
            store.mark_projection_dirty()
            raise
