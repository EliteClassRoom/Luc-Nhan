"""Regression tests for session token-counter persistence.

Background
----------
``SessionState`` carries two cumulative counters that drive the UI:

  * ``last_prompt_tokens`` — the prompt size at the last turn, used by
    :class:`rikugan.ui.context_bar.ContextBar` to paint the context
    window usage percentage on every turn.
  * ``total_usage`` — a :class:`rikugan.core.types.TokenUsage` aggregate
    that drives cumulative spend / cost reporting across a session's
    lifetime.

Pre-fix, ``SessionHistory.save_session`` built the on-disk payload
without ever writing either field, and ``SessionHistory.load_session``
built the restored ``SessionState`` without ever reading them. Opening
a saved chat therefore re-attached to a fresh ``SessionState`` whose
counters were zero — the context bar dropped to 0% and cumulative
spend reset to zero, even though the in-memory counters had been
bumped on every turn.

These tests pin the save → load round-trip and the load-only edge
cases the fix has to keep safe (legacy JSON written before the keys
existed; hostile values for those keys written by a buggy upstream
tool or a hand edit). Persisted JSON is attacker-influenced (binary-
derived content per spec §11.3) so every restored value must be
treated as hostile.
"""

from __future__ import annotations

import json
from pathlib import Path

from rikugan.core.config import RikuganConfig
from rikugan.core.types import Message, Role, TokenUsage
from rikugan.state.history import SessionHistory
from rikugan.state.session import SessionState


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _history(tmp_path: Path) -> SessionHistory:
    """Build a SessionHistory whose storage dir is rooted in *tmp_path*.

    Mirrors the convention in ``tests/state/test_history_on_demand.py``
    and ``rikugan/tests/test_session_restore_sanitization.py``: assign
    ``_config_dir`` on a default ``RikuganConfig`` and then construct
    the history. ``RikuganConfig.checkpoints_dir`` is a computed
    property so we cannot assign it directly.
    """
    config = RikuganConfig()
    config._config_dir = str(tmp_path)
    return SessionHistory(config)


def _base_payload(session_id: str, messages: list[dict], **extra: object) -> dict:
    """Build a minimal valid session JSON payload.

    Mirrors ``_base_payload`` in
    ``rikugan/tests/test_session_restore_sanitization.py`` so the
    hand-forged legacy / hostile cases below match the shape
    ``save_session`` would write, minus the fields under test.
    """
    payload: dict = {
        "schema_version": 1,
        "id": session_id,
        "created_at": 0,
        "provider_name": "test",
        "model_name": "test",
        "idb_path": "",
        "db_instance_id": "",
        "current_turn": 0,
        "metadata": {},
        "messages": messages,
    }
    payload.update(extra)
    return payload


def _write_session(history: SessionHistory, session_id: str, payload: dict) -> Path:
    """Persist a forged session JSON file with explicit UTF-8.

    UTF-8 matters here because persisted files are loaded with the
    same encoding and a stray non-ASCII byte in a forged payload
    would otherwise raise ``UnicodeDecodeError``.
    """
    path = Path(history._dir) / f"{session_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. Round-trip preservation — the core regression.
# ---------------------------------------------------------------------------


def test_save_session_persists_last_prompt_tokens(tmp_path: Path) -> None:
    """``last_prompt_tokens`` must survive save → load exactly.

    The pre-fix code omitted the key entirely; the round-trip therefore
    came back as 0. The fix writes it under the same key
    ``current_turn`` already used and restores it on load.
    """
    history = _history(tmp_path)
    session = SessionState(
        id="roundtrip001",
        idb_path="C:/samples/a.i64",
        provider_name="anthropic",
        model_name="claude",
    )
    session.add_message(Message(role=Role.USER, content="hi"))

    session.last_prompt_tokens = 13000
    history.save_session(session)

    loaded = history.load_session(session.id)
    assert loaded is not None
    assert loaded.last_prompt_tokens == 13000, (
        "last_prompt_tokens must round-trip through save_session/load_session; "
        "pre-fix it silently dropped to 0"
    )


def test_save_session_persists_total_usage_with_distinct_fields(tmp_path: Path) -> None:
    """``total_usage`` must survive save → load with every field intact.

    Each field is set to a distinct, non-zero value so a field-swap
    bug (e.g. assignment in the wrong order) cannot accidentally produce
    a passing test. The pre-fix code omitted ``total_usage`` entirely,
    so the loaded aggregate came back as all-zeros — matching
    ``TokenUsage()`` exactly. Asserting on every field therefore catches
    both the omission and any partial write.
    """
    history = _history(tmp_path)
    session = SessionState(
        id="roundtrip002",
        idb_path="C:/samples/a.i64",
        provider_name="anthropic",
        model_name="claude",
    )
    # ``record_usage`` is the canonical helper for populating total_usage
    # in live code; it routes through ``_record_usage_locked`` which adds
    # to ``total_usage`` and bumps ``last_prompt_tokens`` only when the
    # incoming prompt is positive. To keep ``last_prompt_tokens``
    # untouched in this test we drive ``total_usage`` via direct
    # dataclass field assignment on the freshly-built ``TokenUsage``
    # (mirroring how ``SessionState`` is constructed internally).
    session.total_usage = TokenUsage(
        prompt_tokens=12000,
        completion_tokens=800,
        total_tokens=12800,
        cache_read_tokens=900,
        cache_creation_tokens=100,
    )
    # The fixture above uses a distinct value per slot so a swap would
    # visibly fail. ``0`` would be ambiguous with the pre-fix all-zero
    # default that ``TokenUsage()`` produces.
    assert session.total_usage.prompt_tokens == 12000
    assert session.total_usage.completion_tokens == 800
    assert session.total_usage.total_tokens == 12800
    assert session.total_usage.cache_read_tokens == 900
    assert session.total_usage.cache_creation_tokens == 100

    session.add_message(Message(role=Role.USER, content="hi"))
    history.save_session(session)

    loaded = history.load_session(session.id)
    assert loaded is not None
    usage = loaded.total_usage
    assert usage.prompt_tokens == 12000
    assert usage.completion_tokens == 800
    assert usage.total_tokens == 12800
    assert usage.cache_read_tokens == 900
    assert usage.cache_creation_tokens == 100


def test_save_session_persists_both_counters_together(tmp_path: Path) -> None:
    """End-to-end: ``record_usage`` populates both fields, save → load must preserve both.

    The pre-fix code dropped both to zero on reopen. This test exercises
    the public ``SessionState.record_usage`` path (rather than direct
    field assignment) so any change to that helper — including future
    cache-accounting tweaks — is covered too.
    """
    history = _history(tmp_path)
    session = SessionState(
        id="roundtrip003",
        idb_path="C:/samples/a.i64",
        provider_name="anthropic",
        model_name="claude",
    )
    session.add_message(Message(role=Role.USER, content="hi"))
    session.record_usage(
        TokenUsage(
            prompt_tokens=12000,
            completion_tokens=800,
            total_tokens=12800,
            cache_read_tokens=900,
            cache_creation_tokens=100,
        )
    )
    history.save_session(session)

    loaded = history.load_session(session.id)
    assert loaded is not None
    # ``record_usage`` updates ``last_prompt_tokens`` to
    # ``usage.context_tokens`` (= prompt + cache_read + cache_creation).
    expected_last_prompt = 12000 + 900 + 100
    assert loaded.last_prompt_tokens == expected_last_prompt
    assert loaded.total_usage.prompt_tokens == 12000
    assert loaded.total_usage.completion_tokens == 800
    assert loaded.total_usage.total_tokens == 12800
    assert loaded.total_usage.cache_read_tokens == 900
    assert loaded.total_usage.cache_creation_tokens == 100


# ---------------------------------------------------------------------------
# 2. Legacy / missing-key backward compatibility.
# ---------------------------------------------------------------------------


def test_load_session_tolerates_missing_token_keys(tmp_path: Path) -> None:
    """A session JSON written before the fix has no token keys.

    It must still load with zeroed counters and no exception so users
    with pre-fix saves aren't locked out of their history. Mirrors the
    legacy-on-disk contract already proven for ``binary_memory_id`` /
    ``active_case_id`` (``tests/state/test_memory_binding.py``).
    """
    history = _history(tmp_path)
    payload = _base_payload(
        session_id="legacy00001",
        messages=[{"role": "user", "content": "first"}],
        # Intentionally NO ``last_prompt_tokens`` and NO ``total_usage``.
    )
    _write_session(history, "legacy00001", payload)

    loaded = history.load_session("legacy00001")
    assert loaded is not None, "pre-fix session JSON must still load cleanly"
    assert loaded.last_prompt_tokens == 0
    assert loaded.total_usage == TokenUsage()
    # The legacy load must still hydrate messages — the fix must not
    # regress the existing happy path while plumbing in the new keys.
    assert [m.content for m in loaded.messages] == ["first"]


# ---------------------------------------------------------------------------
# 3. Hostile-value coercion at the load boundary.
# ---------------------------------------------------------------------------


def test_load_session_coerces_string_total_usage_to_zeroed(tmp_path: Path) -> None:
    """``total_usage`` written as a plain string must not crash and must
    load as a zeroed :class:`TokenUsage`.

    ``TokenUsage`` itself accepts only ints; a string is not a dict and
    the load path treats anything that isn't a ``dict`` as a hostile
    payload that falls back to ``TokenUsage()``.
    """
    history = _history(tmp_path)
    payload = _base_payload(
        session_id="hostile00001",
        messages=[{"role": "user", "content": "x"}],
        last_prompt_tokens=42,
        total_usage="not a dict",
    )
    _write_session(history, "hostile00001", payload)

    loaded = history.load_session("hostile00001")
    assert loaded is not None
    assert loaded.total_usage == TokenUsage()
    # ``last_prompt_tokens`` is read independently of ``total_usage``
    # so the hostile string there must not affect this field.
    assert loaded.last_prompt_tokens == 42


def test_load_session_coerces_list_total_usage_to_zeroed(tmp_path: Path) -> None:
    """``total_usage`` written as a JSON list must not crash and must
    load as a zeroed :class:`TokenUsage`.

    A list is not a dict either — it must trigger the same fallback.
    """
    history = _history(tmp_path)
    payload = _base_payload(
        session_id="hostile00002",
        messages=[{"role": "user", "content": "x"}],
        last_prompt_tokens=7,
        total_usage=[1, 2, 3],
    )
    _write_session(history, "hostile00002", payload)

    loaded = history.load_session("hostile00002")
    assert loaded is not None
    assert loaded.total_usage == TokenUsage()
    assert loaded.last_prompt_tokens == 7


def test_load_session_coerces_hostile_dict_total_usage_fields(tmp_path: Path) -> None:
    """A dict ``total_usage`` with negative / float / string / None /
    absurdly large fields must round-trip into non-negative sane ints.

    Each hostile field is mapped to a specific expected outcome so the
    test pins ``TokenUsage.__post_init__`` behaviour and so a regression
    that mistakenly passes raw hostile values through would visibly
    fail rather than coincidentally match the coercion.
    """
    history = _history(tmp_path)
    payload = _base_payload(
        session_id="hostile00003",
        messages=[{"role": "user", "content": "x"}],
        last_prompt_tokens=3,
        total_usage={
            # Negative → 0 (coerce_token_count clamps at zero).
            "prompt_tokens": -50,
            # Float → truncated to int (coerce_token_count uses int()).
            "completion_tokens": 4.7,
            # String parses cleanly → its int.
            "total_tokens": "250",
            # None → 0.
            "cache_read_tokens": None,
            # Absurdly large → kept as the int (Python ints are
            # arbitrary precision; the contract is "non-negative", not
            # "fits in 32 bits"). This pins that behaviour so any
            # future "cap at 1<<32" tweak is a deliberate decision.
            "cache_creation_tokens": 10**30,
        },
    )
    _write_session(history, "hostile00003", payload)

    loaded = history.load_session("hostile00003")
    assert loaded is not None
    assert loaded.total_usage.prompt_tokens == 0
    assert loaded.total_usage.completion_tokens == 4
    # total_tokens was "250" and prompt_tokens + completion_tokens
    # equals 0 + 4 = 4, which is NOT 250, so the explicit "250" wins
    # (TokenUsage only derives when total_tokens <= 0).
    assert loaded.total_usage.total_tokens == 250
    assert loaded.total_usage.cache_read_tokens == 0
    assert loaded.total_usage.cache_creation_tokens == 10**30
    # Independent field, untouched by the hostile total_usage.
    assert loaded.last_prompt_tokens == 3


def test_load_session_coerces_hostile_last_prompt_tokens(tmp_path: Path) -> None:
    """``last_prompt_tokens`` written as a string / negative / float
    must coerce to a sane non-negative int without raising.

    A regression that hand-wrote ``int(value)`` without going through
    :func:`coerce_token_count` would raise ``ValueError`` on the
    string and let a negative number through unchanged.
    """
    history = _history(tmp_path)

    # String that parses cleanly → its int.
    s_payload = _base_payload(
        session_id="hostile00004",
        messages=[{"role": "user", "content": "x"}],
        last_prompt_tokens="1234",
        total_usage={"prompt_tokens": 0},
    )
    _write_session(history, "hostile00004", s_payload)
    loaded = history.load_session("hostile00004")
    assert loaded is not None
    assert loaded.last_prompt_tokens == 1234

    # Negative → clamped to 0.
    n_payload = _base_payload(
        session_id="hostile00005",
        messages=[{"role": "user", "content": "x"}],
        last_prompt_tokens=-99,
        total_usage={"prompt_tokens": 0},
    )
    _write_session(history, "hostile00005", n_payload)
    loaded = history.load_session("hostile00005")
    assert loaded is not None
    assert loaded.last_prompt_tokens == 0

    # Float → truncated to int.
    f_payload = _base_payload(
        session_id="hostile00006",
        messages=[{"role": "user", "content": "x"}],
        last_prompt_tokens=9.9,
        total_usage={"prompt_tokens": 0},
    )
    _write_session(history, "hostile00006", f_payload)
    loaded = history.load_session("hostile00006")
    assert loaded is not None
    assert loaded.last_prompt_tokens == 9


# ---------------------------------------------------------------------------
# 4. Partial/zero usage round-trip.
# ---------------------------------------------------------------------------


def test_round_trip_preserves_derived_total_tokens(tmp_path: Path) -> None:
    """``TokenUsage`` derives the total from prompt+completion when total
    is 0. That must round-trip — a session whose last provider response
    omitted ``total_tokens`` must come back with the derived value, not
    silently zero.

    Pre-fix, the entire ``total_usage`` was dropped on disk so the
    derived value was lost even if the in-memory state had it. Post-
    fix, the load path feeds the dict back through ``TokenUsage(...)``
    which re-runs the same derivation.
    """
    history = _history(tmp_path)
    session = SessionState(
        id="partial00001",
        idb_path="C:/samples/a.i64",
        provider_name="anthropic",
        model_name="claude",
    )
    # Only prompt + completion set; total_tokens left at 0 so the
    # dataclass derives total = 12 + 8 = 20.
    session.total_usage = TokenUsage(prompt_tokens=12, completion_tokens=8)
    assert session.total_usage.total_tokens == 20  # derived in __post_init__

    session.add_message(Message(role=Role.USER, content="hi"))
    history.save_session(session)

    # The on-disk payload must record the derived total. If the save
    # path serialized the dataclass before ``__post_init__`` derived
    # the value, the file would store 0 — which is the kind of subtle
    # oversight the regression covers.
    on_disk = json.loads(
        (Path(history._dir) / f"{session.id}.json").read_text(encoding="utf-8")
    )
    assert on_disk["total_usage"]["total_tokens"] == 20
    assert on_disk["total_usage"]["prompt_tokens"] == 12
    assert on_disk["total_usage"]["completion_tokens"] == 8

    loaded = history.load_session(session.id)
    assert loaded is not None
    assert loaded.total_usage.prompt_tokens == 12
    assert loaded.total_usage.completion_tokens == 8
    assert loaded.total_usage.total_tokens == 20