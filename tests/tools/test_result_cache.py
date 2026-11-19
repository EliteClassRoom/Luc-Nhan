"""Behaviour of the in-memory tool result cache."""

from __future__ import annotations

import unittest

from lucnhan.tools.cache import ToolResultCache


class TestToolResultCache(unittest.TestCase):
    def test_round_trip_hits(self) -> None:
        cache = ToolResultCache()
        self.assertIsNone(cache.get("decompile_function", {"address": "0x40"}))
        cache.put("decompile_function", {"address": "0x40"}, "body")
        self.assertEqual(cache.get("decompile_function", {"address": "0x40"}), "body")
        self.assertEqual(cache.stats, (1, 1))

    def test_argument_order_does_not_matter(self) -> None:
        cache = ToolResultCache()
        cache.put("xrefs_to", {"address": "0x40", "limit": 5}, "body")
        self.assertEqual(cache.get("xrefs_to", {"limit": 5, "address": "0x40"}), "body")

    def test_non_cacheable_tool_is_ignored(self) -> None:
        cache = ToolResultCache()
        cache.put("list_strings", {"limit": 1}, "body")
        self.assertIsNone(cache.get("list_strings", {"limit": 1}))
        self.assertEqual(cache.size, 0)

    def test_invalidate_flushes(self) -> None:
        cache = ToolResultCache()
        cache.put("get_function_name", {"address": "0x40"}, "body")
        cache.invalidate()
        self.assertIsNone(cache.get("get_function_name", {"address": "0x40"}))

    def test_oldest_entry_is_evicted_past_the_ceiling(self) -> None:
        """A read-heavy session must not keep every result forever."""
        cache = ToolResultCache(max_entries=3)
        for addr in ("0x1", "0x2", "0x3"):
            cache.put("decompile_function", {"address": addr}, addr)
        # Touch the oldest so the second entry becomes the eviction victim.
        self.assertEqual(cache.get("decompile_function", {"address": "0x1"}), "0x1")
        cache.put("decompile_function", {"address": "0x4"}, "0x4")
        self.assertEqual(cache.size, 3)
        self.assertIsNotNone(cache.get("decompile_function", {"address": "0x1"}))
        self.assertIsNone(cache.get("decompile_function", {"address": "0x2"}))

    def test_ttl_expiry(self) -> None:
        cache = ToolResultCache(ttl=-1.0)
        cache.put("get_binary_info", {}, "body")
        self.assertIsNone(cache.get("get_binary_info", {}))

    def test_ceiling_is_at_least_one(self) -> None:
        cache = ToolResultCache(max_entries=0)
        cache.put("get_binary_info", {}, "a")
        cache.put("get_binary_info", {"x": 1}, "b")
        self.assertEqual(cache.size, 1)


if __name__ == "__main__":
    unittest.main()
