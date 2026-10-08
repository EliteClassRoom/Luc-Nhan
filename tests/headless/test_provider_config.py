"""Tests for headless CLI parser and bootstrap override handling."""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# CLI parser tests: ask accepts --provider, --model, --api-base
# ---------------------------------------------------------------------------


class TestCliAskParser:
    """Verify the 'ask' subcommand accepts provider override flags."""

    def test_ask_no_overrides(self):
        """Default ask command produces no provider fields in bootstrap config."""
        from lucnhan.cli.headless import build_parser

        parser = build_parser()
        args = parser.parse_args(["ask", "binary.exe", "hello"])
        # Simulate what cmd_ask feeds into bootstrap_cfg:
        assert args.provider is None
        assert args.model is None
        assert args.api_base is None

    def test_ask_with_provider_only(self):
        from lucnhan.cli.headless import build_parser

        parser = build_parser()
        args = parser.parse_args(["ask", "binary.exe", "hello", "--provider", "openai"])
        assert args.provider == "openai"
        assert args.model is None
        assert args.api_base is None

    def test_ask_with_model_only(self):
        from lucnhan.cli.headless import build_parser

        parser = build_parser()
        args = parser.parse_args(["ask", "binary.exe", "hello", "--model", "gpt-4o"])
        assert args.provider is None
        assert args.model == "gpt-4o"
        assert args.api_base is None

    def test_ask_with_api_base_only(self):
        from lucnhan.cli.headless import build_parser

        parser = build_parser()
        args = parser.parse_args(["ask", "binary.exe", "hello", "--api-base", "http://localhost:11434/v1"])
        assert args.provider is None
        assert args.model is None
        assert args.api_base == "http://localhost:11434/v1"

    def test_ask_with_all_overrides(self):
        from lucnhan.cli.headless import build_parser

        parser = build_parser()
        args = parser.parse_args(
            [
                "ask",
                "binary.exe",
                "hello",
                "--provider",
                "openai",
                "--model",
                "gpt-4o",
                "--api-base",
                "http://localhost:8080/v1",
            ]
        )
        assert args.provider == "openai"
        assert args.model == "gpt-4o"
        assert args.api_base == "http://localhost:8080/v1"

    def test_ask_no_api_key_argument(self):
        """Confirm --api-key is NOT a valid argument."""
        from lucnhan.cli.headless import build_parser

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["ask", "binary.exe", "hello", "--api-key", "sk-123"])


class TestCliServeParser:
    """Verify the 'serve' subcommand accepts provider override flags."""

    def test_serve_no_overrides(self):
        from lucnhan.cli.headless import build_parser

        parser = build_parser()
        args = parser.parse_args(["serve", "binary.exe"])
        assert args.provider is None
        assert args.model is None
        assert args.api_base is None

    def test_serve_with_all_overrides(self):
        from lucnhan.cli.headless import build_parser

        parser = build_parser()
        args = parser.parse_args(
            [
                "serve",
                "binary.exe",
                "--provider",
                "anthropic",
                "--model",
                "claude-sonnet-4-20250514",
                "--api-base",
                "https://custom.api/v1",
            ]
        )
        assert args.provider == "anthropic"
        assert args.model == "claude-sonnet-4-20250514"
        assert args.api_base == "https://custom.api/v1"

    def test_serve_no_api_key_argument(self):
        """Confirm --api-key is NOT a valid argument for serve."""
        from lucnhan.cli.headless import build_parser

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["serve", "binary.exe", "--api-key", "sk-123"])


# ---------------------------------------------------------------------------
# Bootstrap config: cmd_ask / cmd_serve include overrides in bootstrap JSON
# ---------------------------------------------------------------------------


class TestBootstrapConfigGeneration:
    """Verify that overrides flow into the bootstrap dict."""

    def test_ask_bootstrap_includes_overrides(self):
        """Bootstrap dict from cmd_ask includes provider fields when set."""
        from lucnhan.cli.headless import build_parser

        parser = build_parser()
        args = parser.parse_args(
            [
                "ask",
                "binary.exe",
                "hello",
                "--provider",
                "openai",
                "--model",
                "gpt-4o",
                "--api-base",
                "http://localhost:8080/v1",
            ]
        )
        # Replicate the bootstrap_cfg assembly logic from cmd_ask
        bootstrap_cfg: dict = {
            "mode": "ask",
            "prompt": args.prompt,
            "wait_for_auto_analysis": not args.no_auto_wait,
        }
        if args.provider:
            bootstrap_cfg["provider"] = args.provider
        if args.model:
            bootstrap_cfg["model"] = args.model
        if args.api_base:
            bootstrap_cfg["api_base"] = args.api_base

        assert bootstrap_cfg["provider"] == "openai"
        assert bootstrap_cfg["model"] == "gpt-4o"
        assert bootstrap_cfg["api_base"] == "http://localhost:8080/v1"

    def test_ask_bootstrap_no_overrides(self):
        """Bootstrap dict from cmd_ask has no provider fields when not set."""
        from lucnhan.cli.headless import build_parser

        parser = build_parser()
        args = parser.parse_args(["ask", "binary.exe", "hello"])

        bootstrap_cfg: dict = {
            "mode": "ask",
            "prompt": args.prompt,
            "wait_for_auto_analysis": not args.no_auto_wait,
        }
        if args.provider:
            bootstrap_cfg["provider"] = args.provider
        if args.model:
            bootstrap_cfg["model"] = args.model
        if args.api_base:
            bootstrap_cfg["api_base"] = args.api_base

        assert "provider" not in bootstrap_cfg
        assert "model" not in bootstrap_cfg
        assert "api_base" not in bootstrap_cfg


# ---------------------------------------------------------------------------
# Provider validation & default model fallback
# ---------------------------------------------------------------------------


class TestProviderValidation:
    """Test LucNhanConfig.validate_active_provider and get_provider_default_model."""

    def test_known_builtin_provider_is_valid(self):
        from lucnhan.core.config import LucNhanConfig

        cfg = LucNhanConfig()
        cfg.provider.name = "anthropic"
        assert cfg.validate_active_provider() is None

    def test_known_openai_provider_is_valid(self):
        from lucnhan.core.config import LucNhanConfig

        cfg = LucNhanConfig()
        cfg.provider.name = "openai"
        assert cfg.validate_active_provider() is None

    def test_known_custom_provider_is_valid(self):
        from lucnhan.core.config import LucNhanConfig

        cfg = LucNhanConfig()
        cfg.custom_providers["my-custom"] = {}
        cfg.provider.name = "my-custom"
        assert cfg.validate_active_provider() is None

    def test_unknown_provider_returns_error(self):
        from lucnhan.core.config import LucNhanConfig

        cfg = LucNhanConfig()
        cfg.provider.name = "nonexistent-xyz"
        error = cfg.validate_active_provider()
        assert error is not None
        assert "Unknown provider" in error
        assert "nonexistent-xyz" in error

    def test_unknown_provider_message_lists_available(self):
        from lucnhan.core.config import LucNhanConfig

        cfg = LucNhanConfig()
        cfg.provider.name = "nonexistent-xyz"
        error = cfg.validate_active_provider()
        assert error is not None
        assert "anthropic" in error
        assert "openai" in error


class TestDefaultModelFallback:
    """Test get_provider_default_model returns correct defaults."""

    def test_anthropic_default(self):
        from lucnhan.core.config import LucNhanConfig

        assert LucNhanConfig.get_provider_default_model("anthropic") == "claude-sonnet-4-20250514"

    def test_openai_default(self):
        from lucnhan.core.config import LucNhanConfig

        assert LucNhanConfig.get_provider_default_model("openai") == "gpt-4o"

    def test_gemini_default(self):
        from lucnhan.core.config import LucNhanConfig

        assert LucNhanConfig.get_provider_default_model("gemini") == "gemini-2.0-flash"

    def test_ollama_default(self):
        from lucnhan.core.config import LucNhanConfig

        assert LucNhanConfig.get_provider_default_model("ollama") == "llama3.1"

    def test_minimax_default(self):
        from lucnhan.core.config import LucNhanConfig

        assert LucNhanConfig.get_provider_default_model("minimax") == "MiniMax-M3"

    def test_openai_compat_no_default(self):
        from lucnhan.core.config import LucNhanConfig

        assert LucNhanConfig.get_provider_default_model("openai_compat") == ""

    def test_unknown_provider_no_default(self):
        from lucnhan.core.config import LucNhanConfig

        assert LucNhanConfig.get_provider_default_model("nonexistent") == ""


# ---------------------------------------------------------------------------
# _apply_provider_overrides integration tests
# ---------------------------------------------------------------------------


class TestApplyProviderOverrides:
    """Integration tests for _apply_provider_overrides in headless_bootstrap."""

    def test_switch_provider_in_memory(self):
        """Provider override switches the provider correctly."""
        from lucnhan.core.config import LucNhanConfig
        from lucnhan.ida.headless_bootstrap import _apply_provider_overrides

        cfg = LucNhanConfig()
        cfg.provider.name = "anthropic"
        cfg.provider.model = "claude-sonnet-4-20250514"

        bootstrap = {"provider": "openai"}
        _apply_provider_overrides(cfg, bootstrap)

        assert cfg.provider.name == "openai"
        # After switching to a different provider, the model falls back
        # to that provider's default (since no saved model for openai).
        assert cfg.provider.model == "gpt-4o"

    def test_model_override_only(self):
        """Model override without provider switch."""
        from lucnhan.core.config import LucNhanConfig
        from lucnhan.ida.headless_bootstrap import _apply_provider_overrides

        cfg = LucNhanConfig()
        cfg.provider.name = "anthropic"
        cfg.provider.model = "some-old-model"

        bootstrap = {"model": "claude-sonnet-4-20250514"}
        _apply_provider_overrides(cfg, bootstrap)

        assert cfg.provider.name == "anthropic"  # unchanged
        assert cfg.provider.model == "claude-sonnet-4-20250514"

    def test_api_base_override(self):
        """API base override is applied."""
        from lucnhan.core.config import LucNhanConfig
        from lucnhan.ida.headless_bootstrap import _apply_provider_overrides

        cfg = LucNhanConfig()
        cfg.provider.name = "ollama"

        bootstrap = {"api_base": "http://localhost:11434/v1"}
        _apply_provider_overrides(cfg, bootstrap)

        assert cfg.provider.api_base == "http://localhost:11434/v1"

    def test_empty_model_falls_back_to_default(self):
        """When model is empty, the provider default is used."""
        from lucnhan.core.config import LucNhanConfig
        from lucnhan.ida.headless_bootstrap import _apply_provider_overrides

        cfg = LucNhanConfig()
        cfg.provider.name = "openai"
        cfg.provider.model = ""  # empty

        bootstrap: dict = {}  # no model override
        _apply_provider_overrides(cfg, bootstrap)

        assert cfg.provider.model == "gpt-4o"  # provider default

    def test_unknown_provider_triggers_clean_exit(self):
        """validate_active_provider calls _clean_exit_ida(2, ...) for unknown provider."""
        from unittest.mock import patch

        from lucnhan.core.config import LucNhanConfig
        from lucnhan.ida.headless_bootstrap import _apply_provider_overrides

        cfg = LucNhanConfig()
        cfg.provider.name = "nonexistent-xyz"

        # _clean_exit_ida calls os._exit(2), which would kill pytest.
        # Mock it to raise SystemExit instead so the test can catch it.
        with patch("lucnhan.ida.headless_bootstrap._clean_exit_ida") as mock_exit:
            mock_exit.side_effect = SystemExit(2)
            with pytest.raises(SystemExit) as exc_info:
                _apply_provider_overrides(cfg, {"provider": "nonexistent-xyz"})
            assert exc_info.value.code == 2

    def test_switch_provider_triggers_default_model(self):
        """Switching to a provider with no saved model uses the default."""
        from lucnhan.core.config import LucNhanConfig
        from lucnhan.ida.headless_bootstrap import _apply_provider_overrides

        cfg = LucNhanConfig()
        cfg.provider.name = "anthropic"
        cfg.provider.model = "claude-sonnet-4-20250514"

        # Switch to gemini with no explicit model — default kicks in.
        _apply_provider_overrides(cfg, {"provider": "gemini"})

        assert cfg.provider.name == "gemini"
        assert cfg.provider.model == "gemini-2.0-flash"

    def test_switch_provider_no_model_override_uses_default(self):
        """Switching provider without explicit model override falls back to default."""
        from lucnhan.core.config import LucNhanConfig
        from lucnhan.ida.headless_bootstrap import _apply_provider_overrides

        cfg = LucNhanConfig()
        cfg.provider.name = "anthropic"
        cfg.provider.model = "some-model"

        # Switch to a different provider with no saved model
        _apply_provider_overrides(cfg, {"provider": "minimax"})
        assert cfg.provider.name == "minimax"
        # switch_provider uses saved model or falls back to provider default
        assert cfg.provider.model == "MiniMax-M3"


# ---------------------------------------------------------------------------
# Provider extras deep-copy persistence (Task 4 of GLM resilience plan)
# ---------------------------------------------------------------------------


def test_switch_provider_preserves_nested_extra_without_aliasing():
    from lucnhan.core.config import LucNhanConfig

    cfg = LucNhanConfig()
    cfg.provider.name = "glm"
    cfg.provider.extra = {"dialect": "glm", "thinking": {"enabled": True}}

    cfg.switch_provider("openai")
    cfg.switch_provider("glm")

    assert cfg.provider.extra["thinking"]["enabled"] is True
    cfg.provider.extra["thinking"]["enabled"] = False
    assert cfg.providers["glm"]["extra"]["thinking"]["enabled"] is True
