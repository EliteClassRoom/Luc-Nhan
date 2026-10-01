"""String decoding, candidate discovery and compact result rendering.

Three pure helpers, no IDA / Unicorn / registry dependency:

* :func:`decode_string_candidates` — decode one captured buffer as ASCII,
  UTF-8 and UTF-16LE, deciding the terminator of *each* encoding
  independently.
* :func:`extract_strings` — discover printable runs of those encodings in a
  buffer, with their virtual addresses.
* :func:`format_result` — render an
  :class:`~rikugan.ida.tools.emulation_types.EmulationResult` summary-first
  inside :data:`OUTPUT_BUDGET_CHARS`.

The budget sits below :data:`rikugan.constants.TOOL_RESULT_TRUNCATE_LEN`
(8000), so the registry never cuts the text: capture summaries and
discovered strings are printed before any hex detail, untrusted text is
escaped onto one line, and every omission is counted in its section header.
"""

from __future__ import annotations

from collections.abc import Iterator
from heapq import merge
from typing import Any

from rikugan.ida.tools.emulation_types import EmulationResult, StringCandidate

__all__ = [
    "OUTPUT_BUDGET_CHARS",
    "decode_string_candidates",
    "extract_strings",
    "format_result",
]

#: Hard ceiling for one rendered result. The registry truncates at
#: ``TOOL_RESULT_TRUNCATE_LEN`` (8000); staying under it keeps the
#: summary-first ordering meaningful instead of relying on that cut.
OUTPUT_BUDGET_CHARS = 7500

#: Hex bytes rendered per capture, regardless of the raw capture size.
_HEX_PREVIEW_BYTES = 64

#: Section item caps.  Every section header reports the shown count against
#: the total, so a cap is always visible instead of silently dropping data.
#: The caps add up to roughly the whole budget, so a saturated result can
#: still reach the terminal truncation note.  One item is one entry (a
#: capture with all of its encoding lines, a register, a mapping).
_DISCOVERED_ROWS = 12
_CAPTURE_ITEMS = 16
_DETAIL_ROWS = 6
_REGISTER_ROWS = 12
_MAPPING_ROWS = 12
_WRITE_ROWS = 12

#: Characters of untrusted text rendered per row.
_TEXT_CHARS = 96

#: Per-candidate text budget, short enough that 16 legal captures with three
#: candidates each still fit inside ``OUTPUT_BUDGET_CHARS``.
_CANDIDATE_TEXT_CHARS = 48

#: Shortest capture text worth reporting from the cached metadata; below it
#: the window fallback shows the candidates found inside the buffer instead.
_MIN_REPORTED_CHARS = 4

_ASCII_SPACE = 0x20
_ASCII_MAX = 0x7F  # exclusive

_OMISSION_NOTE = "\n... output truncated to {budget} chars (later sections omitted)"

_ENCODINGS = ("ascii", "utf8", "utf16le")


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def _is_printable_ascii(byte: int) -> bool:
    return _ASCII_SPACE <= byte < _ASCII_MAX


def _safe_decode(payload: bytes, encoding: str) -> str:
    """Decode strictly, or return ``""`` for malformed input.

    An undecodable payload is never replaced with ``?`` placeholders: a
    manufactured string would reach the model as decoded content.
    """

    try:
        return payload.decode(encoding)
    except (UnicodeDecodeError, ValueError):
        return ""


def _is_printable_text(text: str) -> bool:
    """True when every character of *text* is printable (empty → False)."""

    return bool(text) and all(ch.isprintable() for ch in text)


def _ascii_run(data: bytes) -> tuple[str, bool]:
    """ASCII candidate: printable prefix, plus whether a NUL ended it.

    Stops at the first NUL byte (the terminator) or at the first byte that
    is not printable (a run break, not a terminator).
    """

    out: list[str] = []
    for byte in data:
        if byte == 0:
            return "".join(out), True
        if not _is_printable_ascii(byte):
            break
        out.append(chr(byte))
    return "".join(out), False


def _utf8_run(data: bytes, start: int = 0) -> tuple[str, bool]:
    """UTF-8 candidate from *start*: ``(text, terminated)``.

    Stops at the first NUL (its own terminator) or at the first byte that
    is not part of a well-formed, printable UTF-8 sequence (a run break).
    ASCII is the subset of this scan, so one pass serves both encodings.
    """

    out: list[str] = []
    index = start
    limit = len(data)
    while index < limit:
        byte = data[index]
        if byte == 0:
            return "".join(out), True
        if byte < 0x80:
            if not _is_printable_ascii(byte):
                break
            out.append(chr(byte))
            index += 1
            continue
        if byte < 0xC2 or byte > 0xF4:
            break  # continuation byte, overlong lead, or out of range
        needed = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
        chunk = data[index : index + needed]
        if len(chunk) < needed:
            break
        decoded = _safe_decode(chunk, "utf-8")
        if not _is_printable_text(decoded):
            break
        out.append(decoded)
        index += needed
    return "".join(out), False


# ---------------------------------------------------------------------------
# Wide (UTF-16LE) code units — shared by the buffer decode and discovery.
# ---------------------------------------------------------------------------


def _wide_unit(index: int, payload: bytes, limit: int) -> tuple[str | None, int]:
    """Decode the UTF-16LE code unit at even *index*: ``(char, width)``.

    ``char`` is ``None`` for an unaligned offset, a bare high surrogate, a
    truncated pair, or a non-printable character (control, unassigned,
    zero-width).  An aligned wide NUL decodes to the NUL character, which the
    run scanner treats as a terminator rather than as text.
    """

    if index % 2 or index + 1 >= limit:
        return None, 2
    unit = payload[index] | (payload[index + 1] << 8)
    if unit == 0:  # aligned wide NUL terminator
        return "\0", 2
    if 0xD800 <= unit <= 0xDBFF:  # high surrogate: needs its low pair
        if index + 3 >= limit:
            return None, 2
        low = payload[index + 2] | (payload[index + 3] << 8)
        if not 0xDC00 <= low <= 0xDFFF:
            return None, 2
        char = chr(0x10000 + ((unit - 0xD800) << 10) + (low - 0xDC00))
        return (char if char.isprintable() else None), 4
    if 0xDC00 <= unit <= 0xDFFF:  # unpaired low surrogate
        return None, 2
    char = chr(unit)
    return (char if char.isprintable() else None), 2


def _wide_run(data: bytes) -> tuple[str, bool, int]:
    """UTF-16LE candidate of the buffer from offset 0.

    ``(text, terminated, width)``.  The run starts at the buffer start and
    ends at the first boundary, so a string that only starts later in the
    buffer is never reported as the buffer's candidate.  An aligned wide NUL
    sets the terminator flag; an interior single-byte NUL is the high byte of
    the next unit and does not end the run.
    """

    limit = len(data) & ~1
    chars: list[str] = []
    index = 0
    terminated = False
    while index < limit:
        char, width = _wide_unit(index, data, limit)
        if char is None:
            index += width
            break
        if char == "\0":
            terminated = True
            index += width
            break
        chars.append(char)
        index += width
    return "".join(chars), terminated, index


def decode_string_candidates(data: bytes) -> dict[str, Any]:
    """Decode *data* as ASCII / UTF-8 / UTF-16LE with independent terminators.

    Every candidate describes the buffer *from offset 0*: a wide string that
    only starts later in the buffer is not a candidate of the buffer (use
    :func:`extract_strings` for that).  Returns ``ascii``, ``utf8`` and
    ``utf16le`` (each ``""`` when the payload is not a valid printable
    candidate of that encoding), the per-encoding ``*_terminated`` flags,
    ``raw_length`` and the combined ``has_nul_terminator``.
    """

    payload = bytes(data or b"")
    ascii_text, ascii_terminated = _ascii_run(payload)
    utf8_text, utf8_terminated = _utf8_run(payload)
    wide_text, wide_terminated, _offset = _wide_run(payload)
    return {
        "ascii": ascii_text,
        "ascii_terminated": ascii_terminated,
        "utf8": utf8_text,
        "utf8_terminated": utf8_terminated,
        "utf16le": wide_text,
        "utf16le_terminated": wide_terminated,
        "raw_length": len(payload),
        "has_nul_terminator": ascii_terminated or utf8_terminated or wide_terminated,
    }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _byte_stream(payload: bytes, floor: int) -> Iterator[tuple[int, str, str, bool]]:
    """ASCII / UTF-8 runs, one per run start, in buffer order (lazy)."""

    index = 0
    limit = len(payload)
    while index < limit:
        byte = payload[index]
        if byte == 0 or (byte < 0x80 and not _is_printable_ascii(byte)):
            index += 1
            continue
        text, terminated = _utf8_run(payload, index)
        if len(text) < floor:
            index += 1
            continue
        yield index, ("ascii" if text.isascii() else "utf8"), text, terminated
        index += len(text.encode("utf-8", "replace"))


def _wide_units(payload: bytes, floor: int) -> Iterator[tuple[int, str, str, bool]]:
    """UTF-16LE runs at even offsets, in buffer order (lazy).

    Each run starts at the first code unit of printable text after an
    unaligned offset, a NUL terminator, an invalid unit, or a non-printable
    character, and ends at the next such boundary — so a wide string is found
    anywhere, not only at a terminator boundary.

    Bytes that also read as ASCII are reported twice, once per encoding: the
    same bytes genuinely are both, and neither reading is provably the one
    the program meant.  Only identical ``(address, encoding, text)`` is
    deduplicated.
    """

    index = 0
    limit = len(payload) & ~1
    while index < limit:
        char, width = _wide_unit(index, payload, limit)
        if char is None or char == "\0":
            index += width
            continue
        start = index
        chars: list[str] = []
        terminated = False
        while index < limit:
            char, width = _wide_unit(index, payload, limit)
            if char is None:
                index += width
                break
            if char == "\0":
                terminated = True
                index += width
                break
            chars.append(char)
            index += width
        text = "".join(chars)
        if len(text) < floor:
            continue
        yield start, "utf16le", text, terminated


def extract_strings(
    address: int,
    data: bytes,
    *,
    min_length: int = 4,
    max_candidates: int = 64,
) -> list[StringCandidate]:
    """Discover printable ASCII / UTF-8 / UTF-16LE runs in *data*.

    *min_length* counts characters, not bytes.  Identical
    ``(address, encoding, text)`` candidates are reported once; the result
    is ordered by address and holds at most *max_candidates* entries.

    The two scans are lazy and merged with :func:`heapq.merge`, so the result
    stops as soon as the cap is reached.  A buffer with no candidate in one
    encoding still costs a full scan of that encoding: worst case is O(n) in
    the buffer size, not O(max_candidates).
    """

    payload = bytes(data or b"")
    floor = max(1, int(min_length))
    out: list[StringCandidate] = []
    seen: set[tuple[int, str, str]] = set()
    runs = merge(
        _byte_stream(payload, floor),
        _wide_units(payload, floor),
        key=lambda run: (run[0], run[1]),
    )
    for offset, encoding, text, terminated in runs:
        key = (offset, encoding, text)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            StringCandidate(
                address=address + offset,
                encoding=encoding,
                text=text,
                terminated=terminated,
            )
        )
        if len(out) >= max(1, int(max_candidates)):
            break
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _escape(text: Any, *, limit: int = _TEXT_CHARS) -> str:
    """One-line, control-character-free rendering of untrusted *text*.

    Embedded newlines and control characters are escaped, so decoded
    output can never impersonate a result field.
    """

    escaped = str(text).encode("unicode_escape").decode("ascii")
    if len(escaped) > limit:
        escaped = escaped[:limit] + f"...(+{len(escaped) - limit} chars)"
    return escaped


class _Section:
    """Title + item cap + visible accounting of what was dropped.

    One *item* is one logical entry (a capture and its encoding lines, a
    register, a mapping).  Items may span several lines but are never split,
    so the rendered counts are always item counts.
    """

    def __init__(self, title: str, total: int, item_cap: int, *, note: str = "") -> None:
        self._title = title
        self._total = total
        self._items: list[str] = []
        self._item_cap = item_cap
        self._note = note

    def add(self, item: str) -> bool:
        """Append *item*; False once the cap is reached."""

        if len(self._items) >= self._item_cap:
            return False
        self._items.append(item)
        return True

    def render(self) -> str:
        shown = len(self._items)
        head = f"{self._title}: {self._total} total, showing {shown}"
        if self._total > shown:
            head += f" (omitted {self._total - shown})"
        if self._note:
            head += f" {self._note}"
        return "\n".join([head, *self._items])


def _fit_items(
    header: str,
    items: list[str],
    budget: int,
    used: int,
) -> list[str] | None:
    """Keep whole *items* of a section that does not fit the *budget*.

    The header is rewritten to the number of items actually kept, so the
    ``total / showing`` line never claims items the reader cannot see.  A
    capture item spans several lines but is never cut in half.  ``None``
    means not even one item fits and the caller must name the drop.
    """

    if not items:
        return None
    prefix, _, remainder = header.partition(" total, showing ")
    total = int(prefix.rsplit(" ", 1)[1])
    suffix = remainder.partition(" ")[2]
    if suffix.startswith("(omitted "):
        suffix = suffix.partition(")")[2].lstrip()
    note = _OMISSION_NOTE.format(budget=budget)
    kept: list[str] = []
    # Reserve the widest rewritten count header before keeping whole items.
    longest_header = f"{prefix} total, showing {len(items)} (omitted {total}) {suffix}"
    spent = used + len(longest_header) + 1
    for item in items:
        cost = len(item) + 1
        if spent + cost + len(note) > budget:
            break
        kept.append(item)
        spent += cost
    if not kept:
        return None
    shown = len(kept)
    head = f"{prefix} total, showing {shown}"
    if total > shown:
        head += f" (omitted {total - shown})"
    if suffix:
        head += f" {suffix}"
    return [head, *kept]


def _budgeted(sections: list[str], budget: int) -> str:
    """Join *sections* whole, keeping whole items inside a cropped section.

    Whenever anything is dropped the terminal note says so, so a dropped
    section or item is never silent.  With the item caps in place the note
    only fires for a small explicit budget.
    """

    note = _OMISSION_NOTE.format(budget=budget)
    head = sections[0]
    if len(head) + len(note) >= budget:
        return head[: max(0, budget - len(note))] + note
    parts = [head]
    used = len(head)
    for index, body in enumerate(sections[1:], 1):
        block = f"\n\n{body}"
        reserve = len(note) if index < len(sections) - 1 else 0
        if used + len(block) + reserve <= budget:
            parts.append(block)
            used += len(block)
            continue
        lines = body.split("\n")
        items: list[str] = []
        for line in lines[1:]:
            if line.startswith("    ") and items:
                items[-1] += "\n" + line
            else:
                items.append(line)
        cropped = _fit_items(lines[0], items, budget, used + 2)
        if cropped is not None:
            parts.append("\n\n" + "\n".join(cropped))
        parts.append(note)
        break
    return "".join(parts)


def _perms_text(perms: int) -> str:
    return "".join(flag for bit, flag in ((1, "r"), (2, "w"), (4, "x")) if perms & bit) or "none"


def _capture_lines(result: EmulationResult, label: str, payload: bytes) -> list[str]:
    """One line per independent encoding candidate, header first.

    The same bytes usually have more than one valid reading, and nothing in
    the buffer proves which one the program meant, so every non-empty
    candidate is shown with its own termination flag instead of one
    "winner".
    """

    meta = result.captured_strings.get(label)
    from_buffer = (
        meta is not None
        and max(
            (len(str(meta.get(name) or "")) for name in _ENCODINGS),
            default=0,
        )
        >= _MIN_REPORTED_CHARS
    )
    lines: list[str] = []
    if from_buffer and meta is not None:
        for name in _ENCODINGS:
            text = str(meta.get(name) or "")
            if text:
                lines.append(f"    {name}='{_escape(text, limit=_CANDIDATE_TEXT_CHARS)}'{_terminated_tail(meta, name)}")
    else:
        # A capture is a fixed-size window, so its string often does not
        # start at offset 0: fall back to the candidates at the earliest
        # address inside it, each with its offset.
        for offset, encoding, text, terminated in _in_buffer_candidates(payload):
            tail = " terminated" if terminated else ""
            short = _escape(text, limit=_CANDIDATE_TEXT_CHARS)
            lines.append(f"    in-buffer {encoding}='{short}' @+{offset}{tail}")
    if not lines:
        lines.append("    no printable string")
    head = f"  [{_escape(label, limit=40)}] {len(payload)} bytes"
    flags = [name for name in _ENCODINGS if meta and meta.get(f"{name}_terminated")]
    if flags:
        head += " terminated=" + "+".join(flags)
    if meta is not None and meta.get("raw_length") is not None:
        head += f" raw_length={meta['raw_length']}"
    return [head, *lines]


def _terminated_tail(meta: dict[str, Any], encoding: str) -> str:
    return " terminated" if meta.get(f"{encoding}_terminated") else ""


def _in_buffer_candidates(payload: bytes) -> list[tuple[int, str, str, bool]]:
    """Every encoding candidate at the earliest address inside *payload*.

    Earliest first, so the first string in a window always survives; both
    readings of that address are kept because neither is provably right.
    """

    out: list[tuple[int, str, str, bool]] = []
    first: int | None = None
    for candidate in merge(_byte_stream(payload, 1), _wide_units(payload, 1), key=lambda run: (run[0], run[1])):
        if first is None:
            first = candidate[0]
        elif candidate[0] != first:
            break
        out.append(candidate)
    return out


def _discovered_section(result: EmulationResult) -> str:
    total = len(result.discovered_strings)
    note = "[runner stopped discovering]" if result.discovery_truncated else ""
    section = _Section("Discovered strings", total, _DISCOVERED_ROWS, note=note)
    for candidate in result.discovered_strings:
        if not section.add(
            f"  0x{candidate.address:x} {candidate.encoding} term={candidate.terminated} '{_escape(candidate.text)}'"
        ):
            break
    return section.render()


def _capture_summary_section(result: EmulationResult) -> str:
    """One item per capture, kept before everything optional."""

    if not result.captures:
        return "Captured output: none"
    section = _Section("Captured output", len(result.captures), _CAPTURE_ITEMS)
    for label, data in result.captures.items():
        payload = bytes(data or b"")
        # One item per capture: header + its encoding lines stay together, so
        # the section header counts captures, not lines.
        if not section.add("\n".join(_capture_lines(result, str(label), payload))):
            break
    return section.render()


def _capture_detail_section(result: EmulationResult) -> str:
    """Bounded hex previews; the last thing in the output."""

    if not result.captures:
        return "Capture hex detail: none"
    section = _Section("Capture hex detail", len(result.captures), _DETAIL_ROWS)
    for label, data in result.captures.items():
        payload = bytes(data or b"")
        if not payload:
            continue
        shown = payload[:_HEX_PREVIEW_BYTES]
        hidden = len(payload) - len(shown)
        if not section.add(
            f"  [{_escape(label, limit=40)}] raw {len(payload)} bytes, first {len(shown)}: "
            f"{shown.hex(' ')}" + (f" (+{hidden} bytes not shown)" if hidden > 0 else "")
        ):
            break
    return section.render()


def _register_section(result: EmulationResult) -> str:
    initial = result.initial_registers or {}
    changed = {
        name: value
        for name, value in result.final_registers.items()
        if name not in initial or initial.get(name) != value
    }
    if not result.final_registers:
        return "Changed registers: none"
    unchanged = len(result.final_registers) - len(changed)
    note = f"[{unchanged} unchanged not listed]" if unchanged else ""
    section = _Section("Changed registers", len(changed), _REGISTER_ROWS, note=note)
    for name in sorted(changed):
        if not section.add(f"  {name} = 0x{changed[name]:x}"):
            break
    return section.render()


def _mapping_section(result: EmulationResult) -> str:
    if not result.mapped_ranges:
        return "Mapped ranges: none"
    section = _Section("Mapped ranges", len(result.mapped_ranges), _MAPPING_ROWS)
    for start, end, perms in result.mapped_ranges:
        if not section.add(f"  0x{start:x}-0x{end:x} {_perms_text(perms)}"):
            break
    return section.render()


def _write_section(result: EmulationResult) -> str:
    total = result.write_event_count or len(result.writes)
    if not result.writes:
        return f"Write events: {total} total, none recorded"
    section = _Section("Write events", total, _WRITE_ROWS)
    for entry in result.writes:
        if not section.add(
            f"  0x{entry.get('address', 0):x} size={entry.get('size', 0)} "
            f"value={_escape(entry.get('hex_preview', ''), limit=32)}"
        ):
            break
    return section.render()


def format_result(result: EmulationResult, *, budget: int = OUTPUT_BUDGET_CHARS) -> str:
    """Render *result* summary-first inside *budget* characters.

    Order is the contract: status / reason / entry / stop / instruction
    count, then capture summaries and discovered strings, then changed
    registers, mapping and write summaries, then per-capture hex detail.
    Anything dropped is counted in its section header or in a trailing
    truncation note.
    """

    limit = min(OUTPUT_BUDGET_CHARS, max(200, int(budget)))
    header = "\n".join(
        [
            f"=== Unicorn emulation ({result.architecture.upper()}) ===",
            f"Status: {result.status}",
            f"Reason: {_escape(result.reason, limit=200)}",
            f"Entry PC: 0x{result.entry_pc:x}",
            f"Stop PC:  0x{result.stop_pc:x}",
            f"Instructions executed: {result.instruction_count}",
        ]
    )
    sections = [
        header,
        _capture_summary_section(result),
        _discovered_section(result),
        _register_section(result),
        _mapping_section(result),
        _write_section(result),
        _capture_detail_section(result),
    ]
    return _budgeted(sections, limit)
