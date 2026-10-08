"""Path derivation helpers for the memory subsystem.

Given a binary IDB path (e.g. ``C:/samples/foo.i64``), produce the
canonical sub-directories used by Luc Nhan's knowledge store:

* ``<idb_dir>/notes/``           — human-readable Markdown notes
* ``<idb_dir>/.lucnhan-kb/``     — JSONL machine-facing storage

The filesystem layout is fixed by the plan. ``<idb_dir>`` is the
parent directory of the IDB file, matching how existing code derives
``idb_dir`` for the ``notes/`` directory.

The ``binary_id`` is a stable identifier for the analyzed binary. It is
derived from the IDB path so the same binary reopens with the same
records. Using ``database_instance_id`` from the host (if available) is
preferred for IDA — but falls back to a normalized path.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass

# Folder names. Hidden + dot-prefixed for the JSONL store so it doesn't
# clutter the user's notes view in their editor's tree.
NOTES_DIR_NAME = "notes"
KB_DIR_NAME = ".lucnhan-kb"
#: Spelling used before the plugin rename. Still sitting next to every IDB
#: analysed by an older build; :meth:`KnowledgePaths.ensure` moves it once so
#: the knowledge store does not silently start empty.
LEGACY_KB_DIR_NAME = ".rikugan-kb"
REPORTS_SUBDIR = "reports"


def _adopt_legacy_kb_dir(kb_dir: str) -> None:
    """Move a pre-rename ``.rikugan-kb`` sibling onto *kb_dir*.

    One-way and best effort: runs only while *kb_dir* is still absent, so a
    failed move leaves the old store intact instead of half-migrated.
    """
    legacy = os.path.join(os.path.dirname(kb_dir), LEGACY_KB_DIR_NAME)
    try:
        if not os.path.exists(kb_dir) and os.path.isdir(legacy):
            os.rename(legacy, kb_dir)
    except OSError:
        pass


@dataclass
class KnowledgePaths:
    """Filesystem layout for one binary's knowledge store."""

    idb_path: str  # absolute path to the .i64 / .idb file
    notes_dir: str
    kb_dir: str
    reports_dir: str
    binary_id: str

    # ---- JSONL files ----
    @property
    def memories_path(self) -> str:
        return os.path.join(self.kb_dir, "memories.jsonl")

    @property
    def entities_path(self) -> str:
        return os.path.join(self.kb_dir, "entities.jsonl")

    @property
    def relations_path(self) -> str:
        return os.path.join(self.kb_dir, "relations.jsonl")

    @property
    def observations_path(self) -> str:
        return os.path.join(self.kb_dir, "observations.jsonl")

    @property
    def meta_path(self) -> str:
        return os.path.join(self.kb_dir, "meta.json")

    def ensure(self) -> None:
        """Create notes/, .lucnhan-kb/, and notes/reports/ if missing."""
        _adopt_legacy_kb_dir(self.kb_dir)
        os.makedirs(self.notes_dir, exist_ok=True)
        os.makedirs(self.kb_dir, exist_ok=True)
        os.makedirs(self.reports_dir, exist_ok=True)


def derive_binary_id(idb_path: str, db_instance_id: str = "") -> str:
    """Derive a stable, human-readable identifier for one binary.

    Prefers the host's per-IDB ``database_instance_id`` when supplied
    (IDA stores this in a netnode so the same .i64 file reopened in
    different IDA sessions share an ID). Falls back to a short hash of
    the normalized path so headless / non-IDA contexts still get a
    deterministic value.
    """
    if db_instance_id:
        return db_instance_id
    # Normalize BOTH case and separator style so paths from different
    # working directories or mixed slashes collapse to the same key.
    norm = os.path.normcase(os.path.normpath(os.path.abspath(idb_path)))
    # Forward-slash the input for the hash so Mac/Linux paths with
    # mix-and-match separators also collapse.
    h = hashlib.sha256(norm.replace("\\", "/").encode("utf-8")).hexdigest()[:12]
    # Lowercase basename prefix too — case-insensitive filesystems
    # (NTFS / APFS default) should report identical IDs.
    base = os.path.basename(idb_path).lower()
    base = re.sub(r"[^a-z0-9._-]", "_", base)
    return f"{base}-{h}"


def knowledge_paths(idb_path: str, db_instance_id: str = "") -> KnowledgePaths:
    """Build a :class:`KnowledgePaths` from an IDB path.

    Returns paths whose directories may not yet exist — call
    ``ensure()`` before first write.
    """
    if not idb_path:
        raise ValueError("idb_path is required to derive knowledge paths")
    idb_dir = os.path.dirname(os.path.abspath(idb_path))
    notes_dir = os.path.join(idb_dir, NOTES_DIR_NAME)
    kb_dir = os.path.join(idb_dir, KB_DIR_NAME)
    reports_dir = os.path.join(notes_dir, REPORTS_SUBDIR)
    return KnowledgePaths(
        idb_path=os.path.abspath(idb_path),
        notes_dir=notes_dir,
        kb_dir=kb_dir,
        reports_dir=reports_dir,
        binary_id=derive_binary_id(idb_path, db_instance_id),
    )


# ---------------------------------------------------------------------------
# Entity ID builders
# ---------------------------------------------------------------------------


def normalize_address(addr: int | str | None) -> str:
    """Render a numeric address as ``0x<lowercase-hex>``.

    Accepts ``int`` or a hex string (``"0x401000"``, ``"401000"``).
    Returns empty string for ``None``.
    """
    if addr is None:
        return ""
    if isinstance(addr, str):
        s = addr.strip().lower()
        if s.startswith("0x"):
            s = s[2:]
        try:
            return f"0x{int(s, 16):x}"
        except ValueError:
            return ""
    try:
        return f"0x{int(addr):x}"
    except (TypeError, ValueError):
        return ""


def function_entity_id(address: int | str) -> str:
    return f"func:{normalize_address(address)}"


def string_entity_id(address: int | str) -> str:
    return f"string:{normalize_address(address)}"


def import_entity_id(module: str, name: str) -> str:
    safe_mod = re.sub(r"[^A-Za-z0-9._-]", "_", module)
    safe_name = re.sub(r"[^A-Za-z0-9._@$?()-]", "_", name)
    return f"import:{safe_mod}:{safe_name}"


def global_entity_id(address: int | str) -> str:
    return f"global:{normalize_address(address)}"


def struct_entity_id(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("_") or "unnamed"
    return f"struct:{safe}"


def algo_entity_id(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("_") or "unnamed"
    return f"algo:{safe}"


def capability_entity_id(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("_") or "unnamed"
    return f"capability:{safe}"


def ioc_entity_id(kind: str, value: str) -> str:
    safe_kind = re.sub(r"[^A-Za-z0-9_-]", "_", kind).strip("_") or "unknown"
    safe_val = re.sub(r"[^A-Za-z0-9._:/@?-]", "_", value).strip("_") or "unknown"
    return f"ioc:{safe_kind}:{safe_val}"


def note_entity_id(slug: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", slug).strip("_") or "untitled"
    return f"note:{safe}"


def report_entity_id(slug: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", slug).strip("_") or "untitled"
    return f"report:{safe}"


def relation_id(src: str, predicate: str, dst: str) -> str:
    return f"rel:{src}:{predicate}:{dst}"


def extract_addresses(text: str) -> list[str]:
    """Pull hex addresses out of an arbitrary string.

    Used by ingestion to populate ``entity_refs`` on memories.
    Returns a de-duplicated list while preserving first-seen order.
    Accepts ``0x...`` and ``0X...`` notations.
    """
    seen: dict[str, None] = {}
    for m in re.finditer(r"\b0[xX][0-9a-fA-F]{4,16}\b", text or ""):
        a = m.group(0).lower()
        seen.setdefault(a, None)
    return list(seen.keys())


def entity_id_from_address(address: int | str) -> str:
    """Best-effort mapping of an address literal to a function entity ID."""
    return function_entity_id(address)


def ensure_safe_relative_path(root: str, candidate: str) -> str:
    """Assert *candidate* is contained inside *root*. Raises on traversal.

    Mirrors the pattern from research._safe_note_path so storage helpers
    can use it without circular imports. Returns the resolved absolute
    path using native separators, regardless of what was supplied.
    """
    root_abs = os.path.abspath(root)
    cand_abs = os.path.abspath(os.path.join(root, candidate))
    # Use os.path.normpath on both so separator style doesn't matter
    # for the containment check on Windows.
    root_norm = os.path.normpath(root_abs)
    cand_norm = os.path.normpath(cand_abs)
    if os.path.commonpath([root_norm, cand_norm]) != root_norm:
        raise ValueError(f"Path traversal blocked: '{candidate}' escapes '{root}'")
    return cand_norm
