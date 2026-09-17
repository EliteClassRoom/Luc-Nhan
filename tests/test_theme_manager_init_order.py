"""Regression test for ThemeManager init-order bug.

Bug observed in production log (startup traceback):

    File ".../theme/manager.py", line 362, in _compute_tokens
        self._log_auto_derive_once(tokens)
    File ".../theme/manager.py", line 292, in _log_auto_derive_once
        if self._log_auto_derive_once_flag:
    AttributeError: 'ThemeManager' object has no attribute '_log_auto_derive_once_flag'

Root cause: in ``__init__``, ``self._compute_tokens()`` (line 240) runs
BEFORE ``self._log_auto_derive_once_flag = False`` (line 241). When the
host is IDA and mode is AUTO, ``_compute_tokens`` successfully derives
tokens and calls ``_log_auto_derive_once``, which reads the flag that
has not been assigned yet.

The exception is swallowed by the broad ``except`` in ``_compute_tokens``
(falling back to DARK_TOKENS), so the bug is silent in production except
for a startup traceback in the debug log. It also forces a DARK fallback
even when AUTO/IDA derive should succeed.

This test reproduces the exact path: AUTO mode + IDA host + successful
derive -> ``_log_auto_derive_once`` must NOT raise AttributeError.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from tests.qt_stubs import ensure_pyside6_stubs

ensure_pyside6_stubs()

from rikugan.ui.theme.manager import DARK_TOKENS, ThemeManager


class TestThemeManagerInitOrder(unittest.TestCase):
    def setUp(self) -> None:
        ThemeManager.reset()

    def tearDown(self) -> None:
        ThemeManager.reset()

    def test_auto_mode_init_does_not_raise_attribute_error_on_derive(self):
        # Reproduce the production traceback: when _compute_tokens reaches
        # the AUTO+IDA branch and calls _log_auto_derive_once during
        # __init__, that method must not raise AttributeError for
        # _log_auto_derive_once_flag.
        #
        # The bug is an init-ordering problem: __init__ calls
        # self._compute_tokens() (line 240) BEFORE it assigns
        # self._log_auto_derive_once_flag = False (line 241). We simulate
        # the AUTO+IDA path by patching _compute_tokens to call
        # _log_auto_derive_once — exactly what the real method does on a
        # successful IDA derive. Before the fix this raises inside __init__.

        def fake_compute(self: ThemeManager) -> Any:
            # Mirror the real AUTO+IDA path: derive succeeds, then log.
            self._log_auto_derive_once(DARK_TOKENS)
            return DARK_TOKENS

        ThemeManager.reset()
        with patch.object(ThemeManager, "_compute_tokens", fake_compute):
            try:
                mgr = ThemeManager()
            except AttributeError as exc:
                self.fail(
                    "ThemeManager.__init__ raised AttributeError because "
                    "_log_auto_derive_once_flag is read before it is set: "
                    f"{exc}"
                )
        self.assertTrue(
            hasattr(mgr, "_log_auto_derive_once_flag"),
            "_log_auto_derive_once_flag must be initialised in __init__ BEFORE _compute_tokens() runs.",
        )


if __name__ == "__main__":
    unittest.main()
