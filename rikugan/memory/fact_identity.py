"""Pure fact identity contract.

Defines canonicalization rules and deterministic identity helpers for
the memory durability feature. This is the lowest layer of the
subsystem and must NOT import from :mod:`rikugan.memory.workspace_store`
or :mod:`rikugan.memory.repository` (avoid migration-time circular
imports). Downstream tasks (schema v2 migration, atomic dedup, bundle
import) consume this module.

Canonicalization rules
----------------------

- ``canonicalize_fact_type``: NFC normalize, strip outer whitespace,
  collapse interior runs of any unicode whitespace to a single ASCII
  space, then casefold to lowercase. Result must be non-empty.
- ``canonicalize_fact_content``: NFC normalize, convert ``\\r\\n`` and
  lone ``\\r`` to ``\\n``, strip outer whitespace. Interior spacing,
  case, punctuation, and line order are preserved verbatim.

Hashing
-------

- ``semantic_fact_hash`` returns the lowercase hex SHA-256 of a
  length-prefixed payload. Only the *canonicalized* type and content
  influence the digest; surrounding whitespace noise does not.
- ``deterministic_import_record_id`` returns
  ``<record_type>-<hex32>`` scoped to ``(target, import, type, origin)``
  so the same origin record imported into different targets produces
  distinct IDs.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_TYPE_WS_RE = re.compile(r"\s+")
_RECORD_TYPES = frozenset({"fact", "entity", "relation"})


def canonicalize_fact_type(value: str) -> str:
    """Canonicalize a fact type tag.

    NFC-normalizes, strips outer whitespace, collapses interior
    whitespace runs to a single ASCII space, and casefolds to
    lowercase. Raises :class:`TypeError` for non-string input and
    :class:`ValueError` if the result is empty.
    """
    if not isinstance(value, str):
        raise TypeError("fact_type must be a string")
    canonical = _TYPE_WS_RE.sub(" ", unicodedata.normalize("NFC", value).strip()).casefold()
    if not canonical:
        raise ValueError("fact_type must not be empty after canonicalization")
    return canonical


def canonicalize_fact_content(value: str) -> str:
    """Canonicalize fact content (the body).

    NFC-normalizes, normalizes ``\\r\\n`` and lone ``\\r`` to ``\\n``,
    and strips outer whitespace. Interior spacing, case, punctuation,
    and line order are preserved. Raises :class:`TypeError` for
    non-string input and :class:`ValueError` if the result is empty.
    """
    if not isinstance(value, str):
        raise TypeError("content must be a string")
    canonical = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if not canonical:
        raise ValueError("content must not be empty after canonicalization")
    return canonical


def semantic_fact_hash(fact_type: str, content: str) -> str:
    """Return a stable 64-char lowercase SHA-256 hex digest.

    The payload is length-prefixed by the canonical type so two facts
    that share a content body but differ in type (or vice versa)
    produce different digests.
    """
    category = canonicalize_fact_type(fact_type)
    body = canonicalize_fact_content(content)
    category_bytes = category.encode("utf-8")
    payload = str(len(category_bytes)).encode("ascii") + b":" + category_bytes + body.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def deterministic_import_record_id(
    target_memory_id: str,
    import_id: str,
    record_type: str,
    origin_record_id: str,
) -> str:
    """Return ``<record_type>-<hex32>`` scoped to the four inputs.

    Each field is length-prefixed so concatenated inputs cannot collide
    (``("ab", "c")`` vs ``("a", "bc")``). Only ``fact``, ``entity``,
    and ``relation`` record types are accepted.
    """
    if record_type not in _RECORD_TYPES:
        raise ValueError(f"unsupported import record type: {record_type!r}")
    fields = (target_memory_id, import_id, record_type, origin_record_id)
    encoded = b"".join(
        str(len(field.encode("utf-8"))).encode("ascii") + b":" + field.encode("utf-8") for field in fields
    )
    return f"{record_type}-{hashlib.sha256(encoded).hexdigest()[:32]}"
