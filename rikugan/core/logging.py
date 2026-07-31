"""Rikugan logging bootstrap.

This module is the single public API that all rikugan modules import.
Sink implementations live in ``core.log_sinks`` — changes to file
rotation policy, host integration, or telemetry format do not
propagate to importers of this module.

Public API:
    get_logger, log_info, log_warning, log_error, log_debug, log_trace
    register_host_sink   (re-exported from log_sinks)
    HostOutputHandler, IDAHandler, _FlushFileHandler  (for tests)
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

from .log_sinks import (  # noqa: F401 — re-exported for tests
    STRUCTURED_EVENT_ALLOWLIST,
    HostOutputHandler,
    IDAHandler,
    JSONScalar,
    _FlushFileHandler,
    _JSONFormatter,
    _log_file_path,
    _read_configured_host_level,
    register_host_sink,
    resolve_log_level,
    set_host_log_level,
)

_logger: logging.Logger | None = None


# Third-party SDKs (openai, httpx, httpcore) emit DEBUG records that
# dump full request bodies. On Windows cp1252 streams this triggers
# UnicodeEncodeError when the body contains non-ASCII content, which
# crashes the logging thread mid-turn. Centralized here so both
# ``get_logger`` (UI bootstrap) and the LLM provider boundary call
# the same idempotent helper without duplicating handlers.
_SUPPRESSED_SDK_LOGGERS: tuple[str, ...] = (
    "openai",
    "openai._base_client",
    "httpx",
    "httpcore",
)


def silence_sdk_debug_loggers() -> None:
    """Raise the level on chat-halting SDK loggers. Idempotent."""
    for _name in _SUPPRESSED_SDK_LOGGERS:
        logging.getLogger(_name).setLevel(logging.WARNING)


def get_logger() -> logging.Logger:
    # Idempotent safeguard against chat-halting SDK DEBUG records.
    silence_sdk_debug_loggers()
    global _logger
    if _logger is not None:
        return _logger
    _logger = logging.getLogger("Rikugan")
    _logger.setLevel(logging.DEBUG)
    # Stop propagation so the record is not also delivered to the
    # root logger's handlers (a cp1252 StreamHandler installed by the
    # IDA Python runtime, for example). Rikugan owns all of its own
    # handlers below; falling back to the root would also re-emit
    # the record and crash on Unicode it cannot encode.
    _logger.propagate = False

    fmt = logging.Formatter(
        "[Lục nhãn %(asctime)s.%(msecs)03d %(levelname)s %(threadName)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # Host output handler — default level is read from the user's
    # ``ida_output_log_level`` setting (falls back to WARNING so routine
    # INFO/DEBUG chatter does not spam IDA's Output window).  File and
    # JSONL handlers below remain at DEBUG/INFO regardless of this setting
    # so the full diagnostic stream is always recoverable from disk.
    host_handler = HostOutputHandler()
    host_handler.setLevel(_read_configured_host_level())
    host_handler.setFormatter(logging.Formatter("[Lục nhãn] %(levelname)s: %(message)s"))
    _logger.addHandler(host_handler)

    # File handler (DEBUG — everything, flush immediately)
    try:
        path = _log_file_path()
        file_handler = _FlushFileHandler(path, mode="w", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        _logger.addHandler(file_handler)
        _logger.debug(f"=== Lục nhãn debug log started — {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
        _logger.debug(f"Log file: {path}")
        _logger.debug(f"Python: {sys.version}")
        _logger.debug(f"Thread: {threading.current_thread().name}")
    except OSError as e:
        _logger.warning(f"Could not open debug log file: {e}")

    # Structured JSON log (JSONL format for machine parsing / analytics)
    try:
        json_path = os.path.join(os.path.dirname(_log_file_path()), "rikugan_structured.jsonl")
        json_handler = _FlushFileHandler(json_path, mode="a", encoding="utf-8")
        json_handler.setLevel(logging.INFO)
        json_handler.setFormatter(_JSONFormatter())
        _logger.addHandler(json_handler)
    except OSError as e:
        sys.stderr.write(f"[Lục nhãn] Could not open structured log file: {e}\n")

    return _logger


def log_info(msg: str) -> None:
    get_logger().info(msg)


def log_warning(msg: str) -> None:
    get_logger().warning(msg)


def log_error(msg: str) -> None:
    get_logger().error(msg)


def log_debug(msg: str) -> None:
    logger = get_logger()
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(msg)


def log_trace(label: str) -> None:
    """Verbose trace-level log (logged at DEBUG level with TRACE prefix)."""
    logger = get_logger()
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"TRACE {label}")


def log_structured(event: dict[str, JSONScalar]) -> None:
    """Emit a structured attempt event to the JSONL telemetry stream.

    The event payload MUST use only keys from :data:`STRUCTURED_EVENT_ALLOWLIST`
    and MUST contain only :data:`JSONScalar` values (str / int / float / bool /
    None).  Any deviation raises :class:`KeyError` or :class:`TypeError` so a
    bad call site fails fast at the producer — it is never acceptable to
    silently drop a misused key, because that would let untrusted content
    flow into the structured log via a different (unknown) field name.

    On success, the event is emitted via the standard logging pipeline at
    INFO level with message ``"agent_attempt"``.  The JSON formatter reads
    ``record.rikugan_event`` and emits a single ``rikugan_event`` JSON
    object after sanitizing every string value (injection markers and
    lone surrogates are stripped).  The human-readable log line is left
    untouched.
    """
    unknown = set(event) - STRUCTURED_EVENT_ALLOWLIST
    if unknown:
        raise KeyError(f"Unknown structured log keys: {sorted(unknown)}")
    if any(not isinstance(value, (str, int, float, bool, type(None))) for value in event.values()):
        raise TypeError("Structured log values must be JSON scalars")
    get_logger().info("agent_attempt", extra={"rikugan_event": dict(event)})
