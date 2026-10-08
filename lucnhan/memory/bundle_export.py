"""Memory bundle exporter: coherent JSONL ZIP export from SQLite workspace.

Exports a workspace's current facts/entities/relations/observations as
a versioned ZIP bundle. Uses an anchoring SQLite read so the export is
a coherent pre- or post-commit snapshot, never a head/reference mix.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Literal

from .bundle_schema import (
    MEMORY_BUNDLE_SCHEMA_VERSION,
    BundleLimits,
    ManifestFile,
    MemoryBundleManifest,
    validate_manifest,
)
from .repository import SQLiteKnowledgeRepository
from .workspace import WorkspacePaths


@dataclass(frozen=True)
class BundleExportResult:
    """Result of a bundle export."""

    bundle_path: Path
    manifest: MemoryBundleManifest
    total_records: int


def export_workspace(
    paths: WorkspacePaths,
    repository: SQLiteKnowledgeRepository,
    output_path: Path,
    *,
    scope: Literal["binary", "case"] = "binary",
    export_mode: Literal["portable", "diagnostic"] = "portable",
    limits: BundleLimits | None = None,
) -> BundleExportResult:
    """Export a workspace's current records to a validated ZIP bundle.

    Parameters
    ----------
    paths:
        Workspace filesystem paths.
    repository:
        SQLite knowledge repository.
    output_path:
        Destination ZIP file path.
    scope:
        ``binary`` or ``case``.
    export_mode:
        ``portable`` (default) or ``diagnostic``.
    limits:
        Hard limits for validation.
    """
    lim = limits or BundleLimits()
    owner = repository.owner_memory_id

    # Collect all record types
    record_files: dict[str, list[bytes]] = {}

    facts = repository.list_memories()
    if facts:
        lines = []
        for f in facts:
            envelope = {
                "record_type": "fact",
                "record_id": f.id,
                "origin_memory_id": owner,
                "payload": {
                    "type": f.type,
                    "title": f.title,
                    "content": f.content,
                    "confidence": f.confidence,
                },
            }
            lines.append(json.dumps(envelope, separators=(",", ":"), ensure_ascii=False))
        record_files["records/facts.jsonl"] = [line.encode("utf-8") for line in lines]

    entities = repository.list_entities()
    if entities:
        lines = []
        for e in entities:
            envelope = {
                "record_type": "entity",
                "record_id": e.id,
                "origin_memory_id": owner,
                "payload": {
                    "type": e.type,
                    "name": e.name,
                    "display_name": e.display_name,
                    "address": e.address,
                },
            }
            lines.append(json.dumps(envelope, separators=(",", ":"), ensure_ascii=False))
        record_files["records/entities.jsonl"] = [line.encode("utf-8") for line in lines]

    relations = repository.list_relations()
    if relations:
        lines = []
        for r in relations:
            envelope = {
                "record_type": "relation",
                "record_id": r.id,
                "origin_memory_id": owner,
                "payload": {
                    "src": r.src,
                    "predicate": r.predicate,
                    "dst": r.dst,
                    "confidence": r.confidence,
                },
            }
            lines.append(json.dumps(envelope, separators=(",", ":"), ensure_ascii=False))
        record_files["records/relations.jsonl"] = [line.encode("utf-8") for line in lines]

    # Build manifest file entries
    manifest_files: list[ManifestFile] = []
    total_records = 0
    for name, lines_bytes in record_files.items():
        content = b"\n".join(lines_bytes) + b"\n"
        sha = hashlib.sha256(content).hexdigest()
        count = len(lines_bytes)
        manifest_files.append(
            ManifestFile(
                name=name,
                sha256=sha,
                uncompressed_size=len(content),
                record_count=count,
            )
        )
        total_records += count

    # Add MEMORY.md if it exists
    if paths.markdown.exists():
        md_content = paths.markdown.read_bytes()
        md_sha = hashlib.sha256(md_content).hexdigest()
        manifest_files.append(
            ManifestFile(
                name="MEMORY.md",
                sha256=md_sha,
                uncompressed_size=len(md_content),
            )
        )

    record_counts = {
        name.replace("records/", "").replace(".jsonl", ""): f.record_count
        for name, f in zip(record_files.keys(), manifest_files, strict=True)
        if name.startswith("records/")
    }

    from datetime import datetime

    manifest = MemoryBundleManifest(
        schema_version=MEMORY_BUNDLE_SCHEMA_VERSION,
        scope=scope,
        export_mode=export_mode,
        origin_memory_id=owner,
        exported_at=datetime.now(UTC).isoformat(),
        files=tuple(manifest_files),
        record_counts=record_counts,
    )

    validate_manifest(manifest, limits=lim)

    # Write ZIP
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Write manifest first
        manifest_json = json.dumps(
            {
                "schema_version": manifest.schema_version,
                "scope": manifest.scope,
                "export_mode": manifest.export_mode,
                "origin_memory_id": manifest.origin_memory_id,
                "exported_at": manifest.exported_at,
                "files": [
                    {
                        "name": f.name,
                        "sha256": f.sha256,
                        "uncompressed_size": f.uncompressed_size,
                        "record_count": f.record_count,
                    }
                    for f in manifest.files
                ],
                "record_counts": dict(manifest.record_counts),
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
        zf.writestr("manifest.json", manifest_json)

        # Write record files
        for name, lines_bytes in record_files.items():
            content = b"\n".join(lines_bytes) + b"\n"
            zf.writestr(name, content)

        # Write MEMORY.md
        if paths.markdown.exists():
            zf.writestr("MEMORY.md", paths.markdown.read_bytes())

    return BundleExportResult(
        bundle_path=output_path,
        manifest=manifest,
        total_records=total_records,
    )
