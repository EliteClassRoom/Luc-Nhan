"""Workspace identity contracts: generated IDs, frozen dataclasses, and path locator.

This module is host-agnostic — no IDA, Qt, or provider imports. It defines
the stable identity primitives consumed by the registry, identity resolver,
workspace store, and manager façade.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# ID validation regexes
# ---------------------------------------------------------------------------

_MEMORY_ID_RE = re.compile(r"^mem-[0-9a-f]{32}$")
_CASE_ID_RE = re.compile(r"^case-[0-9a-f]{32}$")
_RECORD_KINDS = frozenset(
    {
        "fact",
        "entity",
        "relation",
        "observation",
        "source",
        "promotion",
        "note",
        "report",
    }
)
_RECORD_ID_RE = re.compile(r"^(?P<kind>[a-z]+)-[0-9a-f]{32}$")


# ---------------------------------------------------------------------------
# ID generators
# ---------------------------------------------------------------------------


def new_memory_id() -> str:
    """Generate a new opaque binary-workspace ID (``mem-<hex32>``)."""
    return f"mem-{uuid.uuid4().hex}"


def new_case_id() -> str:
    """Generate a new opaque case-workspace ID (``case-<hex32>``)."""
    return f"case-{uuid.uuid4().hex}"


def new_record_id(kind: str) -> str:
    """Generate a new opaque record ID (``<kind>-<hex32>``)."""
    if kind not in _RECORD_KINDS:
        raise ValueError(f"invalid record kind: {kind!r}")
    return f"{kind}-{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# ID validators
# ---------------------------------------------------------------------------


def validate_memory_id(value: str) -> str:
    """Validate and return a binary-workspace ID or raise ``ValueError``."""
    if not _MEMORY_ID_RE.fullmatch(value):
        raise ValueError("invalid memory_id")
    return value


def validate_case_id(value: str) -> str:
    """Validate and return a case-workspace ID or raise ``ValueError``."""
    if not _CASE_ID_RE.fullmatch(value):
        raise ValueError("invalid case_id")
    return value


def validate_record_id(kind: str, value: str) -> str:
    """Validate that *value* is a well-formed record ID of the given *kind*."""
    match = _RECORD_ID_RE.fullmatch(value)
    if kind not in _RECORD_KINDS or match is None or match.group("kind") != kind:
        raise ValueError(f"invalid {kind} record ID")
    return value


# ---------------------------------------------------------------------------
# Frozen dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FilesystemIdentity:
    """Durable OS-level identity for a file (device/inode or volume/index)."""

    volume_or_device: str
    file_or_inode: str

    @property
    def evidence_value(self) -> str:
        return f"{self.volume_or_device}:{self.file_or_inode}"


@dataclass(frozen=True)
class IdentityRequest:
    """Evidence bundle collected from the active IDB or raw binary.

    Passed to :class:`MemoryIdentityResolver` to find or create a workspace.
    """

    source_kind: Literal["idb", "raw"]
    idb_path: str
    db_instance_id: str = ""
    source_sha256: str = ""
    display_name: str = ""
    filesystem_identity: FilesystemIdentity | None = None


@dataclass(frozen=True)
class WorkspaceBinding:
    """Result of resolving an :class:`IdentityRequest`.

    ``memory_id`` is empty only when state is ``ephemeral`` or ``disabled``;
    it is never used as a path component in that case.
    """

    memory_id: str
    state: Literal["active", "provisional", "ephemeral", "disabled"]
    display_name: str
    warning: str = ""
    uuid_write_pending: bool = False


@dataclass(frozen=True)
class MemoryRunContext:
    """Frozen run binding captured at the start of each agent run.

    ``database_generation`` and ``case_binding_generation`` are process-local
    counters that increment when the active database or case changes. Record
    revisions remain separate SQLite optimistic-concurrency values; committing
    content does not invalidate the run.
    """

    binary_memory_id: str
    active_case_id: str
    database_generation: int
    case_binding_generation: int


# ---------------------------------------------------------------------------
# WorkspacePaths and MemoryLocator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspacePaths:
    """Canonical filesystem paths for one workspace."""

    root: Path
    database: Path
    markdown: Path
    notes: Path
    reports: Path
    lock: Path


class MemoryLocator:
    """Maps generated workspace IDs to canonical filesystem paths.

    All paths are under ``<config_dir>/memory/``. Directory components are
    generated UUIDs — user-provided names, paths, hashes, and netnode values
    are never joined directly into filesystem paths.
    """

    def __init__(self, memory_root: str | Path) -> None:
        self.root = Path(memory_root)

    def registry_database(self) -> Path:
        """Path to the central registry database."""
        return self.root / "registry.db"

    def binary(self, memory_id: str) -> WorkspacePaths:
        """Paths for a binary workspace."""
        return self._workspace("binaries", validate_memory_id(memory_id))

    def case(self, case_id: str) -> WorkspacePaths:
        """Paths for a case workspace."""
        return self._workspace("cases", validate_case_id(case_id))

    def backups(self, workspace_id: str) -> Path:
        """Directory holding pre-migration backups for *workspace_id*.

        Accepts both binary (``mem-...``) and case (``case-...``)
        workspace IDs. The directory is *not* created — callers (or
        :func:`_exclusive_backup_path`) materialize it lazily.
        """
        if not (_MEMORY_ID_RE.fullmatch(workspace_id) or _CASE_ID_RE.fullmatch(workspace_id)):
            raise ValueError(f"invalid workspace ID: {workspace_id!r}")
        return self.root / "backups" / workspace_id

    def _workspace(self, group: str, workspace_id: str) -> WorkspacePaths:
        root = self.root / group / workspace_id
        notes = root / "notes"
        return WorkspacePaths(
            root=root,
            database=root / "memory.db",
            markdown=root / "MEMORY.md",
            notes=notes,
            reports=notes / "reports",
            lock=root / ".workspace.lock",
        )
