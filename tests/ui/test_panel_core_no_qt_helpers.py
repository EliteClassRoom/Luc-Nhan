"""panel_core must not use the deleted qt_flags/qt_run helpers."""

from __future__ import annotations

import pathlib
import unittest

_PANEL_CORE = pathlib.Path("lucnhan/ui/panel_core.py")


class TestPanelCoreNoQtHelpers(unittest.TestCase):
    def test_no_qt_flags_or_qt_run_references(self) -> None:
        source = _PANEL_CORE.read_text(encoding="utf-8")
        self.assertNotIn("qt_flags", source, "qt_flags must be inlined as `|`")
        self.assertNotIn("qt_run", source, "qt_run must be inlined as `.exec()`")


if __name__ == "__main__":
    unittest.main()
