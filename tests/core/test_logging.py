"""Tests for the logging module."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.core.logging import (  # noqa: E402
    IDAHandler,
    _FlushFileHandler,
    get_logger,
    log_debug,
    log_error,
    log_info,
    log_trace,
    log_warning,
)


class _CaptureHandler(logging.Handler):
    """Test handler that captures log records."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class TestLogFunctions(unittest.TestCase):
    """Test the convenience logging functions."""

    def setUp(self):
        self._capture = _CaptureHandler()
        get_logger().addHandler(self._capture)

    def tearDown(self):
        get_logger().removeHandler(self._capture)

    def test_get_logger_returns_logger(self):
        logger = get_logger()
        self.assertIsInstance(logger, logging.Logger)
        self.assertEqual(logger.name, "Rikugan")

    def test_get_logger_singleton(self):
        a = get_logger()
        b = get_logger()
        self.assertIs(a, b)

    def test_logger_has_handlers(self):
        logger = get_logger()
        handler_types = {type(h) for h in logger.handlers}
        self.assertIn(IDAHandler, handler_types)

    def test_log_info(self):
        log_info("info_test_message")
        matching = [
            r for r in self._capture.records if r.levelno == logging.INFO and "info_test_message" in r.getMessage()
        ]
        self.assertEqual(len(matching), 1)

    def test_log_warning(self):
        log_warning("warn_test_message")
        matching = [
            r for r in self._capture.records if r.levelno == logging.WARNING and "warn_test_message" in r.getMessage()
        ]
        self.assertEqual(len(matching), 1)

    def test_log_error(self):
        log_error("error_test_message")
        matching = [
            r for r in self._capture.records if r.levelno == logging.ERROR and "error_test_message" in r.getMessage()
        ]
        self.assertEqual(len(matching), 1)

    def test_log_debug(self):
        log_debug("debug_test_message")
        matching = [
            r for r in self._capture.records if r.levelno == logging.DEBUG and "debug_test_message" in r.getMessage()
        ]
        self.assertEqual(len(matching), 1)

    def test_log_trace(self):
        log_trace("trace_label")
        matching = [
            r for r in self._capture.records if r.levelno == logging.DEBUG and "TRACE trace_label" in r.getMessage()
        ]
        self.assertEqual(len(matching), 1)


class TestIDAHandler(unittest.TestCase):
    def test_emit_formats_and_delivers(self):
        handler = IDAHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test message",
            args=(),
            exc_info=None,
        )
        # Should not raise (delivers to ida_kernwin.msg mock or stderr)
        handler.emit(record)

    def test_emit_to_stderr_when_no_host_sink(self):
        """When no host sink is registered, HostOutputHandler falls back to stderr."""
        import io

        import rikugan.core.log_sinks as sinks_mod

        handler = IDAHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="stderr test",
            args=(),
            exc_info=None,
        )
        saved_sink = sinks_mod._host_sink
        saved_resolve = sinks_mod._resolve_host_sink
        sinks_mod._host_sink = None
        sinks_mod._resolve_host_sink = lambda: None  # prevent auto-detection
        captured = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured
        try:
            handler.emit(record)
        finally:
            sys.stderr = old_stderr
            sinks_mod._host_sink = saved_sink
            sinks_mod._resolve_host_sink = saved_resolve

        self.assertIn("stderr test", captured.getvalue())


class TestFlushFileHandler(unittest.TestCase):
    def test_emit_and_flush(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name

        try:
            handler = _FlushFileHandler(path, mode="w")
            handler.setFormatter(logging.Formatter("%(message)s"))
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg="flush test",
                args=(),
                exc_info=None,
            )
            handler.emit(record)
            handler.close()

            with open(path) as f:
                content = f.read()
            self.assertIn("flush test", content)
        finally:
            os.unlink(path)

    def test_log_file_path_creates_directory(self):
        from rikugan.core.log_sinks import _log_file_path

        path = _log_file_path()
        self.assertTrue(os.path.isdir(os.path.dirname(path)))
        self.assertTrue(path.endswith("rikugan_debug.log"))


class TestSilenceSDKDebugLoggers(unittest.TestCase):
    """Regression: ``silence_sdk_debug_loggers`` must raise the level on
    chat-halting SDK loggers so an OpenAI request that contains non-ASCII
    content does not crash the logging thread with UnicodeEncodeError on
    Windows cp1252 streams. The test snapshots only the four SDK logger
    levels so it does not interfere with the rikugan.core.logging
    singleton, the Rikugan logger's handler set, or any sibling tests.
    """

    _SDK_LOGGERS = ("openai", "openai._base_client", "httpx", "httpcore")

    def setUp(self):
        self._prior_levels = {
            name: logging.getLogger(name).level for name in self._SDK_LOGGERS
        }
        for name in self._SDK_LOGGERS:
            logging.getLogger(name).setLevel(logging.NOTSET)

    def tearDown(self):
        for name, level in self._prior_levels.items():
            logging.getLogger(name).setLevel(level)

    def test_openai_logger_level_is_warning(self):
        from rikugan.core.logging import silence_sdk_debug_loggers

        silence_sdk_debug_loggers()
        self.assertEqual(logging.getLogger("openai").level, logging.WARNING)

    def test_httpx_logger_level_is_warning(self):
        from rikugan.core.logging import silence_sdk_debug_loggers

        silence_sdk_debug_loggers()
        self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)

    def test_helper_is_idempotent(self):
        from rikugan.core.logging import silence_sdk_debug_loggers

        silence_sdk_debug_loggers()
        silence_sdk_debug_loggers()
        self.assertEqual(logging.getLogger("openai").level, logging.WARNING)
        self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)


class TestRikuganLoggerDoesNotPropagate(unittest.TestCase):
    """Regression: rikugan records must not propagate to the root logger.
    Otherwise an inherited cp1252 StreamHandler (e.g. the one IDA Pro
    installs on sys.stderr) crashes on Unicode in the message.

    Concretely: install a root StreamHandler that uses the cp1252
    codec, emit a record whose text contains ``→`` (U+2192), and
    assert no UnicodeEncodeError is raised.
    """

    def test_codepage_root_handler_does_not_crash(self):
        import io

        from rikugan.core import logging as rikugan_logging_module
        from rikugan.core.logging import get_logger

        # Reset the singleton so get_logger() rebuilds from scratch;
        # otherwise an already-cached logger from an earlier test could
        # mask the create-time propagate=False setting. Snapshot the
        # pre-existing handler set so we can detach and close only the
        # handlers the rebuild appended.
        named = logging.getLogger("Rikugan")
        prior_handlers = list(named.handlers)
        prior_singleton = rikugan_logging_module._logger
        rikugan_logging_module._logger = None
        try:
            rikugan_logger = get_logger()
        finally:
            current = logging.getLogger("Rikugan")
            for handler in list(current.handlers):
                if handler not in prior_handlers:
                    current.removeHandler(handler)
                    try:
                        handler.close()
                    except Exception:
                        pass
            rikugan_logging_module._logger = prior_singleton

        self.assertFalse(
            rikugan_logger.propagate,
            "get_logger() must set propagate=False at creation",
        )
        root_logger = logging.getLogger()

        class _CP1252Stream(io.TextIOBase):
            encoding = "cp1252"

            def __init__(self):
                self._buf = io.StringIO()

            def write(self, s):
                self._buf.write(s)
                return len(s)

            def flush(self):
                self._buf.flush()

            def getvalue(self):
                return self._buf.getvalue()

        sink = _CP1252Stream()
        root_handler = logging.StreamHandler(sink)
        root_handler.setLevel(logging.DEBUG)
        prior_root_handlers = list(root_logger.handlers)
        root_logger.addHandler(root_handler)
        try:
            try:
                rikugan_logger.debug("skill: /test → arrow glyph")
            except UnicodeEncodeError as exc:  # pragma: no cover
                self.fail(f"propagation to cp1252 handler crashed: {exc}")
        finally:
            root_logger.removeHandler(root_handler)
            for handler in list(root_logger.handlers):
                if handler not in prior_root_handlers:
                    root_logger.removeHandler(handler)

        # The root handler is never invoked when propagate is False,
        # so the cp1252 sink stays empty.
        self.assertEqual(
            sink.getvalue(),
            "",
            "root cp1252 handler must not receive rikugan records when propagate=False",
        )

    def test_propagate_is_false_by_default(self):
        from rikugan.core.logging import get_logger

        self.assertFalse(
            get_logger().propagate,
            "get_logger() must disable propagation so root handlers do not crash",
        )


class TestProviderBoundarySilencesSDKLoggers(unittest.TestCase):
    """Regression: ``LLMProvider.chat`` must call
    ``silence_sdk_debug_loggers`` immediately before ``_call_api`` so the
    safeguard holds even when ``get_logger`` was never called. This proves
    the direct /report provider path, not just the helper in isolation.
    """

    _SDK_LOGGERS = ("openai", "openai._base_client", "httpx", "httpcore")

    def setUp(self):
        self._prior_levels = {
            name: logging.getLogger(name).level for name in self._SDK_LOGGERS
        }
        for name in self._SDK_LOGGERS:
            logging.getLogger(name).setLevel(logging.DEBUG)

    def tearDown(self):
        for name, level in self._prior_levels.items():
            logging.getLogger(name).setLevel(level)

    def test_chat_silences_openai_before_call_api(self):
        from rikugan.providers.base import LLMProvider

        observed_level: dict[str, int] = {}

        class _RecordingProvider(LLMProvider):
            def __init__(self):
                super().__init__(api_key="", model="dummy")

            @property
            def name(self) -> str:
                return "dummy"

            @property
            def capabilities(self):
                return None  # type: ignore[return-value]

            def _get_client(self):
                return None

            def _call_api(self, client, kwargs):
                observed_level["openai"] = logging.getLogger("openai").level
                return None

            def _normalize_response(self, raw):
                from rikugan.core.types import Message

                return Message(role="assistant", content="")

            def _format_messages(self, messages):
                return []

            def _build_request_kwargs(self, *_args, **_kwargs):
                return {}

            def _handle_api_error(self, _e):
                raise NotImplementedError

            def _stream_chunks(self, *_args, **_kwargs):
                return iter(())

            @staticmethod
            def _builtin_models():
                return []

            def _fetch_models_live(self):
                return []

        provider = _RecordingProvider()
        provider.chat(messages=[])

        self.assertEqual(
            observed_level["openai"],
            logging.WARNING,
            "LLMProvider.chat must call silence_sdk_debug_loggers before _call_api",
        )


# ---------------------------------------------------------------------------
# Structured attempt logging (telemetry allowlist)
# ---------------------------------------------------------------------------

from rikugan.core.log_sinks import _JSONFormatter  # noqa: E402
from rikugan.core.logging import log_structured  # noqa: E402


def test_json_formatter_includes_allowlisted_attempt_event():
    formatter = _JSONFormatter()
    record = logging.LogRecord("Rikugan", logging.INFO, "", 0, "agent_attempt", (), None)
    record.rikugan_event = {
        "provider": "glm",
        "attempt_number": 1,
        "disposition": "degenerated",
        "discarded_attempt": True,
    }

    payload = json.loads(formatter.format(record))

    assert payload["rikugan_event"]["provider"] == "glm"
    assert payload["rikugan_event"]["discarded_attempt"] is True


def test_log_structured_rejects_content_keys_and_nested_values():
    with pytest.raises(KeyError):
        log_structured({"reasoning_content": "secret"})
    with pytest.raises(TypeError):
        log_structured({"provider": {"nested": "glm"}})


def test_structured_strings_strip_role_markers_and_surrogates():
    formatter = _JSONFormatter()
    record = logging.LogRecord("Rikugan", logging.INFO, "", 0, "agent_attempt", (), None)
    record.rikugan_event = {"provider": "[SYSTEM] glm\ud800"}

    payload = json.loads(formatter.format(record))

    assert "[SYSTEM]" not in payload["rikugan_event"]["provider"]
    assert "\ud800" not in payload["rikugan_event"]["provider"]


# ---------------------------------------------------------------------------
# Telemetry integration: one allowlisted record per logical attempt
# ---------------------------------------------------------------------------

from rikugan.agent.modes.turn_helpers import execute_single_turn  # noqa: E402
from rikugan.core.types import (  # noqa: E402
    LLMRequestContext,
    Message,
    ModelInfo,
    ProviderCapabilities,
    Role,
    StreamChunk,
    TokenUsage,
    TurnDisposition,
)
from rikugan.providers.base import LLMProvider  # noqa: E402
from rikugan.state.session import SessionState  # noqa: E402
from rikugan.tools.base import ParameterSchema, ToolDefinition  # noqa: E402
from rikugan.tools.registry import ToolRegistry  # noqa: E402

# Reasoning payload large enough to trip the hard ceiling.
_DEGENERATED_REASONING = "outputting read_bytes tool now\n" * 3500


class _ScriptedGLMProvider(LLMProvider):
    """Minimal GLM-flagged provider returning scripted chunk lists."""

    def __init__(self, responses: list[list[StreamChunk]] | None = None):
        super().__init__(api_key="test", model="glm-5.2")
        self._responses = responses or []
        self._call_count = 0

    @property
    def name(self) -> str:
        return "glm"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(reasoning_content=True)

    def _get_client(self):
        return None

    def _fetch_models_live(self) -> list[ModelInfo]:
        return [ModelInfo(id="glm-5.2", name="GLM-5.2", provider="glm")]

    @staticmethod
    def _builtin_models() -> list[ModelInfo]:
        return [ModelInfo(id="glm-5.2", name="GLM-5.2", provider="glm")]

    def _format_messages(self, messages):
        return messages

    def _normalize_response(self, raw):
        return raw

    def _build_request_kwargs(self, messages, tools, temperature, max_tokens, system, **kwargs):
        return {}

    def _call_api(self, client, kwargs):
        return None

    def _handle_api_error(self, e):
        raise e

    def _stream_chunks(self, client, kwargs, cancel_event=None):
        yield from ()

    def chat(self, messages, tools=None, temperature=0.3, max_tokens=4096, system="", **kwargs):
        return Message(role=Role.ASSISTANT, content="mock")

    def chat_stream(
        self,
        messages,
        tools=None,
        temperature=0.3,
        max_tokens=4096,
        system="",
        cancel_event=None,
        *,
        request_context: LLMRequestContext | None = None,
    ):
        self._call_count += 1
        idx = self._call_count - 1
        if idx < len(self._responses):
            yield from self._responses[idx]
        else:
            yield StreamChunk(text="No more scripted responses.")


def _drive_turn(responses: list[list[StreamChunk]]) -> list[dict]:
    """Drive ``execute_single_turn`` once with *responses* and return the
    list of captured ``rikugan_event`` dicts.
    """
    import json as _json

    from rikugan.agent.loop import AgentLoop
    from rikugan.core.config import RikuganConfig

    capture = _CaptureHandler()
    capture.setFormatter(_JSONFormatter())
    get_logger().addHandler(capture)
    try:
        provider = _ScriptedGLMProvider(responses=responses)
        config = RikuganConfig()
        config.auto_context = False
        config.provider.name = "glm"
        config.provider.model = "glm-5.2"
        config.provider.api_key = "test"
        config.provider.extra = {
            "dialect": "glm",
            "thinking": {"enabled": True, "reasoning_effort": "max", "preserve": True},
            "degeneration_guard": {
                "enabled": True,
                "reasoning_token_ceiling": 16_384,
                "retry_without_thinking": True,
                "recovery_max_tokens": 16_384,
            },
        }
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="read_bytes",
                description="Read bytes",
                parameters=[ParameterSchema(name="address", type="integer", description="addr", required=True)],
                handler=lambda address: f"bytes at {address}",
                category="navigation",
            )
        )
        session = SessionState(provider_name="glm", model_name="glm-5.2")
        loop = AgentLoop(provider=provider, tool_registry=registry, config=config, session=session)
        tools_schema = loop._build_tools_schema(active_skill=None, use_exploration_mode=False)

        gen = execute_single_turn(loop, "system prompt", tools_schema)
        while True:
            try:
                next(gen)
            except StopIteration:
                break

        records: list[dict] = []
        for rec in capture.records:
            if rec.getMessage() == "agent_attempt":
                payload = _json.loads(capture.formatter.format(rec))
                ev = payload.get("rikugan_event")
                if isinstance(ev, dict):
                    records.append(ev)
        return records
    finally:
        get_logger().removeHandler(capture)


def test_recovered_turn_logs_two_content_free_attempt_records():
    records = _drive_turn(
        responses=[
            [  # attempt 1 — degenerated reasoning
                StreamChunk(reasoning_delta=_DEGENERATED_REASONING),
                StreamChunk(usage=TokenUsage(prompt_tokens=100, completion_tokens=5000, total_tokens=5100)),
            ],
            [  # attempt 2 — successful text
                StreamChunk(text="visible"),
                StreamChunk(usage=TokenUsage(prompt_tokens=50, completion_tokens=5, total_tokens=55)),
            ],
        ]
    )

    assert [record["attempt_number"] for record in records] == [1, 2]
    # Attempt 1 was discarded (degenerated reasoning).
    assert records[0]["discarded_attempt"] is True
    # The guard closes the stream before the usage chunk arrives, so the
    # degenerated attempt's usage is always estimated.
    assert records[0]["usage_provenance"] == "estimated"
    # Attempt 2 succeeded.
    assert records[1]["recovery_result"] == "success"
    # No content-bearing fields may appear in any record.
    forbidden = {"content", "reasoning_content", "tool_args", "tool_results", "messages", "error"}
    assert not any(forbidden & set(record) for record in records)


def test_normal_turn_logs_single_content_free_record():
    """A non-recovery turn must emit exactly one attempt record."""
    records = _drive_turn(
        responses=[
            [
                StreamChunk(text="hello"),
                StreamChunk(usage=TokenUsage(prompt_tokens=50, completion_tokens=5, total_tokens=55)),
            ],
        ]
    )

    assert len(records) == 1
    assert records[0]["attempt_number"] == 1
    assert records[0]["discarded_attempt"] is False
    forbidden = {"content", "reasoning_content", "tool_args", "tool_results", "messages", "error"}
    assert not any(forbidden & set(record) for record in records)


# ---------------------------------------------------------------------------
# Telemetry for error/cancellation paths (one record per started attempt)
# ---------------------------------------------------------------------------

from rikugan.core.errors import CancellationError, ProviderError  # noqa: E402


class _ErroringGLMProvider(_ScriptedGLMProvider):
    """GLM-flagged provider that raises on specific attempt numbers."""

    def __init__(
        self,
        responses: list[list[StreamChunk]] | None = None,
        error_on_attempt: int | None = None,
        error_cls: type[Exception] = ProviderError,
    ):
        super().__init__(responses=responses)
        self._error_on_attempt = error_on_attempt
        self._error_cls = error_cls

    def chat_stream(
        self,
        messages,
        tools=None,
        temperature=0.3,
        max_tokens=4096,
        system="",
        cancel_event=None,
        *,
        request_context: LLMRequestContext | None = None,
    ):
        self._call_count += 1
        if self._error_on_attempt is not None and self._call_count == self._error_on_attempt:
            raise self._error_cls("simulated failure")
        idx = self._call_count - 1
        if self._responses and idx < len(self._responses):
            yield from self._responses[idx]
        else:
            yield StreamChunk(text="No more scripted responses.")


def _drive_turn_with_provider(provider, capture_records=True, cancel_before_recovery=False):
    """Drive execute_single_turn with *provider* and return (records, result, raised).

    When *cancel_before_recovery* is True, sets the loop's cancellation flag
    after attempt 1 degeneration so that ``_check_cancelled()`` at the recovery
    boundary raises ``CancellationError``.
    """
    import json as _json

    from rikugan.agent.loop import AgentLoop
    from rikugan.core.config import RikuganConfig

    capture = _CaptureHandler()
    capture.setFormatter(_JSONFormatter())
    get_logger().addHandler(capture)
    try:
        config = RikuganConfig()
        config.auto_context = False
        config.provider.name = "glm"
        config.provider.model = "glm-5.2"
        config.provider.api_key = "test"
        config.provider.extra = {
            "dialect": "glm",
            "thinking": {"enabled": True, "reasoning_effort": "max", "preserve": True},
            "degeneration_guard": {
                "enabled": True,
                "reasoning_token_ceiling": 16_384,
                "retry_without_thinking": True,
                "recovery_max_tokens": 16_384,
            },
        }
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="read_bytes",
                description="Read bytes",
                parameters=[ParameterSchema(name="address", type="integer", description="addr", required=True)],
                handler=lambda address: f"bytes at {address}",
                category="navigation",
            )
        )
        session = SessionState(provider_name="glm", model_name="glm-5.2")
        loop = AgentLoop(provider=provider, tool_registry=registry, config=config, session=session)

        if cancel_before_recovery:
            # Wrap _stream_llm_turn to set the cancellation flag AFTER attempt 1
            # completes (degenerates) but before the recovery boundary check.
            original_stream = loop._stream_llm_turn

            def _stream_and_cancel(*args, **kwargs):
                outcome = yield from original_stream(*args, **kwargs)
                # Set cancellation after attempt 1 finishes — the recovery
                # boundary _check_cancelled() will now raise.
                loop._cancelled.set()
                return outcome

            loop._stream_llm_turn = _stream_and_cancel  # type: ignore[method-assign]

        tools_schema = loop._build_tools_schema(active_skill=None, use_exploration_mode=False)

        gen = execute_single_turn(loop, "system prompt", tools_schema)
        result_or_exc = None
        raised = None
        while True:
            try:
                next(gen)
            except StopIteration as si:
                result_or_exc = si.value
                break
            except CancellationError as e:
                raised = e
                break

        records: list[dict] = []
        if capture_records:
            for rec in capture.records:
                if rec.getMessage() == "agent_attempt":
                    payload = _json.loads(capture.formatter.format(rec))
                    ev = payload.get("rikugan_event")
                    if isinstance(ev, dict):
                        records.append(ev)
        return records, result_or_exc, raised
    finally:
        get_logger().removeHandler(capture)


def test_attempt1_provider_error_logs_one_discarded_record():
    """Attempt 1 ProviderError must emit exactly one record: discarded, failure."""
    provider = _ErroringGLMProvider(error_on_attempt=1, error_cls=ProviderError)
    records, _result, _raised = _drive_turn_with_provider(provider)

    assert len(records) == 1
    assert records[0]["attempt_number"] == 1
    assert records[0]["discarded_attempt"] is True
    assert records[0]["recovery_result"] == "failure"
    forbidden = {"content", "reasoning_content", "tool_args", "tool_results", "messages", "error"}
    assert not any(forbidden & set(record) for record in records)


def test_attempt1_cancellation_logs_one_record_and_reraises():
    """Attempt 1 CancellationError must emit exactly one record, then re-raise."""
    provider = _ErroringGLMProvider(error_on_attempt=1, error_cls=CancellationError)
    records, _result, raised = _drive_turn_with_provider(provider)

    assert len(records) == 1
    assert records[0]["attempt_number"] == 1
    assert records[0]["discarded_attempt"] is True
    assert records[0]["disposition"] == "cancelled"
    assert isinstance(raised, CancellationError)
    forbidden = {"content", "reasoning_content", "tool_args", "tool_results", "messages", "error"}
    assert not any(forbidden & set(record) for record in records)


def test_cancellation_at_recovery_boundary_logs_only_attempt1():
    """Cancellation between attempts: only attempt 1 is logged, no attempt 2."""
    # Attempt 1 degenerates, then _check_cancelled() at recovery boundary raises.
    provider = _ScriptedGLMProvider(
        responses=[
            [
                StreamChunk(reasoning_delta=_DEGENERATED_REASONING),
                StreamChunk(usage=TokenUsage(prompt_tokens=100, completion_tokens=5000, total_tokens=5100)),
            ],
        ]
    )
    records, _result, raised = _drive_turn_with_provider(provider, cancel_before_recovery=True)

    assert len(records) == 1
    assert records[0]["attempt_number"] == 1
    assert records[0]["discarded_attempt"] is True
    assert records[0]["disposition"] == "degenerated"
    assert isinstance(raised, CancellationError)


def test_cancellation_during_attempt2_logs_two_records_and_reraises():
    """Cancellation during attempt 2: both attempts logged, then re-raise."""
    provider = _ErroringGLMProvider(
        responses=[
            [  # attempt 1 degenerates
                StreamChunk(reasoning_delta=_DEGENERATED_REASONING),
                StreamChunk(usage=TokenUsage(prompt_tokens=100, completion_tokens=5000, total_tokens=5100)),
            ],
        ],
        error_on_attempt=2,
        error_cls=CancellationError,
    )
    records, _result, raised = _drive_turn_with_provider(provider)

    assert len(records) == 2
    assert [r["attempt_number"] for r in records] == [1, 2]
    assert records[0]["discarded_attempt"] is True
    assert records[0]["disposition"] == "degenerated"
    assert records[1]["discarded_attempt"] is True
    assert records[1]["disposition"] == "cancelled"
    assert isinstance(raised, CancellationError)
    forbidden = {"content", "reasoning_content", "tool_args", "tool_results", "messages", "error"}
    assert not any(forbidden & set(record) for record in records)


# ---------------------------------------------------------------------------
# Fail-fast: invalid telemetry event must surface, not be silently swallowed
# ---------------------------------------------------------------------------


def test_emit_attempt_telemetry_surfaces_keyerror_from_log_structured(monkeypatch):
    """If log_structured raises KeyError (unknown key), the helper MUST NOT
    silently swallow it — that would defeat the content-free fail-fast guard
    and let a bad call site leak untrusted content via an unknown field.
    """
    import rikugan.agent.modes.turn_helpers as turn_helpers_mod

    def _raise_keyerror(event):
        raise KeyError("Unknown structured log keys: ['sneaky_field']")

    monkeypatch.setattr(turn_helpers_mod, "log_structured", _raise_keyerror)

    outcome = _EmptyOutcomeLike()
    loop = _make_minimal_loop()

    with pytest.raises(KeyError, match="sneaky_field"):
        turn_helpers_mod._emit_attempt_telemetry(loop, outcome, attempt_number=1, discarded_attempt=False)


def test_emit_attempt_telemetry_surfaces_typeerror_from_log_structured(monkeypatch):
    """If log_structured raises TypeError (non-scalar value), the helper MUST
    surface it, not swallow it.
    """
    import rikugan.agent.modes.turn_helpers as turn_helpers_mod

    def _raise_typeerror(event):
        raise TypeError("Structured log values must be JSON scalars")

    monkeypatch.setattr(turn_helpers_mod, "log_structured", _raise_typeerror)

    outcome = _EmptyOutcomeLike()
    loop = _make_minimal_loop()

    with pytest.raises(TypeError, match="JSON scalars"):
        turn_helpers_mod._emit_attempt_telemetry(loop, outcome, attempt_number=1, discarded_attempt=False)


class _EmptyOutcomeLike:
    """Minimal duck-typed outcome for telemetry helper unit tests."""

    visible_text = ""
    reasoning_content = ""
    tool_calls: list = []  # noqa: RUF012
    usage = None
    finish_reason = None
    disposition = TurnDisposition.COMPLETED
    attempt_usage = None
    guard_trigger = ""
    repetition_ratio_millis = 0


def _make_minimal_loop():
    """Build a minimal AgentLoop for telemetry helper unit tests."""
    from rikugan.agent.loop import AgentLoop
    from rikugan.core.config import RikuganConfig

    config = RikuganConfig()
    config.provider.name = "test"
    config.provider.model = "test-model"
    config.provider.extra = {}
    provider = _ScriptedGLMProvider(responses=[])
    registry = ToolRegistry()
    session = SessionState(provider_name="test", model_name="test-model")
    return AgentLoop(provider=provider, tool_registry=registry, config=config, session=session)


if __name__ == "__main__":
    unittest.main()
