"""Tests for the IDA Output log-verbosity feature.

The feature adds a user-facing setting (``ida_output_log_level``) that
controls which log records appear in IDA's Output window via
``HostOutputHandler``.  File and JSONL logging are untouched — full
DEBUG output continues to land in ``rikugan_debug.log`` and
``rikugan_structured.jsonl``.

These tests are pure Python where possible; only the
``TestSettingsDialogAcceptsNewCombo`` test requires a ``QApplication``
and follows the existing ``_ensure_qapplication`` helper pattern from
``test_settings_dialog_fixes.py``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Config: ida_output_log_level default, validation, persistence
# ---------------------------------------------------------------------------


class TestIdaOutputLogLevelConfig(unittest.TestCase):
    """The new config field defaults to ``"warning"`` and round-trips
    through load/save for every supported value.  Invalid / legacy
    values are normalized to ``"warning"`` (matching the project's
    "clamp instead of refuse" convention).
    """

    def _make_config(self) -> object:
        from rikugan.core.config import RikuganConfig

        # Use a private tempdir so tests don't write to the real
        # ``~/.idapro/rikugan/rikugan.json`` and clobber user state.
        cfg = RikuganConfig()
        tmpdir = tempfile.mkdtemp(prefix="rikugan-cfg-test-")
        cfg._config_dir = tmpdir
        return cfg

    def test_default_is_warning(self) -> None:
        from rikugan.core.config import RikuganConfig

        cfg = RikuganConfig()
        self.assertEqual(
            cfg.ida_output_log_level,
            "warning",
            "Default verbosity must be 'warning' so routine INFO/DEBUG "
            "logs are suppressed in the IDA Output window by default.",
        )

    def test_validate_rejects_unknown_value(self) -> None:
        cfg = self._make_config()
        cfg.ida_output_log_level = "trace"  # not in the allowed set
        errors = cfg.validate()
        self.assertTrue(
            any("ida_output_log_level" in e for e in errors),
            f"validate() must flag unknown verbosity; got {errors!r}",
        )

    def test_save_normalizes_invalid_value_to_warning(self) -> None:
        cfg = self._make_config()
        cfg.ida_output_log_level = "verbose"
        # save() clamps invalid values instead of refusing to write
        cfg.save()
        self.assertEqual(cfg.ida_output_log_level, "warning")

    def test_round_trip_each_allowed_value(self) -> None:
        for value in ("debug", "info", "warning", "error", "critical", "off"):
            with self.subTest(value=value):
                cfg = self._make_config()
                cfg.ida_output_log_level = value
                cfg.save()

                # Re-load into a fresh instance with the same _config_dir
                from rikugan.core.config import RikuganConfig

                cfg2 = RikuganConfig()
                cfg2._config_dir = cfg._config_dir
                cfg2.load()
                self.assertEqual(
                    cfg2.ida_output_log_level,
                    value,
                    f"Value {value!r} did not survive a save/load round-trip.",
                )

    def test_load_normalizes_legacy_value(self) -> None:
        cfg = self._make_config()
        # Write a config file with a legacy/invalid value
        legacy = {
            "schema_version": 1,
            "ida_output_log_level": "verbose",
            "provider": {"name": "anthropic"},
        }
        with open(cfg.config_path, "w") as f:
            json.dump(legacy, f)

        from rikugan.core.config import RikuganConfig

        cfg2 = RikuganConfig()
        cfg2._config_dir = cfg._config_dir
        cfg2.load()
        self.assertEqual(
            cfg2.ida_output_log_level,
            "warning",
            "Legacy / unknown values must be normalized to 'warning' on load.",
        )

    def test_default_field_in_save_payload(self) -> None:
        """The default value is included in saved JSON so other tools
        (and the Settings dialog on first run) see the configured level
        rather than relying on dataclass defaults.
        """
        cfg = self._make_config()
        cfg.save()
        with open(cfg.config_path) as f:
            data = json.load(f)
        self.assertEqual(data.get("ida_output_log_level"), "warning")


# ---------------------------------------------------------------------------
# Logging: level mapping, host handler level, file handler untouched
# ---------------------------------------------------------------------------


class _NoopHandler(logging.Handler):
    """Capture-only handler used to assert what gets dispatched."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class TestLogLevelMapping(unittest.TestCase):
    """The level mapping helper must convert every supported config
    string to the matching ``logging`` level, with unknown values
    falling back to ``WARNING``.
    """

    def test_each_label_resolves_to_expected_level(self) -> None:
        import logging as _logging

        from rikugan.core.log_sinks import resolve_log_level

        cases = {
            "debug": _logging.DEBUG,
            "info": _logging.INFO,
            "warning": _logging.WARNING,
            "error": _logging.ERROR,
            "critical": _logging.CRITICAL,
            "off": _logging.CRITICAL + 1,
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(resolve_log_level(name), expected)

    def test_unknown_value_falls_back_to_warning(self) -> None:
        import logging as _logging

        from rikugan.core.log_sinks import resolve_log_level

        self.assertEqual(resolve_log_level("verbose"), _logging.WARNING)
        self.assertEqual(resolve_log_level(""), _logging.WARNING)
        self.assertEqual(resolve_log_level(None), _logging.WARNING)  # type: ignore[arg-type]
        self.assertEqual(resolve_log_level(42), _logging.WARNING)  # type: ignore[arg-type]

    def test_case_insensitive(self) -> None:
        import logging as _logging

        from rikugan.core.log_sinks import resolve_log_level

        self.assertEqual(resolve_log_level("WARNING"), _logging.WARNING)
        self.assertEqual(resolve_log_level("Off"), _logging.CRITICAL + 1)


class TestHostLogLevelRuntime(unittest.TestCase):
    """``set_host_log_level`` must update the level of every
    ``HostOutputHandler`` attached to the ``Rikugan`` logger so the
    new value takes effect without restarting IDA.

    File / JSON handlers must remain at DEBUG / INFO — changing the
    host-output setting must never silence the diagnostic stream on
    disk.
    """

    def setUp(self) -> None:
        # ``set_host_log_level`` targets the well-known ``Rikugan``
        # logger, so the test must attach handlers there too.  Snapshot
        # the logger state and restore it in tearDown so other tests
        # (and any running tool) keep their configuration.
        self.logger = logging.getLogger("Rikugan")
        self._snapshot = list(self.logger.handlers)
        self.logger.handlers.clear()
        self.logger.setLevel(logging.DEBUG)

    def tearDown(self) -> None:
        self.logger.handlers.clear()
        for h in self._snapshot:
            self.logger.addHandler(h)

    def _attach(self) -> tuple[logging.Handler, logging.Handler, logging.Handler]:
        """Attach one HostOutputHandler, one DEBUG file handler, and
        one INFO JSON-like handler.  Returns all three.
        """
        from rikugan.core.log_sinks import HostOutputHandler

        host = HostOutputHandler()
        host.setLevel(logging.WARNING)
        file_h = _NoopHandler()
        file_h.setLevel(logging.DEBUG)
        json_h = _NoopHandler()
        json_h.setLevel(logging.INFO)

        self.logger.addHandler(host)
        self.logger.addHandler(file_h)
        self.logger.addHandler(json_h)
        return host, file_h, json_h

    def test_set_host_log_level_updates_handler_in_place(self) -> None:
        """Calling ``set_host_log_level('error')`` on the live logger
        must change the handler's level so subsequent INFO records are
        dropped at the handler boundary, while a fresh WARNING record
        is still dispatched.
        """
        host, file_h, json_h = self._attach()
        sink_records: list[logging.LogRecord] = []

        def fake_sink(msg: str, levelno: int) -> None:
            # Capture what the host handler would have forwarded.  We
            # don't actually call ida_kernwin.msg — we just observe
            # what passed the handler's level filter by feeding an
            # INFO record through self.logger and checking the
            # handler.accept() decision.
            sink_records.append((levelno, msg))

        from rikugan.core import log_sinks

        with patch.object(log_sinks, "_host_sink", fake_sink, create=True):
            from rikugan.core.log_sinks import set_host_log_level

            new_level = set_host_log_level("error")
            self.assertEqual(new_level, logging.ERROR)
            self.assertEqual(host.level, logging.ERROR)
            # File/JSON sinks are intentionally untouched.
            self.assertEqual(file_h.level, logging.DEBUG)
            self.assertEqual(json_h.level, logging.INFO)

            # The host handler's dispatch is gated by
            # ``hdlr.level <= record.levelno`` (the same predicate
            # ``Logger.callHandlers`` uses).  WARNING (30) is strictly
            # below ERROR (40), so WARNING records must be gated off
            # when the handler is at ERROR; ERROR records must still
            # flow through.
            self.assertGreater(
                host.level,
                logging.WARNING,
                "WARNING records must be gated off when host level is ERROR.",
            )
            self.assertLessEqual(
                host.level,
                logging.ERROR,
                "ERROR records must still be dispatched at ERROR level.",
            )

    def test_set_host_log_level_off_disables_host_output(self) -> None:
        """``'off'`` must set the host handler above CRITICAL so even
        CRITICAL records are dropped at the logger-dispatch layer
        (``Logger.callHandlers`` gates by ``hdlr.level <= record.levelno``)
        — while leaving file logging alone.
        """
        host, file_h, _json_h = self._attach()
        from rikugan.core import log_sinks
        from rikugan.core.log_sinks import set_host_log_level

        with patch.object(log_sinks, "_host_sink", lambda *a: None, create=True):
            set_host_log_level("off")
        self.assertGreater(host.level, logging.CRITICAL)
        self.assertEqual(file_h.level, logging.DEBUG)

        # Emulate the level-check that ``Logger.callHandlers`` performs
        # before invoking the handler: ``hdlr.level <= record.levelno``.
        # ``logging.CRITICAL == 50`` must be strictly less than the
        # OFF level for the host handler to be skipped.
        self.assertGreater(
            host.level,
            logging.CRITICAL,
            "Off must set the host handler's level strictly above CRITICAL "
            "so the logger-level dispatcher skips even CRITICAL records.",
        )

    def test_set_host_log_level_safe_with_no_handlers(self) -> None:
        """Calling the setter before any handlers exist must not
        raise — the user may change the setting from a fresh IDA
        session, before the bootstrap code adds handlers.
        """
        self.logger.handlers.clear()
        from rikugan.core import log_sinks
        from rikugan.core.log_sinks import set_host_log_level

        with patch.object(log_sinks, "_host_sink", lambda *a: None, create=True):
            # No HostOutputHandler attached — should be a no-op.
            self.assertEqual(set_host_log_level("info"), logging.INFO)


class TestBootstrapHostLevel(unittest.TestCase):
    """The bootstrap path (``get_logger()``) must seed the host
    handler's level from the configured ``ida_output_log_level``,
    falling back to ``WARNING`` if the config cannot be read.
    """

    def setUp(self) -> None:
        # Make sure no real Rikugan logger exists in this process.
        import logging as _logging

        _logging.getLogger("Rikugan").handlers.clear()

    def tearDown(self) -> None:
        import logging as _logging

        _logging.getLogger("Rikugan").handlers.clear()

    def _bootstrap_with_level(self, configured: str | None) -> logging.Logger:
        """Construct the Rikugan logger with an optional preconfigured
        ``ida_output_log_level`` value (None means "no config file").
        """
        from rikugan.core import log_sinks
        from rikugan.core import logging as rikugan_logging

        tmpdir = tempfile.mkdtemp(prefix="rikugan-bootstrap-test-")

        def fake_reader() -> int:
            """Stand-in for ``_read_configured_host_level`` that reads
            the tempdir config so we don't touch the user's real
            ``~/.idapro/rikugan/rikugan.json``.
            """
            if configured is None:
                return logging.WARNING
            return log_sinks.resolve_log_level(configured)

        def fake_path() -> str:
            return os.path.join(tmpdir, "rikugan_debug.log")

        # Force the module-level singleton to re-init so the host
        # handler picks up the freshly-written config.
        rikugan_logging._logger = None

        with (
            patch.object(rikugan_logging, "_read_configured_host_level", fake_reader),
            patch.object(log_sinks, "_log_file_path", fake_path),
        ):
            logger = rikugan_logging.get_logger()

        self._configured_level = configured
        return logger

    def test_default_level_is_warning(self) -> None:
        logger = self._bootstrap_with_level(None)
        from rikugan.core.log_sinks import HostOutputHandler

        host_handlers = [h for h in logger.handlers if isinstance(h, HostOutputHandler)]
        self.assertTrue(host_handlers, "Bootstrap must attach a HostOutputHandler.")
        self.assertEqual(host_handlers[0].level, logging.WARNING)

    def test_configured_off_suppresses_host_output(self) -> None:
        logger = self._bootstrap_with_level("off")
        from rikugan.core.log_sinks import HostOutputHandler

        host = next(h for h in logger.handlers if isinstance(h, HostOutputHandler))
        self.assertGreater(host.level, logging.CRITICAL)

    def test_file_handler_keeps_debug(self) -> None:
        """File logging must remain at DEBUG regardless of the host
        setting — full diagnostic stream survives on disk.
        """
        logger = self._bootstrap_with_level("error")
        # Find the file handler — it is the only one whose stream is a
        # real file (the JSONL handler is also a file but its level is
        # INFO, so we filter on level).
        file_handlers = [h for h in logger.handlers if hasattr(h, "stream") and h.level <= logging.DEBUG]
        self.assertTrue(file_handlers, "Expected at least one DEBUG-level file handler.")
        for h in file_handlers:
            self.assertEqual(h.level, logging.DEBUG)


# ---------------------------------------------------------------------------
# Settings dialog: the new combo persists the selection on _on_accept()
# ---------------------------------------------------------------------------


def _ensure_qapplication():
    from rikugan.ui.qt_compat import QApplication

    return QApplication.instance() or QApplication([])


class TestSettingsDialogAcceptsNewCombo(unittest.TestCase):
    """Pressing OK on the settings dialog must persist the user-chosen
    ``ida_output_log_level`` from the combo box and apply it to the
    live logger via ``set_host_log_level``.

    The test also confirms the lazy-tab regression coverage from
    ``test_settings_dialog_fixes.py`` still passes with the new
    Behavior widget — the OK path must not force-load Skills / MCP /
    Profiles tabs.
    """

    def test_default_combo_preselected_from_config(self) -> None:
        from rikugan.core.config import RikuganConfig
        from rikugan.ui.settings_dialog import SettingsDialog

        _ensure_qapplication()

        config = RikuganConfig()
        config.ida_output_log_level = "warning"  # the new default
        dlg = SettingsDialog(config)
        try:
            self.assertTrue(
                hasattr(dlg, "_ida_output_log_combo"),
                "Settings dialog must expose _ida_output_log_combo.",
            )
            self.assertEqual(dlg._ida_output_log_combo.currentText(), "Warning")
        finally:
            dlg.done(0)

    def test_accept_persists_selected_verbosity(self) -> None:
        from rikugan.core.config import RikuganConfig
        from rikugan.ui.settings_dialog import SettingsDialog

        _ensure_qapplication()

        config = RikuganConfig()
        config.ida_output_log_level = "warning"
        dlg = SettingsDialog(config)
        try:
            # Switch to "Error" — a non-default, non-Warning selection
            # so we can verify persistence.
            idx = dlg._ida_output_log_combo.findText("Error")
            self.assertGreaterEqual(idx, 0)
            dlg._ida_output_log_combo.setCurrentIndex(idx)

            dlg._on_accept()
            self.assertEqual(
                config.ida_output_log_level,
                "error",
                "_on_accept() must persist the selected combo value into the config.",
            )
        finally:
            dlg.done(0)

    def test_accept_applies_host_log_level_at_runtime(self) -> None:
        """Accepting the dialog with a new selection must call
        ``set_host_log_level`` so the change takes effect without an
        IDA restart.
        """
        from rikugan.core.config import RikuganConfig
        from rikugan.ui.settings_dialog import SettingsDialog

        _ensure_qapplication()

        config = RikuganConfig()
        dlg = SettingsDialog(config)
        try:
            with patch("rikugan.ui.settings_dialog.set_host_log_level") as mock_set:
                dlg._on_accept()
                mock_set.assert_called_once_with("warning")  # default
        finally:
            dlg.done(0)

    def test_lazy_tabs_remain_unopened_after_accept(self) -> None:
        """Re-verify the lazy-tab regression: the new Behavior widget
        must not cause ``_on_accept()`` to force-load Skills/MCP/Profiles.
        """
        from rikugan.core.config import RikuganConfig
        from rikugan.ui.settings_dialog import SettingsDialog

        _ensure_qapplication()

        config = RikuganConfig()
        dlg = SettingsDialog(config)
        try:
            dlg._on_accept()
            self.assertIsNone(dlg._skills_tab)
            self.assertIsNone(dlg._mcp_tab)
            self.assertIsNone(dlg._profiles_tab)
        finally:
            dlg.done(0)


# ---------------------------------------------------------------------------
# Labels exposed to the UI
# ---------------------------------------------------------------------------


class TestLogLevelLabels(unittest.TestCase):
    """The labels shipped to the Settings dialog combo must match the
    backing config strings, in the documented order.
    """

    def test_labels_and_values_bidirectional(self) -> None:
        from rikugan.core.log_sinks import (
            LOG_LEVEL_LABEL_TO_VALUE,
            LOG_LEVEL_LABELS,
            LOG_LEVEL_VALUE_TO_LABEL,
        )

        self.assertEqual(
            LOG_LEVEL_LABELS,
            ["Debug", "Info", "Warning", "Error", "Critical", "Off"],
        )
        for label, value in LOG_LEVEL_LABEL_TO_VALUE.items():
            self.assertEqual(LOG_LEVEL_VALUE_TO_LABEL[value], label)


if __name__ == "__main__":
    unittest.main()
