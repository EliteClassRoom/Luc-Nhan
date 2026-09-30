"""OpenAI-compatible provider for third-party endpoints (e.g. Together, Groq, vLLM)."""

from __future__ import annotations

import importlib
from typing import Any
from urllib.parse import urlparse

from ..core.errors import ProviderError
from ..core.logging import log_debug
from ..core.types import ModelInfo, ProviderCapabilities
from .openai_provider import OpenAIProvider

#: Models a vendor documents and serves but does not yet list from
#: ``/v1/models`` — brand-new previews and plan-gated ids.  Without this
#: table the model is unreachable from the Settings dialog: the combo is
#: populated from the live listing only.  Keyed by API-base hostname.
#: Values are ``(model_id, context_window, max_output_tokens, vision)``.
#:
#: This is a stopgap, not a permanent registry — delete each entry once the
#: vendor's ``/v1/models`` response includes it.
_ENDPOINT_LAG_MODELS: dict[str, tuple[tuple[str, int, int, bool], ...]] = {
    "api.minimax.io": (
        # M3.1 Flash Preview. Token Plan / MiniMax Code only. MiniMax
        # documents a 1M context window and publishes no separate output cap
        # for the preview, so it inherits the M3 contract.
        ("MiniMax-M3.1-Flash-Preview", 1_000_000, 524_288, True),
    ),
}


def _lag_models_for(api_base: str) -> tuple[tuple[str, int, int, bool], ...]:
    """Return the lag-entries for the host in *api_base* (empty if none)."""
    host = (urlparse(api_base).hostname or "").lower() if api_base else ""
    return _ENDPOINT_LAG_MODELS.get(host, ())


class OpenAICompatProvider(OpenAIProvider):
    """Provider that speaks the OpenAI API protocol against a custom base URL."""

    # A custom endpoint must never receive the user's real OpenAI key via
    # the OPENAI_API_KEY env fallback; without a configured key the
    # "no-key" placeholder branch in _get_client is the effective path.
    _ALLOW_OPENAI_ENV_KEY = False

    def __init__(
        self,
        api_key: str = "",
        api_base: str = "",
        model: str = "",
        provider_name: str = "openai_compat",
        **kwargs: Any,
    ) -> None:
        super().__init__(api_key=api_key, model=model, **kwargs)
        self.api_base = api_base
        self._provider_name = provider_name

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                openai = importlib.import_module("openai")
            except ImportError as exc:
                raise ProviderError(
                    "openai package not installed. Run: pip install openai",
                    provider=self._provider_name,
                ) from exc
            kwargs: dict[str, Any] = {}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            else:
                # No explicit key — always pass a placeholder so the SDK
                # never falls back to OPENAI_API_KEY from the environment,
                # with or without a custom base URL. A compat endpoint must
                # never receive the user's real OpenAI credential.
                kwargs["api_key"] = "no-key"
            if self.api_base:
                kwargs["base_url"] = self.api_base
            self._client = openai.OpenAI(**kwargs)
        return self._client

    @property
    def name(self) -> str:
        return self._provider_name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            tool_use=True,
            vision=False,
            max_context_window=128000,
            max_output_tokens=4096,
        )

    def list_models(self) -> list[ModelInfo]:
        """Fetch models from the OpenAI-compatible endpoint.

        Override the base ``_builtin_models()`` fallback contract: arbitrary
        OpenAI-compatible endpoints (MiniMax, custom servers) have no
        canonical builtin model list. On failure we fall back to the
        currently configured model, or an empty list if none is set —
        empty is appropriate because the user types the model name
        manually for these endpoints.

        Models listed in ``_ENDPOINT_LAG_MODELS`` for this host are merged
        in when the endpoint does not advertise them, so a model the vendor
        already serves stays selectable while its ``/v1/models`` entry
        catches up.
        """
        try:
            client = self._get_client()
            response = client.models.list()
            models = []
            for m in response.data:
                name = getattr(m, "name", None) or m.id
                models.append(
                    ModelInfo(
                        id=m.id,
                        name=name,
                        provider=self._provider_name,
                    )
                )
            if models:
                models.sort(key=lambda x: x.id)
                return self._with_lag_models(models)
        except Exception as e:
            log_debug(f"list_models for {self._provider_name!r} failed: {e}")
        # Endpoint doesn't support /v1/models or returned nothing.
        # Return current model if set; otherwise the documented-but-unlisted
        # models for this host (user can always still type one manually).
        models = [ModelInfo(self.model, self.model, self._provider_name)] if self.model else []
        return self._with_lag_models(models)

    def _with_lag_models(self, models: list[ModelInfo]) -> list[ModelInfo]:
        """Append documented-but-unlisted models for this endpoint's host.

        The live listing is authoritative, so an id already present is
        never duplicated or overwritten.  When the endpoint returned
        nothing at all, the lag entries are the only thing the user would
        see — return them so a plan-gated model is still reachable.
        """
        known = {m.id for m in models}
        for model_id, ctx, max_out, vision in _lag_models_for(self.api_base):
            if model_id in known:
                continue
            models.append(
                ModelInfo(
                    id=model_id,
                    name=model_id,
                    provider=self._provider_name,
                    context_window=ctx,
                    max_output_tokens=max_out,
                    supports_tools=True,
                    supports_vision=vision,
                )
            )
        models.sort(key=lambda x: x.id)
        return models
