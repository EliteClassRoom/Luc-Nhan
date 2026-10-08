"""Tests for rikugan.memory.paths.

Pure unit tests; no Qt, no fixtures.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from rikugan.memory.paths import (
    algo_entity_id,
    capability_entity_id,
    derive_binary_id,
    ensure_safe_relative_path,
    entity_id_from_address,
    extract_addresses,
    function_entity_id,
    global_entity_id,
    import_entity_id,
    ioc_entity_id,
    knowledge_paths,
    normalize_address,
    note_entity_id,
    relation_id,
    report_entity_id,
    string_entity_id,
    struct_entity_id,
)


class TestNormalizeAddress(unittest.TestCase):
    def test_int_input(self):
        self.assertEqual(normalize_address(0x401000), "0x401000")

    def test_hex_string_with_prefix(self):
        self.assertEqual(normalize_address("0x401000"), "0x401000")

    def test_hex_string_without_prefix(self):
        self.assertEqual(normalize_address("401000"), "0x401000")

    def test_uppercase_normalized(self):
        self.assertEqual(normalize_address("0X401ABC"), "0x401abc")

    def test_none_returns_empty(self):
        self.assertEqual(normalize_address(None), "")

    def test_invalid_returns_empty(self):
        self.assertEqual(normalize_address("zzz"), "")


class TestEntityIdBuilders(unittest.TestCase):
    def test_function_id(self):
        self.assertEqual(function_entity_id(0x401000), "func:0x401000")
        self.assertEqual(function_entity_id("0x401000"), "func:0x401000")

    def test_string_id(self):
        self.assertEqual(string_entity_id(0x408120), "string:0x408120")

    def test_global_id(self):
        self.assertEqual(global_entity_id(0x409000), "global:0x409000")

    def test_import_id_with_unsafe_chars(self):
        eid = import_entity_id("wininet.dll", "HttpSendRequestA")
        self.assertTrue(eid.startswith("import:wininet.dll:"))

    def test_struct_id_spaces_underscored(self):
        self.assertEqual(struct_entity_id("Malware Config"), "struct:Malware_Config")

    def test_struct_id_empty(self):
        self.assertEqual(struct_entity_id(""), "struct:unnamed")

    def test_algo_id(self):
        self.assertEqual(algo_entity_id("rc4_ksa"), "algo:rc4_ksa")

    def test_capability_id(self):
        self.assertEqual(capability_entity_id("c2 communication"), "capability:c2_communication")

    def test_ioc_id(self):
        eid = ioc_entity_id("domain", "example.com")
        self.assertTrue(eid.startswith("ioc:domain:"))
        self.assertTrue(eid.endswith("example.com"))

    def test_note_id(self):
        self.assertEqual(note_entity_id("network-communication"), "note:network-communication")

    def test_report_id(self):
        self.assertEqual(report_entity_id("final"), "report:final")

    def test_relation_id_deterministic(self):
        self.assertEqual(
            relation_id("func:0x401000", "calls", "func:0x401100"),
            "rel:func:0x401000:calls:func:0x401100",
        )

    def test_entity_id_from_address_defaults_function(self):
        self.assertEqual(entity_id_from_address(0x401000), "func:0x401000")


class TestExtractAddresses(unittest.TestCase):
    def test_extracts_multiple(self):
        text = "Found at 0x401000 and 0x401100 (also 0X401abc), not at 0x999 (too short)."
        addrs = extract_addresses(text)
        self.assertIn("0x401000", addrs)
        self.assertIn("0x401100", addrs)
        self.assertIn("0x401abc", addrs)

    def test_dedup_preserves_order(self):
        text = "0x401000 ... 0x401000 ... 0x401100"
        addrs = extract_addresses(text)
        self.assertEqual(addrs.count("0x401000"), 1)
        self.assertEqual(addrs.index("0x401000"), 0)

    def test_empty_text(self):
        self.assertEqual(extract_addresses(""), [])

    def test_short_hex_not_matched(self):
        # 4-char minimum per path regex
        self.assertEqual(extract_addresses("0x1ff"), [])


class TestKnowledgePaths(unittest.TestCase):
    def test_derive_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            idb_path = os.path.join(tmp, "sample.i64")
            paths = knowledge_paths(idb_path)
            self.assertEqual(paths.notes_dir, os.path.join(tmp, "notes"))
            self.assertEqual(paths.kb_dir, os.path.join(tmp, ".rikugan-kb"))
            self.assertEqual(paths.reports_dir, os.path.join(tmp, "notes", "reports"))
            self.assertTrue(paths.binary_id.startswith("sample.i64-"))
            self.assertEqual(len(paths.binary_id.split("-")[-1]), 12)

    def test_derive_binary_id_with_instance(self):
        bid = derive_binary_id("/whatever", db_instance_id="abc123")
        self.assertEqual(bid, "abc123")

    def test_derive_binary_id_path_normalized(self):
        a = derive_binary_id(r"C:\Samples\Foo.i64")
        b = derive_binary_id(r"c:\samples\foo.i64")
        self.assertEqual(a, b)

    def test_ensure_creates_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = knowledge_paths(os.path.join(tmp, "x.i64"))
            paths.ensure()
            self.assertTrue(os.path.isdir(paths.notes_dir))
            self.assertTrue(os.path.isdir(paths.kb_dir))
            self.assertTrue(os.path.isdir(paths.reports_dir))

    def test_ensure_empty_idb_path_raises(self):
        with self.assertRaises(ValueError):
            knowledge_paths("")


class TestPathSafety(unittest.TestCase):
    def test_traversal_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                ensure_safe_relative_path(tmp, "../escape.txt")

    def test_safe_path_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = ensure_safe_relative_path(tmp, "sub/file.txt")
            # Returned path is normalized to native separators and ends
            # inside ``tmp`` — exact byte match is platform-dependent.
            self.assertTrue(os.path.normpath(p).endswith(os.path.normpath("sub/file.txt")))
            self.assertTrue(os.path.normpath(p).startswith(os.path.normpath(tmp)))


if __name__ == "__main__":
    unittest.main()
