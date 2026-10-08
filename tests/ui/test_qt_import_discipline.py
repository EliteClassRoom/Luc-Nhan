"""Regression test: UI and plugin modules must NOT import PySide6/PyQt5 directly.

Background
----------
IDA 9.x 32-bit hosts still ship Qt5. Importing PySide6 in such an environment
loads Qt6 DLLs into a Qt5 process and triggers ``FAST_FAIL_FATAL_APP_EXIT``
inside the Qt widget constructor (see ``rikugan/ui/qt_compat.py``).

Every Qt import in non-test source MUST go through ``rikugan/ui/qt_compat.py``.
This test enforces that rule by scanning all ``.py`` files under
``rikugan/ui/`` and the ``rikugan_plugin.py`` entry point for direct
``from PySide6`` / ``import PySide6`` / ``from PyQt5`` / ``import PyQt5``
statements, ignoring the compatibility layer itself and the tests directory.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
UI_DIR = REPO_ROOT / "rikugan" / "ui"
PLUGIN_FILE = REPO_ROOT / "rikugan_plugin.py"
TESTS_DIR = REPO_ROOT / "rikugan" / "tests"

# Top-level forms we forbid (excluding the compatibility layer).
_FORBIDDEN_RE = re.compile(r"^\s*(?:from\s+(?:PySide6|PyQt5)(?:\.\w+)?\s+import\b|import\s+(?:PySide6|PyQt5)\b)")

# Allow-list paths — modules that legitimately import the raw binding.
_ALLOWED = {
    UI_DIR / "qt_compat.py",
}


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    if PLUGIN_FILE.exists():
        files.append(PLUGIN_FILE)
    if UI_DIR.exists():
        for path in UI_DIR.rglob("*.py"):
            if path in _ALLOWED:
                continue
            files.append(path)
    return files


def _scan_violations(path: Path) -> list[tuple[int, str]]:
    """Return (line_no, line) tuples for any direct PySide6/PyQt5 import."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    hits: list[tuple[int, str]] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.lstrip()
        # Skip pure comments / docstrings: only flag actual import statements.
        if stripped.startswith("#"):
            continue
        # ``from x import …`` may wrap across lines but the import keyword
        # itself is on the first physical line, so scanning per-line is safe.
        if _FORBIDDEN_RE.match(raw_line):
            hits.append((lineno, raw_line.rstrip()))
    return hits


@pytest.mark.parametrize("path", _iter_python_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_direct_qt_imports(path: Path) -> None:
    """No file outside qt_compat.py/tests may import PySide6/PyQt5 directly."""
    violations = _scan_violations(path)
    assert not violations, (
        f"{path.relative_to(REPO_ROOT)} imports PySide6/PyQt5 directly. "
        "Use `rikugan.ui.qt_compat` instead. Violations:\n"
        + "\n".join(f"  line {ln}: {line}" for ln, line in violations)
    )
