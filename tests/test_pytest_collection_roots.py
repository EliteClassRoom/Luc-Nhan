from __future__ import annotations

from pathlib import Path

import tomllib


def test_pytest_testpaths_include_both_regression_roots() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests", "rikugan/tests"]


def test_representative_tests_exist_in_both_roots() -> None:
    assert Path("tests/agent/test_agent_loop.py").is_file()
    assert Path("rikugan/tests/test_token_usage_regression.py").is_file()


def test_rikugan_tests_is_a_package_to_avoid_bare_basename_collisions() -> None:
    # The two roots share a duplicate basename: tests/test_ida_docs_review_prompt.py
    # and rikugan/tests/test_ida_docs_review_prompt.py both exist. The empty
    # __init__.py marker forces package-local tests to import as fully-qualified
    # rikugan.tests.<name> (not bare basenames), preventing a prepend-mode
    # collection collision/shadow between the two roots.
    assert Path("rikugan/tests/__init__.py").is_file()


def test_ci_commands_do_not_narrow_pytest_to_one_root() -> None:
    for path in (Path(".github/workflows/ci.yml"), Path(".github/workflows/release.yml"), Path("ci-local.sh")):
        text = path.read_text(encoding="utf-8")
        assert "pytest tests/" not in text
