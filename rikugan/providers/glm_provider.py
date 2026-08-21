"""GLM dialect provider for Z.AI's GLM-5.x / GLM-4.7 family.

Task 6 of the GLM reasoning resilience plan.  This provider speaks the
OpenAI Chat Completions wire protocol against Z.AI's endpoint but adapts
three things that the plain OpenAI adapter cannot express:

1. **Wire messages** include ``reasoning_content`` on assistant turns so
   the GLM endpoint sees prior thinking context on multi-turn
   conversations.  OpenAI omits this field entirely.
2. **Request kwargs** carry GLM-specific extensions under
   ``extra_body``: ``thinking`` (enable/disable/effort, with
   ``clear_thinking`` polarity), optional ``tool_stream`` flag (streaming
   only, tools-only), and optional ``reasoning_effort``.  These fields
   are derived exclusively from :mod:`rikugan.core.glm_config` metadata
   so unknown model IDs do not trigger upstream 400s.
3. **Capabilities** advertise ``reasoning_content=True`` so the inherited
   ``_iter_stream_chunks`` yields ``reasoning_delta`` chunks instead of
   inlining ``<think>`` tags into the visible text channel.

The stream state machine (tool-call id repair, late-id argument replay,
cumulative-usage dedup) is inherited unchanged from
:class:`OpenAIProvider` — GLM does not copy or override it.

Spec reference: ``docs/superpowers/specs/2026-07-22-glm-reasoning-resilience-design.md``
sections 6.4, 7.1, 9.2, 12.1, 12.4.
"""

from __future__ import annotations

import importlib
import json
from typing import Any

from ..core.errors import ProviderError
from ..core.glm_config import (
    GLM_DIALECT,
    GLMConfig,
    GLMModelMetadata,
    get_glm_model_metadata,
    parse_glm_extra,
)
from ..core.types import (
    LLMRequestContext,
    Message,
    ModelInfo,
    ProviderCapabilities,
    Role,
    TokenUsage,
    ToolCall,
)
from .openai_provider import OpenAIProvider, _format_openai_messages


class GLMProvider(OpenAIProvider):
    """Provider for Z.AI's GLM-5.x / GLM-4.7 models.

    Inherits the OpenAI stream state machine (tool-call lifecycle, usage
    accumulation, cancellation watchdog) unchanged.  Only the wire-format
    hooks that need GLM-specific fields are overridden.
    """

    def __init__(
        self,
        api_key: str = "",
        api_base: str = "",
        model: str = "glm-5.2",
        provider_name: str = "glm",
        extra: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(api_key=api_key, api_base=api_base, model=model, **kwargs)
        self._provider_name = provider_name
        # Snapshot the raw extra dict so ProviderRegistry.get_or_create can
        # deep-compare it on subsequent calls and force a refresh when GLM
        # thinking/guard settings change without a credential change.
        import copy as _copy

        self._provider_extra_raw: dict[str, Any] | None = _copy.deepcopy(extra) if extra else None
        # Parse and validate the GLM extra dict.  An empty / missing extra
        # defaults to the GLM dialect so the built-in ``glm`` entry works
        # without explicit config.
        self._glm_config: GLMConfig = parse_glm_extra(extra or {"dialect": GLM_DIALECT}, model)
        self._glm_metadata: GLMModelMetadata = get_glm_model_metadata(model)

    # -- Identity -----------------------------------------------------------

    @property
    def name(self) -> str:
        return self._provider_name

    @property
    def glm_config(self) -> GLMConfig:
        """The parsed GLM dialect config snapshot.

        Exposed so callers (the agent loop, recovery builder) can
        consume the already-validated :class:`GLMConfig` instead of
        re-parsing ``provider.extra`` on every turn. Re-parsing would
        re-validate user-saved values and could surface
        :class:`ValueError` after the provider was already
        constructed — invalid config now raises at ``__init__`` time
        and this property is a cheap read.
        """
        return self._glm_config

    # -- Client construction ------------------------------------------------

    def _get_client(self) -> Any:
        """Build the OpenAI SDK client against the GLM endpoint.

        Mirrors :class:`OpenAICompatProvider`'s custom-base behavior: a
        custom endpoint without an explicit key falls back to the
        ``"no-key"`` placeholder so the SDK does not read
        ``OPENAI_API_KEY`` from the environment.  Timeout is kept at 120
        seconds (same as the base OpenAI provider).

        Base URL resolution: if the caller set an explicit ``api_base``, it
        wins (user override).  Otherwise the endpoint type from the GLM
        config picks the correct Z.AI base URL — ``standard`` vs
        ``coding_plan`` — because the two endpoints require
        non-interchangeable API keys and route to different Z.AI backends.
        """
        if self._client is None:
            try:
                openai = importlib.import_module("openai")
            except ImportError as exc:
                raise ProviderError(
                    "openai package not installed. Run: pip install openai",
                    provider=self._provider_name,
                ) from exc
            client_kwargs: dict[str, Any] = {"timeout": 120.0}
            effective_base = self.api_base or self._glm_config.base_url
            if self.api_key:
                client_kwargs["api_key"] = self.api_key
            elif effective_base:
                # Custom endpoint without explicit key — use a placeholder
                # to prevent the SDK from reading OPENAI_API_KEY env var.
                client_kwargs["api_key"] = "no-key"
            else:
                client_kwargs["api_key"] = self.api_key
            if effective_base:
                client_kwargs["base_url"] = effective_base
            self._client = openai.OpenAI(**client_kwargs)
        return self._client

    # -- Capabilities & model list -----------------------------------------

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Derive capability flags from the active model's metadata.

        Unknown GLM model IDs inherit conservative defaults: reasoning
        content is still assumed (the user picked GLM), but streamed tool
        arguments and ``reasoning_effort`` are omitted to avoid
        unsupported-parameter failures (spec §12.4).
        """
        meta = self._glm_metadata
        return ProviderCapabilities(
            streaming=True,
            tool_use=True,
            vision=False,
            max_context_window=meta.context_window,
            max_output_tokens=meta.max_output_tokens,
            supports_system_prompt=True,
            reasoning_content=meta.reasoning_content,
            streaming_tool_calls=meta.streaming_tool_calls,
            reasoning_effort=meta.reasoning_effort,
        )

    @staticmethod
    def _builtin_models() -> list[ModelInfo]:
        from ..core.glm_config import (
            KNOWN_GLM_5_2_CONTEXT_WINDOW,
            KNOWN_GLM_CONTEXT_WINDOW,
            KNOWN_GLM_MAX_OUTPUT_TOKENS,
        )

        return [
            ModelInfo(
                id="glm-5.2",
                name="GLM-5.2",
                provider="glm",
                context_window=KNOWN_GLM_5_2_CONTEXT_WINDOW,
                max_output_tokens=KNOWN_GLM_MAX_OUTPUT_TOKENS,
                supports_tools=True,
                supports_vision=False,
            ),
            ModelInfo(
                id="glm-5.1",
                name="GLM-5.1",
                provider="glm",
                context_window=KNOWN_GLM_CONTEXT_WINDOW,
                max_output_tokens=KNOWN_GLM_MAX_OUTPUT_TOKENS,
                supports_tools=True,
                supports_vision=False,
            ),
            ModelInfo(
                id="glm-5",
                name="GLM-5",
                provider="glm",
                context_window=KNOWN_GLM_CONTEXT_WINDOW,
                max_output_tokens=KNOWN_GLM_MAX_OUTPUT_TOKENS,
                supports_tools=True,
                supports_vision=False,
            ),
            ModelInfo(
                id="glm-4.7",
                name="GLM-4.7",
                provider="glm",
                context_window=KNOWN_GLM_CONTEXT_WINDOW,
                max_output_tokens=KNOWN_GLM_MAX_OUTPUT_TOKENS,
                supports_tools=True,
                supports_vision=False,
            ),
        ]

    # -- Wire formatting ----------------------------------------------------

    def _format_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Format messages with ``reasoning_content`` on assistant turns.

        Delegates to :func:`_format_openai_messages` with
        ``include_reasoning_content=True`` so the tool-ID repair logic is
        shared with the OpenAI provider (no duplication).
        """
        return _format_openai_messages(messages, include_reasoning_content=True)

    def _normalize_response(self, response: Any) -> Message:
        """Parse a non-streaming GLM response.

        Unlike :meth:`OpenAIProvider._normalize_response` which inlines
        reasoning into ``content`` wrapped in ``<think>`` tags, GLM puts
        the reasoning trace into :attr:`Message.reasoning_content` and
        keeps the visible text separate in :attr:`Message.content`.
        """
        choice = response.choices[0]
        rm = choice.message

        tool_calls: list[ToolCall] = []
        if rm.tool_calls:
            for tc in rm.tool_calls:
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=json.loads(tc.function.arguments),
                    )
                )

        usage = TokenUsage()
        if response.usage:
            usage = TokenUsage(
                prompt_tokens=getattr(response.usage, "prompt_tokens", 0),
                completion_tokens=getattr(response.usage, "completion_tokens", 0),
                total_tokens=getattr(response.usage, "total_tokens", 0),
            )

        text = rm.content or ""
        reasoning = getattr(rm, "reasoning_content", None) or ""

        return Message(
            role=Role.ASSISTANT,
            content=text,
            reasoning_content=reasoning,
            tool_calls=tool_calls,
            token_usage=usage,
        )

    # -- Request kwargs -----------------------------------------------------

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
        """Build kwargs with GLM-specific ``extra_body`` extensions.

        The ``extra_body`` fields are derived exclusively from GLM metadata
        so unknown model IDs do not trigger upstream 400s on unsupported
        parameters (spec §12.4):

        * ``thinking`` — ``{"type": "enabled", "clear_thinking": bool}``
          normally; ``{"type": "disabled"}`` when ``disable_thinking`` is
          set.  ``clear_thinking`` is the inverse of the user-saved
          ``preserve`` option (``clear_thinking = not preserve``).
        * ``reasoning_effort`` — sent only when the model advertises it.
        * ``tool_stream`` — sent only when the model advertises streamed
          tool arguments, the request is streaming (``request_context.streaming``
          is True), and ``tools`` is non-empty.  Sending this field on a
          non-streaming ``chat()`` call would be rejected by the upstream
          endpoint.
        """
        msgs: list[dict[str, Any]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(self._format_messages(messages))

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools

        extra_body: dict[str, Any] = {}

        # Thinking enable/disable.  ``disable_thinking`` is set on
        # one-shot recovery requests so the GLM endpoint skips reasoning
        # entirely and produces visible output within the recovery token
        # budget.
        thinking = self._glm_config.thinking
        if request_context is not None and request_context.disable_thinking:
            extra_body["thinking"] = {"type": "disabled"}
        elif thinking.enabled:
            extra_body["thinking"] = {
                "type": "enabled",
                # ``clear_thinking`` is the inverse of ``preserve``: when
                # the user wants to preserve thinking context
                # (preserve=True), the endpoint should NOT clear it
                # (clear_thinking=False).
                "clear_thinking": not thinking.preserve,
            }
        else:
            extra_body["thinking"] = {"type": "disabled"}

        # ``reasoning_effort`` — only on models that advertise it.
        if self._glm_metadata.reasoning_effort:
            if request_context is not None and request_context.disable_thinking:
                extra_body["reasoning_effort"] = "none"
            else:
                extra_body["reasoning_effort"] = thinking.reasoning_effort

        # ``tool_stream`` — only on models that advertise streamed tool
        # arguments, AND the request is streaming, AND tools are non-empty.
        # The wire key is ``tool_stream`` (per Z.AI API), not
        # ``streaming_tool_calls``.
        is_streaming = request_context is not None and request_context.streaming
        if self._glm_metadata.streaming_tool_calls and is_streaming and tools:
            extra_body["tool_stream"] = True

        if extra_body:
            kwargs["extra_body"] = extra_body

        return kwargs
