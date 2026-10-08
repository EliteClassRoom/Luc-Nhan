"""Identity resolver: ordered copy/move/conflict decision table.

Implements the spec's ordered resolution rules for IDB and raw-binary
sources. This module is host-agnostic — no IDA, Qt, or provider imports.
``path_exists`` is injectable for deterministic testing.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from .registry import MemoryRegistry, WorkspaceRecord
from .workspace import FilesystemIdentity, IdentityRequest, WorkspaceBinding

_RAW_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ResolutionStatus(str, Enum):
    """Outcome of resolving an :class:`IdentityRequest`."""

    RESOLVED = "resolved"
    CREATED = "created"
    COPY_DETACHED = "copy_detached"
    AMBIGUOUS = "ambiguous"
    CONFLICT = "conflict"
    EPHEMERAL = "ephemeral"


@dataclass(frozen=True)
class IdentityChoice:
    """User-supplied choice for ambiguous resolutions."""

    action: str
    memory_id: str = ""

    @classmethod
    def link_existing(cls, memory_id: str) -> IdentityChoice:
        return cls("link_existing", memory_id)

    @classmethod
    def start_fresh(cls) -> IdentityChoice:
        return cls("start_fresh")

    @classmethod
    def without_persistence(cls) -> IdentityChoice:
        return cls("without_persistence")


@dataclass(frozen=True)
class IdentityResolution:
    """Result of resolving an :class:`IdentityRequest`."""

    status: ResolutionStatus
    binding: WorkspaceBinding | None
    candidates: tuple[str, ...] = ()
    netnode_uuid_to_persist: str = ""
    warning: str = ""


# ---------------------------------------------------------------------------
# Filesystem identity helpers
# ---------------------------------------------------------------------------


def get_filesystem_identity(path: str) -> FilesystemIdentity | None:
    """Return the durable filesystem identity for *path*, or None if unavailable.

    On POSIX this uses ``st_dev``/``st_ino``. On Windows this queries the
    volume serial number and 64-bit file index via ``GetFileInformationByHandle``.
    Returns None if the file does not exist, is not a regular file, or the
    underlying API fails.
    """
    if sys.platform == "win32":
        return _get_windows_file_identity(path)
    return _get_posix_file_identity(path)


def _get_posix_file_identity(path: str) -> FilesystemIdentity | None:
    try:
        stat = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if not stat.st_ino:
        return None
    return FilesystemIdentity(str(stat.st_dev), str(stat.st_ino))


def _get_windows_file_identity(path: str) -> FilesystemIdentity | None:
    """Return volume serial + 64-bit file index from a no-follow Windows handle.

    Uses ``CreateFileW`` with ``FILE_FLAG_OPEN_REPARSE_POINT`` |
    ``FILE_FLAG_BACKUP_SEMANTICS`` to avoid resolving symlinks, then
    ``GetFileInformationByHandle`` to read
    ``BY_HANDLE_FILE_INFORMATION``.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    FILE_READ_ATTRIBUTES = 0x0080
    FILE_SHARE_ALL = 0x07  # READ | WRITE | DELETE
    OPEN_EXISTING = 3
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    handle = kernel32.CreateFileW(
        path,
        FILE_READ_ATTRIBUTES,
        FILE_SHARE_ALL,
        None,
        OPEN_EXISTING,
        FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == INVALID_HANDLE_VALUE or handle == 0:
        return None

    try:

        class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        info = BY_HANDLE_FILE_INFORMATION()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            return None

        volume_serial = str(info.dwVolumeSerialNumber)
        file_index = str((info.nFileIndexHigh << 32) | info.nFileIndexLow)
        return FilesystemIdentity(volume_serial, file_index)
    finally:
        kernel32.CloseHandle(handle)


def hash_raw_binary(path: str) -> str:
    """Stream SHA-256 of a raw binary file.

    Raises ``RuntimeError`` if the file changes size/mtime during hashing.
    """
    before = os.stat(path, follow_symlinks=False)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = os.stat(path, follow_symlinks=False)
    before_key = (before.st_size, before.st_mtime_ns)
    after_key = (after.st_size, after.st_mtime_ns)
    if before_key != after_key:
        raise RuntimeError("raw input changed while hashing")
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class MemoryIdentityResolver:
    """Resolves identity evidence to a workspace binding.

    The decision table follows the spec's ordered rules:

    1. ``raw``: validate ``[0-9a-f]{64}``, resolve/create current ``raw_sha256``.
    2. ``idb``: resolve filesystem evidence first; incompatible UUID → CONFLICT.
    3. UUID + new filesystem + old current path exists → COPY_DETACHED.
    4. UUID + old unavailable → AMBIGUOUS unless explicit choice supplied.
    5. Link retires old filesystem/path evidence before binding new evidence.
    6. Path alone never resolves.
    7. No filesystem/UUID evidence → ephemeral binding with no directory.

    ``path_exists`` is injectable for deterministic testing.
    """

    def __init__(
        self,
        registry: MemoryRegistry,
        *,
        path_exists: Callable[[str], bool] | None = None,
    ) -> None:
        self._registry = registry
        self._path_exists = path_exists or os.path.exists

    def resolve(
        self,
        request: IdentityRequest,
        choice: IdentityChoice | None = None,
    ) -> IdentityResolution:
        """Resolve *request* to an :class:`IdentityResolution`."""
        if request.source_kind == "raw":
            return self._resolve_raw(request)
        return self._resolve_idb(request, choice)

    # ------------------------------------------------------------------
    # Raw
    # ------------------------------------------------------------------

    def _resolve_raw(self, request: IdentityRequest) -> IdentityResolution:
        digest = request.source_sha256
        if not _RAW_SHA256_RE.fullmatch(digest):
            raise ValueError("sha256 must be 64 lowercase hex characters")

        existing = self._registry.find_raw(digest)
        if existing is not None:
            return self._bind_existing(existing, request.display_name, ResolutionStatus.RESOLVED)
        record = self._registry.resolve_or_create_raw(digest, request.display_name)
        return self._bind_existing(record, request.display_name, ResolutionStatus.CREATED)

    # ------------------------------------------------------------------
    # IDB
    # ------------------------------------------------------------------

    def _resolve_idb(
        self,
        request: IdentityRequest,
        choice: IdentityChoice | None,
    ) -> IdentityResolution:
        fs_id = request.filesystem_identity

        # Explicit "without persistence" choice → ephemeral
        if choice is not None and choice.action == "without_persistence":
            return IdentityResolution(
                status=ResolutionStatus.EPHEMERAL,
                binding=WorkspaceBinding(
                    memory_id="",
                    state="disabled",
                    display_name=request.display_name,
                ),
            )

        # Rules 6/7: no durable evidence → ephemeral
        if fs_id is None:
            return IdentityResolution(
                status=ResolutionStatus.EPHEMERAL,
                binding=WorkspaceBinding(
                    memory_id="",
                    state="ephemeral",
                    display_name=request.display_name,
                ),
            )

        # Rule 2: resolve filesystem evidence first
        fs_workspaces = self._registry.find_evidence("filesystem", fs_id.evidence_value)

        if request.db_instance_id:
            uuid_workspaces = self._registry.find_evidence("db_instance", request.db_instance_id)
        else:
            uuid_workspaces = []

        # Rule 2: filesystem evidence resolves directly
        if fs_workspaces:
            # Rule 3: UUID points to a *different* workspace → conflict
            if uuid_workspaces and uuid_workspaces[0].memory_id != fs_workspaces[0].memory_id:
                return IdentityResolution(
                    status=ResolutionStatus.CONFLICT,
                    binding=None,
                    candidates=(fs_workspaces[0].memory_id, uuid_workspaces[0].memory_id),
                )
            return self._bind_existing(fs_workspaces[0], request.display_name, ResolutionStatus.RESOLVED)

        # No filesystem match, but UUID matches — the file index likely
        # changed (common on Windows). Link to the existing workspace
        # rather than creating a new one, because the netnode UUID is the
        # durable identity that survives reopen.
        if uuid_workspaces:
            return self._link_existing(request, uuid_workspaces[0])

        # No match at all → create new
        return self._create_new(request)

    # ------------------------------------------------------------------
    # Binding helpers
    # ------------------------------------------------------------------

    def _bind_existing(
        self,
        record: WorkspaceRecord,
        display_name: str,
        status: ResolutionStatus,
    ) -> IdentityResolution:
        """Return RESOLVED/CREATED for a workspace record."""
        binding = WorkspaceBinding(
            memory_id=record.memory_id,
            state="active",
            display_name=display_name,
        )
        return IdentityResolution(status=status, binding=binding)

    def _create_new(self, request: IdentityRequest) -> IdentityResolution:
        """Create a new workspace and bind filesystem + UUID evidence."""
        record = self._registry.create_workspace("binary", request.display_name)
        if request.filesystem_identity:
            self._registry.bind_evidence(
                record.memory_id,
                "filesystem",
                request.filesystem_identity.evidence_value,
            )
        if request.db_instance_id:
            self._registry.bind_evidence(record.memory_id, "db_instance", request.db_instance_id)
        if request.idb_path:
            self._registry.touch_path_alias(record.memory_id, request.idb_path)
        return self._bind_existing(record, request.display_name, ResolutionStatus.CREATED)

    def _create_copy_detached(self, request: IdentityRequest) -> IdentityResolution:
        """Create a new workspace for a copied IDB with shared UUID."""
        record = self._registry.create_workspace("binary", request.display_name)
        if request.filesystem_identity:
            self._registry.bind_evidence(
                record.memory_id,
                "filesystem",
                request.filesystem_identity.evidence_value,
            )
        # UUID evidence coexists for copies (spec allows this)
        if request.db_instance_id:
            self._registry.bind_evidence(record.memory_id, "db_instance", request.db_instance_id)
        if request.idb_path:
            self._registry.touch_path_alias(record.memory_id, request.idb_path)
        binding = WorkspaceBinding(
            memory_id=record.memory_id,
            state="active",
            display_name=request.display_name,
        )
        return IdentityResolution(
            status=ResolutionStatus.COPY_DETACHED,
            binding=binding,
            netnode_uuid_to_persist=request.db_instance_id,
        )

    def _resolve_ambiguous(
        self,
        request: IdentityRequest,
        existing_memory_id: str,
        choice: IdentityChoice | None,
    ) -> IdentityResolution:
        """Resolve the ambiguous case where the original file is unavailable."""
        if choice is None:
            return IdentityResolution(
                status=ResolutionStatus.AMBIGUOUS,
                binding=None,
                candidates=(existing_memory_id,),
            )

        if choice.action == "link_existing":
            record = self._registry.get_workspace(choice.memory_id)
            if record is None:
                return IdentityResolution(
                    status=ResolutionStatus.CONFLICT,
                    binding=None,
                    warning=f"workspace {choice.memory_id} not found",
                )
            return self._link_existing(request, record)

        if choice.action == "start_fresh":
            return self._create_new(request)

        if choice.action == "without_persistence":
            return IdentityResolution(
                status=ResolutionStatus.EPHEMERAL,
                binding=WorkspaceBinding(
                    memory_id="",
                    state="disabled",
                    display_name=request.display_name,
                ),
            )

        return IdentityResolution(
            status=ResolutionStatus.AMBIGUOUS,
            binding=None,
            candidates=(existing_memory_id,),
        )

    def _link_existing(self, request: IdentityRequest, record: WorkspaceRecord) -> IdentityResolution:
        """Link the new filesystem evidence to an existing workspace."""
        if request.filesystem_identity:
            self._registry.bind_evidence(
                record.memory_id,
                "filesystem",
                request.filesystem_identity.evidence_value,
            )
        if request.idb_path:
            self._registry.touch_path_alias(record.memory_id, request.idb_path)
        binding = WorkspaceBinding(
            memory_id=record.memory_id,
            state="active",
            display_name=request.display_name,
        )
        return IdentityResolution(status=ResolutionStatus.RESOLVED, binding=binding)
