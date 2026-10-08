"""IDA headless session controller.

Uses ``SessionControllerBase`` with a headless dispatcher and the IDA
tool registry configured for headless (no ``ida_ui`` capability).  Waits
for auto-analysis before accepting work.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..memory.workspace import IdentityRequest

from ..core.config import LucNhanConfig
from ..core.host import get_database_path
from ..core.logging import log_info, log_warning
from ..ui.session_controller_base import SessionControllerBase
from .dispatch import IdaHeadlessDispatcher
from .tools.registry import (
    create_default_registry,
    register_advanced_tools,
    reset_failed_advanced_modules,
)


class HeadlessSessionController(SessionControllerBase):
    """IDA headless controller — no Qt, no UI-dependent tools.

    Parameters
    ----------
    config:
        LucNhanConfig instance.
    dispatcher:
        ``IdaHeadlessDispatcher`` used to marshal tool calls onto the
        IDA main thread.  The controller does **not** own the dispatcher
        — the bootstrap loop pumps it.
    wait_for_auto_analysis:
        If True (default), block until ``ida_auto.auto_wait()`` finishes
        before the first prompt or server start.
    """

    def __init__(
        self,
        config: LucNhanConfig,
        dispatcher: IdaHeadlessDispatcher,
        *,
        wait_for_auto_analysis: bool = True,
        memory_source: IdentityRequest | None = None,
    ):
        self._dispatcher = dispatcher
        self._wait_for_auto = wait_for_auto_analysis
        self._memory_source = memory_source

        super().__init__(
            config=config,
            tool_registry_factory=self._make_registry,
            database_path_getter=get_database_path,
            host_name="IDA Pro (headless)",
            ensure_tools_ready=register_advanced_tools,
            reset_deferred_tools=reset_failed_advanced_modules,
        )

    def _make_registry(self) -> Any:
        """Create a ToolRegistry wired for headless mode (no UI tools)."""
        return create_default_registry(
            dispatch_wrapper=self._dispatcher.wrap,
            ida_ui=False,
        )

    def wait_auto_analysis(self) -> bool:
        """Wait for IDA auto-analysis to complete.

        Returns True if analysis completed; False on error.
        Call before ``start_agent`` or starting the server.
        """
        if not self._wait_for_auto:
            log_info("Skipping auto-analysis wait (disabled by flag).")
            return True
        try:
            ida_auto = importlib.import_module("ida_auto")
            log_info("Waiting for IDA auto-analysis...")
            ida_auto.auto_wait()
            log_info("Auto-analysis complete.")
            return True
        except ImportError:
            log_warning("ida_auto not available — skipping auto-analysis wait.")
            return True
        except Exception as e:
            log_warning(f"auto_wait failed: {e} — proceeding anyway.")
            return False

    def list_functions_raw(self, offset: int = 0, limit: int = 0) -> list[dict]:
        """Enumerate all functions in the IDB for the bulk renamer."""
        from .tools.functions import _enumerate_all_functions

        return _enumerate_all_functions(offset=offset, limit=limit)

    def get_function_count(self) -> int:
        """Return the total number of functions in the IDB."""
        from .tools.functions import _get_function_count

        return _get_function_count()

    def begin_function_enumeration(self) -> None:
        """Start a cursor-based function enumeration session."""
        try:
            idautils = importlib.import_module("idautils")
        except ImportError:
            self._function_enum_iter = None
            raise
        self._function_enum_iter = iter(idautils.Functions())

    def next_function_chunk(self, limit: int) -> tuple[list[dict], bool]:
        """Return up to *limit* function entries and whether more may remain."""
        iterator = getattr(self, "_function_enum_iter", None)
        if iterator is None:
            return [], False
        try:
            ida_funcs = importlib.import_module("ida_funcs")
            ida_name = importlib.import_module("ida_name")
        except ImportError:
            self._function_enum_iter = None
            raise

        chunk: list[dict] = []
        more = True
        for _ in range(max(1, limit)):
            try:
                ea = next(iterator)
            except StopIteration:
                more = False
                self._function_enum_iter = None
                break
            name = ida_name.get_name(ea)
            size_bytes = 0
            func = ida_funcs.get_func(ea)
            if func is not None:
                size_bytes = func.end_ea - func.start_ea
            chunk.append({"address": ea, "name": name, "is_import": False, "size_bytes": size_bytes})
        return chunk, more

    def cancel_function_enumeration(self) -> None:
        """Cancel any active cursor-based function enumeration session."""
        self._function_enum_iter = None
