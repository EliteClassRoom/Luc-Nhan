"""Logging sink implementations: host output, crash-safe file, and structured JSONL.

Each sink is a self-contained ``logging.Handler`` subclass. The bootstrap
module (``logging.py``) wires them into the Rikugan logger — importers
never need to depend on individual sinks.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable

from .host import get_user_config_base_dir

# ---------------------------------------------------------------------------
# Log-level mapping
# ---------------------------------------------------------------------------

# Sentinel value used to suppress host output entirely.  Setting a
# handler's level to ``logging.CRITICAL + 1`` filters out every record
# (including CRITICAL), while still keeping the handler attached so
# runtime calls to ``set_host_log_level()`` can re-enable it.
_OFF_LEVEL = logging.CRITICAL + 1

#: Valid config strings and the ``logging`` levels they map to.
_LOG_LEVEL_NAMES: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
    "off": _OFF_LEVEL,
}

#: User-facing labels used by the Settings dialog combo box.  Order is
#: preserved — first entry is the default.
LOG_LEVEL_LABELS: list[str] = ["Debug", "Info", "Warning", "Error", "Critical", "Off"]

#: Map combo label → backing config string (all lowercase).
LOG_LEVEL_LABEL_TO_VALUE: dict[str, str] = {
    "Debug": "debug",
    "Info": "info",
    "Warning": "warning",
    "Error": "error",
    "Critical": "critical",
    "Off": "off",
}

#: Reverse map used by the Settings dialog to preselect the current value.
LOG_LEVEL_VALUE_TO_LABEL: dict[str, str] = {v: k for k, v in LOG_LEVEL_LABEL_TO_VALUE.items()}


def resolve_log_level(name: str) -> int:
    """Map a config string (``"warning"``, ``"off"``, …) to a ``logging`` level.

    Unknown / empty values fall back to ``logging.WARNING`` — the safe
    default that suppresses INFO/DEBUG host spam while still surfacing
    user-actionable warnings and errors in the Output window.
    """
    if not isinstance(name, str):
        return logging.WARNING
    return _LOG_LEVEL_NAMES.get(name.strip().lower(), logging.WARNING)


def _read_configured_host_level() -> int:
    """Read ``ida_output_log_level`` from the saved config without
    forcing an import cycle through ``core.logging`` → ``core.config``.

    ``core.config`` imports ``log_error`` from ``core.logging``, so we
    defer the import to here and fall back to ``WARNING`` on any failure.
    """
    try:
        from .config import RikuganConfig
    except Exception:
        return logging.WARNING
    try:
        cfg = RikuganConfig.load_or_create()
    except Exception:
        return logging.WARNING
    return resolve_log_level(getattr(cfg, "ida_output_log_level", "warning"))


def set_host_log_level(level_name: str) -> int:
    """Apply *level_name* to every ``HostOutputHandler`` already attached
    to the ``Rikugan`` logger.  Returns the resolved ``logging`` level.

    Safe to call before ``get_logger()`` has been invoked — the change is
    then applied lazily on the next ``get_logger()`` call.
    """
    level = resolve_log_level(level_name)
    try:
        logger = logging.getLogger("Rikugan")
        for h in logger.handlers:
            if isinstance(h, HostOutputHandler):
                h.setLevel(level)
    except Exception:
        pass
    return level


# ---------------------------------------------------------------------------
# Host sink registration
# ---------------------------------------------------------------------------

# Callable[[str, int], None] — receives (formatted_message, levelno)
_host_sink: Callable[[str, int], None] | None = None


def register_host_sink(sink: Callable[[str, int], None]) -> None:
    """Register a host-specific log sink (called from host entry points)."""
    global _host_sink
    _host_sink = sink


def _stderr_write(line: str) -> None:
    """Write to ``sys.stderr`` without crashing on cp1252 streams.

    Windows default ``sys.stderr`` uses cp1252 and rejects non-Latin1
    code points (e.g. ``→`` U+2192). Replace un-encodable chars so
    the log line is emitted instead of crashing the logging thread.
    """
    stream = sys.stderr
    try:
        encoding = getattr(stream, "encoding", None) or "ascii"
        stream.write(line.encode(encoding, errors="replace").decode(encoding))
    except Exception:
        # Last-resort: drop the message rather than crash.
        try:
            stream.write(line.encode("ascii", errors="replace").decode("ascii"))
        except Exception:
            pass


def _resolve_host_sink() -> Callable[[str, int], None] | None:
    """Auto-detect and register host sink on first use."""
    global _host_sink
    if _host_sink is not None:
        return _host_sink

    try:
        from .host import IDA_AVAILABLE
    except Exception:
        return None

    if IDA_AVAILABLE:
        try:
            import importlib

            ida_kernwin = importlib.import_module("ida_kernwin")

            def _ida_sink(msg: str, levelno: int) -> None:
                try:
                    ida_kernwin.msg(f"{msg}\n")
                except RuntimeError as e:
                    _stderr_write(f"[Lục nhãn] IDA output window unavailable: {e}\n")

            _host_sink = _ida_sink
            return _host_sink
        except ImportError as exc:
            _stderr_write(f"[Lục nhãn] ida_kernwin import failed: {exc}\n")

    return None


# ---------------------------------------------------------------------------
# Host output handler
# ---------------------------------------------------------------------------


class HostOutputHandler(logging.Handler):
    """Logging handler that delegates to the registered host sink."""

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        sink = _host_sink or _resolve_host_sink()
        if sink is not None:
            sink(msg, record.levelno)
        else:
            _stderr_write(f"{msg}\n")


# Keep old name as alias for backwards compatibility
IDAHandler = HostOutputHandler


# ---------------------------------------------------------------------------
# Crash-safe file handler
# ---------------------------------------------------------------------------


def _log_file_path() -> str:
    base = get_user_config_base_dir()
    d = os.path.join(base, "rikugan")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "rikugan_debug.log")


class _FlushFileHandler(logging.FileHandler):
    """FileHandler that flushes after every record for crash safety."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        stream = self.stream
        if stream is not None:
            try:
                stream.flush()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Structured JSON handler
# ---------------------------------------------------------------------------

#: Allowed scalar types for a structured attempt event.
JSONScalar = str | int | float | bool | None

#: Allowlist of keys permitted in a structured ``rikugan_event``.  Closed
#: set: callers MUST NOT introduce new keys without an explicit review of
#: the privacy/PII implications.  Every value MUST be a :data:`JSONScalar`
#: (no nested dicts, no lists, no bytes).  This is the single defense
#: against leaking reasoning content or other sensitive LLM-derived data
#: into the JSONL telemetry stream.
STRUCTURED_EVENT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "provider",
        "model",
        "dialect",
        "turn_number",
        "attempt_number",
        "transport_retry_index",
        "thinking_mode",
        "reasoning_effort",
        "configured_max_tokens",
        "effective_max_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "usage_provenance",
        "reasoning_chars",
        "visible_chars",
        "tool_start_count",
        "tool_end_count",
        "finish_reason",
        "disposition",
        "guard_trigger",
        "repetition_ratio_millis",
        "recovery_result",
        "discarded_attempt",
    }
)


def _sanitize_structured_event(event: object) -> dict[str, JSONScalar]:
    """Validate and sanitize a structured event dict for JSONL telemetry.

    - Rejects unknown keys (raises :class:`KeyError`).
    - Rejects non-:data:`JSONScalar` values (raises :class:`TypeError`).
    - Strips injection markers and lone surrogates from every string value.

    Returns a new dict (the input is left untouched).
    """
    if not isinstance(event, dict):
        raise TypeError("Structured event must be a dict")
    unknown = set(event) - STRUCTURED_EVENT_ALLOWLIST
    if unknown:
        raise KeyError(f"Unknown structured log keys: {sorted(unknown)}")
    for key, value in event.items():
        if not isinstance(value, (str, int, float, bool, type(None))):
            raise TypeError(f"Structured log value for {key!r} must be a JSON scalar, got {type(value).__name__}")

    # Local import to avoid a top-level cycle: sanitize.py imports from logging
    # transitively through other modules in the same package.
    from .sanitize import strip_injection_markers, strip_lone_surrogates

    sanitized: dict[str, JSONScalar] = {}
    for key, value in event.items():
        if isinstance(value, str):
            sanitized[key] = strip_lone_surrogates(strip_injection_markers(value))
        else:
            sanitized[key] = value
    return sanitized


class _JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON for machine parsing."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": record.created,
            "level": record.levelname,
            "thread": record.threadName,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = self.formatException(record.exc_info)
        # Structured attempt event (provider-neutral telemetry).  Attached
        # via ``extra={"rikugan_event": ...}`` on the logger call.  Skipped
        # silently when absent or when sanitization fails — telemetry MUST
        # NOT block the human-readable log path.
        rikugan_event = getattr(record, "rikugan_event", None)
        if rikugan_event is not None:
            try:
                entry["rikugan_event"] = _sanitize_structured_event(rikugan_event)
            except (KeyError, TypeError):
                pass
        return json.dumps(entry, default=str)
