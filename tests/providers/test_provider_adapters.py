"""Cross-provider tests: builtin models, capabilities, shared adapter contracts."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()


class TestBuiltinModels(unittest.TestCase):
    """All providers must declare non-empty builtin model lists."""

    def test_anthropic_builtin_models(self):
        from lucnhan.providers.anthropic_provider import AnthropicProvider
        p = AnthropicProvider(api_key="test", model="test")
        models = p._builtin_models()
        self.assertTrue(len(models) > 0)
        for m in models:
            self.assertEqual(m.provider, "anthropic")
            self.assertTrue(m.context_window > 0)

    def test_openai_builtin_models(self):
        from lucnhan.providers.openai_provider import OpenAIProvider
        p = OpenAIProvider(api_key="test", model="test")
        models = p._builtin_models()
        self.assertTrue(len(models) > 0)
        for m in models:
            self.assertEqual(m.provider, "openai")

    def test_gemini_builtin_models(self):
        from lucnhan.providers.gemini_provider import GeminiProvider
        models = GeminiProvider._builtin_models()
        self.assertTrue(len(models) > 0)
        for m in models:
            self.assertEqual(m.provider, "gemini")
            self.assertTrue(m.context_window > 0)


class TestProviderCapabilities(unittest.TestCase):
    """All providers must declare streaming and tool_use capabilities."""

    def test_anthropic_capabilities(self):
        from lucnhan.providers.anthropic_provider import AnthropicProvider
        p = AnthropicProvider(api_key="test", model="test")
        caps = p.capabilities
        self.assertTrue(caps.streaming)
        self.assertTrue(caps.tool_use)
        self.assertTrue(caps.vision)

    def test_openai_capabilities(self):
        from lucnhan.providers.openai_provider import OpenAIProvider
        p = OpenAIProvider(api_key="test", model="test")
        caps = p.capabilities
        self.assertTrue(caps.streaming)
        self.assertTrue(caps.tool_use)

    def test_gemini_capabilities(self):
        from lucnhan.providers.gemini_provider import GeminiProvider
        p = GeminiProvider(api_key="test", model="test")
        caps = p.capabilities
        self.assertTrue(caps.streaming)
        self.assertTrue(caps.tool_use)


if __name__ == "__main__":
    unittest.main()
