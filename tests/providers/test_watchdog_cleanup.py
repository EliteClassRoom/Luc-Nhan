"""Provider watchdog lifecycle tests.

Every ``_stream_chunks`` spawns a daemon watchdog thread that parks on the
caller's ``cancel_event``.  That event is a long-lived loop-level event that
is usually never set for a given request, so every streaming request leaked
one parked daemon thread (one per provider, per request).  These tests pin
the contract that:

1. A stream that completes normally leaves NO watchdog thread behind —
   the watchdog must exit shortly after the generator is exhausted.
2. A mid-stream cancel still interrupts a blocked read promptly — the
   watchdog must force-close the underlying stream so the consumer's
   cancellation check fires instead of waiting for the next SSE chunk.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.providers.anthropic_provider import AnthropicProvider
from rikugan.providers.codex_provider import CodexProvider
from rikugan.providers.gemini_provider import GeminiProvider
from rikugan.providers.openai_provider import OpenAIProvider

# How long a completed stream's watchdog thread gets to exit before the
# leak assertion fires.  Must comfortably exceed the watchdog poll interval.
LEAK_JOIN_TIMEOUT = 2.0
# How long the consumer may stay blocked after cancel fires, and the
# join ceiling for the consumer thread.
CANCEL_MAX_LATENCY = 2.0
CANCEL_JOIN_TIMEOUT = 3.0


# ---------------------------------------------------------------------------
# Fake streams per provider
# ---------------------------------------------------------------------------


def _openai_chunk(text: str = "hi") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=None), finish_reason=None)],
        usage=None,
    )


def _anthropic_events() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="text", text="")),
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="hi")),
        SimpleNamespace(type="content_block_stop"),
    ]


def _gemini_chunk(text: str = "hi") -> SimpleNamespace:
    return SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(parts=[SimpleNamespace(text=text, function_call=None, thought=False)])
            )
        ],
        usage_metadata=None,
    )


def _codex_lines() -> list[bytes]:
    return [
        b'data: {"type":"response.output_text.delta","delta":"hi"}',
        b'data: {"type":"response.completed",'
        b'"response":{"usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3}}}',
    ]


class _BlockingOpenAIStream:
    """Yields one chunk then blocks on iteration until close() unblocks us."""

    def __init__(self) -> None:
        self.close_called = threading.Event()
        self.iter_started = threading.Event()

    def __iter__(self):
        self.iter_started.set()
        yield _openai_chunk()
        if not self.close_called.wait(timeout=5.0):
            raise RuntimeError("test bug: stream never closed")
        # Real SDKs raise when the socket is closed underneath the read.
        raise RuntimeError("stream closed by client")

    def close(self) -> None:
        self.close_called.set()


class _BlockingAnthropicStream:
    """Context-manager stream that blocks mid-iteration until close()."""

    def __init__(self) -> None:
        self.close_called = threading.Event()
        self.iter_started = threading.Event()

    def __iter__(self):
        self.iter_started.set()
        yield from _anthropic_events()[:2]
        if not self.close_called.wait(timeout=5.0):
            raise RuntimeError("test bug: stream close() never called")
        raise RuntimeError("stream closed by client")

    def close(self) -> None:
        self.close_called.set()

    def __enter__(self) -> _BlockingAnthropicStream:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.close()
        return False


class _BlockingGeminiStream:
    """Yields one chunk then blocks on iteration until close()."""

    def __init__(self) -> None:
        self.close_called = threading.Event()
        self.iter_started = threading.Event()

    def __iter__(self):
        self.iter_started.set()
        yield _gemini_chunk()
        if not self.close_called.wait(timeout=5.0):
            raise RuntimeError("test bug: stream never closed")
        raise RuntimeError("stream closed by client")

    def close(self) -> None:
        self.close_called.set()


class _BlockingCodexResponse:
    """urllib-style byte-line response that blocks until close()."""

    def __init__(self) -> None:
        self.close_called = threading.Event()
        self.iter_started = threading.Event()

    def __iter__(self):
        self.iter_started.set()
        yield b'data: {"type":"response.output_text.delta","delta":"hi"}'
        if not self.close_called.wait(timeout=5.0):
            raise RuntimeError("test bug: response never closed")
        raise RuntimeError("response closed by client")

    def close(self) -> None:
        self.close_called.set()


# ---------------------------------------------------------------------------
# Per-provider scenarios: how to build a provider and a client whose
# ``_stream_chunks`` either completes normally or blocks mid-stream.
# ---------------------------------------------------------------------------


class _OpenAIScenario:
    name = "openai"

    @staticmethod
    def make_provider() -> OpenAIProvider:
        return OpenAIProvider(api_key="test-key", model="gpt-test")

    @staticmethod
    def make_client(provider: OpenAIProvider, blocking: _BlockingOpenAIStream | None) -> MagicMock:
        del provider  # the MagicMock client is the stream boundary
        client = MagicMock()
        client.chat.completions.create.return_value = blocking if blocking else [_openai_chunk()]
        return client

    @staticmethod
    def make_blocking() -> _BlockingOpenAIStream:
        return _BlockingOpenAIStream()


class _AnthropicScenario:
    name = "anthropic"

    @staticmethod
    def make_provider() -> AnthropicProvider:
        return AnthropicProvider(api_key="test-key", model="claude-test")

    @staticmethod
    def make_client(provider: AnthropicProvider, blocking: _BlockingAnthropicStream | None) -> MagicMock:
        del provider  # the MagicMock client is the stream boundary
        client = MagicMock()
        if blocking is not None:
            client.messages.stream.return_value = blocking
        else:
            client.messages.stream.return_value.__enter__.return_value = _anthropic_events()
        return client

    @staticmethod
    def make_blocking() -> _BlockingAnthropicStream:
        return _BlockingAnthropicStream()


class _GeminiScenario:
    name = "gemini"

    @staticmethod
    def make_provider() -> GeminiProvider:
        return GeminiProvider(api_key="test-key", model="gemini-test")

    @staticmethod
    def make_client(provider: GeminiProvider, blocking: _BlockingGeminiStream | None) -> MagicMock:
        del provider  # the MagicMock client is the stream boundary
        client = MagicMock()
        client.models.generate_content_stream.return_value = blocking if blocking else [_gemini_chunk()]
        return client

    @staticmethod
    def make_blocking() -> _BlockingGeminiStream:
        return _BlockingGeminiStream()


class _CodexScenario:
    name = "codex"

    @staticmethod
    def make_provider() -> CodexProvider:
        return CodexProvider(model="gpt-5-codex")

    @staticmethod
    def make_client(provider: CodexProvider, blocking: _BlockingCodexResponse | None) -> MagicMock:
        # Codex bypasses the SDK client entirely: ``_stream_chunks`` calls
        # ``self._request(...)`` for the urllib response.
        response = blocking if blocking else _codex_lines()
        provider._request = lambda method, path, payload, stream=False: response
        return MagicMock()  # unused by CodexProvider._stream_chunks

    @staticmethod
    def make_blocking() -> _BlockingCodexResponse:
        return _BlockingCodexResponse()


_SCENARIOS = [_OpenAIScenario(), _AnthropicScenario(), _GeminiScenario(), _CodexScenario()]


class TestWatchdogExitsAfterNormalCompletion(unittest.TestCase):
    """A stream that finishes normally must not leak its watchdog thread.

    The cancel event handed to ``_stream_chunks`` is a long-lived loop-level
    event that stays unset for this request — exactly the production shape
    that used to park one daemon thread per request forever.
    """

    def test_no_watchdog_thread_leaks_after_stream_completes(self) -> None:
        for scenario in _SCENARIOS:
            with self.subTest(provider=scenario.name):
                provider = scenario.make_provider()
                client = scenario.make_client(provider, None)
                stale_cancel = threading.Event()  # never set for this request

                before = set(threading.enumerate())
                list(provider._stream_chunks(client, {}, cancel_event=stale_cancel))

                # Give any per-request thread a short window to exit.
                deadline = time.monotonic() + LEAK_JOIN_TIMEOUT
                lingering: list[threading.Thread] = []
                while True:
                    lingering = [t for t in threading.enumerate() if t not in before and t.is_alive()]
                    if not lingering or time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)

                self.assertEqual(
                    [],
                    lingering,
                    f"{scenario.name}: threads spawned by _stream_chunks outlived the "
                    f"completed stream (waited {LEAK_JOIN_TIMEOUT}s): "
                    f"{[t.name for t in lingering]}",
                )


class TestCancelStillInterruptsMidStream(unittest.TestCase):
    """The cleanup fix must not soften cancel semantics: a blocked read is
    still force-closed promptly when cancel fires mid-stream."""

    def test_mid_stream_cancel_closes_stream_promptly(self) -> None:
        for scenario in _SCENARIOS:
            with self.subTest(provider=scenario.name):
                self._assert_cancel_interrupts(scenario)

    def _assert_cancel_interrupts(self, scenario: Any) -> None:
        """One scenario's mid-stream cancel: blocked read must unblock."""
        provider = scenario.make_provider()
        blocking = scenario.make_blocking()
        client = scenario.make_client(provider, blocking)
        cancel = threading.Event()

        def consume() -> None:
            list(provider._stream_chunks(client, {}, cancel_event=cancel))

        consumer = threading.Thread(target=consume, daemon=True, name=f"{scenario.name}-consumer")
        consumer.start()
        self.assertTrue(
            blocking.iter_started.wait(timeout=5.0),
            f"{scenario.name}: stream iteration never started",
        )
        time.sleep(0.1)  # let the consumer park inside the blocked read

        start = time.monotonic()
        cancel.set()
        consumer.join(timeout=CANCEL_JOIN_TIMEOUT)
        elapsed = time.monotonic() - start

        self.assertFalse(
            consumer.is_alive(),
            f"{scenario.name}: consumer still blocked {elapsed:.2f}s after "
            f"cancel — watchdog did not force-close the stream",
        )
        self.assertTrue(
            blocking.close_called.is_set(),
            f"{scenario.name}: stream.close() was never called by the watchdog",
        )
        self.assertLess(
            elapsed,
            CANCEL_MAX_LATENCY,
            f"{scenario.name}: cancel took {elapsed:.2f}s to interrupt the read",
        )


if __name__ == "__main__":
    unittest.main()
