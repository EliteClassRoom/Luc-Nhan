"""Multi-tab parallel agent: SessionControllerBase runner-dict behavior.

These tests exercise the multi-runner refactor (``self._runner`` →
``self._runners: dict``) without starting real agents. They stub the
provider so ``start_agent`` can build a loop cheaply, then assert the
tab-scoped lifecycle: per-tab runners, switch-doesn't-cancel, per-tab
cancellation, concurrency cap, and shutdown-cancels-all.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.core.config import RikuganConfig  # noqa: E402
from rikugan.ida.ui.session_controller import IdaSessionController  # noqa: E402
from rikugan.state.history import SessionHistory  # noqa: E402


class _StubProvider:
    """Minimal provider so AgentLoop builds without network/keys."""

    def __init__(self, *a, **kw) -> None:
        pass

    def stream(self, messages, tools=None, **kw):
        yield {"type": "message_stop"}

    def resolve_auth(self):
        return "stub"

    name = "stub"
    model = "stub-model"


class TestMultiTabRunner(unittest.TestCase):
    def setUp(self):
        self.cfg = RikuganConfig()
        self.cfg._config_dir = tempfile.mkdtemp()
        self.cfg.parallel_agent_enabled = True
        self.cfg.parallel_agent_max_concurrent = 3
        # Patch provider creation so start_agent never needs a real key.
        _patcher = patch.object(IdaSessionController, "_create_provider", return_value=_StubProvider())
        _patcher.start()
        self.addCleanup(_patcher.stop)
        self.ctrl = IdaSessionController(self.cfg)
        # Runtime init runs in a background thread; wait for it so
        # ensure_advanced_tools_ready / skill discovery settle.
        self.ctrl._runtime_init_done.wait(timeout=5.0)

    def tearDown(self):
        SessionHistory.flush_saves()
        self.ctrl.shutdown()
        shutil.rmtree(self.cfg._config_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Per-tab runner dict
    # ------------------------------------------------------------------

    def test_single_runner_dict_no_runner_initially(self):
        self.assertEqual(dict(self.ctrl.iter_runners()), {})

    def test_is_agent_running_false_when_no_runners(self):
        self.assertFalse(self.ctrl.is_agent_running)

    def test_get_runner_returns_none_for_unknown_tab(self):
        self.assertIsNone(self.ctrl.get_runner("nope"))

    def test_get_runner_defaults_to_active_tab(self):
        # Zero-arg form targets the active tab (headless compat, B1).
        self.assertIsNone(self.ctrl.get_runner())
        # And equals the active-tab explicit form.
        self.assertIsNone(self.ctrl.get_runner(self.ctrl.active_tab_id))

    # ------------------------------------------------------------------
    # Pending message queue is per-tab
    # ------------------------------------------------------------------

    def test_queue_message_is_per_tab(self):
        tab_a = self.ctrl.active_tab_id
        tab_b = self.ctrl.create_tab()
        self.ctrl.switch_tab(tab_a)
        self.ctrl.queue_message("a1")
        self.ctrl.switch_tab(tab_b)
        self.ctrl.queue_message("b1")

        # Draining tab B must not return tab A's message.
        self.assertEqual(self.ctrl.pop_queued_message(tab_b), "b1")
        self.assertEqual(self.ctrl.pop_queued_message(tab_a), "a1")
        self.assertIsNone(self.ctrl.pop_queued_message(tab_a))
        self.assertIsNone(self.ctrl.pop_queued_message(tab_b))

    def test_cancel_only_clears_target_tab_queue(self):
        tab_a = self.ctrl.active_tab_id
        tab_b = self.ctrl.create_tab()
        self.ctrl.queue_message_for_tab(tab_a, "a1")
        self.ctrl.queue_message_for_tab(tab_b, "b1")

        self.ctrl.cancel(tab_b)
        # tab A untouched, tab B cleared
        self.assertEqual(self.ctrl.pop_queued_message(tab_a), "a1")
        self.assertIsNone(self.ctrl.pop_queued_message(tab_b))

    # ------------------------------------------------------------------
    # switch_tab no longer cancels a running agent
    # ------------------------------------------------------------------

    def test_switch_tab_does_not_cancel_running_marker(self):
        # We cannot start a real agent cheaply, but the contract we care
        # about is structural: switch_tab must NOT call cancel. Inject a
        # fake runner that records cancels, switch tabs, then assert no
        # cancel happened during the switch (tearDown's shutdown cancel
        # is harmless — it just appends to the list).
        tab_b = self.ctrl.create_tab()
        cancelled: list[str] = []

        class _FakeRunner:
            def __init__(self) -> None:
                class _Loop:
                    is_running = True

                self.agent_loop = _Loop()

            def cancel(self) -> None:
                cancelled.append("cancelled")

        self.ctrl._runners[tab_b] = _FakeRunner()  # type: ignore[assignment]
        self.ctrl.switch_tab(tab_b)
        # switch_tab must not have cancelled the runner.
        self.assertEqual(cancelled, [])
        # Runner still present → switch did not tear it down either.
        self.assertIn(tab_b, dict(self.ctrl.iter_runners()))

    # ------------------------------------------------------------------
    # Concurrency cap
    # ------------------------------------------------------------------

    def test_has_free_slot_until_cap_reached(self):
        self.assertEqual(self.ctrl._max_concurrent_agents, 3)
        self.assertTrue(self.ctrl.has_free_slot())

        class _FakeRunner:
            def __init__(self) -> None:
                class _Loop:
                    is_running = True

                self.agent_loop = _Loop()

            def cancel(self) -> None:
                pass

        for i in range(3):
            tid = f"fake-tab-{i}"
            self.ctrl._sessions[tid] = self.ctrl.session  # type: ignore[index]
            self.ctrl._runners[tid] = _FakeRunner()  # type: ignore[assignment]
        # Cap reached → no free slot.
        self.assertFalse(self.ctrl.has_free_slot())

    # ------------------------------------------------------------------
    # cancel_all / shutdown cancel every runner
    # ------------------------------------------------------------------

    def test_cancel_all_clears_every_runner(self):
        cancelled: list[str] = []

        def _make_fake(name: str):
            class _FakeRunner:
                def __init__(self) -> None:
                    class _Loop:
                        is_running = True

                    self.agent_loop = _Loop()

                def cancel(self) -> None:
                    cancelled.append(name)

            return _FakeRunner()

        for i in range(3):
            tid = f"tab-{i}"
            self.ctrl._sessions[tid] = self.ctrl.session  # type: ignore[index]
            self.ctrl._runners[tid] = _make_fake(tid)  # type: ignore[assignment]

        self.ctrl.cancel_all()
        self.assertEqual(sorted(cancelled), ["tab-0", "tab-1", "tab-2"])

    def test_shutdown_cancels_all_runners(self):
        cancelled = []

        class _FakeRunner:
            def __init__(self) -> None:
                class _Loop:
                    is_running = True

                self.agent_loop = _Loop()

            def cancel(self) -> None:
                cancelled.append(True)

        self.ctrl._runners["tab-x"] = _FakeRunner()  # type: ignore[assignment]
        self.ctrl.shutdown()
        self.assertTrue(cancelled)


if __name__ == "__main__":
    unittest.main()
