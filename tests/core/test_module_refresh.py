"""Unit tests for lucnhan.core.module_refresh.

The plugin runs straight from a source checkout (the IDA plugin
directory is a junction to the repo), so an edit made while IDA is
running leaves the session with a mix of module versions:
``lucnhan.constants`` stays the copy cached before the edit, and the
first module that imports a newly added constant from it dies with
``ImportError: cannot import name ... from 'lucnhan.constants'`` even
though the file on disk is already correct.

These tests pin the refresh that heals that skew.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from lucnhan.core.module_refresh import (
    CONSTANTS_MODULE,
    refresh_constants_module,
    refresh_module,
)


class TestRefreshModule(unittest.TestCase):
    """A stale cached module must pick up the current source on refresh."""

    MODULE_NAME = "lucnhan_stale_probe"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._dir = Path(self._tmp.name)
        sys.path.insert(0, str(self._dir))
        self.addCleanup(self._drop_path)

    def _drop_path(self) -> None:
        if str(self._dir) in sys.path:
            sys.path.remove(str(self._dir))

    def _write(self, source: str) -> None:
        (self._dir / f"{self.MODULE_NAME}.py").write_text(source, encoding="utf-8")

    def _import(self) -> object:
        module = importlib.import_module(self.MODULE_NAME)
        self.addCleanup(self._forget_module)
        return module

    def _forget_module(self) -> None:
        sys.modules.pop(self.MODULE_NAME, None)

    def test_cached_module_hides_a_name_added_after_the_import(self) -> None:
        """The failure this fixes: the cached module predates the edit."""
        self._write("OLD = 1\n")
        self._import()
        self._write("OLD = 1\nNEW = 2\n")

        with self.assertRaises(ImportError):
            exec(f"from {self.MODULE_NAME} import NEW", {})

    def test_refresh_makes_the_added_name_importable(self) -> None:
        self._write("OLD = 1\n")
        self._import()
        self._write("OLD = 1\nNEW = 2\n")

        self.assertTrue(refresh_module(self.MODULE_NAME))

        namespace: dict = {}
        exec(f"from {self.MODULE_NAME} import NEW", namespace)
        self.assertEqual(namespace["NEW"], 2)

    def test_refresh_keeps_the_module_object_identity(self) -> None:
        """The refresh must update the cached module in place.

        Replacing the module object would leave every module that already
        did ``from ..constants import X`` bound to the old namespace.
        """
        self._write("OLD = 1\n")
        module = self._import()
        self._write("OLD = 1\nNEW = 2\n")

        refresh_module(self.MODULE_NAME)

        self.assertIs(sys.modules[self.MODULE_NAME], module)
        self.assertEqual(module.NEW, 2)

    def test_refresh_of_an_unimported_module_is_a_no_op(self) -> None:
        self.assertFalse(refresh_module("lucnhan_module_that_was_never_imported"))


class TestRefreshConstantsModule(unittest.TestCase):
    """The constants module the IDA panel refreshes before it builds."""

    def test_constants_module_is_reloaded_in_place(self) -> None:
        module = sys.modules.get(CONSTANTS_MODULE)
        self.assertIsNotNone(module)

        self.assertTrue(refresh_constants_module())

        self.assertIs(sys.modules[CONSTANTS_MODULE], module)
        self.assertEqual(module.SUBAGENT_CONTEXT_WINDOW, 256_000)


if __name__ == "__main__":
    unittest.main()
