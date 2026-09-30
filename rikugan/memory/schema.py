"""Dataclasses for raw knowledge memory.

These types intentionally have *no* behavior beyond serialization so the
MVP stays portable. Higher-level features (search, ranking, retrieval)
live in :mod:`rikugan.memory.retrieve` and :mod:`rikugan.memory.context`.

All fields use ``str`` for IDs and timestamps because JSONL is the
canonical on-disk format. The :class:`KnowledgeRawStore` is responsible
for converting timestamps to ISO 8601 strings and back.

ID conventions (recommended, enforced by helpers in :mod:`paths`):

* memories: ``mem:<category>:<address-or-slug>:<short-hash>``
* entities: ``func:0x401000``, ``string:0x408120``,
  ``import:wininet.dll:HttpSendRequestA``, ``global:0x409000``,
  ``struct:malware_config``, ``algo:rc4_ksa``,
  ``capability:c2_communication``, ``ioc:domain:example.com``,
  ``note:<slug>``, ``report:<slug>``
* relations: ``rel:<src>:<predicate>:<dst>`` (deterministic)

The deterministic IDs make upsert idempotent: same logical finding
ingested twice updates one record instead of creating a duplicate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

HypothesisStatus = Literal["unverified", "verified", "wrong"]


@dataclass
class KnowledgeMemory:
    """A single durable memory note.

    Mirrors Claude's memory style: a short, retrievable, typed fact
    tagged with the entities/relations/sources it references.
    """

    id: str
    binary_id: str
    type: str  # "fact" | "function_purpose" | "data_structure" | "constant" | "hypothesis" | "string_ref" | "import_usage" | "general" | ...
    title: str
    content: str
    entity_refs: list[str] = field(default_factory=list)
    relation_refs: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.5
    importance: float = 0.5
    verified: bool = False
    status: HypothesisStatus = "unverified"
    verdict_claim: str = ""
    verification_citations: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"unverified", "verified", "wrong"}:
            raise ValueError(f"invalid hypothesis status: {self.status!r}")
        if not isinstance(self.verdict_claim, str):
            raise ValueError("verdict_claim must be a string")
        if not isinstance(self.verification_citations, list) or not all(
            isinstance(citation, str) and citation.strip() for citation in self.verification_citations
        ):
            raise ValueError("verification_citations must be a list of non-empty strings")
        if self.type == "hypothesis":
            # ``verified`` is derived from ``status``. Do NOT auto-promote
            # legacy records that carry ``verified=True`` without an
            # explicit ``status`` field — they predate the ``/verify``
            # flow and were promoted by the legacy ``review_memories``
            # mechanism, which ``/report`` no longer trusts. Only an
            # explicit ``/verify`` verdict (``status="verified"``) may
            # surface a hypothesis in ``/report``.
            self.verified = self.status == "verified"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KnowledgeMemory:
        # Ignore unknown keys so newer producers can pass extras without
        # breaking older readers.
        allowed = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in allowed}
        # Legacy-safe default: when a hypothesis record predates the
        # ``status`` field, default to ``unverified`` so the strict
        # verified-only ``/report`` filter cannot surface a record that
        # was merely ``verified=True`` from an older build. Only an
        # explicit ``status="verified"`` produced by ``/verify`` may
        # authorize report inclusion. Verdict metadata is also
        # defaulted to empty so the legacy record cannot present
        # citations it never had.
        if "status" not in clean and clean.get("type") == "hypothesis":
            clean["status"] = "unverified"
        clean.setdefault("verdict_claim", "")
        clean.setdefault("verification_citations", [])
        return cls(**clean)


@dataclass
class KnowledgeEntity:
    """A named thing in the binary world.

    Entities are the *nouns* of the knowledge graph: functions,
    strings, imports, globals, structs, algos, capabilities,
    IOCs, notes/reports.
    """

    id: str  # e.g. "func:0x401000", "string:0x408120", "import:..."
    binary_id: str
    type: str  # "function" | "string" | "import" | "global" | "struct" | "algo" | "capability" | "ioc" | "note" | "report" | ...
    name: str
    display_name: str = ""
    address: str = ""  # e.g. "0x401000"; empty when not applicable
    tags: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KnowledgeEntity:
        allowed = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in allowed}
        return cls(**clean)


@dataclass
class KnowledgeRelation:
    """A directed edge between two entities.

    *src* and *dst* are entity IDs. *predicate* comes from a small
    controlled vocabulary (calls, uses_import, references_string,
    implements, likely_implements, decrypts, parses,
    belongs_to_capability, has_ioc, mentioned_in_note,
    supports_finding, contradicts_finding, derived_from, ...).
    """

    id: str
    binary_id: str
    src: str
    predicate: str
    dst: str
    evidence: str = ""
    confidence: float = 0.5
    source_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KnowledgeRelation:
        allowed = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in allowed}
        return cls(**clean)


@dataclass
class KnowledgeObservation:
    """An immutable event in the timeline.

    Used for traces such as "save_memory fired with category=...",
    "exploration_report logged finding X", "subagent Y completed",
    or any telemetry we want to keep without rewriting. Append-only.
    """

    id: str
    binary_id: str
    ts: str
    kind: str  # "save_memory" | "exploration_finding" | "research_note_saved" | "report_generated" | "command" | ...
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KnowledgeObservation:
        allowed = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in allowed}
        return cls(**clean)
