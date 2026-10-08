"""User-facing CLI for IDA headless mode.

Runs OUTSIDE the IDA process.  Discovers the IDA executable, builds
a bootstrap JSON config, launches IDA with ``-A -S<bootstrap>``, and
reads/writes results.

Commands:

    lucnhan-headless ask <binary> <prompt> [flags]
    lucnhan-headless serve <binary> [flags]
    lucnhan-headless status --server URL --token TOKEN
    lucnhan-headless tools --server URL --token TOKEN
    lucnhan-headless events --server URL --token TOKEN [--follow] [--run-id ID]
    lucnhan-headless prompt <text> --server URL --token TOKEN
    lucnhan-headless answer <text> --server URL --token TOKEN --run-id ID
    lucnhan-headless tool-approval <decision> --server URL --token TOKEN --run-id ID
    lucnhan-headless approval <action> --server URL --token TOKEN --run-id ID
    lucnhan-headless cancel --server URL --token TOKEN --run-id ID
    lucnhan-headless shutdown --server URL --token TOKEN
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lucnhan.memory.workspace import IdentityRequest

# Auth token must be 64 lowercase/uppercase hex characters (32 bytes).
_TOKEN_HEX_PATTERN = re.compile(r"[0-9a-fA-F]{64}")

# ---------------------------------------------------------------------------
# IDA executable discovery
# ---------------------------------------------------------------------------

_COMMON_IDA_PATHS: dict[str, list[str]] = {
    "win32": [
        r"C:\Program Files\IDA Pro 9.2\idat.exe",
        r"C:\Program Files\IDA Pro 9\idat.exe",
        r"C:\Program Files\IDA Professional 9.0\idat.exe",
        r"E:\ida pro 9.2\idat.exe",
        r"E:\ida pro 9\idat.exe",
    ],
    "linux": [
        "/opt/ida-pro-9.0/idat",
        "/opt/idapro-9.0/idat",
        "/opt/ida-9.0/idat",
    ],
    "darwin": [
        "/Applications/IDA Professional 9.0.app/Contents/MacOS/idat",
        "/opt/ida-pro-9.0/idat",
    ],
}


def _find_ida(ida_path: str | None = None) -> str:
    """Locate the IDA executable.

    Resolution order:
    1. ``--ida`` CLI flag
    2. ``IDA_PATH`` environment variable
    3. Common install paths
    4. ``PATH`` scan
    """
    candidates: list[str] = []
    if ida_path:
        candidates.append(ida_path)
    env_path = os.environ.get("IDA_PATH", "")
    if env_path:
        candidates.append(env_path)

    if sys.platform == "win32":
        exe_name = "idat.exe"  # IDA 9.x text-mode binary (headless)
        legacy_name = "idat64.exe"  # pre-9.x 64-bit text-mode (legacy)
    elif sys.platform == "darwin":
        exe_name = "idat"
        legacy_name = "idat64"
    else:
        exe_name = "idat"
        legacy_name = "idat64"

    candidates.extend(_COMMON_IDA_PATHS.get(sys.platform, []))

    for c in candidates:
        if c and os.path.isfile(c):
            return c

    found = shutil.which(exe_name) or shutil.which("idat")
    if not found and legacy_name:
        found = shutil.which(legacy_name)
    if found:
        return found

    print("ERROR: Could not locate IDA executable.", file=sys.stderr)
    print("  Set --ida PATH, $IDA_PATH, or add IDA to your PATH.", file=sys.stderr)
    sys.exit(3)


# ---------------------------------------------------------------------------
# IDA launch helpers
# ---------------------------------------------------------------------------

_LUCNHAN_PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_PARENT = os.path.dirname(_LUCNHAN_PACKAGE_DIR)


def _generate_bootstrap_script() -> str:
    """Generate a temporary ``-S`` script that calls
    ``lucnhan.ida.headless_bootstrap.main()`` directly.

    This runs on the IDA main/bootstrap thread so the
    ``IdaHeadlessDispatcher`` pumps correctly.
    """
    fd, path = tempfile.mkstemp(suffix=".py", prefix="lucnhan_s_")
    content = (
        "import sys, os\n"
        f"sys.path.insert(0, {json.dumps(_REPO_PARENT)})\n"
        "from lucnhan.ida.headless_bootstrap import main\n"
        "main()\n"
    )
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _build_memory_source(binary: str) -> IdentityRequest | None:
    """Hash a raw binary before IDA launch and build an :class:`IdentityRequest`.

    Returns ``None`` if the binary does not exist or changes during hashing.

    For ``.i64``/``.idb`` inputs, returns an IDB-mode request with no
    SHA-256 (the IDB's own identity will be resolved inside the controller).

    For raw executables, returns a raw-mode request with the full SHA-256
    of the original file. This allows the headless controller to resolve
    the same workspace across runs without depending on the temporary
    IDB path that IDA creates.
    """
    from pathlib import Path

    from lucnhan.memory.identity import hash_raw_binary
    from lucnhan.memory.workspace import IdentityRequest

    p = Path(binary)
    if not p.is_file():
        return None

    resolved = str(p.resolve())
    if resolved.lower().endswith((".i64", ".idb")):
        return IdentityRequest(
            source_kind="idb",
            idb_path=resolved,
            display_name=p.name,
        )

    digest = hash_raw_binary(resolved)
    return IdentityRequest(
        source_kind="raw",
        idb_path=resolved,
        source_sha256=digest,
        display_name=p.name,
    )


def _build_ida_args(
    ida_exe: str,
    binary: str,
    s_path: str,
    *,
    extra_args: list[str] | None = None,
) -> tuple[list[str], str | None]:
    """Build the argument vector for IDA (``shell=False`` safe).

    Returns ``(args, temp_db)`` where *temp_db* is the path of the
    temporary database file (if the input was NOT a ``.i64``/``.idb``),
    or ``None`` if the original database is used in-place.
    """
    binary_lower = binary.lower()
    args = [ida_exe, "-A"]

    if binary_lower.endswith((".i64", ".idb")):
        # Open existing database in-place â€” no temp DB.
        args.append(f"-S{s_path}")
        if extra_args:
            args.extend(extra_args)
        args.append(binary)
        return args, None
    else:
        # Create a temp database so the original binary directory stays clean.
        fd, temp_db = tempfile.mkstemp(suffix=".i64", prefix="lucnhan_db_")
        os.close(fd)
        os.unlink(temp_db)  # IDA will create it
        args.append(f"-o{temp_db}")
        args.append(f"-S{s_path}")
        if extra_args:
            args.extend(extra_args)
        args.append(binary)
        return args, temp_db


def _build_ida_env(cfg_path: str) -> dict[str, str]:
    """Build environment dict for the IDA subprocess.

    Sets ``LUCNHAN_HEADLESS_BOOTSTRAP`` to point at the bootstrap JSON.
    Does **not** inject system site-packages â€” the generated ``-S`` script
    only needs the repo parent on ``sys.path``.
    """
    env = os.environ.copy()
    env["LUCNHAN_HEADLESS_BOOTSTRAP"] = cfg_path
    return env


# ---------------------------------------------------------------------------
# Launch: one-shot ("ask") mode
# ---------------------------------------------------------------------------


def _launch_ida_ask(
    ida_exe: str,
    binary: str,
    bootstrap_cfg: dict,
    timeout: float = 600.0,
) -> dict:
    """Launch IDA with ``-A -S<script>`` for ask (one-shot) mode.

    Uses ``subprocess.run()`` with ``shell=False`` argument vectors.
    Collects stdout from the bootstrap script.
    """
    fd, cfg_path = tempfile.mkstemp(suffix=".json", prefix="lucnhan_bootstrap_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(bootstrap_cfg, f)

    s_path = _generate_bootstrap_script()
    env = _build_ida_env(cfg_path)
    args, temp_db = _build_ida_args(ida_exe, binary, s_path)

    try:
        proc = subprocess.run(
            args,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            # shell=False by default with list args
        )
    except subprocess.TimeoutExpired:
        _cleanup_temp_files(cfg_path, s_path, temp_db)
        return {"exit_code": 3, "errors": ["IDA process timed out"]}
    except FileNotFoundError:
        _cleanup_temp_files(cfg_path, s_path, temp_db)
        return {"exit_code": 3, "errors": [f"IDA executable not found: {ida_exe}"]}
    except Exception:
        _cleanup_temp_files(cfg_path, s_path, temp_db)
        return {"exit_code": 3, "errors": [f"Failed to launch IDA: {sys.exc_info()[1]}"]}

    _cleanup_temp_files(cfg_path, s_path, temp_db)

    # Parse output â€” last JSON line on stdout is the result.
    stdout = proc.stdout or ""
    output_file = bootstrap_cfg.get("output_file")
    if output_file and os.path.isfile(output_file):
        try:
            with open(output_file, encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            pass

    result = _extract_last_json_line(stdout)
    if result is not None:
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            pass

    # Fallback
    return {
        "exit_code": max(proc.returncode, 3),
        "errors": [
            f"Could not parse output (rc={proc.returncode}). stderr: {proc.stderr[:1000] if proc.stderr else ''}"
        ],
    }


# ---------------------------------------------------------------------------
# Launch: serve mode
# ---------------------------------------------------------------------------


def _launch_ida_serve(
    ida_exe: str,
    binary: str,
    bootstrap_cfg: dict,
    ready_file: str | None = None,
    ready_timeout: float = 120.0,
) -> dict:
    """Launch IDA in serve mode.

    Generates a direct ``-S`` bootstrap script, launches IDA with
    ``shell=False`` and stdout/stderr redirected to ``DEVNULL`` (no
    abandoned pipes).  Polls the *ready_file* for the server URL and
    auth token.
    """
    fd, cfg_path = tempfile.mkstemp(suffix=".json", prefix="lucnhan_bootstrap_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(bootstrap_cfg, f)

    # Create an internal ready file if the user didn't provide one.
    internal_ready_file: str | None = None
    if not ready_file:
        rfd, internal_ready_file = tempfile.mkstemp(suffix=".json", prefix="lucnhan_ready_")
        os.close(rfd)
        # Re-open cfg to add the ready_file field.
        bootstrap_cfg["ready_file"] = internal_ready_file
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(bootstrap_cfg, f)
        ready_file = internal_ready_file

    s_path = _generate_bootstrap_script()
    env = _build_ida_env(cfg_path)
    args, temp_db = _build_ida_args(ida_exe, binary, s_path)

    # For serve, redirect stdout/stderr to DEVNULL â€” never leave pipes
    # attached to a long-lived process.
    kwargs: dict = dict(
        args=args,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # shell=False is the default with list args
    )

    try:
        proc = subprocess.Popen(**kwargs)
    except FileNotFoundError:
        _cleanup_temp_files(cfg_path, s_path, temp_db)
        if internal_ready_file:
            _safe_unlink(internal_ready_file)
        return {"error": True, "message": f"IDA executable not found: {ida_exe}"}

    try:
        # Poll ready-file for server info.
        deadline = time.time() + ready_timeout
        while time.time() < deadline:
            if os.path.isfile(ready_file):
                try:
                    with open(ready_file, encoding="utf-8") as rf:
                        parsed = json.load(rf)
                    if "url" in parsed and "token" in parsed:
                        parsed["pid"] = proc.pid
                        # Clean temp bootstrap artifacts now that we're ready.
                        # Do NOT delete temp_db â€” IDA is still using it.
                        _safe_unlink(cfg_path)
                        _safe_unlink(s_path)
                        if internal_ready_file:
                            _safe_unlink(internal_ready_file)
                        return parsed
                except (json.JSONDecodeError, OSError):
                    pass

            rc = proc.poll()
            if rc is not None:
                # IDA exited early â€” clean up and report.
                _cleanup_temp_files(cfg_path, s_path, None)
                if internal_ready_file:
                    _safe_unlink(internal_ready_file)
                if temp_db and os.path.isfile(temp_db):
                    # Temp DB exists but IDA exited â€” safe to clean.
                    _cleanup_temp_files(None, None, temp_db)
                return {"error": True, "message": f"IDA process exited early with code {rc}"}

            time.sleep(0.3)

        # Timeout
        proc.kill()
        proc.wait()
        _cleanup_temp_files(cfg_path, s_path, temp_db)
        if internal_ready_file:
            _safe_unlink(internal_ready_file)
        return {"error": True, "message": "Ready info not received within timeout"}

    except Exception as exc:
        proc.kill()
        proc.wait()
        _cleanup_temp_files(cfg_path, s_path, temp_db)
        if internal_ready_file:
            _safe_unlink(internal_ready_file)
        return {"error": True, "message": f"Exception while waiting for readiness: {exc}"}


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------


def _safe_unlink(path: str | None) -> None:
    """Remove a file, swallowing any OSError."""
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _cleanup_temp_files(cfg_path: str | None, s_path: str | None, temp_db: str | None = None) -> None:
    """Remove temp bootstrap files and optional temp database."""
    for path in (cfg_path, s_path):
        _safe_unlink(path)
    if temp_db:
        if os.path.isfile(temp_db):
            _safe_unlink(temp_db)
        # Also clean up any .i64 shadow files that IDA creates alongside the .i64.
        for suffix in (".id0", ".id1", ".id2", ".nam", ".til", ".i64"):
            alt_path = temp_db[:-4] + suffix if temp_db.endswith(".i64") else temp_db + suffix
            _safe_unlink(alt_path)


def _extract_last_json_line(text: str) -> str | None:
    """Look for the last line that looks like valid JSON in *text*."""
    lines = text.splitlines()
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            return stripped
    return None


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _decode_json_response(raw: bytes) -> dict:
    """Parse JSON bytes, ensuring the result is a dict.

    Returns ``{"error": True, "message": "..."}`` on non-dict JSON or
    parse failure so callers always receive a truthy error key.
    """
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return {"error": True, "message": f"Invalid JSON response: {e}"}
    if not isinstance(parsed, dict):
        return {"error": True, "message": "Non-object JSON response"}
    return parsed


def _http_post(url: str, token: str, data: dict | None = None) -> dict:
    """Send an HTTP POST request with Bearer token.

    Network errors, non-2xx responses, invalid JSON, and non-object JSON
    all produce a truthy ``{"error": True, ...}`` result.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return _decode_json_response(resp.read())
    except urllib.error.HTTPError as e:
        try:
            detail = _decode_json_response(e.read())
            return {"error": True, "status": e.code, "detail": detail}
        except Exception:
            return {"error": True, "status": e.code, "message": e.reason}
    except urllib.error.URLError as e:
        return {"error": True, "message": f"Network error: {e.reason}"}
    except Exception as e:
        return {"error": True, "message": str(e)}


def _http_get(url: str, token: str) -> dict:
    """Send an HTTP GET request with Bearer token.

    Network errors, non-2xx responses, invalid JSON, and non-object JSON
    all produce a truthy ``{"error": True, ...}`` result.
    """
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return _decode_json_response(resp.read())
    except urllib.error.HTTPError as e:
        try:
            detail = _decode_json_response(e.read())
            return {"error": True, "status": e.code, "detail": detail}
        except Exception:
            return {"error": True, "status": e.code, "message": e.reason}
    except urllib.error.URLError as e:
        return {"error": True, "message": f"Network error: {e.reason}"}
    except Exception as e:
        return {"error": True, "message": str(e)}


def _validate_token_format(token: str) -> bool:
    """Return True when ``token`` matches the required 64-char hex format."""
    return _TOKEN_HEX_PATTERN.fullmatch(token) is not None


def _reject_bad_token_format() -> None:
    """Emit the format-rejection message and exit.

    Separated from token validation so the rejection text is never adjacent
    to a secret-bearing variable in the call site.
    """
    print("ERROR: --token must be 64 hex characters.", file=sys.stderr)
    print('  Example: python -c "import secrets; print(secrets.token_hex(32))"', file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_ask(args: argparse.Namespace) -> None:
    """Launch IDA headless to run a single prompt."""
    ida_exe = _find_ida(args.ida)
    binary = os.path.abspath(args.binary)
    if not os.path.isfile(binary):
        print(f"ERROR: Binary not found: {binary}", file=sys.stderr)
        sys.exit(2)

    bootstrap_cfg: dict = {
        "mode": "ask",
        "prompt": args.prompt,
        "wait_for_auto_analysis": not args.no_auto_wait,
    }
    memory_source = _build_memory_source(binary)
    if memory_source is not None:
        bootstrap_cfg["memory_source"] = {
            "kind": memory_source.source_kind,
            "original_path": memory_source.idb_path,
            **({"sha256": memory_source.source_sha256} if memory_source.source_sha256 else {}),
        }
    if args.output:
        bootstrap_cfg["output_file"] = os.path.abspath(args.output)
    if args.json:
        bootstrap_cfg["json_output"] = True
    if args.provider:
        bootstrap_cfg["provider"] = args.provider
    if args.model:
        bootstrap_cfg["model"] = args.model
    if args.api_base:
        bootstrap_cfg["api_base"] = args.api_base

    result = _launch_ida_ask(ida_exe, binary, bootstrap_cfg, timeout=args.timeout)
    exit_code = result.get("exit_code", 1)

    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        if result.get("errors"):
            for e in result["errors"]:
                print(f"  Error: {e}", file=sys.stderr)
        print(result.get("final_text", ""))
        print(f"\n[Exit: {exit_code}  |  Elapsed: {result.get('elapsed', 0):.1f}s]")

    sys.exit(exit_code)


def cmd_serve(args: argparse.Namespace) -> None:
    """Launch IDA headless with a control server."""
    ida_exe = _find_ida(args.ida)
    binary = os.path.abspath(args.binary)
    if not os.path.isfile(binary):
        print(f"ERROR: Binary not found: {binary}", file=sys.stderr)
        sys.exit(2)

    host = args.host
    _LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
    if host not in _LOOPBACK_HOSTS:
        print(f"ERROR: Binding to {host!r} is not allowed.", file=sys.stderr)
        print("  The control server must be local-only.", file=sys.stderr)
        print("  Use --host 127.0.0.1 or --host localhost.", file=sys.stderr)
        sys.exit(2)

    # Validate token format: must be 64 hex characters if provided.
    if args.token and not _validate_token_format(args.token):
        _reject_bad_token_format()

    ready_file = os.path.abspath(args.ready_file) if args.ready_file else None
    bootstrap_cfg = {
        "mode": "serve",
        "server_host": host,
        "server_port": args.port,
        "wait_for_auto_analysis": not args.no_auto_wait,
    }
    memory_source = _build_memory_source(binary)
    if memory_source is not None:
        bootstrap_cfg["memory_source"] = {
            "kind": memory_source.source_kind,
            "original_path": memory_source.idb_path,
            **({"sha256": memory_source.source_sha256} if memory_source.source_sha256 else {}),
        }
    if ready_file:
        bootstrap_cfg["ready_file"] = ready_file
    if args.token:
        bootstrap_cfg["server_token"] = args.token
    if args.provider:
        bootstrap_cfg["provider"] = args.provider
    if args.model:
        bootstrap_cfg["model"] = args.model
    if args.api_base:
        bootstrap_cfg["api_base"] = args.api_base

    ready = _launch_ida_serve(ida_exe, binary, bootstrap_cfg, ready_file=ready_file, ready_timeout=args.ready_timeout)

    if "error" in ready:
        print(f"ERROR: {ready.get('message', 'Failed to start serve mode')}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(ready))


def _die_on_error(result: dict, label: str) -> None:
    """Exit with code 1 and print error result if result has an error key."""
    if "error" in result:
        print(f"ERROR ({label}): {json.dumps(result)}", file=sys.stderr)
        sys.exit(1)


def cmd_status(args: argparse.Namespace) -> None:
    url = f"{args.server}/health"
    result = _http_get(url, args.token)
    _die_on_error(result, "status")
    if not isinstance(result.get("status"), str):
        print(
            "ERROR: /health response missing string 'status' field",
            file=sys.stderr,
        )
        sys.exit(1)
    print(json.dumps(result))


def cmd_tools(args: argparse.Namespace) -> None:
    url = f"{args.server}/tools"
    result = _http_get(url, args.token)
    _die_on_error(result, "tools")
    if not isinstance(result.get("tools"), list):
        print(
            "ERROR: /tools response missing 'tools' list",
            file=sys.stderr,
        )
        sys.exit(1)
    print(json.dumps(result))


def cmd_events(args: argparse.Namespace) -> None:
    """Poll /events with envelope support and optional long-polling."""
    server = args.server.rstrip("/")
    index = 0
    try:
        while True:
            params: dict[str, str] = {"index": str(index)}
            if args.follow:
                params["wait"] = "1"
            if args.run_id:
                params["run_id"] = args.run_id
            query = "?" + urllib.parse.urlencode(params)
            url = f"{server}/events{query}"
            result = _http_get(url, args.token)

            _die_on_error(result, "events")

            # Validate event envelope: {"events": [...], "index": N, "finished": bool}
            if not isinstance(result.get("events"), list):
                print(
                    "ERROR: /events response missing 'events' list",
                    file=sys.stderr,
                )
                sys.exit(1)
            if not isinstance(result.get("finished"), bool):
                print(
                    "ERROR: /events response missing 'finished' boolean",
                    file=sys.stderr,
                )
                sys.exit(1)

            events = result["events"]
            for ev in events:
                print(json.dumps(ev))
            new_index = result.get("index", index + len(events))
            index = new_index

            finished = result["finished"]
            if finished:
                exit_code = result.get("exit_code")
                final_text = result.get("final_text")
                if exit_code is not None:
                    print(f"[Exit: {exit_code}]")
                if final_text:
                    print(final_text)
            if not args.follow or finished:
                break

            time.sleep(0.1)
    except KeyboardInterrupt:
        pass


def cmd_prompt_remote(args: argparse.Namespace) -> None:
    """Send a prompt to a running server via HTTP POST /prompt."""
    url = f"{args.server}/prompt"
    result = _http_post(url, args.token, {"prompt": args.prompt})
    _die_on_error(result, "prompt")
    if not isinstance(result.get("run_id"), str):
        print(
            "ERROR: /prompt response missing string 'run_id'",
            file=sys.stderr,
        )
        sys.exit(1)
    print(json.dumps(result))


def cmd_answer(args: argparse.Namespace) -> None:
    """Send an answer to a running server."""
    url = f"{args.server}/answer"
    result = _http_post(url, args.token, {"run_id": args.run_id, "answer": args.answer})
    _die_on_error(result, "answer")
    print(json.dumps(result))


def cmd_tool_approval_remote(args: argparse.Namespace) -> None:
    """Send a tool-call approval decision to a running server."""
    # Normalize CLI aliases to canonical server decisions.
    _canonical: dict[str, str] = {
        "approve": "allow",
        "allow": "allow",
        "allow_all": "allow_all",
        "deny": "deny",
    }
    decision = _canonical.get(args.decision, args.decision)
    url = f"{args.server}/tool-approval"
    result = _http_post(
        url,
        args.token,
        {"run_id": args.run_id, "decision": decision},
    )
    _die_on_error(result, "tool-approval")
    print(json.dumps(result))


def cmd_approval_remote(args: argparse.Namespace) -> None:
    """Send a plan/exploration approval decision to a running server."""
    url = f"{args.server}/approval"
    result = _http_post(
        url,
        args.token,
        {"run_id": args.run_id, "decision": args.decision},
    )
    _die_on_error(result, "approval")
    print(json.dumps(result))


def cmd_cancel(args: argparse.Namespace) -> None:
    """Cancel a running agent via HTTP POST /cancel."""
    url = f"{args.server}/cancel"
    result = _http_post(url, args.token, {"run_id": args.run_id})
    _die_on_error(result, "cancel")
    print(json.dumps(result))


def cmd_shutdown(args: argparse.Namespace) -> None:
    """Shut down the headless server via HTTP POST /shutdown."""
    url = f"{args.server}/shutdown"
    result = _http_post(url, args.token)
    _die_on_error(result, "shutdown")
    print(json.dumps(result))


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Luc Nhan headless CLI — run Luc Nhan inside IDA without the GUI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ask
    p_ask = sub.add_parser("ask", help="Run a single prompt")
    p_ask.add_argument("binary", help="Binary or .i64/.idb file to load")
    p_ask.add_argument("prompt", help="The prompt to execute")
    p_ask.add_argument("--json", action="store_true", help="Output full JSON result")
    p_ask.add_argument("--output", "-o", help="Write detailed result to file")
    p_ask.add_argument("--ida", help="Path to IDA executable")
    p_ask.add_argument("--no-auto-wait", action="store_true", help="Skip auto-analysis wait")
    p_ask.add_argument("--timeout", type=int, default=600, help="Timeout in seconds (default: 600)")
    p_ask.add_argument("--provider", help="Override the LLM provider (e.g. openai, anthropic, ollama)")
    p_ask.add_argument("--model", help="Override the LLM model name")
    p_ask.add_argument("--api-base", help="Override the API base URL for custom/Ollama endpoints")
    p_ask.set_defaults(func=cmd_ask)

    # serve
    p_serve = sub.add_parser("serve", help="Start a headless control server")
    p_serve.add_argument("binary", help="Binary or .i64/.idb file to load")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=0, help="Port (0=auto)")
    p_serve.add_argument("--token", help="Bearer auth token (64-char hex)")
    p_serve.add_argument("--ida", help="Path to IDA executable")
    p_serve.add_argument("--no-auto-wait", action="store_true", help="Skip auto-analysis wait")
    p_serve.add_argument("--ready-file", help="Write server URL/token to this file")
    p_serve.add_argument(
        "--ready-timeout", type=int, default=120, help="Seconds to wait for server readiness (default: 120)"
    )
    p_serve.add_argument("--provider", help="Override the LLM provider (e.g. openai, anthropic, ollama)")
    p_serve.add_argument("--model", help="Override the LLM model name")
    p_serve.add_argument("--api-base", help="Override the API base URL for custom/Ollama endpoints")
    p_serve.set_defaults(func=cmd_serve)

    # status
    p_status = sub.add_parser("status", help="Check server health")
    p_status.add_argument("--server", default="http://127.0.0.1:14913", help="Server base URL")
    p_status.add_argument("--token", help="Bearer auth token")
    p_status.set_defaults(func=cmd_status)

    # tools
    p_tools = sub.add_parser("tools", help="List available tools")
    p_tools.add_argument("--server", default="http://127.0.0.1:14913", help="Server base URL")
    p_tools.add_argument("--token", help="Bearer auth token")
    p_tools.set_defaults(func=cmd_tools)

    # events
    p_events = sub.add_parser("events", help="Poll agent events")
    p_events.add_argument("--server", default="http://127.0.0.1:14913", help="Server base URL")
    p_events.add_argument("--token", help="Bearer auth token")
    p_events.add_argument("--follow", "-f", action="store_true", help="Long-poll until finished")
    p_events.add_argument("--run-id", help="The run_id to poll events for")
    p_events.set_defaults(func=cmd_events)

    # prompt (remote)
    p_prompt = sub.add_parser("prompt", help="Send a prompt to a running server")
    p_prompt.add_argument("--server", default="http://127.0.0.1:14913", help="Server base URL")
    p_prompt.add_argument("--token", help="Bearer auth token")
    p_prompt.add_argument("--prompt", required=True, help="The prompt to send")
    p_prompt.set_defaults(func=cmd_prompt_remote)

    # answer
    p_answer = sub.add_parser("answer", help="Answer an agent question")
    p_answer.add_argument("--server", default="http://127.0.0.1:14913", help="Server base URL")
    p_answer.add_argument("--token", help="Bearer auth token")
    p_answer.add_argument("--answer", required=True, help="Your answer")
    p_answer.add_argument("--run-id", required=True, help="The run_id to answer for")
    p_answer.set_defaults(func=cmd_answer)

    # tool-approval
    p_ta = sub.add_parser("tool-approval", help="Approve or deny a tool call")
    p_ta.add_argument("--server", default="http://127.0.0.1:14913", help="Server base URL")
    p_ta.add_argument("--token", help="Bearer auth token")
    p_ta.add_argument("--run-id", required=True, help="The run_id to approve for")
    p_ta.add_argument(
        "decision",
        choices=["allow", "allow_all", "deny", "approve"],
        help="Decision: allow, allow_all, deny, or approve (alias for allow)",
    )
    p_ta.set_defaults(func=cmd_tool_approval_remote)

    # approval
    p_ap = sub.add_parser("approval", help="Approve or deny a plan/exploration step")
    p_ap.add_argument("--server", default="http://127.0.0.1:14913", help="Server base URL")
    p_ap.add_argument("--token", help="Bearer auth token")
    p_ap.add_argument("--run-id", required=True, help="The run_id to approve for")
    p_ap.add_argument(
        "decision",
        choices=["approve", "deny"],
        help="Decision: approve or deny",
    )
    p_ap.set_defaults(func=cmd_approval_remote)

    # cancel
    p_cancel = sub.add_parser("cancel", help="Cancel an active run")
    p_cancel.add_argument("--server", default="http://127.0.0.1:14913", help="Server base URL")
    p_cancel.add_argument("--token", help="Bearer auth token")
    p_cancel.add_argument("--run-id", required=True, help="The run_id to cancel")
    p_cancel.set_defaults(func=cmd_cancel)

    # shutdown
    p_shutdown = sub.add_parser("shutdown", help="Shut down the server")
    p_shutdown.add_argument("--server", default="http://127.0.0.1:14913", help="Server base URL")
    p_shutdown.add_argument("--token", help="Bearer auth token")
    p_shutdown.set_defaults(func=cmd_shutdown)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
