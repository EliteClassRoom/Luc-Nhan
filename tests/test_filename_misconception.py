"""Filename misconception tests.

Asserts that:
  * `get_binary_info()` omits the on-disk file name from its output.
  * `build_system_prompt(binary_info=...)` always carries the
    ignore-filename rule even when the binary info is just "File: evil.exe".
  * The rule appears regardless of the ``hide_strings`` flag.
"""

from __future__ import annotations

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from tests.mocks.ida_mock import install_ida_mocks  # noqa: E402

install_ida_mocks()

from rikugan.agent.system_prompt import build_system_prompt  # noqa: E402
from rikugan.ida.tools.database import get_binary_info  # noqa: E402


FILENAME_RULE = (
    "Never infer the binary's purpose, family, or behavior from a file name"
)


class TestGetBinaryInfoOmitsFilename(unittest.TestCase):
    def test_filename_absent(self) -> None:
        result = get_binary_info()
        # IDA mock root file is "test_binary"; it must not appear.
        self.assertNotIn("test_binary", result)
        self.assertNotIn("File:", result)

    def test_metadata_present(self) -> None:
        result = get_binary_info()
        self.assertIn("Processor:", result)
        self.assertIn("Bits:", result)
        self.assertIn("Entry point:", result)
        self.assertIn("Functions:", result)


class TestPromptIgnoresFilename(unittest.TestCase):
    def test_filename_rule_with_evil_label(self) -> None:
        prompt = build_system_prompt(
            binary_info="File: evil.exe\nProcessor: x86",
        )
        self.assertIn(FILENAME_RULE, prompt)

    def test_filename_rule_without_binary_info(self) -> None:
        prompt = build_system_prompt()
        self.assertIn(FILENAME_RULE, prompt)

    def test_filename_rule_with_hide_strings(self) -> None:
        prompt = build_system_prompt(
            binary_info="File: evil.exe\nProcessor: x86",
            hide_strings=True,
        )
        self.assertIn(FILENAME_RULE, prompt)


if __name__ == "__main__":
    unittest.main()
