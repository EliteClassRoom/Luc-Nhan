"""Case service: active-case binding, promotion, and source drift.

Coordinates the case repository, binary service, and workspace store
for authority-bound promotion and lazy source-drift validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .authority import MemoryWriteAuthority
from .case_repository import CaseRepository
from .case_schema import CasePromotion, PromotionSource
from .repository import SQLiteKnowledgeRepository
from .workspace import MemoryRunContext


class CaseMembershipError(RuntimeError):
    """Raised when a binary is not a current member of the active case."""


class SourceDriftError(RuntimeError):
    """Raised when a promotion source has drifted from its recorded revision."""


@dataclass(frozen=True)
class SourceState:
    """Lazy evaluation result of a promotion source."""

    status: Literal["current", "changed", "missing", "source_not_member", "workspace_unavailable"]


class CaseMemoryService:
    """Coordinates promotion, source drift, and active-case operations.

    Parameters
    ----------
    case_repository:
        Case CRUD/membership repository.
    binary_repository:
        Read/write interface for the active binary workspace.
    """

    def __init__(
        self,
        case_repository: CaseRepository,
        binary_repository: SQLiteKnowledgeRepository | None = None,
    ) -> None:
        self._cases = case_repository
        self._binary_repo = binary_repository

    def require_current_member(self, case_id: str, memory_id: str) -> None:
        """Raise ``CaseMembershipError`` if *memory_id* is not a current member."""
        if not self._cases.is_current_member(case_id, memory_id):
            raise CaseMembershipError(f"{memory_id} is not a current member of {case_id}")

    def evaluate_source_state(
        self,
        case_id: str,
        source: PromotionSource,
    ) -> SourceState:
        """Evaluate whether a promotion source is still current.

        This is a lazy check — it does not update any database. Returns
        ``current``, ``changed``, ``missing``, ``source_not_member``, or
        ``workspace_unavailable``.
        """
        if not self._cases.is_current_member(case_id, source.source_memory_id):
            return SourceState(status="source_not_member")

        if self._binary_repo is None:
            return SourceState(status="workspace_unavailable")

        if source.source_memory_id != self._binary_repo.owner_memory_id:
            return SourceState(status="workspace_unavailable")

        # Check if the source record still exists with the same revision
        fact = self._binary_repo._store.get_fact(source.source_record_id)
        if fact is None:
            return SourceState(status="missing")
        if fact.revision != source.source_revision:
            return SourceState(status="changed")
        return SourceState(status="current")

    def promote(
        self,
        authority: MemoryWriteAuthority,
        context: MemoryRunContext,
        case_id: str,
        source_record_id: str,
        promotion_kind: str = "direct",
    ) -> CasePromotion:
        """Promote a binary fact into the active case workspace.

        Requires authority, current membership, and a valid active case.
        The source revision/hash is resolved internally — never accepted
        from the caller. The promotion is persisted in the case workspace
        DB as a structured fact with provenance observation.
        """
        if self._binary_repo is None:
            raise CaseMembershipError("no binary repository attached")

        source_memory_id = context.binary_memory_id
        self.require_current_member(case_id, source_memory_id)

        fact = self._binary_repo._store.get_fact(source_record_id)
        if fact is None:
            raise SourceDriftError(f"source record not found: {source_record_id}")

        import hashlib

        from .workspace import new_record_id
        from .workspace_store import WorkspaceStore

        source_hash = hashlib.sha256(fact.content.encode("utf-8")).hexdigest()

        source = PromotionSource(
            source_memory_id=source_memory_id,
            source_record_id=source_record_id,
            source_revision=fact.revision,
            source_hash=source_hash,
        )

        promotion_id = new_record_id("promotion")
        case_fact_id = new_record_id("fact")

        # Persist in case workspace DB
        from .workspace_open import open_workspace_for_write

        case_paths = self._cases.locator.case(case_id)
        if case_paths.database.exists():
            case_store = open_workspace_for_write(case_paths, case_id, self._cases.locator.backups(case_id))
        else:
            case_store = WorkspaceStore.create(case_paths, owner_memory_id=case_id, workspace_kind="case")
        case_store.put_fact(
            case_fact_id,
            "promoted",
            fact.title,
            fact.content,
            fact.confidence,
            expected_revision=0,
        )
        oid = new_record_id("observation")
        import json

        case_store.append_observation(
            oid,
            "promotion",
            json.dumps(
                {
                    "promotion_id": promotion_id,
                    "source_memory_id": source_memory_id,
                    "source_record_id": source_record_id,
                    "source_revision": fact.revision,
                    "source_hash": source_hash,
                    "promotion_kind": promotion_kind,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        case_store.close()

        return CasePromotion(
            promotion_id=promotion_id,
            case_fact_id=case_fact_id,
            case_id=case_id,
            promotion_kind=promotion_kind,
            source=source,
            revision=1,
        )
