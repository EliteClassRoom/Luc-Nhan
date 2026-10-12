"""Check readable markdown hierarchy in the real Qt rich-text engine."""

from __future__ import annotations

import time
import unittest

from tests.qt_real import real_qt_available

if not real_qt_available():
    raise unittest.SkipTest("real PySide6 unavailable")

from lucnhan.ui.markdown import _legacy_md_to_html, md_to_html
from lucnhan.ui.qt_compat import QApplication, QTextEdit
from lucnhan.ui.theme.manager import ThemeManager
from lucnhan.ui.theme.tokens import ThemeMode, hex_luminance


class TestMarkdownColors(unittest.TestCase):
    def test_semantic_colors_remain_readable_after_theme_switches(self) -> None:
        app = QApplication.instance() or QApplication([])
        manager = ThemeManager.instance()
        previous_mode = manager.mode
        editor = QTextEdit()
        markdown = "# Report\n\nBody with `address` and [reference](https://example.invalid).\n\n```\ncode_line\n```"
        try:
            for mode in (ThemeMode.IDA_NATIVE, ThemeMode.LIGHT, ThemeMode.DARK, ThemeMode.IDA_NATIVE):
                manager.set_mode(mode)
                deadline = time.monotonic() + 5
                while manager._pending_apply is not None and time.monotonic() < deadline:
                    app.processEvents()
                self.assertIsNone(manager._pending_apply, "theme switch did not settle")
                tokens = manager.tokens()
                editor.setStyleSheet(f"color:{tokens.text}; background:{tokens.window};")
                for render in (md_to_html, _legacy_md_to_html):
                    with self.subTest(mode=mode, renderer=render.__name__):
                        editor.setHtml(render(markdown))
                        document = editor.document()
                        for text in ("Report", "address", "reference"):
                            cursor = document.find(text)
                            self.assertFalse(cursor.isNull(), f"missing rendered {text}")
                            brush = cursor.charFormat().foreground()
                            foreground = brush.color().name() if brush.style().value else tokens.text
                            self.assertNotEqual(foreground, tokens.text, f"{text} collapsed to body color")
                            background = cursor.charFormat().background()
                            bg = background.color().name() if background.style().value else tokens.window
                            high, low = sorted((hex_luminance(foreground), hex_luminance(bg)), reverse=True)
                            self.assertGreaterEqual((high + 0.05) / (low + 0.05), 4.5, f"unreadable {text}")
                        code = document.find("code_line")
                        self.assertFalse(code.isNull())
                        background = code.blockFormat().background()
                        self.assertTrue(background.style().value, "code block has no painted background")
                        self.assertNotEqual(background.color().name(), tokens.window)
                        foreground = code.charFormat().foreground().color().name()
                        high, low = sorted(
                            (hex_luminance(foreground), hex_luminance(background.color().name())), reverse=True
                        )
                        self.assertGreaterEqual((high + 0.05) / (low + 0.05), 4.5, "unreadable code block")
        finally:
            editor.close()
            manager.set_mode(previous_mode)
            deadline = time.monotonic() + 5
            while manager._pending_apply is not None and time.monotonic() < deadline:
                app.processEvents()
