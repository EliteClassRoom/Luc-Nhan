"""MemoryWorkspaceManager: facade for the central memory subsystem.

This manager wraps the registry, identity resolver, and locator into a single
controller-owned object. It binds identity evidence to a workspace, tracks
process-local generations, and produces frozen run contexts.
"""

from __future__ import annotations

from ..core.config import LucNhanConfig
from .case_repository import CaseRepository
from .identity import IdentityResolution, MemoryIdentityResolver
from .registry import MemoryRegistry
from .workspace import (
    IdentityRequest,
    MemoryLocator,
    MemoryRunContext,
    WorkspaceBinding,
    WorkspacePaths,
    validate_memory_id,
)


class PersistenceDisabled(RuntimeError):
    """Raised when a caller requests persistent paths while persistence is disabled."""


class MemoryWorkspaceManager:
    """Controller-owned facade for central memory workspace binding.

    Parameters
    ----------
    config:
        LucNhanConfig — only ``memory_dir`` is read.
    """

    def __init__(self, config: LucNhanConfig) -> None:
        self._config = config
        self._locator = MemoryLocator(config.memory_dir)
        self._registry = MemoryRegistry(self._locator.registry_database())
        self._resolver = MemoryIdentityResolver(self._registry)
        self._binding: WorkspaceBinding | None = None
        self._database_generation = 0
        self._case_binding_generation = 0
        self._active_case_id: str = ""

        self._registry.initialize()

    def bind(
        self,
        request: IdentityRequest,
        choice: object | None = None,
    ) -> IdentityResolution:
        """Bind identity evidence to a workspace and return the resolution."""
        resolution = self._resolver.resolve(request, choice)
        if resolution.binding is not None:
            if self._binding is None or resolution.binding.memory_id != self._binding.memory_id:
                self._database_generation += 1
            self._binding = resolution.binding
        return resolution

    def run_context(self, active_case_id: str = "") -> MemoryRunContext:
        """Return a frozen run context for the current binding.

        If *active_case_id* is empty, uses the internally tracked active case.
        """
        memory_id = self._binding.memory_id if self._binding is not None else ""
        case_id = active_case_id if active_case_id else self._active_case_id
        return MemoryRunContext(
            binary_memory_id=memory_id,
            active_case_id=case_id,
            database_generation=self._database_generation,
            case_binding_generation=self._case_binding_generation,
        )

    def validate_run_context(self, context: MemoryRunContext) -> bool:
        """Return True if *context* matches the current binding/generations."""
        current = self.run_context(context.active_case_id)
        return current == context

    # ------------------------------------------------------------------
    # Active-case binding
    # ------------------------------------------------------------------

    def set_active_case(self, case_id: str) -> MemoryRunContext:
        """Set the active case for the current binary.

        Requires the binary to be a current member of a non-deleted case.
        Increments ``case_binding_generation`` on every change.
        """
        if self._binding is None or self._binding.state not in {"active", "provisional"}:
            raise PersistenceDisabled("no active binary binding")

        cases = CaseRepository(self._registry, self._locator)
        case = cases.get_case(case_id)
        if case is None or case.state == "deleted":
            raise ValueError(f"case not found or deleted: {case_id}")
        if not cases.is_current_member(case_id, self._binding.memory_id):
            raise ValueError(f"binary is not a current member of case {case_id}")

        if case_id != self._active_case_id:
            self._active_case_id = case_id
            self._case_binding_generation += 1
        return self.run_context(self._active_case_id)

    def clear_active_case(self) -> MemoryRunContext:
        """Clear the active case binding."""
        if self._active_case_id:
            self._active_case_id = ""
            self._case_binding_generation += 1
        return self.run_context()

    @property
    def active_case_id(self) -> str:
        """Return the current active case ID (empty if none)."""
        return self._active_case_id

    def require_persistent_paths(self) -> WorkspacePaths:
        """Return workspace paths for the current active binding.

        Raises ``PersistenceDisabled`` if the binding is not persistence-capable
        (disabled, ephemeral, or not yet bound).
        """
        if self._binding is None or self._binding.state not in {"active", "provisional"}:
            raise PersistenceDisabled("central memory persistence is unavailable")

        return self._locator.binary(validate_memory_id(self._binding.memory_id))

    @property
    def locator(self) -> MemoryLocator:
        """Expose the memory locator for store creation."""
        return self._locator
