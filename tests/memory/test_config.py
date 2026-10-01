"""Tests for central memory config, constants, and dependency manifests."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from rikugan.core.config import RikuganConfig


def _assert_dependency_consistent(package: str) -> None:
    """Every runtime manifest must declare *package* with the same spec.

    The spec itself is owned by the manifests (dependabot bumps it); the
    regression this catches is a bump landing in two of the three files and
    silently leaving the third behind, which desyncs installs.
    """

    root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    plugin = json.loads((root / "ida-plugin.json").read_text(encoding="utf-8"))
    requirements = {
        line.strip()
        for line in (root / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    def _match(specs: set[str]) -> str:
        hits = {spec for spec in specs if spec.split(">=")[0].split("<")[0].strip() == package}
        assert len(hits) == 1, f"{package} must appear exactly once, got {sorted(hits) or 'none'}"
        return hits.pop()

    project_spec = _match(set(pyproject["project"]["dependencies"]))
    plugin_spec = _match(set(plugin["plugin"]["pythonDependencies"]))
    requirements_spec = _match(requirements)
    assert project_spec == plugin_spec == requirements_spec, (
        f"{package} spec drifted across manifests: pyproject={project_spec!r} "
        f"ida-plugin.json={plugin_spec!r} requirements.txt={requirements_spec!r}"
    )


def test_memory_dir_is_central(tmp_path: Path) -> None:
    config = RikuganConfig()
    config._config_dir = str(tmp_path)

    assert Path(config.memory_dir) == tmp_path / "memory"


def test_anthropic_runtime_dependency_is_in_all_manifests() -> None:
    _assert_dependency_consistent("anthropic")


def test_portalocker_runtime_dependency_is_in_all_manifests() -> None:
    _assert_dependency_consistent("portalocker")


def test_unicorn_runtime_dependency_is_in_all_manifests() -> None:
    _assert_dependency_consistent("unicorn")
