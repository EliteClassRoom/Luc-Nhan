"""Tests for iris.core.thread_safety."""

from __future__ import annotations

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.core.thread_safety import idasync, run_in_background


class TestIdasync(unittest.TestCase):
    def test_direct_call_returns_value(self):
        @idasync
        def add(a, b):
            return a + b

        self.assertEqual(add(2, 3), 5)

    def test_preserves_function_name(self):
        @idasync
        def my_function():
            pass

        self.assertEqual(my_function.__name__, "my_function")

    def test_propagates_exception(self):
        @idasync
        def raises():
            raise ValueError("test error")

        with self.assertRaises(ValueError) as ctx:
            raises()
        self.assertIn("test error", str(ctx.exception))

    def test_handles_kwargs(self):
        @idasync
        def greet(name, greeting="hello"):
            return f"{greeting} {name}"

        self.assertEqual(greet("world"), "hello world")
        self.assertEqual(greet("world", greeting="hi"), "hi world")


class TestRunInBackground(unittest.TestCase):
    def test_runs_function_in_thread(self):
        result = []

        def worker():
            result.append(threading.current_thread().name)

        t = run_in_background(worker)
        t.join(timeout=5)
        self.assertEqual(len(result), 1)
        self.assertNotEqual(result[0], threading.main_thread().name)

    def test_thread_is_daemon(self):
        def noop():
            pass

        t = run_in_background(noop)
        self.assertTrue(t.daemon)
        t.join(timeout=5)

    def test_passes_args_and_kwargs(self):
        result = []

        def worker(a, b, c=10):
            result.append(a + b + c)

        t = run_in_background(worker, 1, 2, c=3)
        t.join(timeout=5)
        self.assertEqual(result, [6])


if __name__ == "__main__":
    unittest.main()
