"""Agent loop: generator-based turn cycle with tool orchestration."""

from __future__ import annotations

import dataclasses
import json
import math
import os
import queue
import threading
import time
import traceback
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from typing import Any

from .. import constants
from ..core.config import RikuganConfig
from ..core.errors import (
    CancellationError,
    ProviderError,
    RateLimitError,
    ToolError,
    ToolNotFoundError,
)
from ..core.logging import log_debug, log_error, log_info
from ..core.sanitize import (
    sanitize_skill_body,
    sanitize_tool_result,
    strip_injection_markers,
    strip_iocs,
    strip_lone_surrogates,
)
from ..core.types import (
    AttemptUsage,
    LLMRequestContext,
    Message,
    Role,
    TokenUsage,
    ToolCall,
    ToolResult,
    TurnDisposition,
    TurnOutcome,
    coerce_token_count,
)
from ..providers.base import LLMProvider
from ..skills.registry import SkillRegistry
from ..state.session import SessionState
from ..tools.coercion import coerce_bool
from ..tools.registry import ToolRegistry
from ..tools.traceback_classifier import TracebackClassification
from ..tools.validate_idapython import validate_idapython
from .context_window import ContextWindowManager
from .exploration_mode import (
    ExplorationPhase,
    ExplorationState,
    Finding,
    FunctionInfo,
    KnowledgeBase,
    PatchRecord,
)
from .glm_guard import GLMGuardSnapshot, GLMReasoningGuard
from .loop_commands import (
    ACTIVE_GOAL_METADATA_KEY,
    _handle_doctor_command,
    _handle_goal_command,
    _handle_knowledge_command,
    _handle_mcp_command,
    _handle_memory_command,
    _handle_report_command,
    _handle_undo_command,
    _handle_verify_command,
    normalize_goal,
)
from .minify import minify_messages, minify_text
from .modes.a2a import run_a2a_mode
from .modes.exploration import run_exploration_mode
from .modes.normal import run_normal_loop
from .modes.orchestra import run_orchestra_mode
from .modes.plan import run_plan_mode
from .modes.research import run_research_mode
from .mutation import MutationRecord, build_reverse_record, capture_pre_state
from .pseudo_tool_schemas import (
    ASK_USER_SCHEMA,
    DELEGATE_EXTERNAL_TASK_SCHEMA,
    EXPLORATION_REPORT_SCHEMA,
    PHASE_TRANSITION_SCHEMA,
    RESEARCH_NOTE_SCHEMA,
    SAVE_MEMORY_SCHEMA,
    SPAWN_SUBAGENT_SCHEMA,
)
from .subagent import SubagentRunner
from .system_prompt import build_system_prompt
from .turn import TurnEvent, TurnEventType

_MIN_CONTEXT_WINDOW_TOKENS = 8_000

# Backward-compat alias — the canonical key now lives in loop_commands
# so the parser, the state-only handler, and the prompt builder all
# share one source of truth.
_GOAL_METADATA_KEY = ACTIVE_GOAL_METADATA_KEY

# High-confidence mutating tools for which post-state verification runs.
_VERIFY_MUTATION_TOOLS: frozenset[str] = frozenset(
    {
        "rename_function",
        "rename_address",
        "set_comment",
        "set_function_comment",
        "set_pseudocode_comment",
    }
)


@dataclass
class _StreamToolState:
    """Ordered per-tool-call state during streaming.

    Tracks the lifecycle of each tool call by **start order** (the order
    in which ``is_tool_call_start`` arrived).  At stream finalisation
    time we walk this list to find the contiguous safe prefix: every
    state before the first incomplete/errored state is safe; the broken
    state and all states after it are discarded.

    For GLM providers, malformed JSON does NOT fall back to ``{}`` --
    ``parse_error`` is set and the call is discarded.  For non-GLM
    providers the existing ``{}`` + warning fallback is preserved.
    """

    id: str
    name: str = ""
    raw_args: list[str] = field(default_factory=list)
    started: bool = False
    ended: bool = False
    parsed_arguments: dict[str, Any] | None = None
    parse_error: str = ""


_FAILURE_PREFIXES = (
    "failed",
    "error:",
    "error ",
    "decompilation failed",
    "decompilation returned none",
    "no function",
    "no segment",
    "no decompilation",
    "hex-rays not available",
    "hex-rays decompiler not available",
    "ida_typeinf not available",
    "idc module not available",
)

_FAILURE_SUBSTRINGS = (
    " not found",
    " not available",
    "outside function",
    "outside segment",
)


# Orchestra temporary safety gate. The Orchestra execution path is
# disabled pending its shared execution-policy and context-isolation
# hardening (see docs/superpowers/specs/2026-07-22-memory-durability-and-
# orchestra-gate-design.md §10). The command remains recognized so users
# receive a precise disabled message instead of an unknown-command error;
# the early gate below returns one ``TEXT_DONE`` event and exits before
# skill resolution, session append, prompt/schema construction, retrieval,
# provider call, or tool execution.
#
# Focused legacy tests may monkeypatch this constant to ``True`` only
# inside the test process; production defaults remain disabled.
_ORCHESTRA_ENABLED = False
_ORCHESTRA_DISABLED_MESSAGE = (
    "Orchestra is temporarily disabled while its execution and context isolation contracts are being hardened."
)


def _result_indicates_failure(result: str) -> bool:
    """Heuristic check: does the tool result string indicate a clear failure?"""
    if not isinstance(result, str):
        return False
    lower = result.strip().lower()
    if lower.startswith(_FAILURE_PREFIXES):
        return True
    return any(needle in lower for needle in _FAILURE_SUBSTRINGS)


@dataclasses.dataclass(frozen=True)
class _MutationVerification:
    """Result of post-state mutation verification.

    *ok* is ``True`` when the mutating tool is confirmed to have
    succeeded.  *reason* carries a human-readable explanation when
    ``ok`` is ``False``.
    """

    ok: bool
    reason: str = ""


@dataclasses.dataclass
class _ParsedCommand:
    """Result of parsing a user message for slash-command prefixes."""

    message: str
    use_plan_mode: bool = False
    use_exploration_mode: bool = False
    explore_only: bool = False
    use_research_mode: bool = False
    use_orchestra_mode: bool = False
    use_a2a_mode: bool = False  # /a2a <agent> <message...> direct delegation
    direct_command: str = ""
    direct_arg: str = ""  # remainder after the direct command token
    # When set, the run loop should store this goal in
    # ``session.metadata[ACTIVE_GOAL_METADATA_KEY]`` BEFORE building
    # the system prompt so the freshly constructed prompt includes the
    # `## Active Goal` section. Used by ``/goal <objective>``.
    goal_to_set: str = ""


def _parse_user_command(user_message: str) -> _ParsedCommand:
    """Strip slash-command prefixes and return a _ParsedCommand descriptor.

    Direct commands (/goal, /memory, /undo, /mcp, /doctor) set `direct_command`.
    Mode prefixes (/plan, /modify, /explore) set the corresponding flag and
    strip the prefix from `message`.  Plain messages are returned unchanged.
    """
    stripped = user_message.strip()
    lower = stripped.lower()
    if lower.startswith("/plan "):
        return _ParsedCommand(message=stripped[6:].strip(), use_plan_mode=True)
    if lower.startswith("/modify "):
        return _ParsedCommand(message=stripped[8:].strip(), use_exploration_mode=True)
    if lower.startswith("/explore "):
        return _ParsedCommand(
            message=stripped[9:].strip(),
            use_exploration_mode=True,
            explore_only=True,
        )
    if lower.startswith("/research "):
        return _ParsedCommand(message=stripped[10:].strip(), use_research_mode=True)
    if lower == "/goal" or lower.startswith("/goal "):
        arg = stripped[5:].strip()
        # `/goal <objective>` (anything that is not the state-only
        # clear/reset/unset commands) becomes a normal run for the
        # objective text, with the parsed goal recorded so the loop
        # updates ``session.metadata`` before the system prompt is
        # built. State-only forms keep the direct-command path so they
        # stay as immediate UI acknowledgements.
        if arg and arg.lower() not in {"clear", "reset", "unset"}:
            goal = normalize_goal(arg)
            if goal:
                return _ParsedCommand(message=goal, goal_to_set=goal)
        return _ParsedCommand(
            message=stripped,
            direct_command="/goal",
            direct_arg=arg,
        )
    if lower == "/memory":
        return _ParsedCommand(message=stripped, direct_command="/memory")
    if lower.startswith("/undo"):
        return _ParsedCommand(
            message=stripped,
            direct_command="/undo",
            direct_arg=stripped,
        )
    if lower == "/mcp":
        return _ParsedCommand(message=stripped, direct_command="/mcp")
    if lower == "/doctor":
        return _ParsedCommand(message=stripped, direct_command="/doctor")
    if lower == "/knowledge" or lower.startswith("/knowledge "):
        return _ParsedCommand(
            message=stripped,
            direct_command="/knowledge",
            direct_arg=stripped[10:].strip() if len(stripped) > 10 else "",
        )
    if lower == "/report" or lower.startswith("/report "):
        return _ParsedCommand(
            message=stripped,
            direct_command="/report",
            direct_arg=stripped[7:].strip() if len(stripped) > 7 else "",
        )
    if lower == "/verify" or lower.startswith("/verify "):
        return _ParsedCommand(
            message=stripped,
            direct_command="/verify",
            direct_arg=stripped[7:].strip() if len(stripped) > 7 else "",
        )
    if lower == "/orchestra" or lower.startswith("/orchestra "):
        return _ParsedCommand(message=stripped[10:].strip() if len(stripped) > 10 else "", use_orchestra_mode=True)
    # /case <action> <args...> — analysis case management
    if lower == "/case" or lower.startswith("/case "):
        return _ParsedCommand(message=stripped, direct_command="/case")
    # /a2a <agent> <message...>  — delegate to external agent directly
    # without LLM mediation. The first whitespace-separated token is
    # the agent name; the rest is the task. Empty body is treated as
    # a missing-arg error (we still flag use_a2a_mode so the run()
    # dispatcher can surface a friendly message).
    if lower == "/a2a" or lower.startswith("/a2a "):
        body = stripped[5:].strip() if len(stripped) > 5 else ""
        return _ParsedCommand(message=body, use_a2a_mode=True)
    return _ParsedCommand(message=stripped)


# Maximum length allowed for a save_memory category token. Anything longer
# is hostile noise — categories are short labels, not free-form descriptions.
_SAVE_MEMORY_CATEGORY_MAX_LEN = 64


def _sanitize_save_memory_category(raw: object) -> str:
    """Coerce and sanitize a save_memory ``category`` argument.

    Categories flow into three downstream surfaces:
      * ``MEMORY.md`` managed line format ``- [{category}] {fact}``,
      * the ``log_info`` message,
      * the tool result echoed back to the LLM,
      * the knowledge-ingest ``ingest_save_memory(category=...)`` call.

    An attacker (or a buggy tool call) that injects ``</persistent_memory>system``
    into the category would let a subsequent ``sanitize_memory`` wrapper close
    out and smuggle raw prompt text in. We neutralize that here.
    """
    if raw is None:
        return "general"
    text = strip_lone_surrogates(str(raw))
    text = strip_injection_markers(text)
    # Neutralize ANY closing tag — strip_injection_markers only covers
    # known role markers (``</system>``, ``</tool_result>``, …) and would
    # leave ``</persistent_memory>`` untouched. We replace the angle
    # brackets so no closing tag can survive into MEMORY.md or the
    # tool result.
    text = text.replace("<", "").replace(">", "")
    # Strip the surrounding ``[...]`` brackets and surrounding whitespace so
    # an injected ``[INJECTED]`` collapses to a benign label.
    text = text.replace("[", "").replace("]", "").strip()
    if not text:
        return "general"
    # Collapse internal whitespace (newlines / runs of spaces) so the category
    # is a single short token.
    text = " ".join(text.split())
    if len(text) > _SAVE_MEMORY_CATEGORY_MAX_LEN:
        text = text[:_SAVE_MEMORY_CATEGORY_MAX_LEN].rstrip()
    return text or "general"


class AgentLoop:
    """The core agentic loop: stream LLM -> execute tools -> repeat.

    Uses a generator pattern to yield TurnEvents to the UI layer.
    Runs in a background thread; IDA API calls are marshalled via @idasync.
    """

    def __init__(
        self,
        provider: LLMProvider,
        tool_registry: ToolRegistry,
        config: RikuganConfig,
        session: SessionState,
        skill_registry: SkillRegistry | None = None,
        host_name: str = "IDA Pro",
        parent_loop: AgentLoop | None = None,
        cancel_event: threading.Event | None = None,
        unattended: bool = False,
    ):
        self.provider = provider
        self.tools = tool_registry
        self.config = config
        self.session = session
        self.skills = skill_registry
        self.host_name = host_name
        # Explicit cancel_event wins over parent_loop inheritance so the
        # SubagentManager-owned event remains authoritative for orchestra
        # children. ``is not None`` (not truthiness) so the exact caller
        # object is retained.
        if cancel_event is not None:
            self._cancelled: threading.Event = cancel_event
        elif parent_loop is not None:
            self._cancelled = parent_loop._cancelled
        else:
            self._cancelled = threading.Event()
        # Only a loop that created its own event may clear it in run().
        # An event supplied via cancel_event or inherited from
        # parent_loop belongs to the caller — clearing it would silently
        # discard a user cancellation that arrived between runs.
        self._owns_cancel_event: bool = cancel_event is None and parent_loop is None
        self._running: bool = False
        self._consecutive_errors: int = 0
        self._tools_disabled_for_turn: bool = False
        # Subagents share the parent's queues so UI signals reach them.
        self._user_answer_queue: queue.Queue[str] = (
            parent_loop._user_answer_queue if parent_loop else queue.Queue(maxsize=1)
        )
        self._tool_approval_queue: queue.Queue[str] = (
            parent_loop._tool_approval_queue if parent_loop else queue.Queue(maxsize=1)
        )
        self._approval_queue: queue.Queue[str] = parent_loop._approval_queue if parent_loop else queue.Queue(maxsize=1)
        self._always_allow_scripts: bool = parent_loop._always_allow_scripts if parent_loop else False
        # Unattended loops run without an attached UI: nobody answers
        # approval/question queues, so interactive gates must be removed from
        # the tool surface and rejected at dispatch (see _build_tools_schema
        # and the _unattended guards in the tool handlers).
        self._unattended: bool = unattended
        self.plan_mode = False

        # Post-error docs-review: max 1 reviewer call per user message.
        # Reset at the start of run() so each user task gets a fresh budget.
        self._docs_reviewer_invoked: bool = False

        # Context window manager — compacts history when approaching limits
        ctx_window = getattr(config.provider, "context_window", 0) or 128000
        self._context_manager = ContextWindowManager(
            max_tokens=ctx_window,
            compaction_threshold=0.8,
        )

        # Central memory service (set by controller when binary identity is resolved).
        # When None, save_memory and /memory report central memory unavailable.
        self.memory_service = None
        self._memory_authority = None
        # Mutation log for /undo support — typed once (see MutationRecord).
        # Guarded by _mutation_lock: subagent threads append via
        # record_mutations while the loop thread appends in
        # _execute_single_tool (ThreadPoolExecutor parallel runs).
        self._mutation_log: list[MutationRecord] = []
        self._mutation_lock = threading.Lock()

        # Exploration mode state (populated when /modify or /explore is used)
        self._exploration_state: ExplorationState | None = None
        self._last_knowledge_base: KnowledgeBase | None = None

        # Research mode state (populated when /research is used)
        from .modes.research import ResearchState as _RS

        self._research_state: _RS | None = None

    @property
    def unattended(self) -> bool:
        """True when no UI is attached to answer approval/question gates."""
        return self._unattended

    def drain_mutations(self) -> list[MutationRecord]:
        """Return this loop's mutation records and clear the log.

        Used by :class:`SubagentRunner` to hand a finished child run's
        mutations to the parent loop's /undo log.
        """
        with self._mutation_lock:
            records = list(self._mutation_log)
            self._mutation_log.clear()
            return records

    def record_mutations(self, records: list[MutationRecord]) -> None:
        """Append mutation records to this loop's /undo log.

        Thread-safe: subagent threads call this concurrently with the
        loop's own appends.
        """
        if not records:
            return
        with self._mutation_lock:
            self._mutation_log.extend(records)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_knowledge_base(self) -> KnowledgeBase | None:
        """The knowledge base from the most recent exploration run."""
        if self._exploration_state is not None:
            return self._exploration_state.knowledge_base
        return self._last_knowledge_base

    def _clear_exploration_state(self) -> None:
        """Save knowledge base and reset exploration state."""
        if self._exploration_state is not None:
            self._last_knowledge_base = self._exploration_state.knowledge_base
            self._exploration_state = None

    def _ensure_research_state(self) -> None:
        """Lazily create a minimal ResearchState for continuation after cancel."""
        if self._research_state is not None:
            return
        from .modes.research import ResearchState

        idb_dir = os.path.dirname(self.session.idb_path) if self.session.idb_path else os.getcwd()
        notes_dir = os.path.join(idb_dir, "notes")
        os.makedirs(notes_dir, exist_ok=True)
        self._research_state = ResearchState(
            notes_dir=notes_dir,
            max_explore_turns=self.config.exploration_turn_limit,
        )

    def _ensure_exploration_state(self) -> ExplorationState:
        """Lazily create a minimal ExplorationState for continuation after cancel.

        ``explore_only`` must match the active mode so resumed
        research findings still auto-ingest (Plan §5.46) while
        resumed ``/explore`` findings remain provisional until the
        post-explore finalizer verifies them.
        """
        if self._exploration_state is None:
            active = self.session.metadata.get("active_mode", "")
            explore_only = active == "exploration"
            self._exploration_state = ExplorationState(explore_only=explore_only)
        return self._exploration_state

    def cancel(self) -> None:
        """Cancel the current run."""
        self._cancelled.set()

    def _drain_queue(self, q: queue.Queue[str]) -> None:
        """Remove any stale item from a maxsize=1 queue (non-blocking)."""
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                break

    def submit_user_answer(self, answer: str) -> None:
        """Submit an answer to an ask_user question (called from UI thread)."""
        self._drain_queue(self._user_answer_queue)
        self._user_answer_queue.put(answer)

    def submit_tool_approval(self, decision: str) -> None:
        """Submit tool approval decision: 'allow', 'allow_all', or 'deny'."""
        self._drain_queue(self._tool_approval_queue)
        self._tool_approval_queue.put(decision)

    def submit_approval(self, decision: str) -> None:
        """Submit orchestra delegation approval decision: 'approve' or 'deny'."""
        self._drain_queue(self._approval_queue)
        self._approval_queue.put(decision)

    def get_approval_queue(self) -> queue.Queue[str]:
        """Return the orchestra approval queue for UI routing."""
        return self._approval_queue

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise CancellationError("Agent run cancelled")

    def _wait_for_queue(self, q: queue.Queue[str]) -> str:
        """Block until a value arrives on `q`, checking for cancellation."""
        while True:
            self._check_cancelled()
            try:
                return q.get(timeout=0.5)
            except queue.Empty:
                continue  # poll timeout — retry until item arrives or cancelled

    def _handle_case_command(self, user_message: str) -> Generator[TurnEvent, None, None]:
        """Handle /case slash commands."""
        from ..memory.case_commands import dispatch_case_command, parse_case_command

        parsed = parse_case_command(user_message)
        if parsed is None:
            yield TurnEvent.text_done("Invalid /case command.")
            return

        if self.memory_service is None:
            yield TurnEvent.text_done("Central memory is not available for this binary.")
            return

        try:
            from ..memory.case_repository import CaseRepository
            from ..memory.case_service import CaseMemoryService

            # Build case repository and service from the current session's memory binding
            session = self.session
            if not session.binary_memory_id:
                yield TurnEvent.text_done("No active binary workspace binding.")
                return

            # Access the manager through the controller (if available) or create a temporary one

            manager = getattr(self, "_memory_manager", None)
            if manager is None:
                yield TurnEvent.text_done("Case operations require an active controller session.")
                return

            cases = CaseRepository(manager._registry, manager.locator)
            case_service = CaseMemoryService(cases, binary_repository=self.memory_service.repository)
            result = dispatch_case_command(
                parsed,
                case_repository=cases,
                case_service=case_service,
                manager=manager,
                authority=self._memory_authority,
                context=manager.run_context(),
            )
            yield TurnEvent.text_done(result)
        except Exception as e:
            yield TurnEvent.error_event(f"Case command failed: {e}")

    def _build_system_prompt(self) -> str:
        profile = self.config.get_active_profile()
        binary_info = None
        current_address = None
        current_function = None

        if self.config.auto_context and not profile.hide_binary_metadata:
            try:
                binary_info = self.tools.execute("get_binary_info", {})
            except Exception as e:
                log_debug(f"get_binary_info failed: {e}")
            try:
                current_address = self.tools.execute("get_cursor_position", {})
                current_function = self.tools.execute("get_current_function", {})
            except Exception as e:
                log_debug(f"cursor/function context failed: {e}")

        skill_summary = None
        if self.skills:
            skill_summary = self.skills.get_summary_for_prompt()

        # Central memory prompt sources: structured facts from SQLite and
        # manual notes from unmanaged MEMORY.md. Both supplied by
        # BinaryMemoryService when wired, otherwise stay empty.
        structured_memory = ""
        manual_memory_notes = ""
        if self.memory_service is not None:
            try:
                structured_memory = self.memory_service.structured_context()
                manual_memory_notes = self.memory_service.manual_notes_context()
            except Exception as e:
                log_debug(f"central memory context load failed: {e}")

        # Retrieved knowledge — per-turn compilation of stored memories,
        # entities, relations, and note excerpts relevant to the current
        # cursor/function/goal. Disabled via ``knowledge_enabled`` config
        # field, so users running with knowledge off get no overhead.
        extra_context = self._build_retrieved_knowledge_section(
            current_address=current_address,
            current_function=current_function,
            profile=profile,
        )

        return build_system_prompt(
            host_name=self.host_name,
            binary_info=binary_info,
            current_function=current_function,
            current_address=current_address,
            extra_context=extra_context,
            active_goal=self.session.metadata.get(_GOAL_METADATA_KEY, ""),
            tool_names=self.tools.list_names(),
            skill_summary=skill_summary,
            profile=profile,
            # Cached — first call rebuilds, every subsequent call returns
            # the same string.  Invalidation happens when a tool is
            # registered/unregistered or capabilities change.
            tools_table=self.tools.tools_catalog(),
            structured_memory=structured_memory,
            manual_memory_notes=manual_memory_notes,
            hide_strings=bool(self.config.hide_strings),
        )

    def _build_retrieved_knowledge_section(
        self,
        current_address: str | None,
        current_function: str | None,
        profile,
    ) -> str:
        """Return the per-turn Retrieved Knowledge block, or "" if disabled/unavailable."""
        try:
            if not getattr(self.config, "knowledge_enabled", True):
                return ""

            active_mode = self.session.metadata.get("active_mode", "normal") or "normal"
            active_goal = self.session.metadata.get(_GOAL_METADATA_KEY, "")

            # Compute the retrieval budget ONCE so both the SQLite and JSONL
            # paths honor the user's ``knowledge_max_context_items`` /
            # ``knowledge_max_context_chars`` config. Previously the SQLite
            # branch silently passed ``budget=None``, falling back to the
            # default ``NORMAL_BUDGET`` and ignoring user caps.
            from ..memory.context import budget_from_config

            budget = budget_from_config(self.config, active_mode=active_mode)

            # Prefer SQLite when the central memory service is wired.
            if self.memory_service is not None:
                from ..memory.sqlite_retrieval import repository_to_retrieval_pack

                pack = repository_to_retrieval_pack(
                    self.memory_service.repository,
                    current_address=current_address,
                    current_function=current_function,
                    active_mode=active_mode,
                    active_goal=active_goal,
                    budget=budget,
                )
            else:
                # Fallback: JSONL store via the legacy ingest/retrieve path.
                from ..memory.context import RetrievalQuery
                from ..memory.ingest import make_store
                from ..memory.retrieve import retrieve as jsonl_retrieve

                store, paths = make_store(self.session.idb_path)
                if store is None or paths is None:
                    return ""

                func_name = ""
                if current_function:
                    # Try to extract a name like "func_name @ 0x401000" —
                    # the second line of ``get_current_function`` output
                    # is the name when present.
                    for line in (current_function or "").splitlines():
                        line = line.strip()
                        if not line or line.lower().startswith("address") or line.lower().startswith("function:"):
                            continue
                        func_name = line
                        break

                query = RetrievalQuery(
                    text=" ".join(
                        filter(
                            None,
                            [
                                current_address or "",
                                current_function or "",
                                func_name,
                                active_goal,
                            ],
                        )
                    ),
                    address=current_address or "",
                    function_name=func_name,
                    active_goal=active_goal,
                    active_mode=active_mode,
                )
                pack = jsonl_retrieve(
                    store,
                    paths,
                    query,
                    max_memories=budget.max_memories,
                    max_entities=budget.max_entities,
                    max_relations=budget.max_relations,
                    max_notes=budget.max_notes,
                    expand_relations=True,
                )

            # Both paths share the same renderer.
            from ..memory.context import build_retrieval_metadata, build_section_from_pack

            section = build_section_from_pack(pack)
            # Record retrieval metadata so the UI can show a compact
            # retrieved-knowledge indicator when configured.  Reuse the
            # same pack we just built — do NOT re-run retrieve.
            try:
                meta = build_retrieval_metadata(pack)
                self.session.metadata["last_knowledge_retrieval"] = meta
            except Exception:
                pass

            return section
        except Exception as e:
            log_debug(f"retrieved knowledge section failed: {e}")
            return ""

    def _resolve_skill(self, user_message: str) -> tuple:
        """Rewrite user message if it matches a skill.

        Checks explicit /slug invocation first, then falls back to
        trigger pattern matching on the user's natural language.

        Returns (rewritten_message, skill_or_None). Plan-mode skills are
        the exception: the message is returned unrewritten (the plan-mode
        runner injects the skill body exactly once via
        _SKILL_PLAN_GENERATION_PROMPT — rewriting here would duplicate it).
        """
        if not self.skills:
            return (user_message, None)

        # 1. Explicit /slug invocation
        skill, remaining = self.skills.resolve_skill_invocation(user_message)
        if skill is not None:
            log_debug(f"AgentLoop: skill invocation /{skill.slug}")
            if skill.mode == "plan":
                return (remaining, skill)
            rewritten = (
                f"[Skill: {skill.name}]\n{sanitize_skill_body(skill.body, skill.name)}\n\nUser request: {remaining}"
            )
            return (rewritten, skill)

        # 2. Trigger pattern matching on natural language
        skill = self.skills.match_triggers(user_message)
        if skill is not None:
            log_debug(f"AgentLoop: trigger-matched skill /{skill.slug}")
            if skill.mode == "plan":
                return (user_message, skill)
            rewritten = (
                f"[Skill: {skill.name}]\n{sanitize_skill_body(skill.body, skill.name)}\n\nUser request: {user_message}"
            )
            return (rewritten, skill)

        return (user_message, None)

    def _format_provider_error_for_user(self, error: ProviderError) -> str:
        """Return a user-facing provider error message for chat display."""
        provider = error.provider or self.config.provider.name or "provider"
        detail = str(error).strip() or "Request failed."

        if isinstance(error, RateLimitError):
            return f"{provider}: rate limit exceeded. {detail}"
        return f"{provider}: {detail}"

    def _stream_llm_turn(
        self,
        system_prompt: str,
        tools_schema: list | None,
        max_retries: int = 0,
        request_context: LLMRequestContext | None = None,
    ) -> Generator[TurnEvent, None, TurnOutcome]:
        """Stream one LLM call, yielding events. Retries on transient errors.

        Returns a :class:`TurnOutcome` carrying visible text, reasoning
        content, tool calls, usage, raw parts, finish reason, disposition,
        and guard trigger info.

        *max_retries* of 0 (default) reads from ``config.max_retries``.

        *request_context* carries attempt-local state (attempt number,
        recovery flag, system suffix, max_tokens override, disable_thinking)
        so the provider can adapt the payload (e.g. GLM one-shot recovery).
        When ``None``, the provider receives a default context (no suffix,
        no override, thinking enabled) and the wire payload is identical
        to the pre-context behaviour.
        """
        if max_retries <= 0:
            max_retries = self.config.max_retries or 3
        silent_mode = self.config.silent_retry_mode

        last_error: Exception | None = None
        for attempt in range(max_retries):
            self._check_cancelled()
            try:
                result = yield from self._stream_llm_turn_inner(
                    system_prompt, tools_schema, request_context=request_context
                )
                return result
            except (RateLimitError, ProviderError) as e:
                is_rate_limit = isinstance(e, RateLimitError)
                if not is_rate_limit and not (e.retryable and attempt < max_retries - 1):
                    raise
                last_error = e
                log_error(f"Retryable error (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    if is_rate_limit:
                        backoff = e.retry_after if e.retry_after > 0 else min(2**attempt, 10)
                    else:
                        backoff = min(2**attempt, 10)
                    if silent_mode:
                        yield TurnEvent.error_event(
                            f"\u23f3 Retrying in {backoff:.0f}s (attempt {attempt + 2}/{max_retries})..."
                        )
                    else:
                        yield TurnEvent.error_event(
                            f"{self._format_provider_error_for_user(e)} "
                            f"Retrying in {backoff:.0f}s (attempt {attempt + 2}/{max_retries})."
                        )
                    deadline = time.monotonic() + backoff
                    while time.monotonic() < deadline:
                        self._check_cancelled()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        time.sleep(min(0.5, remaining))
                continue

        # All retries exhausted
        raise last_error  # type: ignore[misc]

    def _maybe_inject_error_hint(self) -> None:
        """Inject a system hint when consecutive tool errors exceed thresholds."""
        if self._consecutive_errors >= 5:
            self._tools_disabled_for_turn = True
            self._consecutive_errors = 0
            self.session.add_message(
                Message(
                    role=Role.USER,
                    content=(
                        "[SYSTEM] You have failed 5 consecutive tool calls. "
                        "Tools are temporarily disabled. Explain what went wrong "
                        "and what you were trying to do. The user may help you. "
                        "Tools will be re-enabled on your next turn."
                    ),
                )
            )
        elif self._consecutive_errors >= 3:
            self.session.add_message(
                Message(
                    role=Role.USER,
                    content=(
                        "[SYSTEM] You have failed 3 consecutive tool calls. "
                        "Stop retrying the same approach. Try a different strategy "
                        "or explain what is failing."
                    ),
                )
            )

    def _prepare_provider_messages(self, system_prompt: str) -> tuple[list, int, TokenUsage | None]:
        """Estimate tokens, compact context if needed, return (provider_messages, estimated_tokens, estimated_usage)."""
        preserve = self.config.preserve_context

        # Fast path: use running token counter to skip expensive O(n)
        # estimation when we're clearly below the compaction threshold.
        fast_estimate = self.session.token_estimate
        if fast_estimate > 0 and fast_estimate < int(self._context_manager.max_tokens * 0.5):
            # Well below threshold — skip full estimation for compaction
            pass
        else:
            # Estimate full in-memory context so compaction decisions work
            # even when provider streaming usage is missing.
            #
            # We estimate on the raw (un-minified) message list: minify only
            # strips redundant whitespace, so skipping it here makes the
            # token estimate marginally HIGHER, which is safe — compaction is
            # conservative (triggers a touch early rather than late). This
            # avoids a second full-history minify pass; the actual provider
            # messages below are still minified before being sent.
            full_messages = self.session.get_messages_for_provider(context_window=0)
            full_prompt_tokens = self._estimate_prompt_tokens(full_messages, system_prompt)
            if full_prompt_tokens > 0:
                self._context_manager.update_usage(
                    TokenUsage(
                        prompt_tokens=full_prompt_tokens,
                        total_tokens=full_prompt_tokens,
                    )
                )

        if self._context_manager.should_compact():
            log_info(f"Context compaction triggered (usage ratio: {self._context_manager.usage_ratio:.1%})")
            # Use the dedicated replace_messages() method so the cache
            # invalidation hooks (revision bump, provider-message cache
            # clear, token-estimate recompute) all fire — bypassing them
            # would let stale cached messages flow back into the next
            # get_messages_for_provider() call.
            compacted = self._context_manager.compact_messages(self.session.messages)
            self.session.replace_messages(compacted)

        ctx_window = self.config.provider.context_window
        provider_messages = minify_messages(
            self.session.get_messages_for_provider(
                context_window=ctx_window,
                preserve_context=preserve,
            )
        )
        estimated_prompt_tokens = self._estimate_prompt_tokens(provider_messages, system_prompt)
        estimated_usage: TokenUsage | None = None
        if estimated_prompt_tokens > 0:
            estimated_usage = TokenUsage(
                prompt_tokens=estimated_prompt_tokens,
                total_tokens=estimated_prompt_tokens,
            )
            self._context_manager.update_usage(estimated_usage)
        return provider_messages, estimated_prompt_tokens, estimated_usage

    def _accumulate_chunk_usage(self, last: TokenUsage | None, chunk: TokenUsage) -> TokenUsage:
        """Merge a streaming chunk's usage into the accumulated total.

        All numeric fields are coerced through ``coerce_token_count`` so that
        provider SDKs returning ``None`` (or non-numeric values) can never
        raise ``TypeError`` on the addition below.
        """
        prompt = coerce_token_count(chunk.prompt_tokens)
        completion = coerce_token_count(chunk.completion_tokens)
        cache_read = coerce_token_count(chunk.cache_read_tokens)
        cache_creation = coerce_token_count(chunk.cache_creation_tokens)
        chunk_total = coerce_token_count(chunk.total_tokens)

        if last is None:
            # First chunk: build a fresh TokenUsage with coerced values.
            derived_total = chunk_total if chunk_total > 0 else (prompt + completion)
            return TokenUsage(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=derived_total,
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_creation,
            )

        # Later chunks: add the chunk deltas to the (already normalized) accumulator.
        last_prompt = coerce_token_count(last.prompt_tokens)
        last_completion = coerce_token_count(last.completion_tokens)
        last_cache_read = coerce_token_count(last.cache_read_tokens)
        last_cache_creation = coerce_token_count(last.cache_creation_tokens)
        last_total = coerce_token_count(last.total_tokens)

        new_prompt = last_prompt + prompt
        new_completion = last_completion + completion
        computed_total = new_prompt + new_completion
        # If the provider reported a higher total than what we derived
        # from prompt+completion, preserve the larger value.
        new_total = max(computed_total, last_total, chunk_total)
        return TokenUsage(
            prompt_tokens=new_prompt,
            completion_tokens=new_completion,
            total_tokens=new_total,
            cache_read_tokens=last_cache_read + cache_read,
            cache_creation_tokens=last_cache_creation + cache_creation,
        )

    def _finalize_stream_usage(
        self,
        last_usage: TokenUsage | None,
        estimated_usage: TokenUsage | None,
        estimated_prompt_tokens: int,
    ) -> tuple[TokenUsage | None, bool]:
        """Return (finalized_usage, should_emit_update).

        Falls back to the local estimate when the provider omitted usage entirely,
        or patches in prompt_tokens when the provider only emitted completion tokens.

        All numeric fields are coerced via ``coerce_token_count`` so a nullable
        SDK payload can never raise ``TypeError`` here.
        """
        if last_usage is None:
            return estimated_usage, False
        last_prompt = coerce_token_count(last_usage.prompt_tokens)
        last_completion = coerce_token_count(last_usage.completion_tokens)
        last_total = coerce_token_count(last_usage.total_tokens)
        last_cache_read = coerce_token_count(last_usage.cache_read_tokens)
        last_cache_creation = coerce_token_count(last_usage.cache_creation_tokens)
        est_prompt = coerce_token_count(estimated_prompt_tokens)

        if est_prompt > 0 and last_prompt <= 0:
            # Re-derive the total from the new prompt + completion. If the
            # provider reported a higher total, preserve it.
            derived = est_prompt + last_completion
            merged_total = max(last_total, derived) if last_total > 0 else derived
            patched = TokenUsage(
                prompt_tokens=est_prompt,
                completion_tokens=last_completion,
                total_tokens=merged_total,
                cache_read_tokens=last_cache_read,
                cache_creation_tokens=last_cache_creation,
            )
            return patched, True
        return last_usage, False

    def _stream_llm_turn_inner(
        self,
        system_prompt: str,
        tools_schema: list | None,
        *,
        request_context: LLMRequestContext | None = None,
    ) -> Generator[TurnEvent, None, TurnOutcome]:
        """Stream one LLM call, yielding events (no retry logic).

        Returns a :class:`TurnOutcome` with classified disposition.

        *request_context* is forwarded to :meth:`provider.chat_stream` so
        the provider can adapt the payload (e.g. GLM one-shot recovery with
        disabled thinking and capped max_tokens).
        """
        assistant_text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        current_tool_arg_parts: dict[str, list[str]] = {}
        current_tool_names: dict[str, str] = {}
        # Ordered tool-call states by **start order** (the order
        # ``is_tool_call_start`` chunks arrived).  Dicts are insertion-
        # ordered in CPython 3.7+, but we use an explicit list to make
        # the ordering guarantee self-documenting and to allow indexing
        # the contiguous safe prefix at finalisation time.
        tool_states: list[_StreamToolState] = []
        # Map tool_call_id -> index into ``tool_states`` for O(1) lookup.
        tool_state_index: dict[str, int] = {}
        last_usage: TokenUsage | None = None
        raw_parts: Any = None
        # Guard against duplicate tool-call completions.  Some
        # OpenAI-compatible proxies re-emit the same final
        # tool-call-end chunk more than once.  Without this guard
        # the same ``ToolCall`` would be appended to ``tool_calls``
        # twice — the assistant ``Message`` would then be persisted
        # with duplicate ``ToolCall`` ids, and the next
        # ``_format_messages`` call would fail with OpenAI's
        # ``invalid params, duplicate tool_call id`` 400 error.
        #
        # The check is a one-liner today, but extracting it as a
        # static helper makes the production guard a callable,
        # public enough to test in isolation without re-implementing
        # the dedup logic inside the test (the previous
        # ``TestAgentLoopDuplicateToolCallIdGuard`` re-implemented
        # the dedup branch in the test body and could silently
        # diverge from production).
        completed_tool_call_ids: set[str] = set()
        reasoning_parts: list[str] = []

        # Determine if the GLM reasoning guard should be active for this turn.
        # The guard fires only when:
        #   1. The provider is GLM (via extra["dialect"] == "glm" or provider.name == "glm")
        #   2. Tools are exposed (guarding reasoning that degenerates before tool calls)
        #   3. The guard is enabled in parsed GLM config
        guard: GLMReasoningGuard | None = self._maybe_create_guard(tools_schema)

        # Determine whether this provider uses GLM strict partial-tool-call
        # semantics.  GLM providers reject malformed/incomplete tool args
        # instead of falling back to ``{}``; non-GLM providers keep the
        # existing ``{}`` + warning fallback unchanged.
        is_glm = _is_glm_provider(self.config, self.provider)

        provider_messages, estimated_prompt_tokens, estimated_usage = self._prepare_provider_messages(system_prompt)
        # Do not emit a pre-stream estimate — it causes the display to jump
        # to an estimated value only to be overwritten by real data moments later.

        stream = self.provider.chat_stream(
            messages=provider_messages,
            tools=tools_schema if tools_schema else None,
            temperature=self.config.provider.temperature,
            max_tokens=self.config.provider.max_tokens,
            system=system_prompt,
            cancel_event=self._cancelled,
            request_context=request_context,
        )

        chunk_count = 0
        # Last finish_reason seen from the provider (e.g. "stop", "length",
        # "content_filter").  ``None`` means the stream ended without one.
        # Truncation reasons ("length", "content_filter") are surfaced to the
        # user after the stream so they know the response is incomplete —
        # without this the chat appears to end normally mid-sentence.
        finish_reason: str | None = None
        # ``stream_broke`` is set when the provider stream raised mid-flight.
        # We then keep whatever partial text/tool_calls were collected and
        # surface a warning instead of discarding them — otherwise the user
        # sees streamed text vanish and the session gains a silent gap (the
        # "chat bị ngắt đột ngột" symptom).
        stream_broke: bool = False
        guard_triggered: bool = False
        # Track whether the provider actually emitted usage chunks
        # (vs estimated_usage from pre-stream estimation). This is needed
        # to set the correct provenance on AttemptUsage.
        provider_emitted_usage: bool = False
        try:
            for chunk in stream:
                self._check_cancelled()
                chunk_count += 1

                if chunk.text:
                    assistant_text_parts.append(chunk.text)
                    yield TurnEvent.text_delta(chunk.text)
                    if guard is not None:
                        guard.on_visible_delta(chunk.text)

                if chunk.reasoning_delta:
                    reasoning_parts.append(chunk.reasoning_delta)
                    yield TurnEvent.reasoning_event(chunk.reasoning_delta)
                    if guard is not None:
                        guard.on_reasoning_delta(chunk.reasoning_delta)

                if chunk.is_tool_call_start and chunk.tool_call_id:
                    current_tool_arg_parts[chunk.tool_call_id] = []
                    current_tool_names[chunk.tool_call_id] = chunk.tool_name or ""
                    # Track in ordered list for contiguous-safe-prefix logic.
                    state = _StreamToolState(
                        id=chunk.tool_call_id,
                        name=chunk.tool_name or "",
                        started=True,
                    )
                    tool_states.append(state)
                    tool_state_index[chunk.tool_call_id] = len(tool_states) - 1
                    yield TurnEvent.tool_call_start(chunk.tool_call_id, chunk.tool_name or "")
                    if guard is not None:
                        guard.on_tool_call_start()

                if chunk.tool_args_delta and chunk.tool_call_id:
                    if not chunk.is_tool_call_end:
                        current_tool_arg_parts.setdefault(chunk.tool_call_id, []).append(chunk.tool_args_delta)
                        yield TurnEvent.tool_call_args_delta(chunk.tool_call_id, chunk.tool_args_delta)

                if chunk.is_tool_call_end and chunk.tool_call_id:
                    tc_id = chunk.tool_call_id
                    if AgentLoop._is_duplicate_tool_call_end(tc_id, completed_tool_call_ids):
                        log_debug(f"AgentLoop: ignoring duplicate tool_call_end for {tc_id!r}")
                        continue
                    completed_tool_call_ids.add(tc_id)
                    tc_name = current_tool_names.get(tc_id, chunk.tool_name or "")
                    raw_args = "".join(current_tool_arg_parts.get(tc_id, []))
                    try:
                        args = json.loads(raw_args) if raw_args else {}
                    except json.JSONDecodeError as je:
                        if is_glm:
                            # GLM strict mode: do NOT fall back to {}.
                            # Record the parse error and skip appending
                            # to tool_calls.  The contiguous-safe-prefix
                            # logic at finalisation will discard this
                            # state and all later states.
                            log_error(
                                f"GLM malformed tool arguments for {tc_name} "
                                f"(id={tc_id}): {je}. Raw: {raw_args[:200]}. "
                                "Call will be discarded."
                            )
                            idx = tool_state_index.get(tc_id)
                            if idx is not None:
                                st = tool_states[idx]
                                st.ended = True
                                st.parse_error = str(je)
                            # Do NOT append to tool_calls.
                            # Do NOT emit tool_call_done -- the call is
                            # discarded at finalisation with TOOL_CALL_DISCARDED.
                            continue
                        # Non-GLM: preserve the existing {} fallback + warning.
                        log_error(f"Malformed tool arguments for {tc_name} (id={tc_id}): {je}. Raw: {raw_args[:200]}")
                        args = {}
                        yield TurnEvent.error_event(
                            f"Warning: malformed arguments for tool '{tc_name}'. "
                            "The tool call will proceed with empty arguments."
                        )
                    # Record successful parse on the state.
                    idx = tool_state_index.get(tc_id)
                    if idx is not None:
                        st = tool_states[idx]
                        st.ended = True
                        st.parsed_arguments = args
                        st.raw_args = list(current_tool_arg_parts.get(tc_id, []))
                    tool_calls.append(ToolCall(id=tc_id, name=tc_name, arguments=args))
                    yield TurnEvent.tool_call_done(tc_id, tc_name, raw_args)

                if chunk.usage:
                    last_usage = self._accumulate_chunk_usage(last_usage, chunk.usage)
                    provider_emitted_usage = True
                    self._context_manager.update_usage(last_usage)
                    # Do not yield per-chunk updates — emit one final update after the stream

                if chunk.raw_parts is not None:
                    raw_parts = chunk.raw_parts

                # Capture the last non-empty finish_reason.  Providers may emit
                # it on the final SSE chunk (OpenAI) or in a message_delta event
                # (Anthropic stop_reason).  We only act on it once, after the
                # stream is fully consumed.
                if chunk.finish_reason:
                    finish_reason = chunk.finish_reason

                # Check guard after processing each chunk. When the guard
                # triggers, close the stream generator immediately — we stop
                # consuming provider output and finalize with DEGENERATED.
                if guard is not None and guard.should_abort():
                    guard_triggered = True
                    stream.close()
                    break
        except CancellationError:
            # Cancellation must propagate unchanged so the outer try/except
            # in run() converts it into a CANCELLED event.
            raise
        except (RateLimitError, ProviderError) as e:
            # If the stream broke AFTER the user already saw partial output,
            # do not retry the whole request (that would duplicate the
            # streamed text in the UI and waste tokens).  Instead keep the
            # partial result and warn.  If nothing was streamed yet, re-raise
            # so _stream_llm_turn's retry layer can handle it as before.
            #
            # Exception: a transport-layer chunked-read failure
            # (``peer closed connection`` / ``incomplete chunked read``)
            # is always treated as a retryable, transient network
            # error. The chunks that did arrive are typically the
            # model's prefill noise, not user-facing content, so we
            # re-raise unconditionally and let the retry layer restart
            # the request from scratch.
            if _is_chunked_read_error(e):
                log_error(
                    f"Provider stream broke (transport chunked-read error) "
                    f"after {chunk_count} chunks: {e}. Re-raising for retry."
                )
                raise
            has_partial = bool(assistant_text_parts) or bool(tool_calls)
            if not has_partial:
                raise
            stream_broke = True
            log_error(
                f"Provider stream broke after {chunk_count} chunks with partial output: {e}. "
                "Keeping partial response and warning the user."
            )
            # Surface as an error event. The chat view renders it
            # distinctly so the user knows to retry if they need the
            # rest of the answer.
            yield TurnEvent.error_event(
                f"{self._format_provider_error_for_user(e)} "
                "The response above is incomplete — it was cut off mid-stream."
            )
        last_usage, need_usage_update = self._finalize_stream_usage(
            last_usage, estimated_usage, estimated_prompt_tokens
        )
        if last_usage is not None:
            if need_usage_update:
                self._context_manager.update_usage(last_usage)
            yield TurnEvent.usage_update(last_usage)

        # Surface truncation reasons to the user.  A normal "stop" / "tool_calls"
        # means the model finished deliberately; "length" means max_tokens cut
        # the output mid-generation, and "content_filter" means content was
        # suppressed.  Both leave the user looking at an incomplete response
        # with no explanation — exactly the "chat bị ngắt đột ngột" symptom.
        # Skip this when the stream already broke with a mid-stream warning
        # above — finish_reason is almost certainly None in that case anyway.
        if not stream_broke:
            warning = self._finish_reason_warning(finish_reason)
            if warning is not None:
                yield TurnEvent.error_event(warning)

        assistant_text = "".join(assistant_text_parts)
        reasoning_content = "".join(reasoning_parts)

        # ------------------------------------------------------------------
        # Contiguous safe-prefix finalisation for GLM strict mode.
        #
        # Walk ``tool_states`` in **start order**.  The first state that
        # is not fully ended-and-parsed (either never got ``is_tool_call_end``,
        # or got it but JSON parse failed) is the "gap".  Every state at
        # or after the gap is discarded; only the contiguous prefix
        # before the gap survives.  Discarded states get a
        # ``TOOL_CALL_DISCARDED`` event so the UI can render a closed
        # lifecycle, and the corresponding ``ToolCall`` (if it was
        # appended for non-GLM fallback) is removed from ``tool_calls``.
        #
        # For non-GLM providers this logic is a no-op: malformed JSON
        # already fell back to ``{}`` and the call was appended with
        # ``parsed_arguments`` set, so no state has ``parse_error``.
        # ------------------------------------------------------------------
        if is_glm and tool_states:
            safe_prefix_len = len(tool_states)
            for i, st in enumerate(tool_states):
                is_complete = st.ended and st.parse_error == "" and st.parsed_arguments is not None
                if not is_complete:
                    safe_prefix_len = i
                    break
            # Emit TOOL_CALL_DISCARDED for states at/after the gap and
            # remove their ToolCalls from the tool_calls list.
            if safe_prefix_len < len(tool_states):
                discarded_ids: set[str] = set()
                for st in tool_states[safe_prefix_len:]:
                    discarded_ids.add(st.id)
                    reason = st.parse_error or "incomplete (truncated)"
                    yield TurnEvent.tool_call_discarded(st.id, st.name, reason)
                # Filter tool_calls to keep only the safe prefix.
                tool_calls = [tc for tc in tool_calls if tc.id not in discarded_ids]
                log_debug(
                    f"GLM strict: safe prefix={safe_prefix_len}/{len(tool_states)} "
                    f"tool states, discarded={sorted(discarded_ids)}"
                )

        broke_tag = " (stream broke — partial)" if stream_broke else ""
        guard_tag = " (guard triggered)" if guard_triggered else ""
        log_debug(
            f"Stream done: {chunk_count} chunks, {len(assistant_text)} chars, "
            f"{len(tool_calls)} tool calls{broke_tag}{guard_tag}"
        )

        # Build the guard trigger info if applicable.
        guard_trigger_str = ""
        guard_snapshot: GLMGuardSnapshot | None = None
        if guard is not None:
            guard_snapshot = guard.snapshot()
            if guard_triggered:
                guard_trigger_str = guard_snapshot.trigger

        # Classify the disposition of this turn using deterministic precedence.
        # ``has_open_tool_calls`` covers two cases:
        #   1. A tool call started but never received ``is_tool_call_end``
        #      (truncated mid-args).
        #   2. A GLM tool call received ``is_tool_call_end`` but JSON parse
        #      failed — the call is in ``completed_tool_call_ids`` but was
        #      discarded and removed from ``tool_calls``.
        # Both signal that tool calls were attempted but incomplete.
        has_open = bool(current_tool_arg_parts) and not all(
            tc_id in completed_tool_call_ids and not any(st.id == tc_id and st.parse_error for st in tool_states)
            for tc_id in current_tool_arg_parts
        )
        # Also: if any tool states were started at all (even if all discarded),
        # and the turn is truncated, it should be TRUNCATED_PARTIAL_TOOL_USE.
        had_any_tool_starts = bool(tool_states)
        disposition = self._classify_turn_outcome(
            guard_triggered=guard_triggered,
            filtered=False,
            has_open_tool_calls=has_open or had_any_tool_starts,
            truncated=finish_reason in ("length", "max_tokens", "content_filter"),
            stream_broke=stream_broke,
            tool_calls=tool_calls,
            visible_text=assistant_text,
        )

        # Build AttemptUsage with provenance (spec section 9.3).
        #
        # If the provider emitted usage before the guard fired (or the
        # stream completed normally), preserve it as authoritative.
        # Otherwise estimate completion_tokens with the same
        # ceil((reasoning_utf8_bytes + visible_utf8_bytes +
        #       tool_argument_utf8_bytes) / 3) rule, set prompt_tokens to
        # the latest known prompt usage, and mark estimated.
        attempt_usage: AttemptUsage | None = None
        if provider_emitted_usage and last_usage is not None:
            provenance = "authoritative"
            attempt_usage = AttemptUsage(usage=last_usage, provenance=provenance)
        else:
            # Estimate completion from the content that was generated.
            reasoning_bytes = len(reasoning_content.encode("utf-8"))
            visible_bytes = len(assistant_text.encode("utf-8"))
            tool_arg_bytes = sum(len("".join(parts).encode("utf-8")) for parts in current_tool_arg_parts.values())
            est_completion = math.ceil((reasoning_bytes + visible_bytes + tool_arg_bytes) / 3)

            # Prompt tokens: use the pre-stream estimate if available,
            # otherwise fall back to zero.
            est_prompt = coerce_token_count(estimated_usage.prompt_tokens) if estimated_usage else 0
            est_total = est_prompt + est_completion
            est_usage = TokenUsage(
                prompt_tokens=est_prompt,
                completion_tokens=est_completion,
                total_tokens=est_total,
            )
            attempt_usage = AttemptUsage(usage=est_usage, provenance="estimated")

        # Repetition ratio millis from guard snapshot.
        rep_ratio_millis = guard_snapshot.repetition_ratio_millis if guard_snapshot else 0

        return TurnOutcome(
            visible_text=assistant_text,
            reasoning_content=reasoning_content,
            tool_calls=tool_calls,
            usage=last_usage,
            raw_parts=raw_parts,
            finish_reason=finish_reason,
            disposition=disposition,
            attempt_usage=attempt_usage,
            guard_trigger=guard_trigger_str,
            repetition_ratio_millis=rep_ratio_millis,
        )

    @staticmethod
    def _classify_turn_outcome(
        *,
        guard_triggered: bool,
        filtered: bool,
        has_open_tool_calls: bool,
        truncated: bool,
        stream_broke: bool,
        tool_calls: list[ToolCall],
        visible_text: str,
    ) -> TurnDisposition:
        """Classify the disposition of a completed stream.

        Precedence (highest first):
        - DEGENERATED: GLM reasoning guard fired
        - FILTERED: content filter suppressed output
        - TRUNCATED_PARTIAL_TOOL_USE: truncated AND tool calls present
        - TRUNCATED_TEXT: truncated with text but no tool calls
        - STREAM_BROKEN: stream broke mid-generation
        - TOOL_USE: has tool calls (normal turn handoff)
        - COMPLETED: normal text-only completion
        - FAILED: no usable partial data at all
        """
        if guard_triggered:
            return TurnDisposition.DEGENERATED
        if filtered:
            return TurnDisposition.FILTERED
        if truncated and (tool_calls or has_open_tool_calls):
            return TurnDisposition.TRUNCATED_PARTIAL_TOOL_USE
        if truncated:
            return TurnDisposition.TRUNCATED_TEXT
        if stream_broke:
            return TurnDisposition.STREAM_BROKEN
        if tool_calls:
            return TurnDisposition.TOOL_USE
        if visible_text:
            return TurnDisposition.COMPLETED
        return TurnDisposition.FAILED

    def _maybe_create_guard(self, tools_schema: list | None) -> GLMReasoningGuard | None:
        """Create a GLM reasoning guard if this is a GLM+tools request.

        Returns ``None`` when the guard should not be active:
        - Provider is not GLM
        - No tools are exposed
        - Guard is disabled in config
        """
        is_glm = self.config.provider.extra.get("dialect") == "glm" or self.provider.name == "glm"
        if not is_glm:
            return None
        if not tools_schema:
            return None

        # Read the already-validated GLMConfig from the provider instance
        # when available (the canonical GLMProvider path). Fall back to
        # a fresh parse for non-GLM providers that nevertheless report
        # ``dialect == "glm"`` (custom providers, test doubles) so the
        # guard still activates.
        parsed_glm_config = getattr(self.provider, "glm_config", None)
        if parsed_glm_config is None:
            try:
                from ..core.glm_config import parse_glm_extra
            except ImportError:
                log_debug("GLM guard: glm_config module unavailable, skipping guard")
                return None
            parsed_glm_config = parse_glm_extra(self.config.provider.extra, self.config.provider.model)

        if not parsed_glm_config.guard.enabled:
            return None

        # Extract exposed tool names from the schema for meta-intent detection.
        exposed_tool_names: list[str] = []
        for entry in tools_schema:
            if isinstance(entry, dict):
                func = entry.get("function", {})
                name = func.get("name", "")
                if name:
                    exposed_tool_names.append(name)

        return GLMReasoningGuard(
            exposed_tool_names=exposed_tool_names,
            ceiling_tokens=parsed_glm_config.guard.reasoning_token_ceiling,
        )

    @staticmethod
    def _finish_reason_warning(finish_reason: str | None) -> str | None:
        """Return a user-facing warning when the stream ended prematurely.

        Returns ``None`` for deliberate stop reasons ("stop", "tool_calls",
        end_turn) so normal turns produce no warning.  "length" and
        "content_filter" (and their provider-specific spellings) return a
        short explanation the UI renders as an error message.
        """
        if not finish_reason:
            return None
        reason = finish_reason.lower().strip()
        # Anthropic uses stop_reason values like "end_turn", "max_tokens",
        # "stop_sequence", "tool_use"; OpenAI uses finish_reason "stop",
        # "length", "tool_calls", "content_filter".  "tool_use" (Anthropic)
        # is a deliberate, complete turn — the model is handing off to a tool,
        # not truncated — so it belongs in the no-warning set alongside
        # OpenAI's "tool_calls".
        if reason in (
            "stop",
            "tool_calls",
            "end_turn",
            "stop_sequence",
            "tool_use",
        ):
            return None
        if reason in ("length", "max_tokens"):
            return (
                "⚠️ The response was cut off because it reached the max output "
                "token limit (finish_reason=length). Increase max_tokens in "
                "Settings or continue the conversation to get the rest."
            )
        if reason in ("content_filter",):
            return (
                "⚠️ The response was suppressed by the provider's content "
                "filter (finish_reason=content_filter). Try rephrasing the request."
            )
        # Unknown reason — surface it rather than swallow it silently.
        return f"⚠️ The response ended unexpectedly (finish_reason={finish_reason})."

    @staticmethod
    def _is_duplicate_tool_call_end(tc_id: str, completed_ids: set[str]) -> bool:
        """Return True if ``tc_id`` has already been recorded as
        completed.  Production agent loop calls this guard on every
        ``is_tool_call_end`` chunk to keep duplicate-end emissions
        from re-appending the same ``ToolCall`` to the assistant
        message.  See ``_stream_llm_turn_inner``.

        The helper is intentionally a no-side-effect check: the
        caller is responsible for ``completed_ids.add(tc_id)`` when
        the chunk is *not* a duplicate, so a duplicate chunk never
        mutates the set.  The test
        ``TestAgentLoopDuplicateToolCallIdGuard`` exercises the
        helper directly so the production guard is covered without
        re-implementing the dedup branch in the test body.
        """
        return tc_id in completed_ids

    @staticmethod
    def _estimate_prompt_tokens(provider_messages: list[Message], system_prompt: str) -> int:
        """Estimate prompt token usage from message content lengths.

        Uses a lightweight character sum instead of JSON serialization.
        """
        char_count = len(system_prompt)
        for m in provider_messages:
            char_count += len(m.content) if m.content else 0
            if m.tool_calls:
                for tc in m.tool_calls:
                    char_count += len(str(tc.arguments)) if tc.arguments else 0
        return ContextWindowManager.estimate_tokens_from_chars(char_count)

    @staticmethod
    def _describe_tool_call(name: str, args: dict[str, Any]) -> str:
        """Generate a brief human-readable description of what a tool will do."""
        if name == constants.EXECUTE_PYTHON_TOOL_NAME:
            # The unified ExecutePythonWidget renders its own code block,
            # so a description here would duplicate the first line. Return
            # empty.
            return ""
        if name == "delegate_external_task":
            # Approval prompt must show exactly what will run externally:
            # the target agent name and the full task text.
            agent = str(args.get("agent", "?"))
            task = str(args.get("task", ""))
            return f"Delegate task to external agent '{agent}': {task}"
        if name in ("rename_function",):
            return f"Rename function {args.get('old_name', '?')} → {args.get('new_name', '?')}"
        if name in ("rename_variable",):
            return (
                f"Rename variable {args.get('old_name', args.get('variable_name', '?'))} → {args.get('new_name', '?')}"
            )
        if name in ("set_comment", "set_function_comment"):
            return f"Set comment at {args.get('address', '?')}"
        if name in ("set_type", "set_function_prototype"):
            return f"Set type at {args.get('address', '?')}"
        if name in ("nop_microcode",):
            return f"NOP instructions at {args.get('address', args.get('func_address', '?'))}"
        if name in ("create_struct", "create_enum"):
            return f"Create {name.split('_')[1]} '{args.get('name', '?')}'"
        if name in ("decompile_function", "read_disassembly"):
            return f"Decompile/disassemble {args.get('address', args.get('name', '?'))}"
        # Generic
        summary_parts = []
        for k in ("name", "address", "ea", "target", "query"):
            if k in args:
                summary_parts.append(f"{k}={args[k]}")
                break
        return f"Call {name}({', '.join(summary_parts)})" if summary_parts else f"Call {name}"

    def _wait_for_approval(
        self,
        tc: ToolCall,
    ) -> Generator[TurnEvent, None, bool]:
        """Yield an approval request and wait for the user decision.

        Returns True if approved, False if denied.
        Handles 'allow_all' to skip future approval prompts for this session.
        """
        # Skip prompt if user previously chose "Always Allow"
        if self._always_allow_scripts:
            return True

        args_str = json.dumps(tc.arguments, indent=2)
        description = self._describe_tool_call(tc.name, tc.arguments)
        yield TurnEvent.tool_approval_request(tc.id, tc.name, args_str, description)

        decision = self._wait_for_queue(self._tool_approval_queue).lower()
        if decision == "allow_all":
            self._always_allow_scripts = True
            return True
        return decision == "allow"

    # ------------------------------------------------------------------
    # IDAPython docs-review gate (post-error variant)
    # ------------------------------------------------------------------

    #: Max number of IDA modules whose docs we auto-inject after a failed
    #: script. Anything more bloats the tool result without proportional
    #: diagnostic value — most failures trace to 1-2 API calls.
    _DOCS_REFERENCE_MAX_MODULES: int = 3

    #: Per-module character cap for the auto-injected docs block. Matches
    #: ``MAX_CHARS_PER_MODULE`` used by the reviewer's tool calls.
    _DOCS_REFERENCE_MAX_CHARS_PER_MODULE: int = 4000

    def _build_reference_injection(self, modules: tuple[str, ...]) -> str:
        """Pull offline docs cho mỗi module liên quan, ghép thành 1 block.

        Gọi ``lookup_idapython_doc`` core function trực tiếp (pure Python,
        không qua tool dispatch, không tốn LLM round-trip). Giới hạn
        ``_DOCS_REFERENCE_MAX_MODULES`` để tránh phình token.
        """
        from ..tools.idapython_docs import lookup_idapython_doc

        parts: list[str] = []
        # Bounded slice — fail-safe if a future caller ignores the cap.
        for module in modules[: self._DOCS_REFERENCE_MAX_MODULES]:
            try:
                # @tool decorator dùng functools.wraps → __wrapped__ trỏ về func gốc.
                # Gọi core function trực tiếp, bypass tool dispatch + logging.
                core_fn = getattr(lookup_idapython_doc, "__wrapped__", lookup_idapython_doc)
                doc_text = core_fn(module=module, limit=self._DOCS_REFERENCE_MAX_CHARS_PER_MODULE)
                parts.append(f"### {module}\n{doc_text}")
            except Exception as e:
                log_debug(f"reference injection skipped for {module}: {e}")
        return "\n\n".join(parts)

    def _review_failed_script(
        self,
        tc: ToolCall,
        traceback_text: str,
        code: str,
        classification: TracebackClassification,
    ) -> Generator[TurnEvent, None, str]:
        """Spawn docs-reviewer cho script đã fail runtime.

        Reviewer chẩn đoán dựa trên traceback + exception type, trả verdict
        + REWRITE_GUIDANCE. Hệ thống auto-inject reference docs của modules
        liên quan vào kết quả. Trả về augmented result string:
        traceback + reviewer verdict + reference docs.

        Set ``_docs_reviewer_invoked = True`` — chỉ 1 reviewer call per task
        (reset mỗi user message trong ``run()``).
        """
        from .agents.ida_docs_reviewer import (
            IDA_DOCS_REVIEWER_MAX_TURNS,
            build_ida_docs_reviewer_addendum,
        )

        self._docs_reviewer_invoked = True

        yield TurnEvent.docs_gate_status(
            tc.id,
            state="running",
            reasons=(f"runtime {classification.exception_type}: {classification.exception_message}",),
        )

        goal = self.session.metadata.get(_GOAL_METADATA_KEY, "") or ""

        # Build task payload cho reviewer: script + traceback + goal.
        task_lines: list[str] = []
        if goal:
            task_lines.append(f"# User Goal\n\n{goal}\n")
        task_lines.append(f"# Failed IDAPython Script\n\n```python\n{code}\n```\n")
        task_lines.append(
            f"# Runtime Error\n\n"
            f"Exception type: {classification.exception_type}\n"
            f"Message: {classification.exception_message}\n\n"
            f"```\n{traceback_text}\n```\n"
        )
        task_lines.append(
            "# Your Task\n\n"
            "Diagnose why this script failed. Check every IDA API call against "
            "the `ida-scripting` skill and the bundled offline docs. Return the "
            "structured VERDICT block described in your task instructions.\n"
            "Do NOT call execute_python — you are a reviewer, not an executor."
        )
        task = "\n".join(task_lines)

        runner = SubagentRunner(
            provider=self.provider,
            tool_registry=self.tools,
            config=self.config,
            host_name=self.host_name,
            skill_registry=self.skills,
            parent_loop=self,
            unattended=self._unattended,
        )

        try:
            summary = yield from runner.run_task(
                task,
                max_turns=IDA_DOCS_REVIEWER_MAX_TURNS,
                system_addendum=build_ida_docs_reviewer_addendum(),
                silent=True,
            )
        except CancellationError:
            raise
        except Exception as e:
            log_error(f"docs reviewer failed: {e}")
            yield TurnEvent.docs_gate_status(
                tc.id,
                state="failed",
                summary=f"{type(e).__name__}: {e}",
            )
            # Reviewer crash → trả traceback thẳng (không augment).
            # Đây là infrastructure fault, không phải script fault.
            return f"--- Traceback ---\n{traceback_text}\n--- end ---"

        # Inject reference docs của modules liên quan.
        reference_block = self._build_reference_injection(classification.modules_referenced)

        # Augment result: traceback + verdict + reference.
        parts = [
            f"Script failed with {classification.exception_type}: {classification.exception_message}",
            "",
            "--- Traceback ---",
            traceback_text,
            "--- Docs Reviewer Verdict ---",
            summary or "(no verdict returned)",
        ]
        if reference_block:
            parts.append("--- Module Reference (auto-injected) ---")
            parts.append(reference_block)
        parts.append("--- end ---")

        yield TurnEvent.docs_gate_status(tc.id, state="reviewed")
        return "\n".join(parts)

    def _execute_single_tool(self, tc: ToolCall) -> Generator[TurnEvent, None, ToolResult]:
        """Handle approval gating, mutation tracking, and execution of a real tool."""
        # Config: hide string-list/search tools when analysis strings are
        # disabled. Defense-in-depth: even if the schema filter is bypassed
        # (stale provider schema, third-party schema) the agent cannot call
        # these tools directly. Mirrors the profile.denied_tools guard.
        if self.config.hide_strings and tc.name in {"list_strings", "search_strings"}:
            content = "Error: String analysis is disabled by the active configuration."
            log_debug(f"Blocked hidden string tool: {tc.name} (hide_strings=True)")
            tr = ToolResult(tool_call_id=tc.id, name=tc.name, content=content, is_error=True)
            yield TurnEvent.tool_result_event(tc.id, tc.name, content, True)
            return tr
        # Profile: block denied tools at execution time (defense-in-depth —
        # the schema filter already hides them, but the LLM may still try)
        profile = self.config.get_active_profile()
        if profile.denied_tools and tc.name in profile.denied_tools:
            content = f"Error: Tool '{tc.name}' is denied by the active profile."
            log_debug(f"Blocked denied tool: {tc.name} (profile: {profile.name})")
            tr = ToolResult(tool_call_id=tc.id, name=tc.name, content=content, is_error=True)
            yield TurnEvent.tool_result_event(tc.id, tc.name, content, True)
            return tr

        # execute_python always requires explicit approval, and any tool
        # whose definition sets requires_approval (e.g. install_microcode_optimizer,
        # which exec()s LLM-authored optimizer code) is gated identically.
        defn = self.tools.get(tc.name)
        needs_approval = tc.name == constants.EXECUTE_PYTHON_TOOL_NAME or (defn is not None and defn.requires_approval)
        if needs_approval and self._unattended:
            content = (
                f"Error: Tool '{tc.name}' requires user approval but this is an unattended "
                "subagent (no user is attached to approve it). Continue the task without it."
            )
            log_debug(f"Blocked approval-gated tool in unattended subagent: {tc.name}")
            tr = ToolResult(tool_call_id=tc.id, name=tc.name, content=content, is_error=True)
            yield TurnEvent.tool_result_event(tc.id, tc.name, content, True)
            return tr
        if needs_approval:
            if tc.name == constants.EXECUTE_PYTHON_TOOL_NAME:
                code = tc.arguments.get("code", "") or tc.arguments.get("script", "")
                if isinstance(code, str) and code.strip():
                    try:
                        validation = validate_idapython(code)
                    except Exception as e:  # pragma: no cover — defensive
                        log_error(f"docs-gate validation failed: {e}")
                        validation = None

                    if validation is not None and validation.is_blocked:
                        # Hard block: hallucinated API detected pre-execute.
                        block_msg = (
                            "Script blocked by static validator (hallucinated API detected):\n"
                            f"{validation.format_for_agent()}\n"
                            "Fix the API usage and resubmit."
                        )
                        tr = ToolResult(
                            tool_call_id=tc.id,
                            name=tc.name,
                            content=block_msg,
                            is_error=True,
                        )
                        yield TurnEvent.tool_result_event(tc.id, tc.name, block_msg, True)
                        return tr

            approved = yield from self._wait_for_approval(tc)
            if not approved:
                content = "Tool execution denied by user."
                tr = ToolResult(tool_call_id=tc.id, name=tc.name, content=content, is_error=True)
                yield TurnEvent.tool_result_event(tc.id, tc.name, content, True)
                return tr

        is_mutating = defn is not None and defn.mutating

        if is_mutating and self._unattended and self.config.approve_mutations:
            content = (
                f"Error: Tool '{tc.name}' mutates the database and mutation approval is "
                "enabled, but this is an unattended subagent (no user is attached to "
                "approve it). Continue the task without it."
            )
            log_debug(f"Blocked mutation-approval tool in unattended subagent: {tc.name}")
            tr = ToolResult(tool_call_id=tc.id, name=tc.name, content=content, is_error=True)
            yield TurnEvent.tool_result_event(tc.id, tc.name, content, True)
            return tr

        if is_mutating and self.config.approve_mutations:
            approved = yield from self._wait_for_approval(tc)
            if not approved:
                content = "Mutation denied by user."
                tr = ToolResult(tool_call_id=tc.id, name=tc.name, content=content, is_error=True)
                yield TurnEvent.tool_result_event(tc.id, tc.name, content, True)
                return tr

        pre_state: dict[str, Any] = {}
        exec_args: dict[str, Any] = dict(tc.arguments)
        if is_mutating:
            exec_args = self.tools.coerce_arguments_for(tc.name, tc.arguments)
            pre_state = capture_pre_state(
                tc.name,
                exec_args,
                lambda name, args: self.tools.execute(name, args),
            )

        log_debug(f"Executing tool {tc.name}")
        try:
            result = self.tools.execute_coerced(tc.name, exec_args)
            is_error = False
            # Hysteresis: decrement instead of resetting so a single success
            # after several failures doesn't fully clear the counter.
            self._consecutive_errors = max(0, self._consecutive_errors - 1)
            if is_mutating:
                record = build_reverse_record(tc.name, exec_args, pre_state)
                verification = self._verify_mutation(tc.name, exec_args, result)
                if not verification.ok:
                    # Post-state verification failed — do not append to the
                    # undo stack.  Log the reason so it is still available
                    # for debugging but does not consume /undo slots.
                    log_debug(f"mutation recording skipped for {tc.name}: {verification.reason}")
                elif not record.reversible:
                    # Successful mutation but non-reversible — emit a UI-only
                    # diagnostic event but do NOT append to the undo stack.
                    log_debug(f"mutation not added to undo stack because it is not reversible: {record.description}")
                    yield TurnEvent.mutation_recorded(
                        tool_name=record.tool_name,
                        description=record.description,
                        reversible=record.reversible,
                        reverse_tool=record.reverse_tool,
                        reverse_args=record.reverse_arguments,
                    )
                else:
                    with self._mutation_lock:
                        self._mutation_log.append(record)
                    log_debug(f"Mutation recorded: {record.description}")
                    yield TurnEvent.mutation_recorded(
                        tool_name=record.tool_name,
                        description=record.description,
                        reversible=record.reversible,
                        reverse_tool=record.reverse_tool,
                        reverse_args=record.reverse_arguments,
                    )
        except ToolError as e:
            result = f"Error: {e}"
            is_error = True
            self._consecutive_errors += 1
            log_error(f"Tool {tc.name} error: {e}")
        except Exception as e:
            tb = traceback.format_exc()
            is_error = True
            self._consecutive_errors += 1
            log_error(f"Tool {tc.name} unexpected error: {e}\n{tb}")
            # Only include the full traceback in the tool result for
            # execute_python (the docs-review classifier consumes it). For
            # other tools, keep the one-liner so internal paths/line numbers
            # never leak into the LLM context.
            if tc.name == constants.EXECUTE_PYTHON_TOOL_NAME:
                result = f"Unexpected error: {e}\n{tb}"
            else:
                result = f"Unexpected error: {e}"

            # Post-error docs review for execute_python: spawn reviewer only
            # when the exception is API-shaped (AttributeError, ImportError,
            # NameError) and the reviewer hasn't been invoked for this task yet.
            # Configurable via ``docs_review_mode`` ("on_error" / "off").
            if (
                tc.name == constants.EXECUTE_PYTHON_TOOL_NAME
                and getattr(self.config, "docs_review_mode", "on_error") == "on_error"
                and not self._docs_reviewer_invoked
            ):
                from ..tools.traceback_classifier import classify_traceback

                code = tc.arguments.get("code", "") or tc.arguments.get("script", "") or ""
                classification = classify_traceback(tb, code)
                if classification.is_api_shaped:
                    augmented = yield from self._review_failed_script(tc, tb, code, classification)
                    if augmented:
                        result = augmented

        # Sanitize tool output before it enters the conversation. Errors are
        # wrapped too: tracebacks can carry attacker-controlled content
        # (function names, symbol paths), and DATA_INTEGRITY_SECTION promises
        # delimiter wrapping for ALL untrusted tool output.
        sanitized = sanitize_tool_result(result, tc.name)

        # Profile: strip IOCs from tool results when any IOC filter is enabled
        profile = self.config.get_active_profile()
        if profile.has_any_ioc_filter:
            sanitized = strip_iocs(sanitized, profile.ioc_filters, profile.custom_filter_rules)

        tr = ToolResult(tool_call_id=tc.id, name=tc.name, content=sanitized, is_error=is_error)
        # Use sanitized content for the UI event too — the raw `result`
        # could contain injection strings (e.g. ANTHROPIC_MAGIC_STRING from
        # a malicious binary) that must never reach the display layer.
        yield TurnEvent.tool_result_event(tc.id, tc.name, sanitized, is_error)
        return tr

    def _handle_exploration_report_tool(
        self,
        tc: ToolCall,
        state: ExplorationState,
    ) -> Generator[TurnEvent, None, ToolResult]:
        """Handle the exploration_report pseudo-tool."""
        category = tc.arguments.get("category", "general")
        address_raw = tc.arguments.get("address")
        address = None
        if address_raw is not None:
            try:
                address = int(str(address_raw), 0)
            except (ValueError, TypeError) as e:
                log_debug(f"exploration_report: bad address {address_raw!r}: {e}")
        summary = tc.arguments.get("summary", "")
        evidence = tc.arguments.get("evidence", "")
        relevance = tc.arguments.get("relevance", "medium")

        state.knowledge_base.add_finding(
            Finding(
                category=category,
                address=address,
                summary=summary,
                evidence=evidence,
                relevance=relevance,
            )
        )
        func_name = ""
        if category == "function_purpose" and address is not None:
            func_name = tc.arguments.get("function_name", f"sub_{address:x}")
            state.knowledge_base.add_function(
                FunctionInfo(
                    address=address,
                    name=func_name,
                    summary=summary,
                    relevance=relevance,
                )
            )

        # Defer ingestion until the post-explore verifier confirms the
        # finding. ``/explore`` is explore-only; ingestion for those
        # findings happens in ``_finalize_explore_memory`` after review.
        # ``/modify`` continues to ingest immediately because its
        # planning phase needs the durable record.
        if not getattr(state, "explore_only", False):
            try:
                from ..memory.ingest import ingest_exploration_finding, make_store

                store, paths = make_store(self.session.idb_path)
                if store is not None:
                    ingest_exploration_finding(
                        store,
                        paths,
                        category=category,
                        summary=summary,
                        address=address,
                        relevance=relevance,
                        evidence=evidence,
                        function_name=func_name,
                        memory_service=self.memory_service,
                    )
            except Exception as e:
                log_debug(f"knowledge ingest (exploration_report) failed: {e}")
        if category == "patch_result" and address is not None:
            original_hex = tc.arguments.get("original_hex", "")
            new_hex = tc.arguments.get("new_hex", "")
            try:
                original_bytes = bytes.fromhex(original_hex.replace(" ", "")) if original_hex else b""
            except ValueError:
                original_bytes = b""
            try:
                new_bytes = bytes.fromhex(new_hex.replace(" ", "")) if new_hex else b""
            except ValueError:
                new_bytes = b""
            patch_record = PatchRecord(
                address=address,
                original_bytes=original_bytes,
                new_bytes=new_bytes,
                description=summary,
                verified="verif" in evidence.lower() or "confirm" in evidence.lower(),
                verification_result=evidence,
            )
            state.patches_applied.append(patch_record)
            yield TurnEvent.patch_applied(address, summary, original_hex, new_hex)

        content = f"Finding logged: [{category}] {summary}"
        tr = ToolResult(tool_call_id=tc.id, name=tc.name, content=content, is_error=False)
        yield TurnEvent.tool_result_event(tc.id, tc.name, content, False)
        yield TurnEvent.exploration_finding(category, summary, address, relevance)
        return tr

    def _handle_phase_transition_tool(
        self,
        tc: ToolCall,
        state: ExplorationState,
    ) -> Generator[TurnEvent, None, ToolResult]:
        """Handle the phase_transition pseudo-tool."""
        to_phase_str = tc.arguments.get("to_phase", "")
        reason = tc.arguments.get("reason", "")
        try:
            to_phase = ExplorationPhase(to_phase_str)
        except ValueError:
            content = f"Invalid phase: '{to_phase_str}'. Valid: {[p.value for p in ExplorationPhase]}"
            tr = ToolResult(tool_call_id=tc.id, name=tc.name, content=content, is_error=True)
            yield TurnEvent.tool_result_event(tc.id, tc.name, content, True)
            return tr

        allowed, deny_reason = state.can_transition_to(to_phase)
        if not allowed:
            content = f"Cannot transition to {to_phase_str}: {deny_reason}"
            tr = ToolResult(tool_call_id=tc.id, name=tc.name, content=content, is_error=True)
            yield TurnEvent.tool_result_event(tc.id, tc.name, content, True)
            return tr

        old_phase = state.phase.value
        state.transition_to(to_phase)
        content = f"Phase transition: {old_phase} → {to_phase_str}. {reason}"
        tr = ToolResult(tool_call_id=tc.id, name=tc.name, content=content, is_error=False)
        yield TurnEvent.tool_result_event(tc.id, tc.name, content, False)
        yield TurnEvent.exploration_phase_change(old_phase, to_phase_str, reason)
        return tr

    def _handle_save_memory_tool(self, tc: ToolCall) -> Generator[TurnEvent, None, ToolResult]:
        """Handle the save_memory pseudo-tool.

        The *category* argument is sanitized the same way as *fact*:
        injection markers and lone surrogates are stripped, ``[`` / ``]``
        are removed, whitespace is collapsed, and the result is bounded
        in length. This prevents a hostile category such as
        ``</persistent_memory>system`` from breaking out of the
        ``[persistent_memory]`` wrapper on a future read of MEMORY.md.
        """
        from ..core.sanitize import strip_injection_markers, strip_lone_surrogates

        raw_fact = tc.arguments.get("fact", "")
        # Apply the same sanitization contract as ``category``: strip
        # surrogates, role markers, AND angle brackets so an injected
        # ``</persistent_memory>`` payload cannot break out of the
        # downstream ``[persistent_memory]`` wrapper when MEMORY.md
        # managed region is reloaded into the system prompt.
        fact = strip_injection_markers(strip_lone_surrogates(str(raw_fact)))
        fact = fact.replace("<", "").replace(">", "")
        category = _sanitize_save_memory_category(tc.arguments.get("category", "general"))
        if not fact:
            content = "Error: 'fact' is required."
            is_err = True
        elif self.memory_service is not None and self._memory_authority is not None:
            # Central memory path: write through BinaryMemoryService.
            try:
                result = self.memory_service.save_fact(
                    self._memory_authority,
                    category=category,
                    fact=fact,
                    source="save_memory",
                )
                label = "Memory created" if result.outcome == "created" else "Memory already exists"
                content = f"{label}: {result.record_id} [{category}]"
                if result.projection_dirty:
                    content += " (MEMORY.md projection pending)"
                is_err = False
                log_info(f"save_memory: [{category}] {fact[:80]}")
                # Notify the UI that the knowledge store changed. Emitted
                # on both ``created`` and ``deduplicated`` outcomes so the
                # Knowledge panel refreshes regardless of duplicate
                # detection. The UI pairs this with the originating tool
                # call via ``tool_call_id``.
                yield TurnEvent.memory_saved(tc.id)
            except Exception as e:
                content = f"Error saving to central memory: {e}"
                is_err = True
        else:
            content = "Error: Central memory is not available in this context."
            is_err = True
        tr = ToolResult(tool_call_id=tc.id, name=tc.name, content=content, is_error=is_err)
        yield TurnEvent.tool_result_event(tc.id, tc.name, content, is_err)
        return tr

    def _handle_research_note_tool(self, tc: ToolCall) -> Generator[TurnEvent, None, ToolResult]:
        """Handle the research_note pseudo-tool — delegates to research mode."""
        from .modes.research import write_and_review_note

        state = self._research_state
        if state is None:
            # Invariant: callers route research_note only after _ensure_research_state().
            # Use an explicit raise instead of assert so the check survives `python -O`.
            raise RuntimeError("research_note tool called without an active research state")
        genre = tc.arguments.get("genre", "general")
        title = tc.arguments.get("title", "untitled")
        content = tc.arguments.get("content", "")
        related = tc.arguments.get("related_notes", [])

        if not content:
            err = "Error: 'content' is required."
            tr = ToolResult(tool_call_id=tc.id, name=tc.name, content=err, is_error=True)
            yield TurnEvent.tool_result_event(tc.id, tc.name, err, True)
            return tr

        note = yield from write_and_review_note(
            state=state,
            genre=genre,
            title=title,
            content=content,
            related_notes=related,
            runner_factory=lambda: SubagentRunner(
                provider=self.provider,
                tool_registry=self.tools,
                config=self.config,
                host_name=self.host_name,
                skill_registry=self.skills,
                parent_loop=self,
                unattended=self._unattended,
            ),
            loop=self,
        )

        # Auto-ingest the *final* research note into the raw knowledge
        # store. We only do this after the review pipeline commits,
        # so we don't pollute the store with draft content.
        try:
            from ..memory.ingest import ingest_research_note, make_store

            store, paths = make_store(self.session.idb_path)
            if store is not None:
                ingest_research_note(
                    store,
                    paths,
                    note_path=note.path,
                    genre=note.genre,
                    title=note.title,
                    content=note.content,
                    related=note.related_notes,
                    review_passed=note.review_passed,
                    memory_service=self.memory_service,
                )
        except Exception as e:
            log_debug(f"knowledge ingest (research_note) failed: {e}")

        result_text = f"Note saved: {note.path}"
        tr = ToolResult(tool_call_id=tc.id, name=tc.name, content=result_text, is_error=False)
        yield TurnEvent.tool_result_event(tc.id, tc.name, result_text, False)
        return tr

    def _handle_spawn_subagent_tool(self, tc: ToolCall) -> Generator[TurnEvent, None, ToolResult]:
        """Handle the spawn_subagent pseudo-tool."""
        task = tc.arguments.get("task", "")
        max_turns = tc.arguments.get("max_turns", 20)
        if not task:
            content = "Error: 'task' is required."
            is_err = True
        else:
            try:
                runner = SubagentRunner(
                    provider=self.provider,
                    tool_registry=self.tools,
                    config=self.config,
                    host_name=self.host_name,
                    skill_registry=self.skills,
                    parent_loop=self,
                    unattended=self._unattended,
                )
                raw = yield from runner.run_task(task, max_turns=max_turns)
                content = sanitize_tool_result(raw or "(Subagent produced no output)", "spawn_subagent")
                is_err = False
                # Store subagent messages separately for export
                if runner.last_session and runner.last_session.messages:
                    self.session.subagent_logs[tc.id] = list(runner.last_session.messages)
            except Exception as e:
                content = f"Subagent error: {e}"
                is_err = True
                log_error(f"spawn_subagent failed: {e}")
        tr = ToolResult(tool_call_id=tc.id, name=tc.name, content=content, is_error=is_err)
        yield TurnEvent.tool_result_event(tc.id, tc.name, content, is_err)
        return tr

    def _handle_activate_skill_tool(self, tc: ToolCall) -> Generator[TurnEvent, None, ToolResult]:
        """Handle the activate_skill pseudo-tool."""
        slug = tc.arguments.get("slug", "")
        skill = self.skills.get(slug) if self.skills else None
        if skill is None:
            content = f"Skill '{slug}' not found."
            is_err = True
        else:
            content = f"[Skill: {skill.name}]\n\n{sanitize_skill_body(skill.body, skill.name)}"
            is_err = False
            log_debug(f"Agent activated skill: /{slug}")
        tr = ToolResult(tool_call_id=tc.id, name=tc.name, content=content, is_error=is_err)
        yield TurnEvent.tool_result_event(tc.id, tc.name, content, is_err)
        return tr

    def _handle_ask_user_tool(self, tc: ToolCall) -> Generator[TurnEvent, None, ToolResult]:
        """Handle the ask_user pseudo-tool."""
        if self._unattended:
            content = (
                "Error: ask_user is unavailable in an unattended subagent (no user is "
                "attached to answer). Decide yourself using the available context and continue."
            )
            log_debug("Blocked ask_user in unattended subagent")
            tr = ToolResult(tool_call_id=tc.id, name=tc.name, content=content, is_error=True)
            yield TurnEvent.tool_result_event(tc.id, tc.name, content, True)
            return tr
        question = tc.arguments.get("question", "")
        raw_options = tc.arguments.get("options", [])
        # Filter out empty/whitespace-only options. Some LLMs send
        # ``options: [""]`` for open-ended questions; without filtering, the
        # panel treats ``bool([""])`` as truthy, locks the text input, and
        # renders a single empty button the user cannot act on.
        options = [o for o in raw_options if isinstance(o, str) and o.strip()]
        yield TurnEvent.user_question(question, options, tc.id)
        answer = self._wait_for_queue(self._user_answer_queue)
        content = f"User answered: {answer}"
        tr = ToolResult(tool_call_id=tc.id, name=tc.name, content=content, is_error=False)
        yield TurnEvent.tool_result_event(tc.id, tc.name, content, False)
        return tr

    def _handle_delegate_external_task_tool(self, tc: ToolCall) -> Generator[TurnEvent, None, ToolResult]:
        """Handle the delegate_external_task pseudo-tool.

        Streams ``A2ADispatcher`` events through the same TurnEvent
        stream that regular tool calls use, so the UI's existing
        text/message rendering applies. The aggregated result is
        returned as the tool result for the LLM to consume on its
        next turn.
        """
        from ..core.sanitize import sanitize_tool_result
        from .a2a import A2ADispatcher

        agent_name = tc.arguments.get("agent", "")
        task = tc.arguments.get("task", "")
        include_context = bool(tc.arguments.get("include_context", False))

        if not agent_name or not task:
            content = "Error: both 'agent' and 'task' are required."
            yield TurnEvent.tool_result_event(tc.id, tc.name, content, True)
            return ToolResult(tool_call_id=tc.id, name=tc.name, content=content, is_error=True)

        if self._unattended:
            content = (
                "Error: delegate_external_task requires user approval but this is an "
                "unattended subagent (no user is attached to approve it). "
                "Continue the task without it."
            )
            log_debug("Blocked delegate_external_task in unattended subagent")
            yield TurnEvent.tool_result_event(tc.id, tc.name, content, True)
            return ToolResult(tool_call_id=tc.id, name=tc.name, content=content, is_error=True)

        # Approval gate: delegation spawns an external CLI agent (or HTTP
        # endpoint) driven by LLM-authored task text — the same risk class
        # as execute_python. Route through the shared approval queue so the
        # UI prompt and the headless serve-mode HTTP /tool-approval path
        # both apply. The explicit /a2a slash command bypasses this
        # handler entirely (run_a2a_mode) and stays ungated.
        approved = yield from self._wait_for_approval(tc)
        if not approved:
            content = "External task delegation denied by user."
            yield TurnEvent.tool_result_event(tc.id, tc.name, content, True)
            return ToolResult(tool_call_id=tc.id, name=tc.name, content=content, is_error=True)

        # Lazily build a context prefix if requested. We read the
        # binary info + cursor position via the existing tool
        # surface; the dispatcher doesn't know about tool_registry.
        context_prefix = ""
        if include_context:
            try:
                from ..core.host import is_ida

                if is_ida():
                    bin_info = self.tools.execute("get_binary_info", {})
                    ctx_lines = [
                        "# Binary context",
                        f"\n{bin_info}",
                    ]
                    try:
                        cur = self.tools.execute("get_current_function", {})
                        if cur:
                            ctx_lines.append(f"\n# Current function\n\n{cur}")
                    except Exception as exc:
                        log_debug(f"research_note current_function context failed: {exc}")
                    context_prefix = "\n\n".join(ctx_lines)
            except Exception as exc:
                # Context lookup is best-effort — fall back to bare task.
                log_debug(f"research_note context enrichment failed: {exc}")

        # Use the agent loop's existing cancel event so a user cancel
        # propagates cleanly into subprocesses and HTTP retry loops.
        dispatcher = A2ADispatcher(
            auto_discover=getattr(self.config, "a2a_auto_discover", True),
            a2a_agents=getattr(self.config, "a2a_agents", None),
        )

        collected_text = ""
        is_error = False
        try:
            for event in dispatcher.run_task(
                agent_name,
                task,
                cancel_event=self._cancelled,
                include_context=context_prefix,
            ):
                # Forward the dispatcher's TEXT_DELTA/error events to
                # the UI but don't double-emit tool_result_event.
                if event.type == TurnEventType.TEXT_DELTA:
                    collected_text += event.text or ""
                    yield event
                elif event.type == TurnEventType.ERROR:
                    is_error = True
                    collected_text = event.error or "External agent error"
                    yield event
                # Other event types (TURN_START, etc.) are not
                # relevant in a sub-tool context — silently drop.
        except Exception as e:
            is_error = True
            collected_text = f"Delegation error: {e}"
            yield TurnEvent.error_event(collected_text)

        # Sanitize the result before returning to the LLM. Untrusted
        # agent output flows into our conversation history, so it
        # gets the same prompt-injection defense as a tool result.
        sanitized = sanitize_tool_result(collected_text, tc.name)
        yield TurnEvent.tool_result_event(tc.id, tc.name, sanitized, is_error)
        return ToolResult(
            tool_call_id=tc.id,
            name=tc.name,
            content=sanitized,
            is_error=is_error,
        )

    def _verify_mutation(self, tool_name: str, args: dict[str, Any], result: str) -> _MutationVerification:
        """Verify that a mutating tool actually succeeded.

        Returns a :class:`_MutationVerification` whose *ok* attribute is
        ``True`` only when the tool is confirmed to have succeeded.

        Uses a two-pass strategy for the high-confidence mutation tools
        listed in ``_VERIFY_MUTATION_TOOLS``:

        1. **String heuristic** — the result text is checked for clear
           failure indicators (``"Failed"``, ``"Decompilation failed"``,
           etc.).
        2. **Post-state verification** — the matching getter tool is
           called and the returned value is compared exactly against the
           expected mutation result.  If the getter is missing or throws,
           verification fails (does not fall back to string heuristic).

        For tools not in ``_VERIFY_MUTATION_TOOLS``, only the string
        heuristic is used.

        This method never raises — all exceptions are caught and
        returned as a failed verification result.
        """
        # --- Pass 1: string heuristic ---
        if _result_indicates_failure(result):
            return _MutationVerification(False, "result indicates failure")

        # --- Pass 2: post-state verification (high-confidence tools only) ---
        if tool_name not in _VERIFY_MUTATION_TOOLS:
            return _MutationVerification(True)

        try:
            if tool_name == "rename_function":
                addr = args.get("address", "")
                new_name = args.get("new_name", "")
                actual = self.tools.execute("get_function_name", {"address": addr})
                return _MutationVerification(
                    str(actual) == str(new_name),
                    f"expected {new_name!r}, got {str(actual)!r}",
                )

            elif tool_name == "rename_address":
                addr = args.get("address", "")
                new_name = args.get("new_name", "")
                actual = self.tools.execute("get_address_name", {"address": addr})
                return _MutationVerification(
                    str(actual) == str(new_name),
                    f"expected {new_name!r}, got {str(actual)!r}",
                )
            elif tool_name == "set_comment":
                addr = args.get("address", "")
                comment = args.get("comment", "")
                if not isinstance(comment, str):
                    comment = str(comment)
                repeatable = coerce_bool(args.get("repeatable", False))
                actual = self.tools.execute("get_comment", {"address": addr, "repeatable": repeatable})
                return _MutationVerification(
                    str(actual) == comment,
                    f"expected {comment!r}, got {str(actual)!r}",
                )

            elif tool_name == "set_function_comment":
                addr = args.get("address", "")
                comment = args.get("comment", "")
                if not isinstance(comment, str):
                    comment = str(comment)
                repeatable = coerce_bool(args.get("repeatable", False))
                actual = self.tools.execute("get_function_comment", {"address": addr, "repeatable": repeatable})
                return _MutationVerification(
                    str(actual) == comment,
                    f"expected {comment!r}, got {str(actual)!r}",
                )

            elif tool_name == "set_pseudocode_comment":
                raw = self.tools.execute(
                    "get_pseudocode_comment_state",
                    {
                        "func_address": args.get("func_address", ""),
                        "target_address": args.get("target_address", ""),
                    },
                )
                try:
                    state = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    return _MutationVerification(False, "malformed pseudocode comment state JSON")
                if not isinstance(state, dict):
                    return _MutationVerification(False, "pseudocode comment state is not a dict")
                if state.get("ok") is not True:
                    return _MutationVerification(False, "pseudocode comment state ok is not True")
                comment = args.get("comment", "")
                if not isinstance(comment, str):
                    comment = str(comment)
                actual = state.get("comment", "")
                if not isinstance(actual, str):
                    return _MutationVerification(False, "pseudocode comment state comment is not a string")
                return _MutationVerification(
                    actual == comment,
                    f"expected {comment!r}, got {actual!r}",
                )

        except ToolNotFoundError as exc:
            return _MutationVerification(False, f"verification getter missing: {exc}")
        except ToolError as exc:
            return _MutationVerification(False, f"verification getter failed: {exc}")
        except Exception as exc:
            log_debug(f"mutation post-state verification failed for {tool_name}: {exc}")
            return _MutationVerification(False, f"verification exception: {type(exc).__name__}")

        return _MutationVerification(True)

    def _execute_tool_calls(
        self,
        tool_calls: list[ToolCall],
    ) -> Generator[TurnEvent, None, list[ToolResult]]:
        """Execute tool calls, yielding result events. Returns ToolResult list."""
        tool_results: list[ToolResult] = []
        for tc in tool_calls:
            self._check_cancelled()
            state = self._exploration_state
            persisted = self.session.metadata.get("active_mode", "")
            if tc.name == "research_note" and (self._research_state is not None or persisted == "research"):
                self._ensure_research_state()
                tr = yield from self._handle_research_note_tool(tc)
            elif tc.name == "exploration_report" and (state is not None or persisted in ("exploration", "research")):
                state = self._ensure_exploration_state()
                tr = yield from self._handle_exploration_report_tool(tc, state)
            elif tc.name == "phase_transition" and (state is not None or persisted in ("exploration", "research")):
                state = self._ensure_exploration_state()
                tr = yield from self._handle_phase_transition_tool(tc, state)
            elif tc.name == "save_memory":
                tr = yield from self._handle_save_memory_tool(tc)
            elif tc.name == "spawn_subagent":
                tr = yield from self._handle_spawn_subagent_tool(tc)
            elif tc.name == "activate_skill":
                tr = yield from self._handle_activate_skill_tool(tc)
            elif tc.name == "ask_user":
                tr = yield from self._handle_ask_user_tool(tc)
            elif tc.name == "delegate_external_task":
                tr = yield from self._handle_delegate_external_task_tool(tc)
            else:
                tr = yield from self._execute_single_tool(tc)
            tool_results.append(tr)
        return tool_results

    def _build_tools_schema(
        self, active_skill: Any, use_exploration_mode: bool, use_research_mode: bool = False
    ) -> list:
        """Build the full tool schema list for a run, including pseudo-tools."""
        # Include pseudo-tools from a persisted mode so they remain available
        # after cancel + continue (the LLM still sees mode context in history).
        persisted_mode = self.session.metadata.get("active_mode", "")
        if persisted_mode == "research":
            use_research_mode = True
        elif persisted_mode == "exploration":
            use_exploration_mode = True

        tools_schema = list(self.tools.to_provider_format())

        if self._unattended:
            # No UI answers this loop's approval/question queues — never
            # advertise tools that would block in _wait_for_queue. The
            # SubagentRunner registry view already drops these; this repeat
            # filter keeps the invariant local to the loop for direct
            # constructions (tests, future callers).
            gated = {constants.EXECUTE_PYTHON_TOOL_NAME} | {
                d.name for d in self.tools.list_tools() if d.requires_approval
            }
            tools_schema = [t for t in tools_schema if t.get("function", {}).get("name") not in gated]

        # Filter to skill-allowed tools if the skill restricts them
        if active_skill and active_skill.allowed_tools:
            allowed = set(active_skill.allowed_tools)
            tools_schema = [t for t in tools_schema if t.get("function", {}).get("name") in allowed]

        # Profile: remove denied tools
        profile = self.config.get_active_profile()
        if profile.denied_tools:
            denied = set(profile.denied_tools)
            tools_schema = [t for t in tools_schema if t.get("function", {}).get("name") not in denied]
        # Config: hide string-list/search tools when analysis strings are disabled
        if self.config.hide_strings:
            _hidden = {"list_strings", "search_strings"}
            tools_schema = [t for t in tools_schema if t.get("function", {}).get("name") not in _hidden]
        # activate_skill: dynamic because the slug enum depends on loaded skills
        if self.skills and self.skills.list_slugs():
            tools_schema.append(
                {
                    "type": "function",
                    "function": {
                        "name": "activate_skill",
                        "description": (
                            "Load a skill's full prompt and reference material into context. "
                            "Call this when the user's request matches a skill's domain "
                            "(e.g., activate 'malware-analysis' for malware tasks, "
                            "'vuln-audit' for security audits, 'ida-scripting' "
                            "when you need to write scripts). "
                            "The skill body will be returned so you can follow its methodology."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "slug": {
                                    "type": "string",
                                    "description": "The skill slug to activate.",
                                    "enum": self.skills.list_slugs(),
                                },
                            },
                            "required": ["slug"],
                        },
                    },
                }
            )

        if use_exploration_mode:
            tools_schema.append(EXPLORATION_REPORT_SCHEMA)
            tools_schema.append(PHASE_TRANSITION_SCHEMA)

        if use_research_mode:
            tools_schema.append(RESEARCH_NOTE_SCHEMA)
            tools_schema.append(EXPLORATION_REPORT_SCHEMA)
            tools_schema.append(SPAWN_SUBAGENT_SCHEMA)

        if self.session.idb_path:
            tools_schema.append(SAVE_MEMORY_SCHEMA)

        tools_schema.append(SPAWN_SUBAGENT_SCHEMA)
        if not self._unattended:
            tools_schema.append(ASK_USER_SCHEMA)
            tools_schema.append(DELEGATE_EXTERNAL_TASK_SCHEMA)

        # Deduplicate — Anthropic rejects requests with duplicate tool names
        seen: set = set()
        deduped: list = []
        for t in tools_schema:
            name = t.get("function", t).get("name", "")
            if name and name not in seen:
                seen.add(name)
                deduped.append(t)
        return deduped

    def _resolve_direct_handler(self, cmd) -> Callable[[], Any] | None:
        """Return a zero-arg callable for the chosen direct command, or None."""
        if cmd.direct_command == "/goal":
            return lambda: _handle_goal_command(self, cmd.direct_arg)
        if cmd.direct_command == "/memory":
            return lambda: _handle_memory_command(self)
        if cmd.direct_command == "/case":
            return lambda: self._handle_case_command(cmd.direct_arg)
        if cmd.direct_command == "/undo":
            return lambda: _handle_undo_command(self, cmd.direct_arg)
        if cmd.direct_command == "/mcp":
            return lambda: _handle_mcp_command(self)
        if cmd.direct_command == "/doctor":
            return lambda: _handle_doctor_command(self)
        if cmd.direct_command == "/knowledge":
            return lambda: _handle_knowledge_command(self, cmd.direct_arg)
        if cmd.direct_command == "/report":
            return lambda: _handle_report_command(self, cmd.direct_arg)
        if cmd.direct_command == "/verify":
            return lambda: _handle_verify_command(self, cmd.direct_arg)
        return None

    def _dispatch_direct(
        self,
        handler: Callable[[], Any],
    ) -> Generator[TurnEvent, None, None]:
        """Wrap a direct-command handler in paired turn lifecycle events.

        Emits ``TURN_START(1)`` before the handler runs and ``TURN_END(1)``
        in a ``finally`` so the UI shows its "thinking" indicator while
        direct commands like ``/report`` are in flight (including the
        user-question wait) and the indicator is always hidden on error
        or cancellation.
        """
        yield TurnEvent.turn_start(1)
        try:
            yield from handler()
        finally:
            yield TurnEvent.turn_end(1)

    def run(self, user_message: str) -> Generator[TurnEvent, None, None]:
        """Run the agent loop for a user message. Yields TurnEvents.

        This generator should be consumed from a background thread,
        while the UI reads events via the event_queue or directly iterates.
        """
        # Reset stale state only when the event is ours. An inherited or
        # externally-supplied event may already carry a user cancellation
        # (e.g. set between two pipeline child runs) — clearing it here
        # loses that cancellation silently.
        if self._owns_cancel_event:
            self._cancelled.clear()
        self._docs_reviewer_invoked = False
        self._running = True
        self.session.is_running = True

        try:
            cmd = _parse_user_command(user_message)
            # Map each direct command to its handler. Wrapping the
            # dispatch in a single ``_dispatch_direct`` helper emits
            # paired ``TURN_START``/``TURN_END`` events so the UI
            # shows a "thinking" indicator while direct commands
            # like ``/report`` are running, including the long wait
            # on the user-question queue.
            direct_handler = self._resolve_direct_handler(cmd)
            if direct_handler is not None:
                yield from self._dispatch_direct(direct_handler)
                return

            # Orchestra early safety gate: yield one TEXT_DONE and
            # exit BEFORE skill resolution, persisted-mode resumption,
            # session append, prompt/schema construction, retrieval,
            # provider call, tool execution, or child agent. This
            # placement is critical — anything below it assumes the
            # command is safe to run.
            if cmd.use_orchestra_mode and not _ORCHESTRA_ENABLED:
                yield TurnEvent.text_done(_ORCHESTRA_DISABLED_MESSAGE)
                return

            user_message = cmd.message
            use_plan_mode = cmd.use_plan_mode
            use_exploration_mode = cmd.use_exploration_mode
            explore_only = cmd.explore_only
            use_research_mode = cmd.use_research_mode

            # Persist any goal the parser picked up (e.g. `/goal
            # <objective>`) BEFORE the system prompt is built so the
            # freshly constructed prompt includes the `## Active Goal`
            # section for this very run. ``cmd.message`` already
            # contains only the objective text, so the model never sees
            # the `/goal ...` prefix.
            if cmd.goal_to_set:
                self.session.metadata[_GOAL_METADATA_KEY] = cmd.goal_to_set

            user_message, active_skill = self._resolve_skill(user_message)
            if active_skill and active_skill.mode == "exploration":
                use_exploration_mode = True
            elif active_skill and active_skill.mode == "plan":
                use_plan_mode = True

            # Resume the persisted mode pipeline on follow-up after cancel.
            # The conversation history already has the prior tool calls/results,
            # so the LLM will pick up where it left off rather than re-exploring.
            if not (use_research_mode or use_exploration_mode or use_plan_mode or cmd.direct_command):
                persisted = self.session.metadata.get("active_mode", "")
                if persisted == "research":
                    use_research_mode = True
                elif persisted == "exploration":
                    use_exploration_mode = True

            self.session.add_message(Message(role=Role.USER, content=user_message))
            system_prompt = minify_text(self._build_system_prompt())
            tools_schema = self._build_tools_schema(active_skill, use_exploration_mode, use_research_mode)
            log_debug(f"Agent run started: {len(tools_schema)} tools, msg={user_message[:80]!r}")

            # Emit a one-shot retrieved-knowledge indicator so the UI
            # (when enabled) can show what was pulled into this turn.
            # The actual retrieval already happened during
            # _build_system_prompt → metadata stashed in
            # session.metadata["last_knowledge_retrieval"].
            try:
                meta = self.session.metadata.get("last_knowledge_retrieval")
                if meta and getattr(self.config, "knowledge_show_retrieved_in_chat", False):
                    counts = meta.get("counts") or {}
                    parts = []
                    for k in ("memories", "entities", "relations", "notes"):
                        if counts.get(k):
                            parts.append(f"{counts[k]} {k}")
                    summary = ", ".join(parts) if parts else "no items"
                    yield TurnEvent.knowledge_retrieved(summary=summary, counts=counts, items=meta.get("items", []))
            except Exception as e:
                log_debug(f"knowledge_retrieved event emission failed: {e}")

            if use_research_mode:
                self.session.metadata["active_mode"] = "research"
                yield from run_research_mode(
                    self,
                    user_message,
                    system_prompt,
                    tools_schema,
                )
                # Mode completed normally — clear so follow-ups don't
                # keep re-including mode tools.
                self.session.metadata.pop("active_mode", None)
                self.session.metadata.pop("mode_phase", None)
                return

            if cmd.use_orchestra_mode:
                yield from run_orchestra_mode(
                    self,
                    user_message,
                    system_prompt,
                    tools_schema,
                )
                return

            if cmd.use_a2a_mode:
                # /a2a bypasses the LLM turn cycle entirely — the
                # dispatcher streams events straight to the chat.
                # No session metadata is set because this is a
                # one-shot action, not a stateful mode like
                # orchestra / exploration / plan.
                yield from run_a2a_mode(
                    self,
                    cmd.message,
                    system_prompt,
                    tools_schema,
                )
                return

            if use_exploration_mode:
                self.session.metadata["active_mode"] = "exploration"
                yield from run_exploration_mode(
                    self,
                    user_message,
                    system_prompt,
                    tools_schema,
                    explore_only=explore_only,
                )
                self.session.metadata.pop("active_mode", None)
                self.session.metadata.pop("mode_phase", None)
                return

            if use_plan_mode or self.plan_mode:
                yield from run_plan_mode(
                    self,
                    user_message,
                    system_prompt,
                    tools_schema,
                    active_skill=active_skill,
                )
                return

            yield from run_normal_loop(self, system_prompt, tools_schema)

        except CancellationError:
            yield TurnEvent.cancelled_event()
        except Exception as e:
            log_error(f"Agent loop error: {e}\n{traceback.format_exc()}")
            yield TurnEvent.error_event(str(e))
        finally:
            self._running = False
            self.session.is_running = False


_EVENT_QUEUE_MAXSIZE = 500


class BackgroundAgentRunner:
    """Runs the AgentLoop in a background thread, bridging to a bounded queue.

    When the queue is full, consecutive TEXT_DELTA events are coalesced
    into a single event instead of being dropped.
    """

    def __init__(self, agent_loop: AgentLoop):
        self.agent_loop = agent_loop
        self.event_queue: queue.Queue[TurnEvent | None] = queue.Queue(
            maxsize=_EVENT_QUEUE_MAXSIZE,
        )
        self._thread: threading.Thread | None = None

    def start(self, user_message: str) -> None:
        """Start the agent in a background thread."""
        self._thread = threading.Thread(
            target=self._run,
            args=(user_message,),
            daemon=True,
        )
        self._thread.start()

    def _run(self, user_message: str) -> None:
        """Symmetric coalescing runner with low-latency pass-through.

        Maintains a single pending delta type and one buffer. When the
        queue has room, each delta is delivered immediately (low latency).
        Only when the queue is full does the buffer absorb backpressure,
        coalescing consecutive same-type deltas. On type switch, the
        pending buffer is flushed as a single coalesced event before the
        new type begins. This keeps REASONING_DELTA and TEXT_DELTA
        buffers separate.

        RECOVERY_START is a hard boundary: the pending buffer is flushed
        (or discarded if ``discard_transient_reasoning`` is set and the
        pending buffer is reasoning) before it is enqueued, and it is
        never coalesced itself.

        Control events and the sentinel are never dropped — ``_safe_put``
        uses ``timeout=1`` with ``except queue.Full`` to avoid deadlock.
        """
        pending_type: TurnEventType | None = None
        pending_buffer: list[str] = []

        def _flush_pending() -> None:
            """Emit the buffered deltas as one coalesced event."""
            nonlocal pending_type
            if not pending_buffer:
                pending_type = None
                return
            if pending_type == TurnEventType.TEXT_DELTA:
                evt = TurnEvent.text_delta("".join(pending_buffer))
            elif pending_type == TurnEventType.REASONING_DELTA:
                evt = TurnEvent.reasoning_event("".join(pending_buffer))
            else:
                pending_buffer.clear()
                pending_type = None
                return
            try:
                self.event_queue.put(evt, timeout=1)
            except queue.Full:
                log_debug(f"Event queue full, dropping coalesced {pending_type.value}")
            pending_buffer.clear()
            pending_type = None

        def _safe_put(event: TurnEvent) -> None:
            """Put without blocking indefinitely on a full queue.

            Used for control events and sentinel — these must never
            deadlock the producer thread.
            """
            try:
                self.event_queue.put(event, timeout=1)
            except queue.Full:
                log_debug(f"Event queue full, dropping {event.type.value}")

        try:
            for event in self.agent_loop.run(user_message):
                if event.type == TurnEventType.TEXT_DELTA:
                    if pending_type is not None and pending_type != TurnEventType.TEXT_DELTA:
                        _flush_pending()
                    pending_type = TurnEventType.TEXT_DELTA
                    if self.event_queue.full():
                        # Backpressure: buffer for coalescing.
                        pending_buffer.append(event.text)
                    else:
                        # Low latency: deliver immediately.
                        if pending_buffer:
                            pending_buffer.append(event.text)
                            event = TurnEvent.text_delta("".join(pending_buffer))
                            pending_buffer.clear()
                            pending_type = None
                        self.event_queue.put(event)
                elif event.type == TurnEventType.REASONING_DELTA:
                    if pending_type is not None and pending_type != TurnEventType.REASONING_DELTA:
                        _flush_pending()
                    pending_type = TurnEventType.REASONING_DELTA
                    if self.event_queue.full():
                        pending_buffer.append(event.reasoning)
                    else:
                        if pending_buffer:
                            pending_buffer.append(event.reasoning)
                            event = TurnEvent.reasoning_event("".join(pending_buffer))
                            pending_buffer.clear()
                            pending_type = None
                        self.event_queue.put(event)
                elif event.type == TurnEventType.RECOVERY_START:
                    discard = event.metadata.get("discard_transient_reasoning", False)
                    if discard and pending_type == TurnEventType.REASONING_DELTA:
                        pending_buffer.clear()
                        pending_type = None
                    else:
                        _flush_pending()
                    _safe_put(event)
                else:
                    _flush_pending()
                    _safe_put(event)
        except Exception as e:
            log_error(f"BackgroundAgentRunner error: {e}\n{traceback.format_exc()}")
            _flush_pending()
            _safe_put(TurnEvent.error_event(str(e)))
        finally:
            _flush_pending()
            self.event_queue.put(None)  # Sentinel

    def cancel(self) -> None:
        self.agent_loop.cancel()

    def get_event(self, timeout: float = 0.1) -> TurnEvent | None:
        """Get the next event, or None if queue is empty."""
        try:
            return self.event_queue.get(timeout=timeout)
        except queue.Empty:
            return None


def _is_chunked_read_error(exc: BaseException) -> bool:
    """Return True when *exc* is a transport-layer ``incomplete chunked read``.

    The OpenAI / Anthropic SDKs surface upstream HTTP/1.1 stream
    truncation as ``APIConnectionError`` with messages like
    ``peer closed connection without sending complete message body
    (incomplete chunked read)``. These are always transient and
    retryable: the chunks that did arrive are typically model
    prefill noise, not user-facing content, so the right action is
    to re-raise so the retry layer can resend the request.
    """
    msg = str(exc or "").lower()
    if not msg:
        return False
    needles = (
        "incomplete chunked read",
        "peer closed connection",
        "connection broken",
        "incomplete read",
    )
    return any(needle in msg for needle in needles)


def _is_glm_provider(config: RikuganConfig, provider: LLMProvider) -> bool:
    """True iff the active provider speaks the GLM dialect.

    Single source of truth shared by the guard factory and the
    recovery builder. Returns True when ``provider.extra`` carries
    ``dialect == "glm"`` or the provider advertises itself as GLM.
    """
    extra = getattr(getattr(config, "provider", None), "extra", None) or {}
    return extra.get("dialect") == "glm" or getattr(provider, "name", "") == "glm"
