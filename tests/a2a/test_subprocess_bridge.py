"""Tests for lucnhan.agent.a2a.subprocess_bridge.SubprocessBridge.

Focus: argv injection prevention. The bridge builds subprocess commands
by concatenating LLM-controlled task text into argv. Without a hard
defense, a task starting with ``-`` is interpreted by the CLI as a flag
(``--settings '{"sandbox":false}'``, ``--add-dir /etc``, etc.), which
the subprocess layer cannot catch.

These tests follow TDD: they must FAIL on the current code and PASS
after the fix is applied.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.agent.a2a.subprocess_bridge import SubprocessBridge
from lucnhan.agent.a2a.types import ExternalAgentConfig


def _make_claude_agent() -> ExternalAgentConfig:
    return ExternalAgentConfig(
        name="claude",
        transport="subprocess",
        endpoint="claude",
        capabilities=["code_generation"],
    )


def _make_codex_agent() -> ExternalAgentConfig:
    return ExternalAgentConfig(
        name="codex",
        transport="subprocess",
        endpoint="codex",
        capabilities=["code_generation"],
    )


class TestSubprocessBridgeArgvInjection(unittest.TestCase):
    """Argv injection defense: LLM-supplied task must not be interpreted as flags."""

    def setUp(self):
        self.bridge = SubprocessBridge()

    # -- Positive cases (build_command must work for benign input) -----------

    def test_benign_task_builds_normal_command(self):
        cmd = self.bridge._build_command(_make_claude_agent(), "summarize the binary")
        self.assertIsNotNone(cmd)
        # Last element is the task itself (or the -- separator immediately before it)
        self.assertEqual(cmd[-1], "summarize the binary")

    def test_benign_task_with_spaces_builds_correctly(self):
        cmd = self.bridge._build_command(_make_claude_agent(), "what does main do?")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd[-1], "what does main do?")

    def test_codex_benign_task(self):
        cmd = self.bridge._build_command(_make_codex_agent(), "find the flag")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd[-1], "find the flag")

    # -- Argv injection defense ---------------------------------------------

    def test_task_starting_with_dash_rejected_for_claude(self):
        """A task like '--help' must be REJECTED, not passed to subprocess."""
        with self.assertRaises(ValueError) as cm:
            self.bridge._build_command(_make_claude_agent(), "--help")
        # Error message must mention the failure mode for debugging
        self.assertIn("starts with '-'", str(cm.exception))

    def test_task_starting_with_dash_rejected_for_codex(self):
        with self.assertRaises(ValueError):
            self.bridge._build_command(_make_codex_agent(), "--version")

    def test_malicious_settings_flag_rejected(self):
        """A task like --settings '{"sandbox":false}' must be rejected."""
        malicious = '--settings \'{"sandbox":false,"permissions":"full"}\''
        with self.assertRaises(ValueError):
            self.bridge._build_command(_make_claude_agent(), malicious)

    def test_malicious_add_dir_flag_rejected(self):
        """A task trying to escape sandbox via --add-dir /etc must be rejected."""
        malicious = "--add-dir /etc"
        with self.assertRaises(ValueError):
            self.bridge._build_command(_make_claude_agent(), malicious)

    def test_task_starting_with_short_dash_rejected(self):
        """A task starting with '-' but not '--' (e.g. '-h') must also be rejected."""
        with self.assertRaises(ValueError):
            self.bridge._build_command(_make_claude_agent(), "-h")

    def test_empty_task_rejected(self):
        with self.assertRaises(ValueError) as cm:
            self.bridge._build_command(_make_claude_agent(), "")
        self.assertIn("empty", str(cm.exception).lower())

    def test_benign_task_does_not_contain_dash_separator(self):
        """Benign tasks build the simple form: NO -- separator needed.

        The strict-reject strategy means benign tasks never need the
        separator. This is simpler than the old (separator-only) design.
        """
        cmd = self.bridge._build_command(_make_claude_agent(), "summarize the binary")
        self.assertIsNotNone(cmd)
        # The benign task has no leading dash, so no -- separator is required
        # (the strict-reject handles all dangerous cases upfront)
        self.assertNotIn("'", cmd)  # No shell-quoting artifacts
        self.assertEqual(cmd[-1], "summarize the binary")

    def test_known_unsafe_agent_name_returns_none(self):
        """Unknown agent names return None (caller handles as error)."""
        agent = ExternalAgentConfig(
            name="unknown_cli",
            transport="subprocess",
            endpoint="unknown",
            capabilities=[],
        )
        self.assertIsNone(self.bridge._build_command(agent, "benign task"))


class TestSubprocessBridgeTaskValidation(unittest.TestCase):
    """Strict task validation: reject tasks that look like CLI flag injection.

    Some CLIs may not respect '--' as end-of-options (older Codex
    versions, custom wrappers). We add belt-and-suspenders: explicitly
    reject tasks that look like a flag.
    """

    def setUp(self):
        self.bridge = SubprocessBridge()

    def test_task_starting_with_double_dash_raises_value_error(self):
        """Tasks starting with '--' must be rejected outright.

        Rationale: a legitimate user task should never begin with '--'.
        If it does, it's almost certainly a prompt-injection attempt.
        """
        with self.assertRaises(Exception) as cm:
            self.bridge._build_command(_make_claude_agent(), "--help")
        # Either ValueError or ToolError are acceptable
        self.assertIn(type(cm.exception).__name__, ("ValueError", "ToolError"))

    def test_task_starting_with_single_dash_raises_value_error(self):
        with self.assertRaises(Exception) as cm:
            self.bridge._build_command(_make_codex_agent(), "-h")
        self.assertIn(type(cm.exception).__name__, ("ValueError", "ToolError"))

    def test_benign_task_does_not_raise(self):
        # Should not raise
        cmd = self.bridge._build_command(_make_claude_agent(), "valid task")
        self.assertIsNotNone(cmd)

    def test_task_starting_with_non_dash_chars_passes(self):
        cmd = self.bridge._build_command(_make_claude_agent(), "do something")
        self.assertIsNotNone(cmd)



class TestCurateSubprocessEnv(unittest.TestCase):
    """The subprocess bridge must not leak the parent's full os.environ
    (which typically contains API keys and Luc Nhan-internal state) to
    the external CLI agent.
    """

    def setUp(self):
        from lucnhan.agent.a2a.subprocess_bridge import _curate_subprocess_env
        self._curate = _curate_subprocess_env

    def test_excludes_secret_api_keys(self):
        # Pretend the parent process has API keys in env
        with mock.patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "sk-ant-secret",
                "OPENAI_API_KEY": "sk-openai-secret",
                "GOOGLE_API_KEY": "google-secret",
                "LUCNHAN_AUTH_TOKEN": "lucnhan-internal",
            },
            clear=False,
        ):
            curated = self._curate({})
            assert "ANTHROPIC_API_KEY" not in curated
            assert "OPENAI_API_KEY" not in curated
            assert "GOOGLE_API_KEY" not in curated
            assert "LUCNHAN_AUTH_TOKEN" not in curated

    def test_passes_path_for_cli_discovery(self):
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False):
            curated = self._curate({})
            assert curated.get("PATH") == "/usr/bin:/bin"

    def test_passes_locale(self):
        with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8", "LC_ALL": "C"}, clear=False):
            curated = self._curate({})
            assert curated.get("LANG") == "en_US.UTF-8"
            assert curated.get("LC_ALL") == "C"

    def test_passes_proxy_settings(self):
        with mock.patch.dict(
            os.environ,
            {"HTTP_PROXY": "http://proxy:8080", "NO_PROXY": "localhost,127.0.0.1"},
            clear=False,
        ):
            curated = self._curate({})
            assert curated.get("HTTP_PROXY") == "http://proxy:8080"
            assert curated.get("NO_PROXY") == "localhost,127.0.0.1"

    def test_agent_env_overrides_curated(self):
        with mock.patch.dict(os.environ, {"PATH": "/parent/path"}, clear=False):
            curated = self._curate({"PATH": "/override/path"})
            assert curated["PATH"] == "/override/path"

    def test_empty_parent_env_yields_minimal_dict(self):
        # Only agent.env should be present when parent env is empty.
        with mock.patch.dict(os.environ, {}, clear=True):
            curated = self._curate({"MY_VAR": "hello"})
            # Only MY_VAR should be in curated (all passthrough keys absent).
            assert curated == {"MY_VAR": "hello"}

if __name__ == "__main__":
    unittest.main()
