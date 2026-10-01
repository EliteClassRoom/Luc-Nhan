"""Unit tests for scripts/validate_archive.py (HCLI structural shim)."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from scripts.validate_archive import ArchiveValidationError, validate_archive


def _make_zip(entries: dict[str, str | bytes]) -> bytes:
    """Build an in-memory zip from a {arcname: content} mapping."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            if isinstance(content, str):
                zf.writestr(name, content)
            else:
                zf.writestr(name, content)
    return buf.getvalue()


def test_validate_flat_zip_passes(tmp_path: Path) -> None:
    # Arrange — ida-plugin.json + entryPoint at root, rikugan/ package
    data = _make_zip(
        {
            "ida-plugin.json": json.dumps({"plugin": {"entryPoint": "rikugan_plugin.py"}}),
            "rikugan_plugin.py": "# entry",
            "rikugan/__init__.py": "",
        }
    )
    p = tmp_path / "rikugan-v1.0.zip"
    p.write_bytes(data)

    # Act + Assert — no exception
    validate_archive(p)


def test_validate_rejects_missing_ida_plugin_json(tmp_path: Path) -> None:
    # Arrange — no metadata file
    data = _make_zip({"rikugan_plugin.py": "# entry"})
    p = tmp_path / "rikugan-v1.0.zip"
    p.write_bytes(data)

    # Act + Assert
    with pytest.raises(ArchiveValidationError, match=r"ida-plugin\.json"):
        validate_archive(p)


def test_validate_rejects_wrapping_subfolder(tmp_path: Path) -> None:
    # Arrange — ida-plugin.json nested under rikugan-v1.0/
    data = _make_zip(
        {
            "rikugan-v1.0/ida-plugin.json": json.dumps({"plugin": {"entryPoint": "rikugan_plugin.py"}}),
            "rikugan-v1.0/rikugan_plugin.py": "# entry",
        }
    )
    p = tmp_path / "rikugan-v1.0.zip"
    p.write_bytes(data)

    # Act + Assert
    with pytest.raises(ArchiveValidationError, match="root"):
        validate_archive(p)


def test_validate_rejects_missing_entry_point(tmp_path: Path) -> None:
    # Arrange — metadata points to entryPoint that isn't in the zip
    data = _make_zip(
        {
            "ida-plugin.json": json.dumps({"plugin": {"entryPoint": "rikugan_plugin.py"}}),
        }
    )
    p = tmp_path / "rikugan-v1.0.zip"
    p.write_bytes(data)

    # Act + Assert
    with pytest.raises(ArchiveValidationError, match="entryPoint"):
        validate_archive(p)


def test_validate_rejects_invalid_json_metadata(tmp_path: Path) -> None:
    # Arrange
    data = _make_zip(
        {
            "ida-plugin.json": "not json {{{",
            "rikugan_plugin.py": "# entry",
        }
    )
    p = tmp_path / "rikugan-v1.0.zip"
    p.write_bytes(data)

    # Act + Assert
    with pytest.raises(ArchiveValidationError, match="json"):
        validate_archive(p)
