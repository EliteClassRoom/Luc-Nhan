"""Tests for lucnhan.ida.dispatch — headless dispatcher lifecycle."""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()


class TestHeadlessDispatcher(unittest.TestCase):
    """Tests for IdaHeadlessDispatcher queue-based dispatch."""

    def setUp(self):
        from lucnhan.ida.dispatch import IdaHeadlessDispatcher

        self.dispatcher = IdaHeadlessDispatcher()

    def test_wrap_main_thread_direct_call(self):
        """On the main thread, wrap() should call the function directly."""
        called = []

        @self.dispatcher.wrap
        def identity(x):
            called.append(x)
            return x

        result = identity(42)
        self.assertEqual(result, 42)
        self.assertEqual(called, [42])

    def test_wrap_worker_thread_enqueues(self):
        """On a worker thread, wrap() should enqueue and wait for pump."""
        results = []

        @self.dispatcher.wrap
        def double(x):
            return x * 2

        def worker():
            results.append(double(21))

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        processed = self.dispatcher.pump_once(timeout=2.0)
        self.assertTrue(processed)

        t.join(timeout=2.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(results, [42])

    def test_pump_all(self):
        """pump_all() should process multiple queued jobs."""

        @self.dispatcher.wrap
        def add(a, b):
            return a + b

        results = []

        def worker(arg):
            results.append(add(arg, arg))

        threads = []
        for i in range(5):
            t = threading.Thread(target=worker, args=(i,), daemon=True)
            t.start()
            threads.append(t)

        count = self.dispatcher.pump_all(timeout=0.5)
        self.assertEqual(count, 5)

        for t in threads:
            t.join(timeout=2.0)

        self.assertEqual(len(results), 5)
        self.assertCountEqual(results, [0, 2, 4, 6, 8])

    def test_shutdown_rejects_new_jobs(self):
        """After shutdown, new wrap() calls should raise DispatcherShutdownError."""
        from lucnhan.ida.dispatch import DispatcherShutdownError

        self.dispatcher.request_shutdown()

        @self.dispatcher.wrap
        def noop():
            pass

        with self.assertRaises(DispatcherShutdownError):
            noop()

    def test_shutdown_wakes_blocked_workers(self):
        """Workers blocked on the pump should be woken on shutdown."""
        from lucnhan.ida.dispatch import DispatcherShutdownError

        errors = []

        @self.dispatcher.wrap
        def slow():
            pass

        def worker():
            try:
                slow()
            except DispatcherShutdownError as e:
                errors.append(e)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        # Give the worker time to enqueue
        time.sleep(0.1)
        self.dispatcher.request_shutdown()

        t.join(timeout=2.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(errors), 1)

    def test_exception_propagated_to_worker(self):
        """Exceptions raised in the handler should be propagated to worker."""

        @self.dispatcher.wrap
        def fail():
            raise ValueError("test error")

        errors = []

        def worker():
            try:
                fail()
            except ValueError as e:
                errors.append(str(e))

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        self.dispatcher.pump_once(timeout=2.0)
        t.join(timeout=2.0)

        self.assertEqual(errors, ["test error"])

    def test_is_shutdown_requested(self):
        """is_shutdown_requested() should mirror request_shutdown()."""
        self.assertFalse(self.dispatcher.is_shutdown_requested())
        self.dispatcher.request_shutdown()
        self.assertTrue(self.dispatcher.is_shutdown_requested())

    def test_shutdown_wakes_exactly_with_shutdown_error(self):
        """Workers must raise DispatcherShutdownError, not return None."""
        from lucnhan.ida.dispatch import DispatcherShutdownError

        errors = []

        @self.dispatcher.wrap
        def nop():
            pass

        def worker():
            try:
                result = nop()
                # If we get here with a result, that's the bug:
                # timed-out jobs returning None as success.
                errors.append(f"unexpected_success={result}")
            except DispatcherShutdownError:
                errors.append("shutdown")

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.05)
        self.dispatcher.request_shutdown()
        t.join(timeout=2.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(errors, ["shutdown"])

    def test_job_timeout_raises_timeout_error(self):
        """A job that is never pumped must raise DispatcherTimeoutError."""
        # Override the default timeout to something small for testing
        import lucnhan.ida.dispatch as disp_mod
        from lucnhan.ida.dispatch import DispatcherTimeoutError

        old_timeout = disp_mod._DEFAULT_JOB_TIMEOUT
        disp_mod._DEFAULT_JOB_TIMEOUT = 0.2

        try:
            d = disp_mod.IdaHeadlessDispatcher()
            errors = []

            @d.wrap
            def never_called():
                pass

            def worker():
                try:
                    result = never_called()
                    errors.append(f"unexpected_result={result}")
                except DispatcherTimeoutError:
                    errors.append("timeout")

            t = threading.Thread(target=worker, daemon=True)
            t.start()
            t.join(timeout=3.0)
            self.assertFalse(t.is_alive())
            self.assertEqual(errors, ["timeout"])
        finally:
            disp_mod._DEFAULT_JOB_TIMEOUT = old_timeout

    def test_timed_out_job_not_executed_by_pump(self):
        """A timed-out job must be skipped by the pump (not execute later)."""
        import lucnhan.ida.dispatch as disp_mod

        # Give the worker thread a short timeout
        old_timeout = disp_mod._DEFAULT_JOB_TIMEOUT
        disp_mod._DEFAULT_JOB_TIMEOUT = 0.1

        try:
            d = disp_mod.IdaHeadlessDispatcher()
            side_effects = []

            @d.wrap
            def should_not_run():
                side_effects.append("ran")
                return "bad"

            errors = []

            def worker():
                try:
                    should_not_run()
                    errors.append("no_error")
                except disp_mod.DispatcherTimeoutError:
                    errors.append("timeout")

            t = threading.Thread(target=worker, daemon=True)
            t.start()
            t.join(timeout=3.0)
            self.assertFalse(t.is_alive())
            self.assertEqual(errors, ["timeout"])

            # Now pump — the timed-out job should be skipped
            _process = d.pump_once(timeout=0.5)
            # The job was in the queue so pump_once should get it,
            # but it should be skipped (not executed).
            # processed is True because the queue had an item.
            # The pump returns True even after skipping consumed jobs.
            self.assertEqual(side_effects, [])
        finally:
            disp_mod._DEFAULT_JOB_TIMEOUT = old_timeout

    def test_running_job_timeout_waits_for_completion(self):
        """A timeout while the pump is running a slow function must wait
        for completion and return the real result — never None.

        Regression test: the worker thread must not observe job.result
        before the pump has finished setting it.
        """
        import lucnhan.ida.dispatch as disp_mod

        old_timeout = disp_mod._DEFAULT_JOB_TIMEOUT
        # Short timeout so worker fires before the slow function finishes.
        disp_mod._DEFAULT_JOB_TIMEOUT = 0.1

        try:
            d = disp_mod.IdaHeadlessDispatcher()
            # Event to signal the pump has started executing the function.
            started = threading.Event()
            expected_result = "slow_result_42"

            @d.wrap
            def slow_function():
                started.set()
                time.sleep(0.3)  # longer than the timeout
                return expected_result

            errors = []

            def worker():
                try:
                    result = slow_function()
                    errors.append(f"result={result}")
                except Exception as e:
                    errors.append(f"error={type(e).__name__}:{e}")

            t = threading.Thread(target=worker, daemon=True)
            t.start()

            # Wait for the function to be enqueued and started.
            # pump_once will claim the job and begin executing it.
            pump_done = threading.Event()

            def pump():
                d.pump_once(timeout=2.0)
                pump_done.set()

            pump_thread = threading.Thread(target=pump, daemon=True)
            pump_thread.start()

            # Wait for the slow function to signal it has started.
            self.assertTrue(started.wait(timeout=2.0), "Slow function never started")

            t.join(timeout=5.0)
            self.assertFalse(t.is_alive(), "Worker thread still alive after 5s")
            pump_thread.join(timeout=1.0)
            pump_done.wait(timeout=1.0)

            self.assertEqual(len(errors), 1, f"Expected 1 result, got {errors}")
            self.assertEqual(
                errors[0],
                f"result={expected_result}",
                f"Worker should get the real result, not None. Got: {errors}",
            )
        finally:
            disp_mod._DEFAULT_JOB_TIMEOUT = old_timeout
