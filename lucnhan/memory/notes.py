"""Markdown note parsing for the knowledge memory subsystem.

Luc Nhan's research mode writes Obsidian-style Markdown notes. This
module extracts structured metadata from them so the JSONL store can
hold references without re-reading the user's notes from scratch.

Parsing rules (matching research.write_and_review_note / _generate_index):

* Optional YAML-ish frontmatter ``---`` block at the very top:
  ``title``, ``genre``, ``tags``, ``addresses``, ``related``.
* `> Addresses: 0x401000, 0x401100` blockquote line.
* `> Genre: #tag` blockquote line.
* `> Related: [[other-note]]` blockquote line.
* `## Summary` / `## Detailed Analysis` / etc. headings preserved as-is.
* Inline ``[[wiki-link]]`` syntax.
* Inline ``#tag`` syntax (only the simple ``/#[a-z0-9_-]+/`` form).

We deliberately keep the parser **loose** (no third-party YAML dep).
Unknown lines are passed through verbatim. Only fields we recognize
become typed data; everything else goes to ``raw_metadata``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

WIKI_LINK_RE = re.compile(r"\[\[([A-Za-z0-9 _./-]+?)\]\]")
HASHTAG_RE = re.compile(r"(?:^|\s)#([a-z0-9][a-z0-9_-]{0,63})\b", re.IGNORECASE)
ADDRESS_RE = re.compile(r"0[xX][0-9a-fA-F]{4,16}\b")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
BLOCKQUOTE_FIELDS = ("addresses", "genre", "related", "tags")


@dataclass
class ParsedNote:
    """Structured view of one research note."""

    path: str = ""
    title: str = ""
    genre: str = ""
    tags: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)
    wiki_links: list[str] = field(default_factory=list)
    related_notes: list[str] = field(default_factory=list)
    body: str = ""
    raw_frontmatter: dict[str, str] = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)

    def entity_id(self) -> str:
        """A stable entity id based on the file path's slug."""
        base = os.path.splitext(os.path.basename(self.path or ""))[0]
        from .paths import note_entity_id  # local import to avoid cycle

        return note_entity_id(base or (self.title or "untitled"))

    def function_entity_refs(self) -> list[str]:
        """Return the function entity IDs derived from the addresses."""
        from .paths import function_entity_id  # local import to avoid cycle

        refs: list[str] = []
        for addr in self.addresses:
            eid = function_entity_id(addr)
            if eid not in refs:
                refs.append(eid)
        return refs


# ---------------------------------------------------------------------------
# Frontmatter parsing (loose)
# ---------------------------------------------------------------------------


def _parse_frontmatter_block(block: str) -> dict[str, str]:
    """Parse a simple ``key: value`` block (optionally quoted).

    Keeps first occurrence, ignores comments (``#``), and tolerates
    trailing whitespace. Lists come back comma-separated so callers
    can split them once at use site.
    """
    out: dict[str, str] = {}
    for line in block.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if ":" not in s:
            continue
        key, _, val = s.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if not key:
            continue
        # Strip optional surrounding quotes
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {'"', "'"}:
            val = val[1:-1]
        out[key] = val
    return out


def _split_csv(value: str) -> list[str]:
    """Split a comma-separated value, trimming and discarding empties.

    Also unwraps a single-layer YAML-style list (``[a, b]``) into the
    same items, because that is the form the agent tends to write when
    giving multiple tags or addresses up-front.
    """
    if not value:
        return []
    v = value.strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    return [s.strip() for s in v.split(",") if s.strip()]


# ---------------------------------------------------------------------------
# Section + blockquote extraction
# ---------------------------------------------------------------------------


_HEADING_RE = re.compile(r"^(#+)\s+(.*?)\s*$", re.MULTILINE)


def _extract_sections(body: str) -> dict[str, str]:
    """Return a mapping ``heading → body`` for level-2 sections.

    Content under each ``## Heading`` runs until the next same-or-higher
    level heading. ``# Heading`` (level 1) becomes the note title; we
    leave its body accessible under the heading key too.
    """
    sections: dict[str, str] = {}
    matches = list(_HEADING_RE.finditer(body))
    for i, m in enumerate(matches):
        heading = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        # Skip higher-level (lower number) headings' bodies if we want
        # only ## sections; here we keep all and bucket by raw heading.
        sections[heading] = body[start:end].strip()
    return sections


def _extract_blockquote(body: str, name: str) -> str | None:
    """Return the contents (after the colon) of ``> <name>: ...``."""
    pattern = re.compile(rf"^>\s*{re.escape(name)}\s*:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
    m = pattern.search(body)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_note(content: str, path: str = "") -> ParsedNote:
    """Parse a research note's Markdown into a :class:`ParsedNote`.

    Frontmatter (if present) takes precedence over blockquote fields
    because the agent writing the note can stamp them up-front. The
    heuristic fallbacks are the blockquote lines and the file path.
    """
    note = ParsedNote(path=path)
    raw = content or ""
    body = raw

    # 1. Frontmatter
    fm_match = FRONTMATTER_RE.match(raw)
    if fm_match:
        note.raw_frontmatter = _parse_frontmatter_block(fm_match.group(1))
        body = raw[fm_match.end() :]

    note.body = body

    # 2. First H1 (or filename) is the title
    h1_match = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
    if h1_match:
        note.title = h1_match.group(1).strip()
    elif not note.title and note.raw_frontmatter.get("title"):
        note.title = note.raw_frontmatter["title"]
    elif not note.title and path:
        note.title = os.path.splitext(os.path.basename(path))[0].replace("-", " ").replace("_", " ").title()

    # 3. Genre — frontmatter > blockquote > parent directory
    note.genre = (
        note.raw_frontmatter.get("genre", "").strip()
        or (_extract_blockquote(body, "Genre") or "").lstrip("#").strip()
        or _genre_from_path(path)
    )

    # 4. Tags — union of frontmatter, blockquote-tags, inline hashtags
    fm_tags = _split_csv(note.raw_frontmatter.get("tags", ""))
    bq_tags = _split_csv((_extract_blockquote(body, "Tags") or "").lstrip("#"))
    inline_tags = [t.lower() for t in HASHTAG_RE.findall(body)]
    note.tags = _dedup_preserve([*fm_tags, *bq_tags, *inline_tags])

    # 5. Addresses — frontmatter list, blockquote, then inline matches
    fm_addrs = [a.lower() for a in _split_csv(note.raw_frontmatter.get("addresses", ""))]
    bq_addrs = (
        [a.lower() for a in re.findall(ADDRESS_RE, _extract_blockquote(body, "Addresses") or "")]
        if _extract_blockquote(body, "Addresses")
        else []
    )
    inline_addrs = [a.lower() for a in ADDRESS_RE.findall(body)]
    note.addresses = _dedup_preserve([*fm_addrs, *bq_addrs, *inline_addrs])

    # 6. Wiki-links — internal [[...]] references (related notes)
    body_wiki = WIKI_LINK_RE.findall(body)
    fm_wiki = WIKI_LINK_RE.findall(note.raw_frontmatter.get("related", ""))
    all_wiki = _dedup_preserve([w.strip() for w in (*body_wiki, *fm_wiki) if w.strip()])
    note.wiki_links = all_wiki

    # Related notes are wiki-links that look like slug names.
    note.related_notes = list(note.wiki_links)

    # 7. Sections
    note.sections = _extract_sections(body)

    return note


def _dedup_preserve(items: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for it in items:
        if it and it not in seen:
            seen[it] = None
    return list(seen.keys())


def _genre_from_path(path: str) -> str:
    """Best-effort genre from the parent directory name under ``notes/``."""
    if not path:
        return ""
    parent = os.path.basename(os.path.dirname(os.path.abspath(path)))
    if not parent or parent == "notes" or parent == "reports":
        return ""
    return parent


def list_notes(notes_dir: str) -> list[ParsedNote]:
    """Walk *notes_dir* and parse every ``*.md`` file (one level deep).

    Includes ``index.md`` and any subdirectory notes (``functions/*.md``,
    ``findings/*.md``, ...). Reports under ``reports/`` are skipped —
    they have their own entity type.
    """
    out: list[ParsedNote] = []
    if not notes_dir or not os.path.isdir(notes_dir):
        return out
    for root, dirs, files in os.walk(notes_dir):
        # Don't recurse into ``reports`` — those are handled separately.
        dirs[:] = [d for d in dirs if d != "reports"]
        for fn in files:
            if not fn.endswith(".md"):
                continue
            full = os.path.join(root, fn)
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            parsed = parse_note(text, path=full)
            # If the title didn't survive parsing, fall back to the
            # filename so the note still surfaces in search results.
            if not parsed.title:
                parsed.title = os.path.splitext(fn)[0]
            out.append(parsed)
    return out


# ---------------------------------------------------------------------------
# Re-export helpers used by ingest / context
# ---------------------------------------------------------------------------


def extract_inline_addresses(text: str) -> list[str]:
    """Return lowercase hex addresses found in *text* (delegates to ``paths.extract_addresses``).

    Shared with :func:`lucnhan.memory.paths.extract_addresses` so both
    ingestion paths produce identical, deduped, leading-word-boundary
    lists.  Older behavior was a separate regex + ordered scan that
    silently diverged for inputs without a leading word boundary.
    """
    from .paths import extract_addresses

    return extract_addresses(text)


def extract_inline_tags(text: str) -> list[str]:
    return [t.lower() for t in HASHTAG_RE.findall(text or "")]
