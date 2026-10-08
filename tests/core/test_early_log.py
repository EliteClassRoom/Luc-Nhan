"""Unit tests for rikugan.core.early_log.

The early_log module is intentionally stdlib-only: it must be importable
even if the rest of ``rikugan.*`` is broken. These tests run in a
subprocess so we can verify that property independently from the test
runner itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

import rikugan.core.early_log as el


class TestEarlyLogBuffer(unittest.TestCase):
    """The most recent _BUFFER_MAXLEN records are kept in-memory."""

    def test_ring_buffer_truncation(self) -> None:
        el._reset_for_tests()
        for i in range(60):
            el._early_log(f"line-{i}")
        snap = el._early_log_buffer_snapshot()
        # The early_log() helper itself appends a banner line ("=== Lục nhãn
        # early-startup log started ===") on first call after a reset; we
        # allow for that by asserting the snapshot is bounded by the buffer
        # size and contains the most recent tail.
        self.assertLessEqual(len(snap), el._BUFFER_MAXLEN)
        self.assertEqual(len(snap), el._BUFFER_MAXLEN)
        # Last 50 lines from input must all be present (the banner is line 0
        # of the buffer if it landed first, so the surviving tail spans the
        # final 50 records after the banner). Just check the very last entry.
        self.assertTrue(snap[-1].endswith("line-59"))

    def test_buffer_snapshots_are_strings(self) -> None:
        el._reset_for_tests()
        el._early_log("hello")
        snap = el._early_log_buffer_snapshot()
        self.assertTrue(all(isinstance(line, str) for line in snap))


class TestEarlyLogPath(unittest.TestCase):
    def test_log_path_under_user_idapro(self) -> None:
        el._reset_for_tests()
        # Force file resolution by re-running the path resolver.
        el._file_path, el._crash_path = el._resolve_paths()
        log_path = el._early_log_path()
        crash_path = el._early_log_crash_path()
        self.assertTrue(log_path.endswith("early_startup.log"))
        self.assertTrue(crash_path.endswith("early_startup_crash.log"))
        # Sibling files, distinct basenames.
        self.assertEqual(os.path.dirname(log_path), os.path.dirname(crash_path))
        self.assertNotEqual(os.path.basename(log_path), os.path.basename(crash_path))


class TestEarlyLogCrash(unittest.TestCase):
    def test_crash_file_contains_traceback(self) -> None:
        el._reset_for_tests()
        # Redirect both paths into a temp dir so we don't touch $HOME.
        with tempfile.TemporaryDirectory() as tmp:
            el._file_path = os.path.join(tmp, "early_startup.log")
            el._crash_path = os.path.join(tmp, "early_startup_crash.log")
            # Seed the buffer with a recognizable line.
            el._early_log("seed-record-for-crash-test")
            try:
                raise RuntimeError("boom")
            except RuntimeError as exc:
                el._early_log_crash(exc)
            with open(el._crash_path, encoding="utf-8") as fh:
                body = fh.read()
        self.assertIn("boom", body)
        self.assertIn("RuntimeError", body)
        self.assertIn("seed-record-for-crash-test", body)
        # traceback header marker is present.
        self.assertIn("--- traceback ---", body)
        # Buffer snapshot header is present.
        self.assertIn("--- buffer snapshot ---", body)


class TestEarlyLogSinksExceptions(unittest.TestCase):
    """All operations must swallow their own exceptions."""

    def test_log_swallows_io_error(self) -> None:
        el._reset_for_tests()
        # Force the file handle to one that fails on write.
        el._file_path = os.path.join(tempfile.gettempdir(), "early_startup_unused.log")
        el._file = None  # disabled file handle path
        # Should not raise.
        el._early_log("anything")
        el._early_log("anything else")

        # Force _file to an object whose methods always raise.
        class _RaisingFile:
            def write(self, *_args, **_kwargs):  # pragma: no cover - tested below
                raise OSError("disk full")

            def flush(self):  # pragma: no cover
                raise OSError("flush failed")

            def fileno(self):  # pragma: no cover
                raise OSError("no fd")

        el._file = _RaisingFile()  # type: ignore[assignment]
        # Two more calls; neither should propagate.
        el._early_log("still-here")
        el._early_log("still-here-2")
        # The buffer should still receive entries even if file I/O failed.
        snap = el._early_log_buffer_snapshot()
        joined = "\n".join(snap)
        self.assertIn("still-here-2", joined)

    def test_crash_swallows_io_error(self) -> None:
        el._reset_for_tests()

        # Patch _open_file to always return a file-like that raises on write.
        class _RaisingFile:
            def __init__(self, *_a, **_k):
                pass

            def write(self, *_args, **_kwargs):
                raise OSError("crash-sink-full")

            def flush(self):
                raise OSError("flush failed")

            def fileno(self):
                raise OSError("no fd")

            def close(self):
                pass

        original_open = el._open_file
        el._open_file = lambda _path: _RaisingFile()  # type: ignore[assignment]
        try:
            # Should not raise even though every write raises.
            el._early_log_crash(RuntimeError("would-crash-here"))
        finally:
            el._open_file = original_open  # type: ignore[assignment]


class TestEarlyLogImportIsolation(unittest.TestCase):
    """``import rikugan.core.early_log`` must not pull in heavy rikugan modules.

    The package markers ``rikugan`` and ``rikugan.core`` are unavoidably
    present (Python inserts them when loading any submodule). ``rikugan.constants``
    is loaded by an ``__init__.py`` as a side effect. What we DO want to
    verify is that no heavy / optional modules get pulled in — otherwise
    ``early_log`` would inherit their failure modes.
    """

    # Heavy modules we never want early_log to trigger.
    _FORBIDDEN = (
        "rikugan.core.logging",
        "rikugan.ui.qt_compat",
        "rikugan.ui.panel_core",
        "rikugan.ida.ui.panel",
        "rikugan.core.sanitize",
    )

    def test_subprocess_import_isolation(self) -> None:
        """Run a clean subprocess that imports only ``early_log`` and asserts
        that no heavy rikugan submodule lands in ``sys.modules``.
        """
        forbidden_list = ", ".join(repr(m) for m in self._FORBIDDEN)
        snippet = textwrap.dedent(
            f"""
            import sys
            import rikugan.core.early_log  # noqa: F401
            leaked = [
                n for n in sys.modules
                if n in ({forbidden_list})
            ]
            if leaked:
                raise AssertionError(f"early_log pulled in heavy modules: {{leaked}}")
            p = rikugan.core.early_log._early_log_path()
            if not p.endswith("early_startup.log"):
                raise AssertionError(f"unexpected path: {{p}}")
            print("OK")
            """
        )
        # Spawn with PYTHONPATH pointed at the repo root (the directory that
        # contains the ``rikugan/`` package directory) so that the subprocess
        # can import ``rikugan``. A bare ``python -c`` does not implicitly add
        # cwd to ``sys.path`` on Python >= 3.4, so we have to pass it
        # explicitly. Without this the test fails for environment reasons,
        # not for any early_log regression.
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env = os.environ.copy()
        existing_pp = env.get("PYTHONPATH", "")
        sep = os.pathsep
        env["PYTHONPATH"] = repo_root + (sep + existing_pp if existing_pp else "")
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
        )
        self.assertIn("OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
