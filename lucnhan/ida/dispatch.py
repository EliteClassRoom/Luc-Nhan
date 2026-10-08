"""IDA main-thread dispatch abstraction for UI and headless modes.

In UI mode, ``IdaUiDispatcher`` wraps callables with
``ida_kernwin.execute_sync(MFF_WRITE)`` — the same behaviour the
legacy ``idasync`` decorator provides.

In headless mode, ``IdaHeadlessDispatcher`` uses an explicit
``queue.Queue`` so that worker threads can enqueue IDA API work and
wait for the IDA main thread to pump the queue.  This avoids the
deadlock that would occur if the ``-S`` script blocked the main thread
while a thread-pool worker called ``execute_sync``.
"""

from __future__ import annotations

import enum
import importlib
import threading
from collections.abc import Callable
from queue import Empty, Queue
from typing import Any, TypeVar

from ..core.host import has_ida_kernwin, is_ida_headless

F = TypeVar("F", bound=Callable[..., Any])


class DispatcherShutdownError(Exception):
    """Raised when the dispatcher is shutting down and cannot accept new jobs."""


class DispatcherTimeoutError(TimeoutError):
    """Raised when a queued job times out before being pumped on the main thread."""


_MFF_WRITE: int = 0x01  # default if ida_kernwin is unavailable

# Maximum time (seconds) a worker thread will wait for the main thread
# to pump a queued IDA API call before raising DispatcherTimeoutError.
_DEFAULT_JOB_TIMEOUT = 30.0


class _JobState(enum.Enum):
    """Lifecycle states for a dispatched job."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class _DispatchJob:
    """Single unit of work queued for the IDA main thread."""

    def __init__(self, func: Callable[..., Any], args: tuple, kwargs: dict) -> None:
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.event = threading.Event()
        self.result: Any = None
        self.exception: BaseException | None = None
        self._state: _JobState = _JobState.QUEUED
        self._state_lock = threading.Lock()

    @property
    def state(self) -> _JobState:
        with self._state_lock:
            return self._state

    def try_claim(self) -> bool:
        """Atomically claim this job (QUEUED → RUNNING).

        Returns True if claimed, False if already consumed.
        """
        with self._state_lock:
            if self._state != _JobState.QUEUED:
                return False
            self._state = _JobState.RUNNING
            return True

    def cancel(self) -> bool:
        """Atomically cancel this job (QUEUED → CANCELLED).

        Returns True if cancelled, False if already running or completed.
        Only call this from the worker (timeout) path — never from the pump.
        """
        with self._state_lock:
            if self._state != _JobState.QUEUED:
                return False
            self._state = _JobState.CANCELLED
            return True

    def mark_completed(self) -> None:
        """Mark the job as completed (RUNNING → COMPLETED).

        Only the pump thread calls this after setting result/exception.
        """
        with self._state_lock:
            self._state = _JobState.COMPLETED


class IdaUiDispatcher:
    """Dispatch callables via ``ida_kernwin.execute_sync(MFF_WRITE)``.

    This matches the legacy ``idasync`` behaviour.
    """

    def __init__(self) -> None:
        if not has_ida_kernwin():
            raise RuntimeError("IdaUiDispatcher requires ida_kernwin (UI mode only).")
        kernwin = importlib.import_module("ida_kernwin")
        self._execute_sync = kernwin.execute_sync
        self._mff = getattr(kernwin, "MFF_WRITE", _MFF_WRITE)

    def wrap(self, func: F) -> F:
        """Return a wrapper that calls *func* via execute_sync."""

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            result: list[Any] = []
            error: list[BaseException] = []

            def _call() -> int:
                try:
                    result.append(func(*args, **kwargs))
                except Exception as exc:
                    error.append(exc)
                return 0

            self._execute_sync(_call, self._mff)
            if error:
                raise error[0]
            return result[0] if result else None

        return wrapper  # type: ignore[return-value]


class IdaHeadlessDispatcher:
    """Dispatch callables via a main-thread-pumped queue.

    Worker threads (e.g. ``ThreadPoolExecutor`` inside ``ToolRegistry``)
    enqueue callables and block until the IDA main thread pumps the
    queue via ``pump_once()``.

    When ``request_shutdown()`` is called, queued and waiting jobs are
    woken with a ``DispatcherShutdownError`` so that no worker thread is
    left blocked forever.

    Jobs that are not pumped within ``_DEFAULT_JOB_TIMEOUT`` seconds
    raise ``DispatcherTimeoutError`` on the worker side and are
    atomically marked so that the pump thread will skip them if they
    appear later (preventing late-but-successful execution after the
    caller has already given up).
    """

    def __init__(self) -> None:
        self._queue: Queue[_DispatchJob] = Queue()
        self._shutdown = threading.Event()
        self._pending_jobs: list[_DispatchJob] = []
        self._pending_lock = threading.Lock()

    def wrap(self, func: F) -> F:
        """Return a wrapper that enqueues *func* for main-thread execution."""

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if self._shutdown.is_set():
                raise DispatcherShutdownError("Dispatcher is shutting down")

            if threading.current_thread() is threading.main_thread():
                return func(*args, **kwargs)

            job = _DispatchJob(func, args, kwargs)

            with self._pending_lock:
                if self._shutdown.is_set():
                    raise DispatcherShutdownError("Dispatcher is shutting down")
                self._pending_jobs.append(job)

            self._queue.put_nowait(job)

            # Wait with a timeout so the worker is never blocked forever.
            finished = job.event.wait(timeout=_DEFAULT_JOB_TIMEOUT)

            # Clean up from pending list.
            with self._pending_lock:
                try:
                    self._pending_jobs.remove(job)
                except ValueError:
                    pass

            if not finished:
                # The main thread hasn't pumped this job within the
                # timeout.  Try to cancel it if still queued.
                if job.cancel():
                    # Job was still QUEUED — safe to cancel.
                    job.exception = DispatcherTimeoutError(
                        f"IDA dispatcher job timed out after {_DEFAULT_JOB_TIMEOUT:.0f}s"
                    )
                    job.event.set()
                else:
                    # The pump has already claimed this job (state is
                    # RUNNING or COMPLETED).  Wait for the pump to
                    # finish executing the function and set the result.
                    # We must NOT set job.event here — only the pump
                    # signals completion.
                    job.event.wait()
                    finished = True

            if finished:
                if self._shutdown.is_set() and job.exception is None:
                    raise DispatcherShutdownError("Dispatcher shut down while waiting for job")

                if job.exception is not None:
                    raise job.exception
                return job.result

            # Invariant: the timeout branch above always sets job.exception to a
            # DispatcherTimeoutError. Use an explicit guard instead of assert so
            # the check survives `python -O` and produces a clear error if a race
            # ever leaves job.exception unset.
            if job.exception is None:
                raise DispatcherTimeoutError("IDA dispatcher job timed out but no exception was recorded")
            raise job.exception  # timeout error

        return wrapper  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Public shutdown observation API — use this instead of reading
    # the private ``_shutdown`` attribute from bootstrap.
    # ------------------------------------------------------------------

    def is_shutdown_requested(self) -> bool:
        """Return True once ``request_shutdown()`` has been called."""
        return self._shutdown.is_set()

    # ------------------------------------------------------------------
    # Pump methods
    # ------------------------------------------------------------------

    def pump_once(self, timeout: float = 0.5) -> bool:
        """Process one job from the queue (blocking up to *timeout*).

        Returns True if a job was dequeued, False on timeout.
        Timed-out / cancelled jobs are skipped internally.
        """
        try:
            job = self._queue.get(timeout=timeout)
        except Empty:
            return False

        self._process_job(job)
        return True

    def pump_all(self, timeout: float = 0.0) -> int:
        """Process all pending jobs.  Returns count of jobs processed."""
        count = 0
        while True:
            try:
                job = self._queue.get(timeout=timeout)
            except Empty:
                break
            self._process_job(job)
            count += 1
        return count

    def pump_until(self, timeout: float = 1.0) -> int:
        """Process one job with timeout.  Returns count of jobs processed.

        Designed for headless bootstrap pump loops.  Returns 0 on timeout.
        Unlike ``pump_forever``, this returns after processing at most
        one job, giving the caller a chance to check shutdown state.
        """
        try:
            job = self._queue.get(timeout=timeout)
        except Empty:
            return 0
        self._process_job(job)
        return 1

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def request_shutdown(self) -> None:
        """Signal the pump loop to exit and wake all blocked worker threads.

        All queued jobs are processed with a ``DispatcherShutdownError``
        exception, and waiting workers are unblocked.

        ``request_shutdown()`` is idempotent — calling it multiple times
        is safe.
        """
        self._shutdown.set()

        # Process any remaining queued jobs — they get a shutdown error.
        while True:
            try:
                job = self._queue.get_nowait()
            except Empty:
                break
            self._cancel_job(job)

        # Wake waiting workers whose jobs were already evicted from the
        # pending list (i.e. their wait timed out but they haven't
        # raised yet — the race is handled in wrap()).
        with self._pending_lock:
            for job in self._pending_jobs:
                if job.exception is None:
                    job.exception = DispatcherShutdownError("Dispatcher shut down")
                job.event.set()
            self._pending_jobs.clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process_job(self, job: _DispatchJob) -> None:
        if not job.try_claim():
            # Already claimed — either cancelled by worker timeout or
            # already running (should not happen for a freshly dequeued
            # job, but guard defensively).
            return
        try:
            job.result = job.func(*job.args, **job.kwargs)
        except BaseException as exc:
            job.exception = exc
        finally:
            job.mark_completed()
            job.event.set()

    def _cancel_job(self, job: _DispatchJob) -> None:
        """Mark a job as failed due to shutdown."""
        if not job.cancel():
            return  # already claimed or completed
        job.exception = DispatcherShutdownError("Dispatcher shut down")
        job.event.set()


def create_ida_dispatcher() -> IdaUiDispatcher | IdaHeadlessDispatcher:
    """Create the appropriate dispatcher for the current IDA mode."""
    if is_ida_headless():
        return IdaHeadlessDispatcher()
    return IdaUiDispatcher()
