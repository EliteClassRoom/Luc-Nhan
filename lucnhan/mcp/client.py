"""MCP client: manages a single MCP server subprocess using the official mcp SDK.

The official ``mcp`` SDK is only imported when an MCP server is actually
starting — importing this module alone does not pull in the SDK.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from typing import Any

from ..constants import MCP_DEFAULT_TIMEOUT
from ..core.errors import MCPConnectionError, MCPError, MCPTimeoutError
from ..core.logging import log_debug, log_error, log_info
from ..core.sanitize import sanitize_mcp_result
from .config import MCPServerConfig
from .protocol import MCPToolSchema

_HAS_MCP: bool | None = None  # None = not yet probed


def _check_mcp_sdk() -> bool:
    """Probe for the official ``mcp`` SDK. Cached after first check."""
    global _HAS_MCP
    if _HAS_MCP is None:
        try:
            import mcp
            import mcp.client.stdio  # noqa: F401

            _HAS_MCP = True
        except ImportError:
            _HAS_MCP = False
    return _HAS_MCP


def reset_mcp_sdk_probe() -> None:
    """Clear the cached MCP SDK probe so it is re-tested on next access.

    Call during MCP config reload (e.g. after user installed the SDK)
    so a previously negative probe does not block forever.
    """
    global _HAS_MCP
    _HAS_MCP = None


def _child_map() -> tuple[dict[int, list[int]], dict[int, str]]:
    """Map processes to their children plus a pid -> image-name table.

    Returns ``({}, {})`` when the platform gives us nothing to work with.
    """
    rows: list[tuple[int, int, str]] = []
    try:
        if sys.platform == "win32":
            rows = _win_process_pairs()
        else:
            import subprocess

            out = subprocess.run(
                ["ps", "-eo", "pid=,ppid=,comm="],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout
            for line in out.splitlines():
                parts = line.split(maxsplit=2)
                if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                    rows.append((int(parts[0]), int(parts[1]), parts[2]))
    except Exception:  # best effort - stop() must not fail here
        return {}, {}

    children: dict[int, list[int]] = {}
    images: dict[int, str] = {}
    for pid, ppid, image in rows:
        children.setdefault(ppid, []).append(pid)
        images[pid] = image
    return children, images


def _win_process_pairs() -> list[tuple[int, int, str]]:
    """(pid, ppid, exe) rows from a Toolhelp snapshot; no child process needed."""
    import ctypes
    from ctypes import wintypes

    class ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snapshot == -1:
        return []
    rows: list[tuple[int, int, str]] = []
    entry = ProcessEntry32()
    entry.dwSize = ctypes.sizeof(ProcessEntry32)
    try:
        ok = kernel32.Process32First(snapshot, ctypes.byref(entry))
        while ok:
            exe = entry.szExeFile.decode("mbcs", "replace") if entry.szExeFile else ""
            rows.append((entry.th32ProcessID, entry.th32ParentProcessID, exe))
            ok = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return rows


def _descendants(root: int) -> list[int]:
    """All descendants of *root*."""
    return _descendants_in(_child_map()[0], root)


def _descendants_in(children: dict[int, list[int]], root: int) -> list[int]:
    """All descendants of *root* inside an already-snapshotted child map."""
    found: list[int] = []
    pending = list(children.get(root, []))
    while pending:
        pid = pending.pop()
        found.append(pid)
        pending.extend(children.get(pid, []))
    return found


def _same_image(candidate: str, expected: str) -> bool:
    """True when *candidate* names the executable in *expected*."""
    return os.path.basename(candidate).lower().removesuffix(".exe") == os.path.basename(expected).lower().removesuffix(
        ".exe"
    )


def _terminate_pids(pids: list[int], force: bool) -> None:
    """Terminate *pids*; ``force`` escalates to SIGKILL / ``taskkill /F``."""
    if not pids:
        return
    if sys.platform == "win32":
        import subprocess

        # taskkill needs one /PID flag per process
        args = ["taskkill", "/T"]
        for pid in pids:
            args += ["/PID", str(pid)]
        if force:
            args.insert(1, "/F")
        subprocess.run(args, capture_output=True, timeout=10, check=False)
        return

    import signal

    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        except OSError:
            pass


def _kill_leftovers(before: set[int], expect: str = "") -> None:
    """Kill children spawned after *before* was snapshotted (descendants included).

    Last resort for a shutdown that ``join()`` could not complete: the SDK's
    own ``finally`` block is stuck on the wedged loop, so nothing else will.

    *expect* (the configured command) narrows the candidates to processes whose
    image matches it: ``MCPManager.start_servers`` starts servers in parallel,
    so the unfiltered "every new child of IDA" set also contains healthy
    servers that are still handshaking. Wrapper commands (``npx``/``uvx``)
    never match on image name — their child is ``node``/``python`` — so an
    empty match set deliberately falls back to the unfiltered list, and that
    residual race stays.
    """
    children, images = _child_map()
    leftover = [pid for pid in _descendants_in(children, os.getpid()) if pid not in before]
    if not leftover:
        return
    if expect:
        matched: set[int] = {pid for pid in leftover if _same_image(images.get(pid, ""), expect)}
        if matched:
            for pid in list(matched):
                matched.update(_descendants_in(children, pid))
            leftover = [pid for pid in leftover if pid in matched]
        else:
            log_debug(f"MCP: no image match for {expect!r}, killing all new descendants")
    log_debug(f"MCP: killing leftover server processes {leftover}")
    # Windows taskkill without /F only posts to a window, which a console
    # server may not have; servers keep no unsaved state, so force there.
    if sys.platform == "win32":
        _terminate_pids(leftover, force=True)
        return
    _terminate_pids(leftover, force=False)
    for _ in range(10):
        alive = set(_descendants(os.getpid()))
        if not alive.intersection(leftover):
            return
        threading.Event().wait(0.1)
    _terminate_pids(leftover, force=True)


def _unwrap_exception(exc: BaseException) -> str:
    """Extract a human-readable message from potentially nested ExceptionGroups.

    anyio's TaskGroup wraps errors in ExceptionGroup / BaseExceptionGroup,
    hiding the actual cause behind "unhandled errors in a TaskGroup (N sub-exception)".
    """
    # Python 3.11+ ExceptionGroup
    if sys.version_info >= (3, 11) and isinstance(exc, BaseExceptionGroup):
        parts = []
        for sub in exc.exceptions:
            parts.append(_unwrap_exception(sub))
        return "; ".join(parts)
    return str(exc)


def _safe_errlog():
    """Return a file-like object usable as subprocess stderr.

    IDA replaces ``sys.stderr`` with custom objects
    (``IDAPythonStdOut``) that lack ``fileno()``.  ``anyio.open_process()``
    passes ``stderr=`` directly to the OS, which requires a real fd.
    Fall back to opening ``/dev/null`` when the host's stderr is not a
    real file descriptor.
    """
    try:
        sys.stderr.fileno()
        return sys.stderr
    except (AttributeError, OSError):
        # Open /dev/null as a real file descriptor that anyio can use.
        # We return the file object (not subprocess.DEVNULL) because the
        # MCP SDK types errlog as TextIO.
        return open(os.devnull, "w", encoding="utf-8", errors="replace")


class MCPClient:
    """Client for a single MCP server process.

    Uses the official ``mcp`` Python SDK (``ClientSession`` + ``stdio_client``)
    running in a dedicated background thread with its own asyncio event loop.
    All public methods are synchronous — they dispatch to the async loop and
    block until the result is ready.
    """

    def __init__(self, config: MCPServerConfig):
        # SDK presence is checked in start(), not here, so that constructing
        # an MCPClient does not permanently cache a negative probe.
        self.config = config
        self.name = config.name
        self._tools: list[MCPToolSchema] = []
        self._started = False
        self._healthy = True
        self._running = False

        # Async internals — set up in start()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: Any = None  # ClientSession, imported in _async_main
        self._shutdown_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._start_error: str | None = None
        # Child PIDs present before we spawn, so a wedged stop() can kill
        # exactly what this client started and nothing else.
        self._pre_start_pids: set[int] | None = None

    @property
    def is_running(self) -> bool:
        return self._started and self._running and self._thread is not None and self._thread.is_alive()

    @property
    def is_healthy(self) -> bool:
        return self.is_running and self._healthy

    def start(self, timeout: float = 30.0) -> None:
        """Spawn the MCP server process, perform handshake, and discover tools.

        Blocks until the server is ready or the timeout expires.
        """
        if not _check_mcp_sdk():
            raise MCPError("The 'mcp' package is required for MCP support. Install it with: pip install mcp")

        log_info(f"MCP[{self.name}]: starting server: {self.config.command} {self.config.args}")

        self._running = True
        self._ready.clear()
        self._start_error = None
        self._pre_start_pids = set(_descendants(os.getpid()))

        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=f"mcp-{self.name}",
        )
        self._thread.start()

        # Wait for the async loop to finish initialization
        if not self._ready.wait(timeout=timeout):
            self._running = False
            self._kill_leftovers_if_stuck()
            raise MCPConnectionError(f"MCP[{self.name}]: initialize timed out after {timeout}s")

        if self._start_error:
            self._running = False
            self._kill_leftovers_if_stuck()
            raise MCPConnectionError(f"MCP[{self.name}]: handshake failed: {self._start_error}")

        self._started = True
        log_info(f"MCP[{self.name}]: started OK, {len(self._tools)} tools registered")

    def stop(self) -> None:
        """Shut down the MCP server process."""
        log_debug(f"MCP[{self.name}]: stopping")
        self._running = False
        self._started = False

        # Signal the async loop to exit
        if self._loop and self._shutdown_event and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._shutdown_event.set)

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
            self._kill_leftovers_if_stuck()

        self._session = None
        self._loop = None
        self._thread = None

    def _kill_leftovers_if_stuck(self) -> None:
        """Kill the server tree when the loop thread cannot finish on its own.

        The SDK terminates the tree from a ``finally`` block, which never runs
        while the event loop is wedged (a hung handshake, a full pipe). Without
        this the orphaned server outlives the client and the restart collides
        with it.
        """
        if self._pre_start_pids is None or not (self._thread and self._thread.is_alive()):
            return
        _kill_leftovers(self._pre_start_pids, self.config.command)
        self._thread = None

    def get_tools(self) -> list[MCPToolSchema]:
        return list(self._tools)

    def call_tool(self, name: str, arguments: dict[str, Any], timeout: float = MCP_DEFAULT_TIMEOUT) -> str:
        """Call an MCP tool and return the result as a string."""
        log_debug(f"MCP[{self.name}]: calling tool {name}")

        try:
            result = self._run_coro(self._async_call_tool(name, arguments), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except MCPTimeoutError:
            raise
        except MCPError:
            raise
        except Exception as e:
            raise MCPError(f"MCP tool {name} error: {e}") from e

        return result

    # ------------------------------------------------------------------
    # Async internals
    # ------------------------------------------------------------------

    def _run_coro(self, coro, timeout: float = MCP_DEFAULT_TIMEOUT) -> Any:
        """Submit a coroutine to the background loop and block for the result."""
        if not self._loop or self._loop.is_closed():
            raise MCPConnectionError(f"MCP[{self.name}]: not connected")

        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise MCPTimeoutError(f"MCP[{self.name}]: operation timed out after {timeout}s") from None

    async def _async_call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Call an MCP tool via the ClientSession."""
        if not self._session:
            raise MCPConnectionError(f"MCP[{self.name}]: no active session")

        result = await self._session.call_tool(name, arguments)

        # Extract text content
        parts = []
        if result.content:
            for item in result.content:
                if hasattr(item, "text"):
                    parts.append(item.text)
                else:
                    parts.append(str(item))
        raw = "\n".join(parts) if parts else str(result)

        # MCP results are from external servers — sanitize before they
        # enter the conversation to mitigate prompt injection.
        return sanitize_mcp_result(raw, server_name=self.name, tool_name=name)

    def _run_loop(self) -> None:
        """Background thread: run the asyncio event loop with the MCP session."""
        try:
            asyncio.run(self._async_main())
        except BaseException as e:
            msg = _unwrap_exception(e)
            if self._running:
                import traceback

                log_error(f"MCP[{self.name}]: event loop error: {msg}")
                log_debug(f"MCP[{self.name}]: traceback:\n{traceback.format_exc()}")
            if not self._ready.is_set():
                self._start_error = msg
                self._ready.set()
        finally:
            self._running = False

    async def _async_main(self) -> None:
        """Async entry point: connect, handshake, discover tools, then keep alive."""
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        self._loop = asyncio.get_running_loop()
        self._shutdown_event = asyncio.Event()

        server_params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            # Merge with the inherited environment: passing only the configured
            # mapping drops PATH/SYSTEMROOT, so ``command`` no longer resolves.
            env={**os.environ, **self.config.env} if self.config.env else None,
        )

        # Handshake is bounded by the per-server configured timeout — the
        # same value the manager uses for its outer wait — so the manager
        # never abandons a handshake that is still within its own bound.
        handshake_timeout = self.config.resolve_timeout(MCP_DEFAULT_TIMEOUT)

        errlog = _safe_errlog()
        try:
            async with stdio_client(server_params, errlog=errlog) as (
                read_stream,
                write_stream,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    self._session = session

                    # Initialize handshake (bounded by timeout)
                    init_result = await asyncio.wait_for(
                        session.initialize(),
                        timeout=handshake_timeout,
                    )
                    server_info = getattr(init_result, "server_info", None) or getattr(init_result, "serverInfo", None)
                    log_debug(f"MCP[{self.name}]: initialized, server: {server_info}")

                    # Discover tools (bounded by timeout)
                    tools_result = await asyncio.wait_for(
                        session.list_tools(),
                        timeout=handshake_timeout,
                    )
                    self._tools = []
                    for t in tools_result.tools:
                        annotations = getattr(t, "annotations", None)
                        self._tools.append(
                            MCPToolSchema(
                                name=t.name,
                                description=t.description or "",
                                input_schema=t.inputSchema if hasattr(t, "inputSchema") else {},
                                read_only_hint=bool(getattr(annotations, "readOnlyHint", False)),
                                destructive_hint=bool(getattr(annotations, "destructiveHint", True)),
                            )
                        )
                    log_info(f"MCP[{self.name}]: discovered {len(self._tools)} tools")

                    # Signal that initialization is complete
                    self._ready.set()

                    # Keep the session alive until stop() is called
                    await self._shutdown_event.wait()

        except BaseException as e:
            msg = _unwrap_exception(e)
            if not self._ready.is_set():
                self._start_error = msg
                self._ready.set()
            else:
                log_error(f"MCP[{self.name}]: session error: {msg}")
        finally:
            # Close the devnull fd if we opened one (not sys.stderr)
            if errlog is not sys.stderr:
                try:
                    errlog.close()
                except OSError as exc:
                    log_debug(f"MCP[{self.name}]: errlog close failed: {exc}")
