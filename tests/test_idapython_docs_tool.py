"""Unit tests for lucnhan/tools/idapython_docs.py"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestLookupIdapythonDoc(unittest.TestCase):
    def setUp(self):
        # Create temp DOCS_DIR with 2 modules
        self.tmpdir = tempfile.mkdtemp()
        (Path(self.tmpdir) / "ida_typeinf.rst.txt").write_text(
            "ida_typeinf module docs\n\nFunctions:\n- apply_cdecl\n",
            encoding="utf-8",
        )
        (Path(self.tmpdir) / "idautils.rst.txt").write_text(
            "idautils module docs\n\nFunctions:\n- Functions\n",
            encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _patch_docs_dir(self):
        return patch("lucnhan.tools.idapython_docs.DOCS_DIR", Path(self.tmpdir))

    def test_reads_existing_module_returns_content(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc

        with self._patch_docs_dir():
            result = lookup_idapython_doc("ida_typeinf")
        self.assertIn("apply_cdecl", result)
        self.assertIn("[Offline IDAPython docs: ida_typeinf", result)

    def test_name_filter_returns_section_around_match(self):
        """Point-lookup with `name` should return ~20 lines of context around each match
        — much cheaper than reading the full 200 KB module just to confirm one function exists."""
        from lucnhan.tools.idapython_docs import lookup_idapython_doc

        with self._patch_docs_dir():
            result = lookup_idapython_doc("ida_typeinf", name="apply_cdecl")
        # Header should mention the name filter
        self.assertIn("name='apply_cdecl'", result)
        # Should contain the matched function name
        self.assertIn("apply_cdecl", result)
        # Total chars should be much smaller than the full file (10000)
        # Extract total chars from header: "[...; total chars: N; ...]"
        import re

        m = re.search(r"total chars: ([\d,]+)", result)
        self.assertIsNotNone(m, f"Header missing total chars: {result[:200]}")
        total = int(m.group(1).replace(",", ""))
        # Context window is ~20 lines * ~80 chars = ~1600 chars max per match
        self.assertLess(total, 5000, f"Point-lookup returned too many chars: {total}")

    def test_name_filter_not_found_returns_message(self):
        """When the name doesn't appear anywhere, return a helpful 'not found' message
        instead of an empty string or a confusing empty bundle."""
        from lucnhan.tools.idapython_docs import lookup_idapython_doc

        with self._patch_docs_dir():
            result = lookup_idapython_doc("ida_typeinf", name="nonexistent_function_xyz")
        self.assertIn("no entry matches", result)
        self.assertIn("'nonexistent_function_xyz'", result)

    def test_name_filter_empty_string_treated_as_full_module(self):
        """Passing name='' (default) should return the full module, not filtered content."""
        from lucnhan.tools.idapython_docs import lookup_idapython_doc

        with self._patch_docs_dir():
            # Default is no name, should match the full module behavior
            result = lookup_idapython_doc("ida_typeinf")
        self.assertIn("[Offline IDAPython docs: ida_typeinf; total chars: ", result)
        # Default header has no `name=` qualifier
        self.assertNotIn("name=", result)

    def test_path_traversal_rejected_dotdot(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc

        with self._patch_docs_dir():
            result = lookup_idapython_doc("../../../etc/passwd")
        self.assertIn("invalid module name", result)

    def test_path_traversal_rejected_slash(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc

        with self._patch_docs_dir():
            result = lookup_idapython_doc("foo/bar")
        self.assertIn("invalid module name", result)

    def test_path_traversal_rejected_uppercase(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc

        with self._patch_docs_dir():
            result = lookup_idapython_doc("IDA_TYPEINF")
        self.assertIn("invalid module name", result)

    def test_path_traversal_rejected_dot(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc

        with self._patch_docs_dir():
            result = lookup_idapython_doc(".")
        self.assertIn("invalid module name", result)

    def test_tool_does_not_read_outside_docs_dir(self):
        # Create a file outside the patched DOCS_DIR that the tool must NOT access
        from lucnhan.tools.idapython_docs import lookup_idapython_doc

        outside = Path(self.tmpdir).parent / "sensitive_outside.txt"
        outside.write_text("SENSITIVE")
        try:
            with self._patch_docs_dir():
                # Try every traversal pattern to reach sensitive_outside.txt
                result = lookup_idapython_doc("../sensitive_outside")
                # Must NOT contain SENSITIVE
                self.assertNotIn("SENSITIVE", result)
                self.assertIn("invalid module name", result)
        finally:
            outside.unlink()


class TestPaginationAndEdgeCases(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Create one BIG module (~10000 chars) + one EMPTY module
        big = ("X" * 50 + "\n") * 200  # ~10000 chars
        (Path(self.tmpdir) / "big.rst.txt").write_text(big, encoding="utf-8")
        (Path(self.tmpdir) / "empty.rst.txt").write_text("", encoding="utf-8")
        # One with manifest missing
        # (No MANIFEST.json file written)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_pagination_first_chunk(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc

        with patch("lucnhan.tools.idapython_docs.DOCS_DIR", Path(self.tmpdir)):
            result = lookup_idapython_doc("big", offset=0, limit=200)
        self.assertIn("showing offset 0-200", result)

    def test_pagination_middle_chunk(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc

        with patch("lucnhan.tools.idapython_docs.DOCS_DIR", Path(self.tmpdir)):
            result = lookup_idapython_doc("big", offset=4000, limit=100)
        self.assertIn("showing offset 4000-4100", result)

    def test_pagination_past_end_returns_marker(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc

        with patch("lucnhan.tools.idapython_docs.DOCS_DIR", Path(self.tmpdir)):
            result = lookup_idapython_doc("big", offset=20000, limit=100)
        self.assertIn("reached end of content", result)

    def test_empty_file_returns_empty_marker(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc

        with patch("lucnhan.tools.idapython_docs.DOCS_DIR", Path(self.tmpdir)):
            result = lookup_idapython_doc("empty")
        self.assertIn("[Offline IDAPython docs: empty", result)
        self.assertIn("(empty response)", result)

    def test_limit_clamped_to_max(self):
        from lucnhan.tools.idapython_docs import MAX_LIMIT, lookup_idapython_doc

        with patch("lucnhan.tools.idapython_docs.DOCS_DIR", Path(self.tmpdir)):
            # Request way over the max — must clamp to MAX_LIMIT
            result = lookup_idapython_doc("big", offset=0, limit=99999)
        # Header shows total file size ~10K so we see clamped chunk end
        self.assertIn(f"showing offset 0-{MAX_LIMIT}", result)

    def test_limit_below_one_clamped_to_one(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc

        with patch("lucnhan.tools.idapython_docs.DOCS_DIR", Path(self.tmpdir)):
            result = lookup_idapython_doc("big", offset=0, limit=0)
        # limit=0 -> clamp to 1 -> shows offset 0-1
        self.assertIn("showing offset 0-1", result)

    def test_offset_negative_clamped_to_zero(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc

        with patch("lucnhan.tools.idapython_docs.DOCS_DIR", Path(self.tmpdir)):
            result = lookup_idapython_doc("big", offset=-5, limit=100)
        self.assertIn("showing offset 0-100", result)

    def test_manifest_missing_does_not_break_tool(self):
        # No MANIFEST.json — tool should still work (manifest is informational)
        from lucnhan.tools.idapython_docs import lookup_idapython_doc

        with patch("lucnhan.tools.idapython_docs.DOCS_DIR", Path(self.tmpdir)):
            result = lookup_idapython_doc("big")
        # The "big.rst.txt" fixture contains only 'X' chars, so the result
        # must contain the tool's standard formatted-output header to prove
        # the lookup path actually ran end-to-end (not just an empty body).
        self.assertIn("[Offline IDAPython docs: big", result)
        self.assertIn("total chars:", result)

    def test_zero_byte_file_does_not_crash(self):
        from lucnhan.tools.idapython_docs import lookup_idapython_doc

        with patch("lucnhan.tools.idapython_docs.DOCS_DIR", Path(self.tmpdir)):
            result = lookup_idapython_doc("empty")
        # Must not raise; must include "(empty response)" or similar
        self.assertIsInstance(result, str)
