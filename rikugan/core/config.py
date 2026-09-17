"""Rikugan configuration with JSON persistence.

Mutable-state contract: ``RikuganConfig`` instances are created and owned by
the host entry point (``rikugan_plugin.py`` for IDA, ``cli/headless.py`` for
headless). They are passed explicitly to the agent loop and UI rather than
read from a process-global singleton. The dataclass fields (``providers``,
``custom_providers``, ``extra``, ``a2a_agents``) are intentionally mutable so
that the settings dialog can edit them in place before ``save()``.

Import-time side effects: this module reads only ``os.environ`` for the user
config base directory (via ``host.get_user_config_base_dir``) at
``field(default_factory=...)`` evaluation time — no network, no IDA, no file
I/O at import. The config file is loaded lazily via ``load()`` / ``load_or_create()``
after the host has bootstrapped.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .profile import AnalysisProfile

from ..constants import (
    CACHE_DIR_NAME,
    CONFIG_DIR_NAME,
    CONFIG_FILE_NAME,
    CONFIG_SCHEMA_VERSION,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    GLM_DEFAULT_MODEL,
    MCP_CONFIG_FILE,
    MEMORY_DIR_NAME,
    SKILLS_DIR_NAME,
)
from .host import get_user_config_base_dir
from .logging import log_error, log_info, log_warning

# Built-in provider default models — used as a fallback when the user's
# saved config has an empty model string for a provider.
PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-20250514",
    "openai": "gpt-4o",
    "gemini": "gemini-2.0-flash",
    "ollama": "llama3.1",
    "minimax": "MiniMax-M3",
    "glm": GLM_DEFAULT_MODEL,
    # openai_compat is intentionally omitted — user must configure
}

# Built-in provider names (mirrors providers/registry.py:_BUILTIN_PROVIDER_SPECS).
# This avoids importing the registry module at config-load time and provides
# a standalone validation source for headless bootstrap.
_BUILTIN_PROVIDER_NAMES = frozenset(PROVIDER_DEFAULT_MODELS.keys()) | {"openai_compat"}


def _default_config_dir() -> str:
    return os.path.join(get_user_config_base_dir(), CONFIG_DIR_NAME)


@dataclass
class ProviderConfig:
    name: str = "anthropic"
    model: str = "claude-sonnet-4-20250514"
    api_key: str = ""
    api_base: str = ""
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS
    context_window: int = DEFAULT_CONTEXT_WINDOW
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class RikuganConfig:
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    custom_providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    auto_context: bool = True
    plan_mode_default: bool = False
    checkpoint_auto_save: bool = True
    approve_mutations: bool = False  # require approval for mutating tools (rename, retype, etc.)
    exploration_turn_limit: int = 100  # max turns in exploration phase before forcing transition
    max_retries: int = 3  # max retries on rate-limit / transient API errors
    silent_retry_mode: bool = False  # show loading indicator instead of error messages on retry
    docs_review_mode: Literal["on_error", "off"] = (
        "on_error"  # docs-reviewer trigger: on runtime API-shaped error, or off
    )
    theme: str = "auto"  # "auto" follows host theme; "dark" / "light" force Rikugan palettes; "ida" forces IDA-native
    font_family: str = ""  # empty = inherit from host; set to override (e.g. "Consolas")
    font_size_override: int = 0  # 0 = inherit from host; set to override point size
    # Hide string-list/search tools from the agent (prefer disassembly/decompilation).
    hide_strings: bool = False

    # Skills & MCP external integration
    disabled_skills: list[str] = field(default_factory=list)
    enabled_external_skills: list[str] = field(default_factory=list)
    enabled_external_mcp: list[str] = field(default_factory=list)

    # Analysis profiles
    active_profile: str = "default"
    custom_profiles: dict[str, dict] = field(default_factory=dict)

    # A2A / external agents
    a2a_auto_discover: bool = True
    a2a_agents: list[dict[str, Any]] = field(default_factory=list)

    # Context management
    preserve_context: bool = False  # disable tool result truncation + context compaction

    # OAuth consent — user must accept risk before keychain autoload
    oauth_consent_accepted: bool = False

    # Bulk renamer defaults
    bulk_renamer_batch_size: int = 10
    bulk_renamer_max_concurrent: int = 3

    # Multi-tab parallel agent. When enabled, multiple chat tabs can run
    # agents concurrently (one runner per tab); switching tabs does not
    # cancel a running agent. ``max_concurrent`` caps simultaneous agents
    # (default 3, mirrors bulk renamer). Disable for the legacy single-
    # agent behavior (cap=1, tab switch cancels) — a safe A/B fallback.
    parallel_agent_enabled: bool = True
    parallel_agent_max_concurrent: int = 3

    # IDA Output window verbosity.  Controls which log records appear in
    # IDA's Output window via ``HostOutputHandler``.  File and JSON
    # logging are unaffected — full DEBUG output continues to land in
    # ``rikugan_debug.log`` and ``rikugan_structured.jsonl``.
    # Allowed: "debug", "info", "warning", "error", "critical", "off".
    ida_output_log_level: str = "warning"

    # ------------------------------------------------------------------
    # Raw knowledge memory (see rikugan.memory.*)
    # ------------------------------------------------------------------
    # Master switch: when False, no JSONL is written, no system-prompt
    # context is built, and the ``/knowledge`` command reports
    # "memory disabled". Default True — the feature is opt-out, not
    # opt-in, because the auto-ingest paths are designed to be no-ops
    # when no IDB path is set anyway.
    knowledge_enabled: bool = True

    # When True, every retrieval emits a compact ``KNOWLEDGE_RETRIEVED``
    # chat indicator (counts + top item titles). The default is False
    # because the LLM already sees the retrieved-knowledge block in the
    # system prompt; the UI indicator is a user-facing "what is in
    # scope right now" affordance.
    knowledge_show_retrieved_in_chat: bool = False

    # Soft cap on the number of memory items per retrieval.
    knowledge_max_context_items: int = 12

    # Hard cap on the bytes of the rendered retrieved-knowledge block.
    # Truncation is applied as a final pass so we never blow out the
    # system prompt's token budget.
    knowledge_max_context_chars: int = 12_000

    # API key encryption
    encrypt_api_keys: bool = False
    _encryption_block: dict = field(default_factory=dict, repr=False)

    # Session-local: True once ``decrypt_stored_keys`` succeeded for the
    # blob currently in ``_encryption_block``. The blob itself is retained
    # (it is the only durable copy of the keys — password-less save()
    # re-emits it verbatim), so this flag is what stops the UI from
    # re-prompting for a password within the same session.
    _keys_decrypted: bool = field(default=False, repr=False)

    _config_dir: str = field(default_factory=_default_config_dir, repr=False)

    @property
    def config_path(self) -> str:
        return os.path.join(self._config_dir, CONFIG_FILE_NAME)

    @property
    def checkpoints_dir(self) -> str:
        return os.path.join(self._config_dir, "checkpoints")

    @property
    def skills_dir(self) -> str:
        return os.path.join(self._config_dir, SKILLS_DIR_NAME)

    @property
    def mcp_config_path(self) -> str:
        return os.path.join(self._config_dir, MCP_CONFIG_FILE)

    @property
    def cache_dir(self) -> str:
        """Directory for Rikugan-managed persistent caches (e.g. raw string cache)."""
        return os.path.join(self._config_dir, CACHE_DIR_NAME)

    @property
    def memory_dir(self) -> str:
        """Root for Rikugan-owned durable memory workspaces."""
        return os.path.join(self._config_dir, MEMORY_DIR_NAME)

    def validate(self) -> list[str]:
        """Validate config values. Returns list of error messages (empty = valid)."""
        errors: list[str] = []
        if not (0.0 <= self.provider.temperature <= 2.0):
            errors.append(f"temperature {self.provider.temperature} out of range [0, 2]")
        if self.provider.max_tokens <= 0:
            errors.append(f"max_tokens must be positive, got {self.provider.max_tokens}")
        if self.provider.context_window <= 0:
            errors.append(f"context_window must be positive, got {self.provider.context_window}")
        if not (1 <= self.max_retries <= 10):
            errors.append(f"max_retries {self.max_retries} out of range [1, 10]")
        if self.font_family and not isinstance(self.font_family, str):
            errors.append("font_family must be a string")
        if not (0 <= self.font_size_override <= 72):
            errors.append(f"font_size_override {self.font_size_override} out of range [0, 72]")
        if not self.active_profile or not isinstance(self.active_profile, str):
            errors.append("active_profile must be a non-empty string")
        if not isinstance(self.custom_profiles, dict):
            errors.append("custom_profiles must be a dict")
        else:
            for k, v in self.custom_profiles.items():
                if not isinstance(v, dict):
                    errors.append(f"custom_profiles['{k}'] must be a dict")
        if self.ida_output_log_level not in ("debug", "info", "warning", "error", "critical", "off"):
            errors.append(
                f"ida_output_log_level '{self.ida_output_log_level}' must be debug|info|warning|error|critical|off"
            )
        if not isinstance(self.knowledge_enabled, bool):
            errors.append("knowledge_enabled must be a bool")
        if not isinstance(self.knowledge_show_retrieved_in_chat, bool):
            errors.append("knowledge_show_retrieved_in_chat must be a bool")
        if not isinstance(self.hide_strings, bool):
            errors.append("hide_strings must be a bool")
        if not (1 <= self.knowledge_max_context_items <= 100):
            errors.append(f"knowledge_max_context_items {self.knowledge_max_context_items} out of range [1, 100]")
        if not (1_000 <= self.knowledge_max_context_chars <= 60_000):
            errors.append(f"knowledge_max_context_chars {self.knowledge_max_context_chars} out of range [1000, 60000]")
        # GLM dialect validation — only when the active provider has
        # ``extra["dialect"] == "glm"``.  Non-GLM extras remain opaque so
        # custom-provider configuration is never reinterpreted.
        extra = self.provider.extra
        if isinstance(extra, dict) and extra.get("dialect") == "glm":
            from .glm_config import parse_glm_extra

            try:
                parse_glm_extra(extra, self.provider.model)
            except ValueError as exc:
                errors.append(str(exc))
        return errors

    def save(self, password: str = "") -> None:
        errors = self.validate()
        if errors:
            for err in errors:
                log_error(f"Config validation: {err}")
            # Clamp to valid ranges rather than refusing to save
            self.provider.temperature = max(0.0, min(2.0, self.provider.temperature))
            self.provider.max_tokens = max(1, self.provider.max_tokens)
            self.provider.context_window = max(1024, self.provider.context_window)
            self.max_retries = max(1, min(10, self.max_retries))
            # Normalize invalid log verbosity to "warning"
            if self.ida_output_log_level not in (
                "debug",
                "info",
                "warning",
                "error",
                "critical",
                "off",
            ):
                self.ida_output_log_level = "warning"
            # Clamp knowledge memory bounds
            if not isinstance(self.knowledge_enabled, bool):
                self.knowledge_enabled = True
            if not isinstance(self.knowledge_show_retrieved_in_chat, bool):
                self.knowledge_show_retrieved_in_chat = False
            if not isinstance(self.hide_strings, bool):
                self.hide_strings = False
            self.knowledge_max_context_items = max(1, min(100, int(self.knowledge_max_context_items or 12)))
            self.knowledge_max_context_chars = max(1000, min(60_000, int(self.knowledge_max_context_chars or 12_000)))

        os.makedirs(self._config_dir, exist_ok=True)
        # Snapshot current provider into the providers dict before saving
        self._snapshot_current_provider()
        d = asdict(self)
        d.pop("_config_dir", None)
        d.pop("_encryption_block", None)
        d.pop("_keys_decrypted", None)
        d["schema_version"] = CONFIG_SCHEMA_VERSION

        if self.encrypt_api_keys and password:
            from .crypto import encrypt_keys

            # Collect all API keys into a single blob
            key_data = {
                "provider_api_key": d["provider"]["api_key"],
                "providers": {name: info.get("api_key", "") for name, info in d.get("providers", {}).items()},
            }
            d["encryption"] = {"enabled": True, **encrypt_keys(password, key_data)}
            # Zero out plaintext keys on disk
            d["provider"]["api_key"] = ""
            for info in d.get("providers", {}).values():
                info["api_key"] = ""
            # Mirror the new blob and mark this session's pending-prompt
            # state resolved, so later password-less saves re-emit it
            # verbatim instead of downgrading.
            self._encryption_block = d["encryption"]
            self._keys_decrypted = True
        elif self.encrypt_api_keys and self._encryption_block:
            # Password-less save (e.g. a UI toggle) on an encrypted config:
            # re-emit the stored blob verbatim instead of downgrading keys
            # to plaintext. The blob is the only durable copy of the keys —
            # never mutate or re-encrypt it here; plaintext keys exist only
            # in memory (the dump above is a snapshot, so zeroing these
            # fields does not touch the live config).
            d["encryption"] = {"enabled": True, **self._encryption_block}
            d["provider"]["api_key"] = ""
            for info in d.get("providers", {}).values():
                info["api_key"] = ""
        elif self.encrypt_api_keys:
            # Encrypted mode without a stored blob (flag set but the config
            # was never saved with a password): refuse to write plaintext
            # keys. Degrade the file to a coherent disabled state — the
            # persisted flag must not survive pointing at disabled
            # encryption, or a reload restores the degenerate in-memory
            # state. The live session is untouched.
            has_keys = bool(d["provider"]["api_key"]) or any(
                info.get("api_key") for info in d.get("providers", {}).values()
            )
            d["encrypt_api_keys"] = False
            d["encryption"] = {"enabled": False}
            if has_keys:
                d["provider"]["api_key"] = ""
                for info in d.get("providers", {}).values():
                    info["api_key"] = ""
                log_warning(
                    "Config saved without password and no stored encryption "
                    "block: API keys were NOT persisted (would be written as "
                    "plaintext). Re-enter the keys and re-enable encryption."
                )
            else:
                log_info(
                    "Config saved without password and no stored encryption "
                    "block: encryption disabled in the saved file (no keys "
                    "to protect)."
                )
        else:
            d["encryption"] = {"enabled": False}

        with open(self.config_path, "w") as f:
            json.dump(d, f, indent=2)

    def load(self) -> None:
        if not os.path.exists(self.config_path):
            return
        with open(self.config_path) as f:
            data = json.load(f)
        # Schema version check (for future migrations)
        _stored_version = data.pop("schema_version", 0)

        # Detect encrypted API keys — actual decryption deferred to
        # decrypt_stored_keys() which is called at session start.
        enc = data.pop("encryption", {})
        if enc.get("enabled"):
            self.encrypt_api_keys = True
            self._encryption_block = enc
            self._keys_decrypted = False  # fresh blob — prompt again this session

        self._apply_loaded_config(data)

    def _apply_loaded_config(self, data: dict[str, Any]) -> None:
        """Apply loaded config dict to this instance, with legacy migration.

        Extracted from ``load()`` so migration logic is unit-testable
        without touching disk. Handles the legacy
        ``require_ida_docs_for_complex_scripts`` boolean → ``docs_review_mode``
        enum migration.
        """
        # Legacy migration: require_ida_docs_for_complex_scripts → docs_review_mode
        legacy_field = "require_ida_docs_for_complex_scripts"
        if legacy_field in data and "docs_review_mode" not in data:
            legacy_val = data[legacy_field]
            # False (user disabled reviewer) → "off"; True or anything → "on_error"
            data["docs_review_mode"] = "off" if legacy_val is False else "on_error"

        if "provider" in data:
            for k, v in data["provider"].items():
                if not hasattr(self.provider, k):
                    continue
                # Numeric provider fields: hand-edited configs sometimes
                # leave these as JSON strings ("0.3", "4096").  Coerce in
                # place when possible; reject (skip) values that cannot
                # be parsed so validate()/save() never raise TypeError.
                if k == "temperature":
                    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                        continue
                    try:
                        v = float(v)
                    except (TypeError, ValueError):
                        continue
                elif k in ("max_tokens", "context_window"):
                    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                        continue
                    try:
                        v = int(v)
                    except (TypeError, ValueError):
                        continue
                setattr(self.provider, k, v)
        self.providers = data.get("providers", {})
        self.custom_providers = data.get("custom_providers", {})
        for k in (
            "auto_context",
            "plan_mode_default",
            "checkpoint_auto_save",
            "approve_mutations",
            "exploration_turn_limit",
            "max_retries",
            "silent_retry_mode",
            "docs_review_mode",
            "theme",
            "font_family",
            "font_size_override",
            "disabled_skills",
            "enabled_external_skills",
            "enabled_external_mcp",
            "active_profile",
            "custom_profiles",
            "a2a_auto_discover",
            "a2a_agents",
            "bulk_renamer_batch_size",
            "bulk_renamer_max_concurrent",
            "parallel_agent_enabled",
            "parallel_agent_max_concurrent",
            "oauth_consent_accepted",
            "preserve_context",
            "encrypt_api_keys",
            "ida_output_log_level",
            "knowledge_enabled",
            "knowledge_max_context_items",
            "knowledge_max_context_chars",
            "hide_strings",
        ):
            if k in data:
                val = data[k]
                # Normalize unknown/legacy theme to "auto" so the new
                # AUTO/IDA_NATIVE/DARK/LIGHT ThemeMode enum round-trips
                # correctly.  "auto" is the safe default for fresh
                # installs and for older configs that predate the
                # new theme system.
                if k == "theme" and val not in {"ida", "dark", "light", "auto"}:
                    val = "auto"
                # Normalize invalid log verbosity to "warning"
                if k == "ida_output_log_level" and val not in (
                    "debug",
                    "info",
                    "warning",
                    "error",
                    "critical",
                    "off",
                ):
                    val = "warning"
                # Normalize invalid docs_review_mode to "on_error"
                if k == "docs_review_mode" and val not in ("on_error", "off"):
                    val = "on_error"
                # Boolean fields must be real JSON booleans — reject
                # truthy strings/integers so a hand-edited config file
                # can never silently enable security/UX gates (consent,
                # encryption, context preservation, agent discovery, …).
                _BOOLEAN_FIELDS = {
                    "auto_context",
                    "plan_mode_default",
                    "checkpoint_auto_save",
                    "approve_mutations",
                    "silent_retry_mode",
                    "knowledge_enabled",
                    "knowledge_show_retrieved_in_chat",
                    "hide_strings",
                    "parallel_agent_enabled",
                    "oauth_consent_accepted",
                    "preserve_context",
                    "a2a_auto_discover",
                    "encrypt_api_keys",
                }
                if k in _BOOLEAN_FIELDS and not isinstance(val, bool):
                    continue
                setattr(self, k, val)

    def has_encrypted_keys(self) -> bool:
        """True if encrypted keys are present and still pending decryption."""
        return self.encrypt_api_keys and bool(self._encryption_block) and not self._keys_decrypted

    def decrypt_stored_keys(self, password: str) -> bool:
        """Decrypt stored API keys using *password*.

        Returns True on success, False on wrong password.

        The stored blob is retained (only ``_keys_decrypted`` flips) so a
        later password-less ``save()`` can re-emit it verbatim instead of
        downgrading the keys to plaintext.
        """
        if not self._encryption_block:
            return True
        try:
            from .crypto import decrypt_keys

            keys = decrypt_keys(password, self._encryption_block)
        except ValueError:
            return False

        # Restore plaintext keys into the live config
        self.provider.api_key = keys.get("provider_api_key", "")
        for name, key in keys.get("providers", {}).items():
            if name in self.providers:
                self.providers[name]["api_key"] = key

        # Restore the current provider's key from the providers snapshot
        saved = self.providers.get(self.provider.name, {})
        if saved.get("api_key"):
            self.provider.api_key = saved["api_key"]
        self._keys_decrypted = True
        return True

    def _snapshot_current_provider(self) -> None:
        """Store current provider settings into the providers dict.

        ``provider.extra`` is deep-copied so editing the active provider's
        nested options cannot mutate another provider's snapshot via
        reference aliasing (spec §12.3).
        """
        name = self.provider.name
        self.providers[name] = {
            "model": self.provider.model,
            "api_key": self.provider.api_key,
            "api_base": self.provider.api_base,
            "temperature": self.provider.temperature,
            "max_tokens": self.provider.max_tokens,
            "context_window": self.provider.context_window,
            "extra": copy.deepcopy(self.provider.extra),
        }

    def switch_provider(self, new_name: str) -> None:
        """Switch to a different provider, preserving current settings.

        Saves the current provider's config and restores the new one
        (if previously configured).  ``provider.extra`` round-trips
        through independent deep copies so switching providers never
        aliases nested GLM or custom options across snapshots.
        """
        self._snapshot_current_provider()
        self.provider.name = new_name

        saved = self.providers.get(new_name, {})
        if saved:
            self.provider.model = saved.get("model", "")
            self.provider.api_key = saved.get("api_key", "")
            self.provider.api_base = saved.get("api_base", "")
            self.provider.temperature = saved.get("temperature", DEFAULT_TEMPERATURE)
            self.provider.max_tokens = saved.get("max_tokens", DEFAULT_MAX_TOKENS)
            self.provider.context_window = saved.get("context_window", DEFAULT_CONTEXT_WINDOW)
            self.provider.extra = copy.deepcopy(saved.get("extra", {}))
        else:
            # Fresh provider — clear key/base, keep defaults
            self.provider.api_key = ""
            self.provider.api_base = ""
            self.provider.model = ""
            self.provider.temperature = DEFAULT_TEMPERATURE
            self.provider.max_tokens = DEFAULT_MAX_TOKENS
            self.provider.context_window = DEFAULT_CONTEXT_WINDOW
            self.provider.extra = {}

    def add_custom_provider(self, name: str) -> None:
        """Register a new custom OpenAI-compatible provider name."""
        self.custom_providers[name] = {}

    def remove_custom_provider(self, name: str) -> None:
        """Remove a custom provider and its saved settings."""
        self.custom_providers.pop(name, None)
        self.providers.pop(name, None)

    def is_custom_provider(self, name: str) -> bool:
        return name in self.custom_providers

    def get_active_profile(self) -> AnalysisProfile:
        """Return the currently active AnalysisProfile."""
        from .profile import get_profile

        return get_profile(self.active_profile, self.custom_profiles)

    @staticmethod
    def get_provider_default_model(provider_name: str) -> str:
        """Return the default model for a built-in provider, or '' if unknown."""
        return PROVIDER_DEFAULT_MODELS.get(provider_name, "")

    def validate_active_provider(self) -> str | None:
        """Validate the current provider name against built-ins + custom providers.

        Returns an error message string on failure, or None on success.
        """
        name = self.provider.name
        if name in _BUILTIN_PROVIDER_NAMES:
            return None
        if name in self.custom_providers:
            return None
        # Provide a helpful error listing known providers.
        builtins = sorted(_BUILTIN_PROVIDER_NAMES)
        customs = sorted(self.custom_providers.keys())
        known = builtins + customs
        return f"Unknown provider: '{name}'. Available providers: {', '.join(known)}"

    @classmethod
    def load_or_create(cls) -> RikuganConfig:
        cfg = cls()
        cfg.load()
        return cfg
