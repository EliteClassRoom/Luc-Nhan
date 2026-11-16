"""Root pytest conftest for the lucnhan test suite.

This conftest automatically purges ``_StubModule`` entries from
:data:`sys.modules` between test modules, so a sibling test file that
installs panel-internal stubs at import time cannot poison
``lucnhan.core.config``, ``lucnhan.providers.registry``, etc. for
downstream tests (e.g. headless / provider tests that need the real
modules).

Test files that import lucnhan modules at the top of the file —
i.e. *before* this conftest's ``pytest_pycollect_module`` hook fires
for that file — should also call :func:`tests.purge_lucnhan_stubs`
at the very top, before the lucnhan imports.  See
``tests/providers/test_providers.py`` for the canonical pattern.
"""

from __future__ import annotations

import pytest

from tests import purge_lucnhan_stubs
from tests.mocks.ida_mock import install_ida_mocks

# Install the IDA mocks before *any* test module is imported. Several modules
# import lucnhan at import time without installing mocks first, and
# ``lucnhan.core.host`` decides ``_HOST`` from ``importlib.import_module
# ("idaapi")`` at import: whichever module is collected first therefore froze
# the whole process to "standalone" (``get_database_path()`` returns "" and
# every history-restore test fails with WRONG_IDB). Doing it here makes the
# host detection independent of collection order.
install_ida_mocks()


@pytest.fixture(scope="session")
def qapp():
    """Real ``QApplication`` singleton for the real-Qt widget tests.

    The import is deliberately inside the fixture body: in a session where
    ``tests.qt_stubs`` has already replaced ``PySide6.*`` these tests skip
    (see ``tests/qt_real.py``) and the fixture is never requested, while an
    eager module-level import would pull the real binding into every
    session and disable the stubs.
    """
    from lucnhan.ui.qt_compat import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    # Don't quit() here — leave the singleton alive so Qt cleans up via its
    # normal shutdown sequence when the process exits.


def pytest_collect_file(file_path, parent=None):
    """Purge stub modules before a test file is imported.

    Test files install their ``_StubModule`` stand-ins at import time and only
    restore the real modules in a fixture teardown, so a later file binds the
    stubs. This hook is best effort: pytest 9 collects the whole tree before
    importing any module, so a stub left in :data:`sys.modules` is usually
    already too late to purge here. The durable fix is to stub only what the
    file really needs — ``tests/tools/test_panel_core.py`` stubs ``lucnhan.ui.*``
    only, ``tests/tools/test_settings_dialog.py`` and ``tests/tools/test_ida_panel.py``
    import ``lucnhan.core.*`` and ``lucnhan.ida.ui.*`` for real.
    """
    purge_lucnhan_stubs()


def pytest_runtest_setup(item):
    """Purge ``_StubModule`` entries from :data:`sys.modules`
    immediately before each test runs.

    We deliberately do NOT purge in ``pytest_collection_modifyitems``
    because that hook fires *after* all test files have been
    imported.  Purging there would remove real lucnhan modules
    (e.g. ``lucnhan.ui.context_bar``) that earlier test files had
    already imported, and the next test's ``patch("...module.attr",
    ...)`` call would re-import a fresh module instance — leaving the
    test's locally-bound functions pointing at a stale, pre-purge
    module.  Purging per-test, just before the test body executes,
    gives each test a clean sys.modules while preserving the
    locally-bound function references the test set up at import
    time.
    """
    purge_lucnhan_stubs()
