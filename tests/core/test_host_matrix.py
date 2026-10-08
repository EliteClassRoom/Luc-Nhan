"""Host-matrix tests: verify shared contracts under standalone and IDA-mocked modes.

These tests ensure that host-agnostic abstractions (host detection, thread
dispatch, tool registry dispatch_wrapper) behave correctly under each host
configuration.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Host detection flags (lucnhan.core.host)
# ---------------------------------------------------------------------------

class TestHostDetectionFlags(unittest.TestCase):
    """Verify module-level host flags are consistent with detection functions."""

    def test_flags_are_bool(self):
        """Host flags should be bool, regardless of host stubs."""
        from lucnhan.core.host import HAS_HEXRAYS, IDA_AVAILABLE
        self.assertIsInstance(IDA_AVAILABLE, bool)
        self.assertIsInstance(HAS_HEXRAYS, bool)

    def test_hexrays_requires_ida(self):
        """HAS_HEXRAYS should only be True if IDA_AVAILABLE is True."""
        from lucnhan.core.host import HAS_HEXRAYS, IDA_AVAILABLE
        if HAS_HEXRAYS:
            self.assertTrue(IDA_AVAILABLE)

    def test_mutual_exclusion(self):
        """host_kind returns a single valid value."""
        from lucnhan.core.host import HOST_IDA, HOST_STANDALONE, host_kind
        result = host_kind()
        self.assertIn(result, (HOST_IDA, HOST_STANDALONE))


# ---------------------------------------------------------------------------
# dispatch_wrapper in ToolRegistry
# ---------------------------------------------------------------------------

class TestRegistryDispatchWrapper(unittest.TestCase):
    """Verify dispatch_wrapper is applied at execution time."""

    def _make_registry(self, wrapper=None):
        from lucnhan.tools.registry import ToolRegistry
        return ToolRegistry(dispatch_wrapper=wrapper)

    def _register_echo(self, registry):
        from lucnhan.tools.base import tool
        @tool(name="echo_test", description="Echo for testing")
        def echo_test(text: str) -> str:
            """Echo."""
            return f"echo:{text}"
        registry.register(echo_test._tool_definition)
        return echo_test

    def test_no_wrapper_calls_handler_directly(self):
        """Without dispatch_wrapper, handler is called as-is."""
        reg = self._make_registry(wrapper=None)
        self._register_echo(reg)
        result = reg.execute("echo_test", {"text": "hello"})
        self.assertEqual(result, "echo:hello")

    def test_wrapper_is_applied_at_execution(self):
        """dispatch_wrapper should wrap the handler before execution."""
        call_log = []

        def tracking_wrapper(handler):
            def wrapped(*args, **kwargs):
                call_log.append(("wrapped", handler.__name__))
                return handler(*args, **kwargs)
            return wrapped

        reg = self._make_registry(wrapper=tracking_wrapper)
        self._register_echo(reg)
        result = reg.execute("echo_test", {"text": "world"})
        self.assertEqual(result, "echo:world")
        self.assertEqual(len(call_log), 1)
        self.assertEqual(call_log[0][0], "wrapped")

    def test_wrapper_exception_propagates(self):
        """Exceptions from wrapped handler propagate correctly."""
        def error_wrapper(handler):
            def wrapped(*args, **kwargs):
                raise RuntimeError("dispatch error")
            return wrapped

        reg = self._make_registry(wrapper=error_wrapper)
        self._register_echo(reg)
        from lucnhan.core.errors import ToolError
        with self.assertRaises(ToolError):
            reg.execute("echo_test", {"text": "fail"})

    def test_ida_style_wrapper_pattern(self):
        """Simulate IDA's idasync pattern — wrapper delegates to main thread."""
        main_thread_calls = []

        def mock_idasync(handler):
            """Simulated idasync: records call instead of using execute_sync."""
            def wrapped(*args, **kwargs):
                main_thread_calls.append(handler.__name__)
                return handler(*args, **kwargs)
            return wrapped

        reg = self._make_registry(wrapper=mock_idasync)
        self._register_echo(reg)
        result = reg.execute("echo_test", {"text": "ida"})
        self.assertEqual(result, "echo:ida")
        self.assertIn("echo_test", main_thread_calls)


# ---------------------------------------------------------------------------
# idasync behavior under different host conditions
# ---------------------------------------------------------------------------

class TestIdasyncStandalone(unittest.TestCase):
    """Verify idasync in standalone mode (no host) is a direct call."""

    def test_direct_call_standalone(self):
        from lucnhan.core.thread_safety import idasync

        @idasync
        def add(a, b):
            return a + b

        result = add(3, 4)
        self.assertEqual(result, 7)

    def test_preserves_function_name(self):
        from lucnhan.core.thread_safety import idasync

        @idasync
        def my_func():
            pass

        self.assertEqual(my_func.__name__, "my_func")

    def test_exception_propagation(self):
        from lucnhan.core.thread_safety import idasync

        @idasync
        def failing():
            raise ValueError("test error")

        with self.assertRaises(ValueError):
            failing()


class TestIdasyncWithMockedHosts(unittest.TestCase):
    """Verify idasync dispatches correctly when host flags are mocked."""

    def test_ida_main_thread_direct(self):
        """On IDA main thread, idasync should call directly (no execute_sync)."""
        import lucnhan.core.thread_safety as ts

        mock_kernwin = MagicMock()
        original_ida = ts._IDA_AVAILABLE
        original_kw = getattr(ts, "ida_kernwin", None)
        try:
            ts._IDA_AVAILABLE = True
            ts.ida_kernwin = mock_kernwin

            @ts.idasync
            def my_tool():
                return 42

            # On main thread, should call directly without execute_sync
            result = my_tool()
            self.assertEqual(result, 42)
            mock_kernwin.execute_sync.assert_not_called()
        finally:
            ts._IDA_AVAILABLE = original_ida
            if original_kw is not None:
                ts.ida_kernwin = original_kw



# ---------------------------------------------------------------------------
# @tool decorator is host-agnostic
# ---------------------------------------------------------------------------

class TestToolDecoratorHostAgnostic(unittest.TestCase):
    """Verify @tool creates definitions without any host-specific behavior."""

    def test_tool_creates_definition(self):
        from lucnhan.tools.base import tool

        @tool(name="test_host_agnostic", description="Test tool")
        def test_host_agnostic(address: int) -> str:
            """A test tool."""
            return hex(address)

        defn = test_host_agnostic._tool_definition
        self.assertEqual(defn.name, "test_host_agnostic")
        self.assertEqual(defn.description, "Test tool")
        # Handler should be the raw function (no idasync wrapping)
        result = defn.handler(address=0x1000)
        self.assertEqual(result, "0x1000")

    def test_tool_handler_no_thread_dispatch(self):
        """@tool handler should NOT wrap with idasync — dispatch is registry's job."""
        import lucnhan.core.thread_safety as ts
        from lucnhan.tools.base import tool

        call_log = []
        original_idasync = ts.idasync

        def spy_idasync(func):
            call_log.append(func.__name__)
            return original_idasync(func)

        with patch.object(ts, "idasync", spy_idasync):
            @tool(name="test_no_dispatch", description="No dispatch test")
            def test_no_dispatch() -> str:
                """Test."""
                return "ok"

        # idasync should not have been called during @tool decoration
        self.assertNotIn("test_no_dispatch", call_log)


if __name__ == "__main__":
    unittest.main()
