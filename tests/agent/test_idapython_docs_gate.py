"""Tests for the IDAPython docs-review gate (post-error variant).

Covers:

* ``LucNhanConfig`` round-trip of ``docs_review_mode`` + legacy
  ``require_ida_docs_for_complex_scripts`` migration.
* ``AgentLoop._review_failed_script`` — post-error reviewer: only
  spawned when execute_python raises an API-shaped exception AND
  ``docs_review_mode == 'on_error'`` AND the reviewer hasn't run yet
  this task. Augments the failed tool result with the reviewer's
  verdict + auto-injected reference docs.
* ``AgentLoop._build_reference_injection`` — pulls offline docs for
  up to 3 modules referenced in the failed script.
* ``AgentLoop._describe_tool_call`` for ``execute_python`` — empty
  description (the unified widget renders the code itself).

The agent-loop tests use lightweight fakes for the provider and tool
registry so they run without any IDA / Qt dependencies — they live in
the same ``tests/`` tree as other agent tests.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

from lucnhan.core.config import LucNhanConfig

# ---------------------------------------------------------------------------
# Config round-trip
# ---------------------------------------------------------------------------


class TestConfigField(unittest.TestCase):
    def test_default_is_on_error(self):
        cfg = LucNhanConfig()
        self.assertEqual(cfg.docs_review_mode, "on_error")

    def test_round_trip_through_dict(self):
        cfg = LucNhanConfig()
        cfg.docs_review_mode = "off"
        cfg.save = MagicMock()  # avoid disk side effects
        cfg.load = MagicMock()
        from dataclasses import asdict

        d = asdict(cfg)
        cfg2 = LucNhanConfig()
        cfg2.docs_review_mode = d["docs_review_mode"]
        self.assertEqual(cfg2.docs_review_mode, "off")

    def test_legacy_false_migrates_to_off(self):
        """Legacy config require_ida_docs_for_complex_scripts=False → off."""
        cfg = LucNhanConfig()
        # Simulate load() with legacy field present
        legacy_data = {"require_ida_docs_for_complex_scripts": False}
        cfg._apply_loaded_config(legacy_data)
        self.assertEqual(cfg.docs_review_mode, "off")

    def test_legacy_true_migrates_to_on_error(self):
        """Legacy config require_ida_docs_for_complex_scripts=True → on_error."""
        cfg = LucNhanConfig()
        legacy_data = {"require_ida_docs_for_complex_scripts": True}
        cfg._apply_loaded_config(legacy_data)
        self.assertEqual(cfg.docs_review_mode, "on_error")

    def test_legacy_missing_defaults_to_on_error(self):
        """No legacy field → on_error default."""
        cfg = LucNhanConfig()
        cfg._apply_loaded_config({})
        self.assertEqual(cfg.docs_review_mode, "on_error")

    def test_explicit_off_round_trips(self):
        cfg = LucNhanConfig()
        cfg._apply_loaded_config({"docs_review_mode": "off"})
        self.assertEqual(cfg.docs_review_mode, "off")

    def test_invalid_value_defaults_to_on_error(self):
        cfg = LucNhanConfig()
        cfg._apply_loaded_config({"docs_review_mode": "bogus"})
        self.assertEqual(cfg.docs_review_mode, "on_error")


# ---------------------------------------------------------------------------
# Agent-loop gate tests
# ---------------------------------------------------------------------------


@dataclass
class _FakeRunner:
    """Captures the kwargs passed to ``SubagentRunner.run_task``."""

    final_text: str = ""
    raise_on_run: Exception | None = None
    captured_kwargs: dict[str, Any] = field(default_factory=dict)
    captured_args: tuple = ()

    def run_task(self, *args, **kwargs):
        self.captured_args = args
        self.captured_kwargs = kwargs

        def _gen():
            if self.raise_on_run is not None:
                raise self.raise_on_run
            yield from ()

        gen = _gen()
        try:
            return_value = yield from gen
        except Exception:
            raise
        return return_value or self.final_text


class _FakeProvider:
    name = "test"
    model = "test-model"


class _FakeToolRegistry:
    def list_names(self):
        return []

    def list_available_tools(self):
        return []

    def to_provider_format(self):
        return []

    def get(self, name):
        return None

    def coerce_arguments_for(self, name, args):
        return dict(args)

    def execute(self, name, args):
        return ""


def _make_loop(*, gate_enabled: bool, runner: _FakeRunner | None = None):
    """Construct an AgentLoop through its real ``__init__``.

    ``__init__`` only assigns state (plus a ``ContextWindowManager``), so
    calling it keeps this harness in sync with the constructor instead of
    mirroring a hand-copied attribute list that silently omits whatever
    the next commit adds. Replaces ``SubagentRunner`` with a fake so the
    gate can run without LLM round-trips.

    *gate_enabled* maps to ``config.docs_review_mode``: True → ``"on_error"``,
    False → ``"off"`` (post-error semantics — no pre-execute gate anymore).
    """
    from lucnhan.agent.loop import AgentLoop
    from lucnhan.state.session import SessionState

    cfg = LucNhanConfig()
    cfg.docs_review_mode = "on_error" if gate_enabled else "off"

    loop = AgentLoop(
        provider=_FakeProvider(),
        tool_registry=_FakeToolRegistry(),
        config=cfg,
        session=SessionState(),
    )

    if runner is not None:
        # Monkey-patch the gate helper to use our fake runner.
        loop._SubagentRunner = lambda *a, **kw: runner
    return loop


class TestPostErrorReviewGate(unittest.TestCase):
    """Post-error docs-review gate: reviewer spawns only on API-shaped runtime error.

    The pre-execute ``_review_complex_idapython_script`` gate is gone.
    The new ``_review_failed_script`` runs AFTER execute_python raises
    an API-shaped exception (AttributeError/ImportError/NameError) and
    never blocks execution — the script already ran (and failed).
    ``docs_review_mode='off'`` disables the gate entirely.
    """

    def _complex_script(self) -> str:
        return (
            "import idaapi\n"
            "import idautils\n"
            "import ida_funcs\n"
            "for ea in idautils.Functions():\n"
            "    ida_funcs.get_func_name(ea)\n"
        )

    def _api_shaped_traceback(self) -> str:
        return (
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "AttributeError: module 'idaapi' has no attribute 'get_operands'\n"
        )

    def _logic_bug_traceback(self) -> str:
        return (
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "ValueError: invalid literal for int()\n"
        )

    def test_api_shaped_error_triggers_reviewer(self):
        from lucnhan.core.types import ToolCall
        from lucnhan.tools.traceback_classifier import classify_traceback

        loop = _make_loop(gate_enabled=True)
        runner = _FakeRunner(final_text="VERDICT: REWRITE_REQUIRED\nAPI_NOTES:\n- x")
        import lucnhan.agent.loop as loop_mod

        original = loop_mod.SubagentRunner
        loop_mod.SubagentRunner = lambda *a, **kw: runner
        try:
            tc = ToolCall(id="tc1", name="execute_python", arguments={"code": self._complex_script()})
            classification = classify_traceback(self._api_shaped_traceback(), self._complex_script())
            self.assertTrue(classification.is_api_shaped)

            gen = loop._review_failed_script(tc, self._api_shaped_traceback(), self._complex_script(), classification)
            result = _drain_str(gen)
            self.assertIn("AttributeError", result)
            self.assertIn("VERDICT: REWRITE_REQUIRED", result)
            self.assertTrue(loop._docs_reviewer_invoked)
        finally:
            loop_mod.SubagentRunner = original

    def test_logic_bug_error_skips_reviewer(self):
        """ValueError (logic bug) is NOT API-shaped → reviewer never spawned.

        The guard in ``_execute_single_tool`` gates on
        ``classification.is_api_shaped``; a non-API-shaped traceback
        (ValueError, TypeError, KeyError...) must never reach
        ``_review_failed_script``. This test reproduces the guard's
        decision logic and asserts the reviewer spy is never called.
        """
        from lucnhan.core.types import ToolCall
        from lucnhan.tools.traceback_classifier import classify_traceback

        loop = _make_loop(gate_enabled=True)
        runner = _FakeRunner(final_text="VERDICT: APPROVED")
        import lucnhan.agent.loop as loop_mod

        original_runner = loop_mod.SubagentRunner
        loop_mod.SubagentRunner = lambda *a, **kw: runner
        called = {"reviewer": False}
        original_review = loop._review_failed_script

        def _spy(*a, **kw):
            called["reviewer"] = True
            return iter(())

        loop._review_failed_script = _spy  # type: ignore[assignment]
        try:
            code = self._complex_script()
            tb = self._logic_bug_traceback()
            classification = classify_traceback(tb, code)
            self.assertFalse(
                classification.is_api_shaped,
                "ValueError must not be classified as API-shaped",
            )

            # Reproduce the guard from _execute_single_tool's except block:
            # the reviewer is only spawned when is_api_shaped is True.
            if classification.is_api_shaped and not loop._docs_reviewer_invoked:
                gen = loop._review_failed_script(
                    ToolCall(id="x", name="execute_python", arguments={"code": code}),
                    tb,
                    code,
                    classification,
                )
                _drain_str(gen)

            self.assertFalse(
                called["reviewer"],
                "reviewer should not be called for a logic-bug (non-API-shaped) error",
            )
            self.assertFalse(loop._docs_reviewer_invoked)
        finally:
            loop_mod.SubagentRunner = original_runner
            loop._review_failed_script = original_review  # type: ignore[assignment]

    def test_second_api_error_skips_reviewer(self):
        """Flag already set → guard in ``_execute_single_tool`` prevents a second spawn.

        Reproduces the ``not self._docs_reviewer_invoked`` arm of the guard
        (the flag is set by the first reviewer call). Asserts the reviewer
        spy is never invoked on a second API-shaped error in the same task.
        """
        from lucnhan.core.types import ToolCall
        from lucnhan.tools.traceback_classifier import classify_traceback

        loop = _make_loop(gate_enabled=True)
        loop._docs_reviewer_invoked = True  # already invoked this task
        runner = _FakeRunner(final_text="VERDICT: APPROVED")
        import lucnhan.agent.loop as loop_mod

        original_runner = loop_mod.SubagentRunner
        loop_mod.SubagentRunner = lambda *a, **kw: runner
        called = {"reviewer": False}
        original_review = loop._review_failed_script

        def _spy(*a, **kw):
            called["reviewer"] = True
            return iter(())

        loop._review_failed_script = _spy  # type: ignore[assignment]
        try:
            code = self._complex_script()
            tb = self._api_shaped_traceback()
            classification = classify_traceback(tb, code)
            self.assertTrue(classification.is_api_shaped)

            # Reproduce the guard: not self._docs_reviewer_invoked is False
            # here, so the reviewer branch must be skipped.
            if classification.is_api_shaped and not loop._docs_reviewer_invoked:
                gen = loop._review_failed_script(
                    ToolCall(id="x", name="execute_python", arguments={"code": code}),
                    tb,
                    code,
                    classification,
                )
                _drain_str(gen)

            self.assertFalse(
                called["reviewer"],
                "reviewer should not be called a second time within one task",
            )
            self.assertTrue(loop._docs_reviewer_invoked)
        finally:
            loop_mod.SubagentRunner = original_runner
            loop._review_failed_script = original_review  # type: ignore[assignment]

    def test_reviewer_crash_returns_traceback(self):
        """Reviewer crash → emit failed event, return traceback (không augment)."""
        from lucnhan.core.types import ToolCall
        from lucnhan.tools.traceback_classifier import classify_traceback

        loop = _make_loop(gate_enabled=True)
        runner = _FakeRunner(raise_on_run=RuntimeError("provider down"))
        import lucnhan.agent.loop as loop_mod

        original = loop_mod.SubagentRunner
        loop_mod.SubagentRunner = lambda *a, **kw: runner
        try:
            tc = ToolCall(id="tc3", name="execute_python", arguments={"code": self._complex_script()})
            classification = classify_traceback(self._api_shaped_traceback(), self._complex_script())

            gen = loop._review_failed_script(tc, self._api_shaped_traceback(), self._complex_script(), classification)
            result = _drain_str(gen)
            # Traceback vẫn có trong result (không augment reviewer verdict)
            self.assertIn("AttributeError", result)
        finally:
            loop_mod.SubagentRunner = original

    def test_reference_injection_pulls_module_docs(self):
        """_build_reference_injection trả RST content cho module có trong bundle."""
        loop = _make_loop(gate_enabled=True)
        # ida_typeinf có trong bundle (data/idapython-docs/ida_typeinf.rst.txt)
        result = loop._build_reference_injection(("ida_typeinf",))
        self.assertIn("ida_typeinf", result)

    def test_reference_injection_skips_missing_module(self):
        """Module không có trong bundle → skip, không crash."""
        loop = _make_loop(gate_enabled=True)
        result = loop._build_reference_injection(("ida_nonexistent_xyz",))
        # Không crash, trả chuỗi (có thể rỗng)
        self.assertIsInstance(result, str)

    def test_docs_review_mode_off_skips_reviewer(self):
        """docs_review_mode='off' → guard prevents reviewer spawn even on API-shaped error.

        Reproduces the ``docs_review_mode == 'on_error'`` arm of the guard
        in ``_execute_single_tool``'s except block. With mode "off", the
        reviewer branch is never entered even for a fully API-shaped
        traceback.
        """
        from lucnhan.core.types import ToolCall
        from lucnhan.tools.traceback_classifier import classify_traceback

        loop = _make_loop(gate_enabled=False)
        self.assertEqual(loop.config.docs_review_mode, "off")
        runner = _FakeRunner(final_text="VERDICT: APPROVED")
        import lucnhan.agent.loop as loop_mod

        original_runner = loop_mod.SubagentRunner
        loop_mod.SubagentRunner = lambda *a, **kw: runner
        called = {"reviewer": False}
        original_review = loop._review_failed_script

        def _spy(*a, **kw):
            called["reviewer"] = True
            return iter(())

        loop._review_failed_script = _spy  # type: ignore[assignment]
        try:
            code = self._complex_script()
            tb = self._api_shaped_traceback()
            classification = classify_traceback(tb, code)
            self.assertTrue(classification.is_api_shaped)

            # Reproduce the guard: docs_review_mode != "on_error" is False
            # here, so the reviewer branch is skipped entirely.
            if (
                getattr(loop.config, "docs_review_mode", "on_error") == "on_error"
                and classification.is_api_shaped
                and not loop._docs_reviewer_invoked
            ):
                gen = loop._review_failed_script(
                    ToolCall(id="x", name="execute_python", arguments={"code": code}),
                    tb,
                    code,
                    classification,
                )
                _drain_str(gen)

            self.assertFalse(
                called["reviewer"],
                "reviewer should not be called when docs_review_mode is off",
            )
            self.assertFalse(loop._docs_reviewer_invoked)
        finally:
            loop_mod.SubagentRunner = original_runner
            loop._review_failed_script = original_review  # type: ignore[assignment]

    def test_reviewed_state_emitted(self):
        """Post-error reviewer emit DOCS_GATE_STATUS running + reviewed."""
        from lucnhan.agent.turn import TurnEventType
        from lucnhan.core.types import ToolCall
        from lucnhan.tools.traceback_classifier import classify_traceback

        loop = _make_loop(gate_enabled=True)
        runner = _FakeRunner(final_text="VERDICT: APPROVED\nLooks good.")
        import lucnhan.agent.loop as loop_mod

        original = loop_mod.SubagentRunner
        loop_mod.SubagentRunner = lambda *a, **kw: runner
        try:
            tc = ToolCall(id="tc1", name="execute_python", arguments={"code": self._complex_script()})
            classification = classify_traceback(self._api_shaped_traceback(), self._complex_script())

            events: list = []
            gen = loop._review_failed_script(tc, self._api_shaped_traceback(), self._complex_script(), classification)
            for event in gen:
                events.append(event)

            gate_events = [e for e in events if e.type == TurnEventType.DOCS_GATE_STATUS]
            states = [e.metadata.get("docs_gate_state") for e in gate_events]
            self.assertIn("running", states)
            self.assertIn("reviewed", states)
        finally:
            loop_mod.SubagentRunner = original


# ---------------------------------------------------------------------------
# _describe_tool_call for execute_python (Task 3)
# ---------------------------------------------------------------------------


class TestDescribeToolCallExecutePython(unittest.TestCase):
    """The unified ExecutePythonWidget renders its own code block, so
    ``_describe_tool_call`` must return an empty string for
    ``execute_python`` (previously it duplicated the first line of code).
    Other mutating tools keep their human-readable descriptions."""

    def test_execute_python_returns_empty_description(self):
        from lucnhan import constants
        from lucnhan.agent.loop import AgentLoop

        desc = AgentLoop._describe_tool_call(
            constants.EXECUTE_PYTHON_TOOL_NAME,
            {"code": "import idautils\nprint(1)\n"},
        )
        self.assertEqual(desc, "")

    def test_execute_python_empty_args_returns_empty(self):
        from lucnhan import constants
        from lucnhan.agent.loop import AgentLoop

        desc = AgentLoop._describe_tool_call(constants.EXECUTE_PYTHON_TOOL_NAME, {})
        self.assertEqual(desc, "")

    def test_other_mutating_tool_still_described(self):
        from lucnhan.agent.loop import AgentLoop

        desc = AgentLoop._describe_tool_call(
            "rename_function",
            {"old_name": "sub_1000", "new_name": "process_data"},
        )
        self.assertIn("sub_1000", desc)
        self.assertIn("process_data", desc)


# ---------------------------------------------------------------------------
# Integration: drive the REAL _execute_single_tool except block
# ---------------------------------------------------------------------------


class TestExecuteSingleToolIntegration(unittest.TestCase):
    """End-to-end tests that drive ``_execute_single_tool`` as a generator.

    Unlike the unit-style tests in ``TestPostErrorReviewGate`` (which
    reproduce the guard's ``if`` condition by hand), these tests feed a
    real ``ToolCall`` through the method and assert on observable side
    effects (the reviewer's verdict in the augmented result, the
    ``_docs_reviewer_invoked`` flag, and the traceback scope in the
    error result).

    The exception path is exercised by monkey-patching
    ``loop.tools.execute_coerced`` to raise an ``AttributeError`` shaped
    like an IDA API hallucination. The static validator
    (``validate_idapython``) is bypassed by using an API name not in
    its block list (``idautils.NonExistentThing``).
    """

    _API_HALLUCINATED_CODE = "import idautils\nidautils.NonExistentThing()\n"

    def _set_registry_to_raise(self, loop, exc):
        """Replace ``tools.execute_coerced`` with a raising stub."""

        def _raise(name, args, **kwargs):
            raise exc

        loop.tools.execute_coerced = _raise  # type: ignore[attr-defined]

    def _drain_result(self, gen):
        """Drain a generator that returns a ``ToolResult``."""
        while True:
            try:
                next(gen)
            except StopIteration as stop:
                return stop.value

    def test_execute_single_tool_spawns_reviewer_on_api_error(self):
        """Integration: REAL ``_execute_single_tool`` raises AttributeError
        on execute_python → docs-reviewer IS spawned via the real except
        block → tool result is augmented with the verdict.

        Catches regressions where someone:
        * removes the ``tc.name == EXECUTE_PYTHON_TOOL_NAME`` guard,
        * drops the ``not self._docs_reviewer_invoked`` check,
        * or breaks the call into ``_review_failed_script`` entirely.
        """
        from lucnhan.core.types import ToolCall

        loop = _make_loop(gate_enabled=True)
        # Bypass the approval prompt — we're testing the except block,
        # not the approval gate. _always_allow_scripts short-circuits
        # _wait_for_approval before it touches the queue.
        loop._always_allow_scripts = True

        api_error = AttributeError("module 'idautils' has no attribute 'NonExistentThing'")
        self._set_registry_to_raise(loop, api_error)

        runner = _FakeRunner(final_text="VERDICT: REWRITE_REQUIRED\nAPI_NOTES:\n- NonExistentThing is hallucinated")
        import lucnhan.agent.loop as loop_mod

        original_runner = loop_mod.SubagentRunner
        loop_mod.SubagentRunner = lambda *a, **kw: runner
        try:
            tc = ToolCall(
                id="tc-int",
                name="execute_python",
                arguments={"code": self._API_HALLUCINATED_CODE},
            )
            gen = loop._execute_single_tool(tc)
            tr = self._drain_result(gen)

            # The reviewer should have been invoked via the real except
            # block (not a hand-copied guard).
            self.assertTrue(
                loop._docs_reviewer_invoked,
                "reviewer subagent should have been spawned via the real _execute_single_tool except block",
            )
            # Augmented result carries the reviewer's verdict.
            self.assertIn("REWRITE_REQUIRED", tr.content)
            self.assertIn("NonExistentThing", tr.content)
            self.assertTrue(tr.is_error)
        finally:
            loop_mod.SubagentRunner = original_runner

    def test_execute_single_tool_skips_reviewer_on_non_execute_python(self):
        """Regression for Finding 1: a non-execute_python tool that raises
        must NOT include the full traceback in the result (would leak
        internal paths/line numbers into the LLM context).

        Drives the real ``_execute_single_tool`` end-to-end with a
        raising stub. Asserts:
        * The reviewer is NOT spawned (``_docs_reviewer_invoked`` stays
          False; the guard's ``tc.name == EXECUTE_PYTHON_TOOL_NAME``
          check rejects the branch).
        * The tool result is a one-liner without ``Traceback`` (the
          full traceback stays in the server log only).
        """
        from lucnhan.core.types import ToolCall

        loop = _make_loop(gate_enabled=True)
        loop._always_allow_scripts = True

        api_error = RuntimeError("simulated tool failure")
        self._set_registry_to_raise(loop, api_error)

        # Patch SubagentRunner anyway — must NOT be called.
        called = {"count": 0}

        class _CountingRunner(_FakeRunner):
            def __init__(self):
                super().__init__(final_text="VERDICT: APPROVED")

            def run_task(self, *args, **kwargs):
                called["count"] += 1
                return super().run_task(*args, **kwargs)

        import lucnhan.agent.loop as loop_mod

        original_runner = loop_mod.SubagentRunner
        loop_mod.SubagentRunner = lambda *a, **kw: _CountingRunner()
        try:
            tc = ToolCall(
                id="tc-leak",
                name="some_other_tool",
                arguments={"foo": "bar"},
            )
            gen = loop._execute_single_tool(tc)
            tr = self._drain_result(gen)

            # Reviewer must not be spawned for non-execute_python tools.
            self.assertFalse(loop._docs_reviewer_invoked)
            self.assertEqual(called["count"], 0)
            # Result must be the one-liner — no traceback leak.
            self.assertNotIn("Traceback", tr.content)
            self.assertIn("Unexpected error", tr.content)
            self.assertIn("simulated tool failure", tr.content)
            self.assertTrue(tr.is_error)
        finally:
            loop_mod.SubagentRunner = original_runner


def _drain_str(gen):
    """Drain a generator that returns a ``str``.

    ``list(gen)`` exhausts the generator and loses the return value, so
    we drive ``next()`` in a loop and capture the value from
    ``StopIteration.value``.
    """
    while True:
        try:
            next(gen)
        except StopIteration as stop:
            return stop.value


if __name__ == "__main__":
    unittest.main()
