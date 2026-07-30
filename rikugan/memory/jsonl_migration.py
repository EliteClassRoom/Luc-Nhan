"""Adapter converting legacy JSONL knowledge records into bundle envelopes.

The output matches the wire format consumed by ``import_workspace_bundle``
so the durability tranche's idempotent importer can ingest legacy data
without a separate import code path.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .bundle_import import import_workspace_bundle
from .bundle_schema import MEMORY_BUNDLE_SCHEMA_VERSION, ManifestFile
from .paths import KnowledgePaths
from .raw_store import KnowledgeRawStore
from .repository import SQLiteKnowledgeRepository
from .workspace_store import WorkspaceStore

_LEGACY_JSONL_IMPORTED_KEY = "legacy_jsonl_imported"


def maybe_import_legacy_jsonl(
    workspace_store: WorkspaceStore,
    owner_memory_id: str,
    jsonl_paths: KnowledgePaths,
) -> None:
    """Import JSONL records into SQLite once per workspace.

    Idempotent via the ``legacy_jsonl_imported`` marker in ``workspace_meta``.
    JSONL files are never deleted. If the JSONL store has no records the
    marker is still written so the next open is a no-op. Any error
    raised by ``import_workspace_bundle`` propagates and leaves the
    marker unset, so the next open retries the migration.
    """
    row = workspace_store._conn.execute(
        "SELECT value FROM workspace_meta WHERE key = ?",
        (_LEGACY_JSONL_IMPORTED_KEY,),
    ).fetchone()
    if row is not None:
        return

    raw_store = KnowledgeRawStore(jsonl_paths)
    envelopes = jsonl_to_bundle_envelopes(raw_store, jsonl_paths)
    if not envelopes:
        workspace_store._conn.execute(
            "INSERT OR REPLACE INTO workspace_meta(key, value) VALUES(?, ?)",
            (_LEGACY_JSONL_IMPORTED_KEY, datetime.now(UTC).isoformat()),
        )
        workspace_store._conn.commit()
        return

    bundle_path = write_envelopes_to_temp_bundle(envelopes, owner_memory_id)
    try:
        repo = SQLiteKnowledgeRepository(workspace_store, owner_memory_id=owner_memory_id)
        import_workspace_bundle(bundle_path, repo)
    finally:
        bundle_path.unlink(missing_ok=True)

    workspace_store._conn.execute(
        "INSERT OR REPLACE INTO workspace_meta(key, value) VALUES(?, ?)",
        (_LEGACY_JSONL_IMPORTED_KEY, datetime.now(UTC).isoformat()),
    )
    workspace_store._conn.commit()


def jsonl_to_bundle_envelopes(
    store: KnowledgeRawStore,
    paths: KnowledgePaths,  # reserved for future source_refs
) -> list[dict[str, Any]]:
    """Convert all JSONL records into bundle envelope dicts."""
    envelopes: list[dict[str, Any]] = []

    for mem in store.list_memories():
        envelopes.append(
            {
                "record_type": "fact",
                "record_id": mem.id,
                "payload": {
                    "type": mem.type,
                    "title": mem.title,
                    "content": mem.content,
                    "confidence": mem.confidence,
                    "entity_refs": mem.entity_refs,
                    "tags": mem.tags,
                },
            }
        )

    for ent in store.list_entities():
        envelopes.append(
            {
                "record_type": "entity",
                "record_id": ent.id,
                "payload": {
                    "type": ent.type,
                    "name": ent.name,
                    "display_name": ent.display_name,
                    "address": ent.address,
                    "tags": ent.tags,
                },
            }
        )

    for rel in store.list_relations():
        envelopes.append(
            {
                "record_type": "relation",
                "record_id": rel.id,
                "payload": {
                    "src": rel.src,
                    "predicate": rel.predicate,
                    "dst": rel.dst,
                    "confidence": rel.confidence,
                    "evidence": rel.evidence,
                },
            }
        )

    return envelopes


def write_envelopes_to_temp_bundle(
    envelopes: list[dict[str, Any]],
    origin_memory_id: str,
) -> Path:
    """Write envelopes to a temp ZIP bundle matching export_workspace layout."""
    record_files: dict[str, list[bytes]] = {}
    for env in envelopes:
        rtype = env["record_type"]
        fname = f"records/{rtype}s.jsonl"
        line = json.dumps(env, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        record_files.setdefault(fname, []).append(line)

    manifest_files: list[ManifestFile] = []
    for name, lines in record_files.items():
        content = b"\n".join(lines) + b"\n"
        sha = hashlib.sha256(content).hexdigest()
        count = len(lines)
        manifest_files.append(
            ManifestFile(
                name=name,
                sha256=sha,
                uncompressed_size=len(content),
                record_count=count,
            )
        )

    manifest_json = json.dumps(
        {
            "schema_version": MEMORY_BUNDLE_SCHEMA_VERSION,
            "scope": "binary",
            "export_mode": "portable",
            "origin_memory_id": origin_memory_id,
            "exported_at": datetime.now(UTC).isoformat(),
            "files": [
                {
                    "name": f.name,
                    "sha256": f.sha256,
                    "uncompressed_size": f.uncompressed_size,
                    "record_count": f.record_count,
                }
                for f in manifest_files
            ],
            "record_counts": {
                name.replace("records/", "").replace(".jsonl", ""): f.record_count
                for name, f in zip(record_files.keys(), manifest_files, strict=True)
            },
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )

    fd, tmp_path_str = tempfile.mkstemp(prefix="rikugan-jsonl-", suffix=".zip")
    tmp_path = Path(tmp_path_str)
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", manifest_json)
            for name, lines in record_files.items():
                content = b"\n".join(lines) + b"\n"
                zf.writestr(name, content)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path
