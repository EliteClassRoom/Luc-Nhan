"""LLM provider abstract base class."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Generator
from dataclasses import replace as dataclass_replace
from typing import Any, NoReturn

from ..core.logging import log_debug, silence_sdk_debug_loggers
from ..core.sanitize import (
    sanitize_messages_for_provider,
    strip_lone_surrogates,
)
from ..core.types import (
    LLMRequestContext,
    Message,
    ModelInfo,
    ProviderCapabilities,
    StreamChunk,
)

# Watchdog poll interval: how often a streaming cancel watchdog re-checks
# its per-request ``done`` event while parked on the loop cancel event.
# Small enough that a finished request releases the thread promptly.
_WATCHDOG_POLL_INTERVAL = 0.5


class LLMProvider(ABC):
    """Abstract base for all LLM provider adapters.

    The translation pipeline (format -> build kwargs -> call API -> normalize)
    is implemented once in the concrete ``chat`` and ``chat_stream`` methods.
    Subclasses supply provider-specific hooks:

    API key auto-discovery contract
    --------------------------------
    Subclasses SHOULD honour environment-variable auto-discovery for API keys
    in ``__init__`` (e.g. ``api_key = api_key or os.environ.get("OPENAI_API_KEY", "")``).
    The canonical env-var names currently in use:

    * ``OPENAI_API_KEY`` (OpenAIProvider only — adapters that reuse the
      OpenAI protocol against third-party endpoints, e.g.
      ``OpenAICompatProvider`` / ``GLMProvider``, must opt out via
      ``_ALLOW_OPENAI_ENV_KEY = False`` so the key never leaks to another
      host as a bearer credential)
    * ``GOOGLE_API_KEY`` or ``GEMINI_API_KEY`` (GeminiProvider)
    * ``ANTHROPIC_API_KEY`` and ``CLAUDE_CODE_OAUTH_TOKEN`` (AnthropicProvider —
      handled by ``resolve_anthropic_auth`` which also walks keychain)
    * MiniMaxProvider does not auto-discover — requires explicit ``api_key`` arg
    * OllamaProvider does not require a key (defaults to literal ``"ollama"``)

    New subclasses should follow this pattern and document any new env-var
    names in this contract.

    * ``_format_messages`` -- convert internal ``Message`` list to wire format
    * ``_build_request_kwargs`` -- assemble the full request dict
    * ``_call_api`` -- invoke the SDK and return the raw response
    * ``_normalize_response`` -- convert the raw response to a ``Message``
    * ``_handle_api_error`` -- translate SDK exceptions to Rikugan errors
    * ``_stream_chunks`` -- yield ``StreamChunk`` objects from the provider stream

    Subclasses must also implement: ``name``, ``capabilities``,
    ``_get_client``, ``_fetch_models_live``, ``_builtin_models``.
    """

    def __init__(self, api_key: str = "", api_base: str = "", model: str = ""):
        self.api_key = api_key
        self.api_base = api_base
        self.model = model
        self._client: Any = None

    # -- Abstract interface ----------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name (e.g. 'anthropic', 'openai')."""
        ...

    @property
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """Provider capabilities."""
        ...

    @abstractmethod
    def _get_client(self) -> Any:
        """Return the SDK client, creating it lazily if needed."""
        ...

    @abstractmethod
    def _fetch_models_live(self) -> list[ModelInfo]:
        """Fetch models from the remote API. May raise on failure."""
        ...

    @staticmethod
    @abstractmethod
    def _builtin_models() -> list[ModelInfo]:
        """Return built-in fallback model list (no network required)."""
        ...

    # -- Translation pipeline hooks (abstract) ---------------------------------

    @abstractmethod
    def _format_messages(self, messages: list[Message]) -> Any:
        """Convert internal messages to provider wire format."""

    @abstractmethod
    def _build_request_kwargs(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        max_tokens: int,
        system: str,
        *,
        request_context: LLMRequestContext | None = None,
    ) -> dict[str, Any]:
        """Assemble the full request kwargs for the provider SDK call.

        ``request_context`` is keyword-only and is forwarded by
        :meth:`chat` / :meth:`chat_stream` so providers can adapt the
        payload to attempt-local state (e.g. GLM one-shot recovery).
        Non-GLM providers MUST treat the context as a pure pass-through
        — adding it MUST NOT change the wire payload.
        """

    @abstractmethod
    def _call_api(self, client: Any, kwargs: dict[str, Any]) -> Any:
        """Invoke the provider SDK and return the raw response object."""

    @abstractmethod
    def _normalize_response(self, raw: Any) -> Message:
        """Convert provider response to internal Message."""

    @abstractmethod
    def _handle_api_error(self, e: Exception) -> NoReturn:
        """Translate a provider SDK exception into a Rikugan error."""

    @abstractmethod
    def _stream_chunks(
        self,
        client: Any,
        kwargs: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> Generator[StreamChunk, None, None]:
        """Yield ``StreamChunk`` objects from the provider's streaming API.

        Receives the same kwargs produced by ``_build_request_kwargs``.
        The implementation may modify *kwargs* (e.g. add ``stream=True``)
        before passing them to the SDK.

        ``cancel_event`` (optional) — if set, the implementation MUST
        force-close the underlying HTTP stream within ~100ms so the
        consumer's cancellation check fires promptly instead of waiting
        for the next SSE chunk.
        """

    # -- Concrete pipeline implementations -------------------------------------
    #
    # Both ``chat`` and ``chat_stream`` derive the ``system`` string and
    # ``max_tokens`` int that flow into ``_build_request_kwargs`` from the
    # optional ``LLMRequestContext``.  The merge happens here so the
    # provider override sees a single, pre-resolved pair whether a context
    # was supplied or not.
    #
    # Suffix separator policy
    # -----------------------
    # When the context supplies a ``system_suffix``, the base pipeline
    # joins it onto ``safe_system`` with exactly ``"\n\n"`` so the result
    # is a clean Markdown paragraph break — but ONLY when both halves are
    # non-empty.  Concretely:
    #
    # * safe_system = "x", suffix = "y"  -> "x\n\ny"
    # * safe_system = "",   suffix = "y"  -> "y"          (suffix alone)
    # * safe_system = "x", suffix = ""    -> "x"          (system alone)
    # * safe_system = "",   suffix = ""   -> ""           (both empty)
    #
    # This keeps non-GLM providers that supply neither value identical to
    # the pre-context wire payload (effective_system == safe_system == "").
    #
    # Max tokens override
    # -------------------
    # ``context.max_tokens_override`` is treated as a strict override:
    # ``None`` (or omitted) means "use the caller's ``max_tokens``", any
    # positive int replaces it.  A 0 or negative override is invalid — the
    # plan/global config minimum is 1, so 0 cannot mean "produce nothing"
    # and a negative value would obviously never be honoured.  The pipeline
    # raises ``ValueError`` with a clear message before building kwargs so
    # the failure happens at the LLM call boundary, not deep inside an
    # SDK's HTTP serialization layer.

    _SUFFIX_SEPARATOR = "\n\n"

    @classmethod
    def _resolve_effective_kwargs(
        cls,
        request_context: LLMRequestContext | None,
        max_tokens: int,
        system: str,
    ) -> tuple[str, int, LLMRequestContext]:
        """Derive ``(effective_system, effective_max_tokens, context)``.

        See the class-level comment above for the full separator /
        override policy.  This helper is the single source of truth so
        ``chat`` and ``chat_stream`` cannot drift.
        """
        context = request_context or LLMRequestContext()
        safe_system = strip_lone_surrogates(system) if system else system

        # Suffix separator: insert ``"\n\n"`` only when both halves are
        # non-empty; suffix alone or system alone are passed through.
        suffix = context.system_suffix
        if suffix and safe_system:
            effective_system = safe_system + cls._SUFFIX_SEPARATOR + suffix
        else:
            effective_system = safe_system + suffix  # suffix alone / system alone / both empty

        # Explicit None handling: ``None`` (or absent) means "use the
        # caller's ``max_tokens``".  A 0 or negative override is invalid
        # and rejected with a clear message — the plan/global config
        # minimum is 1, so 0 cannot mean "produce nothing".
        if context.max_tokens_override is None:
            effective_max_tokens = max_tokens
        else:
            if context.max_tokens_override <= 0:
                raise ValueError(
                    f"LLMRequestContext.max_tokens_override must be a positive "
                    f"integer (got {context.max_tokens_override!r}); the plan/"
                    f"global config minimum is 1 and 0 cannot mean 'produce "
                    f"nothing'. Pass `None` to use the caller's `max_tokens` "
                    f"instead."
                )
            effective_max_tokens = context.max_tokens_override

        return effective_system, effective_max_tokens, context

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        system: str = "",
        *,
        request_context: LLMRequestContext | None = None,
    ) -> Message:
        """Non-streaming chat completion.

        Orchestrates the standard pipeline:
        get client -> build kwargs -> call API -> normalize response.

        Lone surrogates (U+D800-DFFF) are stripped from messages and the
        system prompt before the request kwargs are built — otherwise the
        provider SDK's HTTP body encoding (``str.encode('utf-8')``) raises
        ``UnicodeEncodeError: surrogates not allowed`` and aborts the turn.
        See :func:`rikugan.core.sanitize.sanitize_messages_for_provider`.

        ``request_context`` (optional) carries attempt-local state
        (attempt number, recovery flag, system suffix, max_tokens override,
        disable_thinking).  The effective ``system`` and ``max_tokens``
        passed to :meth:`_build_request_kwargs` are derived from the
        context here so non-GLM providers receive identical kwargs when no
        context is supplied.  See :meth:`_resolve_effective_kwargs` for
        the separator / override policy.
        """
        client = self._get_client()
        safe_messages = sanitize_messages_for_provider(messages)
        effective_system, effective_max_tokens, context = self._resolve_effective_kwargs(
            request_context, max_tokens, system
        )
        kwargs = self._build_request_kwargs(
            safe_messages,
            tools,
            temperature,
            effective_max_tokens,
            effective_system,
            request_context=context,
        )
        silence_sdk_debug_loggers()
        try:
            raw = self._call_api(client, kwargs)
        except Exception as e:
            self._handle_api_error(e)
        return self._normalize_response(raw)

    def chat_stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        system: str = "",
        cancel_event: threading.Event | None = None,
        *,
        request_context: LLMRequestContext | None = None,
    ) -> Generator[StreamChunk, None, None]:
        """Streaming chat completion.

        Builds request kwargs then delegates to ``_stream_chunks`` for the
        provider-specific streaming state machine.

        ``cancel_event`` (optional) is a ``threading.Event`` the caller can
        set to interrupt a slow HTTP stream. When set, the provider force-closes
        the underlying connection so the consumer's cancellation check fires
        within ~100ms instead of waiting for the next SSE chunk.

        Lone surrogates are stripped from messages and the system prompt
        before serialization (see ``chat`` docstring for rationale).

        ``request_context`` (optional) is forwarded to
        :meth:`_build_request_kwargs` so providers can adapt the payload
        to attempt-local state (e.g. GLM one-shot recovery).  Effective
        ``system`` and ``max_tokens`` are derived here; non-GLM providers
        receive identical kwargs when no context is supplied.  See
        :meth:`_resolve_effective_kwargs` for the separator / override
        policy.
        """
        client = self._get_client()
        safe_messages = sanitize_messages_for_provider(messages)
        effective_system, effective_max_tokens, context = self._resolve_effective_kwargs(
            request_context, max_tokens, system
        )
        # Mark the context as streaming so providers can gate
        # transport-only wire fields (e.g. GLM ``tool_stream``) — the
        # field must not appear on non-streaming ``chat()`` calls.
        context = dataclass_replace(context, streaming=True)
        kwargs = self._build_request_kwargs(
            safe_messages,
            tools,
            temperature,
            effective_max_tokens,
            effective_system,
            request_context=context,
        )
        silence_sdk_debug_loggers()
        yield from self._stream_chunks(client, kwargs, cancel_event=cancel_event)

    # -- Streaming cancel watchdog ---------------------------------------------

    def _spawn_cancel_watchdog(
        self,
        cancel_event: threading.Event | None,
        stream_ref: list,
        stream_ready: threading.Event,
    ) -> threading.Event:
        """Spawn the per-request cancel watchdog; return its ``done`` event.

        When ``cancel_event`` fires while the stream is blocked, the watchdog
        force-closes the stream (``stream_ref[0]``, valid once
        ``stream_ready`` is set) so the consumer's cancellation check fires
        promptly instead of waiting for the next SSE chunk.

        The returned ``done`` event is per-request: the caller MUST set it in
        a ``finally:`` when the stream finishes.  Without it the watchdog
        would park forever on the long-lived loop cancel event — one leaked
        daemon thread per streaming request.

        Returns ``done`` even when ``cancel_event`` is None (no thread is
        spawned) so callers can set it unconditionally.
        """
        done = threading.Event()

        def _watchdog() -> None:
            if cancel_event is None:  # unreachable: only spawned when set
                return
            # Park until either this request finishes (``done``) or cancel
            # fires.  ``cancel_event.wait`` wakes immediately when the event
            # is set, so a blocked stream is still interrupted promptly; the
            # short poll timeout only bounds how soon a finished request
            # releases the thread.
            while not done.is_set():
                if cancel_event.wait(timeout=_WATCHDOG_POLL_INTERVAL):
                    break
            if done.is_set():
                return
            if not stream_ready.wait(timeout=2.0):
                return
            stream = stream_ref[0] if stream_ref else None
            if stream is None:
                return
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    log_debug(f"{type(self).__name__} stream.close() during cancel failed: {exc}")

        if cancel_event is not None:
            threading.Thread(target=_watchdog, daemon=True, name=f"{self.name}-cancel-watchdog").start()
        return done

    # -- Concrete shared implementations ---------------------------------------

    def list_models(self) -> list[ModelInfo]:
        """List available models.

        Attempts a live API fetch via ``_fetch_models_live()``.  On any
        failure, logs the error and returns ``_builtin_models()`` so callers
        never see an exception.
        """
        try:
            return self._fetch_models_live()
        except Exception as exc:
            log_debug(f"{self.name} list_models failed, using builtins: {exc}")
            return self._builtin_models()

    def ensure_ready(self) -> None:
        """Pre-initialize the provider (imports, client objects, etc.).

        Temporarily bypasses Shiboken's ``__import__`` hook during SDK
        import to prevent UAF crashes in IDA Pro (Python > 3.10).
        PySide6 modules are already loaded by IDA's own UI, so using
        ``importlib.__import__`` (CPython's standard import) during this
        window is safe — SDK packages and their C-extension dependencies
        (httpx, h2, ssl, ...) do not need Shiboken type wrapping.

        MUST be called on the main thread before handing the provider to a
        background thread.  Python 3.14 crashes when heavy C-extension
        packages (httpx, h2, ssl ...) are first imported from a non-main
        thread, so providers that lazy-import SDK packages override
        ``_init_client`` to force the import on the caller's thread.
        """
        import builtins
        import importlib

        saved_import = builtins.__import__
        builtins.__import__ = importlib.__import__
        try:
            self._init_client()
        finally:
            builtins.__import__ = saved_import

    def _init_client(self) -> None:
        """Pre-import SDK and create client. Delegates to ``_get_client()``."""
        self._get_client()

    def auth_status(self) -> tuple[str, str]:
        """Return (label, status_type) describing the current auth state.

        status_type is one of: "ok", "error", "none".
        Subclasses override for provider-specific logic (e.g. OAuth detection).
        """
        if self.api_key:
            return "API Key", "ok"
        return "", "none"

    def validate_key(self) -> bool:
        """Probe whether current credentials can reach the API.

        Calls ``_fetch_models_live()`` directly (bypassing the fallback
        in ``list_models()``) so that authentication errors are surfaced
        rather than silently masked by built-in model lists.
        """
        try:
            self._fetch_models_live()
            return True
        except Exception as e:
            log_debug(f"validate_key failed for {self.name}: {e}")
            return False
