"""Tests for microcode optimizer classes and lifecycle."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

# Force-reload the module so it picks up the real stub base classes
# from our updated ida_mock (optinsn_t, optblock_t as real classes).
import importlib

import lucnhan.ida.tools.microcode_optim as _mod

importlib.reload(_mod)

# Import from the reloaded module object (not the old cached names)
# to ensure we get the correct dict/class references.
NopOptimizer = _mod.NopOptimizer
DynamicInsnOptimizer = _mod.DynamicInsnOptimizer
DynamicBlockOptimizer = _mod.DynamicBlockOptimizer
compile_optimizer = _mod.compile_optimizer
build_optimizer_namespace = _mod.build_optimizer_namespace
installed_optimizers = _mod.installed_optimizers
remove_optimizer = _mod.remove_optimizer


class TestNopOptimizer(unittest.TestCase):
    def test_nops_target_address(self):
        opt = NopOptimizer("test_nop", target_eas={0x1000, 0x1004}, func_ea=0x1000)
        ins = MagicMock(ea=0x1000)
        blk = MagicMock()
        result = opt.func(blk, ins, 0)
        self.assertEqual(result, 1)
        self.assertEqual(opt.applied_count, 1)

    def test_skips_non_target(self):
        opt = NopOptimizer("test_nop", target_eas={0x1000}, func_ea=0x1000)
        ins = MagicMock(ea=0x2000)
        blk = MagicMock()
        result = opt.func(blk, ins, 0)
        self.assertEqual(result, 0)
        self.assertEqual(opt.applied_count, 0)


class TestDynamicInsnOptimizer(unittest.TestCase):
    def test_calls_user_function(self):
        fn = MagicMock(return_value=1)
        opt = DynamicInsnOptimizer("test", "desc", fn)
        blk, ins = MagicMock(), MagicMock()
        result = opt.func(blk, ins, 0)
        self.assertEqual(result, 1)
        fn.assert_called_once_with(blk, ins)

    def test_catches_user_exceptions(self):
        fn = MagicMock(side_effect=RuntimeError("boom"))
        opt = DynamicInsnOptimizer("test", "desc", fn)
        result = opt.func(MagicMock(), MagicMock(), 0)
        self.assertEqual(result, 0)

    def test_handles_none_return(self):
        fn = MagicMock(return_value=None)
        opt = DynamicInsnOptimizer("test", "desc", fn)
        result = opt.func(MagicMock(), MagicMock(), 0)
        self.assertEqual(result, 0)


class TestDynamicBlockOptimizer(unittest.TestCase):
    def test_calls_user_function(self):
        fn = MagicMock(return_value=1)
        opt = DynamicBlockOptimizer("test", "desc", fn)
        result = opt.func(MagicMock())
        self.assertEqual(result, 1)
        fn.assert_called_once()

    def test_catches_user_exceptions(self):
        fn = MagicMock(side_effect=ValueError("bad"))
        opt = DynamicBlockOptimizer("test", "desc", fn)
        result = opt.func(MagicMock())
        self.assertEqual(result, 0)


class TestCompileOptimizer(unittest.TestCase):
    def test_compiles_valid_code(self):
        code = "def optimize(blk, ins):\n    return 0"
        fn = compile_optimizer("test", code)
        self.assertTrue(callable(fn))

    def test_rejects_missing_optimize(self):
        code = "def helper():\n    pass"
        with self.assertRaises(ValueError) as ctx:
            compile_optimizer("test", code)
        self.assertIn("optimize", str(ctx.exception))

    def test_dedents_code(self):
        code = "    def optimize(blk, ins):\n        return 1"
        fn = compile_optimizer("test", code)
        self.assertTrue(callable(fn))


class TestBuildOptimizerNamespace(unittest.TestCase):
    def test_includes_ida_hexrays(self):
        ns = build_optimizer_namespace()
        self.assertIn("ida_hexrays", ns)

    def test_includes_builtins(self):
        ns = build_optimizer_namespace()
        self.assertIn("__builtins__", ns)


class TestRemoveOptimizer(unittest.TestCase):
    @property
    def _optimizers(self):
        """Always access the current module's dict (survives reimport)."""
        return _mod.installed_optimizers

    def setUp(self):
        self._saved = dict(self._optimizers)

    def tearDown(self):
        self._optimizers.clear()
        self._optimizers.update(self._saved)

    def test_removes_from_registry(self):
        mock_opt = MagicMock()
        self._optimizers["test_remove"] = mock_opt
        _mod.remove_optimizer("test_remove")
        self.assertNotIn("test_remove", self._optimizers)
        mock_opt.remove.assert_called_once()

    def test_missing_name_is_noop(self):
        _mod.remove_optimizer("nonexistent_optimizer_xyz")

    def test_handles_remove_failure(self):
        mock_opt = MagicMock()
        mock_opt.remove.side_effect = RuntimeError("already removed")
        self._optimizers["test_fail"] = mock_opt
        _mod.remove_optimizer("test_fail")  # should not raise
        self.assertNotIn("test_fail", self._optimizers)


if __name__ == "__main__":
    unittest.main()
