"""Tests for rikugan.memory.notes — frontmatter / wiki-link / address extraction."""

from __future__ import annotations

import os
import tempfile
import textwrap
import unittest

from rikugan.memory.notes import (
    extract_inline_addresses,
    extract_inline_tags,
    list_notes,
    parse_note,
)

SAMPLE_NOTE = textwrap.dedent(
    """
    ---
    title: Network Communication
    genre: networking
    tags: [c2, http]
    addresses: 0x401000, 0x401100
    related: [[c2-endpoints]], [[http-stack]]
    ---

    # Network Communication

    > Addresses: 0x402000, 0x401000
    > Genre: #networking
    > Related: [[dns-resolution]]

    ## Summary

    The binary uses HTTP POST to ``/api/v2/report`` with RC4-encrypted body.

    ## Key Functions

    | Address | Name | Purpose |
    | --- | --- | --- |
    | `0x401000` | `send_beacon` | sends beacon |
    | `0x401100` | `recv_command` | receives command |

    ## Detailed Analysis

    The function at [[http-stack]] uses #crypto before writing to `0x402000`.
    """
).strip()


class TestParseNote(unittest.TestCase):
    def setUp(self):
        self.parsed = parse_note(SAMPLE_NOTE, path="/tmp/networking/network-communication.md")

    def test_frontmatter_parsed(self):
        self.assertEqual(self.parsed.title, "Network Communication")
        self.assertEqual(self.parsed.genre, "networking")
        # We do not parse YAML lists inside tags; the loose parser keeps
        # the raw value. Ingestion code path splits on commas itself.
        self.assertIn("c2", self.parsed.tags)
        self.assertIn("http", self.parsed.tags)
        self.assertIn("networking", self.parsed.tags)  # from bq `#networking`

    def test_addresses_union_no_dupes(self):
        # fm: 0x401000 + 0x401100, bq: 0x402000 + 0x401000, body: 0x402000
        self.assertIn("0x401000", self.parsed.addresses)
        self.assertIn("0x401100", self.parsed.addresses)
        self.assertIn("0x402000", self.parsed.addresses)
        self.assertEqual(self.parsed.addresses.count("0x401000"), 1)

    def test_wiki_links(self):
        # Three unique wiki-links across related bq + body
        self.assertIn("c2-endpoints", self.parsed.wiki_links)
        self.assertIn("http-stack", self.parsed.wiki_links)
        self.assertIn("dns-resolution", self.parsed.wiki_links)

    def test_sections_present(self):
        self.assertIn("Summary", self.parsed.sections)
        self.assertIn("Key Functions", self.parsed.sections)

    def test_entity_id_stable(self):
        self.assertEqual(self.parsed.entity_id(), "note:network-communication")

    def test_function_entity_refs(self):
        refs = self.parsed.function_entity_refs()
        self.assertIn("func:0x401000", refs)
        self.assertIn("func:0x401100", refs)
        self.assertIn("func:0x402000", refs)


class TestParseNoteFallbacks(unittest.TestCase):
    def test_no_frontmatter_uses_bq_only(self):
        body = "# X\n\n> Addresses: 0x401000\n\n## Summary\nfoo"
        p = parse_note(body, path="/x/y.md")
        self.assertEqual(p.title, "X")
        self.assertEqual(p.addresses, ["0x401000"])

    def test_no_frontmatter_no_title_uses_filename(self):
        p = parse_note("just text\n## Summary\nstuff", path="/x/foo-bar.md")
        self.assertEqual(p.title, "Foo Bar")

    def test_genre_from_directory(self):
        p = parse_note("# t\n\n## Summary\nx", path="/notes/functions/handler.md")
        self.assertEqual(p.genre, "functions")


class TestInlineExtractors(unittest.TestCase):
    def test_extract_addresses(self):
        text = "patch at 0x401000 and 0xabcdef (5 chars)"
        addrs = extract_inline_addresses(text)
        self.assertIn("0x401000", addrs)
        self.assertIn("0xabcdef", addrs)

    def test_extract_tags(self):
        text = "uses #crypto and #obfuscation; #Malware-2 also valid"
        tags = extract_inline_tags(text)
        self.assertIn("crypto", tags)
        self.assertIn("obfuscation", tags)


class TestListNotes(unittest.TestCase):
    def test_lists_files_recursively_excluding_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes_dir = os.path.join(tmp, "notes")
            os.makedirs(os.path.join(notes_dir, "functions"))
            os.makedirs(os.path.join(notes_dir, "reports"))
            with open(os.path.join(notes_dir, "index.md"), "w") as f:
                f.write("# Index\n\n## Summary\nroot")
            with open(os.path.join(notes_dir, "functions", "handler.md"), "w") as f:
                f.write("# handler\n\n## Summary\ndoes X")
            with open(os.path.join(notes_dir, "reports", "report.md"), "w") as f:
                f.write("# final report\n\n## Summary\nreport")
            notes = list_notes(notes_dir)
            self.assertEqual(len(notes), 2)
            titles = {n.title for n in notes}
            self.assertIn("Index", titles)
            self.assertIn("handler", titles)


if __name__ == "__main__":
    unittest.main()
