"""MCP manager: orchestrates multiple MCP server connections."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..constants import MCP_TOOL_PREFIX
from ..core.logging import log_debug, log_error, log_info, log_warning
from .config import MCPServerConfig, load_mcp_config

if TYPE_CHECKING:
    from ..tools.registry import ToolRegistry
    from .client import MCPClient
else:
    ToolRegistry = Any
    MCPClient = Any

# Soft timeout: log a warning if startup takes longer than this.
_SOFT_TIMEOUT = 5.0
# Hard timeout fallback: abort startup after this many seconds when the
# per-server "timeout" is unset or invalid (<= 0). Configured timeouts are
# honored as-is — cold-starting stdio servers (npx/uvx-based) can
# legitimately need far more than 15s to answer "initialize".
_HARD_TIMEOUT = 15.0


class MCPManager:
    """Manages multiple MCP server connections.

    Servers are started in background threads and their tools are
    registered into the Luc Nhan ToolRegistry as they come online.

    This class can be used as a singleton via `get_instance()`.
    """

    _instance: MCPManager | None = None

    def __init__(self):
        self._configs: list[MCPServerConfig] = []
        self._clients: dict[str, MCPClient] = {}
        self._lock = threading.Lock()
        self._shut_down = False
        self._generation = 0  # incremented on each start/reload cycle
        MCPManager._instance = self

    @classmethod
    def get_instance(cls) -> MCPManager:
        """Get the singleton MCPManager instance.

        Returns the global MCPManager if one exists.
        Raises RuntimeError if no manager has been created.
        """
        if cls._instance is None:
            raise RuntimeError("MCPManager has not been initialized")
        return cls._instance

    def load_config(self, path: str = "") -> int:
        """Load MCP config. Returns number of enabled servers found."""
        configs = load_mcp_config(path)
        enabled = [c for c in configs if c.enabled]
        with self._lock:
            self._configs = configs
        log_info(f"MCP config: {len(enabled)} enabled servers out of {len(configs)} total")
        return len(enabled)

    def add_external_configs(self, configs: list[MCPServerConfig]) -> None:
        """Append additional MCP server configs (from external sources).

        These are added to ``_configs`` before ``start_servers()`` is called.
        """
        if not configs:
            return
        with self._lock:
            self._configs.extend(configs)
        log_info(f"MCP: added {len(configs)} external server config(s)")

    def start_servers(
        self,
        registry: ToolRegistry,
        on_complete: Callable[[str, int], None] | None = None,
    ) -> None:
        """Start all enabled servers in background threads.

        Each server's tools are registered into `registry` as they come online.
        Optional `on_complete(server_name, tool_count)` callback is called per server.
        """
        if self._shut_down:
            log_warning("MCP: start_servers called after shutdown — ignoring")
            return

        # Reset the cached SDK probe so a previously missing SDK can be
        # detected after the user installs the ``mcp`` package.  Only done
        # here (when servers are actually about to start), not during reload()
        # or config load, so that lucnhan.mcp.client is not imported unless
        # an enabled server needs it.
        enabled_configs = [c for c in self._configs if c.enabled]
        if enabled_configs:
            try:
                from .client import reset_mcp_sdk_probe

                reset_mcp_sdk_probe()
            except Exception as exc:
                log_warning(f"MCP: failed to reset SDK probe before server start: {exc}")

        # Snapshot enabled configs and generation under lock so that
        # concurrent load_config/add_external_configs calls do not mutate
        # the list while we iterate.
        with self._lock:
            self._generation += 1
            gen = self._generation
            configs = [c for c in self._configs if c.enabled]

        for config in configs:
            thread = threading.Thread(
                target=self._start_one,
                args=(config, registry, on_complete, gen),
                daemon=True,
                name=f"mcp-start-{config.name}",
            )
            thread.start()

    def _start_one(
        self,
        config: MCPServerConfig,
        registry: ToolRegistry,
        on_complete: Callable[[str, int], None] | None,
        generation: int,
    ) -> None:
        """Start a single MCP server (runs in background thread).

        Uses a soft timeout (_SOFT_TIMEOUT) to emit a warning and the
        per-server "timeout" as the hard bound (falling back to
        _HARD_TIMEOUT when unset/invalid), preventing indefinite waits.
        The *generation* token guards against late registrations after a
        reload or shutdown has superseded this start cycle.

        MCPClient and bridge modules are imported here, NOT at module level,
        so importing lucnhan.mcp.manager does not pull in the official MCP SDK.

        ``MCPClient(config)`` construction and all imports are inside the
        try block so that missing-SDK or client-init failures are logged
        at error level instead of raising unhandled exceptions.
        """
        hard = config.resolve_timeout(_HARD_TIMEOUT)
        client = None
        safe_name = config.name.replace("-", "_").replace(".", "_")
        prefix = f"{MCP_TOOL_PREFIX}{safe_name}_"
        try:
            from .bridge import register_mcp_tools
            from .client import MCPClient

            client = MCPClient(config)
            t0 = time.monotonic()
            client.start(timeout=hard)
            elapsed = time.monotonic() - t0
            if elapsed > _SOFT_TIMEOUT:
                log_warning(f"MCP[{config.name}]: startup took {elapsed:.1f}s (soft limit {_SOFT_TIMEOUT}s)")
            with self._lock:
                if self._generation != generation or self._shut_down:
                    log_warning(
                        f"MCP[{config.name}]: discarding stale startup (gen {generation} vs current {self._generation})"
                    )
                    client.stop()
                    return
                # Do NOT store client yet — registration happens outside lock
            count = register_mcp_tools(client, registry, prefix=prefix)
            # Re-check generation after registration.  If a reload or
            # shutdown happened during registration, unregister the tools
            # we just added, stop the client, and do NOT store it.
            with self._lock:
                if self._generation != generation or self._shut_down:
                    log_warning(
                        f"MCP[{config.name}]: discarding after registration "
                        f"(gen {generation} vs current {self._generation})"
                    )
                    registry.unregister_by_prefix(prefix)
                    client.stop()
                    return
                self._clients[config.name] = client
            log_info(f"MCP[{config.name}]: started OK, {count} tools registered")
            if on_complete:
                on_complete(config.name, count)
        except Exception as e:
            log_error(f"MCP[{config.name}]: failed to start: {e}")
            if client is not None:
                try:
                    client.stop()
                except Exception as stop_err:
                    log_debug(f"MCP[{config.name}]: cleanup after start failure: {stop_err}")

    def stop_all(self) -> None:
        """Stop all running MCP servers."""
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()

        for client in clients:
            try:
                client.stop()
            except Exception as e:
                log_error(f"MCP[{client.name}]: stop error: {e}")

        log_info("MCP: all servers stopped")

    def shutdown(self) -> None:
        """Stop all servers and prevent further starts.

        Call this during application shutdown instead of ``stop_all()``
        to ensure no new servers are started after cleanup.
        Increments the generation to invalidate any in-flight startups.
        """
        with self._lock:
            self._shut_down = True
            self._generation += 1
        self.stop_all()

    def list_servers(self) -> list[str]:
        """List names of connected servers."""
        with self._lock:
            return list(self._clients.keys())

    def get_client(self, name: str) -> MCPClient | None:
        """Get a client by server name."""
        with self._lock:
            return self._clients.get(name)

    def reload(
        self,
        registry: ToolRegistry,
        config_path: str = "",
        on_complete: Callable[[str, int], None] | None = None,
    ) -> None:
        """Reload MCP config and restart servers.

        Stops all running servers, removes stale MCP tools from the
        registry, re-reads the config file, and starts any newly-enabled
        servers.  Safe to call from a background thread.

        Does NOT import lucnhan.mcp.client unless an enabled server is
        actually starting (SDK probe reset happens inside start_servers).
        """
        log_info("MCP: reloading configuration")
        self.stop_all()
        registry.unregister_by_prefix(MCP_TOOL_PREFIX)
        self.load_config(config_path)
        self.start_servers(registry, on_complete=on_complete)
