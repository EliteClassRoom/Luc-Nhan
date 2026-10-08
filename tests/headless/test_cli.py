"""Tests for the headless CLI parser and subcommand handlers."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()


class TestCLIParser(unittest.TestCase):
    """Tests for CLI parser construction and help output."""

    @classmethod
    def setUpClass(cls):
        from lucnhan.cli.headless import build_parser
        cls.parser = build_parser()

    def test_top_level_help_exits_zero(self):
        """--help exits 0."""
        with self.assertRaises(SystemExit) as cm:
            self.parser.parse_args(["--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_ask_help_exits_zero(self):
        """ask --help exits 0."""
        with self.assertRaises(SystemExit) as cm:
            self.parser.parse_args(["ask", "--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_serve_help_exits_zero(self):
        """serve --help exits 0."""
        with self.assertRaises(SystemExit) as cm:
            self.parser.parse_args(["serve", "--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_status_help_exits_zero(self):
        """status --help exits 0."""
        with self.assertRaises(SystemExit) as cm:
            self.parser.parse_args(["status", "--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_tools_help_exits_zero(self):
        """tools --help exits 0."""
        with self.assertRaises(SystemExit) as cm:
            self.parser.parse_args(["tools", "--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_events_help_exits_zero(self):
        """events --help exits 0."""
        with self.assertRaises(SystemExit) as cm:
            self.parser.parse_args(["events", "--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_prompt_help_exits_zero(self):
        """prompt --help exits 0."""
        with self.assertRaises(SystemExit) as cm:
            self.parser.parse_args(["prompt", "--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_answer_help_exits_zero(self):
        """answer --help exits 0."""
        with self.assertRaises(SystemExit) as cm:
            self.parser.parse_args(["answer", "--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_tool_approval_help_exits_zero(self):
        """tool-approval --help exits 0."""
        with self.assertRaises(SystemExit) as cm:
            self.parser.parse_args(["tool-approval", "--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_approval_help_exits_zero(self):
        """approval --help exits 0."""
        with self.assertRaises(SystemExit) as cm:
            self.parser.parse_args(["approval", "--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_cancel_help_exits_zero(self):
        """cancel --help exits 0."""
        with self.assertRaises(SystemExit) as cm:
            self.parser.parse_args(["cancel", "--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_shutdown_help_exits_zero(self):
        """shutdown --help exits 0."""
        with self.assertRaises(SystemExit) as cm:
            self.parser.parse_args(["shutdown", "--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_all_subcommands_have_callable_handlers(self):
        """Every subcommand must have a valid callable func."""
        for action in self.parser._actions:
            if hasattr(action, "choices") and action.choices:
                for name, subparser in action.choices.items():
                    func = subparser.get_default("func")
                    self.assertIsNotNone(
                        func,
                        f"Subcommand '{name}' has no func default",
                    )
                    self.assertTrue(
                        callable(func),
                        f"Subcommand '{name}' func is not callable: {func}",
                    )

    def test_no_undefined_symbols_at_parser_construction(self):
        """build_parser() must not raise NameError."""
        from lucnhan.cli.headless import build_parser
        # Re-build to test from scratch
        p = build_parser()
        self.assertIsNotNone(p)

    def test_ask_parses_required_args(self):
        """ask requires binary and prompt."""
        ns = self.parser.parse_args(["ask", "test.exe", "analyze this"])
        self.assertEqual(ns.binary, "test.exe")
        self.assertEqual(ns.prompt, "analyze this")
        self.assertEqual(ns.command, "ask")

    def test_serve_parses_required_args(self):
        """serve requires binary."""
        ns = self.parser.parse_args(["serve", "test.exe"])
        self.assertEqual(ns.binary, "test.exe")
        self.assertEqual(ns.command, "serve")
        self.assertEqual(ns.host, "127.0.0.1")

    def test_cancel_requires_run_id(self):
        """cancel requires --run-id."""
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["cancel", "--server", "http://127.0.0.1:8765", "--token", "t"])

    def test_cancel_with_run_id(self):
        """cancel accepts --run-id."""
        ns = self.parser.parse_args([
            "cancel", "--server", "http://127.0.0.1:8765",
            "--token", "t", "--run-id", "abc123"
        ])
        self.assertEqual(ns.run_id, "abc123")

    def test_answer_requires_run_id(self):
        """answer requires --run-id and --answer."""
        ns = self.parser.parse_args([
            "answer",
            "--answer", "my answer",
            "--server", "http://127.0.0.1:8765",
            "--token", "t", "--run-id", "abc123"
        ])
        self.assertEqual(ns.run_id, "abc123")
        self.assertEqual(ns.answer, "my answer")

    def test_prompt_accepts_text(self):
        """prompt accepts --prompt argument."""
        ns = self.parser.parse_args([
            "prompt",
            "--prompt", "test prompt",
            "--server", "http://127.0.0.1:8765",
            "--token", "t",
        ])
        self.assertEqual(ns.prompt, "test prompt")


class TestCLICommandHandlers(unittest.TestCase):
    """Integration-style tests for CLI command handlers."""

    def test_cmd_tools_wired_to_correct_handler(self):
        """tools subcommand uses cmd_tools, not cmd_tools_cmd."""
        from lucnhan.cli.headless import build_parser, cmd_tools
        p = build_parser()
        action = next(a for a in p._actions if a.dest == "command")
        tools_parser = action.choices.get("tools")
        self.assertIsNotNone(tools_parser)
        func = tools_parser.get_default("func")
        self.assertEqual(func, cmd_tools)

    def test_prompt_subcommand_wired_to_correct_handler(self):
        """prompt subcommand uses cmd_prompt_remote."""
        from lucnhan.cli.headless import build_parser, cmd_prompt_remote
        p = build_parser()
        action = next(a for a in p._actions if a.dest == "command")
        prompt_parser = action.choices.get("prompt")
        self.assertIsNotNone(prompt_parser)
        func = prompt_parser.get_default("func")
        self.assertEqual(func, cmd_prompt_remote)

    def test_answer_subcommand_wired_to_correct_handler(self):
        """answer subcommand uses cmd_answer."""
        from lucnhan.cli.headless import build_parser, cmd_answer
        p = build_parser()
        action = next(a for a in p._actions if a.dest == "command")
        answer_parser = action.choices.get("answer")
        self.assertIsNotNone(answer_parser)
        func = answer_parser.get_default("func")
        self.assertEqual(func, cmd_answer)

    def test_cmd_shutdown_has_no_stray_code(self):
        """cmd_shutdown should not contain stray _extract_last_json_line
        fragments after the HTTP POST."""
        import inspect

        from lucnhan.cli.headless import cmd_shutdown
        src = inspect.getsource(cmd_shutdown)
        # The function should NOT contain 'text.splitlines' (the stray fragment).
        self.assertNotIn("text.splitlines", src)

    def test_tool_approval_requires_decision_and_run_id(self):
        """tool-approval requires run-id and decision positional arg."""
        from lucnhan.cli.headless import build_parser
        p = build_parser()
        ns = p.parse_args([
            "tool-approval", "allow",
            "--run-id", "abc123",
            "--server", "http://127.0.0.1:8765",
            "--token", "t",
        ])
        self.assertEqual(ns.decision, "allow")
        self.assertEqual(ns.run_id, "abc123")

    def test_tool_approval_rejects_bad_decision(self):
        """tool-approval rejects invalid decision values."""
        from lucnhan.cli.headless import build_parser
        p = build_parser()
        with self.assertRaises(SystemExit):
            p.parse_args([
                "tool-approval", "bogus",
                "--run-id", "abc123",
            ])

    def test_approval_requires_decision_and_run_id(self):
        """approval requires run-id and decision positional arg."""
        from lucnhan.cli.headless import build_parser
        p = build_parser()
        ns = p.parse_args([
            "approval", "approve",
            "--run-id", "abc123",
            "--server", "http://127.0.0.1:8765",
            "--token", "t",
        ])
        self.assertEqual(ns.decision, "approve")
        self.assertEqual(ns.run_id, "abc123")

    def test_serve_ready_timeout_default(self):
        """serve defaults to a positive ready_timeout."""
        from lucnhan.cli.headless import build_parser
        p = build_parser()
        ns = p.parse_args(["serve", "test.exe"])
        self.assertEqual(ns.ready_timeout, 120)

    def test_serve_ready_timeout_cli(self):
        """serve --ready-timeout is parsed."""
        from lucnhan.cli.headless import build_parser
        p = build_parser()
        ns = p.parse_args(["serve", "test.exe", "--ready-timeout", "60"])
        self.assertEqual(ns.ready_timeout, 60)

    def test_cmd_tool_approval_normalizes_approve_to_allow(self):
        """cmd_tool_approval_remote normalizes 'approve' to 'allow' in the body."""
        from unittest import mock

        from lucnhan.cli.headless import cmd_tool_approval_remote

        args = mock.Mock()
        args.server = "http://127.0.0.1:8765"
        args.token = "test-token"
        args.run_id = "abc123"
        args.decision = "approve"  # CLI alias

        with mock.patch("lucnhan.cli.headless._http_post") as mock_post:
            mock_post.return_value = {"status": "ok"}
            cmd_tool_approval_remote(args)
            mock_post.assert_called_once()
            _, _, body = mock_post.call_args[0]
            self.assertEqual(body["decision"], "allow")
            self.assertEqual(body["run_id"], "abc123")

    def test_cmd_approval_sends_decision_field(self):
        """cmd_approval_remote sends 'decision' in the body."""
        from unittest import mock

        from lucnhan.cli.headless import cmd_approval_remote

        args = mock.Mock()
        args.server = "http://127.0.0.1:8765"
        args.token = "test-token"
        args.run_id = "abc123"
        args.decision = "approve"

        with mock.patch("lucnhan.cli.headless._http_post") as mock_post:
            mock_post.return_value = {"status": "ok"}
            cmd_approval_remote(args)
            mock_post.assert_called_once()
            _, _, body = mock_post.call_args[0]
            self.assertEqual(body["decision"], "approve")
            self.assertEqual(body["run_id"], "abc123")

    def test_cmd_prompt_uses_args_prompt(self):
        """cmd_prompt_remote uses args.prompt, not args.text."""
        from unittest import mock

        from lucnhan.cli.headless import cmd_prompt_remote

        args = mock.Mock()
        args.server = "http://127.0.0.1:8765"
        args.token = "test-token"
        args.prompt = "test prompt"

        with mock.patch("lucnhan.cli.headless._http_post") as mock_post:
            mock_post.return_value = {"run_id": "r1", "status": "running"}
            cmd_prompt_remote(args)
            mock_post.assert_called_once()
            _, _, body = mock_post.call_args[0]
            self.assertEqual(body["prompt"], "test prompt")

    def test_cmd_answer_uses_args_answer(self):
        """cmd_answer uses args.answer, not args.text."""
        from unittest import mock

        from lucnhan.cli.headless import cmd_answer

        args = mock.Mock()
        args.server = "http://127.0.0.1:8765"
        args.token = "test-token"
        args.run_id = "abc123"
        args.answer = "my answer"

        with mock.patch("lucnhan.cli.headless._http_post") as mock_post:
            mock_post.return_value = {"status": "ok"}
            cmd_answer(args)
            mock_post.assert_called_once()
            _, _, body = mock_post.call_args[0]
            self.assertEqual(body["answer"], "my answer")
            self.assertEqual(body["run_id"], "abc123")


if __name__ == "__main__":
    unittest.main()
