"""Tests for the GLM provider dialect (Task 6).

Covers:

* ``GLMProvider`` replays ``reasoning_content`` as its own wire field, never
  inlined into ``content`` with ``<think>`` tags.
* Recovery requests disable thinking via ``extra_body.thinking.type`` and
  ``extra_body.reasoning_effort``.
* Unknown GLM model IDs omit ``tool_stream`` and ``reasoning_effort`` so the
  upstream endpoint does not 400 on unsupported parameters.
* ``tool_stream`` wire key (not ``streaming_tool_calls``) is emitted only
  when tools are non-empty AND the request is streaming.
* ``thinking.clear_thinking`` polarity is the inverse of ``preserve``.
* Non-streaming ``_normalize_response`` puts reasoning in
  ``Message.reasoning_content``, visible text in ``Message.content``.
* Stream chunks yield ``reasoning_delta`` when the provider capability
  ``reasoning_content`` is True; the base OpenAI provider still yields
  inline ``<think>`` text when the capability is False.
* Registry resolves the built-in ``glm`` name and custom GLM-dialect names
  to :class:`GLMProvider`.
* Registry cache refreshes when ``extra`` changes.
* Registry dialect flip (GLM -> compat) after class resolution.
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from tests import purge_rikugan_stubs

purge_rikugan_stubs()

from rikugan.core.types import (
    LLMRequestContext,
    Message,
    Role,
    ToolCall,
)

# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def test_glm_replays_reasoning_content_without_think_tags():
    """GLM must send ``reasoning_content`` as its own wire field, never
    inlined into ``content`` with ``<think>`` tags.
    """
    from rikugan.providers.glm_provider import GLMProvider

    provider = GLMProvider(api_key="test", api_base="https://api.z.ai/api/paas/v4", model="glm-5.2")
    message = Message(
        role=Role.ASSISTANT,
        content="Visible",
        reasoning_content="Reasoning",
        tool_calls=[ToolCall(id="call_1", name="read_bytes", arguments={"address": 4096})],
    )

    wire = provider._format_messages([message])[0]

    assert wire["reasoning_content"] == "Reasoning"
    assert wire["content"] == "Visible"
    assert "<think>" not in wire["content"]


def test_glm_recovery_request_disables_thinking():
    """When ``LLMRequestContext.disable_thinking`` is True the request must
    emit ``extra_body.thinking.type = "disabled"`` and
    ``extra_body.reasoning_effort = "none"``.
    """
    from rikugan.providers.glm_provider import GLMProvider

    provider = GLMProvider(api_key="test", model="glm-5.2", extra={"dialect": "glm"})
    context = LLMRequestContext(
        attempt_number=2,
        recovery=True,
        max_tokens_override=16_384,
        disable_thinking=True,
        streaming=True,
    )

    kwargs = provider._build_request_kwargs([], [], 0.3, 16_384, "system", request_context=context)

    assert kwargs["extra_body"]["thinking"]["type"] == "disabled"
    assert kwargs["extra_body"]["reasoning_effort"] == "none"
    assert kwargs["max_tokens"] == 16_384


def test_unknown_glm_model_omits_tool_stream_and_reasoning_effort():
    """Unknown GLM model IDs must omit ``tool_stream`` and
    ``reasoning_effort`` from the wire payload because the upstream endpoint
    may reject unsupported parameters.
    """
    from rikugan.providers.glm_provider import GLMProvider

    provider = GLMProvider(api_key="test", model="glm-experimental", extra={"dialect": "glm"})
    context = LLMRequestContext(streaming=True)

    kwargs = provider._build_request_kwargs([], [{"type": "function"}], 0.3, 4096, "", request_context=context)

    assert "tool_stream" not in kwargs["extra_body"]
    assert "streaming_tool_calls" not in kwargs["extra_body"]
    assert "reasoning_effort" not in kwargs["extra_body"]


def test_glm_known_model_includes_tool_stream_and_reasoning_effort():
    """Known GLM model IDs (``glm-5.2``) include ``tool_stream`` and
    ``reasoning_effort`` when streaming with tools.
    """
    from rikugan.providers.glm_provider import GLMProvider

    provider = GLMProvider(api_key="test", model="glm-5.2", extra={"dialect": "glm"})
    context = LLMRequestContext(streaming=True)

    kwargs = provider._build_request_kwargs([], [{"type": "function"}], 0.3, 4096, "", request_context=context)

    assert kwargs["extra_body"]["tool_stream"] is True
    assert "reasoning_effort" in kwargs["extra_body"]


# ---------------------------------------------------------------------------
# Finding 1: tool_stream wire key, stream-only, tools-only
# ---------------------------------------------------------------------------


def test_tool_stream_uses_exact_wire_key_not_streaming_tool_calls():
    """The wire key under ``extra_body`` must be exactly ``tool_stream``,
    never ``streaming_tool_calls``.
    """
    from rikugan.providers.glm_provider import GLMProvider

    provider = GLMProvider(api_key="test", model="glm-5.2", extra={"dialect": "glm"})
    context = LLMRequestContext(streaming=True)

    kwargs = provider._build_request_kwargs([], [{"type": "function"}], 0.3, 4096, "", request_context=context)

    assert "tool_stream" in kwargs["extra_body"]
    assert kwargs["extra_body"]["tool_stream"] is True
    assert "streaming_tool_calls" not in kwargs["extra_body"]


def test_tool_stream_omitted_when_not_streaming():
    """``tool_stream`` must NOT be sent on non-streaming ``chat()`` calls.
    The upstream endpoint rejects this parameter outside streaming mode.
    """
    from rikugan.providers.glm_provider import GLMProvider

    provider = GLMProvider(api_key="test", model="glm-5.2", extra={"dialect": "glm"})
    # Non-streaming context (streaming defaults to False).
    context = LLMRequestContext(streaming=False)

    kwargs = provider._build_request_kwargs([], [{"type": "function"}], 0.3, 4096, "", request_context=context)

    assert "tool_stream" not in kwargs.get("extra_body", {})


def test_tool_stream_omitted_when_no_context():
    """Without a request context (direct ``_build_request_kwargs`` call,
    e.g. from ``chat()``), ``tool_stream`` must not appear.
    """
    from rikugan.providers.glm_provider import GLMProvider

    provider = GLMProvider(api_key="test", model="glm-5.2", extra={"dialect": "glm"})

    kwargs = provider._build_request_kwargs([], [{"type": "function"}], 0.3, 4096, "")

    assert "tool_stream" not in kwargs.get("extra_body", {})


def test_tool_stream_omitted_when_tools_empty():
    """``tool_stream`` must NOT be sent when ``tools`` is empty, even in
    streaming mode.
    """
    from rikugan.providers.glm_provider import GLMProvider

    provider = GLMProvider(api_key="test", model="glm-5.2", extra={"dialect": "glm"})
    context = LLMRequestContext(streaming=True)

    kwargs = provider._build_request_kwargs([], [], 0.3, 4096, "", request_context=context)

    assert "tool_stream" not in kwargs["extra_body"]


def test_non_streaming_request_has_no_tool_stream_but_has_thinking():
    """A non-streaming GLM request must carry ``thinking`` and
    ``reasoning_effort`` (those are transport-independent) but must NOT
    carry ``tool_stream`` (transport-only).
    """
    from rikugan.providers.glm_provider import GLMProvider

    provider = GLMProvider(api_key="test", model="glm-5.2", extra={"dialect": "glm"})

    # No request_context — simulates non-streaming chat() call.
    kwargs = provider._build_request_kwargs([], [{"type": "function"}], 0.3, 4096, "")

    assert "tool_stream" not in kwargs.get("extra_body", {})
    assert kwargs["extra_body"]["thinking"]["type"] == "enabled"
    assert "reasoning_effort" in kwargs["extra_body"]


# ---------------------------------------------------------------------------
# Finding 2: clear_thinking polarity
# ---------------------------------------------------------------------------


def test_thinking_clear_thinking_is_inverse_of_preserve():
    """When ``preserve=True`` (default), ``clear_thinking`` must be False.
    When ``preserve=False``, ``clear_thinking`` must be True.
    """
    from rikugan.providers.glm_provider import GLMProvider

    # Default config: preserve=True -> clear_thinking=False
    provider_default = GLMProvider(api_key="test", model="glm-5.2", extra={"dialect": "glm"})
    kwargs = provider_default._build_request_kwargs([], [], 0.3, 4096, "")
    assert kwargs["extra_body"]["thinking"]["clear_thinking"] is False

    # preserve=False -> clear_thinking=True
    provider_no_preserve = GLMProvider(
        api_key="test",
        model="glm-5.2",
        extra={"dialect": "glm", "thinking": {"preserve": False}},
    )
    kwargs_np = provider_no_preserve._build_request_kwargs([], [], 0.3, 4096, "")
    assert kwargs_np["extra_body"]["thinking"]["clear_thinking"] is True


def test_thinking_wire_body_has_no_preserve_key():
    """The wire body must use ``clear_thinking``, never ``preserve``."""
    from rikugan.providers.glm_provider import GLMProvider

    provider = GLMProvider(api_key="test", model="glm-5.2", extra={"dialect": "glm"})
    kwargs = provider._build_request_kwargs([], [], 0.3, 4096, "")

    assert "preserve" not in kwargs["extra_body"]["thinking"]
    assert "clear_thinking" in kwargs["extra_body"]["thinking"]


# ---------------------------------------------------------------------------
# Finding 3: non-stream _normalize_response separates reasoning
# ---------------------------------------------------------------------------


def test_glm_normalize_response_separates_reasoning_from_content():
    """GLM non-streaming ``_normalize_response`` must put reasoning into
    ``Message.reasoning_content`` and keep visible text in
    ``Message.content`` — no ``<think>`` tags inlined.
    """
    from rikugan.providers.glm_provider import GLMProvider

    provider = GLMProvider(api_key="test", model="glm-5.2")
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="Visible answer",
                    reasoning_content="Hidden reasoning",
                    tool_calls=None,
                ),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )

    msg = provider._normalize_response(response)

    assert msg.content == "Visible answer"
    assert msg.reasoning_content == "Hidden reasoning"
    assert "<think>" not in msg.content


def test_glm_normalize_response_no_reasoning():
    """When the response has no ``reasoning_content``, the message's
    ``reasoning_content`` field stays empty (no ``<think>`` wrapping)."""
    from rikugan.providers.glm_provider import GLMProvider

    provider = GLMProvider(api_key="test", model="glm-5.2")
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="Just text", reasoning_content=None, tool_calls=None),
            )
        ],
        usage=None,
    )

    msg = provider._normalize_response(response)
    assert msg.content == "Just text"
    assert msg.reasoning_content == ""


# ---------------------------------------------------------------------------
# Identity & capabilities
# ---------------------------------------------------------------------------


def test_glm_provider_name_is_glm():
    """The built-in GLM provider advertises ``name == "glm"``."""
    from rikugan.providers.glm_provider import GLMProvider

    provider = GLMProvider(api_key="test", model="glm-5.2")
    assert provider.name == "glm"


def test_glm_custom_provider_name_preserved():
    """A custom GLM-dialect provider preserves its custom name."""
    from rikugan.providers.glm_provider import GLMProvider

    provider = GLMProvider(
        api_key="test",
        model="glm-5.2",
        provider_name="glm-coding",
        extra={"dialect": "glm"},
    )
    assert provider.name == "glm-coding"


def test_glm_capabilities_advertise_reasoning_content():
    """The GLM provider advertises ``reasoning_content=True`` so the stream
    parser yields ``reasoning_delta`` chunks instead of inlining
    ``<think>`` tags.
    """
    from rikugan.providers.glm_provider import GLMProvider

    provider = GLMProvider(api_key="test", model="glm-5.2")
    caps = provider.capabilities
    assert caps.reasoning_content is True


# ---------------------------------------------------------------------------
# Stream chunk reasoning delta gating
# ---------------------------------------------------------------------------


def _reasoning_delta_chunk(reasoning: str, *, content: str = "", finish_reason: str | None = None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=content or None,
                    reasoning_content=reasoning,
                    tool_calls=None,
                ),
                finish_reason=finish_reason,
            )
        ],
        usage=None,
    )


def test_glm_stream_yields_reasoning_delta():
    """GLM provider yields ``reasoning_delta`` chunks for reasoning content
    — never inline ``<think>`` tags.
    """
    from rikugan.providers.glm_provider import GLMProvider

    provider = GLMProvider(api_key="test", model="glm-5.2")
    chunks = [
        _reasoning_delta_chunk("thinking...", content=""),
        _reasoning_delta_chunk("more thinking", content="Visible"),
        _reasoning_delta_chunk("", content="end", finish_reason="stop"),
    ]
    emitted = list(provider._iter_stream_chunks(iter(chunks)))

    reasoning_deltas = [c.reasoning_delta for c in emitted if c.reasoning_delta]
    assert reasoning_deltas == ["thinking...", "more thinking"]
    text_chunks = [c.text for c in emitted if c.text]
    assert "<think>" not in text_chunks
    assert "</think>" not in text_chunks


def test_openai_stream_still_yields_inline_think_tags():
    """Non-GLM OpenAI provider (``reasoning_content`` capability False)
    must still inline reasoning content as ``<think>`` tags so existing
    OpenAI o-series behavior is unchanged.
    """
    from rikugan.providers.openai_provider import OpenAIProvider

    provider = OpenAIProvider(api_key="test", model="o3-mini")
    assert provider.capabilities.reasoning_content is False

    chunks = [
        _reasoning_delta_chunk("Reasoning here", content=""),
        _reasoning_delta_chunk("", content="Visible", finish_reason="stop"),
    ]
    emitted = list(provider._iter_stream_chunks(iter(chunks)))

    text = "".join(c.text for c in emitted if c.text)
    assert "<think>Reasoning here</think>" in text
    reasoning_deltas = [c.reasoning_delta for c in emitted if c.reasoning_delta]
    assert reasoning_deltas == []


# ---------------------------------------------------------------------------
# Registry routing
# ---------------------------------------------------------------------------


def test_builtin_glm_resolves_glm_provider():
    """The built-in ``glm`` entry resolves to a GLMProvider instance."""
    from rikugan.providers.glm_provider import GLMProvider
    from rikugan.providers.registry import ProviderRegistry

    provider = ProviderRegistry().new_instance("glm", api_key="test", model="glm-5.2")
    assert isinstance(provider, GLMProvider)


def test_custom_glm_dialect_resolves_glm_provider():
    """A custom provider name whose dialect is ``"glm"`` must resolve to a
    GLMProvider instance with the custom name preserved.
    """
    from rikugan.providers.glm_provider import GLMProvider
    from rikugan.providers.registry import ProviderRegistry

    registry = ProviderRegistry()
    registry.register_custom_providers(["glm-coding"], dialects={"glm-coding": "glm"})
    provider = registry.new_instance("glm-coding", api_key="test", model="glm-5.2")
    assert isinstance(provider, GLMProvider)
    assert provider.name == "glm-coding"


def test_custom_openai_compat_dialect_unaffected_by_glm_routing():
    """Custom providers without a ``"glm"`` dialect must still resolve to
    OpenAICompatProvider.
    """
    from rikugan.providers.openai_compat import OpenAICompatProvider
    from rikugan.providers.registry import ProviderRegistry

    registry = ProviderRegistry()
    registry.register_custom_providers(["my-llm"], dialects={"my-llm": ""})
    provider = registry.new_instance("my-llm", api_key="test", model="some-model")
    assert isinstance(provider, OpenAICompatProvider)


def test_register_custom_providers_preserves_glm_dialect_on_re_register():
    """Re-registering the same custom GLM provider name keeps the dialect
    association stable across calls.
    """
    from rikugan.providers.glm_provider import GLMProvider
    from rikugan.providers.registry import ProviderRegistry

    registry = ProviderRegistry()
    registry.register_custom_providers(["glm-coding"], dialects={"glm-coding": "glm"})
    registry.register_custom_providers(["glm-coding"], dialects={"glm-coding": "glm"})
    provider = registry.new_instance("glm-coding", api_key="test", model="glm-5.2")
    assert isinstance(provider, GLMProvider)


def test_unregister_removes_glm_dialect_association():
    """Unregistering a custom GLM provider removes its dialect association."""
    from rikugan.providers.openai_compat import OpenAICompatProvider
    from rikugan.providers.registry import ProviderRegistry

    registry = ProviderRegistry()
    registry.register_custom_providers(["glm-coding"], dialects={"glm-coding": "glm"})
    registry.unregister("glm-coding")
    registry.register_custom_providers(["glm-coding"], dialects={})
    provider = registry.new_instance("glm-coding", api_key="test", model="some-model")
    assert isinstance(provider, OpenAICompatProvider)


# ---------------------------------------------------------------------------
# Finding 4: cache refresh on extra change + dialect flip after resolution
# ---------------------------------------------------------------------------


def test_cache_refreshes_when_extra_changes():
    """``get_or_create`` must rebuild the provider when ``extra`` changes
    even if credentials are identical, because GLM thinking/guard settings
    are parsed at construction time.
    """
    from rikugan.providers.glm_provider import GLMProvider
    from rikugan.providers.registry import ProviderRegistry

    registry = ProviderRegistry()
    extra1 = {"dialect": "glm", "thinking": {"reasoning_effort": "max"}}
    extra2 = {"dialect": "glm", "thinking": {"reasoning_effort": "low"}}

    p1 = registry.get_or_create("glm", api_key="test", model="glm-5.2", extra=extra1)
    assert isinstance(p1, GLMProvider)

    p2 = registry.get_or_create("glm", api_key="test", model="glm-5.2", extra=extra2)
    # Must be a new instance because extra changed.
    assert p2 is not p1
    assert p2._glm_config.thinking.reasoning_effort == "low"


def test_cache_reuses_when_extra_unchanged():
    """``get_or_create`` must return the same instance when extra is
    identical (deep-equal), even for GLM providers.
    """
    from rikugan.providers.registry import ProviderRegistry

    registry = ProviderRegistry()
    extra = {"dialect": "glm", "thinking": {"reasoning_effort": "high"}}

    p1 = registry.get_or_create("glm", api_key="test", model="glm-5.2", extra=extra)
    # Deep-copy the same config to verify deep-equality works.
    import copy

    p2 = registry.get_or_create("glm", api_key="test", model="glm-5.2", extra=copy.deepcopy(extra))
    assert p1 is p2


def test_cache_reuses_non_glm_provider_without_extra():
    """Non-GLM providers (no ``extra`` kwarg) must still use the original
    credential-only cache behavior — the ``extra`` comparison should not
    break existing providers.
    """
    from rikugan.providers.openai_provider import OpenAIProvider
    from rikugan.providers.registry import ProviderRegistry

    registry = ProviderRegistry()
    p1 = registry.get_or_create("openai", api_key="test", model="gpt-4o")
    p2 = registry.get_or_create("openai", api_key="test", model="gpt-4o")
    assert isinstance(p1, OpenAIProvider)
    assert p1 is p2


def test_glm_to_compat_dialect_flip_after_class_resolution():
    """When a custom GLM provider name is resolved (spec string -> class)
    and then re-registered as compat, the registry must route to
    OpenAICompatProvider, not the stale resolved GLMProvider class.
    """
    from rikugan.providers.glm_provider import GLMProvider
    from rikugan.providers.openai_compat import OpenAICompatProvider
    from rikugan.providers.registry import ProviderRegistry

    registry = ProviderRegistry()
    # Register as GLM and resolve (forces class caching).
    registry.register_custom_providers(["flip-test"], dialects={"flip-test": "glm"})
    glm_provider = registry.new_instance("flip-test", api_key="test", model="glm-5.2")
    assert isinstance(glm_provider, GLMProvider)

    # Now flip dialect to compat (empty) and re-register.
    registry.register_custom_providers(["flip-test"], dialects={"flip-test": ""})
    compat_provider = registry.new_instance("flip-test", api_key="test", model="some-model")
    assert isinstance(compat_provider, OpenAICompatProvider)


def test_compat_to_glm_dialect_flip_after_class_resolution():
    """Reverse direction: compat -> GLM flip after class resolution."""
    from rikugan.providers.glm_provider import GLMProvider
    from rikugan.providers.openai_compat import OpenAICompatProvider
    from rikugan.providers.registry import ProviderRegistry

    registry = ProviderRegistry()
    # Register as compat and resolve (forces class caching).
    registry.register_custom_providers(["flip-test2"], dialects={"flip-test2": ""})
    compat_provider = registry.new_instance("flip-test2", api_key="test", model="some-model")
    assert isinstance(compat_provider, OpenAICompatProvider)

    # Now flip dialect to GLM and re-register.
    registry.register_custom_providers(["flip-test2"], dialects={"flip-test2": "glm"})
    glm_provider = registry.new_instance("flip-test2", api_key="test", model="glm-5.2")
    assert isinstance(glm_provider, GLMProvider)


if __name__ == "__main__":
    unittest.main()
