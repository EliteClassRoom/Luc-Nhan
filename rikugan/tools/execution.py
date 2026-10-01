"""Per-invocation tool execution context.

Carries the three things a tool handler cannot know on its own: which
dispatcher marshals work onto the host (IDA main) thread, the
cancellation event of the current agent run, and the registry's
monotonic deadline for this call.

The context lives in a :class:`~contextvars.ContextVar` because it must
be installed *inside* the executor worker — a ``ContextVar`` set on the
submitting thread does not cross a thread boundary, and two concurrent
registry executions must never see each other's cancel event.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolExecutionContext:
    """Environment of a single tool invocation.

    ``dispatch_wrapper`` is the registry's host-thread dispatcher (the
    ``wrap(handler) -> handler`` seam).  ``cancel_event`` is the agent
    run's cancellation event, already-set events mean "abandon the call
    and return partial state".  ``deadline`` is a ``time.monotonic()``
    bound recorded before the handler was submitted.
    """

    dispatch_wrapper: Callable | None = None
    cancel_event: threading.Event | None = None
    deadline: float | None = None


_EMPTY_CONTEXT = ToolExecutionContext()

_execution_context: ContextVar[ToolExecutionContext] = ContextVar(
    "rikugan_tool_execution_context", default=_EMPTY_CONTEXT
)


def get_execution_context() -> ToolExecutionContext:
    """Return the context of the current tool invocation.

    Outside any invocation this is the empty default, so callers never
    have to guard against ``None``.
    """
    return _execution_context.get()


@contextmanager
def tool_execution_context(ctx: ToolExecutionContext) -> Iterator[ToolExecutionContext]:
    """Install *ctx* for the duration of the block, then restore the previous one.

    Used by the registry inside the executor worker.  The reset in
    ``finally`` is what keeps a cancelled context from leaking into the
    next task that reuses the same pool thread.
    """
    token = _execution_context.set(ctx)
    try:
        yield ctx
    finally:
        _execution_context.reset(token)


def run_on_host_thread(func: Callable, *args: Any, **kwargs: Any) -> Any:
    """Run *func* on the host thread and return its result.

    Uses the dispatcher installed in the current
    :class:`ToolExecutionContext`; without one it falls back to the
    ``idasync`` seam, which is a direct call outside UI-mode IDA.  When
    the caller is already on the host thread the call is made directly —
    re-dispatching would just deadlock waiting for ourselves.
    """
    if threading.current_thread() is threading.main_thread():
        return func(*args, **kwargs)

    dispatcher = get_execution_context().dispatch_wrapper
    if dispatcher is None:
        from rikugan.core.thread_safety import idasync

        dispatcher = idasync
    return dispatcher(func)(*args, **kwargs)
