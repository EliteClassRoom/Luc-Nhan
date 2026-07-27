"""Skill discovery and loading from the Rikugan skills directory."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import SkillError
from ..core.logging import log_debug, log_error

# ---------------------------------------------------------------------------
# Minimal frontmatter parser (no PyYAML dependency)
# ---------------------------------------------------------------------------

# YAML block scalar indicators: `>` = folded (newlines → spaces), `|` =
# literal (newlines preserved). Optional chomping indicator: `-` strip,
# `+` keep, none = clip. Examples handled: `>`, `>-`, `>+`, `|`, `|-`, `|+`.
_BLOCK_SCALAR_RE = re.compile(r"^[>|]([-+]?)")


def _parse_block_scalar(
    lines: list[str],
    start: int,
    indicator: str,
    chomp: str,
) -> tuple[str, int]:
    """Collect an indented block scalar starting after ``lines[start-1]``.

    Parameters
    ----------
    lines
        All frontmatter lines.
    start
        Index of the first line *after* the indicator line.
    indicator
        ``">"`` (folded — newlines become spaces, blank lines become a
        single newline) or ``"|"`` (literal — newlines preserved).
    chomp
        ``"-"`` strip trailing newlines, ``"+"`` keep all trailing
        newlines, ``""`` clip to a single trailing newline.

    Returns
    -------
    (value, next_index)
        The decoded scalar text and the index of the first line *after*
        the block (so the caller can continue parsing).
    """

    # Determine the block's indentation from the first content line.
    indent = 0
    body_lines: list[str] = []
    j = start
    while j < len(lines):
        bline = lines[j]
        # A non-empty line at column 0 ends the block.
        if bline and not bline[0].isspace() and bline.strip():
            break
        # A line with less indentation than the first content line ends
        # the block (covers dedented keys, the closing ``---``, etc.).
        if bline.strip() and indent == 0:
            indent = len(bline) - len(bline.lstrip())
        if bline.strip():
            current_indent = len(bline) - len(bline.lstrip())
            if current_indent < indent:
                break
            body_lines.append(bline[indent:])
        else:
            # Blank line — preserve as empty string so paragraph breaks
            # survive the fold step below.
            body_lines.append("")
        j += 1

    if indicator == ">":
        # Folded: join lines with spaces, but blank lines become a newline
        # so paragraph breaks survive. Two consecutive non-empty lines
        # collapse to one space.
        folded_parts: list[str] = []
        for bl in body_lines:
            if bl == "":
                folded_parts.append("\n")
            else:
                if folded_parts and not folded_parts[-1].endswith("\n"):
                    folded_parts.append(" ")
                folded_parts.append(bl)
        value = "".join(folded_parts)
    else:
        # Literal: preserve newlines verbatim.
        value = "\n".join(body_lines)

    # Strip a single trailing blank-line artifact left by join.
    value = value.rstrip("\n")

    # Apply chomping.
    if chomp == "-":
        pass  # already stripped
    elif chomp == "+":
        # Keep all trailing newlines from the raw block. We stripped them
        # above, so re-add by counting trailing empty body lines.
        trailing = 0
        for bl in reversed(body_lines):
            if bl == "":
                trailing += 1
            else:
                break
        value = value + ("\n" * trailing) if trailing else value
    else:
        # Clip: single trailing newline. Skills store these in a dict that
        # is later compared as a plain Python str, so we follow the existing
        # scalar-parser convention of stripping it (the surrounding `.strip()`
        # calls in `_parse_frontmatter` for inline scalars drop trailing
        # whitespace; matching that here keeps the dict values consistent).
        pass

    return value, j


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse YAML-like frontmatter between --- markers.

    Supports:
      key: value              → str
      key: [a, b, c]          → list (inline)
      key:                     → list (block)
        - item1
        - item2
      key:                     → dict (nested key-value)
        subkey: value
        subkey2: value2
      key: >-                  → str (YAML folded block scalar)
        Multi-line content
        folded onto one line.
      key: |-                  → str (YAML literal block scalar)
        Verbatim line 1
        Verbatim line 2
    """
    result: dict[str, Any] = {}
    lines = text.strip().splitlines()

    i = 0
    while i < len(lines):
        line = lines[i]

        # Skip blank lines and comments
        if not line.strip() or line.strip().startswith("#"):
            i += 1
            continue

        # key: value
        m = re.match(r"^(\w[\w\-]*)\s*:\s*(.*)", line)
        if not m:
            i += 1
            continue

        key = m.group(1).strip()
        value_part = m.group(2).strip()

        # YAML block scalar (`>`, `|-`, etc.) — value must be exactly the
        # indicator with no inline content. The actual text lives on the
        # following indented lines.
        block_match = _BLOCK_SCALAR_RE.match(value_part)
        if block_match and value_part == block_match.group(0):
            indicator = value_part[0]
            chomp = block_match.group(1)
            value, next_i = _parse_block_scalar(lines, i + 1, indicator, chomp)
            result[key] = value
            i = next_i
            continue

        if value_part:
            # Inline list: [a, b, c]
            if value_part.startswith("[") and value_part.endswith("]"):
                inner = value_part[1:-1]
                items = [s.strip().strip("\"'") for s in inner.split(",") if s.strip()]
                result[key] = items
            else:
                # Scalar — strip surrounding quotes
                result[key] = value_part.strip("\"'")
        else:
            # Check for block list (next lines starting with "  - ")
            # or nested dict (next lines starting with "  key: value")
            block_items: list[str] = []
            nested_dict: dict[str, str] = {}
            j = i + 1
            while j < len(lines):
                bline = lines[j]
                # Skip comments inside a block (consistent with top-level parsing)
                if bline.strip().startswith("#"):
                    j += 1
                    continue
                # Block list item
                bm = re.match(r"^\s+-\s+(.*)", bline)
                if bm:
                    block_items.append(bm.group(1).strip().strip("\"'"))
                    j += 1
                    continue
                # Nested key-value pair (indented)
                nm = re.match(r"^\s+(\w[\w\-]*)\s*:\s+(.*)", bline)
                if nm:
                    nested_dict[nm.group(1).strip()] = nm.group(2).strip().strip("\"'")
                    j += 1
                    continue
                if not bline.strip():
                    j += 1
                    continue
                break
            if block_items:
                result[key] = block_items
                i = j
                continue
            elif nested_dict:
                result[key] = nested_dict
                i = j
                continue
            else:
                result[key] = ""

        i += 1

    return result


def _split_frontmatter(text: str) -> tuple:
    """Split a SKILL.md into (frontmatter_text, body_text).

    Returns ("", text) if no frontmatter markers found.
    """
    stripped = text.lstrip("\ufeff\n")  # strip BOM (if present) + leading newlines
    if not stripped.startswith("---"):
        return ("", text)

    # Find closing ---
    rest = stripped[3:].lstrip("\n")
    idx = rest.find("\n---")
    if idx == -1:
        return ("", text)

    fm_text = rest[:idx]
    body = rest[idx + 4 :]  # skip past "\n---"
    return (fm_text, body.lstrip("\n"))


def _read_frontmatter_only(md_path: str) -> str:
    """Read only the YAML frontmatter block (up to the closing ``---``) from *md_path*.

    Stops reading once the closing marker is found, avoiding loading the
    entire body text and reference files during discovery.  Returns the
    raw frontmatter text (without the opening ``---``) or ``""`` if no
    frontmatter is found.

    Tolerates leading blank lines and BOM characters so that
    indented/commented frontmatter still parses correctly.
    """
    try:
        with open(md_path, encoding="utf-8-sig") as f:
            # Skip leading blank lines and BOM characters
            line = f.readline()
            while line and line.lstrip("\ufeff").strip() == "":
                line = f.readline()
            if not line:
                return ""
            if not line.lstrip("\ufeff").startswith("---"):
                return ""
            lines: list[str] = []
            for line in f:
                if line.strip() == "---":
                    return "\n".join(lines)
                lines.append(line.rstrip("\r\n"))
    except OSError as e:
        log_debug(f"Failed to read skill frontmatter {md_path}: {e}")
    return ""


# ---------------------------------------------------------------------------
# SkillDefinition
# ---------------------------------------------------------------------------


@dataclass
class SkillDefinition:
    """A loaded skill from the Rikugan skills directory<slug>/SKILL.md."""

    name: str
    description: str
    directory: str
    allowed_tools: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    mode: str = ""  # e.g. "exploration" to trigger exploration mode
    author: str = ""
    version: str = ""
    frontmatter: dict[str, Any] = field(default_factory=dict)
    _body: str | None = field(default=None, repr=False)
    _md_path: str = field(default="", repr=False)

    @property
    def slug(self) -> str:
        """Slug = directory basename, used as /slug invocation."""
        return os.path.basename(self.directory)

    @property
    def body(self) -> str:
        """Lazy-load the body text on first access."""
        if self._body is None:
            self._body = _load_body(self._md_path)
        return self._body


def _load_body(md_path: str) -> str:
    """Read the body (everything after frontmatter) from a SKILL.md file."""
    try:
        with open(md_path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as e:
        raise SkillError(f"Cannot read skill file {md_path}: {e}") from e

    _fm, body = _split_frontmatter(text)
    body = body.strip()

    # Append reference files from <skill>/references/ if they exist
    refs = _load_references(os.path.dirname(md_path))
    if refs:
        body += "\n\n" + refs

    return body


def _load_references(skill_dir: str) -> str:
    """Load .md files from <skill>/references/ and concatenate them.

    Also loads host-specific references from <skill>/references/ida/ depending on the active host, so generic
    skills can ship separate reference docs per tool without injecting
    both into the context.
    """
    from ..core.host import HOST_IDA, host_kind

    refs_dir = os.path.join(skill_dir, "references")
    if not os.path.isdir(refs_dir):
        return ""

    _HOST_SUBDIR = {HOST_IDA: "ida"}

    parts: list[str] = []

    def _load_dir(directory: str) -> None:
        for fname in sorted(os.listdir(directory)):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(directory, fname)
            try:
                with open(fpath, encoding="utf-8-sig") as f:
                    content = f.read().strip()
                if content:
                    parts.append(f"## Reference: {fname}\n{content}")
                    log_debug(f"Loaded skill reference: {fpath}")
            except OSError as e:
                log_error(f"Failed to load skill reference {fpath}: {e}")

    # Flat references — always loaded
    _load_dir(refs_dir)

    # Host-specific subdirectory — only the active host's folder is loaded
    host_subdir = _HOST_SUBDIR.get(host_kind())
    if host_subdir:
        host_refs_dir = os.path.join(refs_dir, host_subdir)
        if os.path.isdir(host_refs_dir):
            _load_dir(host_refs_dir)

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_skills(skills_dir: str) -> list[SkillDefinition]:
    """Scan skills_dir for <slug>/SKILL.md, return loaded SkillDefinitions.

    Each subdirectory with a SKILL.md is treated as a skill.
    **Only frontmatter metadata is loaded eagerly.**  The body text and
    reference files are loaded lazily on first access via ``SkillDefinition.body``.

    This avoids reading potentially large reference files (*.md) during
    discovery, which runs in the background runtime init thread on panel boot.
    """
    if not os.path.isdir(skills_dir):
        log_debug(f"Skills directory not found: {skills_dir}")
        return []

    skills: list[SkillDefinition] = []

    for entry in sorted(os.listdir(skills_dir)):
        skill_dir = os.path.join(skills_dir, entry)
        md_path = os.path.join(skill_dir, "SKILL.md")
        if not os.path.isfile(md_path):
            continue

        try:
            # Read only frontmatter during discovery — the body is loaded
            # lazily on first access via SkillDefinition.body.
            fm_text = _read_frontmatter_only(md_path)
            fm = _parse_frontmatter(fm_text) if fm_text else {}

            # Extract author/version from top-level or nested metadata
            meta = fm.get("metadata", {})
            author = fm.get("author", "")
            version = fm.get("version", "")
            if isinstance(meta, dict):
                author = author or meta.get("author", "")
                version = version or meta.get("version", "")

            # Parse triggers — list of keywords that auto-activate this skill
            raw_triggers = fm.get("triggers", [])
            if isinstance(raw_triggers, str):
                raw_triggers = [t.strip() for t in raw_triggers.split(",") if t.strip()]
            triggers = [t.lower() for t in raw_triggers]

            skill = SkillDefinition(
                name=fm.get("name", entry),
                description=fm.get("description", ""),
                directory=skill_dir,
                allowed_tools=fm.get("allowed_tools", []),
                tags=fm.get("tags", []),
                triggers=triggers,
                mode=fm.get("mode", ""),
                author=author,
                version=version,
                frontmatter=fm,
                _body=None,  # lazy — loaded on first access via SkillDefinition.body
                _md_path=md_path,
            )

            skills.append(skill)
            log_debug(f"Discovered skill: /{entry} — {skill.description or '(no description)'}")

        except (OSError, ValueError, KeyError) as e:
            log_error(f"Failed to load skill from {md_path}: {e}")

    return skills
