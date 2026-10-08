"""Guards for where the test suite lives.

The suite is a single root (``tests/``).  ``rikugan/tests/`` used to be a
second, in-package root; it was merged here because:

* the tests are not shipped anyway (``scripts/build_release.py`` strips any
  ``tests`` path part), so keeping them inside the importable package only
  exposed ``rikugan.tests.*`` at runtime and cost a ``sys.path`` bootstrap
  plus three mypy overrides;
* a session that collected both roots imported the real ``PySide6`` binding
  (``rikugan/tests/conftest.py``) *before* the first ``tests.qt_stubs``
  installer ran, so the stubs then overwrote classes inside the real Qt
  modules — two incompatible Qt type universes in one process, which
  fast-fails natively (exit 0xC0000409) instead of raising.
"""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_pytest_testpaths_is_the_single_suite_root() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]


def test_in_package_test_tree_is_gone() -> None:
    # ``rikugan/conftest.py`` existed only to make ``rikugan.*`` importable
    # from inside the package tree, i.e. to serve the removed second root.
    assert not Path("rikugan/tests").exists()
    assert not Path("rikugan/conftest.py").exists()


def test_tests_relocated_from_the_old_second_root_exist() -> None:
    assert Path("tests/a2a/test_a2a_dispatcher.py").is_file()
    assert Path("tests/agent/test_token_usage_regression.py").is_file()
    assert Path("tests/knowledge/_helpers.py").is_file()
    assert Path("tests/ui/test_message_widgets.py").is_file()
