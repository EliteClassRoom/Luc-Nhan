"""Versioned bundle schema for memory interchange.

Defines the frozen dataclasses and limits for the ZIP/JSONL bundle format.
A bundle is a ZIP containing ``manifest.json``, ``records/*.jsonl``,
``MEMORY.md``, and optional ``notes/**``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

MEMORY_BUNDLE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ManifestFile:
    """One file entry in a bundle manifest."""

    name: str
    sha256: str
    uncompressed_size: int
    record_count: int = 0


@dataclass(frozen=True)
class BundleLimits:
    """Hard limits for bundle validation."""

    max_compressed_bytes: int = 100 * 1024 * 1024  # 100 MiB
    max_uncompressed_bytes: int = 500 * 1024 * 1024  # 500 MiB
    max_records: int = 100_000
    max_jsonl_line_bytes: int = 1024 * 1024  # 1 MiB
    max_files: int = 10_000


@dataclass(frozen=True)
class BundleRecordEnvelope:
    """One JSONL record envelope in a bundle."""

    record_type: str
    record_id: str
    origin_memory_id: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class MemoryBundleManifest:
    """Manifest for a memory bundle."""

    schema_version: int
    scope: Literal["binary", "case"]
    export_mode: Literal["portable", "diagnostic"]
    origin_memory_id: str
    exported_at: str
    files: tuple[ManifestFile, ...]
    record_counts: Mapping[str, int]


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MEMBER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def validate_member_name(name: str) -> str:
    """Validate a ZIP member name. Raises ``ValueError`` on traversal/unsafe paths."""
    if not name or not isinstance(name, str):
        raise ValueError("empty member name")
    if "\\" in name:
        raise ValueError(f"backslash in member name: {name!r}")
    if name.startswith("/"):
        raise ValueError(f"absolute member name: {name!r}")
    if ".." in name.split("/"):
        raise ValueError(f"traversal in member name: {name!r}")
    if "\x00" in name:
        raise ValueError(f"NUL in member name: {name!r}")
    if not _MEMBER_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid member name: {name!r}")
    return name


def validate_manifest(manifest: MemoryBundleManifest, *, limits: BundleLimits | None = None) -> None:
    """Validate manifest invariants. Raises ``ValueError`` on any violation."""
    lim = limits or BundleLimits()

    if manifest.schema_version != MEMORY_BUNDLE_SCHEMA_VERSION:
        raise ValueError(f"unsupported schema version: {manifest.schema_version}")
    if manifest.scope not in ("binary", "case"):
        raise ValueError(f"invalid scope: {manifest.scope!r}")
    if manifest.export_mode not in ("portable", "diagnostic"):
        raise ValueError(f"invalid export_mode: {manifest.export_mode!r}")
    if not manifest.origin_memory_id:
        raise ValueError("empty origin_memory_id")

    # Validate files
    seen_names: set[str] = set()
    total_uncompressed = 0
    total_records = 0
    total_files = len(manifest.files)

    if total_files > lim.max_files:
        raise ValueError(f"too many files: {total_files} > {lim.max_files}")

    for f in manifest.files:
        validate_member_name(f.name)
        if f.name in seen_names:
            raise ValueError(f"duplicate member name: {f.name!r}")
        seen_names.add(f.name)
        if not _SHA256_RE.fullmatch(f.sha256):
            raise ValueError(f"invalid sha256 for {f.name}: {f.sha256!r}")
        if f.uncompressed_size < 0:
            raise ValueError(f"negative size for {f.name}")
        total_uncompressed += f.uncompressed_size
        total_records += f.record_count

    if total_uncompressed > lim.max_uncompressed_bytes:
        raise ValueError(f"uncompressed size {total_uncompressed} exceeds {lim.max_uncompressed_bytes}")
    if total_records > lim.max_records:
        raise ValueError(f"record count {total_records} exceeds {lim.max_records}")
