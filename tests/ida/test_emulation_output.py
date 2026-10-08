"""Behavioral tests for the emulation output layer.

Covers the three pure helpers owned by
:mod:`lucnhan.ida.tools.emulation_output`:

* per-encoding terminator detection (``decode_string_candidates``),
* printable ASCII / UTF-8 / UTF-16LE discovery (``extract_strings``),
* the bounded result rendering (``format_result``), including survival of
  the real ``ToolRegistry`` result cap for a default-sized capture.

No IDA / Unicorn import is required: the module only depends on
``emulation_types``.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from lucnhan.constants import TOOL_RESULT_TRUNCATE_LEN
from lucnhan.ida.tools.emulation_output import (
    OUTPUT_BUDGET_CHARS,
    decode_string_candidates,
    extract_strings,
    format_result,
)
from lucnhan.ida.tools.emulation_types import EmulationResult, StringCandidate


def _texts(candidates: list[StringCandidate], encoding: str) -> list[str]:
    return [c.text for c in candidates if c.encoding == encoding]


# ---------------------------------------------------------------------------
# decode_string_candidates — per-encoding terminators.
# ---------------------------------------------------------------------------


class TestDecodeStringCandidates(unittest.TestCase):
    def test_ascii_candidate_stops_at_single_nul(self) -> None:
        meta = decode_string_candidates(b"hello\x00trailing")
        self.assertEqual(meta["ascii"], "hello")
        self.assertTrue(meta["ascii_terminated"])
        self.assertTrue(meta["utf8_terminated"])
        self.assertEqual(meta["raw_length"], 14)
        self.assertTrue(meta["has_nul_terminator"])

    def test_unterminated_ascii_candidate_is_not_terminated(self) -> None:
        meta = decode_string_candidates(b"abcdef")
        self.assertEqual(meta["ascii"], "abcdef")
        self.assertFalse(meta["ascii_terminated"])
        self.assertFalse(meta["utf8_terminated"])
        self.assertFalse(meta["has_nul_terminator"])

    def test_utf8_only_termination_counts_for_combined_flag(self) -> None:
        # b"\xc3\xa9\x00" is "é" + NUL: ASCII breaks at the lead byte, the
        # wide run is unterminated, only UTF-8 reaches the NUL.
        meta = decode_string_candidates(b"\xc3\xa9\x00")
        self.assertTrue(meta["utf8_terminated"])
        self.assertFalse(meta["ascii_terminated"])
        self.assertFalse(meta["utf16le_terminated"])
        self.assertTrue(meta["has_nul_terminator"])

    def test_aligned_wide_terminator_ends_wide_candidate(self) -> None:
        payload = "hello".encode("utf-16le") + b"\x00\x00\xff\xff"
        meta = decode_string_candidates(payload)
        self.assertEqual(meta["utf16le"], "hello")
        self.assertTrue(meta["utf16le_terminated"])
        # The wide terminator is not an ASCII terminator: the ASCII run is
        # cut at the first NUL high byte, so only the first char survives.
        self.assertEqual(meta["ascii"], "h")

    def test_odd_aligned_nul_is_not_a_wide_terminator(self) -> None:
        # 0x0041 'A', then 00 00 lands at byte offset 1 (odd) — a wide NUL
        # must start at an even offset, so the run continues past it.
        payload = b"A\x00\x00" + "BC".encode("utf-16le")
        meta = decode_string_candidates(payload)
        self.assertFalse(meta["utf16le_terminated"])
        # 0xBC00 / 0x4400 are printable CJK code units, so the whole
        # even-length payload is the candidate — the point is that it was not
        # cut at the odd-offset NUL, and no terminator was invented.
        self.assertEqual(meta["utf16le"], "A䈀䌀")
        self.assertEqual(meta["raw_length"], 7)

    def test_wide_candidate_survives_interior_single_byte_nul(self) -> None:
        # Regression: 41 00 00 42 43 00 44 00 has no aligned double NUL, so
        # the wide candidate must not be cut to "A" at the interior NUL.
        # 00 42 / 00 44 are well-formed units, so the full buffer decodes.
        meta = decode_string_candidates(bytes.fromhex("4100004243004400"))
        self.assertFalse(meta["utf16le_terminated"])
        self.assertEqual(meta["utf16le"], "A䈀CD")
        self.assertEqual(meta["raw_length"], 8)

    def test_wide_candidate_drops_trailing_odd_byte(self) -> None:
        meta = decode_string_candidates(b"\x41\x00\x42\x00\x43")
        self.assertEqual(meta["utf16le"], "AB")
        self.assertFalse(meta["utf16le_terminated"])

    def test_valid_utf8_candidate_decoded(self) -> None:
        meta = decode_string_candidates("héllo\x00".encode())
        self.assertEqual(meta["utf8"], "héllo")
        self.assertTrue(meta["utf8_terminated"])
        # The ASCII run stops at the first non-printable byte.
        self.assertEqual(meta["ascii"], "h")

    def test_malformed_utf8_yields_no_utf8_text(self) -> None:
        meta = decode_string_candidates(b"\xff\xfe\xfa\x00")
        self.assertEqual(meta["utf8"], "")
        self.assertFalse(meta["utf8_terminated"])
        self.assertEqual(meta["raw_length"], 4)
        # 0xFF breaks the run before the NUL, so nothing is terminated.
        self.assertFalse(meta["ascii_terminated"])
        self.assertFalse(meta["has_nul_terminator"])

    def test_malformed_wide_payload_yields_no_wide_text(self) -> None:
        # 0xDC00 is an unpaired low surrogate: it breaks the run before the
        # aligned terminator at offset 4, so no wide candidate is reported.
        meta = decode_string_candidates(b"\x00\xdc\x00\xdc\x00\x00")
        self.assertEqual(meta["utf16le"], "")
        self.assertFalse(meta["utf16le_terminated"])
        self.assertTrue(meta["has_nul_terminator"])

    def test_no_replacement_manufacturing_for_undecodable_bytes(self) -> None:
        meta = decode_string_candidates(b"\xff\xff\xff\xff")
        self.assertNotIn("?", meta["ascii"])
        self.assertEqual(meta["utf16le"], "")

    def test_decode_only_reports_candidates_from_offset_zero(self) -> None:
        # The wide string starts at offset 4: the buffer decode must not
        # present it as the buffer's own candidate.
        meta = decode_string_candidates(b"\x01\x00\x01\x00" + "wide".encode("utf-16le"))
        self.assertEqual(meta["utf16le"], "")
        self.assertFalse(meta["utf16le_terminated"])
        self.assertIn(
            "wide", _texts(extract_strings(0x1000, b"\x01\x00\x01\x00" + "wide".encode("utf-16le")), "utf16le")
        )

    def test_non_printable_wide_characters_end_the_candidate(self) -> None:
        # U+200B is not printable, so it breaks the run instead of joining it.
        meta = decode_string_candidates("wide".encode("utf-16le") + "\u200b".encode("utf-16le"))
        self.assertEqual(meta["utf16le"], "wide")
        self.assertFalse(meta["utf16le_terminated"])

    def test_empty_buffer(self) -> None:
        meta = decode_string_candidates(b"")
        self.assertEqual(meta["raw_length"], 0)
        self.assertEqual(meta["ascii"], "")
        self.assertEqual(meta["utf8"], "")
        self.assertEqual(meta["utf16le"], "")
        self.assertFalse(meta["has_nul_terminator"])


# ---------------------------------------------------------------------------
# extract_strings — printable candidate discovery.
# ---------------------------------------------------------------------------


class TestExtractStrings(unittest.TestCase):
    def test_discovers_ascii_string_with_address_and_termination(self) -> None:
        buf = b"\x00\x00ABCD\x00rest"
        found = extract_strings(0x401000, buf)
        ascii = [c for c in found if c.encoding == "ascii"]
        self.assertIn("ABCD", [c.text for c in ascii])
        first = next(c for c in ascii if c.text == "ABCD")
        self.assertEqual(first.address, 0x401002)
        self.assertTrue(first.terminated)

    def test_discovers_wide_string(self) -> None:
        buf = b"\x00\x00" + "wide".encode("utf-16le") + b"\x00\x00"
        found = extract_strings(0x500000, buf)
        self.assertIn("wide", _texts(found, "utf16le"))
        self.assertEqual(found[0].address, 0x500002)
        self.assertTrue(found[0].terminated)

    def test_discovers_wide_string_between_invalid_units(self) -> None:
        # The units around the wide text are invalid (non-printable control,
        # unpaired high surrogate), not terminators: the run has to start and
        # end at those boundaries.
        buf = b"\x01\x00\x00\xd8\x01\x00" + "hello".encode("utf-16le") + b"\xff\xff\x01\x00"
        found = extract_strings(0x700000, buf)
        self.assertEqual(_texts(found, "utf16le"), ["hello"])
        self.assertEqual(found[0].address, 0x700006)
        self.assertFalse(found[0].terminated)

    def test_discovers_wide_string_before_trailing_garbage(self) -> None:
        # A wide NUL inside a still-unterminated run: the run ends on the
        # first non-printable unit instead of swallowing the rest.
        buf = "wide".encode("utf-16le") + b"\x00\x00" + b"\x99\x9a\xff\xff"
        found = extract_strings(0x700000, buf)
        self.assertEqual(_texts(found, "utf16le"), ["wide"])
        self.assertTrue(found[0].terminated)

    def test_ambiguous_bytes_are_reported_as_both_encodings(self) -> None:
        # Eight printable ASCII bytes are also four valid UTF-16LE code
        # units.  Neither reading is provably the intended one, so both are
        # reported; the wide text is whatever the stdlib decodes.
        buf = b"ABCDEFGH"
        found = extract_strings(0x1000, buf, min_length=4)
        self.assertEqual(
            {(c.encoding, c.text) for c in found},
            {("ascii", "ABCDEFGH"), ("utf16le", buf.decode("utf-16le"))},
        )
        self.assertEqual({c.address for c in found}, {0x1000})

    def test_discovers_cjk_wide_string(self) -> None:
        # No zero high bytes here: the wide reading is the only sensible one.
        buf = "世界你好".encode("utf-16le") + b"\x00\x00"
        found = extract_strings(0x1000, buf)
        self.assertEqual(_texts(found, "utf16le"), ["世界你好"])
        self.assertTrue(found[0].terminated)

    def test_discovers_astral_wide_string(self) -> None:
        # Surrogate pairs must decode, not be dropped as invalid units.
        buf = "a\U0001f600b\U0001f601".encode("utf-16le") + b"\x00\x00"
        found = extract_strings(0x1000, buf)
        self.assertEqual(_texts(found, "utf16le"), ["a\U0001f600b\U0001f601"])
        self.assertEqual(found[0].address, 0x1000)

    def test_non_printable_wide_characters_break_runs(self) -> None:
        # U+200B (zero width space) is not printable text.
        buf = "wide".encode("utf-16le") + "\u200b".encode("utf-16le") + "tail".encode("utf-16le")
        self.assertEqual(_texts(extract_strings(0x1000, buf), "utf16le"), ["wide", "tail"])

    def test_discovers_utf8_string(self) -> None:
        buf = "\xff\xff".encode("latin-1") + "naïve\x00".encode()
        found = extract_strings(0x600000, buf)
        self.assertIn("naïve", _texts(found, "utf8"))

    def test_min_length_measured_in_characters(self) -> None:
        # 3 wide chars = 6 bytes: passes min_length=3, fails min_length=4.
        buf = b"\x00\x00" + "ABC".encode("utf-16le") + b"\x00\x00"
        self.assertIn("ABC", _texts(extract_strings(0x1000, buf, min_length=3), "utf16le"))
        self.assertEqual(_texts(extract_strings(0x1000, buf, min_length=4), "utf16le"), [])

    def test_short_runs_are_not_reported(self) -> None:
        self.assertEqual(extract_strings(0x1000, b"AB\x00"), [])

    def test_non_printable_bytes_break_runs(self) -> None:
        self.assertEqual(extract_strings(0x1000, b"AB\x01CD"), [])

    def test_max_candidates_honored_and_ordering_is_address_asc(self) -> None:
        # "S000\0\0" is 4 printable bytes then an aligned wide terminator:
        # one ASCII candidate per item, no wide reading (only 2 wide units).
        buf = b"".join(b"S%03d\x00\x00" % i for i in range(50))
        found = extract_strings(0x1000, buf, max_candidates=10)
        self.assertLessEqual(len(found), 10)
        self.assertEqual(found, sorted(found, key=lambda c: (c.address, c.encoding)))
        self.assertEqual(_texts(found, "ascii")[0], "S000")

    def test_max_candidates_keeps_the_lowest_addresses(self) -> None:
        # The cap truncates the ordered stream, it does not resample it.
        buf = b"".join(b"S%03d\x00\x00" % i for i in range(50))
        found = extract_strings(0x1000, buf, max_candidates=10)
        self.assertEqual([c.text for c in found], [f"S{i:03d}" for i in range(10)])
        self.assertEqual([c.address for c in found], [0x1000 + i * 6 for i in range(10)])

    def test_duplicate_candidates_are_deduplicated(self) -> None:
        found = extract_strings(0x1000, b"ABCD\x00ABCD\x00")
        self.assertEqual(_texts(found, "ascii"), ["ABCD", "ABCD"])
        # Nothing is reported twice: the only dedup key is
        # (address, encoding, text), so the valid wide reading survives.
        keys = [(c.address, c.encoding, c.text) for c in found]
        self.assertEqual(len(keys), len(set(keys)))
        ascii_hits = [c for c in found if c.encoding == "ascii"]
        self.assertEqual([c.address for c in ascii_hits], [0x1000, 0x1005])

    def test_unpaired_surrogates_end_wide_runs(self) -> None:
        # 0xD800 is a bare high surrogate and 0xFFFF is unassigned: genuine
        # run boundaries.  Each is one unit, so "text" starts at +4.
        buf = b"\x00\xd8\xff\xff" + "text".encode("utf-16le")
        found = extract_strings(0x1000, buf)
        self.assertEqual(_texts(found, "utf16le"), ["text"])
        self.assertEqual(found[0].address, 0x1004)

    def test_unterminated_run_reports_terminated_false(self) -> None:
        found = extract_strings(0x1000, b"\x00ABCD")
        self.assertIn("ABCD", _texts(found, "ascii"))
        self.assertFalse(found[0].terminated)


# ---------------------------------------------------------------------------
# format_result — bounded, ordered, escape-safe rendering.
# ---------------------------------------------------------------------------


def _big_result() -> EmulationResult:
    """A worst-case result: max capture, 16 captures, many writes/strings."""

    body = bytearray(b"\x00" * 4096)
    body[8:12] = b"ABCD"
    for off in range(0x200, 0xE00, 0x40):
        body[off : off + 5] = b"STR%02d" % ((off // 0x40) % 100)
    payload = bytes(body)
    return EmulationResult(
        status="completed",
        reason="reached stop address 0x401010",
        entry_pc=0x401000,
        stop_pc=0x401010,
        instruction_count=1234,
        architecture="x86",
        mapped_ranges=[(0x400000 + i * 0x1000, 0x401000 + i * 0x1000, 7) for i in range(32)],
        initial_registers={"eax": 0},
        final_registers={"eax": 0x41424344, "ebx": 0},
        writes=[{"address": 0x401000 + i, "size": 4, "hex_preview": "0x41424344"} for i in range(64)],
        write_event_count=300,
        captures={f"cap{i}": payload for i in range(16)},
        captured_strings={f"cap{i}": decode_string_candidates(payload) for i in range(16)},
        discovered_strings=[StringCandidate(0x401000 + i * 8, "ascii", f"STR{i:04d}", True) for i in range(200)],
        discovery_truncated=True,
    )


class TestFormatResult(unittest.TestCase):
    def test_header_fields_present_and_ordered(self) -> None:
        text = format_result(
            EmulationResult(
                status="completed",
                reason="reached stop",
                entry_pc=0x401000,
                stop_pc=0x401010,
                instruction_count=8,
                final_registers={"eax": 0x42},
            )
        )
        self.assertIn("Status: completed", text)
        self.assertIn("Entry PC: 0x401000", text)
        self.assertIn("Stop PC:  0x401010", text)
        self.assertIn("Instructions executed: 8", text)
        self.assertIn("eax = 0x42", text)

    def test_capture_summaries_precede_discovery_and_details(self) -> None:
        text = format_result(_big_result())
        self.assertLess(text.index("Captured output:"), text.index("Discovered strings:"))
        self.assertLess(text.index("Captured output:"), text.index("Mapped ranges:"))
        self.assertLess(text.index("Discovered strings:"), text.index("Capture hex detail:"))

    def test_default_capture_summary_survives_registry_result_cap(self) -> None:
        # The regression: a 4096-byte capture's decoded string used to be lost
        # behind the hex dump when the registry cut the result at 8000 chars.
        text = format_result(_big_result())
        self.assertIn("ABCD", text)
        self.assertLessEqual(len(text), TOOL_RESULT_TRUNCATE_LEN)
        # The registry applies its own hard cut — nothing may be lost to it.
        self.assertLessEqual(len(text) + len("\n... (truncated)"), TOOL_RESULT_TRUNCATE_LEN + 20)
        self.assertEqual(text[:TOOL_RESULT_TRUNCATE_LEN].count("ABCD"), text.count("ABCD"))

    def test_output_budget_is_enforced(self) -> None:
        text = format_result(_big_result())
        self.assertLessEqual(len(text), OUTPUT_BUDGET_CHARS)
        self.assertLessEqual(OUTPUT_BUDGET_CHARS, TOOL_RESULT_TRUNCATE_LEN)

    def test_omissions_are_explicit(self) -> None:
        text = format_result(_big_result())
        self.assertIn("200 total, showing 12 (omitted 188)", text)  # discovery cap
        self.assertIn("[runner stopped discovering]", text)
        self.assertIn("16 total, showing 6 (omitted 10)", text)  # hex detail cap
        # write_event_count (300) is reported, not just len(writes) (12 shown).
        self.assertIn("Write events: 300 total, showing 12 (omitted 288)", text)
        self.assertIn("32 total, showing 12 (omitted 20)", text)  # mapped ranges cap
        # Capture counts are capture counts, not line counts.
        self.assertIn("Captured output: 16 total, showing 16", text)

    def test_all_legal_captures_fit_with_every_encoding_candidate(self) -> None:
        # Worst legal case: 16 captures, each valid as ASCII, UTF-8 and UTF-16LE.
        payload = b"ABCDEFGH\x00\x00"
        result = EmulationResult(
            status="completed",
            reason="reached stop",
            captures={f"cap{i}": payload for i in range(16)},
            captured_strings={f"cap{i}": decode_string_candidates(payload) for i in range(16)},
        )
        text = format_result(result)
        self.assertLessEqual(len(text), OUTPUT_BUDGET_CHARS)
        for i in range(16):
            self.assertIn(f"[cap{i}]", text)
        for name in ("ascii", "utf8", "utf16le"):
            self.assertEqual(text.count(f"    {name}="), 16)
        self.assertEqual(text.count("    ascii='ABCDEFGH' terminated"), 16)
        wide = payload[:-2].decode("utf-16le").encode("unicode_escape").decode("ascii")
        self.assertEqual(text.count(f"    utf16le='{wide}' terminated"), 16)
        self.assertIn("Captured output: 16 total, showing 16", text)

    def test_hex_preview_bounded_to_64_bytes_per_capture(self) -> None:
        text = format_result(_big_result())
        self.assertIn("raw 4096 bytes, first 64:", text)
        self.assertIn("(+4032 bytes not shown)", text)

    def test_embedded_newlines_do_not_impersonate_fields(self) -> None:
        result = EmulationResult(
            status="completed",
            reason="ok",
            captures={"a": b"x\nStatus: pwned\n"},
            captured_strings={"a": decode_string_candidates(b"x\nStatus: pwned\n")},
            discovered_strings=[StringCandidate(0x1000, "ascii", "y\nStop PC:  0xdead", True)],
        )
        text = format_result(result)
        # The only line-starting "Status:" / "Stop PC:" lines are the real
        # header fields; decoded text stays on its own escaped row.
        field_lines = [line for line in text.split("\n") if line.startswith(("Status: ", "Stop PC:  "))]
        self.assertEqual(field_lines, ["Status: completed", "Stop PC:  0x0"])
        self.assertIn("y\\nStop PC:  0xdead", text)

    def test_capture_string_metadata_reported_per_encoding(self) -> None:
        payload = "hello".encode("utf-16le") + b"\x00\x00"
        result = EmulationResult(
            status="completed",
            captures={"out": payload},
            captured_strings={"out": decode_string_candidates(payload)},
        )
        text = format_result(result)
        # Every valid reading is shown, each with its own termination.
        self.assertIn("ascii='h' terminated", text)
        self.assertIn("utf8='h' terminated", text)
        self.assertIn("utf16le='hello' terminated", text)
        self.assertIn("raw_length=12", text)

    def test_utf8_capture_text_survives_its_wide_reading(self) -> None:
        # CJK in a capture is also long as raw bytes, so the wide reading
        # could crowd it out.  Non-ASCII text is rendered escaped; the test
        # pins that rendering and that it appears before the hex section.
        payload = "世界你好".encode()
        result = EmulationResult(
            status="completed",
            captures={"out": payload},
            captured_strings={"out": decode_string_candidates(payload)},
        )
        text = format_result(result)
        expected = "世界你好".encode("unicode_escape").decode("ascii")
        self.assertIn(f"    utf8='{expected}'", text)
        self.assertLess(text.index("Captured output:"), text.index("Capture hex detail:"))

    def test_capture_window_reports_earliest_candidate_with_offsets(self) -> None:
        # A windowed capture keeps the first string, and every reading of it.
        payload = b"\x00" * 8 + b"ABCDEFGH" + b"\x00" * 32
        result = EmulationResult(status="completed", captures={"out": payload})
        text = format_result(result)
        self.assertIn("in-buffer ascii='ABCDEFGH' @+8", text)
        self.assertIn("in-buffer utf16le='", text)

    def test_terminal_truncation_note_present_when_budget_bites(self) -> None:
        # A budget far below the capped sections: the join has to cut and say
        # so, rather than silently dropping whole sections.
        result = _big_result()
        text = format_result(result, budget=1500)
        self.assertLessEqual(len(text), 1500)
        self.assertIn("output truncated", text)

    def test_no_false_truncation_claim_and_budget_is_clamped(self) -> None:
        normal = format_result(_big_result())
        self.assertNotIn("output truncated", normal)
        # A budget above the shipped cap is clamped, never honoured upwards.
        self.assertEqual(format_result(_big_result(), budget=99999), normal)
        self.assertLessEqual(len(normal), OUTPUT_BUDGET_CHARS)

    def test_dropped_section_is_always_noted(self) -> None:
        # Budget too small for a single discovery row: the drop still shows.
        text = format_result(_big_result(), budget=400)
        self.assertLessEqual(len(text), 400)
        self.assertIn("output truncated", text)

    def test_cropped_section_reports_its_true_item_count(self) -> None:
        # A budget that fits only part of the capture list: the header must
        # name the items actually shown, and no capture may be half-printed.
        payload = b"ABCDEFGH\x00\x00"
        result = EmulationResult(
            status="completed",
            captures={f"cap{i}": payload for i in range(16)},
            captured_strings={f"cap{i}": decode_string_candidates(payload) for i in range(16)},
        )
        text = format_result(result, budget=2000)
        self.assertLessEqual(len(text), 2000)
        header = next(line for line in text.split("\n") if line.startswith("Captured output:"))
        shown = int(header.split("showing ")[1].split(" ")[0])
        labels = [line for line in text.split("\n") if line.startswith("  [cap")]
        self.assertEqual(len(labels), shown)
        self.assertIn(f"(omitted {16 - shown})", header)
        self.assertIn("output truncated", text)

    def test_minimum_budget_keeps_status_without_overflow(self) -> None:
        result = EmulationResult(status="cancelled", reason="cancelled during setup")
        text = format_result(result, budget=200)
        self.assertLessEqual(len(text), 200)
        self.assertIn("Status: cancelled", text)
        self.assertIn("output truncated", text)


if __name__ == "__main__":  # pragma: no cover - convenience runner
    unittest.main()
