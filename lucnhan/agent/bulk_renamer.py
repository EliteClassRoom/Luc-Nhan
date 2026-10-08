"""Bulk function renaming engine with quick and deep analysis modes."""

from __future__ import annotations

import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core.config import LucNhanConfig
from ..core.logging import log_debug, log_error, log_info
from ..core.types import Message, Role
from ..providers.base import LLMProvider
from ..skills.registry import SkillRegistry
from ..tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

QUICK_ANALYSIS_PROMPT = """\
You are a reverse engineering assistant specializing in function naming.

Below are decompiled functions from a binary, each accompanied by its
disassembly listing when available. For each function, suggest a descriptive
name based on its behavior.

Naming convention (CRITICAL):
- Functions: PascalCase verb-noun (InitializeGlobals, ParseHttpRequest,
  DecryptConfig). NEVER snake_case.
- Use verb prefixes: Init/Parse/Send/Recv/Encrypt/Decrypt/Alloc/Free/
  Check/Validate/Handle/Dispatch.
- If a function is a wrapper/thunk, prefix: j_<Orig>, thunk_<Orig>,
  or <Orig>Wrapper.
- If a function's purpose is unclear (<70% confident), output:
  Unknown_<Hint>_<hexaddr>   (e.g. Unknown_HashFunc_4012a0)
  Do NOT guess a confident name when uncertain.

Rules:
- Analyze what each function does based on decompiled code + disassembly
- If a function is a wrapper, name it after what it wraps (e.g. MallocWrapper)
- Use both decompiled code AND disassembly to understand the function

Output format: one line per function, exactly:
0x<address> <new_name>

Do NOT include any other text, explanations, or markdown formatting.
Only output the address-name pairs.

Functions to analyze:
"""

DEEP_ANALYSIS_PROMPT = """\
You are a reverse engineering expert. Analyze this function in depth.

Examine:
1. All callers and callees (decompile them if needed)
2. String references
3. API imports used
4. Data structures accessed
5. Control flow patterns
6. Magic constants (CRC32=0xEDB88320, AES S-box, SHA256 init=0x6a09e667)

Based on your thorough analysis, determine the function's purpose and
suggest a single descriptive name using PascalCase verb-noun convention
(InitializeGlobals, DecryptConfig, ParseHttpRequest). NEVER snake_case.

If confidence <70%, output:
Unknown_<Hint>_<hexaddr>   (e.g. Unknown_HashFunc_4012a0)

Your final line of output MUST be exactly:
RENAME: 0x<address> <new_name>

Function to analyze:
"""

# ---------------------------------------------------------------------------
# Auto-generated name patterns to skip
# ---------------------------------------------------------------------------

_AUTO_NAME_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^sub_[0-9a-fA-F]+$"),
    re.compile(r"^FUN_[0-9a-fA-F]+$"),
    re.compile(r"^func_[0-9a-fA-F]+$"),
    re.compile(r"^unnamed_[0-9a-fA-F]+$"),
    re.compile(r"^loc_[0-9a-fA-F]+$"),
]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class RenameStatus(str, Enum):
    PENDING = "pending"
    DECOMPILING = "decompiling"
    ANALYZING = "analyzing"
    RENAMING = "renaming"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class RenameJob:
    """A single function rename request."""

    address: int
    current_name: str
    new_name: str = ""
    status: RenameStatus = RenameStatus.PENDING
    error: str = ""
    decompiled_code: str = ""


class RenameEventType(str, Enum):
    JOB_STARTED = "job_started"
    JOB_COMPLETED = "job_completed"
    JOB_ERROR = "job_error"
    BATCH_PROGRESS = "batch_progress"
    ALL_DONE = "all_done"


@dataclass
class RenameEvent:
    """Event emitted during bulk renaming."""

    type: RenameEventType
    address: int = 0
    current_name: str = ""
    new_name: str = ""
    error: str = ""
    completed: int = 0
    total: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class _QuickProgressState:
    """Thread-safe shared counters for the quick-mode sub-batch workers.

    Each worker thread calls :meth:`next_batch_num` for its 1-based ordinal
    and :meth:`add_completed` to fold its job count into the running total.
    The lock keeps both operations consistent across the pool.
    """

    __slots__ = ("_batch_counter", "_completed", "_lock", "total")

    def __init__(self, total: int) -> None:
        self.total = total
        self._completed = 0
        self._batch_counter = 0
        self._lock = threading.Lock()

    def next_batch_num(self) -> int:
        with self._lock:
            self._batch_counter += 1
            return self._batch_counter

    def add_completed(self, n: int) -> int:
        with self._lock:
            self._completed += n
            return self._completed


class _DeepProgressState:
    """Thread-safe completed counter shared across the deep-mode workers.

    Each deep worker analyzes a single job, so :meth:`add_completed` is
    called with ``n=1``; the lock still guards the shared total so the
    ``BATCH_PROGRESS`` events reflect a consistent global position.
    """

    __slots__ = ("_completed", "_lock", "total")

    def __init__(self, total: int) -> None:
        self.total = total
        self._completed = 0
        self._lock = threading.Lock()

    def add_completed(self, n: int) -> int:
        with self._lock:
            self._completed += n
            return self._completed


class BulkRenamerEngine:
    """Bulk function renaming with quick and deep analysis modes.

    Quick mode: batch N functions into a single LLM prompt, parse results
    as lines of ``0x<addr> <name>``.

    Deep mode: spawn a SubagentRunner per function via ThreadPoolExecutor
    for thorough multi-turn analysis.
    """

    def __init__(
        self,
        provider: LLMProvider,
        tool_registry: ToolRegistry,
        config: LucNhanConfig,
        host_name: str = "IDA Pro",
        skill_registry: SkillRegistry | None = None,
        batch_size: int = 10,
        max_workers: int = 3,
        subagent_manager: Any = None,
    ) -> None:
        self._provider = provider
        self._tools = tool_registry
        self._config = config
        self._host_name = host_name
        self._skills = skill_registry
        self._batch_size = batch_size
        self._max_workers = max_workers
        self._subagent_manager = subagent_manager

        self._jobs: list[RenameJob] = []
        self._original_names: dict[int, str] = {}
        self._event_queue: queue.Queue[RenameEvent] = queue.Queue()
        self._cancel = threading.Event()
        self._paused = threading.Event()
        self._paused.set()  # not paused initially
        self._thread: threading.Thread | None = None

    def enqueue(self, jobs: list[RenameJob]) -> None:
        """Add jobs to the rename queue.

        All jobs are processed — the user's selection is the filter.
        """
        for job in jobs:
            self._jobs.append(job)
            self._original_names[job.address] = job.current_name

    def start(self, deep: bool = False, preload_on_main_thread: bool = False) -> None:
        """Start the renaming engine in a background thread.

        Parameters
        ----------
        deep : bool
            If True, use deep analysis mode (one subagent per function).
            If False, use quick mode (batch prompting).
        preload_on_main_thread : bool
            If True, run decompilation synchronously on the main thread
            (via QTimer.singleShot) before the worker thread starts.
            Use this when the host (e.g. IDA Pro) requires all API calls
            on the main thread. The widget calls this with preload=True
            and waits for completion before starting the worker pool.
        """
        self._cancel.clear()
        self._paused.set()
        if deep:
            target = self._run_deep_preloaded if preload_on_main_thread else self._run_deep
        else:
            target = self._run_quick
        self._thread = threading.Thread(
            target=target,
            daemon=True,
            name="lucnhan-bulk-renamer",
        )
        self._thread.start()
        log_info(f"Bulk renamer started: {len(self._pending_jobs())} jobs, mode={'deep' if deep else 'quick'}")

    def poll_event(self) -> RenameEvent | None:
        """Non-blocking poll for the next rename event."""
        try:
            return self._event_queue.get_nowait()
        except queue.Empty:
            return None

    def preload_decompilation(self) -> int:
        """Preload decompilation for all pending jobs synchronously on the main thread.

        Returns the number of jobs successfully decompiled. Use this before calling
        ``start(preload_on_main_thread=True)`` so worker threads never call host APIs.

        Uses ``execute_current_thread`` to avoid the thread-pool + idasync
        deadlock: when called from the IDA main thread (via QTimer.singleShot),
        dispatching to the thread pool would cause the pool worker to wait for
        ``ida_kernwin.execute_sync`` while this thread blocks on the future.
        """
        pending = self._pending_jobs()
        decompiled_count = 0
        for job in pending:
            if self._cancel.is_set():
                break
            self._paused.wait()
            if self._cancel.is_set():
                break

            job.status = RenameStatus.DECOMPILING
            self._event_queue.put(
                RenameEvent(
                    type=RenameEventType.JOB_STARTED,
                    address=job.address,
                    current_name=job.current_name,
                )
            )
            try:
                decomp = self._tools.execute_current_thread(
                    "decompile_function",
                    {"address": f"0x{job.address:x}"},
                )
                job.decompiled_code = decomp

                disasm = ""
                try:
                    disasm = self._tools.execute_current_thread(
                        "read_disassembly",
                        {"address": f"0x{job.address:x}", "count": 100},
                    )
                except Exception as disasm_err:
                    log_debug(f"Preload disassembly failed for 0x{job.address:x}: {disasm_err}")

                if disasm:
                    job.decompiled_code += f"\n\n---\n// Disassembly:\n{disasm}"
                decompiled_count += 1
            except Exception as e:
                log_error(f"Preload decompile failed for 0x{job.address:x}: {e}")
                job.status = RenameStatus.FAILED
                job.error = str(e)
                self._event_queue.put(
                    RenameEvent(
                        type=RenameEventType.JOB_ERROR,
                        address=job.address,
                        current_name=job.current_name,
                        error=str(e),
                    )
                )
                if self._cancel.is_set():
                    break
        return decompiled_count

    def pause(self) -> None:
        """Pause the renaming engine."""
        self._paused.clear()
        log_info("Bulk renamer paused")

    def resume(self) -> None:
        """Resume the renaming engine."""
        self._paused.set()
        log_info("Bulk renamer resumed")

    def cancel(self) -> None:
        """Cancel the renaming engine."""
        self._cancel.set()
        self._paused.set()  # unblock if paused
        log_info("Bulk renamer cancelled")

    def undo_all(self) -> None:
        """Revert all completed renames to their original names."""
        reverted = 0
        to_undo = [j for j in self._jobs if j.status == RenameStatus.COMPLETED and j.address in self._original_names]
        total = len(to_undo)
        for job in to_undo:
            original = self._original_names[job.address]
            try:
                self._tools.execute(
                    "rename_function",
                    {"address": f"0x{job.address:x}", "new_name": original},
                )
                reverted += 1
                job.status = RenameStatus.PENDING
                job.new_name = ""
                # Emit event so UI table updates
                self._event_queue.put(
                    RenameEvent(
                        type=RenameEventType.JOB_COMPLETED,
                        address=job.address,
                        current_name=original,
                        new_name="",
                    )
                )
                log_debug(f"Reverted 0x{job.address:x} from {job.new_name!r} to {original!r}")
            except Exception as e:
                log_error(f"Failed to revert 0x{job.address:x}: {e}")
                self._event_queue.put(
                    RenameEvent(
                        type=RenameEventType.JOB_ERROR,
                        address=job.address,
                        current_name=job.current_name,
                        error=f"Undo failed: {e}",
                    )
                )
        # Signal completion — reset progress
        self._event_queue.put(RenameEvent(type=RenameEventType.ALL_DONE, completed=0, total=total))
        log_info(f"Undo complete: reverted {reverted}/{total} renames")

    @staticmethod
    def should_skip(name: str) -> bool:
        """Return True if the name looks auto-generated and should be renamed.

        Note: returns True for names that *should* be processed (i.e., are
        auto-generated), False for names that look human-assigned.
        """
        for pattern in _AUTO_NAME_PATTERNS:
            if pattern.match(name):
                return True
        return False

    # -- Internal helpers ------------------------------------------------------

    def _pending_jobs(self) -> list[RenameJob]:
        """Return jobs that are still pending."""
        return [j for j in self._jobs if j.status == RenameStatus.PENDING]

    # Decompiled code averages ~2 chars/token (hex, operators, short names).
    # Target ≤100k tokens to stay well under the 200k API limit.
    _MAX_PROMPT_CHARS = 180_000  # ~90k tokens at 2 chars/tok
    _MAX_FUNC_CHARS = 6_000  # cap per-function (decomp + disasm combined)

    def _run_quick(self) -> None:
        """Quick mode: parallel sub-batches, each with a clean LLM context.

        When jobs already have `decompiled_code` set (preloaded via
        ``preload_decompilation()``), decompilation is skipped and
        workers only run the LLM call + rename.
        """
        pending = self._pending_jobs()
        total = len(pending)

        # Phase 1 — decompile (or reuse preloaded) each pending job.
        decompiled = self._quick_decompile_jobs(pending)
        if not decompiled:
            self._event_queue.put(RenameEvent(type=RenameEventType.ALL_DONE, completed=0, total=total))
            return

        # Phase 2 — bin-pack into context-window-sized sub-batches.
        sub_batches = self._quick_split_batches(decompiled)
        log_info(
            f"Quick mode: {len(decompiled)} functions -> {len(sub_batches)} sub-batches, "
            f"max_workers={self._max_workers}"
        )

        # Phase 3 — run sub-batches in parallel, sharing progress counters.
        state = _QuickProgressState(total=total)
        with ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="lucnhan-quick-rename",
        ) as executor:
            futures = [executor.submit(self._quick_run_one_batch, sub, state) for sub in sub_batches]
            for future in futures:
                try:
                    future.result()
                except Exception as e:
                    log_error(f"Quick rename worker error: {e}")

        # Count all finished jobs (completed + failed + skipped) so progress
        # reaches 100% and the Start button re-enables.
        _FINISHED = {RenameStatus.COMPLETED, RenameStatus.FAILED, RenameStatus.SKIPPED}
        finished = sum(1 for j in self._jobs if j.status in _FINISHED)
        self._event_queue.put(
            RenameEvent(
                type=RenameEventType.ALL_DONE,
                completed=finished,
                total=total,
            )
        )
        log_info("Bulk renamer quick mode finished")

    def _quick_decompile_jobs(self, pending: list[RenameJob]) -> list[tuple[RenameJob, str]]:
        """Phase 1: decompile each pending job into a ``(job, part_text)`` tuple.

        Jobs whose ``decompiled_code`` was already populated by
        ``preload_decompilation()`` skip the tool calls entirely.
        Decompile failures mark the job FAILED and emit a JOB_ERROR event
        rather than aborting the whole run.
        """
        decompiled: list[tuple[RenameJob, str]] = []
        for job in pending:
            if self._cancel.is_set():
                break
            self._paused.wait()
            if self._cancel.is_set():
                break

            # Skip if already preloaded by preload_decompilation()
            if job.decompiled_code:
                decompiled.append((job, self._quick_render_part(job, job.decompiled_code, "")))
                continue

            job.status = RenameStatus.DECOMPILING
            self._event_queue.put(
                RenameEvent(
                    type=RenameEventType.JOB_STARTED,
                    address=job.address,
                    current_name=job.current_name,
                )
            )
            try:
                decomp = self._tools.execute(
                    "decompile_function",
                    {"address": f"0x{job.address:x}"},
                )
                job.decompiled_code = decomp

                disasm = ""
                try:
                    disasm = self._tools.execute(
                        "read_disassembly",
                        {"address": f"0x{job.address:x}", "count": 100},
                    )
                except Exception as e:
                    # Best-effort disassembly fetch — render proceeds without it.
                    disasm = ""
                    log_debug(f"bulk_renamer read_disassembly 0x{job.address:x} failed: {e}")

                decompiled.append((job, self._quick_render_part(job, decomp, disasm)))
            except Exception as e:
                job.status = RenameStatus.FAILED
                job.error = str(e)
                self._event_queue.put(
                    RenameEvent(
                        type=RenameEventType.JOB_ERROR,
                        address=job.address,
                        current_name=job.current_name,
                        error=str(e),
                    )
                )
                log_error(f"Decompile failed for 0x{job.address:x}: {e}")
        return decompiled

    @staticmethod
    def _quick_render_part(job: RenameJob, decomp: str, disasm: str) -> str:
        """Render a function's decompilation (+optional disasm) as a prompt part.

        Capped at ``_MAX_FUNC_CHARS`` so a single huge function can't crowd
        out the rest of its sub-batch.
        """
        part = f"// Function at 0x{job.address:x} (current name: {job.current_name})\n"
        part += f"// Decompiled:\n{decomp}\n"
        if disasm:
            part += f"// Disassembly:\n{disasm}\n"
        if len(part) > BulkRenamerEngine._MAX_FUNC_CHARS:
            part = part[: BulkRenamerEngine._MAX_FUNC_CHARS] + "\n// ... (truncated)\n"
        return part

    def _quick_split_batches(self, decompiled: list[tuple[RenameJob, str]]) -> list[list[tuple[RenameJob, str]]]:
        """Phase 2: bin-pack decompiled jobs into context-window-sized sub-batches.

        Starts a new sub-batch whenever adding the next part would exceed
        ``_MAX_PROMPT_CHARS`` or ``_batch_size`` functions.
        """
        sub_batches: list[list[tuple[RenameJob, str]]] = []
        current_sub: list[tuple[RenameJob, str]] = []
        current_chars = len(QUICK_ANALYSIS_PROMPT)
        funcs_in_sub = 0
        for item in decompiled:
            part_len = len(item[1])
            would_overflow = current_sub and (current_chars + part_len) > self._MAX_PROMPT_CHARS
            batch_full = current_sub and funcs_in_sub >= self._batch_size
            if would_overflow or batch_full:
                sub_batches.append(current_sub)
                current_sub = []
                current_chars = len(QUICK_ANALYSIS_PROMPT)
                funcs_in_sub = 0
            current_sub.append(item)
            current_chars += part_len
            funcs_in_sub += 1
        if current_sub:
            sub_batches.append(current_sub)
        return sub_batches

    def _quick_run_one_batch(
        self,
        sub: list[tuple[RenameJob, str]],
        state: _QuickProgressState,
    ) -> None:
        """Phase 3 worker: run one sub-batch's LLM call + apply results.

        Shares ``state`` (counters + lock) across worker threads so the
        progress events reflect the global batch position. Honours cancel
        and pause before each batch.
        """
        if self._cancel.is_set():
            return
        self._paused.wait()
        if self._cancel.is_set():
            return

        mgr = self._subagent_manager
        num = state.next_batch_num()
        sub_jobs = [item[0] for item in sub]
        sub_parts = [item[1] for item in sub]

        # Register agent for tracking
        agent_id = None
        if mgr is not None:
            addrs = ", ".join(f"0x{j.address:x}" for j in sub_jobs[:3])
            suffix = f" +{len(sub_jobs) - 3} more" if len(sub_jobs) > 3 else ""
            agent_id = mgr.register(
                name=f"quick_batch_{num}",
                task=f"Quick batch {num} ({len(sub_jobs)} funcs): {addrs}{suffix}",
                agent_type="bulk_rename(quick)",
                category="bulk_rename",
            )
            self._update_mgr_agent(agent_id, "running", "", 0)

        # Fresh single-message prompt — clean context, no history
        full_prompt = QUICK_ANALYSIS_PROMPT + "\n".join(sub_parts)
        log_info(
            f"Quick batch {num}: {len(sub_jobs)} funcs, {len(full_prompt)} chars (~{len(full_prompt) // 2}tok est)"
        )
        for job in sub_jobs:
            job.status = RenameStatus.ANALYZING

        try:
            names = self._quick_llm_call(full_prompt)
            self._apply_quick_results(sub_jobs, names, agent_id)
            renamed_count = sum(1 for j in sub_jobs if j.status == RenameStatus.COMPLETED)
            summary = f"Renamed {renamed_count}/{len(sub_jobs)} functions"
            self._update_mgr_agent(agent_id, "completed", summary, len(sub_jobs))
        except Exception as e:
            for job in sub_jobs:
                job.status = RenameStatus.FAILED
                job.error = str(e)
                self._event_queue.put(
                    RenameEvent(
                        type=RenameEventType.JOB_ERROR,
                        address=job.address,
                        current_name=job.current_name,
                        error=str(e),
                    )
                )
            log_error(f"Quick analysis batch {num} failed: {e}")
            self._update_mgr_agent(agent_id, "failed", str(e), 1)

        completed = state.add_completed(len(sub_jobs))
        self._event_queue.put(
            RenameEvent(
                type=RenameEventType.BATCH_PROGRESS,
                completed=completed,
                total=state.total,
            )
        )

    def _quick_llm_call(self, prompt: str) -> dict[int, str]:
        """Call the LLM with a batch prompt and parse address-name pairs."""
        messages = [Message(role=Role.USER, content=prompt)]
        response = self._provider.chat(
            messages=messages,
            tools=None,
            temperature=0.2,
            max_tokens=4096,
        )

        results: dict[int, str] = {}
        addr_pattern = re.compile(r"^0x([0-9a-fA-F]+)\s+(\S+)$")
        for line in response.content.strip().splitlines():
            line = line.strip()
            match = addr_pattern.match(line)
            if match:
                addr = int(match.group(1), 16)
                name = match.group(2)
                results[addr] = name

        return results

    def _apply_quick_results(
        self,
        jobs: list[RenameJob],
        names: dict[int, str],
        agent_id: str | None = None,
    ) -> None:
        """Apply parsed rename results to jobs."""
        applied = 0
        for job in jobs:
            new_name = names.get(job.address)
            if not new_name:
                job.status = RenameStatus.FAILED
                job.error = "No name suggested by LLM"
                self._event_queue.put(
                    RenameEvent(
                        type=RenameEventType.JOB_ERROR,
                        address=job.address,
                        current_name=job.current_name,
                        error=job.error,
                    )
                )
                continue

            job.new_name = new_name
            job.status = RenameStatus.RENAMING
            try:
                self._tools.execute(
                    "rename_function",
                    {"address": f"0x{job.address:x}", "new_name": new_name},
                )
                job.status = RenameStatus.COMPLETED
                self._event_queue.put(
                    RenameEvent(
                        type=RenameEventType.JOB_COMPLETED,
                        address=job.address,
                        current_name=job.current_name,
                        new_name=new_name,
                    )
                )
                applied += 1
                self._update_mgr_agent(agent_id, "running", f"{applied}/{len(jobs)}", applied)
                log_debug(f"Renamed 0x{job.address:x}: {job.current_name!r} -> {new_name!r}")
            except Exception as e:
                job.status = RenameStatus.FAILED
                job.error = str(e)
                self._event_queue.put(
                    RenameEvent(
                        type=RenameEventType.JOB_ERROR,
                        address=job.address,
                        current_name=job.current_name,
                        error=str(e),
                    )
                )
                applied += 1
                self._update_mgr_agent(agent_id, "running", f"{applied}/{len(jobs)}", applied)
                log_error(f"Rename failed for 0x{job.address:x}: {e}")

    def _run_deep(self) -> None:
        """Deep mode: one SubagentRunner per function via ThreadPoolExecutor.

        Each worker decompiles its job, runs the subagent analysis, parses
        the ``RENAME:`` line, and applies the rename. When a
        SubagentManager is provided, each per-function agent is registered
        there so it appears in the Agents tab.
        """
        self._run_deep_common(decompile_in_worker=True)

    def _run_deep_preloaded(self) -> None:
        """Deep mode with preloaded decompilation — workers never call host APIs.

        Decompilation was done synchronously on the main thread via
        ``preload_decompilation()`` before this method is called.
        Workers only run the subagent analysis + rename.
        """
        self._run_deep_common(decompile_in_worker=False)

    def _run_deep_common(self, *, decompile_in_worker: bool) -> None:
        """Shared deep-mode loop for both worker-decompile and preloaded paths.

        When *decompile_in_worker* is False, jobs are assumed to already
        carry ``decompiled_code`` from a main-thread preload, so the
        worker skips straight to analysis and never touches host APIs.
        """
        from .subagent import SubagentRunner  # deferred to avoid circular import

        pending = self._pending_jobs()
        total = len(pending)
        state = _DeepProgressState(total=total)
        rename_pattern = re.compile(r"RENAME:\s*0x([0-9a-fA-F]+)\s+(\S+)")

        with ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="lucnhan-deep-rename",
        ) as executor:
            futures = [
                executor.submit(
                    self._deep_analyze_job,
                    job,
                    SubagentRunner,
                    rename_pattern,
                    state,
                    decompile_in_worker,
                )
                for job in pending
            ]
            for future in futures:
                try:
                    future.result()
                except Exception as e:
                    log_error(f"Deep rename worker error: {e}")

        _FINISHED = {RenameStatus.COMPLETED, RenameStatus.FAILED, RenameStatus.SKIPPED}
        finished = sum(1 for j in self._jobs if j.status in _FINISHED)
        self._event_queue.put(
            RenameEvent(
                type=RenameEventType.ALL_DONE,
                completed=finished,
                total=total,
            )
        )
        if decompile_in_worker:
            log_info("Bulk renamer deep mode finished")
        else:
            log_info("Bulk renamer deep mode (preloaded) finished")

    def _deep_analyze_job(
        self,
        job: RenameJob,
        SubagentRunner: Any,
        rename_pattern: re.Pattern[str],
        state: _DeepProgressState,
        decompile_in_worker: bool,
    ) -> None:
        """Analyze one job deeply: decompile (optional) -> subagent -> rename.

        The *decompile_in_worker* flag is the only behavioural fork between
        the worker-decompile and preloaded paths: when False, the host
        ``decompile_function`` call is skipped because the main thread
        already populated ``job.decompiled_code``.
        """
        mgr = self._subagent_manager

        if self._cancel.is_set():
            return

        self._paused.wait()
        if self._cancel.is_set():
            return

        agent_name = f"agent_{job.current_name}"

        # Register with SubagentManager for display (no background thread)
        agent_id = None
        if mgr is not None:
            agent_id = mgr.register(
                name=agent_name,
                task=f"Deep rename analysis for {job.current_name} at 0x{job.address:x}",
                agent_type="bulk_rename(deep)",
                category="bulk_rename",
            )

        if decompile_in_worker:
            job.status = RenameStatus.DECOMPILING
            self._event_queue.put(
                RenameEvent(
                    type=RenameEventType.JOB_STARTED,
                    address=job.address,
                    current_name=job.current_name,
                )
            )

            # Decompile first
            try:
                result = self._tools.execute(
                    "decompile_function",
                    {"address": f"0x{job.address:x}"},
                )
                job.decompiled_code = result
            except Exception as e:
                job.status = RenameStatus.FAILED
                job.error = str(e)
                self._event_queue.put(
                    RenameEvent(
                        type=RenameEventType.JOB_ERROR,
                        address=job.address,
                        current_name=job.current_name,
                        error=str(e),
                    )
                )
                self._update_mgr_agent(agent_id, "failed", str(e), 0)
                return
        else:
            # Preloaded: decompile already done on main thread; still emit
            # JOB_STARTED so the UI shows the job entering analysis.
            self._event_queue.put(
                RenameEvent(
                    type=RenameEventType.JOB_STARTED,
                    address=job.address,
                    current_name=job.current_name,
                )
            )

        # Run deep analysis via subagent
        job.status = RenameStatus.ANALYZING
        task = (
            f"{DEEP_ANALYSIS_PROMPT}\n"
            f"// Function at 0x{job.address:x} (current name: {job.current_name})\n"
            f"{job.decompiled_code}"
        )

        # Mark running in the manager
        self._update_mgr_agent(agent_id, "running", "", 0)

        runner = SubagentRunner(
            provider=self._provider,
            tool_registry=self._tools,
            config=self._config,
            host_name=self._host_name,
            skill_registry=self._skills,
        )

        final_text = ""
        turn_count = 0
        try:
            for event in runner.run_task(task, max_turns=10):
                if self._cancel.is_set():
                    self._update_mgr_agent(agent_id, "cancelled", "Cancelled", turn_count)
                    return
                if event.type.value == "turn_end":
                    turn_count += 1
                if event.type.value == "text_done" and event.text:
                    final_text = event.text
        except Exception as e:
            job.status = RenameStatus.FAILED
            job.error = str(e)
            self._event_queue.put(
                RenameEvent(
                    type=RenameEventType.JOB_ERROR,
                    address=job.address,
                    current_name=job.current_name,
                    error=str(e),
                )
            )
            self._update_mgr_agent(agent_id, "failed", str(e), turn_count)
            return

        # Parse the RENAME line from the subagent output
        match = rename_pattern.search(final_text)
        if not match:
            job.status = RenameStatus.FAILED
            job.error = "No RENAME line found in subagent output"
            self._event_queue.put(
                RenameEvent(
                    type=RenameEventType.JOB_ERROR,
                    address=job.address,
                    current_name=job.current_name,
                    error=job.error,
                )
            )
            self._update_mgr_agent(agent_id, "failed", job.error, turn_count)
            return

        new_name = match.group(2)
        job.new_name = new_name
        job.status = RenameStatus.RENAMING

        try:
            self._tools.execute(
                "rename_function",
                {"address": f"0x{job.address:x}", "new_name": new_name},
            )
            job.status = RenameStatus.COMPLETED
            self._event_queue.put(
                RenameEvent(
                    type=RenameEventType.JOB_COMPLETED,
                    address=job.address,
                    current_name=job.current_name,
                    new_name=new_name,
                )
            )
            log_debug(f"Deep renamed 0x{job.address:x}: {job.current_name!r} -> {new_name!r}")
            self._update_mgr_agent(agent_id, "completed", f"Renamed to {new_name}", turn_count)
        except Exception as e:
            job.status = RenameStatus.FAILED
            job.error = str(e)
            self._event_queue.put(
                RenameEvent(
                    type=RenameEventType.JOB_ERROR,
                    address=job.address,
                    current_name=job.current_name,
                    error=str(e),
                )
            )
            log_error(f"Deep rename failed for 0x{job.address:x}: {e}")
            self._update_mgr_agent(agent_id, "failed", str(e), turn_count)

        completed = state.add_completed(1)
        self._event_queue.put(
            RenameEvent(
                type=RenameEventType.BATCH_PROGRESS,
                completed=completed,
                total=state.total,
            )
        )

    def _update_mgr_agent(
        self,
        agent_id: str | None,
        status: str,
        summary: str,
        turn_count: int,
    ) -> None:
        """Update an externally registered agent in the SubagentManager."""
        if agent_id is None or self._subagent_manager is None:
            return

        from .subagent_manager import SubagentStatus

        status_map = {
            "running": SubagentStatus.RUNNING,
            "completed": SubagentStatus.COMPLETED,
            "failed": SubagentStatus.FAILED,
            "cancelled": SubagentStatus.CANCELLED,
        }
        mapped = status_map.get(status)
        if mapped is not None:
            self._subagent_manager.update_external(agent_id, mapped, summary=summary, turn_count=turn_count)
