"""Tests for central memory config, constants, and dependency manifests."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from rikugan.core.config import RikuganConfig

# Version specifier starts at the first comparison/exclusion operator; everything
# before it is the distribution name (PEP 508), e.g. ``portalocker>=4.1.0,<5``.
_NAME = re.compile(r"^[A-Za-z0-9._-]+")
_ROOT = Path(__file__).resolve().parents[2]


def _package_name(spec: str) -> str:
    match = _NAME.match(spec)
    assert match, f"cannot parse a package name out of {spec!r}"
    return match.group().lower()


def _manifests() -> dict[str, list[str]]:
    """Runtime dependency specs per manifest, ``pyproject.toml`` first (the source of truth)."""

    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    plugin = json.loads((_ROOT / "ida-plugin.json").read_text(encoding="utf-8"))
    requirements = [
        line.strip()
        for line in (_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return {
        "pyproject.toml": list(pyproject["project"]["dependencies"]),
        "ida-plugin.json": list(plugin["plugin"]["pythonDependencies"]),
        "requirements.txt": requirements,
    }


@pytest.mark.parametrize("package", [_package_name(spec) for spec in _manifests()["pyproject.toml"]])
def test_runtime_dependency_spec_is_consistent_across_manifests(package: str) -> None:
    """Every runtime dep must be declared once, with the same spec, in all three manifests.

    Driven by ``pyproject.toml``'s ``[project].dependencies`` so a dependabot bump
    landing in two of the three files cannot silently leave the third behind — and
    so a dependency added to only one manifest is reported as missing, not skipped.
    The spec itself is owned by the manifests; this only guards against drift.
    """

    specs = {}
    for manifest, declared in _manifests().items():
        hits = [spec for spec in declared if _package_name(spec) == package]
        assert len(hits) == 1, f"{package} must appear exactly once in {manifest}, got {sorted(hits) or 'none'}"
        specs[manifest] = hits[0]

    assert len(set(specs.values())) == 1, f"{package} spec drifted across manifests: {specs}"


def test_memory_dir_is_central(tmp_path: Path) -> None:
    config = RikuganConfig()
    config._config_dir = str(tmp_path)

    assert Path(config.memory_dir) == tmp_path / "memory"
