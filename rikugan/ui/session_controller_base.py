"""Host-agnostic session controller orchestration.

Performance note
----------------
The agent runtime (``AgentLoop``, ``BackgroundAgentRunner``, ``MCPManager``,
``ProviderRegistry``, ``SkillRegistry``, ``SessionState``, ``SessionHistory``)
is a heavy import chain (~25ms cold). It is needed to actually drive a
chat session, but the panel is created long before the user sends a
first message. We defer the imports into the methods that need them so
the panel-construction path stays light.
"""

from __future__ import annotations

import copy
import os
import threading
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..core.config import RikuganConfig
from ..core.host import get_database_instance_id, set_database_instance_id
from ..core.logging import log_debug, log_error, log_info, log_warning
from ..state.history_types import (
    HistoryAttachResult,
    HistoryAttachStatus,
    HistoryDeleteResult,
    HistoryDeleteStatus,
    HistoryLoadResult,
    HistoryRequestStatus,
    HistoryScope,
    SessionHistoryEntry,
)

if TYPE_CHECKING:
    from ..agent.loop import AgentLoop, BackgroundAgentRunner
    from ..agent.turn import TurnEvent
    from ..mcp.manager import MCPManager
    from ..providers.base import LLMProvider
    from ..providers.registry import ProviderRegistry
    from ..skills.registry import SkillRegistry
    from ..state.history import SessionHistory
    from ..state.session import SessionState
    from ..tools.registry import ToolRegistry
else:
    AgentLoop = BackgroundAgentRunner = TurnEvent = None  # type: ignore[assignment]
    MCPManager = ProviderRegistry = SkillRegistry = None  # type: ignore[assignment]
    SessionHistory = SessionState = None  # type: ignore[assignment]
    ToolRegistry = Any


def _normalize_db_path(path: str) -> str:
    if not path:
        return ""
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(path)))
    except OSError:
        return path


class SessionControllerBase:
    """Non-Qt orchestrator for Rikugan sessions."""

    def __init__(
        self,
        config: RikuganConfig,
        tool_registry_factory: Callable[[], ToolRegistry],
        database_path_getter: Callable[[], str],
        host_name: str,
        ensure_tools_ready: Callable[[Any], Any] | None = None,
        reset_deferred_tools: Callable[[], None] | None = None,
    ):
        # Lazy imports — keep heavy agent runtime off the panel-import
        # path. Each import is performed at most once per process; we
        # cache the imported symbols back into the module globals so
        # subsequent calls (and any sibling code) see them immediately.
        global AgentLoop, BackgroundAgentRunner, TurnEvent
        global MCPManager, ProviderRegistry, SkillRegistry
        global SessionHistory, SessionState
        if AgentLoop is None:
            from ..agent.loop import AgentLoop as _AgentLoop
            from ..agent.loop import BackgroundAgentRunner as _BackgroundAgentRunner
            from ..agent.turn import TurnEvent as _TurnEvent
            from ..mcp.manager import MCPManager as _MCPManager
            from ..providers.registry import ProviderRegistry as _ProviderRegistry
            from ..skills.registry import SkillRegistry as _SkillRegistry
            from ..state.history import SessionHistory as _SessionHistory
            from ..state.session import SessionState as _SessionState

            AgentLoop = _AgentLoop
            BackgroundAgentRunner = _BackgroundAgentRunner
            TurnEvent = _TurnEvent
            MCPManager = _MCPManager
            ProviderRegistry = _ProviderRegistry
            SkillRegistry = _SkillRegistry
            SessionHistory = _SessionHistory
            SessionState = _SessionState

        self.config = config
        self.host_name = host_name
        self._provider_registry = ProviderRegistry()
        self._provider_registry.register_custom_providers(
            list(config.custom_providers.keys()),
            dialects=self._compute_custom_dialects(config),
        )
        # Build the tool registry eagerly on the IDA/main thread.
        #
        # The factory typically imports the host's tool module tree
        # (``ida_funcs`` etc.), which AGENTS.md flags as a Shiboken
        # UAF risk on non-main threads (Python > 3.10). Calling it
        # here — in __init__, which the host invokes synchronously
        # from the plugin entry point — keeps every import on the
        # main thread and prevents the background ``_initialize_runtime``
        # from racing with ``start_agent`` over the same factory.
        self._tool_registry_factory = tool_registry_factory
        self._tool_registry: ToolRegistry = tool_registry_factory()
        self._skill_registry = SkillRegistry()
        self._mcp_manager = MCPManager()
        self._idb_path = _normalize_db_path(database_path_getter())
        self._db_instance_id = self._ensure_db_instance_id()
        self._runtime_init_done = threading.Event()
        self._runtime_shutdown = threading.Event()
        # Host-provided callbacks for advanced/deferred tool registration.
        # The base class does not import IDA modules — host subclasses
        # wire the actual registration function in (e.g. IDA Pro passes
        # ``register_advanced_tools`` from ``rikugan.ida.tools.registry``).
        self._advanced_tools_registered = False
        self._ensure_tools_ready = ensure_tools_ready
        self._reset_deferred_tools = reset_deferred_tools
        self._runtime_init_thread = threading.Thread(
            target=self._initialize_runtime,
            daemon=True,
            name="rikugan-runtime-init",
        )
        self._runtime_init_thread.start()

        # Multi-tab session management
        self._sessions: dict[str, SessionState] = {}
        self._active_tab_id: str = ""
        tab_id = self._create_session()
        self._active_tab_id = tab_id

        # Multi-runner: one BackgroundAgentRunner per tab so agents can run
        # concurrently. ``_pending_messages`` is per-tab too so a queued
        # follow-up in tab A never leaks into tab B. When
        # ``parallel_agent_enabled`` is False the effective cap is 1, which
        # reproduces the legacy single-agent behavior.
        self._runners: dict[str, BackgroundAgentRunner] = {}
        self._pending_messages: dict[str, list[str]] = {}
        self._max_concurrent_agents = config.parallel_agent_max_concurrent if config.parallel_agent_enabled else 1
        # Snapshot of the skill-relevant config fields so ``update_settings``
        # can skip the expensive ``_reload_skills`` filesystem rescan when
        # only non-skill config (provider/model/theme) changed. Theme-only
        # and provider-only edits used to pay the full discovery cost.
        self._skill_config_signature = self._compute_skill_config_signature()

    def _compute_skill_config_signature(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return an order-insensitive snapshot of the skill-relevant config.

        These two fields are what :meth:`_reload_skills` feeds into
        :meth:`SkillRegistry.load_external_skills`, so they are the only
        fields whose change requires a reload. Comparing the signature
        before/after a Settings round-trip lets us skip the filesystem
        scan when the user only flipped the theme or the model.
        """
        return (
            tuple(sorted(self.config.enabled_external_skills)),
            tuple(sorted(self.config.disabled_skills)),
        )

    def _initialize_runtime(self) -> None:
        """Load heavy runtime components off the UI path."""
        started = time.perf_counter()
        try:
            if self._runtime_shutdown.is_set():
                return
            self._skill_registry.discover()

            # Apply disabled skills + load enabled external skills
            self._skill_registry.load_external_skills(
                self.config.enabled_external_skills,
                self.config.disabled_skills,
            )

            if self._runtime_shutdown.is_set():
                return
            self._mcp_manager.load_config()

            enabled_set = set(self.config.enabled_external_mcp)
            if enabled_set:
                # Load enabled external MCP servers only when explicitly configured.
                from ..core.external_sources import discover_all_external_mcp

                external_mcp = discover_all_external_mcp()
                for source_key, servers in external_mcp.items():
                    enabled = [s for s in servers if f"{source_key}:{s.name}" in enabled_set]
                    if enabled:
                        self._mcp_manager.add_external_configs(enabled)

            if self._runtime_shutdown.is_set():
                return
            self._mcp_manager.start_servers(self.tool_registry)
        except Exception as e:
            log_error(f"Background runtime initialization failed: {e}")
        finally:
            self._runtime_init_done.set()
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            log_debug(f"Runtime initialization completed in {elapsed_ms} ms")

    # --- Instance ID ---

    @staticmethod
    def _ensure_db_instance_id() -> str:
        """Read or generate a database-instance UUID for the current IDB."""
        existing = get_database_instance_id()
        if existing:
            log_debug(f"Database instance ID: {existing}")
            return existing
        new_id = uuid.uuid4().hex
        if set_database_instance_id(new_id):
            log_info(f"Generated new database instance ID: {new_id}")
            return new_id
        # Standalone or write failure — use an ephemeral ID (won't persist)
        log_debug("Could not persist database instance ID, using ephemeral")
        return new_id

    # --- Tab / multi-session management ---

    def _create_session(self) -> str:
        """Create a new SessionState and return its tab_id."""
        tab_id = uuid.uuid4().hex[:8]
        session = SessionState(
            provider_name=self.config.provider.name,
            model_name=self.config.provider.model,
            idb_path=self._idb_path,
            db_instance_id=self._db_instance_id,
        )
        self._sessions[tab_id] = session
        return tab_id

    def create_tab(self) -> str:
        """Create a new tab with a fresh session. Returns tab_id."""
        tab_id = self._create_session()
        log_info(f"Created new tab {tab_id}")
        return tab_id

    def fork_session(self, source_tab_id: str) -> str | None:
        """Duplicate a session into a new tab. Returns new tab_id or None."""
        source = self._sessions.get(source_tab_id)
        if source is None:
            return None
        new_tab_id = uuid.uuid4().hex[:8]
        forked = SessionState(
            provider_name=source.provider_name,
            model_name=source.model_name,
            idb_path=source.idb_path,
            db_instance_id=source.db_instance_id,
        )
        forked.messages = copy.deepcopy(source.messages)
        forked.total_usage = copy.copy(source.total_usage)
        forked.last_prompt_tokens = source.last_prompt_tokens
        forked.current_turn = source.current_turn
        forked.metadata = dict(source.metadata)
        forked.metadata["forked_from"] = source.id
        self._sessions[new_tab_id] = forked
        log_info(f"Forked session {source.id} → new tab {new_tab_id}")
        return new_tab_id

    def close_tab(self, tab_id: str) -> None:
        """Save and remove a tab's session, cancelling any running agent on it."""
        session = self._sessions.get(tab_id)
        if session is None:
            return
        # Stop the agent that belongs to this tab before tearing it down.
        self.cancel(tab_id)
        self._runners.pop(tab_id, None)
        if self.config.checkpoint_auto_save and session.messages:
            try:
                history = SessionHistory(self.config)
                history.save_session(session)
            except (OSError, ValueError) as e:
                log_error(f"Failed to save session on tab close: {e}")
        del self._sessions[tab_id]
        log_debug(f"Closed tab {tab_id}")

    def switch_tab(self, tab_id: str) -> None:
        """Switch active tab.

        Unlike the legacy single-agent behavior, switching tabs does NOT
        cancel a running agent — agents keep running in their own tab so
        multiple can progress concurrently.
        """
        if tab_id == self._active_tab_id:
            return
        if tab_id not in self._sessions:
            return
        self._active_tab_id = tab_id
        log_debug(f"Switched to tab {tab_id}")

    def tab_label(self, tab_id: str) -> str:
        """Return a display label for a tab.

        Uses the shared :func:`derive_history_title` helper at
        ``max_chars=20`` so tab text and History titles share one
        derivation pipeline (spec §9.1). Returns ``"New Chat"`` for
        empty tabs and unknown ids, matching the previous behavior.
        """
        session = self._sessions.get(tab_id)
        if session is None or not session.messages:
            return "New Chat"
        # Lazy import — the derivation helper lives in the persistence
        # module which is already loaded by ``__init__`` via the heavy
        # runtime chain.
        from ..state.history import derive_history_title

        return derive_history_title(session.messages, max_chars=20)

    @property
    def active_tab_id(self) -> str:
        return self._active_tab_id

    @property
    def tab_ids(self) -> list[str]:
        return list(self._sessions.keys())

    @property
    def session(self) -> SessionState:
        return self._sessions[self._active_tab_id]

    def get_session(self, tab_id: str) -> SessionState | None:
        return self._sessions.get(tab_id)

    @property
    def provider_registry(self) -> ProviderRegistry:
        return self._provider_registry

    @property
    def tool_registry(self) -> ToolRegistry:
        # The registry is built eagerly in ``__init__`` so the IDA
        # module imports land on the main thread. This is a plain
        # getter; do NOT make it lazy — the original lazy form
        # triggered a Shiboken UAF on Python > 3.10 when the property
        # was first accessed from the ``_initialize_runtime`` background
        # thread.
        return self._tool_registry

    @property
    def skill_slugs(self) -> list[str]:
        if not self._runtime_init_done.is_set():
            return []
        return self._skill_registry.list_slugs()

    @property
    def runtime_ready(self) -> bool:
        return self._runtime_init_done.is_set()

    @property
    def is_agent_running(self) -> bool:
        """True if *any* tab has a running agent (used by headless/control)."""
        return any(r.agent_loop.is_running for r in self._runners.values())

    def is_tab_running(self, tab_id: str) -> bool:
        """True if the given tab's agent is currently running."""
        runner = self._runners.get(tab_id)
        return runner is not None and runner.agent_loop.is_running

    def get_runner(self, tab_id: str | None = None) -> BackgroundAgentRunner | None:
        """Return the runner for *tab_id* (defaults to the active tab).

        Zero-arg default targets the active tab so headless/control call
        sites (which always have exactly one tab) keep working unchanged.
        """
        tid = tab_id if tab_id is not None else self._active_tab_id
        return self._runners.get(tid)

    def iter_runners(self):
        """Yield ``(tab_id, runner)`` for every live runner."""
        return iter(self._runners.items())

    def _running_count(self) -> int:
        return sum(1 for r in self._runners.values() if r.agent_loop.is_running)

    def has_free_slot(self) -> bool:
        """True if a new agent can start without exceeding the concurrency cap."""
        return self._running_count() < self._max_concurrent_agents

    def get_provider(self) -> LLMProvider | None:
        """Create and return an LLMProvider instance for the current config.

        Returns None when provider construction or ensure_ready fails (failure
        is logged via ``log_error``); callers must defensively handle this.
        """
        try:
            return self._create_provider()
        except Exception as e:
            log_error(f"Provider creation failed: {e}")
            return None

    def get_tool_registry(self) -> ToolRegistry:
        """Return the lazily-created tool registry."""
        return self.tool_registry

    # --- Advanced / deferred tool registration (host-provided) ---

    def ensure_advanced_tools_ready(self) -> bool:
        """Register host-specific advanced tool modules, if a callback was provided.

        The base class does not know about IDA/Binja/Hex-Rays — host
        subclasses pass a callable in ``__init__`` (e.g.
        ``register_advanced_tools`` from ``rikugan.ida.tools.registry``).
        The call is idempotent and never raises: failures are logged
        and a retry is attempted on the next prompt or settings reload.

        Returns True when all modules registered (or the host does not
        provide advanced registration), False when at least one module
        failed and may be retried later.
        """
        if self._advanced_tools_registered:
            return True
        if self._ensure_tools_ready is None:
            self._advanced_tools_registered = True
            return True
        try:
            result = self._ensure_tools_ready(self.tool_registry)
        except Exception as e:
            log_warning(f"Advanced tool registration failed: {e}")
            return False
        ok = bool(getattr(result, "ok", True))
        if not ok:
            failed = getattr(result, "failed_modules", []) or []
            log_warning(
                "Advanced tool registration partially failed: "
                f"{len(failed)} modules ({', '.join(failed)}). "
                "Will retry on next prompt or settings reload."
            )
            return False
        registered = int(getattr(result, "registered", 0) or 0)
        self._advanced_tools_registered = True
        log_info(f"Advanced tool registration complete ({registered} tools)")
        return True

    def reset_deferred_tools(self) -> None:
        """Reset the deferred-registration retry state.

        The host may need to retry every module (not just the
        previously-failed ones) after the operator changes environment
        state (e.g. installs Hex-Rays).  The optional
        ``reset_deferred_tools`` callback is invoked first so the host
        can clear its own bookkeeping; we then clear the cached
        "already-registered" flag so the next call to
        :func:`ensure_advanced_tools_ready` re-imports everything.
        """
        if self._reset_deferred_tools is not None:
            try:
                self._reset_deferred_tools()
            except Exception as e:
                log_warning(f"Failed to reset deferred tool state: {e}")
        self._advanced_tools_registered = False

    @staticmethod
    def _compute_custom_dialects(config: Any) -> dict[str, str]:
        """Extract the dialect identifier for each custom provider.

        Reads ``config.providers[name]["extra"]["dialect"]`` (the saved
        snapshot) so GLM-dialect custom providers route to
        :class:`GLMProvider` instead of the generic OpenAI-compat adapter.
        """
        dialects: dict[str, str] = {}
        for name in config.custom_providers:
            saved = config.providers.get(name, {})
            extra = saved.get("extra", {}) if isinstance(saved, dict) else {}
            dialect = extra.get("dialect", "") if isinstance(extra, dict) else ""
            dialects[name] = str(dialect)
        return dialects

    def _create_provider(self) -> Any:
        """Create (or fetch) an LLMProvider instance for the current config.

        Centralised so OAuth/keychain consent prompts and MiniMax web
        config sync run exactly once per provider creation. Host
        subclasses may override to add custom side effects.
        """
        if not self._runtime_init_done.is_set():
            self._runtime_init_done.wait(timeout=10.0)
        # Apply OAuth keychain consent for Anthropic before creating the
        # provider so the provider does not see a "missing" key on the
        # first run.
        try:
            from ..providers.auth_cache import resolve_auth_cached

            resolve_auth_cached(self.config.provider.api_key or "")
        except Exception as exc:
            log_debug(f"Session auth cache warm-up failed: {exc}")
        # Deep-copy the provider's ``extra`` dict so the GLM provider
        # (or any future dialect-aware provider) cannot mutate the live
        # config's nested options via reference aliasing (spec §12.3).
        extra = copy.deepcopy(self.config.provider.extra) if self.config.provider.extra else None
        provider = self._provider_registry.get_or_create(
            self.config.provider.name,
            api_key=self.config.provider.api_key,
            api_base=self.config.provider.api_base,
            model=self.config.provider.model,
            extra=extra,
        )
        # Let ``ensure_ready`` raise: ``start_agent`` and ``get_provider``
        # both wrap this call in a try/except and surface the failure as
        # a user-facing "Provider error" string. Silently swallowing a
        # broken-provider here would feed a half-initialised provider to
        # ``AgentLoop`` and crash the loop with an opaque traceback.
        provider.ensure_ready()
        return provider

    def _sync_web_tool_config(self) -> None:
        """Sync MiniMax web tool runtime config + capabilities onto the registry.

        Mirrors ``start_agent``'s pre-loop side effect: web tools need
        the active config baked in before the LLM sees the tool schema,
        and the registry's capability flags must reflect which provider
        is active so the agent can branch on it.
        """
        try:
            from ..tools import web as _web

            _web.set_runtime_config(self.config)
        except Exception as exc:
            log_debug(f"web tool runtime config sync failed: {exc}")
        is_minimax = (self.config.provider.name or "").lower() == "minimax"
        try:
            self.tool_registry.set_capabilities({"minimax_provider": is_minimax})
        except Exception as exc:
            log_debug(f"set_capabilities(minimax_provider) failed: {exc}")

    def start_agent(self, user_message: str, tab_id: str | None = None) -> str | None:
        """Create provider + agent loop and start the background runner.

        Targets *tab_id* (defaults to the active tab). The runner is stored
        per-tab in ``_runners`` so multiple agents can run concurrently.
        """
        tid = tab_id if tab_id is not None else self._active_tab_id
        if not self._runtime_init_done.is_set():
            # Delay only the first agent start if background init is still running.
            self._runtime_init_done.wait(timeout=10.0)

        # Make sure host-specific advanced tools (decompiler, types,
        # scripting, web) are registered before the LLM sees the schema.
        self.ensure_advanced_tools_ready()

        try:
            provider = self._create_provider()
        except Exception as e:
            log_error(f"Provider creation failed: {e}")
            return f"Provider error: {e}"

        # Sync MiniMax web-tool runtime config + capability flags. This
        # has to happen *after* the tool registry exists (it depends on
        # tool_registry.set_capabilities) but *before* AgentLoop builds
        # the tool schema to send to the model.
        self._sync_web_tool_config()

        session = self._sessions.get(tid)
        if session is None:
            return f"Unknown tab: {tid}"

        loop = AgentLoop(
            provider,
            self.tool_registry,
            self.config,
            session,
            skill_registry=self._skill_registry,
            host_name=self.host_name,
        )

        # Inject central memory service for every agent run.
        self._wire_central_memory(loop, tid)

        runner = BackgroundAgentRunner(loop)
        self._runners[tid] = runner
        runner.start(user_message)
        return None

    def _wire_central_memory(self, loop: AgentLoop, tab_id: str | None = None) -> None:
        """Construct and inject BinaryMemoryService into the loop.

        Called for every agent run. If identity resolution fails (bind
        returns ephemeral), this method returns early without injecting
        a service, so the loop runs without central memory.
        """
        tid = tab_id if tab_id is not None else self._active_tab_id
        try:
            from ..memory.authority import MemoryAuthorityIssuer
            from ..memory.manager import MemoryWorkspaceManager
            from ..memory.markdown import MemoryProjector
            from ..memory.repository import SQLiteKnowledgeRepository
            from ..memory.workspace_open import open_workspace_for_write
            from ..memory.workspace_store import WorkspaceStore

            session = self._sessions.get(tid)
            if session is None:
                return
            manager = MemoryWorkspaceManager(self.config)

            # Build identity request from current session
            from ..memory.identity import get_filesystem_identity
            from ..memory.workspace import IdentityRequest

            fs = get_filesystem_identity(session.idb_path) if session.idb_path else None
            request = IdentityRequest(
                source_kind="idb",
                idb_path=session.idb_path or "",
                db_instance_id=session.db_instance_id or "",
                display_name=session.idb_path.split("/")[-1] if session.idb_path else "",
                filesystem_identity=fs,
            )
            result = manager.bind(request)
            if result.binding is None or result.binding.state not in {"active", "provisional"}:
                return

            paths = manager.require_persistent_paths()
            if paths.database.exists():
                store = open_workspace_for_write(
                    paths,
                    result.binding.memory_id,
                    manager.locator.backups(result.binding.memory_id),
                )
            else:
                store = WorkspaceStore.create(paths, owner_memory_id=result.binding.memory_id)

            # Auto-import legacy JSONL records once per workspace. Best-effort:
            # an exception here is logged but never blocks memory wiring.
            try:
                from ..memory.jsonl_migration import maybe_import_legacy_jsonl
                from ..memory.paths import knowledge_paths

                if session.idb_path:
                    jsonl_paths = knowledge_paths(session.idb_path, session.db_instance_id or "")
                    maybe_import_legacy_jsonl(store, result.binding.memory_id, jsonl_paths)
            except Exception as e:
                log_error(f"legacy JSONL import failed: {e}")

            repo = SQLiteKnowledgeRepository(store, owner_memory_id=result.binding.memory_id)
            projector = MemoryProjector()
            issuer = MemoryAuthorityIssuer()

            context = manager.run_context()
            from ..memory.service import BinaryMemoryService

            service = BinaryMemoryService(
                context=context,
                paths=paths,
                repository=repo,
                store=store,
                projector=projector,
                authority_issuer=issuer,
            )
            loop.memory_service = service
            loop._memory_authority = issuer.issue(context)
            loop._memory_manager = manager
            session.binary_memory_id = result.binding.memory_id
            log_info(f"Central memory wired: memory_id={result.binding.memory_id[:12]}")
        except Exception as e:
            log_error(f"Central memory wiring failed: {e}")
            import traceback

            log_error(traceback.format_exc())

    def get_event(self, tab_id: str | None = None, timeout: float = 0) -> TurnEvent | None:
        """Return the next event for *tab_id*'s runner (defaults to active tab).

        Zero-arg form targets the active tab to keep headless/control callers
        working without changes.
        """
        tid = tab_id if tab_id is not None else self._active_tab_id
        runner = self._runners.get(tid)
        if runner is None:
            return None
        return runner.get_event(timeout=timeout)

    def cancel(self, tab_id: str | None = None) -> None:
        """Cancel the agent for *tab_id* (defaults to the active tab).

        Only that tab's pending queue is cleared — other tabs are untouched.
        """
        tid = tab_id if tab_id is not None else self._active_tab_id
        self._pending_messages.pop(tid, None)
        runner = self._runners.get(tid)
        if runner:
            runner.cancel()

    def cancel_all(self) -> None:
        """Cancel every running agent across all tabs."""
        self._pending_messages.clear()
        for runner in self._runners.values():
            runner.cancel()

    def queue_message(self, text: str) -> None:
        """Queue *text* as a follow-up for the active tab."""
        self.queue_message_for_tab(self._active_tab_id, text)

    def queue_message_for_tab(self, tab_id: str, text: str) -> None:
        """Queue *text* as a follow-up for a specific tab."""
        self._pending_messages.setdefault(tab_id, []).append(text)
        pending = self._pending_messages.get(tab_id, [])
        log_debug(f"Message queued for tab {tab_id}, {len(pending)} pending")

    def pop_queued_message(self, tab_id: str) -> str | None:
        """Pop and return the oldest queued follow-up for *tab_id*, if any."""
        queue = self._pending_messages.get(tab_id)
        if not queue:
            return None
        msg = queue.pop(0)
        log_debug(f"Draining queue for tab {tab_id}: {len(queue)} remaining")
        return msg

    def on_agent_finished(self, tab_id: str | None = None) -> str | None:
        """Handle end-of-run cleanup for *tab_id* and return its next queued message.

        Pops that tab's runner from ``_runners`` and saves its session. The
        returned message (if any) lets the UI drain that tab's follow-up queue.
        Cancellation paths (``cancel``, ``new_chat``, ``shutdown``) clear the
        queue separately to avoid leaking stale-context requests across runs.

        Returns
        -------
        str | None
            That tab's first pending message if any remain, otherwise ``None``.
        """
        tid = tab_id if tab_id is not None else self._active_tab_id
        self._runners.pop(tid, None)

        # Re-persist the instance ID in the database so a freshly created
        # IDB still gets one recorded before the next checkpoint cycle.
        set_database_instance_id(self._db_instance_id)

        session = self._sessions.get(tid)
        if session and self.config.checkpoint_auto_save and session.messages:
            try:
                history = SessionHistory(self.config)
                # Off-main-thread: the JSON dump of the whole transcript can
                # take long enough to visibly freeze IDA at end of turn.
                # Fire-and-forget is safe here — the next turn re-reads from
                # the in-memory session, not the file, and a missed save is
                # recovered on the next checkpoint. The single-worker
                # executor inside SessionHistory serialises back-to-back
                # saves so the latest state always wins.
                future = history.save_session_async(session)
                future.add_done_callback(self._on_async_save_done)
            except (OSError, ValueError) as e:
                log_error(f"Failed to auto-save session: {e}")

        queue = self._pending_messages.get(tid)
        if not queue:
            return None
        next_message = queue.pop(0)
        log_debug(f"Draining queue after run (tab {tid}): {len(queue)} remaining")
        return next_message

    @staticmethod
    def _on_async_save_done(future: Any) -> None:
        """Log the outcome of a fire-and-forget ``save_session_async`` call.

        Exceptions raised inside the worker thread are captured by the
        Future and would otherwise be silently dropped. This callback
        surfaces them so a corrupted save is visible in the log.
        """
        exc = future.exception()
        if exc is not None:
            log_error(f"Background session save failed: {exc}")

    def new_chat(self) -> None:
        """Reset the active tab to a fresh session."""
        self._pending_messages.pop(self._active_tab_id, None)
        # A running agent on this tab is now orphaned by the session reset —
        # cancel it so it doesn't write results into the discarded session.
        self.cancel(self._active_tab_id)
        session = self._sessions.get(self._active_tab_id)
        if session and self.config.checkpoint_auto_save and session.messages:
            try:
                history = SessionHistory(self.config)
                # Off-main-thread for the same reason as on_agent_finished:
                # the dump freezes IDA's shared event loop otherwise. Safe to
                # fire-and-forget — the session object stays alive via the
                # Future's reference until the worker finishes.
                future = history.save_session_async(session)
                future.add_done_callback(self._on_async_save_done)
            except OSError as e:
                log_debug(f"Failed to save session on new chat: {e}")
        self._sessions[self._active_tab_id] = SessionState(
            provider_name=self.config.provider.name,
            model_name=self.config.provider.model,
            idb_path=self._idb_path,
            db_instance_id=self._db_instance_id,
        )
        log_info("Started new chat session (active tab)")

    # ------------------------------------------------------------------
    # History on-demand APIs (spec §8.1, §8.3, §10.1-§10.3)
    # ------------------------------------------------------------------

    def capture_history_scope(self, generation: int) -> HistoryScope:
        """Snapshot the live IDB identity + a request generation (Qt main thread).

        The returned :class:`HistoryScope` is immutable; it is the only
        data the background history worker reads about "which IDB this
        request belongs to". PanelCore owns ``generation`` so a stale
        result can be discarded after an IDB switch or shutdown.
        """
        return HistoryScope(
            idb_path=self._idb_path,
            db_instance_id=self._db_instance_id,
            generation=generation,
        )

    def find_tab_for_session(self, persisted_session_id: str) -> str | None:
        """Return the open ``tab_id`` whose ``session.id`` matches, else ``None``.

        The parameter is the persisted ``SessionState.id`` stored in the
        manifest, never the ephemeral ``_sessions`` key (spec §10.2).
        Used for duplicate detection before starting a load and again
        before attaching the loaded session.
        """
        for tab_id, session in self._sessions.items():
            if session.id == persisted_session_id:
                return tab_id
        return None

    def list_history_sessions(self, scope: HistoryScope) -> list[SessionHistoryEntry]:
        """List persisted sessions that belong to ``scope`` (background worker).

        Maps manifest metadata to frozen :class:`SessionHistoryEntry` rows
        without opening any session JSON. ``load_session`` is never called
        per row (spec §8.1). The caller (PanelCore worker) is expected
        to ``flush_saves`` first and sort newest-first.
        """
        history = SessionHistory(self.config)
        summaries = history.list_sessions(
            idb_path=scope.idb_path,
            db_instance_id=scope.db_instance_id,
        )
        entries: list[SessionHistoryEntry] = []
        for row in summaries:
            entries.append(
                SessionHistoryEntry(
                    session_id=row.get("id", ""),
                    title=row.get("description", "") or "Untitled chat",
                    created_at=float(row.get("created_at", 0) or 0),
                    updated_at=float(row.get("updated_at", 0) or 0),
                    provider=row.get("provider", "") or "",
                    model=row.get("model", "") or "",
                    message_count=int(row.get("messages", 0) or 0),
                )
            )
        return entries

    def load_history_session(self, session_id: str, scope: HistoryScope) -> HistoryLoadResult:
        """Load one session payload and validate it belongs to ``scope``.

        Runs on the dedicated history worker thread. Does not mutate
        ``_sessions`` and does not touch Qt. The same
        :func:`_matches_current_idb` predicate used by listing is
        re-applied here so list-time and post-load authorization cannot
        drift (spec §8.3, §10.1).
        """
        from ..state.history import _matches_current_idb

        history = SessionHistory(self.config)
        try:
            session = history.load_session(session_id)
        except (OSError, ValueError, KeyError) as exc:
            log_error(f"Failed to load history session {session_id}: {exc}")
            return HistoryLoadResult(
                status=HistoryRequestStatus.FAILED,
                scope=scope,
                error=str(exc),
            )
        if session is None:
            return HistoryLoadResult(status=HistoryRequestStatus.NOT_FOUND, scope=scope)
        if not session.messages:
            return HistoryLoadResult(status=HistoryRequestStatus.EMPTY, scope=scope)
        # Re-validate against the captured scope using the shared predicate.
        if not _matches_current_idb(
            entry_idb_path=session.idb_path,
            entry_db_instance_id=session.db_instance_id,
            target_idb_path=scope.idb_path,
            target_db_instance_id=scope.db_instance_id,
        ):
            return HistoryLoadResult(status=HistoryRequestStatus.WRONG_IDB, scope=scope)
        return HistoryLoadResult(status=HistoryRequestStatus.LOADED, scope=scope, session=session)

    def attach_history_session(self, result: HistoryLoadResult) -> HistoryAttachResult:
        """Attach a loaded session to ``_sessions`` (Qt main thread only).

        Compares ``result.scope`` with the freshly captured live path /
        instance identity. A mismatch after an IDB switch yields
        ``STALE_SCOPE`` without creating a tab. A persisted id already
        open in some tab yields ``ALREADY_OPEN`` with the existing
        ``tab_id``. Otherwise the session is inserted under a fresh
        ``uuid4().hex[:8]`` tab key (spec §10.1, §10.3).
        """
        # STALE_SCOPE check: scope must match the live identity. Per the
        # spec, the drain step compares ``result.scope.generation`` on
        # the PanelCore side; here we compare the path/instance identity
        # directly so a stale load cannot be attached to the wrong IDB.
        from ..state.history import _matches_current_idb

        if result.session is None or result.status is not HistoryRequestStatus.LOADED:
            return HistoryAttachResult(status=HistoryAttachStatus.STALE_SCOPE)
        if not _matches_current_idb(
            entry_idb_path=result.scope.idb_path,
            entry_db_instance_id=result.scope.db_instance_id,
            target_idb_path=self._idb_path,
            target_db_instance_id=self._db_instance_id,
        ):
            return HistoryAttachResult(status=HistoryAttachStatus.STALE_SCOPE)
        # Duplicate detection (post-load): the persisted id may have
        # been opened by another request while this load was in flight.
        session = result.session
        existing_tab_id = self.find_tab_for_session(session.id)
        if existing_tab_id is not None:
            return HistoryAttachResult(
                status=HistoryAttachStatus.ALREADY_OPEN,
                tab_id=existing_tab_id,
                session=session,
            )
        # Reuse path: if the active tab is an untouched empty draft,
        # load the historical session INTO it instead of spawning a new
        # tab. This matches the user mental model of "open a chat into
        # the blank New Chat I'm looking at". A non-empty active tab (a
        # real conversation in progress) keeps the historical behaviour
        # of creating a new tab.
        active_tab_id = self._active_tab_id
        active_session = self._sessions.get(active_tab_id) if active_tab_id else None
        if active_session is not None and not active_session.messages:
            self._sessions[active_tab_id] = session  # type: ignore[assignment]
            log_info(f"Reused active tab {active_tab_id} for history session {session.id}")
            return HistoryAttachResult(status=HistoryAttachStatus.REUSED, tab_id=active_tab_id, session=session)
        tab_id = uuid.uuid4().hex[:8]
        self._sessions[tab_id] = session
        log_info(f"Attached history session {session.id} as tab {tab_id}")
        return HistoryAttachResult(status=HistoryAttachStatus.OPENED, tab_id=tab_id, session=session)

    def delete_history_session(
        self,
        session_id: str,
        scope: HistoryScope,
    ) -> HistoryDeleteResult:
        """Delete one persisted current-IDB chat on the history worker thread.

        Qt-free controller boundary (spec §8.4, §11.5): validates the
        requested ``scope`` against the live ``_idb_path`` /
        ``_db_instance_id`` before touching the persistence worker, then
        forwards the delete to :meth:`SessionHistory.delete_session_async`
        and maps the internal :class:`SessionDeleteStatus` outcome onto
        the UI-facing :class:`HistoryDeleteStatus`. A scope mismatch
        short-circuits to ``WRONG_IDB`` without enqueuing any I/O — the
        delete worker must never see a request targeting a different
        IDB, because the user already switched away.

        Exceptions deliberately NOT caught:
          * ``CancelledError`` / ``KeyboardInterrupt`` / ``SystemExit`` —
            the worker cancellation contract (spec §8.1) lets these
            propagate so a Qt-cancelled request can unwind the future
            chain and let the History panel re-enable its row UI.
          * ``BaseException`` would mask real bugs; every caught branch
            below is a concrete persistence failure mode the
            ``HistoryDeleteResult`` is designed to surface to the UI.

        ``json.JSONDecodeError`` is already a ``ValueError`` so the
        explicit catch set stays minimal.
        """
        if scope.idb_path != self._idb_path or scope.db_instance_id != self._db_instance_id:
            return HistoryDeleteResult(
                HistoryDeleteStatus.WRONG_IDB,
                scope,
                session_id,
            )

        from ..state.history import SessionDeleteStatus, SessionHistory

        history = SessionHistory(self.config)
        try:
            outcome = history.delete_session_async(
                session_id,
                expected_idb_path=scope.idb_path,
                expected_db_instance_id=scope.db_instance_id,
            ).result()
        except FileNotFoundError:
            status = HistoryDeleteStatus.NOT_FOUND
            error = ""
        except (OSError, ValueError, KeyError) as exc:
            status = HistoryDeleteStatus.FAILED
            error = f"{type(exc).__name__}: {exc}"
        else:
            status = {
                SessionDeleteStatus.DELETED: HistoryDeleteStatus.DELETED,
                SessionDeleteStatus.NOT_FOUND: HistoryDeleteStatus.NOT_FOUND,
                SessionDeleteStatus.WRONG_IDB: HistoryDeleteStatus.WRONG_IDB,
                SessionDeleteStatus.FAILED: HistoryDeleteStatus.FAILED,
            }[outcome.status]
            error = outcome.error
        return HistoryDeleteResult(status, scope, session_id, error=error)

    def reset_for_new_file(self, new_idb_path: str) -> None:
        """Save all sessions and reset for a new database file.

        Spec §7.2: after ``cancel()``, each non-empty old-IDB session is
        submitted to ``save_session_async`` in deterministic tab order
        and then detached by clearing controller state. The futures
        retain the old session objects; no code may mutate them after
        detachment. Shutdown keeps its synchronous durability behavior
        unchanged.
        """
        self.cancel()
        for tab_id, session in list(self._sessions.items()):
            if not session.messages:
                continue
            try:
                future = SessionHistory(self.config).save_session_async(session)
                future.add_done_callback(self._on_async_save_done)
            except (OSError, ValueError) as exc:
                log_error(f"Failed to enqueue session {tab_id} on file change: {exc}")
        self._sessions.clear()
        self._idb_path = _normalize_db_path(new_idb_path)
        self._db_instance_id = self._ensure_db_instance_id()
        tab_id = self._create_session()
        self._active_tab_id = tab_id

    def update_settings(self) -> None:
        # Re-register custom providers in case user added/removed one
        self._provider_registry.register_custom_providers(
            list(self.config.custom_providers.keys()),
            dialects=self._compute_custom_dialects(self.config),
        )
        # Clear provider instances cache to force fresh creation with new credentials.
        # Without this, get_or_create() may return a cached instance from
        # _ModelFetcher in Settings dialog with stale internal state (e.g. cached
        # HTTP client), causing crashes when streaming starts on the next message.
        # Use the public ``retire_instances`` method instead of touching the
        # private ``_instances`` dict directly so the registry's safety
        # invariants (retire-before-clear) are preserved.
        self._provider_registry.retire_instances()
        for session in self._sessions.values():
            session.provider_name = self.config.provider.name
            session.model_name = self.config.provider.model
        # Re-arm advanced tool registration: the operator may have
        # installed Hex-Rays (or another decompiler), closed/reopened
        # the database, or otherwise changed the deferred-registration
        # retry state.  Resetting the flag makes the next prompt retry
        # the host-provided ``ensure_tools_ready`` path.
        self.reset_deferred_tools()
        # Reload skills ONLY when the skill-relevant config changed.
        # ``_reload_skills`` does a filesystem rescan (``SkillRegistry.discover``)
        # which is the dominant cost of a Settings round-trip; theme-only and
        # provider/model-only edits must not pay it. Compare the signature of
        # the two fields _reload_skills consumes before doing the work.
        new_skill_signature = self._compute_skill_config_signature()
        if new_skill_signature != self._skill_config_signature:
            self._skill_config_signature = new_skill_signature
            self._reload_skills()

    def _reload_skills(self) -> None:
        """Re-discover skills and apply current config for enabled/disabled state.

        Called after settings change so newly enabled external skills appear
        immediately without requiring an IDA restart.
        """
        if not self._runtime_init_done.is_set():
            return
        self._skill_registry.discover()
        self._skill_registry.load_external_skills(
            self.config.enabled_external_skills,
            self.config.disabled_skills,
        )

    def reload_mcp(self) -> None:
        """Reload MCP config and restart servers in the background.

        Safe to call at any time — stops existing servers first, then
        re-reads the config and starts newly-enabled servers.
        """
        thread = threading.Thread(
            target=self._mcp_manager.reload,
            args=(self.tool_registry,),
            daemon=True,
            name="rikugan-mcp-reload",
        )
        thread.start()

    def shutdown(self) -> None:
        self._runtime_shutdown.set()
        if self._runtime_init_thread.is_alive():
            self._runtime_init_done.wait(timeout=1.0)
        # Cancel every running agent across all tabs.
        self.cancel_all()
        self._runners.clear()
        # Final attempt to persist instance ID before the host saves the DB.
        set_database_instance_id(self._db_instance_id)
        for tab_id, session in self._sessions.items():
            if self.config.checkpoint_auto_save and session.messages:
                try:
                    history = SessionHistory(self.config)
                    history.save_session(session)
                except (OSError, ValueError) as e:
                    log_error(f"Failed to save session {tab_id} on shutdown: {e}")
        self._mcp_manager.shutdown()
