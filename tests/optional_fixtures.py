"""Optional-test-data path helpers.

Some integration tests need real binary fixtures (e.g. ``.idb`` /
``.i64`` samples) that are too large to check in.  Reading those
tests must NEVER make a default CI run depend on a local path
existing — if the path is absent the test must skip cleanly
rather than fail.

The convention is:

- Set the env var ``LUCNHAN_OPTIONAL_TEST_DATA`` to a directory
  containing the fixtures.  Production tests do NOT set this env
  var, so default CI runs skip.
- Tests that need a fixture call :func:`optional_test_data_path`
  with the fixture's filename.  The helper returns the absolute
  path when the env var is configured and the file exists, or
  ``None`` otherwise.  Tests then ``self.skipTest(...)`` or
  ``pytest.skip(...)`` when ``None`` is returned.

This module deliberately does NOT raise on the missing path —
that would let a test silently fail with a confusing
``FileNotFoundError`` instead of a clean skip.
"""

from __future__ import annotations

import os
from pathlib import Path

_ENV_VAR = "LUCNHAN_OPTIONAL_TEST_DATA"


def optional_test_data_path(filename: str) -> Path | None:
    """Return the absolute path to an optional binary fixture, or
    ``None`` when the path is not configured.

    Args:
        filename: The file name under the configured directory
            (e.g. ``"samples/notepad.i64"``).  May contain
            forward slashes for nested paths.

    Returns:
        The absolute :class:`Path` to the fixture when the
        ``LUCNHAN_OPTIONAL_TEST_DATA`` env var is set and the
        file exists; ``None`` otherwise.
    """
    base = os.environ.get(_ENV_VAR)
    if not base:
        return None
    candidate = Path(base) / filename
    return candidate if candidate.is_file() else None


def optional_test_data_dir() -> Path | None:
    """Return the configured optional-test-data directory, or
    ``None`` if it is not set or does not exist.

    Useful for tests that walk a directory rather than reading a
    single known file.
    """
    base = os.environ.get(_ENV_VAR)
    if not base:
        return None
    candidate = Path(base)
    return candidate if candidate.is_dir() else None


__all__ = ["optional_test_data_dir", "optional_test_data_path"]
