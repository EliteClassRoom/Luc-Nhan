"""Frozen-contract tests for Chat History On-Demand shared types.

Task 1 of the on-demand history feature only locks the immutable DTO/status
contracts and the ``HISTORY_TITLE_MAX_CHARS`` constant. Later tasks consume
these symbols; renaming or re-typing them here would silently break the
history worker, controller, and UI panels that depend on the frozen shape.

Task 3 adds title-derivation, manifest v2 rebuild, current-IDB predicate
reuse, and empty-row exclusion tests on top of the same persistence layer.
"""

from __future__ import annotations

import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from lucnhan.constants import HISTORY_DELETE_SLOW_NOTICE_SECONDS, HISTORY_TITLE_MAX_CHARS
from lucnhan.core.config import LucNhanConfig
from lucnhan.core.types import Message, Role
from lucnhan.state.history import (
    MANIFEST_FILE,
    MANIFEST_SCHEMA_VERSION,
    SessionDeleteOutcome,
    SessionDeleteStatus,
    SessionHistory,
    derive_history_title,
)
from lucnhan.state.history_types import (
    HistoryAttachResult,
    HistoryAttachStatus,
    HistoryDeleteResult,
    HistoryDeleteStatus,
    HistoryLoadResult,
    HistoryRequestStatus,
    HistoryScope,
    SessionHistoryEntry,
)
from lucnhan.state.session import SessionState


def test_history_entry_and_scope_are_frozen() -> None:
    entry = SessionHistoryEntry("abc123", "Title", 1.0, 2.0, "anthropic", "claude", 3)
    scope = HistoryScope("C:/sample.i64", "deadbeef", 7)

    with pytest.raises(FrozenInstanceError):
        entry.title = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        scope.generation = 8  # type: ignore[misc]


def test_history_result_status_contracts() -> None:
    scope = HistoryScope("C:/sample.i64", "deadbeef", 1)
    loaded = HistoryLoadResult(status=HistoryRequestStatus.NOT_FOUND, scope=scope)
    attached = HistoryAttachResult(status=HistoryAttachStatus.STALE_SCOPE)

    assert loaded.session is None
    assert loaded.error == ""
    assert HistoryRequestStatus.SAVE_FLUSH_TIMEOUT.value == "save_flush_timeout"
    assert attached.tab_id == ""


def test_history_request_status_enum_values() -> None:
    # Every status string is a stable wire value that the UI, worker, and
    # persistence layer match on. Pin them so a rename cannot drift.
    assert HistoryRequestStatus.LISTED.value == "listed"
    assert HistoryRequestStatus.LOADED.value == "loaded"
    assert HistoryRequestStatus.NOT_FOUND.value == "not_found"
    assert HistoryRequestStatus.WRONG_IDB.value == "wrong_idb"
    assert HistoryRequestStatus.EMPTY.value == "empty"
    assert HistoryRequestStatus.SAVE_FLUSH_TIMEOUT.value == "save_flush_timeout"
    assert HistoryRequestStatus.FAILED.value == "failed"


def test_history_attach_status_enum_values() -> None:
    assert HistoryAttachStatus.OPENED.value == "opened"
    assert HistoryAttachStatus.ALREADY_OPEN.value == "already_open"
    assert HistoryAttachStatus.STALE_SCOPE.value == "stale_scope"


def test_history_results_are_frozen() -> None:
    scope = HistoryScope("C:/sample.i64", "deadbeef", 1)
    loaded = HistoryLoadResult(status=HistoryRequestStatus.LOADED, scope=scope)
    attached = HistoryAttachResult(status=HistoryAttachStatus.OPENED)

    with pytest.raises(FrozenInstanceError):
        loaded.error = "boom"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        attached.tab_id = "t1"  # type: ignore[misc]


def test_history_title_max_chars_constant() -> None:
    # Spec section 9.1 fixes this at 80. The UI truncates titles to it and
    # manifest rebuild derives titles against it; a change here is a
    # deliberate product decision, not a silent tweak.
    assert HISTORY_TITLE_MAX_CHARS == 80


def test_history_delete_contract_is_frozen_and_stable() -> None:
    # Delete flows share the same cross-thread contract as list/load: a
    # frozen typed result travels worker -> queue -> Qt main thread.
    # ``HISTORY_DELETE_SLOW_NOTICE_SECONDS`` is the UI's "still working"
    # threshold for slow-delete disk I/O; both the constant and the
    # ``HistoryDeleteStatus`` wire values must stay stable so later tasks
    # (delete worker, controller, panel) can build on them without drift.
    scope = HistoryScope("C:/sample.i64", "a" * 32, 7)
    result = HistoryDeleteResult(
        status=HistoryDeleteStatus.DELETED,
        scope=scope,
        session_id="abc123",
    )

    assert HISTORY_DELETE_SLOW_NOTICE_SECONDS == 30.0
    assert [status.value for status in HistoryDeleteStatus] == [
        "deleted",
        "not_found",
        "wrong_idb",
        "failed",
    ]
    assert result.error == ""
    with pytest.raises(FrozenInstanceError):
        result.session_id = "changed"  # type: ignore[misc]


def _history(tmp_path: Path) -> SessionHistory:
    config = LucNhanConfig()
    config._config_dir = str(tmp_path)
    return SessionHistory(config)


@pytest.mark.parametrize(
    "session_id",
    ["", ".", "..", "../escape", "..\\escape", "/absolute", "a" * 33, "x.json", "x\x00y"],
)
def test_invalid_session_ids_do_not_touch_storage(tmp_path: Path, session_id: str) -> None:
    history = _history(tmp_path)
    before = sorted(Path(history._dir).iterdir())

    assert history.load_session(session_id) is None
    outcome = history.delete_session_async(
        session_id,
        expected_idb_path="C:/sample.i64",
        expected_db_instance_id="a" * 32,
    ).result(timeout=5)
    assert outcome.status is SessionDeleteStatus.NOT_FOUND
    assert sorted(Path(history._dir).iterdir()) == before


def test_rebuild_and_list_skip_tampered_ids(tmp_path: Path) -> None:
    history = _history(tmp_path)
    bad_path = Path(history._dir) / "safe-file.json"
    bad_path.write_text(
        json.dumps({"id": "../../escape", "messages": [], "created_at": 1.0}),
        encoding="utf-8",
    )

    assert history.list_sessions(idb_path="C:/sample.i64") == []


def test_load_rejects_traversal_even_when_escape_file_exists(tmp_path: Path) -> None:
    history = _history(tmp_path)
    escape_path = Path(history._dir).parent / "escape.json"
    escape_path.write_text(
        json.dumps(
            {
                "id": "escape",
                "created_at": 1.0,
                "provider_name": "test",
                "model_name": "test",
                "messages": [{"role": "user", "content": "PWNED"}],
            },
        ),
        encoding="utf-8",
    )

    assert history.load_session("../escape") is None


def test_manifest_list_skips_invalid_existing_key(tmp_path: Path) -> None:
    history = _history(tmp_path)
    manifest_path = Path(history._manifest_path())
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "last_full_scan": 1.0,
                "entries": {
                    "../escape": {
                        "idb_path": "C:/sample.i64",
                        "messages": 1,
                        "file_mtime_ns": 1,
                        "file_size": 1,
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    assert history.list_sessions(idb_path="C:/sample.i64") == []


def test_valid_current_id_shape_remains_accepted(tmp_path: Path) -> None:
    history = _history(tmp_path)
    expected = os.path.normcase(os.path.realpath(str(Path(history._dir) / "abc123def456.json")))
    assert history._session_path("abc123def456") == expected
    assert isinstance(HISTORY_TITLE_MAX_CHARS, int)


# ---------------------------------------------------------------------------
# Task 2 — Ordered persistence deletion (FIFO behind autosaves).
# ---------------------------------------------------------------------------
# These tests pin the spec section 6.5 + 7.x contract: every deletion is
# submitted to the same single-worker save executor as autosaves, so
# previously queued saves always complete before the delete runs. The
# primary session JSON is the authoritative user-visible artifact;
# sidecar and manifest cleanup are best-effort. Wrong-IDB and ID mismatches
# must NOT mutate the primary file.
# ---------------------------------------------------------------------------


def _wait_delete(
    history: SessionHistory,
    session_id: str,
    idb_path: str,
    db_instance_id: str,
) -> SessionDeleteOutcome:
    """Resolve the ordered delete future with a bounded timeout.

    Returns a frozen :class:`SessionDeleteOutcome` typed result so the
    Task 2 delete tests can pin the never-raises terminal-outcome
    contract end-to-end through ``Future.result(timeout=5)``.
    """
    return history.delete_session_async(
        session_id,
        expected_idb_path=idb_path,
        expected_db_instance_id=db_instance_id,
    ).result(timeout=5)


def test_ordered_delete_removes_primary_sidecar_and_manifest(tmp_path: Path) -> None:
    history = _history(tmp_path)
    session = SessionState(
        id="delete-me",
        idb_path=str(tmp_path / "sample.i64"),
        db_instance_id="a" * 32,
    )
    session.add_message(Message(role=Role.USER, content="Delete me"))
    primary = Path(history.save_session(session))
    sidecar = primary.with_name(f"{session.id}.summary.json")
    sidecar.write_text('{"messages":1}', encoding="utf-8")

    outcome = _wait_delete(history, session.id, session.idb_path, session.db_instance_id)

    assert outcome.status is SessionDeleteStatus.DELETED
    assert not primary.exists()
    assert not sidecar.exists()
    manifest = json.loads((primary.parent / MANIFEST_FILE).read_text(encoding="utf-8"))
    assert session.id not in manifest["entries"]


def test_ordered_delete_missing_primary_cleans_stale_metadata(tmp_path: Path) -> None:
    history = _history(tmp_path)
    session = SessionState(
        id="already-gone",
        idb_path=str(tmp_path / "sample.i64"),
        db_instance_id="b" * 32,
    )
    session.add_message(Message(role=Role.USER, content="Delete me"))
    primary = Path(history.save_session(session))
    sidecar = primary.with_name(f"{session.id}.summary.json")
    sidecar.write_text('{"messages":1}', encoding="utf-8")
    primary.unlink()

    outcome = _wait_delete(history, session.id, session.idb_path, session.db_instance_id)

    assert outcome.status is SessionDeleteStatus.NOT_FOUND
    assert not sidecar.exists()
    manifest = json.loads((primary.parent / MANIFEST_FILE).read_text(encoding="utf-8"))
    assert session.id not in manifest["entries"]


@pytest.mark.parametrize(
    ("expected_path", "expected_instance"),
    [
        ("C:/other.i64", "a" * 32),
        ("C:/sample.i64", "b" * 32),
    ],
)
def test_ordered_delete_rejects_wrong_idb_without_mutation(
    tmp_path: Path,
    expected_path: str,
    expected_instance: str,
) -> None:
    history = _history(tmp_path)
    session = SessionState(
        id="keep-me",
        idb_path="C:/sample.i64",
        db_instance_id="a" * 32,
    )
    session.add_message(Message(role=Role.USER, content="Keep me"))
    primary = Path(history.save_session(session))

    outcome = _wait_delete(history, session.id, expected_path, expected_instance)

    assert outcome.status is SessionDeleteStatus.WRONG_IDB
    assert primary.exists()


def test_ordered_delete_rejects_filename_payload_id_mismatch(tmp_path: Path) -> None:
    history = _history(tmp_path)
    session = SessionState(
        id="filename-id",
        idb_path="C:/sample.i64",
        db_instance_id="a" * 32,
    )
    session.add_message(Message(role=Role.USER, content="Keep me"))
    primary = Path(history.save_session(session))
    payload = json.loads(primary.read_text(encoding="utf-8"))
    payload["id"] = "different-id"
    primary.write_text(json.dumps(payload), encoding="utf-8")

    outcome = _wait_delete(history, session.id, session.idb_path, session.db_instance_id)

    assert outcome.status is SessionDeleteStatus.WRONG_IDB
    assert primary.exists()


def test_ordered_delete_primary_remove_failure_keeps_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = _history(tmp_path)
    session = SessionState(
        id="locked",
        idb_path="C:/sample.i64",
        db_instance_id="a" * 32,
    )
    session.add_message(Message(role=Role.USER, content="Keep me"))
    primary = Path(history.save_session(session))
    original_remove = os.remove

    def fail_primary(path: str) -> None:
        if os.path.normcase(path) == os.path.normcase(str(primary)):
            raise PermissionError("locked")
        original_remove(path)

    monkeypatch.setattr(os, "remove", fail_primary)
    outcome = _wait_delete(history, session.id, session.idb_path, session.db_instance_id)

    assert outcome.status is SessionDeleteStatus.FAILED
    assert primary.exists()
    manifest = json.loads((primary.parent / MANIFEST_FILE).read_text(encoding="utf-8"))
    assert session.id in manifest["entries"]


def test_delete_runs_after_queued_save_and_wins_final_state(tmp_path: Path) -> None:
    history = _history(tmp_path)
    session = SessionState(
        id="ordered",
        idb_path="C:/sample.i64",
        db_instance_id="a" * 32,
    )
    session.add_message(Message(role=Role.USER, content="Original"))

    save_future = history.save_session_async(session)
    delete_future = history.delete_session_async(
        session.id,
        expected_idb_path=session.idb_path,
        expected_db_instance_id=session.db_instance_id,
    )

    assert Path(save_future.result(timeout=5)).name == f"{session.id}.json"
    assert delete_future.result(timeout=5).status is SessionDeleteStatus.DELETED
    SessionHistory.flush_saves(timeout=5)
    assert history.load_session(session.id) is None
    assert all(row["id"] != session.id for row in history.list_sessions())


def test_manifest_cleanup_failure_after_primary_delete_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = _history(tmp_path)
    session = SessionState(
        id="manifest-stale",
        idb_path="C:/sample.i64",
        db_instance_id="a" * 32,
    )
    session.add_message(Message(role=Role.USER, content="Delete me"))
    primary = Path(history.save_session(session))
    original_write = history._write_manifest
    calls = 0

    def fail_once(entries, last_full_scan=0.0):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("manifest locked")
        return original_write(entries, last_full_scan)

    monkeypatch.setattr(history, "_write_manifest", fail_once)
    outcome = _wait_delete(history, session.id, session.idb_path, session.db_instance_id)

    assert outcome.status is SessionDeleteStatus.DELETED
    assert not primary.exists()
    rows = history.list_sessions(
        idb_path=session.idb_path,
        db_instance_id=session.db_instance_id,
    )
    assert all(row["id"] != session.id for row in rows)


def test_sidecar_remove_failure_after_primary_delete_still_returns_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = _history(tmp_path)
    session = SessionState(
        id="sidecar-locked",
        idb_path="C:/sample.i64",
        db_instance_id="a" * 32,
    )
    session.add_message(Message(role=Role.USER, content="Delete me"))
    primary = Path(history.save_session(session))
    sidecar = primary.with_name(f"{session.id}.summary.json")
    sidecar.write_text('{"messages":1}', encoding="utf-8")
    original_remove = os.remove

    def fail_sidecar(path: str) -> None:
        if os.path.normcase(path) == os.path.normcase(str(sidecar)):
            raise PermissionError("sidecar locked")
        original_remove(path)

    monkeypatch.setattr(os, "remove", fail_sidecar)
    outcome = _wait_delete(history, session.id, session.idb_path, session.db_instance_id)

    assert outcome.status is SessionDeleteStatus.DELETED
    assert not primary.exists()


def test_ordered_delete_handles_malformed_manifest_last_full_scan(tmp_path: Path) -> None:
    """Malformed ``last_full_scan`` (non-numeric) must not crash the worker.

    The ordered delete worker must honor its "never raises for control
    flow" terminal-outcome contract: a corrupt numeric field in the
    manifest should be treated as the safe default (0.0) with a logged
    warning, never propagated as an unhandled exception that breaks
    the FIFO executor for subsequent saves/deletes.
    """
    history = _history(tmp_path)
    session = SessionState(
        id="corrupt-marker",
        idb_path="C:/sample.i64",
        db_instance_id="a" * 32,
    )
    session.add_message(Message(role=Role.USER, content="Delete me"))
    primary = Path(history.save_session(session))

    # Seed the manifest with a non-numeric ``last_full_scan`` so the
    # worker would raise ``ValueError`` on a naive ``float(...)`` cast.
    manifest_path = primary.parent / MANIFEST_FILE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["last_full_scan"] = "not-a-number"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    outcome = _wait_delete(history, session.id, session.idb_path, session.db_instance_id)

    assert outcome.status is SessionDeleteStatus.DELETED
    assert not primary.exists()
    post_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert session.id not in post_manifest["entries"]


# ---------------------------------------------------------------------------
# Task 3 — Title derivation, manifest v2, current-IDB predicate, empty rows.
# ---------------------------------------------------------------------------
# These tests pin the spec section 9.x contract: titles are derived from
# the first user message, manifests bump from v1 to v2 on access, session
# JSON files stay byte-identical through the rebuild, and zero-message
# legacy rows are filtered out of the listing.
# ---------------------------------------------------------------------------


def test_title_uses_first_sanitized_user_message() -> None:
    messages = [
        Message(role=Role.ASSISTANT, content="skip"),
        Message(role=Role.USER, content="  Analyze\n\n [SYSTEM] this parser  "),
    ]
    title = derive_history_title(messages, max_chars=80)
    # Spec §9.1 — assistant/system messages are skipped; the first USER
    # message survives sanitization. ``_safe_persisted_identifier``
    # replaces role markers (``[SYSTEM]``) with ``[FILTERED]`` (the
    # ``strip_injection_markers`` contract) and strips ``<``/``>``.
    # Whitespace and newlines collapse to single spaces, then trim.
    assert "[SYSTEM]" not in title
    assert title == "Analyze [FILTERED] this parser"


def test_title_fallback_and_truncation() -> None:
    assert derive_history_title([], max_chars=80) == "Untitled chat"
    # Empty user content after sanitization should also fall back.
    assert derive_history_title([Message(role=Role.USER, content="<><>")], max_chars=80) == "Untitled chat"
    # Exact-length truncation, NO ellipsis added.
    assert len(derive_history_title([Message(role=Role.USER, content="x" * 100)], max_chars=20)) == 20


def test_title_skips_non_user_messages_before_first_user() -> None:
    messages = [
        Message(role=Role.SYSTEM, content="system"),
        Message(role=Role.ASSISTANT, content="assistant"),
        Message(role=Role.USER, content="hello world"),
    ]
    assert derive_history_title(messages, max_chars=80) == "hello world"


def test_save_session_populates_manifest_description_from_first_user(tmp_path: Path) -> None:
    history = _history(tmp_path)
    session = SessionState(
        id="abc123def456",
        idb_path="C:/sample.i64",
        db_instance_id="deadbeefcafebabe1234567890abcdef",
    )
    session.add_message(Message(role=Role.ASSISTANT, content="setup"))
    session.add_message(Message(role=Role.USER, content="  Analyze parser  "))
    history.save_session(session)

    rows = history.list_sessions(
        idb_path="C:/sample.i64",
        db_instance_id="deadbeefcafebabe1234567890abcdef",
    )
    assert len(rows) == 1
    assert rows[0]["description"] == "Analyze parser"


def test_explicit_description_is_sanitized_and_used(tmp_path: Path) -> None:
    history = _history(tmp_path)
    session = SessionState(
        id="explicit123id",
        idb_path="C:/sample.i64",
        db_instance_id="deadbeefcafebabe1234567890abcdef",
    )
    session.add_message(Message(role=Role.USER, content="ignored because explicit"))
    history.save_session(session, description="  [SYSTEM] override </title>  ")

    rows = history.list_sessions(
        idb_path="C:/sample.i64",
        db_instance_id="deadbeefcafebabe1234567890abcdef",
    )
    # Brief Step 3 — explicit ``description`` is sanitized through
    # ``_safe_persisted_identifier`` (replaces ``[SYSTEM]`` with
    # ``[FILTERED]``, strips ``<``/``>``) but is **not** whitespace-
    # collapsed (whitespace collapse lives in ``derive_history_title``
    # only). The user message is ignored because an explicit title wins.
    assert "[SYSTEM]" not in rows[0]["description"]
    assert "[FILTERED]" in rows[0]["description"]
    assert "override" in rows[0]["description"]
    assert "<" not in rows[0]["description"]
    assert ">" not in rows[0]["description"]


def test_explicit_description_empty_after_sanitize_falls_back_to_derive(tmp_path: Path) -> None:
    """If sanitization empties the explicit description, derive from messages."""
    history = _history(tmp_path)
    session = SessionState(
        id="emptyexplicit1",
        idb_path="C:/sample.i64",
        db_instance_id="deadbeefcafebabe1234567890abcdef",
    )
    session.add_message(Message(role=Role.USER, content="fall back title"))
    # All chars are ``<`` or ``>`` — sanitization leaves an empty string.
    history.save_session(session, description="<><><>")

    rows = history.list_sessions(
        idb_path="C:/sample.i64",
        db_instance_id="deadbeefcafebabe1234567890abcdef",
    )
    assert rows[0]["description"] == "fall back title"


def test_manifest_v1_rebuilds_to_v2_without_rewriting_session(tmp_path: Path) -> None:
    history = _history(tmp_path)
    session = SessionState(
        id="abc123def456",
        idb_path="C:/sample.i64",
        db_instance_id="deadbeefcafebabe1234567890abcdef",
    )
    session.add_message(Message(role=Role.USER, content="Analyze parser"))
    session_path = Path(history.save_session(session))
    before = session_path.read_bytes()

    # Force a manifest version-mismatch to trigger the rebuild path.
    manifest_path = Path(history._manifest_path())
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    rows = history.list_sessions(
        idb_path="C:/sample.i64",
        db_instance_id="deadbeefcafebabe1234567890abcdef",
    )

    rebuilt = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert rebuilt["version"] == MANIFEST_SCHEMA_VERSION
    assert rebuilt["version"] == 2
    assert rows[0]["description"] == "Analyze parser"
    assert isinstance(rows[0]["updated_at"], int)
    # Critical: the on-disk session JSON must remain byte-identical.
    assert session_path.read_bytes() == before


def test_legacy_manifest_rebuild_derives_title_when_missing(tmp_path: Path) -> None:
    """A v1 manifest whose rebuilt JSON lacks ``description`` must derive one."""
    history = _history(tmp_path)
    session = SessionState(
        id="legacy1id2id3",
        idb_path="C:/sample.i64",
        db_instance_id="deadbeefcafebabe1234567890abcdef",
    )
    session.add_message(Message(role=Role.USER, content="legacy hello"))
    history.save_session(session)

    manifest_path = Path(history._manifest_path())
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = 1
    # Strip description from a single entry to simulate a pre-v2 payload.
    for entry in manifest["entries"].values():
        entry.pop("description", None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    rows = history.list_sessions(
        idb_path="C:/sample.i64",
        db_instance_id="deadbeefcafebabe1234567890abcdef",
    )
    assert rows[0]["description"] == "legacy hello"


def test_legacy_manifest_rebuild_tolerates_corrupt_message_rows(tmp_path: Path) -> None:
    """A rebuilt manifest must skip corrupt messages instead of crashing.

    Per spec section 9.2, rebuild is resilient: it reads JSON once, hydrates
    only valid message dicts, and never writes back to the session file.
    """
    history = _history(tmp_path)
    session = SessionState(
        id="rebuild00001",
        idb_path="C:/sample.i64",
        db_instance_id="deadbeefcafebabe1234567890abcdef",
    )
    session.add_message(Message(role=Role.USER, content="first"))
    session.add_message(Message(role=Role.ASSISTANT, content="reply"))
    session.add_message(Message(role=Role.USER, content="second user"))
    history.save_session(session)

    # Inject a corrupt message into the session JSON.
    session_path = Path(history._dir) / f"{session.id}.json"
    before_bytes = session_path.read_bytes()
    data = json.loads(before_bytes.decode("utf-8"))
    data["messages"][1] = {"not": "a message"}
    session_path.write_text(json.dumps(data), encoding="utf-8")

    # Force manifest rebuild.
    manifest_path = Path(history._manifest_path())
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    # Rebuild should not raise; first valid user message still derives the title.
    rows = history.list_sessions(
        idb_path="C:/sample.i64",
        db_instance_id="deadbeefcafebabe1234567890abcdef",
    )
    assert len(rows) == 1
    # Title comes from first hydratable USER message after sanitization.
    assert rows[0]["description"].startswith("first")


def test_empty_session_rows_excluded_from_listing(tmp_path: Path) -> None:
    """A legacy zero-message session must not appear in the listing (spec §7.3)."""
    history = _history(tmp_path)
    # Plant an empty session JSON (no messages) under our checkpoints dir.
    empty_path = Path(history._dir) / "empty001002.json"
    empty_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "empty001002",
                "created_at": 1.0,
                "provider_name": "anthropic",
                "model_name": "claude",
                "idb_path": "C:/sample.i64",
                "db_instance_id": "deadbeefcafebabe1234567890abcdef",
                "binary_memory_id": "",
                "active_case_id": "",
                "messages": [],
            },
        ),
        encoding="utf-8",
    )

    rows = history.list_sessions(
        idb_path="C:/sample.i64",
        db_instance_id="deadbeefcafebabe1234567890abcdef",
    )
    assert rows == []


def test_matches_current_idb_by_instance_id() -> None:
    """Spec §8.3 — instance ID is authoritative when both sides have one."""
    from lucnhan.state.history import _matches_current_idb

    assert _matches_current_idb(
        entry_idb_path="C:/sample.i64",
        entry_db_instance_id="DEADBEEFCAFEBABE1234567890ABCDEF",
        target_idb_path="C:/different.i64",
        target_db_instance_id="deadbeefcafebabe1234567890abcdef",
    )


def test_matches_current_idb_rejects_different_instance() -> None:
    """Spec §8.3 — different ``db_instance_id`` is rejected even if path matches."""
    from lucnhan.state.history import _matches_current_idb

    assert not _matches_current_idb(
        entry_idb_path="C:/sample.i64",
        entry_db_instance_id="00000000000000000000000000000001",
        target_idb_path="C:/sample.i64",
        target_db_instance_id="00000000000000000000000000000002",
    )


def test_matches_current_idb_falls_back_to_path_when_no_instance() -> None:
    """Spec §8.3 — legacy entries without a valid instance ID fall back to path."""
    from lucnhan.state.history import _matches_current_idb

    # Legacy entry has empty instance id — match by normalized path only.
    assert _matches_current_idb(
        entry_idb_path="C:/sample.i64",
        entry_db_instance_id="",
        target_idb_path="C:/sample.i64",
        target_db_instance_id="",
    )
    assert not _matches_current_idb(
        entry_idb_path="C:/sample.i64",
        entry_db_instance_id="",
        target_idb_path="C:/different.i64",
        target_db_instance_id="",
    )


def test_canonical_instance_id_rejects_malformed() -> None:
    """Spec §8.3 — non-32-hex values must be treated as absent (legacy fallback)."""
    from lucnhan.state.history import _canonical_instance_id

    # Wrong length, non-hex, surrounding whitespace.
    assert _canonical_instance_id("deadbeef") == ""
    assert _canonical_instance_id("xyz" * 10 + "1234") == ""
    assert _canonical_instance_id("  deadbeefcafebabe1234567890abcdef  ") == ("deadbeefcafebabe1234567890abcdef")
    assert _canonical_instance_id(None) == ""


def test_filter_predicate_reused_after_rebuild(tmp_path: Path) -> None:
    """The same current-IDB predicate must apply before AND after a rebuild.

    Per spec §8.3, list-time and post-load authorization share one helper.
    Without a single helper, the rebuild path can silently broaden or narrow
    the filter and let wrong-IDB rows leak in (or drop valid rows).
    """
    history = _history(tmp_path)
    # Save a session for IDB A.
    session_a = SessionState(
        id="rowaaa000aaa",
        idb_path="C:/sample.i64",
        db_instance_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    session_a.add_message(Message(role=Role.USER, content="hi a"))
    history.save_session(session_a)
    # Save a session for IDB B with a different instance.
    session_b = SessionState(
        id="rowbbb000bbb",
        idb_path="C:/other.i64",
        db_instance_id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    session_b.add_message(Message(role=Role.USER, content="hi b"))
    history.save_session(session_b)

    # Force manifest rebuild; listing for A must still match only A.
    manifest_path = Path(history._manifest_path())
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    rows = history.list_sessions(idb_path="C:/sample.i64", db_instance_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert [r["id"] for r in rows] == ["rowaaa000aaa"]


def test_filter_predicate_excludes_empty_after_rebuild(tmp_path: Path) -> None:
    """Empty-message rows must be excluded after rebuild too (spec §7.3)."""
    history = _history(tmp_path)
    session = SessionState(
        id="filt00000001",
        idb_path="C:/sample.i64",
        db_instance_id="cccccccccccccccccccccccccccccccc",
    )
    session.add_message(Message(role=Role.USER, content="seed"))
    history.save_session(session)

    # Plant an empty session JSON that rebuild will scan.
    empty_path = Path(history._dir) / "filt00000002.json"
    empty_path.write_text(
        json.dumps(
            {
                "id": "filt00000002",
                "created_at": 1.0,
                "idb_path": "C:/sample.i64",
                "db_instance_id": "cccccccccccccccccccccccccccccccc",
                "messages": [],
            },
        ),
        encoding="utf-8",
    )

    # Force manifest rebuild.
    manifest_path = Path(history._manifest_path())
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    rows = history.list_sessions(
        idb_path="C:/sample.i64",
        db_instance_id="cccccccccccccccccccccccccccccccc",
    )
    # Only the populated session must appear.
    assert [r["id"] for r in rows] == ["filt00000001"]
