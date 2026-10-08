"""Provider registry: factory for creating provider instances.

Lazy import via spec strings: only the actively requested provider's
adapter module is imported on first use, avoiding the cost of importing
all provider SDKs (anthropic, openai, gemini, ollama, ...) at panel boot.
"""

from __future__ import annotations

import copy
import importlib
import os
from typing import Any

from ..core.dependencies import get_missing_dependency_warnings
from ..core.errors import ProviderError
from .base import LLMProvider

# module_path:ClassName — SDK modules are imported on first request.
_BUILTIN_PROVIDER_SPECS: dict[str, str] = {
    "anthropic": "lucnhan.providers.anthropic_provider:AnthropicProvider",
    "openai": "lucnhan.providers.openai_provider:OpenAIProvider",
    "openai_compat": "lucnhan.providers.openai_compat:OpenAICompatProvider",
    "gemini": "lucnhan.providers.gemini_provider:GeminiProvider",
    "ollama": "lucnhan.providers.ollama_provider:OllamaProvider",
    "minimax": "lucnhan.providers.minimax_provider:MiniMaxProvider",
    "codex": "lucnhan.providers.codex_provider:CodexProvider",
    "glm": "lucnhan.providers.glm_provider:GLMProvider",
}

# Entry is either an import spec string ("module:ClassName") or an
# already-resolved class (from register()).
ProviderEntry = str | type[LLMProvider]


def _deep_extra_equal(cached_provider: Any, new_extra: Any) -> bool:
    """Deep-compare the ``extra`` dict used to build a cached provider
    against the ``extra`` kwarg of a new ``get_or_create`` call.

    Reads the ``_provider_extra_raw`` snapshot from the cached provider
    instance.  Providers that do not accept ``extra`` (all non-GLM
    providers) lack this attribute and are treated as having no extra —
    so the comparison always returns True for them, preserving the
    original credential-only cache behavior.
    """
    cached_raw = getattr(cached_provider, "_provider_extra_raw", None)
    # Normalize: None and {} are equivalent (both mean "no extra").
    cached_norm = cached_raw if cached_raw else {}
    new_norm = new_extra if new_extra else {}
    return copy.deepcopy(cached_norm) == copy.deepcopy(new_norm)


class ProviderRegistry:
    """Factory for LLM providers with instance-level config state."""

    def __init__(self) -> None:
        # Provider name -> import spec or resolved class. Initialized from
        # built-ins without resolving any classes.
        self._providers: dict[str, ProviderEntry] = dict(_BUILTIN_PROVIDER_SPECS)
        self._instances: dict[str, LLMProvider] = {}
        # Replaced providers are kept alive for process lifetime: dropping
        # the last ref to SDK/http clients while Qt/Shiboken is dispatching
        # can run C cleanup at an unsafe time.
        self._retired_instances: list[LLMProvider] = []
        # Per-instance tracking. _registered_names survives
        # register_custom_providers() cleanup (in-process classes are not
        # config-managed); _openai_compat_names tracks which custom names
        # route to the openai_compat adapter; _glm_names tracks which
        # custom names route to the GLM adapter.
        self._openai_compat_names: set[str] = set()
        self._glm_names: set[str] = set()
        self._registered_names: set[str] = set()

    # -- Resolution ----------------------------------------------------------

    def _resolve_entry(self, name: str) -> type[LLMProvider]:
        """Resolve an entry to its provider class, importing its module if needed."""
        entry = self._providers.get(name)
        if entry is None:
            raise ProviderError(f"Unknown provider: {name}. Available: {self.list_providers()}")
        if isinstance(entry, str):
            mod_path, cls_name = entry.rsplit(":", 1)
            cls = getattr(importlib.import_module(mod_path), cls_name)
            self._providers[name] = cls  # cache resolved class
            return cls
        return entry

    def _is_compat_name(self, name: str) -> bool:
        """True if name is the built-in compat adapter or a custom compat name."""
        return name == "openai_compat" or name in self._openai_compat_names

    def _is_glm_name(self, name: str) -> bool:
        """True if name is the built-in GLM adapter or a custom GLM-dialect name."""
        return name == "glm" or name in self._glm_names

    def _normalized_api_base(self, name: str, api_base: str) -> str:
        """Effective API base for cache comparison. Empty string means unset."""
        if api_base:
            return api_base
        if name == "minimax":
            return getattr(self._resolve_entry("minimax"), "DEFAULT_API_BASE", "")
        if name == "ollama":
            from .ollama_provider import DEFAULT_OLLAMA_URL

            return os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_URL)
        return ""

    # -- Public registration API ---------------------------------------------

    def register(self, name: str, provider_cls: type[LLMProvider]) -> None:
        """Register an in-process provider class (bypasses import strings)."""
        self._providers[name] = provider_cls
        self._registered_names.add(name)
        self._openai_compat_names.discard(name)
        self._glm_names.discard(name)

    def unregister(self, name: str) -> None:
        """Remove a provider entry by name. Built-in entries are preserved."""
        if name in _BUILTIN_PROVIDER_SPECS:
            return
        self._providers.pop(name, None)
        self._openai_compat_names.discard(name)
        self._glm_names.discard(name)
        self._registered_names.discard(name)

    def register_custom_providers(
        self,
        names: list[str],
        *,
        dialects: dict[str, str] | None = None,
    ) -> None:
        """Set the active list of custom provider names.

        ``dialects`` maps a custom provider name to a dialect identifier.
        Currently the only supported dialect is ``"glm"``; names whose
        dialect is empty or absent default to the OpenAI-compatible
        adapter.

        Names previously in config but absent from ``names`` are removed
        (and their live instances retired). Built-ins and register()-set
        names are preserved.
        """
        compat_spec = _BUILTIN_PROVIDER_SPECS["openai_compat"]
        glm_spec = _BUILTIN_PROVIDER_SPECS["glm"]
        new_set = set(names)

        for existing_name in list(self._providers.keys()):
            if existing_name in _BUILTIN_PROVIDER_SPECS:
                continue
            if existing_name in self._registered_names:
                continue
            if existing_name in new_set:
                continue
            self._providers.pop(existing_name, None)
            self._openai_compat_names.discard(existing_name)
            self._glm_names.discard(existing_name)
            retired = self._instances.pop(existing_name, None)
            if retired is not None:
                self._retired_instances.append(retired)

        dialects = dialects or {}
        for name in names:
            if name in _BUILTIN_PROVIDER_SPECS or name in self._registered_names:
                continue
            dialect = dialects.get(name, "")
            if dialect == "glm":
                # Always (re)set the spec string.  After _resolve_entry
                # resolves a spec string to a class (caching it in-place),
                # the stored value is no longer a string, so string-equality
                # checks would fail and the dialect flip would be silently
                # dropped.  Resetting unconditionally is safe because
                # _resolve_entry re-resolves and re-caches on next access.
                self._providers[name] = glm_spec
                self._glm_names.add(name)
                self._openai_compat_names.discard(name)
            else:
                self._providers[name] = compat_spec
                self._openai_compat_names.add(name)
                self._glm_names.discard(name)

    def list_providers(self) -> list[str]:
        """All known provider names. Does NOT import any adapter module."""
        return list(self._providers.keys())

    def dependency_warnings(self) -> list[str]:
        """Missing optional dependency warnings (e.g. uninstalled provider SDKs).

        Thin wrapper over :func:`get_missing_dependency_warnings` so the UI
        can source both registry state and dependency checks from one place.
        """
        return get_missing_dependency_warnings()

    # -- Instance creation ---------------------------------------------------

    def new_instance(
        self,
        name: str,
        api_key: str = "",
        api_base: str = "",
        model: str = "",
        **kwargs: Any,
    ) -> LLMProvider:
        """Create an uncached provider instance for temporary probes.

        Settings/auth/model-refresh code should use this instead of
        ``create()`` so the live chat provider cache is not disturbed.
        """
        cls = self._resolve_entry(name)
        # Custom compat and GLM-dialect profiles both need their custom
        # name preserved so the provider's ``name`` property does not
        # report the generic adapter name.
        if self._is_compat_name(name) and name != "openai_compat":
            kwargs.setdefault("provider_name", name)
        elif self._is_glm_name(name) and name != "glm":
            kwargs.setdefault("provider_name", name)
        return cls(api_key=api_key, api_base=api_base, model=model, **kwargs)

    def create(
        self,
        name: str,
        api_key: str = "",
        api_base: str = "",
        model: str = "",
        **kwargs: Any,
    ) -> LLMProvider:
        """Create and cache a new provider instance, retiring the old one."""
        instance = self.new_instance(name, api_key=api_key, api_base=api_base, model=model, **kwargs)
        old = self._instances.get(name)
        if old is not None:
            self._retired_instances.append(old)
        self._instances[name] = instance
        return instance

    def get_or_create(
        self,
        name: str,
        api_key: str = "",
        api_base: str = "",
        model: str = "",
        **kwargs: Any,
    ) -> LLMProvider:
        """Return cached instance, recreating on credential or ``extra`` change.

        Model-only switches mutate the existing instance (SDK clients do
        not bind to a model; it is sent per request).

        ``extra`` (GLM dialect config, custom provider options) is deep-
        compared so a user changing GLM thinking settings without changing
        credentials still produces a fresh provider instance with the
        updated parsed config.
        """
        cached = self._instances.get(name)
        if cached is not None:
            creds_match = api_key == cached.api_key and self._normalized_api_base(
                name, api_base
            ) == self._normalized_api_base(name, cached.api_base)
            # Deep-compare ``extra`` so GLM thinking/guard settings changes
            # force a refresh even when credentials are identical.
            extra_match = _deep_extra_equal(cached, kwargs.get("extra"))
            if creds_match and extra_match:
                if model and cached.model != model:
                    cached.model = model
                return cached
            self._retired_instances.append(cached)
        return self.create(name, api_key=api_key, api_base=api_base, model=model, **kwargs)

    def get_instance(self, name: str) -> LLMProvider | None:
        """Return a cached provider instance, or None. Does not import modules."""
        return self._instances.get(name)

    # -- Lifecycle -----------------------------------------------------------

    def reset(self) -> None:
        """Remove all non-built-in providers and cached instances.

        Useful after a full config reload. Retired instances keep their
        C state until process exit to avoid unsafe Qt-during-cleanup races.
        """
        self._providers = dict(_BUILTIN_PROVIDER_SPECS)
        self._openai_compat_names.clear()
        self._glm_names.clear()
        self._registered_names.clear()
        self._retired_instances.extend(self._instances.values())
        self._instances.clear()

    def retire_instances(self) -> None:
        """Retire all cached instances without touching provider specs.

        Use this when credentials change but the set of provider *types*
        is the same; the next get_or_create / new_instance builds fresh
        instances without disrupting the provider-spec registry.
        """
        if not self._instances:
            return
        self._retired_instances.extend(self._instances.values())
        self._instances.clear()
