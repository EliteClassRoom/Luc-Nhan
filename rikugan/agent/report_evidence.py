"""Static binary evidence collection for ``/report``.

Gathers decompiled pseudocode (or disassembly) for the addresses cited
by verified memories so reports can ground Key Findings and Technical
Details in actual code from the binary. Tool calls go through the
``ToolRegistry`` — the same execution path the agent loop uses — so
capability gating, argument coercion, timeouts, and the IDA dispatch
wrapper all apply. When IDA tools are unavailable the collector
returns an empty list and reports generate without evidence blocks;
the writer then omits ``### Evidence`` subsections.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import ToolError, ToolNotFoundError
from ..core.sanitize import strip_injection_markers, strip_lone_surrogates
from ..memory.paths import extract_addresses
from ..memory.report import EvidenceBlock
from ..memory.schema import KnowledgeMemory

# Must match the untrusted-envelope label used by
# ``_render_evidence_section`` (rikugan/memory/report.py) so the
# breakout neutralization is consistent across both layers.
_EVIDENCE_TAG = "binary_evidence"
_EVIDENCE_BLOCK_TRUNCATION = "\n... (truncated)"

# Scopes that carry static binary evidence.  Executive / IOCs /
# network reports stay prose-only; ``full`` and ``technical`` embed
# code so findings can be verified against the binary.
_EVIDENCE_SCOPES: frozenset[str] = frozenset({"full", "technical"})


def collect_binary_evidence(
    memories: list[KnowledgeMemory],
    registry: Any,
    *,
    scope: str,
    max_addresses: int = 8,
    max_block_chars: int = 6000,
    max_total_chars: int = 32000,
) -> list[EvidenceBlock]:
    """Collect static evidence blocks for *scope*.

    Only ``full`` and ``technical`` scopes carry evidence; every other
    scope (and a ``None`` registry) returns ``[]``. Addresses are
    harvested — first-seen order, capped at *max_addresses* — from each
    memory's ``title``, ``content``, ``verdict_claim``, and
    ``verification_citations``.

    Per address the collector prefers Hex-Rays pseudocode
    (``decompile_function``); on :class:`ToolError` /
    :class:`ToolNotFoundError` it falls back to
    ``read_function_disassembly`` (or ``read_disassembly`` when only
    the flat reader exists). Any other failure skips the address
    silently. Collection stops once the cumulative text would exceed
    *max_total_chars*.
    """
    if scope not in _EVIDENCE_SCOPES:
        return []
    if registry is None:
        return []
    addresses = _collect_addresses(memories, max_addresses)
    if not addresses:
        return []
    available = _available_tool_names(registry)
    if not available:
        return []
    blocks: list[EvidenceBlock] = []
    total = 0
    for address in addresses:
        block = _evidence_for_address(registry, available, address, max_block_chars)
        if block is None:
            continue
        if total + len(block.text) > max_total_chars:
            break
        blocks.append(block)
        total += len(block.text)
    return blocks


def fetch_binary_info(registry: Any) -> str:
    """Return ``get_binary_info`` output for the report's File Metadata.

    Empty string when the registry is missing, the tool is not
    available, or execution fails — never raises.
    """
    if registry is None:
        return ""
    if "get_binary_info" not in _available_tool_names(registry):
        return ""
    try:
        result = registry.execute("get_binary_info", {})
    except Exception:
        return ""
    return str(result) if result else ""


def _available_tool_names(registry: Any) -> set[str]:
    """Snapshot the capability-filtered tool names.

    Defensive: a fake or partially-initialized registry must not blow
    up the whole report — an unreadable snapshot means "no tools".
    """
    try:
        return {t.name for t in registry.list_available_tools()}
    except Exception:
        return set()


def _collect_addresses(memories: list[KnowledgeMemory], max_addresses: int) -> list[str]:
    """Harvest deduplicated addresses (first-seen order) up to the cap."""
    seen: dict[str, None] = {}
    for memory in memories:
        fields = [memory.title, memory.content, memory.verdict_claim]
        fields.extend(memory.verification_citations or [])
        for text in fields:
            for address in extract_addresses(text):
                seen.setdefault(address, None)
                if len(seen) >= max_addresses:
                    return list(seen)
    return list(seen)


def _evidence_for_address(
    registry: Any,
    available: set[str],
    address: str,
    max_block_chars: int,
) -> EvidenceBlock | None:
    """Fetch one evidence block for *address* (pseudocode → disassembly).

    Returns ``None`` when no tool can serve the address (silent skip).
    """
    if "decompile_function" in available:
        try:
            result = registry.execute("decompile_function", {"address": address})
        except (ToolError, ToolNotFoundError):
            # Decompiler unavailable for this address — fall through
            # to disassembly.  Both exceptions are caught explicitly so
            # the fallback chain works regardless of inheritance.
            pass
        except Exception:
            return None  # unexpected failure — skip the address
        else:
            return EvidenceBlock(
                address,
                "pseudocode",
                _sanitize_block(str(result), max_block_chars),
            )
    try:
        if "read_function_disassembly" in available:
            result = registry.execute("read_function_disassembly", {"address": address})
        elif "read_disassembly" in available:
            result = registry.execute("read_disassembly", {"address": address, "count": 40})
        else:
            return None
    except Exception:
        return None
    return EvidenceBlock(
        address,
        "disassembly",
        _sanitize_block(str(result), max_block_chars),
    )


def _sanitize_block(text: str, max_block_chars: int) -> str:
    """Sanitize tool output: surrogates, markers, envelope breakout, cap."""
    s = strip_lone_surrogates(text)
    s = strip_injection_markers(s)
    s = s.replace(f"</{_EVIDENCE_TAG}>", f"[/{_EVIDENCE_TAG}]")
    if len(s) > max_block_chars:
        s = s[:max_block_chars] + _EVIDENCE_BLOCK_TRUNCATION
    return s
