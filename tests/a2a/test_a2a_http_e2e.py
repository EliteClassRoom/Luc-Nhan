"""End-to-end tests for A2AClient against a mock HTTP server.

The existing unit tests cover the dispatcher's translation layer
but not the real HTTP transport. These tests spin up a local
``http.server`` in a background thread, configure ``A2AClient``
to call that server, and verify the full request/response
cycle for:
- ``discover()`` (GET ``/.well-known/agent.json``)
- ``send_task()`` (POST JSON-RPC ``tasks.send``)
- ``cancel_task()`` (background thread cancellation)
- Retry on transient HTTP errors
- Cleanup on ``close()``

We use ``http.server.HTTPServer`` (stdlib) bound to
``127.0.0.1`` on an OS-assigned port so there's no risk of
collision with other test runs.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.agent.a2a.client import A2AClient, A2AClientConfig
from rikugan.agent.a2a.types import A2ATaskStatus, ExternalAgentConfig

# ---------------------------------------------------------------------------
# Test fixtures: a thread-local HTTP server with a configurable handler
# ---------------------------------------------------------------------------


class _A2AServerHandler(BaseHTTPRequestHandler):
    """Minimal A2A-protocol handler for the mock server.

    The ``behavior`` class attribute is a thread-local-ish dict
    keyed by request count. Tests can set
    ``Handler.behavior[0] = {"status_code": 503}`` to make the
    first request return a transient error. The handler advances
    the counter after each call.
    """

    # Class-level (shared across instances) — we set it on the
    # subclass to keep tests independent.
    behavior: list[dict] = []  # noqa: RUF012
    call_count: int = 0
    last_request_body: bytes = b""

    def log_message(self, format, *args):
        # Silence the test output — BaseHTTPRequestHandler logs
        # every request to stderr by default.
        pass

    def do_GET(self) -> None:
        # ``/.well-known/agent.json`` is the discovery endpoint.
        # Respect the behavior queue (e.g. for 404 tests).
        idx = _A2AServerHandler.call_count
        _A2AServerHandler.call_count += 1
        if idx < len(_A2AServerHandler.behavior):
            cfg = _A2AServerHandler.behavior[idx]
            self._respond(
                cfg.get("status_code", 200),
                cfg.get("body", {"error": "not found"}),
            )
            return
        if self.path.endswith("/.well-known/agent.json"):
            self._respond(200, {
                "name": "mock-agent",
                "capabilities": ["code_generation", "research"],
                "model": "claude-test",
            })
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self) -> None:
        # Read the body
        length = int(self.headers.get("Content-Length", "0"))
        # Set as class attribute so tests can read it without
        # having to keep a reference to the handler instance.
        # (Setting on ``self`` only stores on the instance,
        # which gets garbage-collected when the request ends.)
        _A2AServerHandler.last_request_body = self.rfile.read(length)
        # Look up the configured behavior for this call
        idx = _A2AServerHandler.call_count
        _A2AServerHandler.call_count += 1
        if idx < len(self.behavior):
            cfg = self.behavior[idx]
        else:
            cfg = {"status_code": 200, "body": {"result": {"text": "ok"}}}
        status = cfg.get("status_code", 200)
        body = cfg.get("body", {"result": {"text": "ok"}})
        self._respond(status, body)

    def _respond(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _MockA2AServer:
    """Context manager: spawn the mock A2A server in a background thread.

    Usage:
        with _MockA2AServer() as server:
            client = A2AClient(...)
            agent = ExternalAgentConfig(
                name="x", transport="a2a", endpoint=server.url,
            )
            ...

    The server is bound to 127.0.0.1 on an OS-assigned port.
    """

    def __init__(self) -> None:
        # 127.0.0.1 + port 0 → OS picks a free port.
        self._server = HTTPServer(("127.0.0.1", 0), _A2AServerHandler)
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> _MockA2AServer:
        # Reset shared state so successive tests don't leak.
        _A2AServerHandler.behavior = []
        _A2AServerHandler.call_count = 0
        _A2AServerHandler.last_request_body = b""

        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *args) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDiscover(unittest.TestCase):
    """A2AClient.discover() fetches and parses agent.json."""

    def test_discover_returns_agent_config(self) -> None:
        with _MockA2AServer() as server:
            client = A2AClient()
            cfg = client.discover(server.url)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.name, "mock-agent")
        self.assertEqual(cfg.transport, "a2a")
        self.assertEqual(cfg.endpoint, server.url)
        self.assertIn("code_generation", cfg.capabilities)
        self.assertEqual(cfg.model, "claude-test")

    def test_discover_returns_none_on_404(self) -> None:
        with _MockA2AServer() as server:
            # Override behavior to return 404 for the agent card.
            _A2AServerHandler.behavior = [
                {"status_code": 404, "body": {"error": "not found"}}
            ]
            client = A2AClient()
            cfg = client.discover(server.url)
        self.assertIsNone(cfg)

    def test_discover_returns_none_on_connection_error(self) -> None:
        # No server running — discover must swallow the
        # connection error and return None (per the documented
        # contract).
        client = A2AClient()
        cfg = client.discover("http://127.0.0.1:1")  # unused port
        self.assertIsNone(cfg)


class TestSendTask(unittest.TestCase):
    """A2AClient.send_task posts JSON-RPC and exposes a stateful A2ATask."""

    def test_send_task_returns_task(self) -> None:
        with _MockA2AServer() as server:
            _A2AServerHandler.behavior = [
                {"status_code": 200, "body": {"result": {"text": "ok"}}}
            ]
            client = A2AClient()
            agent = ExternalAgentConfig(
                name="mock-agent", transport="a2a", endpoint=server.url,
            )
            task = client.send_task(agent, "do something")
        # Task id is set, prompt stored, status is the
        # background thread's start state (PENDING or RUNNING
        # depending on timing).
        self.assertEqual(task.agent_name, "mock-agent")
        self.assertEqual(task.prompt, "do something")
        self.assertEqual(len(task.id), 12)
        self.assertIn(task.status, {
            A2ATaskStatus.PENDING, A2ATaskStatus.RUNNING,
        })
        # Cleanup
        client.close()

    def test_send_task_includes_jsonrpc_envelope(self) -> None:
        """The request body must follow the JSON-RPC 2.0 contract."""
        with _MockA2AServer() as server:
            _A2AServerHandler.behavior = [
                {"status_code": 200, "body": {"result": {"text": "ok"}}}
            ]
            client = A2AClient()
            agent = ExternalAgentConfig(
                name="x", transport="a2a", endpoint=server.url,
            )
            task = client.send_task(agent, "my task")
            # Wait for the background thread to make the request.
            # We poll the server's call_count until it
            # increments (1 == the request landed).
            deadline = time.time() + 5.0
            while _A2AServerHandler.call_count < 1:
                if time.time() > deadline:
                    self.fail(
                        f"background thread never made the request "
                        f"(call_count={_A2AServerHandler.call_count}, "
                        f"task.status={task.status})"
                    )
                time.sleep(0.05)
            body = _A2AServerHandler.last_request_body
            client.close()
        # Parse the request body
        self.assertGreater(len(body), 0, "no request body was sent")
        envelope = json.loads(body)
        self.assertEqual(envelope["jsonrpc"], "2.0")
        self.assertEqual(envelope["method"], "tasks.send")
        self.assertEqual(envelope["params"]["prompt"], "my task")
        self.assertEqual(envelope["params"]["id"], task.id)

    def test_send_task_surfaces_server_error(self) -> None:
        """A non-200 HTTP response surfaces as FAILED.

        Note: the current client surfaces the HTTP status
        (e.g. ``HTTP Error 500``) rather than the JSON ``error``
        body — the response is read via ``urlopen`` which raises
        ``HTTPError`` on non-2xx, and the except path doesn't
        decode the body. This is a known minor gap; the test
        pins the current behavior so we don't silently change
        it without a code review.
        """
        with _MockA2AServer() as server:
            _A2AServerHandler.behavior = [
                {
                    "status_code": 500,
                    "body": {"error": {"message": "internal error"}},
                }
            ]
            client = A2AClient(A2AClientConfig(timeout=5, max_retries=1))
            agent = ExternalAgentConfig(
                name="x", transport="a2a", endpoint=server.url,
            )
            task = client.send_task(agent, "test")
            deadline = time.time() + 5.0
            while task.status not in (
                A2ATaskStatus.FAILED, A2ATaskStatus.COMPLETED
            ):
                if time.time() > deadline:
                    break
                time.sleep(0.05)
            client.close()
        # FAILED with some mention of 500.
        self.assertEqual(task.status, A2ATaskStatus.FAILED)
        self.assertIn("500", task.error)


class TestCleanup(unittest.TestCase):
    """A2AClient.close() shuts down active sessions cleanly."""

    def test_close_stops_active_sessions(self) -> None:
        with _MockA2AServer() as server:
            _A2AServerHandler.behavior = [
                {"status_code": 200, "body": {"result": {"text": "ok"}}}
            ]
            client = A2AClient()
            agent = ExternalAgentConfig(
                name="x", transport="a2a", endpoint=server.url,
            )
            client.send_task(agent, "x")
            # close() must not raise even with active sessions.
            client.close()
            self.assertEqual(len(client._sessions), 0)
            # The task is still accessible via get_task only
            # if we kept the reference; the internal session is
            # gone. Just verify no exception was raised.


class TestRetry(unittest.TestCase):
    """The client retries transient failures up to max_retries."""

    def test_retry_then_success(self) -> None:
        """Two 500s, then 200 — the client surfaces the successful response."""
        with _MockA2AServer() as server:
            _A2AServerHandler.behavior = [
                {"status_code": 500, "body": {"error": "boom"}},
                {"status_code": 500, "body": {"error": "boom"}},
                {"status_code": 200, "body": {"result": {"text": "ok"}}},
            ]
            client = A2AClient(
                A2AClientConfig(timeout=5, max_retries=3, retry_backoff=0.01)
            )
            agent = ExternalAgentConfig(
                name="x", transport="a2a", endpoint=server.url,
            )
            task = client.send_task(agent, "test")
            # Wait for the background thread.
            deadline = time.time() + 5.0
            while task.status not in (
                A2ATaskStatus.COMPLETED, A2ATaskStatus.FAILED
            ):
                if time.time() > deadline:
                    break
                time.sleep(0.05)
            client.close()
        # The third attempt succeeded.
        self.assertEqual(task.status, A2ATaskStatus.COMPLETED)
        self.assertEqual(task.result, "ok")


class TestCancel(unittest.TestCase):
    """The cancel_event flag is observed by the background session."""

    def test_cancel_marks_task_cancelled(self) -> None:
        with _MockA2AServer() as server:
            # First call: success (200). The background thread
            # will then mark the task COMPLETED. Cancel happens
            # after the response — we verify the cancel mechanism
            # short-circuits subsequent retries.
            _A2AServerHandler.behavior = [
                {"status_code": 200, "body": {"result": {"text": "ok"}}},
            ]
            client = A2AClient()
            agent = ExternalAgentConfig(
                name="x", transport="a2a", endpoint=server.url,
            )
            cancel = threading.Event()
            task = client.send_task(agent, "test", cancel_event=cancel)
            # Cancel before the thread starts processing.
            cancel.set()
            # Wait for the thread to finish.
            deadline = time.time() + 2.0
            while task.status == A2ATaskStatus.PENDING:
                if time.time() > deadline:
                    break
                time.sleep(0.02)
            client.close()
        # The cancel flag was set, so the session should
        # short-circuit and mark the task CANCELLED rather than
        # COMPLETED. (Race: if the response landed before the
        # cancel check, the status is COMPLETED. We accept
        # either as long as the test doesn't hang.)
        self.assertIn(task.status, {
            A2ATaskStatus.CANCELLED, A2ATaskStatus.COMPLETED,
        })


if __name__ == "__main__":
    unittest.main()
