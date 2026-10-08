"""IDA session controller.

Performance note
----------------
``create_default_registry`` imports the entire IDA tool module tree
(``ida.tools.navigation``, ``functions``, ``strings``, etc.) —
about 20 tool modules, ~20ms cold. The panel constructs an
``IdaSessionController`` immediately on user click, so we defer that
import to the moment the session controller is actually instantiated.
"""

from __future__ import annotations

import importlib

from ...core.config import LucNhanConfig
from ...core.host import get_database_path
from ...core.logging import log_debug
from ...ui.session_controller_base import SessionControllerBase

# Segment names that the IDA loader assigns to import thunks.  A
# function whose containing segment has one of these names is an
# import stub (e.g. ``.idata``, ``__imp_*``, external symbol
# dispatcher), not a real binary function — the bulk renamer must
# skip it to avoid attempting to rename imports.
_IMPORT_SEGMENT_NAMES = frozenset({".idata", ".extern", "extern"})


def _lazy_create_ida_registry():
    """Return a registry of all built-in IDA tools.

    Imports ``ida.tools.registry`` (and its 12 submodules) on the first
    call. The factory is invoked by ``SessionControllerBase.__init__``
    which itself is called from a background thread, so the cost lands
    off the user-click path.
    """
    from ..tools.registry import create_default_registry

    return create_default_registry()


class IdaSessionController(SessionControllerBase):
    """IDA-oriented controller."""

    def __init__(self, config: LucNhanConfig):
        # Keep the IDA-specific advanced-tool registration imports
        # local to this module — the shared UI base must remain
        # host-agnostic.
        from ..tools.registry import register_advanced_tools, reset_failed_advanced_modules

        super().__init__(
            config=config,
            tool_registry_factory=_lazy_create_ida_registry,
            database_path_getter=get_database_path,
            host_name="IDA Pro",
            ensure_tools_ready=register_advanced_tools,
            reset_deferred_tools=reset_failed_advanced_modules,
        )
        # State for the bulk-renamer function-enumeration pump.
        # ``begin_function_enumeration`` sets ``_funcs_iter``; each
        # ``next_function_chunk`` call drains N entries and returns
        # them.  ``cancel_function_enumeration`` clears the iterator
        # so the next enumeration starts from scratch.
        self._funcs_iter = None

    # --- Bulk-renamer function enumeration ---

    def begin_function_enumeration(self) -> None:
        """Start a fresh structured enumeration of all functions.

        Returns nothing; subsequent calls to :func:`next_function_chunk`
        pull from the iterator established here.  We resolve the IDA
        modules on demand via ``importlib`` to keep this method
        callable from test stubs that do not load the real
        ``ida_*`` extensions.

        Defensive contract
        ------------------
        If ``importlib.import_module("idautils")`` raises
        ``ImportError`` (e.g. the host's IDA build is missing the
        module, or the controller is being exercised outside IDA
        and ``idautils`` is not on ``sys.path``) we MUST clear any
        previously-set ``_funcs_iter`` state before re-raising.  The
        old revision left a stale iterator reference, which meant
        a later ``next_function_chunk`` call would happily resume
        draining an iterator from a *previous* enumeration whose
        IDA modules no longer exist — silently producing stale
        results.  Tests pin the import-failure behaviour: state
        must be cleared, and the exception must propagate so
        callers can decide how to recover.
        """
        try:
            idautils = importlib.import_module("idautils")
        except ImportError as exc:
            log_debug(
                f"IdaSessionController.begin_function_enumeration: "
                f"idautils import failed: {exc!r}; clearing enumeration state."
            )
            self._funcs_iter = None
            raise
        # ``idautils.Functions()`` returns a fresh generator.  Hold a
        # reference on the controller so ``next_function_chunk`` can
        # resume from the same iterator across timer ticks.
        self._funcs_iter = iter(idautils.Functions())

    def next_function_chunk(self, limit: int) -> tuple[list[dict], bool]:
        """Pull the next *limit* functions from the active enumeration.

        Returns ``(chunk, more)`` where ``chunk`` is a list of
        ``{"address", "name", "is_import", "size_bytes"}`` dicts and
        ``more`` is True if the iterator still has more entries
        (caller should call again to keep draining).

        Defensive contract
        ------------------
        If a required IDA module (``ida_funcs`` or ``ida_name``)
        cannot be imported mid-enumeration, we MUST clear
        ``_funcs_iter`` before re-raising.  Leaving a stale
        iterator behind would let a future ``begin_function_enumeration``
        + ``next_function_chunk`` cycle silently produce empty
        results, hiding the underlying import failure from the
        UI.  ``ida_segment`` is best-effort and does NOT trigger
        cleanup on its own.
        """
        if self._funcs_iter is None:
            return [], False
        try:
            ida_funcs = importlib.import_module("ida_funcs")
            ida_name = importlib.import_module("ida_name")
        except ImportError as exc:
            log_debug(
                f"IdaSessionController.next_function_chunk: "
                f"ida_funcs/ida_name import failed: {exc!r}; clearing enumeration state."
            )
            self._funcs_iter = None
            raise
        # ``ida_segment`` may not be importable on stripped-down IDA
        # builds; treat the import-seg heuristic as best-effort.
        try:
            ida_segment = importlib.import_module("ida_segment")
        except ImportError:
            ida_segment = None

        chunk: list[dict] = []
        for _ in range(max(1, int(limit))):
            try:
                ea = next(self._funcs_iter)
            except StopIteration:
                self._funcs_iter = None
                return chunk, False
            name = ida_name.get_name(ea) or ""
            is_import = False
            if ida_segment is not None:
                try:
                    seg = ida_segment.getseg(ea)
                except Exception:
                    seg = None
                if seg is not None:
                    seg_name = ida_segment.get_segm_name(seg) or ""
                    is_import = seg_name in _IMPORT_SEGMENT_NAMES
            func = ida_funcs.get_func(ea)
            size_bytes = (func.end_ea - func.start_ea) if func is not None else 0
            chunk.append(
                {
                    "address": int(ea),
                    "name": name,
                    "is_import": bool(is_import),
                    "size_bytes": int(size_bytes),
                }
            )
        return chunk, True

    def cancel_function_enumeration(self) -> None:
        """Drop the in-flight enumeration iterator so the next
        :func:`begin_function_enumeration` starts from scratch.

        Safe to call when no enumeration is in progress.
        """
        self._funcs_iter = None

    # --- Backwards-compatible read-only helpers ---

    def get_function_count(self) -> int:
        """Total number of functions in the IDB.

        Retained for callers that need a single-shot count (no
        pagination).  Bulk renamer code should prefer the
        ``begin_function_enumeration`` / ``next_function_chunk``
        pump above.
        """
        idautils = importlib.import_module("idautils")
        return sum(1 for _ in idautils.Functions())

    def list_functions_raw(self, offset: int = 0, limit: int = 0) -> list[dict]:
        """Return structured function metadata as a flat list.

        ``offset`` and ``limit`` are 0-based; ``limit == 0`` means
        "no limit".  This is the canonical source for renamer-style
        consumers that want address / name / is_import / size_bytes
        without going through the chunked pump.
        """
        self.begin_function_enumeration()
        # Drain into a list so the caller can apply its own
        # offset/limit semantics; the iterator is consumed at the
        # end of this call.
        rows: list[dict] = []
        idx = 0
        stop = offset + limit if limit > 0 else None
        while True:
            chunk, more = self.next_function_chunk(limit=500)
            for row in chunk:
                if idx >= offset:
                    rows.append(row)
                idx += 1
                if stop is not None and idx >= stop:
                    self.cancel_function_enumeration()
                    return rows
            if not more:
                return rows
            # Cooperative log spam guard — enumerate never raises
            # but a tight loop with no log is opaque if a future
            # caller hangs.
            log_debug("list_functions_raw drained another batch")


# Backwards-compatible alias
SessionController = IdaSessionController
