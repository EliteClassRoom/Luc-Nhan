"""Tests for the error hierarchy and consistency across the codebase.

Validates that all error types are correctly structured, provider errors
carry the right metadata, and error handling is consistent across providers.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.core.errors import (
    AgentError,
    AuthenticationError,
    CancellationError,
    ConfigError,
    ContextLengthError,
    MCPConnectionError,
    MCPError,
    MCPTimeoutError,
    ProviderError,
    RateLimitError,
    LucNhanError,
    SessionError,
    SkillError,
    ToolError,
    ToolNotFoundError,
    ToolValidationError,
    UIError,
)


class TestErrorHierarchy(unittest.TestCase):
    """Every error type must be a subclass of LucNhanError."""

    def test_all_errors_inherit_lucnhan_error(self):
        for cls in (
            ConfigError, ProviderError, AuthenticationError, RateLimitError,
            ContextLengthError, ToolError, ToolNotFoundError, ToolValidationError,
            AgentError, CancellationError, SessionError, UIError, SkillError,
            MCPError, MCPConnectionError, MCPTimeoutError,
        ):
            self.assertTrue(
                issubclass(cls, LucNhanError),
                f"{cls.__name__} must inherit LucNhanError",
            )

    def test_provider_subtypes(self):
        for cls in (AuthenticationError, RateLimitError, ContextLengthError):
            self.assertTrue(issubclass(cls, ProviderError))

    def test_tool_subtypes(self):
        for cls in (ToolNotFoundError, ToolValidationError):
            self.assertTrue(issubclass(cls, ToolError))

    def test_agent_subtypes(self):
        self.assertTrue(issubclass(CancellationError, AgentError))

    def test_mcp_subtypes(self):
        for cls in (MCPConnectionError, MCPTimeoutError):
            self.assertTrue(issubclass(cls, MCPError))


class TestProviderErrorMetadata(unittest.TestCase):
    """Provider errors must carry provider name, status code, and retryable flag."""

    def test_auth_error_fields(self):
        e = AuthenticationError(provider="anthropic")
        self.assertEqual(e.provider, "anthropic")
        self.assertEqual(e.status_code, 401)
        self.assertFalse(e.retryable)

    def test_rate_limit_error_fields(self):
        e = RateLimitError(provider="openai", retry_after=5.0)
        self.assertEqual(e.provider, "openai")
        self.assertEqual(e.status_code, 429)
        self.assertTrue(e.retryable)
        self.assertEqual(e.retry_after, 5.0)

    def test_context_length_error_fields(self):
        e = ContextLengthError("too long", provider="gemini")
        self.assertEqual(e.provider, "gemini")
        self.assertEqual(e.status_code, 400)
        self.assertFalse(e.retryable)

    def test_generic_provider_error(self):
        e = ProviderError("failed", provider="openai", status_code=500, retryable=True)
        self.assertEqual(e.provider, "openai")
        self.assertEqual(e.status_code, 500)
        self.assertTrue(e.retryable)

    def test_tool_error_carries_tool_name(self):
        e = ToolError("broke", tool_name="decompile")
        self.assertEqual(e.tool_name, "decompile")


class TestProviderErrorConsistency(unittest.TestCase):
    """All three providers must map errors to the correct Luc Nhan error types.

    Anthropic/OpenAI use SDK exception types (isinstance checks); Gemini
    uses string matching.  We create mock SDK exceptions to test the
    isinstance path for Anthropic/OpenAI, and use string triggers for Gemini.
    """

    @staticmethod
    def _mock_httpx_response(status_code=401):
        """Create a minimal mock httpx.Response for SDK exceptions."""
        resp = MagicMock()
        resp.status_code = status_code
        resp.headers = {}
        resp.request = MagicMock()
        return resp

    @unittest.skipUnless(
        importlib.util.find_spec("anthropic"), "anthropic SDK not installed"
    )
    def test_anthropic_sdk_auth_error(self):
        """Anthropic maps anthropic.AuthenticationError → AuthenticationError."""
        import anthropic

        from lucnhan.providers.anthropic_provider import AnthropicProvider
        p = AnthropicProvider(api_key="test", model="test")
        resp = self._mock_httpx_response(401)
        err = anthropic.AuthenticationError("auth failed", response=resp, body=None)
        with self.assertRaises(AuthenticationError):
            p._handle_api_error(err)

    @unittest.skipUnless(
        importlib.util.find_spec("openai"), "openai SDK not installed"
    )
    def test_openai_sdk_auth_error(self):
        """OpenAI maps openai.AuthenticationError → AuthenticationError."""
        import openai

        from lucnhan.providers.openai_provider import OpenAIProvider
        p = OpenAIProvider(api_key="test", model="test")
        resp = self._mock_httpx_response(401)
        err = openai.AuthenticationError("auth failed", response=resp, body=None)
        with self.assertRaises(AuthenticationError):
            p._handle_api_error(err)

    def test_gemini_string_auth_error(self):
        """Gemini maps 'API key' in message → AuthenticationError."""
        from lucnhan.providers.gemini_provider import GeminiProvider
        p = GeminiProvider(api_key="test", model="test")
        with self.assertRaises(AuthenticationError):
            p._handle_api_error(RuntimeError("Invalid API key provided"))

    def test_gemini_string_rate_limit(self):
        """Gemini maps 'Rate' in message → RateLimitError."""
        from lucnhan.providers.gemini_provider import GeminiProvider
        p = GeminiProvider(api_key="test", model="test")
        with self.assertRaises(RateLimitError):
            p._handle_api_error(RuntimeError("Rate limit exceeded, 429"))

    def test_all_providers_generic_fallback(self):
        """All providers map unknown errors → ProviderError."""
        from lucnhan.providers.anthropic_provider import AnthropicProvider
        from lucnhan.providers.gemini_provider import GeminiProvider
        from lucnhan.providers.openai_provider import OpenAIProvider
        for cls, kwargs in (
            (AnthropicProvider, {"api_key": "t", "model": "t"}),
            (OpenAIProvider, {"api_key": "t", "model": "t"}),
            (GeminiProvider, {"api_key": "t", "model": "t"}),
        ):
            p = cls(**kwargs)
            with self.assertRaises(ProviderError, msg=f"{cls.__name__} generic"):
                p._handle_api_error(RuntimeError("something unexpected"))


class TestProviderHandleApiErrorReturnType(unittest.TestCase):
    """_handle_api_error must always raise (NoReturn); never silently return."""

    def test_anthropic_never_returns(self):
        from lucnhan.providers.anthropic_provider import AnthropicProvider
        p = AnthropicProvider(api_key="test", model="test")
        with self.assertRaises(ProviderError):
            p._handle_api_error(ValueError("test"))

    def test_openai_never_returns(self):
        from lucnhan.providers.openai_provider import OpenAIProvider
        p = OpenAIProvider(api_key="test", model="test")
        with self.assertRaises(ProviderError):
            p._handle_api_error(ValueError("test"))

    def test_gemini_never_returns(self):
        from lucnhan.providers.gemini_provider import GeminiProvider
        p = GeminiProvider(api_key="test", model="test")
        with self.assertRaises(ProviderError):
            p._handle_api_error(ValueError("test"))


if __name__ == "__main__":
    unittest.main()
