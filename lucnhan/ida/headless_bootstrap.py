"""Headless bootstrap for IDA -S scripts.

This module is invoked from IDA via a temporary ``-S`` script that
adds the repo root to ``sys.path`` and calls ``main()``.

It reads configuration from a JSON file referenced by the environment
variable ``LUCNHAN_HEADLESS_BOOTSTRAP`` to avoid fragile quoting of
arguments through IDA's ``-S`` flag.

Example bootstrap JSON:

.. code-block:: json

    {
        "mode": "ask",
        "prompt": "Summarize this binary's capabilities.",
        "output_file": "result.json",
        "server_port": 0,
        "ready_file": "lucnhan-ready.json",
        "wait_for_auto_analysis": true
    }

Modes:
  - ``"ask"`` — one-shot prompt
  - ``"serve"`` — start control server
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import traceback
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lucnhan.memory.workspace import IdentityRequest


def _log_best_effort(context: str, exc: BaseException) -> None:
    """Emit a short note for swallowed best-effort exceptions.

    Bootstrap runs before structured logging is configured and may run
    during process exit where ``sys.stderr`` is the only usable channel.
    Keeping the message on stderr avoids silent failure without pulling
    in ``lucnhan.core.logging`` (which is not safe to import at every
    point this is called).
    """
    try:
        sys.stderr.write(f"[lucnhan:bootstrap] {context}: {exc}\n")
        sys.stderr.flush()
    except Exception:
        # stderr itself is closed/unwritable — nothing more we can do.
        pass


def _clean_exit_ida(exit_code: int, message: str = "") -> None:
    """Write structured output and attempt a clean IDA exit.

    On success, writes a JSON line to stdout and calls ``idc.qexit(code)``.
    If ``qexit`` returns without terminating (which can happen),
    raises ``SystemExit``.  If ``idc`` is unavailable or raises,
    falls back to ``os._exit()`` after flushing stdout.
    """
    if message:
        result = {
            "error": True,
            "exit_code": exit_code,
            "message": message,
        }
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
        sys.stdout.write("\n")
        sys.stdout.flush()

    # Try IDA's qexit first.  It may return without exiting.
    try:
        idc = importlib.import_module("idc")
        idc.qexit(exit_code)
        # qexit returned — force the issue.
        raise SystemExit(exit_code)
    except SystemExit:
        raise
    except Exception as exc:
        # idc unavailable or qexit failed — fall through to os._exit.
        _log_best_effort("idc.qexit fallback", exc)

    # Final fallback: flush and hard-exit.
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception as exc:
        _log_best_effort("stream flush before hard exit", exc)
    os._exit(exit_code)


def _load_bootstrap_config() -> dict | None:
    """Load the bootstrap JSON from LUCNHAN_HEADLESS_BOOTSTRAP env var."""
    path = os.environ.get("LUCNHAN_HEADLESS_BOOTSTRAP", "")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        _clean_exit_ida(3, f"Bootstrap config file not found: {path}")
    except json.JSONDecodeError as e:
        _clean_exit_ida(3, f"Invalid bootstrap config JSON: {e}")
    return None


def validate_bootstrap_memory_source(value: object) -> IdentityRequest | None:
    """Validate the ``memory_source`` bootstrap key into a typed :class:`IdentityRequest`.

    Returns ``None`` when the value is not a dict, the kind is unknown,
    or a raw source lacks a valid 64-hex SHA-256.

    This is a pure validation helper — it never touches the filesystem
    and can be unit-tested without IDA.
    """
    import re
    from pathlib import Path

    from lucnhan.memory.workspace import IdentityRequest

    if not isinstance(value, dict):
        return None

    kind = value.get("kind")
    original_path = value.get("original_path")
    if kind not in {"idb", "raw"} or not isinstance(original_path, str) or not original_path:
        return None

    digest = ""
    if kind == "raw":
        raw_digest = value.get("sha256")
        if not isinstance(raw_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", raw_digest):
            return None
        digest = raw_digest

    return IdentityRequest(
        source_kind=kind,
        idb_path=original_path,
        source_sha256=digest,
        display_name=Path(original_path).name,
    )


def _apply_provider_overrides(config, bootstrap_cfg: dict) -> None:
    """Apply provider/model/api_base overrides from the bootstrap config.

    This runs after LucNhanConfig.load() so saved GUI settings are the
    baseline.  Overrides are applied in-memory only — nothing is persisted.

    Provider name is validated against built-in names and custom providers.
    An empty model string is replaced with the provider's built-in default.
    Exit codes:
        2 — unknown provider
    """
    provider_override: str | None = bootstrap_cfg.get("provider")
    model_override: str | None = bootstrap_cfg.get("model")
    api_base_override: str | None = bootstrap_cfg.get("api_base")

    if provider_override and provider_override != config.provider.name:
        config.switch_provider(provider_override)

    error = config.validate_active_provider()
    if error:
        _clean_exit_ida(2, error)

    if model_override:
        config.provider.model = model_override

    if api_base_override:
        config.provider.api_base = api_base_override

    # If the model is still empty (fresh provider or user didn't specify),
    # fall back to the provider's built-in default model.
    if not config.provider.model:
        default_model = config.get_provider_default_model(config.provider.name)
        if default_model:
            config.provider.model = default_model


def _decrypt_headless_config_keys(config) -> None:
    """Decrypt encrypted API keys in headless mode when a password is supplied.

    The password is read from the environment instead of command-line arguments
    so it does not appear in the IDA process argv or bootstrap JSON file.
    """
    if not config.has_encrypted_keys():
        return

    password = os.environ.get("LUCNHAN_CONFIG_PASSWORD", "")
    if not password:
        return

    if not config.decrypt_stored_keys(password):
        _clean_exit_ida(4, "Failed to decrypt Luc Nhan API keys: wrong config password.")


def _run_one_shot(controller, dispatcher, config: dict) -> None:
    """Run a single prompt, write output, and exit IDA cleanly."""
    from lucnhan.headless.runner import run_prompt

    prompt = config.get("prompt", "")
    if not prompt:
        try:
            controller.shutdown()
        except Exception as exc:
            _log_best_effort("controller.shutdown before missing-prompt exit", exc)
        _clean_exit_ida(2, "One-shot mode requires a 'prompt' field.")

    output_file = config.get("output_file", "")
    json_output = config.get("json_output", False)

    result_holder: list = []
    error_holder: list = []  # P1-3: capture worker thread exceptions.

    def _do_prompt() -> None:
        try:
            result_holder.append(run_prompt(controller, prompt, json_events=bool(output_file or json_output)))
        except Exception as exc:
            import traceback

            error_holder.append(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")

    t = threading.Thread(target=_do_prompt, daemon=True)
    t.start()

    # Main thread pumps dispatcher until the prompt thread finishes.
    while t.is_alive():
        dispatcher.pump_once(timeout=0.1)

    # P1-3: report worker thread exceptions.
    if error_holder:
        try:
            controller.shutdown()
        except Exception as exc:
            _log_best_effort("controller.shutdown before error exit", exc)
        _clean_exit_ida(1, error_holder[0])

    result = result_holder[0] if result_holder else None
    if result is None:
        try:
            controller.shutdown()
        except Exception as exc:
            _log_best_effort("controller.shutdown before no-result exit", exc)
        _clean_exit_ida(1, "Prompt failed — no result returned.")

    stdout_payload: dict = {
        "exit_code": result.exit_code,
        "run_id": result.run_id,
        "session_id": result.session_id,
        "elapsed": result.elapsed,
        "turn_count": result.turn_count,
        "final_text": result.final_text,
        "errors": result.errors,
    }
    if not output_file:
        stdout_payload["events"] = result.events

    json_out = json.dumps(stdout_payload, ensure_ascii=False)

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(json_out)
            f.write("\n")
        sys.stdout.write(
            json.dumps(
                {
                    "exit_code": result.exit_code,
                    "run_id": result.run_id,
                    "output_file": output_file,
                }
            )
        )
        sys.stdout.write("\n")
    else:
        sys.stdout.write(json_out)
        sys.stdout.write("\n")
    sys.stdout.flush()

    # Clean shutdown before exiting IDA.
    try:
        controller.shutdown()
    except Exception as exc:
        _log_best_effort("controller.shutdown after one-shot", exc)

    _clean_exit_ida(result.exit_code or 0)


def _run_server(controller, dispatcher, config: dict) -> None:
    """Start the control server and pump dispatcher on the main thread."""
    from lucnhan.control.server import ControlServer

    host = config.get("server_host", "127.0.0.1")
    port = int(config.get("server_port", 0))
    token = config.get("server_token", None)
    ready_file = config.get("ready_file", "")

    server = ControlServer(controller, host=host, port=port, token=token)
    server.start()

    # Print ready info to stdout for the CLI launcher.
    ready = {
        "mode": "serve",
        "url": server.url,
        "port": server.port,
        "token": server.token,
    }
    sys.stdout.write(json.dumps(ready, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()

    if ready_file:
        server.write_ready_file(ready_file)

    # Main-thread pump loop — blocks here dispatching IDA API calls
    # until the server's shutdown callback signals that /shutdown was
    # received and the HTTP server has been instructed to stop.
    try:
        while True:
            dispatcher.pump_until(timeout=1.0)
            if dispatcher.is_shutdown_requested():
                break
            # Also observe the server's own shutdown signal (set by
            # /shutdown handler after stopping the HTTP server).
            if server.shutdown_callback is not None and server.shutdown_callback.signalled.is_set():
                dispatcher.request_shutdown()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        try:
            controller.shutdown()
        except Exception as exc:
            _log_best_effort("controller.shutdown in server finally", exc)

    _clean_exit_ida(0)


def main() -> None:
    """Entry point invoked by IDA's -S script mechanism."""
    # Must be set immediately
    os.environ["LUCNHAN_HEADLESS"] = "1"

    config = _load_bootstrap_config()
    if config is None:
        # Legacy fallback: read mode/prompt from env
        mode = os.environ.get("LUCNHAN_HEADLESS_MODE", "")
        prompt = os.environ.get("LUCNHAN_HEADLESS_PROMPT", "")
        if not mode:
            _clean_exit_ida(
                2,
                "No bootstrap config found. Set LUCNHAN_HEADLESS_BOOTSTRAP env var or use lucnhan-headless CLI.",
            )
        config = {"mode": mode, "prompt": prompt}

    mode = config.get("mode", "ask")

    # Deferred imports — only import Luc Nhan internals after headless
    # env is set (avoids premature idaapi/Qt imports).
    from lucnhan.core.config import LucNhanConfig
    from lucnhan.core.logging import log_error, log_info
    from lucnhan.ida.dispatch import IdaHeadlessDispatcher
    from lucnhan.ida.headless_controller import HeadlessSessionController

    wait_auto = config.get("wait_for_auto_analysis", True)

    controller = None
    try:
        try:
            rk_config = LucNhanConfig()
            rk_config.load()
            _decrypt_headless_config_keys(rk_config)
        except Exception as e:
            _clean_exit_ida(4, f"Failed to load Luc Nhan config: {e}")

        # Apply provider/model/api_base overrides from bootstrap config.
        _apply_provider_overrides(rk_config, config)

        memory_source = validate_bootstrap_memory_source(config.get("memory_source"))

        dispatcher = IdaHeadlessDispatcher()
        controller = HeadlessSessionController(
            rk_config,
            dispatcher,
            wait_for_auto_analysis=wait_auto,
            memory_source=memory_source,
        )

        controller.wait_auto_analysis()

        if mode == "ask":
            _run_one_shot(controller, dispatcher, config)
        elif mode == "serve":
            _run_server(controller, dispatcher, config)
        else:
            _clean_exit_ida(2, f"Unknown mode: {mode}.  Use 'ask' or 'serve'.")
    except Exception as e:
        log_error(f"Headless bootstrap failed: {e}")
        traceback.print_exc(file=sys.stderr)
        if controller is not None:
            try:
                controller.shutdown()
            except Exception as exc:
                _log_best_effort("controller.shutdown before bootstrap-failure exit", exc)
        _clean_exit_ida(1, str(e))


# Allow both direct invocation and import.
if __name__ == "__main__":
    main()
