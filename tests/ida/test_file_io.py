"""Tests for the agent-callable file_io tools."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.ida.tools import file_io
from rikugan.tools.registry import ToolRegistry


def _with_root(root: str):
    return patch.object(file_io, "get_database_path", return_value=os.path.join(root, "x.idb"))


def _build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_module(file_io)
    return registry


class TestReadFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.idb = os.path.join(self.tmp, "x.idb")
        with open(self.idb, "w", encoding="utf-8") as f:
            f.write("idb placeholder")
        os.makedirs(os.path.join(self.tmp, "notes"), exist_ok=True)
        with open(os.path.join(self.tmp, "notes", "ok.md"), "w", encoding="utf-8") as f:
            f.write("hello world")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reads_relative_utf8(self):
        with _with_root(self.tmp):
            result = file_io.read_file("notes/ok.md", max_chars=200)
        self.assertIn("hello world", result)
        self.assertIn("notes/ok.md", result)

    def test_rejects_absolute_path(self):
        with _with_root(self.tmp):
            result = file_io.read_file("/etc/hosts")
        self.assertIn("Error", result)

    def test_rejects_drive_letter(self):
        with _with_root(self.tmp):
            result = file_io.read_file("C:/Windows/system.ini")
        self.assertIn("Error", result)

    def test_rejects_traversal(self):
        with _with_root(self.tmp):
            result = file_io.read_file("../escape.md")
        self.assertIn("Error", result)
        self.assertIn("traversal", result)

    def test_rejects_missing_file(self):
        with _with_root(self.tmp):
            result = file_io.read_file("notes/missing.md")
        self.assertIn("Error", result)
        self.assertIn("not found", result)

    def test_rejects_non_utf8(self):
        with open(os.path.join(self.tmp, "notes", "bin.md"), "wb") as f:
            f.write(b"\xff\xfe\x00\x01")
        with _with_root(self.tmp):
            result = file_io.read_file("notes/bin.md")
        self.assertIn("Error", result)
        self.assertIn("UTF-8", result)

    def test_rejects_directory(self):
        with _with_root(self.tmp):
            result = file_io.read_file("notes")
        self.assertIn("Error", result)

    def test_caps_large_file(self):
        with open(os.path.join(self.tmp, "notes", "huge.md"), "w", encoding="utf-8") as f:
            f.write("a" * (file_io._READ_MAX_BYTES + 1))
        with _with_root(self.tmp):
            result = file_io.read_file("notes/huge.md")
        self.assertIn("too large", result)

    def test_max_chars_zero_clamps_to_one(self):
        # Plan §3.31: max_chars is clamped to 1..12000. A 0 input
        # must NOT fall through to the 12000-char limit.
        target = os.path.join(self.tmp, "notes", "zero.md")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("a" * 100)
        with _with_root(self.tmp):
            result = file_io.read_file("notes/zero.md", max_chars=0)
        # max_chars=0 clamps to 1; the first line of the returned
        # body must be a single 'a', not the full 100-char payload.
        body = result.split(":\n", 1)[1]
        self.assertTrue(body.startswith("a\n…(truncated)…"))
        self.assertEqual(len(body.splitlines()[0]), 1)
class TestWriteFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.idb = os.path.join(self.tmp, "x.idb")
        with open(self.idb, "w", encoding="utf-8") as f:
            f.write("idb placeholder")
        os.makedirs(os.path.join(self.tmp, "notes"), exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_utf8(self):
        with _with_root(self.tmp):
            result = file_io.write_file("notes/out.md", "hello\nworld")
        self.assertIn("Wrote", result)
        with open(os.path.join(self.tmp, "notes", "out.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "hello\nworld")

    def test_rejects_existing_without_overwrite(self):
        target = os.path.join(self.tmp, "notes", "present.md")
        with open(target, "w", encoding="utf-8") as f:
            f.write("original")
        with _with_root(self.tmp):
            result = file_io.write_file("notes/present.md", "new")
        self.assertIn("Error", result)
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "original")

    def test_overwrite_replaces(self):
        target = os.path.join(self.tmp, "notes", "present.md")
        with open(target, "w", encoding="utf-8") as f:
            f.write("original")
        with _with_root(self.tmp):
            result = file_io.write_file("notes/present.md", "new", overwrite=True)
        self.assertIn("Wrote", result)
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "new")

    def test_rejects_traversal(self):
        with _with_root(self.tmp):
            result = file_io.write_file("../escape.md", "x")
        self.assertIn("Error", result)

    def test_rejects_absolute_path(self):
        with _with_root(self.tmp):
            result = file_io.write_file("/etc/passwd", "x")
        self.assertIn("Error", result)

    def test_rejects_oversize_content(self):
        with _with_root(self.tmp):
            result = file_io.write_file("notes/big.md", "a" * (file_io._WRITE_MAX_BYTES + 1))
        self.assertIn("Error", result)
        self.assertIn("too large", result)

    def test_writes_atomic_failure_leaves_no_partial(self):
        """Force a real ``mkstemp`` + ``atomic_replace`` failure and
        confirm the temp file is cleaned up.
        """
        from rikugan.ida.tools import file_io as fio
        from unittest.mock import patch as _patch

        with _with_root(self.tmp), _patch.object(
            fio, "atomic_replace", side_effect=OSError("forced failure")
        ):
            result = file_io.write_file("notes/atomic.md", "x")
        self.assertIn("Error", result)
        self.assertIn("forced failure", result)
        leftovers = [
            root
            for root, _dirs, files in os.walk(self.tmp)
            for name in files
            if name.startswith(".rikugan-write-")
        ]
        self.assertEqual(leftovers, [])

    def test_write_file_is_mutating(self):
        registry = _build_registry()
        for tool in registry.list_available_tools():
            if tool.name == "write_file":
                self.assertTrue(tool.mutating)
                return
        self.fail("write_file not registered")

    def test_read_file_not_mutating(self):
        registry = _build_registry()
        for tool in registry.list_available_tools():
            if tool.name == "read_file":
                self.assertFalse(tool.mutating)
                return
        self.fail("read_file not registered")


if __name__ == "__main__":
    unittest.main()
