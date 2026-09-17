"""Tests for Gemini provider: error handling, format history, builtin models."""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from rikugan.core.errors import ProviderError
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.core.types import LLMRequestContext, Message, Role


def _make_provider():
    from rikugan.providers.gemini_provider import GeminiProvider

    return GeminiProvider(api_key="test-key", model="gemini-test")


class TestGeminiHandleApiError(unittest.TestCase):
    def test_generic_error_raises_provider_error(self):
        from rikugan.core.errors import ProviderError

        p = _make_provider()
        with self.assertRaises(ProviderError):
            p._handle_api_error(RuntimeError("something broke"))

    def test_auth_error_from_string_matching(self):
        from rikugan.core.errors import AuthenticationError

        p = _make_provider()
        with self.assertRaises(AuthenticationError):
            p._handle_api_error(RuntimeError("Invalid API key provided"))

    def test_rate_limit_from_string_matching(self):
        from rikugan.core.errors import RateLimitError

        p = _make_provider()
        with self.assertRaises(RateLimitError):
            p._handle_api_error(RuntimeError("Rate limit exceeded, 429"))

    def test_context_length_from_string(self):
        from rikugan.core.errors import ContextLengthError

        p = _make_provider()
        with self.assertRaises(ContextLengthError):
            p._handle_api_error(RuntimeError("token limit exceeded"))

    def test_permission_denied_from_string(self):
        from rikugan.core.errors import AuthenticationError

        p = _make_provider()
        with self.assertRaises(AuthenticationError):
            p._handle_api_error(RuntimeError("permission denied"))


class TestGeminiFormatHistory(unittest.TestCase):
    """Test GeminiProvider._format_history (basic path without genai SDK)."""

    def test_builtin_models(self):
        from rikugan.providers.gemini_provider import GeminiProvider

        models = GeminiProvider._builtin_models()
        self.assertTrue(len(models) > 0)
        for m in models:
            self.assertEqual(m.provider, "gemini")
            self.assertTrue(m.context_window > 0)


class TestGeminiCapabilities(unittest.TestCase):
    def test_capabilities(self):
        p = _make_provider()
        caps = p.capabilities
        self.assertTrue(caps.streaming)
        self.assertTrue(caps.tool_use)


# ---------------------------------------------------------------------------
# Task 5: payload equivalence.
# ---------------------------------------------------------------------------


class TestGeminiRequestContextPayloadEquivalence(unittest.TestCase):
    def test_request_context_does_not_change_gemini_payload(self) -> None:
        import importlib

        provider = _make_provider()
        # The google-genai SDK is optional; load types only if available.
        # Without types the Gemini build path cannot construct a real
        # ``GenerateContentConfig`` payload, so we skip the assertion
        # rather than fabricating a fake module.
        try:
            provider._types = importlib.import_module("google.genai.types")
        except ImportError:
            self.skipTest("google-genai SDK not installed")

        messages = [Message(role=Role.USER, content="hello")]
        baseline = provider._build_request_kwargs(messages, None, 0.3, 4096, "system")
        contextual = provider._build_request_kwargs(
            messages,
            None,
            0.3,
            4096,
            "system",
            request_context=LLMRequestContext(recovery=True),
        )

        self.assertEqual(
            contextual,
            baseline,
            "Gemini payload differs when request_context is provided — "
            "the context must be a pure pass-through for non-GLM "
            "providers.",
        )


# ---------------------------------------------------------------------------
# Task 6: retryable transient-error classification + empty-candidate guard.
# ---------------------------------------------------------------------------


def _fake_api_core_exceptions() -> ModuleType:
    """Stand-in for google.api_core.exceptions (optional dependency).

    Carries every exception name ``GeminiProvider._handle_api_error``
    references so the isinstance wiring is exercised deterministically
    whether or not the real SDK is installed.
    """
    mod = ModuleType("google.api_core.exceptions")
    for name in (
        "Unauthenticated",
        "PermissionDenied",
        "ResourceExhausted",
        "InvalidArgument",
        "InternalServerError",
        "ServiceUnavailable",
        "DeadlineExceeded",
        "RetryError",
    ):
        setattr(mod, name, type(name, (Exception,), {}))
    return mod


def _patched_api_core() -> tuple[ModuleType, Any]:
    """Return (fake_module, import patch) forcing the defensive import seam."""
    fake = _fake_api_core_exceptions()
    real_import = importlib.import_module

    def _selective(name: str, *args: Any, **kwargs: Any):
        if name == "google.api_core.exceptions":
            return fake
        return real_import(name, *args, **kwargs)

    return fake, patch("rikugan.providers.gemini_provider.importlib.import_module", side_effect=_selective)


class TestGeminiRetryableErrors(unittest.TestCase):
    """Transient server/transport errors must classify retryable=True."""

    def setUp(self) -> None:
        self.p = _make_provider()

    def _assert_retryable(self, exc: Exception) -> ProviderError:
        with self.assertRaises(ProviderError) as ctx:
            self.p._handle_api_error(exc)
        self.assertTrue(ctx.exception.retryable, f"{exc!r} must be retryable")
        return ctx.exception

    def test_api_core_server_and_retry_errors_are_retryable(self):
        fake, importer = _patched_api_core()
        with importer:
            for cls in (fake.InternalServerError, fake.ServiceUnavailable, fake.DeadlineExceeded, fake.RetryError):
                with self.subTest(error=cls.__name__):
                    self._assert_retryable(cls("upstream unavailable"))

    def test_genai_server_error_is_retryable(self):
        try:
            genai_errors = importlib.import_module("google.genai.errors")
        except ImportError:
            self.skipTest("google-genai SDK not installed")
        err = self._assert_retryable(genai_errors.ServerError(503, {"message": "backend unavailable"}))
        self.assertEqual(err.status_code, 503)

    def test_httpx_transport_errors_are_retryable(self):
        try:
            httpx = importlib.import_module("httpx")
        except ImportError:
            self.skipTest("httpx not installed")
        for cls in (httpx.ConnectError, httpx.ReadTimeout):
            with self.subTest(error=cls.__name__):
                self._assert_retryable(cls("connection reset by peer"))

    def test_generic_error_is_not_retryable(self):
        with self.assertRaises(ProviderError) as ctx:
            self.p._handle_api_error(RuntimeError("something broke"))
        self.assertFalse(ctx.exception.retryable)


class TestGeminiEmptyCandidateGuard(unittest.TestCase):
    """Blocked/empty non-streaming responses must raise a descriptive ProviderError."""

    def setUp(self) -> None:
        self.p = _make_provider()

    def test_no_candidates_raises_provider_error(self):
        with self.assertRaises(ProviderError):
            self.p._normalize_response(SimpleNamespace(candidates=[], prompt_feedback=None))

    def test_none_candidates_raises_provider_error(self):
        with self.assertRaises(ProviderError):
            self.p._normalize_response(SimpleNamespace(candidates=None, prompt_feedback=None))

    def test_none_content_raises_provider_error_not_indexerror(self):
        response = SimpleNamespace(
            candidates=[SimpleNamespace(content=None)],
            prompt_feedback=SimpleNamespace(block_reason="SAFETY"),
        )
        with self.assertRaises(ProviderError) as ctx:
            self.p._normalize_response(response)
        self.assertIn("SAFETY", str(ctx.exception))

    def test_none_content_without_feedback_still_descriptive(self):
        response = SimpleNamespace(candidates=[SimpleNamespace(content=None)])
        with self.assertRaises(ProviderError) as ctx:
            self.p._normalize_response(response)
        self.assertTrue(str(ctx.exception))

    def test_normal_response_still_parses(self):
        response = SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(parts=[SimpleNamespace(text="hi", thought=False, function_call=None)])
                )
            ],
            prompt_feedback=None,
            usage_metadata=None,
        )
        msg = self.p._normalize_response(response)
        self.assertEqual(msg.content, "hi")

    def test_guard_error_carries_provider_name(self):
        with self.assertRaises(ProviderError) as ctx:
            self.p._normalize_response(SimpleNamespace(candidates=[], prompt_feedback=None))
        self.assertEqual(ctx.exception.provider, "gemini")


if __name__ == "__main__":
    unittest.main()
