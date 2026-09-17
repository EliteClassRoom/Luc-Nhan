"""Memory test directory conftest.

The presence of this file plus a top-level ``import pytest`` makes
pytest resolvable for Pyright (and any LSP that wires the Pyright
pytest plugin) in the sibling test files. Without it, Pyright cannot
see the ``pytest`` package on disk and flags every test module with
``Import "pytest" could not be resolved``.
"""

from __future__ import annotations

import pytest  # noqa: F401  -- presence triggers Pyright's pytest plugin
