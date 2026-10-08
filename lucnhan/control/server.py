"""Local HTTP control server for headless IDA automation.

Uses stdlib ``http.server.ThreadingHTTPServer`` to avoid introducing
new dependencies inside the IDA process.  Binds to ``127.0.0.1`` by
default and requires a bearer token for all non-health endpoints.

Thread-safety: All shared state lives in ``ControlServerState`` and
is guarded by ``ControlServerState.lock``.  A ``threading.Condition``
notifies clients blocked in ``/events`` or the shutdown waiter.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from ..core.logging import get_logger, log_info, log_warning
from .protocol import (
    make_error_json,
    make_json_response,
)

logger = get_logger()

if TYPE_CHECKING:
    from ..ida.headless_controller import HeadlessSessionController

# ---------------------------------------------------------------------------
# Shared state — all mutable fields protected by a single lock
# ---------------------------------------------------------------------------

_EVENT_RING_SIZE = 4096
_MAX_REQUEST_BODY = 1_048_576  # 1 MiB — conservative for local-only use
# Upper bound on the number of queued events drained on shutdown.
# Guards against an unbounded drain if the runner keeps producing events
# (e.g. a stuck generator) — the ring buffer caps retained history anyway.
_DRAIN_MAX_ITERATIONS = 200


class RunState:
    """Per-run state, written by EventBroker and read by HTTP handlers.

    All reads/writes MUST be performed while holding the parent
    ``ControlServerState.lock``.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id: str = run_id
        # Monotonically increasing event sequence numbers
        self.event_buffer: deque[dict[str, Any]] = deque(maxlen=_EVENT_RING_SIZE)
        self.next_seq: int = 0
        self.finished: bool = False
        self.errors: list[str] = []
        self.final_text: str = ""
        self.exit_code: int = 0
        # Broker thread reference (for shutdown / stop)
        self.broker: EventBroker | None = None


class ControlServerState:
    """Lock-protected shared state for the control server.

    All fields except ``lock``, ``condition``, and ``shutdown_complete``
    are guarded by ``lock``.  ``condition`` is created from ``lock``
    and used to signal event availability and completion.
    """

    def __init__(self, token: str) -> None:
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.token: str = token
        self.shutting_down: bool = False
        self.shutdown_complete = threading.Event()
        # The active run (None when no run is in progress)
        self.run: RunState | None = None
        # Callback invoked on /shutdown to stop the HTTP server and
        # signal the bootstrap pump loop.  Set by ControlServer.start().
        self._shutdown_callback: ShutdownCallback | None = None

    @property
    def is_idle(self) -> bool:
        """True when no active (unfinished) run is in progress.

        A finished run is considered idle — ``/prompt`` may replace it
        and decision endpoints reject it via ``_resolve_run(required=True)``.
        """
        return self.run is None or self.run.finished


class ShutdownCallback:
    """Encapsulates the HTTP server shutdown routine.

    Exposes ``signalled`` (``threading.Event``) so the bootstrap pump
    loop can observe when ``/shutdown`` has been received and proceed
    with controller/idc shutdown.
    """

    def __init__(self, httpd_shutdown: Any) -> None:
        self._httpd_shutdown = httpd_shutdown
        self.signalled = threading.Event()

    def trigger(self) -> None:
        """Tell the HTTP server to stop accepting requests (non-blocking)."""
        if not self.signalled.is_set():
            self.signalled.set()
        try:
            self._httpd_shutdown()
        except Exception:
            logger.exception("EventBroker.stop: httpd shutdown failed")


# ---------------------------------------------------------------------------
# Event broker — drains the agent runner queue into RunState
# ---------------------------------------------------------------------------


class EventBroker:
    """Drains the agent runner queue into ``RunState`` inside a daemon thread.

    Uses ``ControlServerState.lock`` for all writes so that HTTP
    handlers always see a consistent snapshot.
    """

    def __init__(
        self,
        state: ControlServerState,
        controller: HeadlessSessionController,
    ) -> None:
        self._state = state
        self._controller = controller
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Begin draining events in a daemon thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the drain thread to exit and wait for it."""
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _drain(self) -> None:
        """Poll the runner, push events into RunState under the state lock.

        When the run finishes (runner gone and agent not running), the
        broker drains any remaining events, derives terminal status, and
        calls ``controller.on_agent_finished()`` exactly once.
        """
        from ..agent.turn import TurnEventType

        try:
            while not self._stop.is_set():
                runner = self._controller.get_runner()
                if runner is None:
                    if not self._controller.is_agent_running:
                        break
                    self._stop.wait(0.1)
                    continue

                event = runner.get_event(timeout=0.2)
                if event is None:
                    if not self._controller.is_agent_running:
                        break
                    continue

                ev_dict = event.to_dict()
                with self._state.lock:
                    run = self._state.run
                    if run is None:
                        break  # run was cancelled externally
                    ev_dict["seq"] = run.next_seq
                    run.next_seq += 1
                    run.event_buffer.append(ev_dict)

                    if event.type == TurnEventType.ERROR and event.error:
                        run.errors.append(event.error)
                    elif event.type == TurnEventType.TEXT_DONE and event.text:
                        run.final_text = event.text
                    elif event.type == TurnEventType.TEXT_DELTA and event.text:
                        pass  # deltas captured in the buffer
                    elif event.type in (
                        TurnEventType.REASONING_DELTA,
                        TurnEventType.RECOVERY_START,
                        TurnEventType.TOOL_CALL_DISCARDED,
                    ):
                        # GLM reasoning/recovery events: pass through without
                        # mutating final_text, errors, or exit_code.  Already
                        # captured in event_buffer via to_dict() above.
                        pass

                    self._state.condition.notify_all()
        finally:
            self._mark_finished()

    def _mark_finished(self) -> None:
        """Exactly-once completion: drain, derive exit code, finalise run.

        Drains runner events OUTSIDE the state lock (P2-1) so that
        long-polling /events clients aren't blocked while the broker
        pulls the last events from the runner queue.
        """
        from ..agent.turn import TurnEventType

        run_id: str | None = None
        with self._state.lock:
            run = self._state.run
            if run is None or run.finished:
                return  # already finalised — nothing to do
            run_id = run.run_id

        # Drain any remaining events OUTSIDE the state lock.
        drained: list[tuple[str, str]] = []
        runner = self._controller.get_runner()
        if runner is not None:
            for _ in range(_DRAIN_MAX_ITERATIONS):
                event = runner.get_event(timeout=0.02)
                if event is None:
                    break
                ev_dict = event.to_dict()
                # Verify the run hasn't been replaced while we drained.
                with self._state.lock:
                    if self._state.run is None or self._state.run.run_id != run_id:
                        return  # run was replaced — nothing to finalise
                    ev_dict["seq"] = self._state.run.next_seq
                    self._state.run.next_seq += 1
                    self._state.run.event_buffer.append(ev_dict)
                if event.type == TurnEventType.TEXT_DONE and event.text:
                    drained.append(("text_done", event.text))
                elif event.type == TurnEventType.ERROR and event.error:
                    drained.append(("error", event.error))

        # Re-acquire lock to finalise.
        with self._state.lock:
            run = self._state.run
            if run is None or run.finished or run.run_id != run_id:
                # Run was replaced or already finalised while we drained.
                self._state.condition.notify_all()
                return

            # Apply drained TEXT_DONE / ERROR outcomes.
            for kind, value in drained:
                if kind == "text_done":
                    run.final_text = value
                elif kind == "error":
                    run.errors.append(value)

            # Derive terminal exit_code (only if still at the default 0).
            if run.exit_code == 0:
                for ev in run.event_buffer:
                    t = ev.get("type", "")
                    if t == "error":
                        run.exit_code = 5
                    elif t == "cancelled":
                        run.exit_code = 6

            # Final text fallback: if no TEXT_DONE was seen, concatenate
            # TEXT_DELTA fragments.
            if not run.final_text:
                deltas: list[str] = []
                for ev in run.event_buffer:
                    if ev.get("type") == "text_delta" and ev.get("text"):
                        deltas.append(ev["text"])
                run.final_text = "".join(deltas).strip()

            run.finished = True
            self._state.condition.notify_all()

        # Notify controller lifecycle (outside lock to avoid re-entrancy).
        try:
            self._controller.on_agent_finished()
        except Exception as e:
            log_warning(f"on_agent_finished() raised: {e}")


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------


class ControlHandler(BaseHTTPRequestHandler):
    """Per-request handler — receives state/controller via ``__init__``."""

    def __init__(
        self,
        state: ControlServerState,
        controller: HeadlessSessionController,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self._state = state
        self._controller = controller
        super().__init__(*args, **kwargs)

    # -- helpers -----------------------------------------------------------

    @property
    def _token(self) -> str:
        return self._state.token

    def _is_authenticated(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return secrets.compare_digest(auth[7:], self._token)

    def _require_auth(self) -> bool:
        if not self._is_authenticated():
            self._send(*make_error_json("Unauthorized", status=401))
            return False
        return True

    def _send(self, status: int, headers: dict[str, str], body: str) -> None:
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body.encode("utf-8"))

    def _read_body(self) -> tuple[dict, tuple | None]:
        """Read and parse the request body as a JSON object.

        Returns ``(body, error)`` — exactly one is non-None / truthy.
        On parse / validation failure, the error tuple ``(status, headers, body)``
        has already been sent to the client and callers MUST return immediately
        without sending a second response.
        """
        length_raw = self.headers.get("Content-Length", "0")
        try:
            length = int(length_raw)
        except (ValueError, TypeError):
            err = make_error_json("Invalid Content-Length header", status=400)
            self._send(*err)
            return {}, err
        if length < 0:
            err = make_error_json("Invalid Content-Length header", status=400)
            self._send(*err)
            return {}, err
        if length == 0:
            return {}, None
        if length > _MAX_REQUEST_BODY:
            err = make_error_json(
                f"Request body too large ({length} bytes, max {_MAX_REQUEST_BODY})",
                status=413,
            )
            self._send(*err)
            return {}, err

        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            err = make_error_json("Malformed JSON body", status=400)
            self._send(*err)
            return {}, err
        if not isinstance(parsed, dict):
            err = make_error_json("Body must be a JSON object", status=400)
            self._send(*err)
            return {}, err
        return parsed, None

    def _path_parts(self) -> list[str]:
        parsed = urlparse(self.path)
        return [p for p in parsed.path.strip("/").split("/") if p]

    def _query_params(self) -> dict[str, list[str]]:
        parsed = urlparse(self.path)
        return parse_qs(parsed.query)

    # -- routing -----------------------------------------------------------

    def do_OPTIONS(self) -> None:
        """Return minimal OPTIONS without CORS headers by default (P2-4).

        CORS is opt-in only — the server does not set any
        ``Access-Control-Allow-*`` headers unless explicitly
        configured.
        """
        self._send(
            200,
            {"Content-Type": "text/plain"},
            "",
        )

    def do_GET(self) -> None:
        self._dispatch(
            {
                ("health",): (self._handle_health, False),
                ("tools",): (self._handle_tools, True),
                ("events",): (self._handle_events, True),
            }
        )

    def do_POST(self) -> None:
        self._dispatch(
            {
                ("prompt",): (self._handle_prompt, True),
                ("answer",): (self._handle_answer, True),
                ("tool-approval",): (self._handle_tool_approval, True),
                ("approval",): (self._handle_approval, True),
                ("cancel",): (self._handle_cancel, True),
                ("shutdown",): (self._handle_shutdown, True),
            }
        )

    def _dispatch(
        self,
        routes: dict[tuple[str, ...], tuple[Callable[[], None], bool]],
    ) -> None:
        """Resolve *routes* by path, gating auth-protected ones, else 404.

        Each entry maps a path-parts key to ``(handler, requires_auth)``.
        ``health`` is the only public route; every other endpoint goes
        through ``_require_auth`` before its handler runs.
        """
        key = tuple(self._path_parts())
        entry = routes.get(key)
        if entry is None:
            self._send(404, {"Content-Type": "text/plain"}, "Not Found")
            return
        handler, requires_auth = entry
        if requires_auth and not self._require_auth():
            return
        handler()

    # -- endpoint handlers ------------------------------------------------

    def _handle_health(self) -> None:
        """Public health check — no auth, no sensitive data."""
        with self._state.lock:
            running = self._state.run is not None and not self._state.run.finished
            shutting_down = self._state.shutting_down

        self._send(
            *make_json_response(
                {
                    "status": "ok",
                    "ready": not shutting_down,
                    "running": running,
                }
            )
        )

    def _handle_tools(self) -> None:
        registry = self._controller.tool_registry
        tools_list = registry.list_available_tools()
        body = {
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.to_json_schema(),
                }
                for t in tools_list
            ],
            "count": len(tools_list),
        }
        self._send(*make_json_response(body))

    def _handle_prompt(self) -> None:
        body, body_err = self._read_body()
        if body_err is not None:
            return  # error already sent by _read_body
        prompt = body.get("prompt", "")
        if not prompt:
            self._send(*make_error_json("Missing 'prompt' field"))
            return

        with self._state.lock:
            if self._state.shutting_down:
                self._send(*make_error_json("Server is shutting down", status=503))
                return
            if not self._state.is_idle:
                self._send(*make_error_json("A run is already in progress", status=409))
                return

            run_id = uuid.uuid4().hex[:8]
            run_state = RunState(run_id=run_id)
            # Publish the broker reference under the lock BEFORE publishing
            # run_state, so that concurrent /shutdown always sees the broker (P2-2).
            broker = EventBroker(self._state, self._controller)
            run_state.broker = broker
            self._state.run = run_state

        # Start the agent *outside* the lock.
        error = self._controller.start_agent(prompt)
        if error is not None:
            with self._state.lock:
                if self._state.run is not None and self._state.run.run_id == run_id:
                    self._state.run.finished = True
                    self._state.run.errors.append(error)
                    self._state.run.exit_code = 4
            self._send(*make_error_json(error, status=400))
            return

        # Start the event broker now that the runner exists — outside lock (P0-1).
        broker.start()

        self._send(
            *make_json_response(
                {
                    "run_id": run_id,
                    "status": "running",
                }
            )
        )

    # -- run-specific helpers (run_id validation) --------------------------

    def _resolve_run(
        self, run_id: str | None = None, required: bool = True
    ) -> tuple[RunState | None, tuple[int, dict, str] | None]:
        """Validate *run_id* against the active run.

        Returns ``(run, error)`` — exactly one is non-None.
        When *required* is True (default), a missing / mismatched /
        finished run is treated as an error (404 / 409).  When False,
        a missing run returns ``(None, None)`` without error.
        """
        with self._state.lock:
            run = self._state.run
            if run is None:
                if required:
                    return (
                        None,
                        make_error_json("No active run", status=404),
                    )
                return (None, None)

            if run_id is not None and run.run_id != run_id:
                if required:
                    return (
                        None,
                        make_error_json(
                            f"Run ID mismatch — requested {run_id}, active is {run.run_id}",
                            status=404,
                        ),
                    )
                return (None, None)

            if required and run.finished:
                return (
                    None,
                    make_error_json("Run has already finished", status=409),
                )

        return (run, None)

    # -- answer / approval / cancel ----------------------------------------

    def _handle_answer(self) -> None:
        body, body_err = self._read_body()
        if body_err is not None:
            return
        if "answer" not in body:
            self._send(*make_error_json("Missing 'answer' field", status=400))
            return
        if "run_id" not in body:
            self._send(*make_error_json("Missing 'run_id' field", status=400))
            return

        _run, err = self._resolve_run(run_id=body["run_id"])
        if err is not None:
            self._send(*err)
            return

        runner = self._controller.get_runner()
        if runner is None:
            self._send(*make_error_json("No active runner", status=404))
            return

        try:
            runner.agent_loop.submit_user_answer(body["answer"])
        except Exception:
            logger.exception("submit_user_answer RPC failed")
            self._send(*make_error_json("Failed to submit answer", status=500))
            return

        self._send(*make_json_response({"status": "ok"}))

    def _handle_tool_approval(self) -> None:
        body, body_err = self._read_body()
        if body_err is not None:
            return
        if "run_id" not in body:
            self._send(*make_error_json("Missing 'run_id' field", status=400))
            return

        # Validate decision payload before touching any agent state.
        has_decision = "decision" in body
        has_approved = "approved" in body

        if has_decision and has_approved:
            self._send(*make_error_json("Use 'decision' or 'approved', not both", status=400))
            return

        if has_decision:
            decision = body["decision"]
            if not isinstance(decision, str) or decision not in (
                "allow",
                "allow_all",
                "deny",
            ):
                self._send(
                    *make_error_json(
                        "Invalid 'decision' value. Use one of: allow, allow_all, deny",
                        status=400,
                    )
                )
                return
        elif has_approved:
            approved = body["approved"]
            if not isinstance(approved, bool):
                self._send(
                    *make_error_json(
                        "'approved' must be a boolean (true or false)",
                        status=400,
                    )
                )
                return
            decision = "allow" if approved else "deny"
        else:
            self._send(
                *make_error_json(
                    "Missing decision field. Send {'decision': 'allow|allow_all|deny'} or {'approved': true|false}",
                    status=400,
                )
            )
            return

        _run, err = self._resolve_run(run_id=body["run_id"])
        if err is not None:
            self._send(*err)
            return

        runner = self._controller.get_runner()
        if runner is None:
            self._send(*make_error_json("No active runner", status=404))
            return

        try:
            runner.agent_loop.submit_tool_approval(decision)
        except Exception:
            logger.exception("submit_tool_approval RPC failed")
            self._send(*make_error_json("Failed to submit tool approval", status=500))
            return

        self._send(*make_json_response({"status": "ok"}))

    def _handle_approval(self) -> None:
        body, body_err = self._read_body()
        if body_err is not None:
            return
        if "run_id" not in body:
            self._send(*make_error_json("Missing 'run_id' field", status=400))
            return

        # Validate decision payload before touching any agent state.
        has_decision = "decision" in body
        has_approved = "approved" in body

        if has_decision and has_approved:
            self._send(*make_error_json("Use 'decision' or 'approved', not both", status=400))
            return

        if has_decision:
            decision = body["decision"]
            if not isinstance(decision, str) or decision not in (
                "approve",
                "deny",
            ):
                self._send(
                    *make_error_json(
                        "Invalid 'decision' value. Use 'approve' or 'deny'.",
                        status=400,
                    )
                )
                return
        elif has_approved:
            approved = body["approved"]
            if not isinstance(approved, bool):
                self._send(
                    *make_error_json(
                        "'approved' must be a boolean (true or false)",
                        status=400,
                    )
                )
                return
            decision = "approve" if approved else "deny"
        else:
            self._send(
                *make_error_json(
                    "Missing decision field. Send {'decision': 'approve|deny'} or {'approved': true|false}",
                    status=400,
                )
            )
            return

        _run, err = self._resolve_run(run_id=body["run_id"])
        if err is not None:
            self._send(*err)
            return

        runner = self._controller.get_runner()
        if runner is None:
            self._send(*make_error_json("No active runner", status=404))
            return

        try:
            runner.agent_loop.submit_approval(decision)
        except Exception:
            logger.exception("submit_approval RPC failed")
            self._send(*make_error_json("Failed to submit approval", status=500))
            return

        self._send(*make_json_response({"status": "ok"}))

    def _handle_cancel(self) -> None:
        body, body_err = self._read_body()
        if body_err is not None:
            return
        if "run_id" not in body:
            self._send(*make_error_json("Missing 'run_id' field", status=400))
            return

        _run, err = self._resolve_run(run_id=body["run_id"])
        if err is not None:
            self._send(*err)
            return

        # run is not None here because _resolve_run with required=True
        # guarantees it when err is None.
        self._controller.cancel()
        self._send(*make_json_response({"status": "cancelled"}))

    # -- events ------------------------------------------------------------

    def _handle_events(self) -> None:
        params = self._query_params()
        index_raw = params.get("index", ["0"])[0]
        try:
            from_index = int(index_raw)
        except (ValueError, TypeError):
            self._send(*make_error_json("Invalid 'index' parameter", status=400))
            return
        if from_index < 0:
            self._send(*make_error_json("Index must be non-negative", status=400))
            return

        wait: bool = params.get("wait", [None])[0] is not None
        requested_run_id: str | None = params.get("run_id", [None])[0]

        # Validate run_id if provided.
        if requested_run_id is not None:
            run, err = self._resolve_run(run_id=requested_run_id, required=False)
            if err is not None:
                self._send(*err)
                return
            if run is None:
                self._send(
                    *make_json_response({"events": [], "index": 0, "finished": True, "exit_code": 0, "final_text": ""})
                )
                return

        # Long-poll: wait for new events or run completion.
        if wait:
            deadline = time.monotonic() + 30.0
            with self._state.lock:
                while time.monotonic() < deadline:
                    run_s = self._state.run
                    # Revalidate run_id after waking (P0-4).
                    if requested_run_id is not None:
                        if run_s is None or run_s.run_id != requested_run_id:
                            break
                    if run_s is not None and (run_s.next_seq > from_index or run_s.finished):
                        break
                    self._state.condition.wait(timeout=min(1.0, deadline - time.monotonic()))

        with self._state.lock:
            run_state = self._state.run

            # If a run_id was requested, only return events for that run (P0-4).
            if requested_run_id is not None:
                if run_state is None or run_state.run_id != requested_run_id:
                    self._send(
                        *make_json_response(
                            {
                                "events": [],
                                "index": from_index,
                                "finished": True,
                                "exit_code": 0,
                                "final_text": "",
                            }
                        )
                    )
                    return

            if run_state is None:
                self._send(
                    *make_json_response(
                        {
                            "events": [],
                            "index": 0,
                            "finished": True,
                            "exit_code": 0,
                            "final_text": "",
                        }
                    )
                )
                return

            events: list[dict[str, Any]] = []
            for ev in run_state.event_buffer:
                seq = ev.get("seq", 0)
                if seq >= from_index:
                    events.append(ev)

            next_index = run_state.next_seq
            finished = run_state.finished
            exit_code = run_state.exit_code
            final_text = run_state.final_text

        self._send(
            *make_json_response(
                {
                    "events": events,
                    "index": next_index,
                    "finished": finished,
                    "exit_code": exit_code,
                    "final_text": final_text,
                }
            )
        )

    # -- shutdown -----------------------------------------------------------

    def _handle_shutdown(self) -> None:
        """Cancel active run, stop broker/HTTP server, notify bootstrap."""
        with self._state.lock:
            if self._state.shutting_down:
                self._send(*make_json_response({"status": "already_shutting_down"}))
                return
            self._state.shutting_down = True
            run_state = self._state.run
            # Snapshot broker reference under lock, stop outside lock (P0-1).
            broker = run_state.broker if run_state is not None else None

        # Cancel any active run first.
        if run_state is not None:
            try:
                self._controller.cancel()
            except Exception:
                logger.exception("Shutdown: cancelling active run failed")

        # Send response while the HTTP socket is still available.
        self._send(*make_json_response({"status": "shutting_down"}))

        # Offload the actual shutdown work so the response can be
        # flushed before the HTTP server stops.
        def _do_shutdown() -> None:
            # Give the response a moment to flush.
            time.sleep(0.3)

            # Stop the event broker if active — OUTSIDE the state lock (P0-1).
            if broker is not None:
                broker.stop()

            # Signal the HTTP server to stop accepting connections.
            cb = self._state._shutdown_callback
            if cb is not None:
                cb.trigger()

            # Mark shutdown complete so the bootstrap pump loop
            # observes it and proceeds with controller/idc shutdown.
            self._state.shutdown_complete.set()

        threading.Thread(target=_do_shutdown, daemon=True).start()

    # -- suppress default request logging ----------------------------------

    def log_message(self, format: str, *args: Any) -> None:
        """Override default logging — never log auth headers."""
        pass


# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------


def _make_handler(
    state: ControlServerState,
    controller: HeadlessSessionController,
) -> type[ControlHandler]:
    """Return a handler class bound to the shared state."""

    class BoundHandler(ControlHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(state, controller, *args, **kwargs)

    return BoundHandler


# ---------------------------------------------------------------------------
# Control server
# ---------------------------------------------------------------------------


class ControlServer:
    """Local HTTP server for headless-mode control.

    Parameters
    ----------
    controller:
        The ``HeadlessSessionController`` that owns the tool registry
        and agent lifecycle.
    host:
        Bind address — must be ``127.0.0.1`` or ``localhost``.
        ``0.0.0.0`` is rejected at construction time.
    port:
        Port number; 0 for auto-assign.
    token:
        Bearer token; auto-generated if not provided.
    """

    def __init__(
        self,
        controller: HeadlessSessionController,
        host: str = "127.0.0.1",
        port: int = 0,
        token: str | None = None,
    ) -> None:
        _LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
        if host not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"Binding to {host!r} is not allowed. "
                f"The control server must be local-only. "
                f"Use 127.0.0.1 or localhost."
            )
        # Validate token format if provided.
        if token is not None:
            import re

            if not re.fullmatch(r"[0-9a-fA-F]{64}", token):
                raise ValueError("Server token must be 64 hex characters.")

        self._controller = controller
        self._host = host
        self._port = port
        self._token = token or secrets.token_hex(32)
        self._httpd: ThreadingHTTPServer | None = None
        self._state = ControlServerState(self._token)

    @property
    def host(self) -> str:
        return self._host

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self.port}"

    @property
    def port(self) -> int:
        return self._port

    @property
    def token(self) -> str:
        return self._token

    def write_ready_file(self, path: str) -> None:
        """Write ready info to a JSON file for the CLI launcher."""
        ready = {
            "url": self.url,
            "port": self.port,
            "token": self._token,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ready, f)

    def start(self) -> None:
        """Start the HTTP server on a background thread.

        Does NOT block — the caller (bootstrap loop) is responsible for
        pumping the dispatcher and detecting shutdown.
        """
        handler_cls = _make_handler(self._state, self._controller)
        self._httpd = ThreadingHTTPServer((self._host, self._port), handler_cls)
        self._port = self._httpd.server_port

        # Wire the shutdown callback so /shutdown can stop the server.
        self._state._shutdown_callback = ShutdownCallback(
            httpd_shutdown=self._httpd.shutdown,
        )

        log_info(f"Control server listening at {self.url}")

        t = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        t.start()

    @property
    def shutdown_callback(self) -> ShutdownCallback | None:
        """Return the shutdown callback so bootstrap can observe it.

        When ``shutdown_callback.signalled`` becomes True, the
        bootstrap pump loop should wind down and exit.
        """
        return self._state._shutdown_callback

    def shutdown(self) -> None:
        """Stop the HTTP server and clean up.

        Snapshot the broker reference under lock and call ``stop()``
        outside the lock to avoid deadlocking with the broker thread's
        ``_mark_finished()`` path (P0-1).
        """
        broker: EventBroker | None = None
        with self._state.lock:
            self._state.shutting_down = True
            run_state = self._state.run
            if run_state is not None and run_state.broker is not None:
                broker = run_state.broker

        if broker is not None:
            broker.stop()

        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

        self._state.shutdown_complete.set()

    @property
    def is_shutting_down(self) -> bool:
        with self._state.lock:
            return self._state.shutting_down
