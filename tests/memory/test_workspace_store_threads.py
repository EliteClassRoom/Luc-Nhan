"""Concurrent access to one WorkspaceStore must serialize without
``started a transaction within a transaction`` / ``cannot rollback`` errors.

The store's single long-lived connection is shared between the agent
background thread (writes via save_memory/retrieval) and the Qt main
thread (knowledge-panel refresh reads via the same service). Every
public method that touches the connection must serialize on the store
lock, and transactions must run atomically inside that lock.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from rikugan.memory.fact_identity import semantic_fact_hash
from rikugan.memory.workspace import MemoryLocator, new_memory_id, new_record_id
from rikugan.memory.workspace_store import WorkspaceStore

_ROUNDS = 40
_JOIN_TIMEOUT = 60.0


def _create_store(tmp_path: Path) -> WorkspaceStore:
    memory_id = new_memory_id()
    paths = MemoryLocator(tmp_path).binary(memory_id)
    return WorkspaceStore.create(paths, owner_memory_id=memory_id)


def test_concurrent_writers_and_readers_no_sqlite_errors(tmp_path: Path) -> None:
    """Writers and readers hammering one store must not surface SQLite
    threading errors (BEGIN inside a transaction, commit/rollback races)."""
    store = _create_store(tmp_path)
    errors: list[str] = []
    barrier = threading.Barrier(5)  # 2 put_fact writers + 1 semantic writer + 2 readers

    def writer(tag: str) -> None:
        barrier.wait()
        for i in range(_ROUNDS):
            try:
                store.put_fact(
                    new_record_id("fact"),
                    "fact",
                    f"{tag}-{i}",
                    f"content from {tag} round {i}",
                    0.5,
                    expected_revision=0,
                )
            # Collecting failures is the point of the test.
            except Exception as e:
                errors.append(f"writer:{tag}:{i}:{e}")

    def semantic_writer(tag: str) -> None:
        barrier.wait()
        for i in range(_ROUNDS):
            content = f"semantic {tag} {i}"
            try:
                store.save_fact_if_semantically_absent(
                    fact_id=new_record_id("fact"),
                    fact_type="fact",
                    title=f"{tag}-{i}",
                    content=content,
                    semantic_hash=semantic_fact_hash("fact", content),
                    confidence=0.5,
                    observation_id=new_record_id("observation"),
                    observation_type="save_memory",
                    observation_payload="{}",
                )
            except Exception as e:
                errors.append(f"semantic:{tag}:{i}:{e}")

    def reader() -> None:
        barrier.wait()
        for i in range(_ROUNDS):
            try:
                store.list_facts()
                store.list_entities()
                store.list_relations()
                store.count_observations()
                store.projection_state()
                store.get_fact("fact-does-not-exist")
            except Exception as e:
                errors.append(f"reader:{i}:{e}")

    threads = [
        threading.Thread(target=writer, args=("w0",)),
        threading.Thread(target=writer, args=("w1",)),
        threading.Thread(target=semantic_writer, args=("s0",)),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_JOIN_TIMEOUT)
        assert not t.is_alive(), "worker thread did not finish — possible deadlock"

    assert not errors, f"concurrent access produced errors: {errors[:5]}"
    store.close()


def test_rolled_back_fact_never_visible_to_concurrent_reader(tmp_path: Path) -> None:
    """A fact whose save fails mid-transaction must never be observable,
    even by a reader polling on another thread during the writes."""
    store = _create_store(tmp_path)

    # Seed one good save with a known observation id.
    good_obs = new_record_id("observation")
    _record, outcome = store.save_fact_if_semantically_absent(
        fact_id=new_record_id("fact"),
        fact_type="fact",
        title="good",
        content="good fact content",
        semantic_hash=semantic_fact_hash("fact", "good fact content"),
        confidence=0.5,
        observation_id=good_obs,
        observation_type="save_memory",
        observation_payload="{}",
    )
    assert outcome == "created"

    doomed_id = new_record_id("fact")
    doomed_content = "doomed fact content"
    # Reusing *good_obs* makes the final observation INSERT fail with an
    # IntegrityError *after* the fact + revision inserts — a genuine
    # mid-transaction failure whose rollback must hide the fact entirely.
    errors: list[str] = []
    dirty_reads: list[str] = []
    stop = threading.Event()

    def failing_writer() -> None:
        for _ in range(25):
            try:
                store.save_fact_if_semantically_absent(
                    fact_id=doomed_id,
                    fact_type="fact",
                    title="doomed",
                    content=doomed_content,
                    semantic_hash=semantic_fact_hash("fact", doomed_content),
                    confidence=0.5,
                    observation_id=good_obs,  # PK collision → forced rollback
                    observation_type="save_memory",
                    observation_payload="{}",
                )
            except sqlite3.IntegrityError:
                pass  # expected: mid-transaction failure triggers rollback
            except Exception as e:
                errors.append(f"writer:{e}")

    def reader() -> None:
        while not stop.is_set():
            try:
                fact = store.get_fact(doomed_id)
            except Exception as e:
                errors.append(f"reader:{e}")
                continue
            if fact is not None:
                dirty_reads.append(fact.content)

    writer_thread = threading.Thread(target=failing_writer)
    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    writer_thread.start()
    writer_thread.join(timeout=_JOIN_TIMEOUT)
    stop.set()
    reader_thread.join(timeout=_JOIN_TIMEOUT)
    assert not writer_thread.is_alive(), "writer thread did not finish"
    assert not reader_thread.is_alive(), "reader thread did not finish"

    assert not errors, f"unexpected errors: {errors[:5]}"
    assert not dirty_reads, "reader observed a rolled-back fact"
    assert store.get_fact(doomed_id) is None
    assert store.count_observations() == 1
    store.close()
