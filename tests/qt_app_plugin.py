"""Pytest plugin that guarantees a ``QApplication`` for real-widget runs.

A real-Qt file dies with ``QWidget: Must construct a QApplication before a
QWidget`` — ``qFatal``, so the process aborts and loses its buffered output —
when it is run on its own.  Two separate triggers exist:

* tests that build widgets at run time (``setUp``) without an application;
* ``lucnhan.ui.theme.manager`` deriving host colours from a throwaway
  ``QWidget`` probe **while the module is imported**, which happens at
  collection time, i.e. before any fixture could run.

So the application is created at *plugin import*, not in a fixture, and the
reference is kept alive at module level.  That is also why the plugin has to
be loaded before the test modules are collected::

    python -m pytest -p tests.qt_app_plugin tests/ui/test_chat_view_restore.py

``tests/qt_real.py`` decides its skip at import time, so a file needs its own
process for the guard to see the real ``PySide6``.
"""

from __future__ import annotations

import pytest

from lucnhan.ui.qt_compat import QApplication

_APP = QApplication.instance() or QApplication([])


@pytest.fixture(scope="session", autouse=True)
def _ensure_qapp():
    """Keep the application alive for the whole session."""
    assert _APP is not None
    yield
