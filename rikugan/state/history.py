"""Session history: persist, list, and restore past sessions.

This is the single persistence layer for all session state.
Includes a versioned session manifest (index file) for fast startup
filtering without opening/parsing every session JSON.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..constants import HISTORY_TITLE_MAX_CHARS, SESSION_SCHEMA_VERSION
from ..core.atomic_io import atomic_replace
from ..core.config import RikuganConfig
from ..core.logging import log_debug, log_warning
from ..core.types import (
    Message,
    Role,
    _safe_persisted_identifier,
    _safe_persisted_text,
)
from .session import SessionState

# ``_safe_persisted_text`` / ``_safe_persisted_identifier`` are imported
# from ``core.types`` (single source of truth). The lenient helper
# preserves ``<`` / ``>`` for content fields; the strict helper scrubs
# them for identifiers and metadata values.


MANIFEST_FILE = "_session_manifest.json"
#: Title-aware manifest shape (spec §9.2). The bump from 1 → 2 introduces
#: ``updated_at`` (whole-seconds mtime) on each entry and forces a one-time
#: rebuild that derives titles from existing session JSON without rewriting
#: any session file.
MANIFEST_SCHEMA_VERSION = 2

#: Fallback used by :func:`derive_history_title` when no usable user
#: message survives sanitization. Rendered as plain text in History rows
#: and shorter tab labels — never interpolated into QSS or sent to the
#: LLM (spec §11.3).
UNTITLED_HISTORY_TITLE = "Untitled chat"


#: Terminal outcome of an ordered persistence deletion. The persistence
#: worker emits exactly one ``SessionDeleteOutcome`` per request and
#: exceptions are never used for cross-thread control flow (spec §6.5).
class SessionDeleteStatus(str, Enum):
    DELETED = "deleted"
    NOT_FOUND = "not_found"
    WRONG_IDB = "wrong_idb"
    FAILED = "failed"


#: Typed terminal result returned by the ordered delete worker. ``error``
#: is empty on every status except ``FAILED``, where it carries a
#: sanitized diagnostic string for the log file but is NEVER rendered to
#: the user — the controller / History panel shows a generic notice and
#: keeps the row state consistent. Wire values are stable strings, NOT
#: the public ``HistoryDeleteStatus`` enum (Task 1 owns the UI contract).
@dataclass(frozen=True)
class SessionDeleteOutcome:
    status: SessionDeleteStatus
    error: str = ""


#: Conservative session-id rule (spec §11.1). The current generator emits
#: 12 lowercase hex characters, so the existing ID space is a strict
#: subset of this rule. ``-`` and ``_`` allow human-readable IDs without
#: permitting any path separator, NUL, or control character.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _validate_session_id(session_id: object) -> bool:
    """Return True iff *session_id* is a safe filesystem identifier.

    Rejects non-strings, empty strings, the ``.``/``..`` specials, and
    anything outside ``[A-Za-z0-9_-]{1,32}``. Used as the single guard
    at every SessionHistory boundary that accepts a session id.
    """
    if not isinstance(session_id, str):
        return False
    if not session_id or session_id in {".", ".."}:
        return False
    return _SESSION_ID_RE.fullmatch(session_id) is not None


#: Fork writes a ``{id}.summary.json`` beside each session with ``messages``
#: as an int count (not a list) for fast listing. MAIN never writes these,
#: but they linger on disk after a user runs the fork, so directory scans
#: must skip them rather than treat them as sessions.
_SUMMARY_SUFFIX = ".summary.json"

#: Single-worker pool for off-main-thread session saves. A single worker is
#: enough (saves are I/O-bound and serialising them avoids manifest
#: read-modify-write races for back-to-back calls from the same tab). The
#: process-wide ``_manifest_lock`` still guards cross-tab/cross-process
#: manifest writes. ``max_workers=1`` keeps ordering deterministic.
_SAVE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rikugan-save")


def _normalize_db_path(path: str) -> str:
    """Return a stable canonical DB path for session filtering."""
    if not path:
        return ""
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(path)))
    except OSError:
        return path


_HEX_INSTANCE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _safe_last_full_scan(value: object) -> float:
    """Return a finite ``last_full_scan`` timestamp, or ``0.0`` on malformed input.

    Manifests persisted by older Rikugan versions or hand-edited by the
    operator may carry a non-numeric ``last_full_scan``. The ordered
    delete worker refuses to propagate that as an unhandled exception:
    a corrupt value is replaced with ``0.0`` (forcing the next list to
    rebuild) and logged, never re-raised. ``bool`` is explicitly excluded
    so ``True``/``False`` don't accidentally appear as timestamps; string
    numerics like ``"1700000000"`` are accepted because that's how an
    operator might patch a manifest in a pinch.
    """
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        try:
            result = float(value)
        except (ValueError, OverflowError):
            return 0.0
        if result != result or result in (float("inf"), float("-inf")):
            return 0.0
        return result
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return 0.0
        try:
            result = float(stripped)
        except ValueError:
            log_warning(f"Ignoring malformed manifest last_full_scan: {value!r}")
            return 0.0
        if result != result or result in (float("inf"), float("-inf")):
            return 0.0
        return result
    return 0.0


def _canonical_instance_id(value: object) -> str:
    """Return the canonical 32-hex ``db_instance_id`` or empty string.

    Spec section 8.3 mandates that persisted instance IDs are sanitized
    and compared as canonical lowercase hex after trimming surrounding
    whitespace. Malformed values (wrong length, non-hex, ``None``) are
    treated as absent so the caller falls back to the legacy path-based
    match rather than silently rejecting a valid same-path row.
    """
    if value is None:
        return ""
    text = _safe_persisted_identifier(value).strip().lower()
    if not text or _HEX_INSTANCE_ID_RE.fullmatch(text) is None:
        return ""
    return text


def derive_history_title(
    messages: Sequence[Message],
    max_chars: int = HISTORY_TITLE_MAX_CHARS,
) -> str:
    """Derive a safe, plain-text title for a chat history row.

    Pipeline (spec §9.1):
      1. Find the first message whose role is ``Role.USER`` and whose
         content survives sanitization and whitespace trimming.
      2. Strip injection-marker patterns via :func:`_safe_persisted_identifier`
         (the strict helper also scrubs ``<``/``>`` — titles are not
         rendered into rich text or sent back to the LLM as instructions).
      3. Collapse all whitespace and line breaks to single spaces.
      4. Truncate to exactly ``max_chars`` characters with **no** ellipsis
         appended (the caller's UI may elide for tab labels; the stored
         value stays exact-length so list rendering does not double-ellipsize).
      5. Fall back to :data:`UNTITLED_HISTORY_TITLE` when no usable user
         message exists, so History rows always have a non-empty title.

    This helper is the single shared derivation point for the manifest
    entry description, the History panel row title, and the tab label.
    """
    if max_chars < 0:
        max_chars = 0
    for message in messages:
        if message.role is not Role.USER:
            continue
        text = _safe_persisted_identifier(message.content)
        text = " ".join(text.split())
        if text:
            return text[:max_chars]
    return UNTITLED_HISTORY_TITLE


def _matches_current_idb(
    *,
    entry_idb_path: object,
    entry_db_instance_id: object,
    target_idb_path: str,
    target_db_instance_id: str,
) -> bool:
    """Single source of truth for "does this entry belong to the target IDB".

    Spec §8.3 requires one pure helper over a normalized target record so
    list-time and post-load authorization cannot drift. Manifest entries
    and loaded ``SessionState`` objects are adapted to that same record
    before matching.

    Matching rules:
      * If the entry has a canonical 32-hex ``db_instance_id``, that must
        equal the target's canonical instance id (path is display only).
      * Otherwise, fall back to a normalized non-empty path match.
      * A row with neither a valid instance id nor a non-empty path
        never matches — it must be excluded by the caller.
    """
    entry_instance = _canonical_instance_id(entry_db_instance_id)
    target_instance = _canonical_instance_id(target_db_instance_id)
    if entry_instance:
        return bool(target_instance) and entry_instance == target_instance
    entry_path = _normalize_db_path(str(entry_idb_path or ""))
    return bool(entry_path) and entry_path == _normalize_db_path(target_idb_path)


def _entry_matches_current_idb(
    entry: dict[str, Any],
    target_idb_path: str,
    target_db_instance_id: str,
) -> bool:
    """Adapter from a manifest entry dict to :func:`_matches_current_idb`."""
    return _matches_current_idb(
        entry_idb_path=entry.get("idb_path", ""),
        entry_db_instance_id=entry.get("db_instance_id", ""),
        target_idb_path=target_idb_path,
        target_db_instance_id=target_db_instance_id,
    )


class SessionHistory:
    """Manages saved sessions on disk.

    Uses a versioned manifest (JSON index) for fast session listing.
    The manifest is validated against file mtime/size before trusting.
    Falls back to full directory scan for backfill/recovery.
    """

    # Process-local lock serialises manifest read-modify-write operations
    # so concurrent saves in the *same process* (e.g. multiple tabs) cannot
    # silently drop entries.
    _manifest_lock = threading.RLock()

    def __init__(self, config: RikuganConfig):
        self._dir = os.path.join(config.checkpoints_dir, "sessions")
        os.makedirs(self._dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Manifest helpers
    # ------------------------------------------------------------------

    def _manifest_path(self) -> str:
        return os.path.join(self._dir, MANIFEST_FILE)

    def _session_path(self, session_id: str) -> str | None:
        """Resolve a safe absolute path for *session_id* inside ``self._dir``.

        Returns ``None`` when the id fails validation or when the resolved
        path would escape ``self._dir``. Every public boundary that touches
        a session file must call this first (spec §11.1).
        """
        if not _validate_session_id(session_id):
            return None
        root = os.path.normcase(os.path.realpath(self._dir))
        candidate = os.path.normcase(os.path.realpath(os.path.join(root, f"{session_id}.json")))
        try:
            if os.path.commonpath((root, candidate)) != root:
                return None
        except ValueError:
            # Different drives on Windows, or otherwise incomparable paths.
            return None
        return candidate

    def _read_manifest(self) -> dict[str, Any]:
        """Read the session manifest.

        Returns **entries** (``{'entries': {...}, 'version': int,
        'last_full_scan': 0}``) or ``{}`` if missing or corrupt.
        """
        path = self._manifest_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log_debug(f"Session manifest corrupt: {exc}")
            return {}
        if not isinstance(data, dict):
            return {}
        version = data.get("version", 0)
        if version != MANIFEST_SCHEMA_VERSION:
            log_debug(f"Session manifest version mismatch ({version} != {MANIFEST_SCHEMA_VERSION}), rebuilding")
            return {}
        entries = data.get("entries", {})
        if not isinstance(entries, dict):
            return {}
        return data

    def _write_manifest(self, entries: dict[str, dict[str, Any]], last_full_scan: float = 0.0) -> None:
        """Atomically write the session manifest to disk.

        Writes to a temp file and renames to avoid partial/corrupt writes.
        Cleans up the temp file on any write failure.
        """
        path = self._manifest_path()
        data: dict[str, Any] = {
            "version": MANIFEST_SCHEMA_VERSION,
            "entries": entries,
        }
        if last_full_scan > 0:
            data["last_full_scan"] = last_full_scan
        tmp_path = ""
        try:
            fd, tmp_path = tempfile.mkstemp(dir=self._dir, prefix=".manifest_tmp_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                # Compact — manifest is parsed on every list_sessions() call.
                json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
            atomic_replace(tmp_path, path)
        except Exception as e:
            log_warning(f"Failed to write session manifest: {e}")
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError as cleanup_err:
                    log_warning(f"Failed to clean up temp manifest {tmp_path}: {cleanup_err}")
            raise

    def _build_manifest_entry(self, session: SessionState, description: str = "") -> dict[str, Any]:
        """Build a manifest entry dict for a session.

        Uses ``st_mtime_ns`` (nanosecond precision) for reliable
        validation, falling back to ``int(st_mtime * 1e9)`` on older
        Python where ``st_mtime_ns`` is unavailable. ``updated_at`` is the
        whole-seconds mtime used for newest-first sorting and display only;
        ``file_mtime_ns`` stays the validation anchor (spec §9.2).
        """
        db_path = _normalize_db_path(session.idb_path)
        file_path = os.path.join(self._dir, f"{session.id}.json")
        try:
            st = os.stat(file_path)
            file_mtime = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
            file_size = st.st_size
            updated_at = int(st.st_mtime)
        except OSError:
            file_mtime = 0
            file_size = 0
            updated_at = 0
        return {
            "created_at": session.created_at,
            "updated_at": updated_at,
            "provider": session.provider_name,
            "model": session.model_name,
            "idb_path": db_path,
            "db_instance_id": session.db_instance_id,
            "binary_memory_id": session.binary_memory_id,
            "active_case_id": session.active_case_id,
            "messages": len(session.messages),
            "description": description,
            "file_mtime_ns": file_mtime,
            "file_size": file_size,
        }

    def _update_manifest_entry(self, session: SessionState, description: str = "") -> None:
        """Add or update a manifest entry for *session*.

        Locked internally so concurrent saves in the same process do not
        drop entries.  Failure to update the manifest is non-fatal — the
        session JSON has already been saved and the manifest can be rebuilt
        on next startup.
        """
        try:
            with self._manifest_lock:
                data = self._read_manifest()
                entries = data.get("entries", {})
                last_full_scan = data.get("last_full_scan", 0.0)
                entries[session.id] = self._build_manifest_entry(session, description)
                try:
                    self._write_manifest(entries, last_full_scan=last_full_scan)
                except OSError as write_err:
                    log_warning(f"Failed to update session manifest after saving {session.id}: {write_err}")
        except Exception as e:
            log_warning(f"Failed to update session manifest entry for {session.id}: {e}")

    def _validate_manifest_entry(self, session_id: str, entry: dict[str, Any]) -> bool:
        """Check that the session file on disk matches the manifest entry.

        Returns True if the file exists and its mtime_ns/size match.
        Uses ``st_mtime_ns`` for nanosecond precision; falls back to
        ``int(st_mtime * 1e9)``.

        Invalid session ids (tampered or hostile manifest) are rejected
        without any filesystem operation.
        """
        file_path = self._session_path(session_id)
        if file_path is None:
            log_warning(f"Skipping invalid session id in manifest: {session_id!r}")
            return False
        try:
            st = os.stat(file_path)
        except OSError:
            return False
        stored_mtime = entry.get("file_mtime_ns", 0)
        stored_size = entry.get("file_size", 0)
        current_mtime = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
        return stored_mtime == current_mtime and stored_size == st.st_size

    def _rebuild_manifest(self) -> dict[str, dict[str, Any]]:
        """Full scan of session JSON files and rebuild the manifest.

        Called on first run (no manifest) or when the manifest is stale/corrupt.
        Writes the manifest under the class-level lock; write failure is
        non-fatal — entries are always returned.

        Spec §9.2 requires:
          * Validate each JSON/filename session id before admitting it.
          * Hydrate messages through :func:`Message.from_dict` so corrupt
            individual rows are skipped without crashing the rebuild
            (spec §9.2 — "reads existing session JSON files once").
          * Derive a title via :func:`derive_history_title` when the
            legacy JSON lacks ``description``; an explicit stored
            description is preserved verbatim (still sanitized through
            :func:`_safe_persisted_identifier`).
          * Record ``updated_at`` as ``int(st_mtime)`` for display/sort
            while ``file_mtime_ns`` stays the validation anchor.
          * Never rewrite the session JSON files.
          * Exclude zero-message legacy rows from the rebuilt manifest
            (spec §7.3).
        """
        entries: dict[str, dict[str, Any]] = {}
        try:
            fnames = os.listdir(self._dir)
        except OSError:
            return entries
        for fname in sorted(fnames):
            if not fname.endswith(".json") or fname == MANIFEST_FILE:
                continue
            # Skip fork summary files: their "messages" is an int count, not a
            # list, and their id-slice (fname[:-5]) would yield "{id}.summary".
            if fname.endswith(_SUMMARY_SUFFIX):
                continue
            path = os.path.join(self._dir, fname)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                log_debug(f"Skipping corrupt session JSON {fname}: {exc}")
                continue
            sid = data.get("id", fname[:-5])
            if not _validate_session_id(sid):
                log_warning(f"Skipping session file with invalid id: {fname}")
                continue
            try:
                st = os.stat(path)
                file_mtime = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
                file_size = st.st_size
                updated_at = int(st.st_mtime)
            except OSError:
                file_mtime = 0
                file_size = 0
                updated_at = 0
            # Hydrate messages one at a time so a single bad row does
            # not poison the rebuild (spec §9.2 — "resiliently").
            hydrated_messages: list[Message] = []
            for md in data.get("messages", []) or []:
                if not isinstance(md, dict):
                    continue
                try:
                    hydrated_messages.append(Message.from_dict(md))
                except (KeyError, TypeError, ValueError) as exc:
                    log_warning(f"Skipping corrupt message during rebuild of {fname}: {exc}")
                    continue
            # Spec §7.3 — exclude zero-message legacy rows so History
            # never surfaces an empty draft.
            if not hydrated_messages:
                log_debug(f"Skipping zero-message session during rebuild: {fname}")
                continue
            explicit = _safe_persisted_identifier(data.get("description", ""))
            description = explicit or derive_history_title(hydrated_messages)
            entries[sid] = {
                "created_at": data.get("created_at", 0),
                "updated_at": updated_at,
                "provider": data.get("provider_name", ""),
                "model": data.get("model_name", ""),
                "idb_path": _normalize_db_path(data.get("idb_path", "")),
                "db_instance_id": data.get("db_instance_id", ""),
                "binary_memory_id": data.get("binary_memory_id", ""),
                "active_case_id": data.get("active_case_id", ""),
                "messages": len(hydrated_messages),
                "description": description,
                "file_mtime_ns": file_mtime,
                "file_size": file_size,
            }
        last_full_scan = time.time()
        with self._manifest_lock:
            try:
                self._write_manifest(entries, last_full_scan=last_full_scan)
            except OSError as write_err:
                log_warning(
                    f"Failed to write rebuilt session manifest: {write_err}. "
                    f"Session listing will still work from directory scan."
                )
        return entries

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def save_session(self, session: SessionState, description: str = "") -> str:
        """Save a session atomically and return the file path.

        The persisted ``description`` (manifest title) is either an
        explicit value supplied by the caller — sanitized through
        :func:`_safe_persisted_identifier` — or derived from the first
        user message via :func:`derive_history_title` so legacy callers
        and new saves agree on a single derivation pipeline (spec §9.1).
        """
        path = os.path.join(self._dir, f"{session.id}.json")
        db_path = _normalize_db_path(session.idb_path)
        data = {
            "schema_version": SESSION_SCHEMA_VERSION,
            "id": session.id,
            "created_at": session.created_at,
            "provider_name": session.provider_name,
            "model_name": session.model_name,
            "idb_path": db_path,
            "db_instance_id": session.db_instance_id,
            "binary_memory_id": session.binary_memory_id,
            "active_case_id": session.active_case_id,
            "current_turn": session.current_turn,
            "metadata": session.metadata,
            "messages": [m.to_dict() for m in session.messages],
        }
        if session.subagent_logs:
            data["subagent_logs"] = {key: [m.to_dict() for m in msgs] for key, msgs in session.subagent_logs.items()}
        # Resolve the description once: explicit (sanitized) wins, else
        # derive from the message stream. Reused by both the persisted
        # payload and the manifest entry so the two cannot drift.
        explicit = _safe_persisted_identifier(description)
        resolved_description = explicit or derive_history_title(session.messages)
        if resolved_description and resolved_description != UNTITLED_HISTORY_TITLE:
            data["description"] = resolved_description
        # Write to temp file first, then atomically rename to final path.
        tmp_path = ""
        try:
            fd, tmp_path = tempfile.mkstemp(dir=self._dir, prefix=".session_tmp_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                # Compact separators — internal autosaves don't need pretty
                # printing and the size difference matters for long sessions
                # (typical 200-message session drops ~30% on disk and is
                # significantly faster to write/read).
                json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
            atomic_replace(tmp_path, path)
        except Exception as e:
            log_warning(f"Failed to save session {session.id}: {e}")
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError as cleanup_err:
                    log_warning(f"Failed to clean up temp session {tmp_path}: {cleanup_err}")
            raise
        # Update manifest.  Failure is non-fatal — the session JSON has
        # already been saved atomically and the manifest can be rebuilt
        # from the directory on next startup.
        try:
            self._update_manifest_entry(session, description=resolved_description)
        except Exception as manifest_err:
            log_warning(f"Failed to update session manifest for {session.id}: {manifest_err}")
        return path

    def save_session_async(self, session: SessionState, description: str = "") -> Future[str]:
        """Save a session on a background thread; return a Future for the path.

        Why this exists: ``save_session`` JSON-encodes the entire transcript
        and writes it to disk synchronously. When the caller is a Qt
        main-thread handler (end-of-turn auto-save, tab close, "new chat"),
        that dump blocks the shared IDA Pro event loop for the duration of
        the write — a freeze spike that grows with conversation size. This
        wrapper off-loads the identical work to a worker thread so the
        caller returns immediately.

        The persisted content is identical to ``save_session`` — this is a
        threading wrapper, not a separate code path.
        """
        return _SAVE_EXECUTOR.submit(self.save_session, session, description)

    @staticmethod
    def flush_saves(timeout: float = 10.0) -> None:
        """Block until every pending ``save_session_async`` has completed.

        Used at process shutdown and in tests where a caller must observe
        the saved file immediately. The single-worker executor means at
        most one save is in flight; we wait for the queue to drain by
        submitting a sentinel and waiting for it. Errors inside workers
        are surfaced via the per-save ``done_callback`` — this method does
        not re-raise them.
        """
        _SAVE_EXECUTOR.submit(lambda: None).result(timeout=timeout)

    def load_session(self, session_id: str) -> SessionState | None:
        """Load a session by ID. Returns None if not found or corrupt.

        Robustness contract (added by ``.kilo/fixing-plan.md``):
          * Opens JSON with explicit UTF-8 encoding so binary-originated
            surrogate halves and embedded NULs survive decode.
          * Skips individual corrupt message entries instead of aborting
            the whole restore (so a single poisoned message does not
            permanently break a session).
          * Sanitizes ``metadata`` string values (including ``active_goal``)
            before constructing the ``SessionState``.
          * Sanitizes subagent log keys.
          * Validates ``session_id`` at the storage boundary (spec §11.1):
            rejects empty, ``.``, ``..``, path separators, NUL, or any
            string outside ``[A-Za-z0-9_-]{1,32}`` before constructing
            a path. Returns ``None`` without touching the filesystem.
        """
        path = self._session_path(session_id)
        if path is None:
            return None
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log_debug(f"Failed to load session {session_id}: {exc}")
            return None

        # Sanitize metadata — particularly ``active_goal`` which the
        # system-prompt builder feeds into ``quote_untrusted``. Metadata
        # values flow into a prompt-bound wrapper without downstream
        # closing-tag neutralization, so we use the strict helper that
        # also strips ``<``/``>``.
        raw_metadata = data.get("metadata") or {}
        if not isinstance(raw_metadata, dict):
            raw_metadata = {}
        safe_metadata: dict[str, str] = {}
        for k, v in raw_metadata.items():
            if not isinstance(k, str):
                continue
            safe_metadata[_safe_persisted_identifier(k)] = _safe_persisted_identifier(v)

        session = SessionState(
            id=_safe_persisted_identifier(data.get("id")) or session_id,
            created_at=data.get("created_at", 0),
            provider_name=_safe_persisted_identifier(data.get("provider_name", "")),
            model_name=_safe_persisted_identifier(data.get("model_name", "")),
            idb_path=_safe_persisted_identifier(data.get("idb_path", "")),
            db_instance_id=_safe_persisted_identifier(data.get("db_instance_id", "")),
            binary_memory_id=_safe_persisted_identifier(data.get("binary_memory_id", "")),
            active_case_id=_safe_persisted_identifier(data.get("active_case_id", "")),
            current_turn=data.get("current_turn", 0),
            metadata=safe_metadata,
        )

        # Skip corrupt messages one at a time — a single bad entry must
        # not abort the entire restore.  ``Message.from_dict`` already
        # sanitizes content/tool results/tool names.
        for md in data.get("messages", []) or []:
            if not isinstance(md, dict):
                log_warning(f"Skipping non-dict message in session {session_id}")
                continue
            try:
                session.messages.append(Message.from_dict(md))
            except (KeyError, TypeError, ValueError) as exc:
                log_warning(f"Skipping corrupt session message in {session_id}: {exc}")
                continue

        # Subagent logs share the same message shape — sanitize keys and
        # skip corrupt entries the same way.
        for raw_key, msg_dicts in (data.get("subagent_logs") or {}).items():
            if not isinstance(msg_dicts, list):
                continue
            safe_key = _safe_persisted_text(raw_key)
            if not safe_key:
                continue
            restored: list[Message] = []
            for md in msg_dicts:
                if not isinstance(md, dict):
                    continue
                try:
                    restored.append(Message.from_dict(md))
                except (KeyError, TypeError, ValueError) as exc:
                    log_warning(f"Skipping corrupt subagent message in {session_id}/{safe_key}: {exc}")
                    continue
            if restored:
                session.subagent_logs[safe_key] = restored
        return session

    def list_sessions(
        self,
        idb_path: str = "",
        db_instance_id: str = "",
        binary_memory_id: str = "",
    ) -> list[dict[str, Any]]:
        """List saved session summaries, filtered by IDB path, instance ID, or memory ID.

        When ``binary_memory_id`` is supplied, it is the authoritative filter —
        path/UUID remain compatibility/display metadata.

        Uses the session manifest for fast filtering when available.
        Falls back to scanning JSON files if the manifest is missing,
        corrupt, or stale. The current-IDB filter is applied through one
        shared helper (:func:`_entry_matches_current_idb`) before *and*
        after a rebuild so list-time and post-rebuild authorization
        cannot drift (spec §8.3).

        Zero-message rows are excluded from the listing (spec §7.3 — an
        empty draft is an in-memory tab, not history). The returned
        ``description`` is sanitized again through
        :func:`_safe_persisted_identifier` before being returned, and
        falls back to ``Untitled chat`` if sanitization empties the value.
        """
        normalized_target = _normalize_db_path(idb_path)

        # Try manifest first
        manifest_data = self._read_manifest()
        entries = manifest_data.get("entries", {}) if manifest_data else {}

        if not entries:
            # No manifest or corrupt — rebuild from disk
            entries = self._rebuild_manifest()
            if not entries:
                return []

        # Detect whether there may be JSON files unknown to the manifest
        # (pre-manifest or added outside the normal save path).
        last_full_scan = manifest_data.get("last_full_scan", 0.0)
        need_rebuild = last_full_scan == 0

        if not need_rebuild and last_full_scan > 0:
            # Quick check: if any session JSON file on disk is not in the
            # manifest the listing would silently miss that session.
            try:
                json_ids = {
                    fname[:-5]
                    for fname in os.listdir(self._dir)
                    if fname.endswith(".json") and fname != MANIFEST_FILE and not fname.endswith(_SUMMARY_SUFFIX)
                }
            except OSError as scan_err:
                log_warning(f"Failed to scan sessions directory for manifest validation: {scan_err}")
                json_ids = set()
            if json_ids and not json_ids.issubset(set(entries.keys())):
                need_rebuild = True

        if need_rebuild:
            entries = self._rebuild_manifest()
            if not entries:
                return []

        sessions, manifest_misses = self._filter_manifest_entries(
            entries,
            idb_path=normalized_target,
            db_instance_id=db_instance_id,
            binary_memory_id=binary_memory_id,
        )

        # If many entries are stale, rebuild once and re-filter
        # in this same call — no recursion. The same predicate is reused
        # so the post-rebuild path cannot broaden or narrow the filter.
        if manifest_misses:
            log_debug(f"Manifest had {manifest_misses} stale entries, rebuilding")
            entries = self._rebuild_manifest()
            if not entries:
                return []
            sessions, _ = self._filter_manifest_entries(
                entries,
                idb_path=normalized_target,
                db_instance_id=db_instance_id,
                binary_memory_id=binary_memory_id,
            )

        if sessions:
            sessions.sort(key=lambda s: s.get("created_at", 0))
        return sessions

    def _filter_manifest_entries(
        self,
        entries: dict[str, dict[str, Any]],
        *,
        idb_path: str,
        db_instance_id: str,
        binary_memory_id: str,
    ) -> tuple[list[dict[str, Any]], int]:
        """Apply the single current-IDB predicate plus zero-row exclusion.

        Used by both the initial :meth:`list_sessions` loop and the
        post-rebuild re-filter loop so they cannot drift (spec §8.3).

        Returns ``(rows, manifest_misses)``. ``manifest_misses`` counts
        rows whose on-disk file is missing or whose mtime/size drifted
        from the cached entry — those drive the post-rebuild retry in
        :meth:`list_sessions`.
        """
        rows: list[dict[str, Any]] = []
        manifest_misses = 0
        for sid, entry in entries.items():
            if not _validate_session_id(sid):
                log_warning(f"Skipping invalid session id in manifest: {sid!r}")
                continue
            # ``binary_memory_id`` is the authoritative filter when supplied.
            # History v1 itself never requests workspace-wide scope (spec §8.3).
            if binary_memory_id:
                if entry.get("binary_memory_id", "") != binary_memory_id:
                    continue
            elif db_instance_id or idb_path:
                if not _entry_matches_current_idb(
                    entry,
                    target_idb_path=idb_path,
                    target_db_instance_id=db_instance_id,
                ):
                    continue
            else:
                # No filter was requested — surface every non-empty row.
                pass
            # Zero-message rows are never history (spec §7.3).
            if entry.get("messages", 0) <= 0:
                continue
            # Validate against actual file on disk.
            if not self._validate_manifest_entry(sid, entry):
                manifest_misses += 1
                continue
            # Re-sanitize description before returning — covers rows
            # written before the v2 schema or tampered since.
            description = _safe_persisted_identifier(entry.get("description", ""))
            if not description:
                description = UNTITLED_HISTORY_TITLE
            rows.append(
                {
                    "id": sid,
                    "created_at": entry.get("created_at", 0),
                    "updated_at": entry.get("updated_at", 0),
                    "provider": entry.get("provider", ""),
                    "model": entry.get("model", ""),
                    "idb_path": entry.get("idb_path", ""),
                    "db_instance_id": entry.get("db_instance_id", ""),
                    "binary_memory_id": entry.get("binary_memory_id", ""),
                    "active_case_id": entry.get("active_case_id", ""),
                    "messages": entry.get("messages", 0),
                    "description": description,
                }
            )
        return rows, manifest_misses

    @staticmethod
    def _remove_file_if_present(path: str, *, session_id: str) -> None:
        """Best-effort companion cleanup used by the ordered delete worker.

        Logs and swallows OS errors so the caller can continue without
        converting a missing secondary artifact into a primary failure
        (spec §7.2 — primary file is the authoritative user-visible
        artifact; sidecar and manifest are best-effort cleanup).
        """
        try:
            os.remove(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            log_warning(f"Failed to clean up companion for session {session_id}: {type(exc).__name__}")

    def delete_session_async(
        self,
        session_id: str,
        *,
        expected_idb_path: str,
        expected_db_instance_id: str,
    ) -> Future[SessionDeleteOutcome]:
        """Queue one hard delete behind all earlier autosaves.

        The single-worker ``_SAVE_EXECUTOR`` is the FIFO ordering
        guarantee: every save already queued completes before this
        delete runs, so a queued autosave cannot resurrect the session
        mid-deletion (spec §6.5, §7.1). The call returns a ``Future``
        immediately; the worker emits exactly one terminal
        :class:`SessionDeleteOutcome` (never raises for control flow).
        """
        return _SAVE_EXECUTOR.submit(
            self._delete_session_ordered,
            session_id,
            expected_idb_path,
            expected_db_instance_id,
        )

    def _delete_session_ordered(
        self,
        session_id: str,
        expected_idb_path: str,
        expected_db_instance_id: str,
    ) -> SessionDeleteOutcome:
        """Ordered persistence worker — never call outside the save executor.

        Contract (spec §7.2):
          * Primary JSON is authoritative. ``DELETED`` only when the
            primary file is gone. ``NOT_FOUND`` when the primary file
            is already absent at the moment we try to remove it.
          * Sidecar and manifest cleanup are best-effort after a
            successful primary removal. A failure to clean them logs a
            warning but keeps the result ``DELETED``.
          * ``WRONG_IDB`` is returned (no mutation) when the payload's
            ``id`` does not match the requested session id, when the
            path/instance does not match the expected scope, or when
            the JSON itself is missing/invalid.
          * ``FAILED`` is returned when the primary remove raises a
            non-``FileNotFoundError`` ``OSError`` — the manifest entry
            is intentionally preserved so the caller can retry.
        """
        path = self._session_path(session_id)
        if path is None:
            return SessionDeleteOutcome(SessionDeleteStatus.NOT_FOUND)

        sidecar_path = os.path.join(self._dir, f"{session_id}{_SUMMARY_SUFFIX}")
        with self._manifest_lock:
            data = self._read_manifest()
            entries = dict(data.get("entries", {}))
            last_full_scan = _safe_last_full_scan(data.get("last_full_scan"))

            if not os.path.exists(path):
                self._remove_file_if_present(sidecar_path, session_id=session_id)
                entries.pop(session_id, None)
                try:
                    self._write_manifest(entries, last_full_scan=last_full_scan)
                except OSError as exc:
                    log_warning(f"Session {session_id} was already absent, but manifest cleanup failed: {exc}")
                return SessionDeleteOutcome(SessionDeleteStatus.NOT_FOUND)

            try:
                with open(path, encoding="utf-8") as handle:
                    persisted = json.load(handle)
            except (json.JSONDecodeError, OSError):
                # Corrupt primary — refuse to delete so the operator
                # can investigate (does not match the requested scope).
                return SessionDeleteOutcome(SessionDeleteStatus.WRONG_IDB)

            # Strict identity match: payload ``id`` must equal the
            # request, and BOTH ``idb_path`` / ``db_instance_id`` must
            # match the expected scope on every populated field.
            # Mirrors :func:`_matches_current_idb` semantics for
            # list/load (instance_id authoritative when populated) but
            # adds the deletion-time requirement that the caller's
            # expected scope and the on-disk record agree on every
            # populated field — a single-field match is not enough when
            # the caller explicitly supplied the full scope.
            if persisted.get("id") != session_id:
                return SessionDeleteOutcome(SessionDeleteStatus.WRONG_IDB)
            persisted_idb_path = str(persisted.get("idb_path", ""))
            persisted_db_instance_id = str(persisted.get("db_instance_id", ""))
            if (
                expected_idb_path
                and persisted_idb_path
                and (_normalize_db_path(persisted_idb_path) != _normalize_db_path(expected_idb_path))
            ):
                return SessionDeleteOutcome(SessionDeleteStatus.WRONG_IDB)
            entry_instance = _canonical_instance_id(persisted_db_instance_id)
            target_instance = _canonical_instance_id(expected_db_instance_id)
            if entry_instance or target_instance:
                if entry_instance != target_instance:
                    return SessionDeleteOutcome(SessionDeleteStatus.WRONG_IDB)
            elif not _matches_current_idb(
                entry_idb_path=persisted_idb_path,
                entry_db_instance_id=persisted_db_instance_id,
                target_idb_path=expected_idb_path,
                target_db_instance_id=expected_db_instance_id,
            ):
                return SessionDeleteOutcome(SessionDeleteStatus.WRONG_IDB)

            try:
                os.remove(path)
            except FileNotFoundError:
                primary_status = SessionDeleteStatus.NOT_FOUND
            except OSError as exc:
                return SessionDeleteOutcome(SessionDeleteStatus.FAILED, str(exc))
            else:
                primary_status = SessionDeleteStatus.DELETED

            self._remove_file_if_present(sidecar_path, session_id=session_id)
            entries.pop(session_id, None)
            try:
                self._write_manifest(entries, last_full_scan=last_full_scan)
            except OSError as exc:
                log_warning(f"Deleted session {session_id}, but manifest cleanup failed: {exc}")
            return SessionDeleteOutcome(primary_status)
