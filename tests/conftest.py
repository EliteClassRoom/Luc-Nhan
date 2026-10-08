"""Root pytest conftest for the rikugan test suite.

This conftest automatically purges ``_StubModule`` entries from
:data:`sys.modules` between test modules, so a sibling test file that
installs panel-internal stubs at import time cannot poison
``rikugan.core.config``, ``rikugan.providers.registry``, etc. for
downstream tests (e.g. headless / provider tests that need the real
modules).

Test files that import rikugan modules at the top of the file —
i.e. *before* this conftest's ``pytest_pycollect_module`` hook fires
for that file — should also call :func:`tests.purge_rikugan_stubs`
at the very top, before the rikugan imports.  See
``tests/providers/test_providers.py`` for the canonical pattern.
"""

from __future__ import annotations

import pytest

from tests import purge_rikugan_stubs


@pytest.fixture(scope="session")
def qapp():
    """Real ``QApplication`` singleton for the real-Qt widget tests.

    The import is deliberately inside the fixture body: in a session where
    ``tests.qt_stubs`` has already replaced ``PySide6.*`` these tests skip
    (see ``tests/qt_real.py``) and the fixture is never requested, while an
    eager module-level import would pull the real binding into every
    session and disable the stubs.
    """
    from rikugan.ui.qt_compat import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    # Don't quit() here — leave the singleton alive so Qt cleans up via its
    # normal shutdown sequence when the process exits.


def pytest_runtest_setup(item):
    """Purge ``_StubModule`` entries from :data:`sys.modules`
    immediately before each test runs.

    We deliberately do NOT purge in ``pytest_collection_modifyitems``
    because that hook fires *after* all test files have been
    imported.  Purging there would remove real rikugan modules
    (e.g. ``rikugan.ui.context_bar``) that earlier test files had
    already imported, and the next test's ``patch("...module.attr",
    ...)`` call would re-import a fresh module instance — leaving the
    test's locally-bound functions pointing at a stale, pre-purge
    module.  Purging per-test, just before the test body executes,
    gives each test a clean sys.modules while preserving the
    locally-bound function references the test set up at import
    time.
    """
    purge_rikugan_stubs()
