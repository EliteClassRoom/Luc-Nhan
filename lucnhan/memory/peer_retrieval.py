"""Controlled peer retrieval: eligibility, ranking, read-only opens, caps.

Opens peer workspace DBs in true SQLite read-only/query-only mode. Only
peers with a current case relation at confidence ≥ 0.7 or exact artifact
match are eligible. Results are capped and namespaced.
"""

from __future__ import annotations

from dataclasses import dataclass

from .case_repository import CaseRepository
from .case_schema import CaseRelationType
from .sqlite_backend import SchemaMigrationRequired
from .workspace import MemoryLocator
from .workspace_store import WorkspaceStore

_PEER_RELATION_THRESHOLD = 0.7
_MAX_PEERS = 3
_MAX_FACTS_PER_PEER = 5


@dataclass(frozen=True)
class PeerCandidate:
    """One eligible peer for retrieval."""

    memory_id: str
    display_name: str
    score: float
    relation_type: CaseRelationType | None = None
    confidence: float = 0.0


@dataclass(frozen=True)
class PeerContextRecord:
    """One retrieved record from a peer."""

    record_id: str
    record_type: str
    content: str
    source_memory_id: str


@dataclass(frozen=True)
class PeerContextPack:
    """Result of peer retrieval."""

    peers: tuple[PeerCandidate, ...]
    records: tuple[PeerContextRecord, ...]
    used_chars: int


class PeerMemoryRetriever:
    """Controlled cross-binary retrieval within an analysis case.

    Parameters
    ----------
    case_repository:
        Case repository for membership/relation lookups.
    locator:
        Central memory locator for workspace path resolution.
    """

    def __init__(self, case_repository: CaseRepository, locator: MemoryLocator) -> None:
        self._cases = case_repository
        self._locator = locator

    def retrieve(
        self,
        case_id: str,
        active_memory_id: str,
        query: str = "",
        max_chars: int = 2000,
    ) -> PeerContextPack:
        """Retrieve structured facts from eligible peer workspaces.

        Eligibility requires a current case relation at confidence ≥ 0.7
        or an exact ``shares_artifact_with`` relation. Peer DBs are opened
        read-only/query-only.
        """
        # Find eligible peers
        eligible = self._find_eligible_peers(case_id, active_memory_id)
        if not eligible:
            return PeerContextPack(peers=(), records=(), used_chars=0)

        records: list[PeerContextRecord] = []
        used_chars = 0
        for peer in eligible[:_MAX_PEERS]:
            peer_records = self._read_peer_facts(peer, query)
            for r in peer_records[:_MAX_FACTS_PER_PEER]:
                if used_chars + len(r.content) > max_chars:
                    break
                records.append(r)
                used_chars += len(r.content) + 20  # overhead per record

        return PeerContextPack(
            peers=tuple(eligible[:_MAX_PEERS]),
            records=tuple(records),
            used_chars=used_chars,
        )

    def _find_eligible_peers(self, case_id: str, active_memory_id: str) -> list[PeerCandidate]:
        """Find peers eligible for retrieval based on relations."""
        try:
            relations = self._cases.list_case_relations(case_id)
        except SchemaMigrationRequired:
            # Reading a stale v1 case DB must not trigger migration; treat
            # the workspace as having no eligible peers instead.
            return []
        members = self._cases.list_members(case_id)
        member_names = {m.memory_id: m for m in members}

        candidates: list[PeerCandidate] = []
        for rel in relations:
            # Find the peer endpoint (the one that's NOT active_memory_id)
            if rel.subject_memory_id == active_memory_id:
                peer_mid = rel.object_memory_id
            elif rel.object_memory_id == active_memory_id:
                peer_mid = rel.subject_memory_id
            else:
                continue

            if peer_mid not in member_names:
                continue  # not a current member

            # Check eligibility: relation confidence or artifact match
            if rel.confidence >= _PEER_RELATION_THRESHOLD or (
                rel.predicate is CaseRelationType.SHARES_ARTIFACT_WITH and rel.artifact_ref
            ):
                # Get display name from registry
                ws = self._cases._registry.get_workspace(peer_mid)
                display = ws.display_name if ws else peer_mid[:12]
                candidates.append(
                    PeerCandidate(
                        memory_id=peer_mid,
                        display_name=display,
                        score=rel.confidence,
                        relation_type=rel.predicate,
                        confidence=rel.confidence,
                    )
                )

        # Sort by score descending, then by memory_id for determinism
        candidates.sort(key=lambda c: (-c.score, c.memory_id))
        return candidates

    def _read_peer_facts(self, peer: PeerCandidate, query: str) -> list[PeerContextRecord]:
        """Read current facts from a peer workspace DB (read-only)."""
        paths = self._locator.binary(peer.memory_id)
        if not paths.database.exists():
            return []

        try:
            store = WorkspaceStore.open(paths, owner_memory_id=peer.memory_id, read_only=True)
        except (FileNotFoundError, ValueError):
            return []

        try:
            facts = store.list_facts()
            query_lower = query.lower() if query else ""
            records: list[PeerContextRecord] = []
            for f in facts:
                if query_lower and query_lower not in f.content.lower() and query_lower not in f.title.lower():
                    continue
                records.append(
                    PeerContextRecord(
                        record_id=f.fact_id,
                        record_type=f.fact_type,
                        content=f.content,
                        source_memory_id=peer.memory_id,
                    )
                )
            return records
        finally:
            store.close()
