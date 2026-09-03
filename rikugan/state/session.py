"""Session state management."""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from ..core.logging import log_debug
from ..core.sanitize import strip_injection_markers
from ..core.types import Message, Role, TokenUsage, ToolResult

# ---------- Token estimation ----------

_CHARS_PER_TOKEN = 3.5

# Tool results older than this many messages from the end get truncated
_OLD_RESULT_THRESHOLD = 8
_OLD_RESULT_MAX_CHARS = 500
_RECENT_RESULT_MAX_CHARS = 8000


def _estimate_tokens(msg: Message) -> int:
    """Rough token count estimate from message text content."""
    chars = len(msg.content or "")
    for tc in msg.tool_calls:
        chars += len(tc.name) + 50
        if tc.arguments:
            try:
                chars += len(json.dumps(tc.arguments))
            except (TypeError, ValueError):
                chars += 100
    for tr in msg.tool_results:
        chars += len(tr.content or "") + len(tr.name or "") + 20
    return max(1, int(chars / _CHARS_PER_TOKEN))


def _truncate_tool_result(tr: ToolResult, max_chars: int) -> ToolResult:
    """Return a truncated copy of a tool result if it exceeds max_chars."""
    if not tr.content or len(tr.content) <= max_chars:
        return tr
    omitted = len(tr.content) - max_chars
    return ToolResult(
        tool_call_id=tr.tool_call_id,
        name=tr.name,
        content=tr.content[:max_chars] + f"\n... [{omitted} chars omitted]",
        is_error=tr.is_error,
    )


@dataclass
class SessionState:
    """Holds the state of one Rikugan conversation session.

    Thread-safety: all mutations to ``messages`` are guarded by ``_lock``.
    Readers that need a consistent snapshot should also hold the lock.
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    messages: list[Message] = field(default_factory=list)
    total_usage: TokenUsage = field(default_factory=TokenUsage)
    last_prompt_tokens: int = 0
    current_turn: int = 0
    is_running: bool = False
    provider_name: str = ""
    model_name: str = ""
    idb_path: str = ""
    db_instance_id: str = ""
    # Central memory workspace binding — durable across reopen/restart.
    # Populated by the MemoryWorkspaceManager when central memory is enabled.
    binary_memory_id: str = ""
    active_case_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    # Subagent message logs, keyed by the spawn_subagent tool_call_id.
    # Stored separately from main messages to avoid burning context tokens.
    subagent_logs: dict[str, list[Message]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._token_estimate: int = 0
        # Phase 3 caches:
        #
        # ``_revision`` is incremented on every message mutation. The
        # provider-message cache keys on the revision + ``context_window``
        # + ``preserve_context`` triple, so any mutation forces a rebuild
        # on the next read.
        #
        # ``_provider_cache`` holds the sanitized/minified message lists
        # keyed by ``(revision, context_window, preserve_context)``.
        # Values are *list copies* — callers cannot mutate the cached
        # payload and corrupt future reads.
        #
        # ``_message_token_cache`` is an ``id(msg) -> int`` map that lets
        # :func:`_estimate_tokens` skip the (relatively expensive) char
        # walk on repeated calls for the same ``Message`` object. Cleared
        # on full replace/clear since object identity no longer matches.
        self._revision: int = 0
        self._provider_cache: dict[tuple[int, int, bool], list[Message]] = {}
        self._message_token_cache: dict[int, int] = {}

    @property
    def token_estimate(self) -> int:
        """Running O(1) estimate of total token usage across all messages."""
        return self._token_estimate

    def add_message(self, msg: Message) -> None:
        with self._lock:
            self.messages.append(msg)
            self._token_estimate += _estimate_tokens(msg)
            self._revision += 1
            # Cache the per-message token estimate for the just-added
            # object — it's likely to be re-counted during provider
            # preparation below.
            self._message_token_cache[id(msg)] = _estimate_tokens(msg)
            if msg.token_usage:
                self._record_usage_locked(msg.token_usage)

    def record_usage(self, usage: TokenUsage) -> None:
        """Record token usage from a discarded (non-persisted) attempt.

        Used by the GLM one-shot recovery transaction to account for the
        degenerated first attempt's tokens without storing its assistant
        message.  Successful persisted messages still account through
        :meth:`add_message`, which calls the same internal helper — so the
        two paths can never double-count.
        """
        with self._lock:
            self._record_usage_locked(usage)

    def _record_usage_locked(self, usage: TokenUsage) -> None:
        """Internal: accumulate usage into ``total_usage``.

        Caller MUST hold ``self._lock``.
        """
        self.total_usage.prompt_tokens += usage.prompt_tokens
        self.total_usage.completion_tokens += usage.completion_tokens
        self.total_usage.total_tokens += usage.total_tokens
        self.total_usage.cache_read_tokens += usage.cache_read_tokens
        self.total_usage.cache_creation_tokens += usage.cache_creation_tokens
        if usage.prompt_tokens > 0:
            self.last_prompt_tokens = usage.context_tokens

    def clear(self) -> None:
        with self._lock:
            self.messages.clear()
            self._token_estimate = 0
            self.total_usage = TokenUsage()
            self.last_prompt_tokens = 0
            self.current_turn = 0
            self.is_running = False
            self._revision += 1
            self._provider_cache.clear()
            self._message_token_cache.clear()

    def prune_messages(self, keep_last_n: int = 50) -> int:
        """Drop old messages in place, preserving the system prompt + last N.

        Returns the number of messages removed.
        """
        with self._lock:
            if len(self.messages) <= keep_last_n + 1:
                return 0

            # Keep messages[0] (system prompt / first user message) + tail.
            # Advance the tail start past any TOOL message whose assistant
            # tool_call partner falls outside the tail — otherwise the
            # provider rejects the orphaned tool result (OpenAI/Anthropic
            # both 400). The walk never reaches the head: tail_start >= 1.
            tail_start = max(1, len(self.messages) - keep_last_n)
            while (
                tail_start < len(self.messages)
                and self.messages[tail_start].role == Role.TOOL
            ):
                tail_start += 1
            head = self.messages[:1]
            tail = self.messages[tail_start:]
            removed_msgs = self.messages[1:tail_start]
            removed = len(removed_msgs)
            for m in removed_msgs:
                self._token_estimate -= _estimate_tokens(m)
                # Drop the per-message cache entry for removed messages so
                # the dict doesn't grow without bound across long sessions.
                self._message_token_cache.pop(id(m), None)
            self._token_estimate = max(0, self._token_estimate)
            self.messages[:] = head + tail
            self._revision += 1
            return removed

    def replace_messages(self, messages: list[Message]) -> None:
        """Atomically replace the message list (used by context compaction).

        Recomputes the running token estimate from scratch, increments
        the revision, and invalidates the provider-message cache.  The
        message-token cache is also cleared because the surviving
        messages keep the same Python identity but the bulk-replace
        semantic means future token estimates should not silently use
        stale entries.

        Prefer this over ``self.messages[:] = new_list`` so the cache
        invalidation hooks fire consistently.  The agent loop used to
        do ``self.session.messages[:] = self._context_manager.compact_messages(...)``
        directly under the session lock; that path bypassed all the
        bookkeeping and was a latent bug for any future cache.
        """
        with self._lock:
            self.messages[:] = messages
            self._token_estimate = sum(_estimate_tokens(m) for m in messages)
            self._revision += 1
            self._provider_cache.clear()
            self._message_token_cache.clear()

    def get_messages_for_provider(
        self,
        context_window: int = 0,
        preserve_context: bool = False,
    ) -> list[Message]:
        """Return messages sanitized and trimmed for the provider API.

        1. Ensures every tool_use has a matching tool_result.
        2. Strips injection markers from assistant output (anti self-injection).
        3. Truncates old / large tool results (skipped when *preserve_context*).
        4. Drops oldest messages if the estimated token count exceeds
           the context window budget (skipped when *preserve_context*).

        When *preserve_context* is True, only safety sanitization is applied —
        no tool result truncation or message trimming.  This preserves full
        decompilation output and analysis context at the cost of higher token
        usage.

        Results are cached by ``(revision, context_window, preserve_context)``
        so back-to-back calls from the same turn — e.g. an estimate pass
        and the actual provider payload — do not pay for the full
        sanitize + truncate + trim pipeline twice.  Returns a *list copy*
        of the cached value so callers cannot mutate the cache entry.
        """
        with self._lock:
            snapshot = list(self.messages)
            revision = self._revision
            cache_key = (revision, context_window, preserve_context)
            cached = self._provider_cache.get(cache_key)

        if cached is not None:
            return list(cached)

        sanitized = self._sanitize(snapshot)
        sanitized = self._sanitize_assistant_output(sanitized)
        if not preserve_context:
            sanitized = self._truncate_results(sanitized)
            if context_window > 0:
                sanitized = self._trim_to_budget(sanitized, context_window)

        with self._lock:
            # Re-check after re-acquiring the lock — another thread may
            # have completed a rebuild while we were sanitizing.
            current_revision = self._revision
            if current_revision == revision:
                self._provider_cache[cache_key] = list(sanitized)
        return sanitized

    # --- Internal helpers ---

    @staticmethod
    def _sanitize_assistant_output(messages: list[Message]) -> list[Message]:
        """Strip injection markers from assistant text (anti self-injection).

        The model may reconstruct filtered strings in its own response —
        e.g. by reading raw bytes via hexdump and decoding them to ASCII.
        This prevents those strings from re-entering the context on
        subsequent turns while leaving the displayed message untouched.
        """
        result: list[Message] = []
        for msg in messages:
            if msg.role == Role.ASSISTANT and msg.content:
                cleaned = strip_injection_markers(msg.content)
                if cleaned != msg.content:
                    result.append(replace(msg, content=cleaned))
                    continue
            result.append(msg)
        return result

    @staticmethod
    def _sanitize(msgs: list[Message]) -> list[Message]:
        """Patch orphaned tool_use blocks with synthetic error results."""
        sanitized: list[Message] = []
        i = 0
        while i < len(msgs):
            msg = msgs[i]
            if msg.role == Role.ASSISTANT and msg.tool_calls:
                sanitized.append(msg)
                i += 1
                needed_ids: set[str] = {tc.id for tc in msg.tool_calls}
                if i < len(msgs) and msgs[i].role == Role.TOOL:
                    tool_msg = msgs[i]
                    found_ids = {tr.tool_call_id for tr in tool_msg.tool_results}
                    missing = needed_ids - found_ids
                    if missing:
                        log_debug(f"Sanitize: patching {len(missing)} orphaned tool_use(s): {', '.join(missing)}")
                        patched_results = list(tool_msg.tool_results)
                        for tc in msg.tool_calls:
                            if tc.id in missing:
                                patched_results.append(
                                    ToolResult(
                                        tool_call_id=tc.id,
                                        name=tc.name,
                                        content="Cancelled.",
                                        is_error=True,
                                    )
                                )
                        sanitized.append(
                            Message(
                                role=Role.TOOL,
                                tool_results=patched_results,
                            )
                        )
                    else:
                        sanitized.append(tool_msg)
                    i += 1
                else:
                    log_debug(
                        f"Sanitize: no tool_result message for {len(msg.tool_calls)} tool_use(s), inserting stubs"
                    )
                    stubs = [
                        ToolResult(
                            tool_call_id=tc.id,
                            name=tc.name,
                            content="Cancelled.",
                            is_error=True,
                        )
                        for tc in msg.tool_calls
                    ]
                    sanitized.append(Message(role=Role.TOOL, tool_results=stubs))
            else:
                sanitized.append(msg)
                i += 1
        return sanitized

    @staticmethod
    def _truncate_results(messages: list[Message]) -> list[Message]:
        """Truncate tool results — aggressively for old, moderately for recent."""
        n = len(messages)
        result: list[Message] = []
        for idx, msg in enumerate(messages):
            if msg.role != Role.TOOL or not msg.tool_results:
                result.append(msg)
                continue
            age = n - idx
            max_chars = _OLD_RESULT_MAX_CHARS if age > _OLD_RESULT_THRESHOLD else _RECENT_RESULT_MAX_CHARS
            new_results = [_truncate_tool_result(tr, max_chars) for tr in msg.tool_results]
            result.append(Message(role=Role.TOOL, tool_results=new_results))
        return result

    @staticmethod
    def _trim_to_budget(messages: list[Message], context_window: int) -> list[Message]:
        """Drop oldest messages if estimated tokens exceed context budget."""
        # Reserve 25% for system prompt + new completion
        budget = int(context_window * 0.75)
        total = sum(_estimate_tokens(m) for m in messages)

        if total <= budget:
            return messages

        # Drop messages from the front, keeping at least the last 4
        result = list(messages)
        while total > budget and len(result) > 4:
            removed = result.pop(0)
            total -= _estimate_tokens(removed)
            # If we removed a USER msg, also drop the following assistant+tool
            # to keep message pairs coherent
            while result and result[0].role != Role.USER and len(result) > 4:
                removed = result.pop(0)
                total -= _estimate_tokens(removed)

        return result

    def message_count(self) -> int:
        with self._lock:
            return len(self.messages)
