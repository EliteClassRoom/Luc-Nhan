"""P1-4: Real bootstrap lifecycle tests for headless_bootstrap.py."""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()


class TestBootstrapCleanExit(unittest.TestCase):
    """_clean_exit_ida real-IDA-absent behaviour."""

    def test_clean_exit_writes_json_and_raises_system_exit(self):
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            from lucnhan.ida.headless_bootstrap import _clean_exit_ida

            with self.assertRaises(SystemExit):
                _clean_exit_ida(1, "test error message")

            output = mock_stdout.getvalue()
            self.assertIn("test error message", output)
            data = json.loads(output.strip())
            self.assertTrue(data["error"])
            self.assertEqual(data["exit_code"], 1)

    def test_clean_exit_no_message_no_json_output(self):
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            from lucnhan.ida.headless_bootstrap import _clean_exit_ida

            with self.assertRaises(SystemExit):
                _clean_exit_ida(0, "")

            output = mock_stdout.getvalue()
            self.assertEqual(output.strip(), "")

    def test_clean_exit_code_propagates(self):
        with patch("sys.stdout", new_callable=io.StringIO):
            from lucnhan.ida.headless_bootstrap import _clean_exit_ida

            with self.assertRaises(SystemExit) as ctx:
                _clean_exit_ida(7, "exit seven")
            self.assertEqual(ctx.exception.code, 7)


class TestBootstrapMainErrorPaths(unittest.TestCase):
    """main() error-path tests that work without complex mocking."""

    def _write_config(self, config: dict) -> str:
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json", prefix="bstrap_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f)
        return path

    def test_no_config_and_no_env_exits(self):
        """No LUCNHAN_HEADLESS_BOOTSTRAP and no mode env → _clean_exit_ida(2)."""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                from lucnhan.ida.headless_bootstrap import main
                main()
            self.assertEqual(ctx.exception.code, 2)

    def test_ask_mode_with_empty_prompt_exits(self):
        """ask mode with empty prompt triggers clean exit."""
        config = {
            "mode": "ask",
            "prompt": "",
            "wait_for_auto_analysis": True,
        }
        cfg_path = self._write_config(config)

        with patch.dict(os.environ, {"LUCNHAN_HEADLESS_BOOTSTRAP": cfg_path}):
            with self.assertRaises(SystemExit) as ctx:
                from lucnhan.ida.headless_bootstrap import main
                main()
            self.assertEqual(ctx.exception.code, 2)

        os.unlink(cfg_path)

    def test_ask_mode_requires_prompt_key(self):
        """ask mode with missing prompt field exits with error."""
        config = {
            "mode": "ask",
            "wait_for_auto_analysis": True,
        }
        cfg_path = self._write_config(config)

        with patch.dict(os.environ, {"LUCNHAN_HEADLESS_BOOTSTRAP": cfg_path}):
            with self.assertRaises(SystemExit) as ctx:
                from lucnhan.ida.headless_bootstrap import main
                main()
            self.assertEqual(ctx.exception.code, 2)

        os.unlink(cfg_path)


class TestBootstrapImports(unittest.TestCase):
    """P1-9: Verify headless_bootstrap.py is importable."""

    def test_import_headless_bootstrap(self):
        """headless_bootstrap is importable."""
        import lucnhan.ida.headless_bootstrap  # noqa: F401
