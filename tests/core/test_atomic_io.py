"""Retry contract of ``atomic_replace`` on transient Windows denials."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lucnhan.core import atomic_io


def _denied(winerror: int) -> PermissionError:
    exc = PermissionError(13, "denied")
    exc.winerror = winerror  # type: ignore[attr-defined]
    return exc


def _patch_replace(monkeypatch: pytest.MonkeyPatch, failures: int, winerror: int) -> dict[str, int]:
    """Make ``os.replace`` fail *failures* times with *winerror*, then work."""
    real_replace = os.replace
    state = {"calls": 0}

    def flaky(src: object, dst: object) -> None:
        state["calls"] += 1
        if state["calls"] <= failures:
            raise _denied(winerror)
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(atomic_io.os, "replace", flaky)
    monkeypatch.setattr(atomic_io.time, "sleep", lambda _s: None)
    return state


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_transient_denial_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winerror: int) -> None:
    """Access denied (5) is what Windows returns while a scanner holds the target."""
    src, dst = tmp_path / "src.tmp", tmp_path / "dst.json"
    src.write_text("new")
    dst.write_text("old")
    state = _patch_replace(monkeypatch, failures=2, winerror=winerror)

    atomic_io.atomic_replace(src, dst)

    assert dst.read_text() == "new"
    assert state["calls"] == 3


def test_persistent_denial_still_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src, dst = tmp_path / "src.tmp", tmp_path / "dst.json"
    src.write_text("new")
    state = _patch_replace(monkeypatch, failures=10_000, winerror=5)

    with pytest.raises(PermissionError):
        atomic_io.atomic_replace(src, dst)

    assert state["calls"] == atomic_io._DEFAULT_ATTEMPTS


def test_other_errors_are_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def missing(src: object, dst: object) -> None:
        calls["n"] += 1
        raise FileNotFoundError(2, "no such file")

    monkeypatch.setattr(atomic_io.os, "replace", missing)

    with pytest.raises(FileNotFoundError):
        atomic_io.atomic_replace(tmp_path / "a", tmp_path / "b")

    assert calls["n"] == 1
