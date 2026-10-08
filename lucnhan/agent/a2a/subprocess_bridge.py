"""Subprocess bridge for CLI-based external agents (Claude Code, Codex, etc.)."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass

from ...core.logging import log_debug
from .types import A2AEvent, ExternalAgentConfig


def _curate_subprocess_env(agent_env: dict[str, str]) -> dict[str, str]:
    """Build the env dict passed to the subprocess for external CLIs.

    We deliberately do NOT inherit the parent's full ``os.environ``,
    because it typically contains Luc Nhan-internal secrets
    (ANTHROPIC_API_KEY, OPENAI_API_KEY, LUCNHAN_*, …) that an
    external CLI agent has no legitimate need for and which would
    otherwise leak to a third-party process.

    We pass only:
      * ``PATH`` / ``Path`` / ``PATHEXT``  — required to locate the CLI
      * ``HOME`` / ``USERPROFILE``         — config lookup
      * ``LANG`` / ``LC_ALL`` / ``LC_CTYPE`` — locale
      * ``TMP`` / ``TMPDIR`` / ``TEMP``    — temp dir
      * ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` if set — TLS verification
      * ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``NO_PROXY`` — networking
      * Anything in ``agent_env`` (caller-supplied overrides)
    """
    passthrough_keys = (
        "PATH",
        "Path",  # Windows
        "PATHEXT",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMP",
        "TMPDIR",
        "TEMP",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    )
    curated: dict[str, str] = {k: os.environ[k] for k in passthrough_keys if k in os.environ}
    curated.update(agent_env)
    return curated


_SUBPROCESS_AGENTS = {
    "claude": ["claude"],
    "codex": ["codex"],
}


def _validate_task(task: str) -> None:
    """Reject tasks that look like a CLI flag.

    A legitimate user task should never begin with ``-``. Tasks that do
    are almost certainly a prompt-injection attempt: the LLM was
    manipulated into crafting argv that flips CLI flags such as
    ``--settings '{"sandbox":false}'`` or ``--add-dir /etc``.

    Defense layers (in order):
      1. This explicit reject — fails fast with a clear error.
      2. ``--`` end-of-options separator inserted by ``_build_command``
         below, so even if validation is bypassed, the subprocess layer
         passes the task as a positional argument rather than a flag.
    """
    if not task:
        raise ValueError("SubprocessBridge task is empty")
    if task.startswith("-"):
        raise ValueError(
            f"SubprocessBridge task starts with '-', refusing to pass to subprocess as a CLI flag: {task[:80]!r}"
        )


@dataclass
class SubprocessBridge:
    """Bridge to CLI-based agents via subprocess.

    Detects available CLI agents on PATH and runs tasks via structured
    subprocess invocations with JSON output parsing.
    """

    def discover(self) -> list[ExternalAgentConfig]:
        """Auto-detect CLI agents available on PATH."""
        agents: list[ExternalAgentConfig] = []

        for agent_name, commands in _SUBPROCESS_AGENTS.items():
            cmd = commands[0]
            if shutil.which(cmd):
                agents.append(
                    ExternalAgentConfig(
                        name=agent_name,
                        transport="subprocess",
                        endpoint=cmd,
                        capabilities=self._capabilities_for(agent_name),
                    )
                )

        return agents

    def run_task(
        self,
        agent: ExternalAgentConfig,
        task: str,
        timeout: int = 300,
        cancel_event: threading.Event | None = None,
    ) -> Generator[A2AEvent, None, str]:
        """Run a task via CLI subprocess, yielding events.

        Yields events as the subprocess produces output, then yields a
        final event with the aggregated result string.

        For Claude CLI: uses ``claude --print --output-format json -- <task>``
        For Codex CLI: uses ``codex --quiet --format json -- <task>``

        The ``--`` end-of-options separator prevents the LLM-supplied
        ``task`` from being interpreted as a CLI flag, even if validation
        in ``_build_command`` is bypassed.

        Args:
            agent: External agent config (claude/codex).
            task: The user/LLM task text. Validated against argv injection.
            timeout: Maximum runtime in seconds.
            cancel_event: If set, polling this each stdout line will
                terminate the subprocess early. Use to wire to the agent
                loop's user-cancel Event.
        """
        cmd = self._build_command(agent, task)
        if cmd is None:
            yield A2AEvent(type="error", text=f"No known command for agent: {agent.name}")
            return ""

        proc: subprocess.Popen[str] | None = None
        result_lines: list[str] = []
        cancelled = False
        # Background thread that drains the subprocess's stdout
        # into ``stdout_queue``. The main thread polls the queue
        # AND the cancel_event, so cancellation is responsive
        # even when the subprocess is blocked on I/O and never
        # produces a line.
        stdout_queue: queue.Queue[str | None] = queue.Queue()

        # ``None`` is the sentinel: thread enqueues ``None`` after
        # stdout closes (process exited) to wake the main loop.
        def _drain_stdout() -> None:
            """Read stdout line by line into the queue until EOF."""
            try:
                if proc is None or proc.stdout is None:
                    return
                for line in proc.stdout:
                    stdout_queue.put(line)
            except Exception as exc:
                # Subprocess pipe may be closed mid-iteration;
                # the None sentinel handles the wakeup either way.
                log_debug(f"A2A subprocess stdout drain ended: {exc}")
            finally:
                stdout_queue.put(None)  # wake the main loop

        reader_thread: threading.Thread | None = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_curate_subprocess_env(agent.env),
                text=True,
                encoding="utf-8",
            )

            # Spawn the drain thread. ``daemon=True`` so the test
            # process can exit even if the bridge hangs.
            reader_thread = threading.Thread(target=_drain_stdout, daemon=True)
            reader_thread.start()

            deadline = time.monotonic() + timeout
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                if time.monotonic() >= deadline:
                    # Surface a timeout by killing the process;
                    # the bridge doesn't currently have a separate
                    # timeout-error event path (the kill itself
                    # is the recovery).
                    if proc.poll() is None:
                        proc.kill()
                    cancelled = True  # treat timeout as cancellation
                    yield A2AEvent(type="error", text=f"Timeout after {timeout}s")
                    break
                try:
                    line = stdout_queue.get(timeout=0.1)
                except queue.Empty:
                    continue  # re-check cancel and timeout
                if line is None:
                    # Stdout closed — process exited (or was
                    # killed). Break out and finalize.
                    break
                if not line.strip():
                    continue
                result_lines.append(line)
                yield A2AEvent(type="stdout", text=line.rstrip("\n"))

            # If the process is still alive, wait for it briefly
            # (shouldn't be needed since the kill path already
            # handled it, but be defensive).
            if not cancelled and proc.poll() is None:
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception as e:
            yield A2AEvent(type="error", text=str(e))
            return ""
        finally:
            if proc and proc.poll() is None:
                proc.kill()

        if cancelled:
            yield A2AEvent(
                type="cancelled",
                text="Task cancelled by user",
                done=True,
            )
            return ""

        # Try to parse last line as JSON
        result = ""
        for line in result_lines:
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    content = parsed.get("content", parsed.get("result", str(parsed)))
                    result = content if isinstance(content, str) else json.dumps(content)
                else:
                    result = str(parsed)
            except json.JSONDecodeError:
                result = line

        yield A2AEvent(type="completed", text=result, done=True)
        return result

    def _build_command(self, agent: ExternalAgentConfig, task: str) -> list[str] | None:
        name = agent.name.lower()
        # Validate first: refuse tasks that look like CLI flags.
        _validate_task(task)
        # Then insert '--' so the task is always a positional argument
        # even if it contains spaces or quote-like characters.
        if name == "claude":
            return ["claude", "--print", "--output-format", "json", "--", task]
        if name == "codex":
            return ["codex", "--quiet", "--format", "json", "--", task]
        return None

    @staticmethod
    def _capabilities_for(name: str) -> list[str]:
        if name == "claude":
            return ["code_generation", "research", "refactoring", "analysis"]
        if name == "codex":
            return ["code_generation", "research", "refactoring"]
        return []
