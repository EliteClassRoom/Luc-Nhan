"""Persistence and validation tests for the `hide_strings` config field.

Round-trips the boolean through ``RikuganConfig.load``/``save``, asserts the
strict-bool guard rejects malformed values, and verifies ``validate`` reports
the field's type. Independent of Qt, IDA, or any other subsystem.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

# Force the real config module even if a sibling test installed a
# stub for ``rikugan.core.config`` earlier in the collection order.
sys.modules.pop("rikugan.core.config", None)

from rikugan.core.config import RikuganConfig  # noqa: E402


def _write_config(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestHideStringsConfig(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fresh_config(self) -> RikuganConfig:
        cfg = RikuganConfig()
        cfg._config_dir = str(self.tmp)
        return cfg

    def test_default_value_is_false(self) -> None:
        cfg = self._fresh_config()
        self.assertFalse(cfg.hide_strings)

    def test_round_trip_true(self) -> None:
        cfg = self._fresh_config()
        cfg.hide_strings = True
        cfg.save()
        reloaded = RikuganConfig()
        reloaded._config_dir = str(self.tmp)
        reloaded.load()
        self.assertTrue(reloaded.hide_strings)

    def test_round_trip_false(self) -> None:
        cfg = self._fresh_config()
        cfg.hide_strings = False
        cfg.save()
        reloaded = RikuganConfig()
        reloaded._config_dir = str(self.tmp)
        reloaded.load()
        self.assertFalse(reloaded.hide_strings)

    def test_load_missing_field_defaults_to_false(self) -> None:
        _write_config(self.tmp / "config.json", {"provider": {"name": "anthropic"}})
        cfg = self._fresh_config()
        cfg.load()
        self.assertFalse(cfg.hide_strings)

    def test_load_rejects_string_value(self) -> None:
        _write_config(self.tmp / "config.json", {"hide_strings": "true"})
        cfg = self._fresh_config()
        cfg.load()
        self.assertFalse(cfg.hide_strings, "truthy string must not enable hide_strings")

    def test_load_rejects_integer_value(self) -> None:
        _write_config(self.tmp / "config.json", {"hide_strings": 1})
        cfg = self._fresh_config()
        cfg.load()
        self.assertFalse(cfg.hide_strings, "integer must not enable hide_strings")

    def test_validate_rejects_non_bool(self) -> None:
        cfg = self._fresh_config()
        cfg.hide_strings = "yes"  # type: ignore[assignment]
        errors = cfg.validate()
        self.assertIn("hide_strings must be a bool", errors)

    def test_save_clamps_non_bool_to_false(self) -> None:
        cfg = self._fresh_config()
        cfg.hide_strings = "true"  # type: ignore[assignment]
        # validate() flags it; save() must still produce a loadable file.
        cfg.save()
        reloaded = RikuganConfig()
        reloaded._config_dir = str(self.tmp)
        reloaded.load()
        self.assertFalse(reloaded.hide_strings)


if __name__ == "__main__":
    unittest.main()
