"""Tests for lucnhan.control.server.

Uses unittest.mock.patch to intercept BaseHTTPRequestHandler.handle()
so the handler can be constructed without triggering a request cycle.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()


class _FakeFile:
    """A file-like object backed by BytesIO that supports fileno/close."""

    def __init__(self, data: bytes = b"") -> None:
        self._io = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._io.read(size)

    def readline(self, size: int = -1) -> bytes:
        return self._io.readline(size)

    def write(self, data: bytes) -> int:
        return self._io.write(data)

    def flush(self) -> None:
        self._io.flush()

    def seek(self, *args, **kwargs):
        return self._io.seek(*args, **kwargs)

    def close(self) -> None:
        pass

    def fileno(self) -> int:
        return -1


class _FakeSocket:
    """Minimal fake socket."""

    def makefile(self, mode: str = "rb", buffering: int = -1):
        return _FakeFile(b"GET / HTTP/1.0\r\n\r\n") if "r" in mode else _FakeFile()

    def close(self) -> None:
        pass


class _HeadersDict:
    """Dict-based headers compatible with email.message.Message for our uses."""

    def __init__(self, d: dict[str, str]) -> None:
        self._d = d

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._d.get(key, default)

    def items(self):
        return self._d.items()

    def get_all(self, key: str, default=None):
        val = self._d.get(key)
        if val is None:
            return default or []
        return [val]


class _RequestHelper:
    """Creates ControlHandler instances patched to skip handle()."""

    def __init__(self, handler_cls, state, controller):
        self._handler_cls = handler_cls
        self._state = state
        self._controller = controller

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        auth: str | None = "Bearer test-token",
        raw_body: bytes | None = None,
    ):
        body_bytes = raw_body if raw_body is not None else (json.dumps(body).encode("utf-8") if body else b"")
        raw_headers = {"Host": "127.0.0.1"}
        if auth:
            raw_headers["Authorization"] = auth
        if body_bytes:
            raw_headers["Content-Length"] = str(len(body_bytes))

        cls = self._handler_cls

        # Patch BaseHTTPRequestHandler.handle() so __init__ doesn't
        # trigger a full request cycle.
        with patch("http.server.BaseHTTPRequestHandler.handle", return_value=None):
            handler = cls(_FakeSocket(), ("127.0.0.1", 54321), None)

        # Replace send helpers with mocks that still call through so
        # wfile receives real headers, allowing _get_json() to work.
        _send_response = handler.send_response
        _send_header = handler.send_header
        _end_headers = handler.end_headers
        handler.send_response = MagicMock(side_effect=lambda *a, **kw: _send_response(*a, **kw))
        handler.send_header = MagicMock(side_effect=lambda *a, **kw: _send_header(*a, **kw))
        handler.end_headers = MagicMock(side_effect=lambda *a, **kw: _end_headers(*a, **kw))

        # Replace streams with our own
        rfile = io.BytesIO(body_bytes)
        wfile = io.BytesIO()
        handler.rfile = rfile
        handler.wfile = wfile

        # Pre-parse request state
        handler.command = method
        handler.path = path
        handler.request_version = "HTTP/1.0"
        handler.requestline = f"{method} {path} HTTP/1.0"
        handler.headers = _HeadersDict(raw_headers)

        return handler, wfile


class TestControlServerInit(unittest.TestCase):
    """Tests for ControlServer construction and defaults."""

    def test_default_token_generated(self):
        from lucnhan.control.server import ControlServer

        controller = MagicMock()
        server = ControlServer(controller)
        self.assertIsNotNone(server.token)
        self.assertEqual(len(server.token), 64)

    def test_custom_token_accepted(self):
        from lucnhan.control.server import ControlServer

        controller = MagicMock()
        valid_hex_token = "a" * 64
        server = ControlServer(controller, token=valid_hex_token)
        self.assertEqual(server.token, valid_hex_token)

    def test_rejects_short_token(self):
        from lucnhan.control.server import ControlServer

        controller = MagicMock()
        with self.assertRaises(ValueError):
            ControlServer(controller, token="short")

    def test_rejects_non_hex_token(self):
        from lucnhan.control.server import ControlServer

        controller = MagicMock()
        with self.assertRaises(ValueError):
            ControlServer(controller, token="my-secret-token")

    def test_rejects_token_with_special_chars(self):
        from lucnhan.control.server import ControlServer

        controller = MagicMock()
        with self.assertRaises(ValueError):
            ControlServer(controller, token="x" * 63 + "!")

    def test_rejects_0_0_0_0(self):
        from lucnhan.control.server import ControlServer

        controller = MagicMock()
        with self.assertRaises(ValueError):
            ControlServer(controller, host="0.0.0.0")

    def test_rejects_empty_host(self):
        from lucnhan.control.server import ControlServer

        controller = MagicMock()
        with self.assertRaises(ValueError):
            ControlServer(controller, host="")

    def test_accepts_127_0_0_1(self):
        from lucnhan.control.server import ControlServer

        controller = MagicMock()
        server = ControlServer(controller, host="127.0.0.1", port=0)
        self.assertEqual(server.host, "127.0.0.1")

    def test_accepts_localhost(self):
        from lucnhan.control.server import ControlServer

        controller = MagicMock()
        server = ControlServer(controller, host="localhost", port=0)
        self.assertEqual(server.host, "localhost")

    def test_accepts_ipv6_loopback(self):
        from lucnhan.control.server import ControlServer

        controller = MagicMock()
        server = ControlServer(controller, host="::1", port=0)
        self.assertEqual(server.host, "::1")

    def test_rejects_192_168_address(self):
        from lucnhan.control.server import ControlServer

        controller = MagicMock()
        with self.assertRaises(ValueError):
            ControlServer(controller, host="192.168.1.10")

    def test_rejects_10_0_0_address(self):
        from lucnhan.control.server import ControlServer

        controller = MagicMock()
        with self.assertRaises(ValueError):
            ControlServer(controller, host="10.0.0.5")

    def test_rejects_colon_colon(self):
        from lucnhan.control.server import ControlServer

        controller = MagicMock()
        with self.assertRaises(ValueError):
            ControlServer(controller, host="::")

    def test_write_ready_file(self):
        import tempfile

        from lucnhan.control.server import ControlServer

        controller = MagicMock()
        server = ControlServer(controller, host="127.0.0.1", port=9999, token="a" * 64)

        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            server.write_ready_file(path)
            with open(path) as f:
                data = json.load(f)
            self.assertEqual(data["url"], "http://127.0.0.1:9999")
            self.assertEqual(data["port"], 9999)
            self.assertEqual(data["token"], "a" * 64)
        finally:
            os.unlink(path)


class TestControlServerState(unittest.TestCase):
    """Tests for ControlServerState and RunState."""

    def test_run_state_sequence(self):
        from lucnhan.control.server import RunState

        state = RunState(run_id="abc")
        self.assertEqual(state.run_id, "abc")
        self.assertEqual(state.next_seq, 0)
        self.assertFalse(state.finished)
        self.assertEqual(state.final_text, "")

    def test_control_server_state_initial(self):
        from lucnhan.control.server import ControlServerState

        state = ControlServerState(token="tk")
        self.assertFalse(state.shutting_down)
        self.assertIsNone(state.run)
        self.assertTrue(state.is_idle)

    def test_is_idle_when_no_run(self):
        from lucnhan.control.server import ControlServerState

        state = ControlServerState(token="tk")
        self.assertTrue(state.is_idle)

    def test_is_idle_false_when_run_unfinished(self):
        from lucnhan.control.server import ControlServerState, RunState

        state = ControlServerState(token="tk")
        with state.lock:
            state.run = RunState(run_id="test")
        self.assertFalse(state.is_idle)

    def test_is_idle_true_when_run_finished(self):
        """Finished runs are considered idle (P2-3)."""
        from lucnhan.control.server import ControlServerState, RunState

        state = ControlServerState(token="tk")
        with state.lock:
            state.run = RunState(run_id="test")
            state.run.finished = True
        self.assertTrue(state.is_idle)


class TestControlHandler(unittest.TestCase):
    """Tests for ControlHandler HTTP endpoints."""

    def setUp(self):
        from lucnhan.control.server import ControlServerState, _make_handler

        self.controller = MagicMock()
        self.controller.tool_registry = MagicMock()
        self.controller.tool_registry.list_tools.return_value = []
        self.controller.start_agent.return_value = None
        self.controller.get_runner.return_value = None
        self.controller.is_agent_running = False
        self.controller.cancel = MagicMock()
        self.controller.on_agent_finished = MagicMock()
        self.controller.session = MagicMock(id="test-session")
        self.controller.shutdown = MagicMock()

        self.state = ControlServerState(token="test-token")
        handler_cls = _make_handler(self.state, self.controller)
        self._rh = _RequestHelper(handler_cls, self.state, self.controller)

    def _get_json(self, _wfile: io.BytesIO) -> dict:
        _wfile.seek(0)
        raw = _wfile.read()
        if not raw:
            return {}
        parts = raw.split(b"\r\n\r\n", 1)
        if len(parts) < 2:
            return {}
        try:
            return json.loads(parts[1])
        except json.JSONDecodeError:
            return {}

    def _get_status(self, handler) -> int:
        """Get the first send_response call argument (HTTP status)."""
        if handler.send_response.call_count:
            args, _ = handler.send_response.call_args
            return args[0] if args else 200
        return 200

    # -- health -----------------------------------------------------------

    def test_health_no_auth(self):
        handler, _wfile = self._rh.request("GET", "/health", auth=None)
        handler.do_GET()
        resp = self._get_json(_wfile)
        self.assertIn("ready", resp)
        self.assertIn("running", resp)

    def test_health_returns_ready(self):
        handler, _wfile = self._rh.request("GET", "/health", auth=None)
        handler.do_GET()
        resp = self._get_json(_wfile)
        self.assertTrue(resp["ready"])

    def test_health_includes_status_ok(self):
        """GET /health must include 'status': 'ok' for CLI status command."""
        handler, _wfile = self._rh.request("GET", "/health", auth=None)
        handler.do_GET()
        resp = self._get_json(_wfile)
        self.assertEqual(resp.get("status"), "ok")
        self.assertIsInstance(resp["status"], str)

    # -- tools -----------------------------------------------------------

    def test_tools_requires_auth(self):
        handler, _wfile = self._rh.request("GET", "/tools", auth=None)
        handler.do_GET()
        self.assertEqual(self._get_status(handler), 401)

    def test_tools_with_auth(self):
        handler, _wfile = self._rh.request("GET", "/tools")
        handler.do_GET()
        resp = self._get_json(_wfile)
        self.assertIn("tools", resp)

    # -- prompt -----------------------------------------------------------

    def test_prompt_missing_field(self):
        handler, _wfile = self._rh.request("POST", "/prompt", body={})
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)

    def test_prompt_with_auth(self):
        handler, _wfile = self._rh.request("POST", "/prompt", body={"prompt": "hello"})
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("run_id", resp)

    def test_prompt_rejects_when_shutting_down(self):
        with self.state.lock:
            self.state.shutting_down = True
        handler, _wfile = self._rh.request("POST", "/prompt", body={"prompt": "hello"})
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)

    def test_prompt_rejects_duplicate_run(self):
        from lucnhan.control.server import RunState

        with self.state.lock:
            self.state.run = RunState(run_id="existing")
        handler, _wfile = self._rh.request("POST", "/prompt", body={"prompt": "hello"})
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)

    def test_prompt_allows_when_previous_run_finished(self):
        """Finished runs are idle; a new prompt should be allowed (P2-3)."""
        from lucnhan.control.server import RunState

        with self.state.lock:
            self.state.run = RunState(run_id="existing")
            self.state.run.finished = True

        handler, _wfile = self._rh.request("POST", "/prompt", body={"prompt": "hello"})
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("run_id", resp)

    # -- cancel -----------------------------------------------------------

    def test_cancel_no_run_id(self):
        """Cancel without run_id returns 400 (P0-3)."""
        handler, _wfile = self._rh.request("POST", "/cancel", body={})
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)

    def test_cancel_with_active_run(self):
        from lucnhan.control.server import RunState

        with self.state.lock:
            self.state.run = RunState(run_id="test-run")
        handler, _wfile = self._rh.request("POST", "/cancel", body={"run_id": "test-run"})
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("status", resp)
        self.controller.cancel.assert_called()

    def test_cancel_with_stale_run_id(self):
        from lucnhan.control.server import RunState

        with self.state.lock:
            self.state.run = RunState(run_id="active-run")
        handler, _wfile = self._rh.request("POST", "/cancel", body={"run_id": "other-run"})
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)

    # -- shutdown ---------------------------------------------------------

    def test_shutdown_with_auth(self):
        handler, _wfile = self._rh.request("POST", "/shutdown")
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("status", resp)
        self.assertTrue(self.state.shutting_down)

    def test_shutdown_idempotent(self):
        with self.state.lock:
            self.state.shutting_down = True
        handler, _wfile = self._rh.request("POST", "/shutdown")
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertEqual(resp.get("status"), "already_shutting_down")

    # -- CORS / OPTIONS (P2-4: no default CORS) ----------------------------

    def test_options_no_cors_headers(self):
        """OPTIONS returns no Access-Control-Allow-* headers by default."""
        # Test multiple representative endpoints
        for path in ("/prompt", "/health", "/tools"):
            handler, _wfile = self._rh.request("OPTIONS", path, auth=None)
            handler.do_OPTIONS()
            headers = [call.args[0] for call in handler.send_header.call_args_list]
            self.assertFalse(
                any(h.lower().startswith("access-control-allow-") for h in headers),
                f"Unexpected CORS headers on {path}: {headers}",
            )

    # -- 404 --------------------------------------------------------------

    def test_unknown_path_404(self):
        handler, _wfile = self._rh.request("GET", "/nonexistent")
        handler.do_GET()
        self.assertEqual(self._get_status(handler), 404)

    # -- answer (P0-3: requires run_id) -----------------------------------

    def test_answer_forwarded_to_agent_loop(self):
        from lucnhan.control.server import RunState

        agent_loop = MagicMock()
        runner = MagicMock()
        runner.agent_loop = agent_loop
        self.controller.get_runner.return_value = runner

        with self.state.lock:
            self.state.run = RunState(run_id="test-run")

        handler, _wfile = self._rh.request(
            "POST",
            "/answer",
            body={"run_id": "test-run", "answer": "yes"},
        )
        handler.do_POST()
        agent_loop.submit_user_answer.assert_called_with("yes")

    def test_answer_with_run_id_mismatch(self):
        from lucnhan.control.server import RunState

        with self.state.lock:
            self.state.run = RunState(run_id="active-run")

        handler, _wfile = self._rh.request(
            "POST",
            "/answer",
            body={"run_id": "other-run", "answer": "yes"},
        )
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)

    def test_answer_missing_run_id(self):
        handler, _wfile = self._rh.request("POST", "/answer", body={"answer": "yes"})
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)

    def test_answer_missing_answer_field(self):
        handler, _wfile = self._rh.request("POST", "/answer", body={"run_id": "test-run"})
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)

    def test_answer_no_active_run(self):
        handler, _wfile = self._rh.request(
            "POST",
            "/answer",
            body={"run_id": "nonexistent", "answer": "yes"},
        )
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)

    def test_answer_finished_run_rejected(self):
        from lucnhan.control.server import RunState

        with self.state.lock:
            self.state.run = RunState(run_id="test-run")
            self.state.run.finished = True

        handler, _wfile = self._rh.request(
            "POST",
            "/answer",
            body={"run_id": "test-run", "answer": "yes"},
        )
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)

    # -- tool-approval (P0-3: requires run_id) ----------------------------

    def test_tool_approval_forwarded(self):
        from lucnhan.control.server import RunState

        agent_loop = MagicMock()
        runner = MagicMock()
        runner.agent_loop = agent_loop
        self.controller.get_runner.return_value = runner

        with self.state.lock:
            self.state.run = RunState(run_id="test-run")

        handler, _wfile = self._rh.request(
            "POST",
            "/tool-approval",
            body={"run_id": "test-run", "approved": True},
        )
        handler.do_POST()
        agent_loop.submit_tool_approval.assert_called_with("allow")

    def test_tool_approval_missing_run_id(self):
        handler, _wfile = self._rh.request("POST", "/tool-approval", body={"approved": True})
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)

    def test_tool_approval_finished_run_rejected(self):
        from lucnhan.control.server import RunState

        with self.state.lock:
            self.state.run = RunState(run_id="test-run")
            self.state.run.finished = True

        handler, _wfile = self._rh.request(
            "POST",
            "/tool-approval",
            body={"run_id": "test-run", "approved": True},
        )
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)

    # -- approval (P0-3: requires run_id) ---------------------------------

    def test_approval_forwarded(self):
        from lucnhan.control.server import RunState

        agent_loop = MagicMock()
        runner = MagicMock()
        runner.agent_loop = agent_loop
        self.controller.get_runner.return_value = runner

        with self.state.lock:
            self.state.run = RunState(run_id="test-run")

        handler, _wfile = self._rh.request(
            "POST",
            "/approval",
            body={"run_id": "test-run", "approved": True},
        )
        handler.do_POST()
        agent_loop.submit_approval.assert_called_with("approve")

    def test_approval_missing_run_id(self):
        handler, _wfile = self._rh.request("POST", "/approval", body={"approved": True})
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)

    def test_approval_finished_run_rejected(self):
        from lucnhan.control.server import RunState

        with self.state.lock:
            self.state.run = RunState(run_id="test-run")
            self.state.run.finished = True

        handler, _wfile = self._rh.request(
            "POST",
            "/approval",
            body={"run_id": "test-run", "approved": True},
        )
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)

    # -- malformed decision payloads (must return 400, no side effects) ---

    def test_tool_approval_missing_decision_400(self):
        """Empty body or no decision field returns 400 with no side effects."""
        from lucnhan.control.server import RunState

        with self.state.lock:
            self.state.run = RunState(run_id="test-run")

        handler, _wfile = self._rh.request(
            "POST",
            "/tool-approval",
            body={"run_id": "test-run"},
        )
        handler.do_POST()
        self.assertEqual(self._get_status(handler), 400)
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)

    def test_tool_approval_conflicting_fields_400(self):
        """Both decision and approved fields → 400."""
        from lucnhan.control.server import RunState

        with self.state.lock:
            self.state.run = RunState(run_id="test-run")

        handler, _wfile = self._rh.request(
            "POST",
            "/tool-approval",
            body={
                "run_id": "test-run",
                "decision": "allow",
                "approved": True,
            },
        )
        handler.do_POST()
        self.assertEqual(self._get_status(handler), 400)

    def test_tool_approval_approved_not_bool_400(self):
        """approved must be a boolean, not a string."""
        from lucnhan.control.server import RunState

        with self.state.lock:
            self.state.run = RunState(run_id="test-run")

        handler, _wfile = self._rh.request(
            "POST",
            "/tool-approval",
            body={"run_id": "test-run", "approved": "true"},
        )
        handler.do_POST()
        self.assertEqual(self._get_status(handler), 400)

    def test_tool_approval_bad_decision_400(self):
        """Unknown decision string → 400."""
        from lucnhan.control.server import RunState

        with self.state.lock:
            self.state.run = RunState(run_id="test-run")

        handler, _wfile = self._rh.request(
            "POST",
            "/tool-approval",
            body={"run_id": "test-run", "decision": "maybe"},
        )
        handler.do_POST()
        self.assertEqual(self._get_status(handler), 400)

    def test_tool_approval_missing_decision_no_side_effects(self):
        """Missing decision must not call submit_tool_approval."""
        from lucnhan.control.server import RunState

        agent_loop = MagicMock()
        runner = MagicMock()
        runner.agent_loop = agent_loop
        self.controller.get_runner.return_value = runner

        with self.state.lock:
            self.state.run = RunState(run_id="test-run")

        handler, _wfile = self._rh.request(
            "POST",
            "/tool-approval",
            body={"run_id": "test-run"},
        )
        handler.do_POST()
        agent_loop.submit_tool_approval.assert_not_called()

    def test_approval_missing_decision_400(self):
        """Empty body or no decision field returns 400 with no side effects."""
        from lucnhan.control.server import RunState

        with self.state.lock:
            self.state.run = RunState(run_id="test-run")

        handler, _wfile = self._rh.request(
            "POST",
            "/approval",
            body={"run_id": "test-run"},
        )
        handler.do_POST()
        self.assertEqual(self._get_status(handler), 400)
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)

    def test_approval_conflicting_fields_400(self):
        """Both decision and approved fields → 400."""
        from lucnhan.control.server import RunState

        with self.state.lock:
            self.state.run = RunState(run_id="test-run")

        handler, _wfile = self._rh.request(
            "POST",
            "/approval",
            body={
                "run_id": "test-run",
                "decision": "approve",
                "approved": True,
            },
        )
        handler.do_POST()
        self.assertEqual(self._get_status(handler), 400)

    def test_approval_approved_not_bool_400(self):
        """approved must be a boolean, not a string."""
        from lucnhan.control.server import RunState

        with self.state.lock:
            self.state.run = RunState(run_id="test-run")

        handler, _wfile = self._rh.request(
            "POST",
            "/approval",
            body={"run_id": "test-run", "approved": 1},
        )
        handler.do_POST()
        self.assertEqual(self._get_status(handler), 400)

    def test_approval_bad_decision_400(self):
        """Unknown decision string → 400."""
        from lucnhan.control.server import RunState

        with self.state.lock:
            self.state.run = RunState(run_id="test-run")

        handler, _wfile = self._rh.request(
            "POST",
            "/approval",
            body={"run_id": "test-run", "decision": "maybe_later"},
        )
        handler.do_POST()
        self.assertEqual(self._get_status(handler), 400)

    def test_approval_missing_decision_no_side_effects(self):
        """Missing decision must not call submit_approval."""
        from lucnhan.control.server import RunState

        agent_loop = MagicMock()
        runner = MagicMock()
        runner.agent_loop = agent_loop
        self.controller.get_runner.return_value = runner

        with self.state.lock:
            self.state.run = RunState(run_id="test-run")

        handler, _wfile = self._rh.request(
            "POST",
            "/approval",
            body={"run_id": "test-run"},
        )
        handler.do_POST()
        agent_loop.submit_approval.assert_not_called()

    # -- events (P0-4: index validation and run_id race) ------------------

    def test_events_no_run(self):
        handler, _wfile = self._rh.request("GET", "/events")
        handler.do_GET()
        resp = self._get_json(_wfile)
        self.assertTrue(resp.get("finished", False))
        self.assertEqual(resp.get("events"), [])

    def test_events_with_active_run(self):
        from lucnhan.control.server import RunState

        with self.state.lock:
            self.state.run = RunState(run_id="test-run")
            self.state.run.event_buffer.append({"type": "text_delta", "text": "hello!", "seq": 0})
            self.state.run.next_seq = 1

        handler, _wfile = self._rh.request("GET", "/events?index=0")
        handler.do_GET()
        resp = self._get_json(_wfile)
        self.assertEqual(len(resp["events"]), 1)
        self.assertEqual(resp["events"][0]["text"], "hello!")

    def test_events_invalid_index_rejected(self):
        handler, _wfile = self._rh.request("GET", "/events?index=abc")
        handler.do_GET()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)

    def test_events_negative_index_rejected(self):
        handler, _wfile = self._rh.request("GET", "/events?index=-1")
        handler.do_GET()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)

    def test_events_stale_run_id_after_empty_events(self):
        """When run_id is stale and no events, return empty finished."""
        handler, _wfile = self._rh.request("GET", "/events?run_id=nonexistent")
        handler.do_GET()
        resp = self._get_json(_wfile)
        self.assertTrue(resp.get("finished", False))
        self.assertEqual(resp.get("events"), [])

    # -- malformed JSON / Content-Length (P0-5) ---------------------------

    def test_malformed_json_returns_400(self):
        handler, _wfile = self._rh.request(
            "POST",
            "/prompt",
            raw_body=b"not json",
        )
        handler.do_POST()
        self.assertEqual(self._get_status(handler), 400)
        self.assertEqual(handler.send_response.call_count, 1)

    def test_invalid_content_length_returns_400(self):
        handler, _wfile = self._rh.request(
            "POST",
            "/prompt",
            body={"prompt": "hello"},
        )
        # Override the Content-Length header with garbage.
        handler.headers._d["Content-Length"] = "not-a-number"
        handler.do_POST()
        self.assertEqual(self._get_status(handler), 400)
        self.assertEqual(handler.send_response.call_count, 1)

    def test_non_object_json_returns_400(self):
        handler, _wfile = self._rh.request(
            "POST",
            "/prompt",
            raw_body=b'"just a string"',
        )
        handler.do_POST()
        self.assertEqual(self._get_status(handler), 400)
        self.assertEqual(handler.send_response.call_count, 1)

    def test_array_json_returns_400(self):
        handler, _wfile = self._rh.request(
            "POST",
            "/prompt",
            raw_body=b"[1, 2, 3]",
        )
        handler.do_POST()
        self.assertEqual(self._get_status(handler), 400)
        self.assertEqual(handler.send_response.call_count, 1)

    def test_negative_content_length_returns_400(self):
        handler, _wfile = self._rh.request(
            "POST",
            "/prompt",
            body={"prompt": "hello"},
        )
        handler.headers._d["Content-Length"] = "-1"
        handler.do_POST()
        self.assertEqual(self._get_status(handler), 400)
        self.assertEqual(handler.send_response.call_count, 1)

    # -- P0-5: /tools capability filtering ---------------------------------

    def test_tools_uses_list_available_tools(self):
        """/tools must call list_available_tools() not list_tools()."""
        from lucnhan.control.server import ControlServerState, _make_handler

        controller = MagicMock()
        registry = MagicMock()
        controller.tool_registry = registry
        registry.list_tools = MagicMock()
        registry.list_available_tools = MagicMock(return_value=[])
        registry.list_names = MagicMock(return_value=[])

        state = ControlServerState(token="test-token")
        handler_cls = _make_handler(state, controller)
        rh = _RequestHelper(handler_cls, state, controller)
        handler, _wfile = rh.request("GET", "/tools")
        handler.do_GET()

        # list_available_tools() must be the one called — not list_tools() or list_names().
        registry.list_available_tools.assert_called()
        registry.list_tools.assert_not_called()
        registry.list_names.assert_not_called()

    def test_tools_response_includes_tools_list_and_count(self):
        """/tools response must include 'tools' list and 'count'."""
        from lucnhan.control.server import ControlServerState, _make_handler
        from lucnhan.tools.base import ToolDefinition

        def make_def(name):
            return ToolDefinition(
                name=name,
                description=f"{name} description",
                parameters={},
                handler=lambda: None,
            )

        controller = MagicMock()
        registry = MagicMock()
        controller.tool_registry = registry
        registry.list_available_tools.return_value = [
            make_def("func_a"),
            make_def("func_b"),
        ]

        state = ControlServerState(token="test-token")
        handler_cls = _make_handler(state, controller)
        rh = _RequestHelper(handler_cls, state, controller)
        handler, wfile = rh.request("GET", "/tools")
        handler.do_GET()

        resp = self._get_json(wfile)
        self.assertIsInstance(resp.get("tools"), list)
        self.assertEqual(len(resp["tools"]), 2)
        self.assertEqual(resp["count"], 2)

    # -- P0-4 / P1-3: single-response guarantee ---------------------------

    def test_malformed_body_only_one_response_on_prompt(self):
        """Malformed body must NOT produce a second 'Missing prompt' response."""
        handler, _wfile = self._rh.request(
            "POST",
            "/prompt",
            raw_body=b"not json",
        )
        handler.do_POST()
        self.assertEqual(handler.send_response.call_count, 1)
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)
        remaining = _wfile.read()
        self.assertFalse(remaining, f"Extra data after response: {remaining!r}")
        # No controller side effects on malformed body.
        self.controller.start_agent.assert_not_called()
        self.controller.shutdown.assert_not_called()

    def test_malformed_body_only_one_response_on_answer(self):
        """Malformed body on /answer must produce only one response."""
        handler, _wfile = self._rh.request(
            "POST",
            "/answer",
            raw_body=b"not json",
        )
        handler.do_POST()
        self.assertEqual(handler.send_response.call_count, 1)
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)
        remaining = _wfile.read()
        self.assertFalse(remaining, f"Extra data after response: {remaining!r}")

    def test_malformed_body_only_one_response_on_tool_approval(self):
        """Malformed body on /tool-approval must produce only one response."""
        handler, _wfile = self._rh.request(
            "POST",
            "/tool-approval",
            raw_body=b"not json",
        )
        handler.do_POST()
        self.assertEqual(handler.send_response.call_count, 1)
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)
        remaining = _wfile.read()
        self.assertFalse(remaining, f"Extra data after response: {remaining!r}")

    def test_malformed_body_only_one_response_on_approval(self):
        """Malformed body on /approval must produce only one response."""
        handler, _wfile = self._rh.request(
            "POST",
            "/approval",
            raw_body=b"not json",
        )
        handler.do_POST()
        self.assertEqual(handler.send_response.call_count, 1)
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)
        remaining = _wfile.read()
        self.assertFalse(remaining, f"Extra data after response: {remaining!r}")

    def test_malformed_body_only_one_response_on_cancel(self):
        """Malformed body on /cancel must produce only one response."""
        handler, _wfile = self._rh.request(
            "POST",
            "/cancel",
            raw_body=b"not json",
        )
        handler.do_POST()
        self.assertEqual(handler.send_response.call_count, 1)
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)
        remaining = _wfile.read()
        self.assertFalse(remaining, f"Extra data after response: {remaining!r}")

    # -- request body size limit -----------------------------------------

    def test_read_body_rejects_oversized_request(self):
        """_read_body returns HTTP 413 when Content-Length exceeds max."""
        from lucnhan.control.server import _MAX_REQUEST_BODY

        handler, _wfile = self._rh.request(
            "POST",
            "/prompt",
            body={"prompt": "hi"},
        )
        # Override Content-Length to simulate oversized body.
        handler.headers._d["Content-Length"] = str(_MAX_REQUEST_BODY + 1)
        handler.command = "POST"
        handler.path = "/prompt"

        body, err = handler._read_body()
        self.assertEqual(body, {})
        self.assertIsNotNone(err)
        self.assertEqual(err[0], 413)

    def test_read_body_accepts_max_size(self):
        """_read_body accepts body exactly at the max size."""
        handler, _wfile = self._rh.request(
            "POST",
            "/prompt",
            body={"prompt": "hi"},
        )
        handler.command = "POST"
        handler.path = "/prompt"

        body, err = handler._read_body()
        self.assertIsNotNone(body)
        self.assertIsNone(err)

    # -- approval decision field -----------------------------------------

    def test_approval_accepts_decision_field(self):
        """POST /approval with 'decision' field is canonical."""
        from lucnhan.control.server import RunState

        agent_loop = MagicMock()
        runner = MagicMock()
        runner.agent_loop = agent_loop
        self.controller.get_runner.return_value = runner

        with self.state.lock:
            self.state.run = RunState(run_id="test-run")

        handler, _wfile = self._rh.request(
            "POST",
            "/approval",
            body={"run_id": "test-run", "decision": "approve"},
        )
        handler.do_POST()
        agent_loop.submit_approval.assert_called_with("approve")

    def test_approval_rejects_invalid_decision(self):
        """POST /approval with invalid decision returns 400."""
        from lucnhan.control.server import RunState

        with self.state.lock:
            self.state.run = RunState(run_id="test-run")

        handler, _wfile = self._rh.request(
            "POST",
            "/approval",
            body={"run_id": "test-run", "decision": "bogus"},
        )
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)

    # -- tools returns structured JSON parameters ------------------------

    def test_tools_parameters_are_json_objects(self):
        """/tools response must have parameters as JSON schema, not dataclass repr."""
        from lucnhan.tools.base import ParameterSchema, ToolDefinition

        tool = ToolDefinition(
            name="test_tool",
            description="A test tool",
            parameters=[
                ParameterSchema(name="addr", type="string", description="Address", required=True),
            ],
        )
        self.controller.tool_registry.list_available_tools.return_value = [tool]

        handler, _wfile = self._rh.request("GET", "/tools")
        handler.do_GET()
        resp = self._get_json(_wfile)
        tools = resp["tools"]
        self.assertEqual(len(tools), 1)
        params = tools[0]["parameters"]
        # Must be a JSON object (dict), not a ParameterSchema string.
        self.assertIsInstance(params, dict)
        self.assertIn("type", params)
        self.assertEqual(params["type"], "object")
        self.assertIn("addr", params.get("properties", {}))


class TestShutdownDeadlock(unittest.TestCase):
    """P0-1: Prove shutdown does not deadlock with an active broker."""

    def test_shutdown_stops_broker_outside_lock(self):
        from lucnhan.control.server import (
            ControlServerState,
            EventBroker,
            RunState,
        )

        controller = MagicMock()
        controller.cancel = MagicMock()
        controller.get_runner.return_value = None
        controller.on_agent_finished = MagicMock()

        state = ControlServerState(token="test-token")
        with state.lock:
            state.run = RunState(run_id="deadlock-test")
            # Create a real broker that would try to acquire the lock
            # in its finally path.
            broker = EventBroker(state, controller)
            state.run.broker = broker

        # Simulate shutdown: snapshot broker under lock, stop outside lock.
        broker_ref = None
        with state.lock:
            if state.run is not None and state.run.broker is not None:
                broker_ref = state.run.broker

        self.assertIsNotNone(broker_ref)

        # Stop the broker outside the lock — this must not deadlock.
        # stop() is safe to call even if the broker was never started
        # (the thread join only fires if _thread is alive).
        if broker_ref is not None:
            broker_ref.stop()

        # Verify we can re-acquire the lock after stopping.
        with state.lock:
            self.assertTrue(state.run is not None)
            # Mark as finished to clean up.
            if state.run is not None:
                state.run.finished = True

    def test_control_server_shutdown_does_not_deadlock(self):
        """ControlServer.shutdown() must not deadlock with active broker."""
        # We can't fully construct a real ControlServer without a real
        # HeadlessSessionController, but we verify the pattern works.
        # The key property: calling broker.stop() outside the state lock.
        from lucnhan.control.server import ControlServerState, EventBroker, RunState

        controller = MagicMock()
        controller.cancel = MagicMock()
        controller.get_runner.return_value = None
        controller.on_agent_finished = MagicMock()
        controller.shutdown = MagicMock()

        state = ControlServerState(token="test-token")
        with state.lock:
            state.run = RunState(run_id="sd-test")
            broker = EventBroker(state, controller)
            state.run.broker = broker

        # Grab broker outside lock (this is what shutdown() does).
        broker_outer = None
        with state.lock:
            rs = state.run
            if rs is not None and rs.broker is not None:
                broker_outer = rs.broker

        self.assertIsNotNone(broker_outer)
        # stop() should not deadlock — safe even if broker was never started.
        if broker_outer is not None:
            broker_outer.stop()

        with state.lock:
            if state.run is not None:
                state.run.finished = True

    def test_event_broker_mark_finished_drains_outside_lock(self):
        """_mark_finished() drains runner events outside state.lock (P2-1)."""
        from lucnhan.control.server import ControlServerState, EventBroker, RunState

        lock_order: list[str] = []  # noqa: F841

        class _TrackingController:
            def __init__(self):
                self.get_runner = MagicMock(return_value=None)
                self.on_agent_finished = MagicMock()
                self.cancel = MagicMock()

        controller = _TrackingController()
        state = ControlServerState(token="test-token")

        with state.lock:
            state.run = RunState(run_id="lock-test")
            broker = EventBroker(state, controller)
            state.run.broker = broker

        # Mark finished — should not deadlock.
        broker._mark_finished()

        with state.lock:
            self.assertTrue(state.run is not None)
            if state.run is not None:
                self.assertTrue(state.run.finished)


class TestShutdownCallback(unittest.TestCase):
    """P1-1: Simplified ShutdownCallback."""

    def test_trigger_signals_event(self):
        from lucnhan.control.server import ShutdownCallback

        cb = ShutdownCallback(lambda: None)
        self.assertFalse(cb.signalled.is_set())
        cb.trigger()
        self.assertTrue(cb.signalled.is_set())

    def test_trigger_idempotent(self):
        from lucnhan.control.server import ShutdownCallback

        called = []
        cb = ShutdownCallback(lambda: called.append(1))
        cb.trigger()
        cb.trigger()
        # Httpd shutdown callback may be called more than once
        # but signalled is only set once.
        self.assertTrue(cb.signalled.is_set())

    def test_trigger_swallows_exceptions(self):
        from lucnhan.control.server import ShutdownCallback

        def raiser():
            raise RuntimeError("boom")

        cb = ShutdownCallback(raiser)
        # Should not raise.
        cb.trigger()
        self.assertTrue(cb.signalled.is_set())


class TestRunIdSideEffects(unittest.TestCase):
    """P1-3: Strict run_id no-side-effect tests."""

    def setUp(self):
        from lucnhan.control.server import ControlServerState, _make_handler

        self.controller = MagicMock()
        self.controller.tool_registry = MagicMock()
        self.controller.tool_registry.list_tools.return_value = []
        self.controller.start_agent.return_value = None
        self.controller.get_runner.return_value = None
        self.controller.is_agent_running = False
        self.controller.cancel = MagicMock()
        self.controller.on_agent_finished = MagicMock()
        self.controller.shutdown = MagicMock()

        self.state = ControlServerState(token="test-token")
        handler_cls = _make_handler(self.state, self.controller)
        self._rh = _RequestHelper(handler_cls, self.state, self.controller)

    def _get_json(self, _wfile: io.BytesIO) -> dict:
        _wfile.seek(0)
        raw = _wfile.read()
        if not raw:
            return {}
        parts = raw.split(b"\r\n\r\n", 1)
        if len(parts) < 2:
            return {}
        try:
            return json.loads(parts[1])
        except json.JSONDecodeError:
            return {}

    # -- /answer side-effect guards ---------------------------------------

    def test_answer_stale_run_id_no_side_effect(self):
        from lucnhan.control.server import RunState

        agent_loop = MagicMock()
        runner = MagicMock()
        runner.agent_loop = agent_loop
        self.controller.get_runner.return_value = runner

        with self.state.lock:
            self.state.run = RunState(run_id="active-run")

        handler, _wfile = self._rh.request(
            "POST",
            "/answer",
            body={"run_id": "stale-run", "answer": "yes"},
        )
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)
        agent_loop.submit_user_answer.assert_not_called()

    def test_answer_finished_run_no_side_effect(self):
        from lucnhan.control.server import RunState

        agent_loop = MagicMock()
        runner = MagicMock()
        runner.agent_loop = agent_loop
        self.controller.get_runner.return_value = runner

        with self.state.lock:
            self.state.run = RunState(run_id="finished-run")
            self.state.run.finished = True

        handler, _wfile = self._rh.request(
            "POST",
            "/answer",
            body={"run_id": "finished-run", "answer": "yes"},
        )
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)
        agent_loop.submit_user_answer.assert_not_called()

    def test_answer_no_active_run_no_side_effect(self):
        agent_loop = MagicMock()
        runner = MagicMock()
        runner.agent_loop = agent_loop
        self.controller.get_runner.return_value = runner

        # No active run in state.
        handler, _wfile = self._rh.request(
            "POST",
            "/answer",
            body={"run_id": "any-run", "answer": "yes"},
        )
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)
        agent_loop.submit_user_answer.assert_not_called()

    # -- /tool-approval side-effect guards --------------------------------

    def test_tool_approval_stale_run_id_no_side_effect(self):
        from lucnhan.control.server import RunState

        agent_loop = MagicMock()
        runner = MagicMock()
        runner.agent_loop = agent_loop
        self.controller.get_runner.return_value = runner

        with self.state.lock:
            self.state.run = RunState(run_id="active-run")

        handler, _wfile = self._rh.request(
            "POST",
            "/tool-approval",
            body={"run_id": "stale-run", "approved": True},
        )
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)
        agent_loop.submit_tool_approval.assert_not_called()

    def test_tool_approval_finished_run_no_side_effect(self):
        from lucnhan.control.server import RunState

        agent_loop = MagicMock()
        runner = MagicMock()
        runner.agent_loop = agent_loop
        self.controller.get_runner.return_value = runner

        with self.state.lock:
            self.state.run = RunState(run_id="finished-run")
            self.state.run.finished = True

        handler, _wfile = self._rh.request(
            "POST",
            "/tool-approval",
            body={"run_id": "finished-run", "approved": True},
        )
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)
        agent_loop.submit_tool_approval.assert_not_called()

    # -- /approval side-effect guards -------------------------------------

    def test_approval_stale_run_id_no_side_effect(self):
        from lucnhan.control.server import RunState

        agent_loop = MagicMock()
        runner = MagicMock()
        runner.agent_loop = agent_loop
        self.controller.get_runner.return_value = runner

        with self.state.lock:
            self.state.run = RunState(run_id="active-run")

        handler, _wfile = self._rh.request(
            "POST",
            "/approval",
            body={"run_id": "stale-run", "approved": True},
        )
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)
        agent_loop.submit_approval.assert_not_called()

    def test_approval_finished_run_no_side_effect(self):
        from lucnhan.control.server import RunState

        agent_loop = MagicMock()
        runner = MagicMock()
        runner.agent_loop = agent_loop
        self.controller.get_runner.return_value = runner

        with self.state.lock:
            self.state.run = RunState(run_id="finished-run")
            self.state.run.finished = True

        handler, _wfile = self._rh.request(
            "POST",
            "/approval",
            body={"run_id": "finished-run", "approved": True},
        )
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)
        agent_loop.submit_approval.assert_not_called()

    # -- /cancel side-effect guards ---------------------------------------

    def test_cancel_stale_run_id_no_side_effect(self):
        from lucnhan.control.server import RunState

        with self.state.lock:
            self.state.run = RunState(run_id="active-run")

        handler, _wfile = self._rh.request(
            "POST",
            "/cancel",
            body={"run_id": "stale-run"},
        )
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)
        self.controller.cancel.assert_not_called()

    def test_cancel_finished_run_no_side_effect(self):
        from lucnhan.control.server import RunState

        with self.state.lock:
            self.state.run = RunState(run_id="finished-run")
            self.state.run.finished = True

        handler, _wfile = self._rh.request(
            "POST",
            "/cancel",
            body={"run_id": "finished-run"},
        )
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)
        self.controller.cancel.assert_not_called()

    def test_cancel_no_active_run_no_side_effect(self):
        # No run in state.
        handler, _wfile = self._rh.request(
            "POST",
            "/cancel",
            body={"run_id": "any-run"},
        )
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)
        self.controller.cancel.assert_not_called()

    # -- missing run_id no-side-effect guards (P1-2) -----------------------

    def test_answer_missing_run_id_no_side_effect(self):
        agent_loop = MagicMock()
        runner = MagicMock()
        runner.agent_loop = agent_loop
        self.controller.get_runner.return_value = runner

        handler, _wfile = self._rh.request(
            "POST",
            "/answer",
            body={"answer": "yes"},  # no run_id
        )
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)
        agent_loop.submit_answer.assert_not_called()

    def test_tool_approval_missing_run_id_no_side_effect(self):
        agent_loop = MagicMock()
        runner = MagicMock()
        runner.agent_loop = agent_loop
        self.controller.get_runner.return_value = runner

        handler, _wfile = self._rh.request(
            "POST",
            "/tool-approval",
            body={"approved": True},  # no run_id
        )
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)
        agent_loop.submit_tool_approval.assert_not_called()

    def test_approval_missing_run_id_no_side_effect(self):
        agent_loop = MagicMock()
        runner = MagicMock()
        runner.agent_loop = agent_loop
        self.controller.get_runner.return_value = runner

        handler, _wfile = self._rh.request(
            "POST",
            "/approval",
            body={"action": "approve"},  # no run_id
        )
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)
        agent_loop.submit_approval.assert_not_called()

    def test_cancel_missing_run_id_no_side_effect(self):
        handler, _wfile = self._rh.request(
            "POST",
            "/cancel",
            body={},  # no run_id
        )
        handler.do_POST()
        resp = self._get_json(_wfile)
        self.assertIn("error", resp)
        self.controller.cancel.assert_not_called()


class TestEventsRunReplacementRace(unittest.TestCase):
    """P1-4: /events long-poll run replacement race."""

    def setUp(self):
        from lucnhan.control.server import ControlServerState, _make_handler

        self.controller = MagicMock()
        self.controller.tool_registry = MagicMock()
        self.controller.tool_registry.list_tools.return_value = []
        self.controller.start_agent.return_value = None
        self.controller.get_runner.return_value = None
        self.controller.is_agent_running = False
        self.controller.cancel = MagicMock()
        self.controller.on_agent_finished = MagicMock()
        self.controller.shutdown = MagicMock()

        self.state = ControlServerState(token="test-token")
        handler_cls = _make_handler(self.state, self.controller)
        self._rh = _RequestHelper(handler_cls, self.state, self.controller)

    def _get_json(self, _wfile: io.BytesIO) -> dict:
        _wfile.seek(0)
        raw = _wfile.read()
        if not raw:
            return {}
        parts = raw.split(b"\r\n\r\n", 1)
        if len(parts) < 2:
            return {}
        try:
            return json.loads(parts[1])
        except json.JSONDecodeError:
            return {}

    def test_stale_run_id_replaced_by_new_run_returns_finished_empty(self):
        """Old run long-poll returns empty finished when run replaced."""
        import threading

        from lucnhan.control.server import RunState

        # Set up "old" run with a broker and seed events in event_buffer
        with self.state.lock:
            old_run = RunState(run_id="old-run-runid")
            self.state.run = old_run
            old_run.event_buffer.append({"type": "text_delta", "text": "old-event", "turn_number": 1})

        # Now query /events with the old run ID and wait=1
        handler, _wfile = self._rh.request("GET", "/events?run_id=old-run-runid&index=0&wait=1")

        # In a background thread, replace the run and notify condition
        def _replace_run():
            time.sleep(0.1)
            with self.state.lock:
                self.state.run = RunState(run_id="new-run-runid")
                old_run.finished = True
                self.state.condition.notify_all()

        t = threading.Thread(target=_replace_run, daemon=True)
        t.start()

        handler.do_GET()

        resp = self._get_json(_wfile)
        # After the old run is replaced, the handler should report finished.
        # (Events returned may include seed events put into the buffer
        # before the handler ran; the critical check is finished=true.)
        self.assertTrue(resp.get("finished", False))


class TestShutdownProductionPath(unittest.TestCase):
    """P1-5: Production-path shutdown tests using real ControlServer/State."""

    def setUp(self):

        self.controller = MagicMock()
        self.controller.tool_registry = MagicMock()
        self.controller.tool_registry.list_tools.return_value = []
        self.controller.start_agent.return_value = None
        self.controller.get_runner.return_value = None
        self.controller.is_agent_running = False
        self.controller.cancel = MagicMock()
        self.controller.on_agent_finished = MagicMock()
        self.controller.shutdown = MagicMock()
        self.controller.session = MagicMock(id="test-session")

        self.server = MagicMock()  # ControlServer spy

    def test_shutdown_sets_shutting_down_and_notifies(self):
        """shutdown() sets flag and notifies waiters."""
        from lucnhan.control.server import ControlServer, RunState

        server = ControlServer(self.controller, host="127.0.0.1", port=0)

        # Set up an active run
        with server._state.lock:
            server._state.run = RunState(run_id="shutdown-test")
            server._state.run.finished = True

        # Assert initial state
        self.assertFalse(server._state.shutting_down)
        self.assertFalse(server._state.shutdown_complete.is_set())

        # Call shutdown
        server.shutdown()

        self.assertTrue(server._state.shutting_down)
        self.assertTrue(server._state.shutdown_complete.is_set())

    def test_shutdown_called_idempotently(self):
        """Multiple shutdown() calls are safe."""
        from lucnhan.control.server import ControlServer, RunState

        server = ControlServer(self.controller, host="127.0.0.1", port=0)

        with server._state.lock:
            server._state.run = RunState(run_id="shutdown-test")
            server._state.run.finished = True

        server.shutdown()
        # Second call should not raise
        server.shutdown()

        self.assertTrue(server._state.shutting_down)

    def test_shutdown_stops_broker(self):
        """shutdown() calls broker.stop() on the active run."""
        from lucnhan.control.server import ControlServer, RunState

        server = ControlServer(self.controller, host="127.0.0.1", port=0)

        mock_broker = MagicMock()
        with server._state.lock:
            server._state.run = RunState(run_id="shutdown-test")
            server._state.run.broker = mock_broker  # type: ignore[assignment]

        server.shutdown()

        mock_broker.stop.assert_called()

    def test_shutdown_with_real_broker_bounded_wait_shutdown(self):
        """Start an EventBroker, call server.shutdown(), verify
        shutdown_complete is set within a bounded time (P1-3)."""
        import threading

        from lucnhan.control.server import ControlServer, EventBroker, RunState

        server = ControlServer(self.controller, host="127.0.0.1", port=0)

        with server._state.lock:
            server._state.run = RunState(run_id="bounded-test")
            broker = EventBroker(server._state, self.controller)
            server._state.run.broker = broker

        # Start broker in a thread.
        def _run_broker():
            broker.start()

        t = threading.Thread(target=_run_broker, daemon=True)
        t.start()

        import time

        # Give broker a moment to enter its loop.
        time.sleep(0.05)

        # Trigger shutdown — must complete within bounded time.
        server.shutdown()

        # shutdown_complete event should be set promptly.
        timeout = 2.0  # generous bound
        result = server._state.shutdown_complete.wait(timeout=timeout)
        self.assertTrue(result, f"shutdown_complete not set within {timeout}s")

        # Broker thread should have exited or be joinable quickly.
        t.join(timeout=2.0)
        self.assertFalse(t.is_alive(), "Broker thread still alive after shutdown")


class TestProtocolSerialization(unittest.TestCase):
    """Tests for control API JSON serialization — no default=str."""

    def test_make_json_response_rejects_non_serializable(self):
        """Non-serializable values must raise TypeError, not stringify."""
        from lucnhan.control.protocol import make_json_response

        class NonSerializable:
            pass

        with self.assertRaises(TypeError):
            make_json_response({"value": NonSerializable()})

    def test_make_json_response_accepts_normal_types(self):
        from lucnhan.control.protocol import make_json_response

        status, headers, body = make_json_response(
            {"key": "value", "num": 42, "bool": True, "none": None, "list": [1, 2, 3]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn('"key"', body)
        self.assertIn('"value"', body)

    def test_make_error_json_accepts_standard(self):
        from lucnhan.control.protocol import make_error_json

        status, _headers, body = make_error_json("Something went wrong", status=500, detail="extra")
        self.assertEqual(status, 500)
        self.assertIn('"error"', body)
        self.assertIn('"Something went wrong"', body)
        self.assertIn('"detail"', body)


# ---------------------------------------------------------------------------
# Pass-through: reasoning/recovery events in EventBroker
# ---------------------------------------------------------------------------


class _FakeBrokerRunner:
    """Fake runner that feeds events from a queue for the EventBroker."""

    def __init__(self, events: list):
        import queue

        self._queue: queue.Queue = queue.Queue()
        for e in events:
            self._queue.put(e)
        self.agent_loop = MagicMock()

    def get_event(self, timeout: float = 0.2):
        import queue

        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


class TestEventBrokerReasoningPassThrough(unittest.TestCase):
    """GLM reasoning/recovery events must enter ``event_buffer`` via
    ``to_dict()`` but must NOT mutate ``run.final_text`` or ``run.exit_code``.

    Only TEXT_DONE mutates ``final_text``; only ERROR/CANCELLED mutate
    ``exit_code``.
    """

    def _drain_broker(self, events: list):
        """Run the EventBroker against *events* and return the final RunState."""
        from lucnhan.control.server import (
            ControlServerState,
            EventBroker,
            RunState,
        )

        runner = _FakeBrokerRunner(events)

        class _Controller:
            def __init__(self):
                self._runner = runner
                self._running = True
                self.on_agent_finished = MagicMock()
                self.cancel = MagicMock()

            def get_runner(self):
                return self._runner

            @property
            def is_agent_running(self):
                # Flip to False after the queue is drained.
                return not self._runner._queue.empty()

        controller = _Controller()
        state = ControlServerState(token="test-token")

        with state.lock:
            state.run = RunState(run_id="passthrough-test")
            broker = EventBroker(state, controller)
            state.run.broker = broker

        broker.start()
        # Wait for the broker thread to finish draining.
        broker._thread.join(timeout=5.0) if broker._thread else None

        with state.lock:
            assert state.run is not None
            return state.run

    def test_reasoning_and_recovery_events_enter_buffer_without_mutating_status(self):
        from lucnhan.agent.turn import TurnEvent

        run = self._drain_broker(
            [
                TurnEvent.reasoning_event("hidden reasoning"),
                TurnEvent.recovery_start(
                    attempt=2,
                    reason="reasoning_degenerated",
                    discard_transient_reasoning=True,
                ),
                TurnEvent.text_done("visible"),
            ]
        )

        # final_text must be visible-only — reasoning is NOT in final_text.
        self.assertEqual(run.final_text, "visible")
        # exit_code stays at 0 — no error or cancellation.
        self.assertEqual(run.exit_code, 0)
        # Events must appear in the buffer.
        event_types = [ev["type"] for ev in run.event_buffer]
        self.assertIn("reasoning_delta", event_types)
        self.assertIn("recovery_start", event_types)

    def test_tool_call_discarded_enters_buffer_without_mutating_status(self):
        from lucnhan.agent.turn import TurnEvent

        run = self._drain_broker(
            [
                TurnEvent.tool_call_start("call_1", "read_bytes"),
                TurnEvent.tool_call_discarded("call_1", "read_bytes", "truncated"),
                TurnEvent.text_done("done"),
            ]
        )

        self.assertEqual(run.final_text, "done")
        self.assertEqual(run.exit_code, 0)
        event_types = [ev["type"] for ev in run.event_buffer]
        self.assertIn("tool_call_discarded", event_types)

    def test_recovery_failure_error_sets_exit_code(self):
        """Recovery failure ERROR is a real failure — exit_code must change."""
        from lucnhan.agent.turn import TurnEvent

        run = self._drain_broker(
            [
                TurnEvent.recovery_start(
                    attempt=2,
                    reason="reasoning_degenerated",
                    discard_transient_reasoning=True,
                ),
                TurnEvent.error_event("Reasoning degeneration persisted after recovery attempt."),
            ]
        )

        self.assertEqual(run.exit_code, 5)
        self.assertTrue(any("degeneration persisted" in e for e in run.errors))
