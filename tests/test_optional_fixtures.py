"""Tests for the optional-test-data gating helper.

These tests prove that the gating contract holds in three
configurations:

1. Env var unset — every helper returns ``None`` (default CI run).
2. Env var set to a directory that does not exist — returns
   ``None`` so tests skip cleanly.
3. Env var set to a directory that does contain a fixture file —
   the helper returns the absolute path.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tests.optional_fixtures import optional_test_data_dir, optional_test_data_path

_ENV_VAR = "LUCNHAN_OPTIONAL_TEST_DATA"


class TestOptionalTestDataGating(unittest.TestCase):
    def setUp(self) -> None:
        # Snapshot the env var so each test can mutate it freely.
        self._old = os.environ.pop(_ENV_VAR, None)

    def tearDown(self) -> None:
        # Restore the original env state.
        if self._old is not None:
            os.environ[_ENV_VAR] = self._old
        else:
            os.environ.pop(_ENV_VAR, None)

    def test_unset_returns_none(self) -> None:
        """Default CI run with no env var set — every helper
        returns ``None`` so tests must skip cleanly."""
        os.environ.pop(_ENV_VAR, None)
        self.assertIsNone(optional_test_data_path("any.i64"))
        self.assertIsNone(optional_test_data_dir())

    def test_set_to_missing_dir_returns_none(self) -> None:
        """Env var set to a non-existent path must return
        ``None`` so the test skips instead of failing with
        ``FileNotFoundError``."""
        os.environ[_ENV_VAR] = "/this/path/should/never/exist"
        self.assertIsNone(optional_test_data_dir())
        self.assertIsNone(optional_test_data_path("any.i64"))

    def test_set_to_existing_dir_returns_path(self) -> None:
        """Env var set to a real directory with a fixture file:
        the helper returns the absolute path."""
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "sample.i64"
            fixture.write_bytes(b"")
            os.environ[_ENV_VAR] = tmp
            self.assertEqual(optional_test_data_dir(), Path(tmp))
            self.assertEqual(optional_test_data_path("sample.i64"), fixture)

    def test_set_to_dir_missing_target_file_returns_none(self) -> None:
        """Env var set to a real directory but the requested
        fixture file is absent — the helper returns ``None``
        for the file (and the directory) so the test skips."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[_ENV_VAR] = tmp
            self.assertEqual(optional_test_data_dir(), Path(tmp))
            # A file that does not exist must still return None.
            self.assertIsNone(optional_test_data_path("missing.i64"))


if __name__ == "__main__":
    unittest.main()
