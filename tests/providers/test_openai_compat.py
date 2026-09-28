"""Tests for OpenAICompatProvider / GLMProvider credential isolation (Task 5).

``OPENAI_API_KEY`` env auto-discovery must stay opt-in: only the direct
OpenAI adapter (api.openai.com) may consume it.  Adapters that reuse the
OpenAI protocol against third-party endpoints (``OpenAICompatProvider``
for custom base URLs, ``GLMProvider`` for Z.AI) must never silently send
the user's real OpenAI key as a bearer credential to a different host —
the ``"no-key"`` placeholder branch in their ``_get_client`` is the
effective path when no key is configured.
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from tests import purge_rikugan_stubs  # noqa: E402

purge_rikugan_stubs()

ENV_KEY = "sk-real-openai-secret"


# ---------------------------------------------------------------------------
# __init__: the env fallback must not fire for third-party endpoints
# ---------------------------------------------------------------------------


def test_compat_never_inherits_openai_env_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", ENV_KEY)
    from rikugan.providers.openai_compat import OpenAICompatProvider

    p = OpenAICompatProvider(api_key="", api_base="https://api.z.ai/v1")
    assert p.api_key != ENV_KEY
    assert p.api_key == ""


def test_glm_never_inherits_openai_env_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", ENV_KEY)
    from rikugan.providers.glm_provider import GLMProvider

    p = GLMProvider(api_key="")
    assert p.api_key != ENV_KEY
    assert p.api_key == ""


def test_openai_provider_keeps_env_fallback(monkeypatch):
    """Direct OpenAIProvider (api.openai.com) keeps env auto-discovery."""
    monkeypatch.setenv("OPENAI_API_KEY", ENV_KEY)
    from rikugan.providers.openai_provider import OpenAIProvider

    p = OpenAIProvider(api_key="")
    assert p.api_key == ENV_KEY


def test_compat_explicit_key_still_wins(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", ENV_KEY)
    from rikugan.providers.openai_compat import OpenAICompatProvider

    p = OpenAICompatProvider(api_key="sk-explicit", api_base="https://api.z.ai/v1")
    assert p.api_key == "sk-explicit"


def test_glm_explicit_key_still_wins(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", ENV_KEY)
    from rikugan.providers.glm_provider import GLMProvider

    p = GLMProvider(api_key="sk-explicit")
    assert p.api_key == "sk-explicit"


# ---------------------------------------------------------------------------
# _get_client: the "no-key" placeholder is the effective path without a key
# ---------------------------------------------------------------------------


def _fake_openai_module(monkeypatch) -> MagicMock:
    fake = MagicMock()
    monkeypatch.setattr(importlib, "import_module", lambda _name: fake)
    return fake


def test_compat_client_uses_placeholder_without_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", ENV_KEY)
    from rikugan.providers.openai_compat import OpenAICompatProvider

    fake = _fake_openai_module(monkeypatch)
    p = OpenAICompatProvider(api_key="", api_base="https://api.z.ai/v1")
    p._get_client()
    fake.OpenAI.assert_called_once_with(api_key="no-key", base_url="https://api.z.ai/v1")


def test_glm_client_uses_placeholder_without_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", ENV_KEY)
    from rikugan.providers.glm_provider import GLMProvider

    fake = _fake_openai_module(monkeypatch)
    p = GLMProvider(api_key="")
    p._get_client()
    kwargs = fake.OpenAI.call_args.kwargs
    assert kwargs["api_key"] == "no-key"
    assert kwargs["timeout"] == 120.0
    assert "z.ai" in kwargs["base_url"]


def test_compat_client_uses_explicit_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", ENV_KEY)
    from rikugan.providers.openai_compat import OpenAICompatProvider

    fake = _fake_openai_module(monkeypatch)
    p = OpenAICompatProvider(api_key="sk-explicit", api_base="https://api.z.ai/v1")
    p._get_client()
    fake.OpenAI.assert_called_once_with(api_key="sk-explicit", base_url="https://api.z.ai/v1")


def test_compat_default_constructor_never_leaks_env_key_to_sdk(monkeypatch):
    """Default empty-base path: ``openai.OpenAI()`` without an ``api_key``
    kwarg makes the SDK read ``OPENAI_API_KEY`` itself — the placeholder
    must reach the SDK kwargs even when no base URL is configured.
    """
    monkeypatch.setenv("OPENAI_API_KEY", ENV_KEY)
    from rikugan.providers.openai_compat import OpenAICompatProvider

    fake = _fake_openai_module(monkeypatch)
    p = OpenAICompatProvider(api_key="", api_base="")
    p._get_client()
    assert fake.OpenAI.call_args.kwargs["api_key"] == "no-key"


# ---------------------------------------------------------------------------
# _ENDPOINT_LAG_MODELS: documented models missing from /v1/models
# ---------------------------------------------------------------------------

#: The exact payload ``GET https://api.minimax.io/v1/models`` returned —
#: eight ids, none of them the M3.1 preview.
_MINIMAX_LISTING = [
    "MiniMax-M3",
    "MiniMax-M2.7",
    "MiniMax-M2.7-highspeed",
    "MiniMax-M2.5",
    "MiniMax-M2.5-highspeed",
    "MiniMax-M2.1",
    "MiniMax-M2.1-highspeed",
    "MiniMax-M2",
]


def _compat_with_listing(ids, api_base="https://api.minimax.io/v1"):
    from types import SimpleNamespace

    from rikugan.providers.openai_compat import OpenAICompatProvider

    p = OpenAICompatProvider(api_key="sk-test", api_base=api_base, model="MiniMax-M3")
    p._client = SimpleNamespace(
        models=SimpleNamespace(list=lambda: SimpleNamespace(data=[SimpleNamespace(id=i) for i in ids]))
    )
    return p


def test_lag_model_merged_into_live_minimax_listing():
    """M3.1 Flash Preview is served but unlisted, so it must appear
    alongside the eight ids the endpoint does advertise."""
    models = _compat_with_listing(_MINIMAX_LISTING).list_models()
    ids = [m.id for m in models]
    assert "MiniMax-M3.1-Flash-Preview" in ids
    assert "MiniMax-M3" in ids
    assert len(ids) == len(_MINIMAX_LISTING) + 1
    m31 = next(m for m in models if m.id == "MiniMax-M3.1-Flash-Preview")
    assert m31.context_window == 1_000_000
    assert m31.max_output_tokens == 524_288
    assert m31.supports_vision is True


def test_lag_model_never_duplicates_an_advertised_id():
    """Once MiniMax ships M3.1 in ``/v1/models``, the live entry wins —
    the lag table must not shadow or duplicate it."""
    listed = [*_MINIMAX_LISTING, "MiniMax-M3.1-Flash-Preview"]
    models = _compat_with_listing(listed).list_models()
    ids = [m.id for m in models]
    assert ids.count("MiniMax-M3.1-Flash-Preview") == 1
    assert len(ids) == len(listed)


def test_lag_models_are_scoped_to_the_matching_host():
    """A different OpenAI-compatible endpoint must not inherit MiniMax
    model ids from the lag table."""
    models = _compat_with_listing(["my-model"], api_base="https://api.example.com/v1").list_models()
    assert [m.id for m in models] == ["my-model"]


def test_lag_model_reachable_when_endpoint_fails():
    """When ``/v1/models`` errors out entirely, the lag entries keep the
    plan-gated model selectable instead of collapsing to one echo entry."""
    from rikugan.providers.openai_compat import OpenAICompatProvider

    p = OpenAICompatProvider(api_key="sk-test", api_base="https://api.minimax.io/v1", model="MiniMax-M3")
    p._client = None  # no openai SDK installed -> list_models raises, caught
    p._get_client = lambda: (_ for _ in ()).throw(RuntimeError("endpoint down"))
    assert "MiniMax-M3.1-Flash-Preview" in [m.id for m in p.list_models()]
