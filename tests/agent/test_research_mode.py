"""Tests for research mode: command parsing, note writing, slugs, index."""

from __future__ import annotations

import os
import sys
import threading
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.agent.loop import _parse_user_command
from rikugan.agent.modes.research import (
    ResearchNote,
    ResearchState,
    _generate_index,
    _safe_note_path,
    _slugify,
    write_and_review_note,
)
from rikugan.agent.turn import TurnEvent, TurnEventType
from rikugan.core.errors import CancellationError


class TestParseResearchCommand(unittest.TestCase):
    """Test /research command parsing."""

    def test_research_command_parsed(self):
        cmd = _parse_user_command("/research analyze network protocol")
        self.assertTrue(cmd.use_research_mode)
        self.assertEqual(cmd.message, "analyze network protocol")

    def test_research_case_insensitive(self):
        cmd = _parse_user_command("/Research Analyze crypto")
        self.assertTrue(cmd.use_research_mode)
        self.assertEqual(cmd.message, "Analyze crypto")

    def test_non_research_command(self):
        cmd = _parse_user_command("just a question")
        self.assertFalse(cmd.use_research_mode)

    def test_explore_not_research(self):
        cmd = _parse_user_command("/explore check strings")
        self.assertFalse(cmd.use_research_mode)
        self.assertTrue(cmd.use_exploration_mode)


class TestSlugify(unittest.TestCase):
    """Test the _slugify helper."""

    def test_basic(self):
        self.assertEqual(_slugify("Socket Initialization"), "socket-initialization")

    def test_special_chars(self):
        self.assertEqual(_slugify("C2 — Command & Control!"), "c2-command-control")

    def test_unicode(self):
        self.assertEqual(_slugify("résumé café"), "resume-cafe")

    def test_empty(self):
        self.assertEqual(_slugify(""), "untitled")

    def test_whitespace_only(self):
        self.assertEqual(_slugify("   "), "untitled")

    def test_hyphens_collapsed(self):
        self.assertEqual(_slugify("foo - bar -- baz"), "foo-bar-baz")


class TestResearchNote(unittest.TestCase):
    """Test ResearchNote dataclass."""

    def test_defaults(self):
        note = ResearchNote(
            genre="networking",
            title="Socket Init",
            slug="socket-init",
            path="/tmp/notes/networking/socket-init.md",
            content="# Socket Init\n\nContent here.",
        )
        self.assertFalse(note.reviewed)
        self.assertFalse(note.review_passed)
        self.assertEqual(note.related_notes, [])


class TestGenerateIndex(unittest.TestCase):
    """Test index.md generation."""

    def test_empty_notes(self):
        state = ResearchState(notes_dir="/tmp/notes")
        index = _generate_index(state, "firmware.bin", "analyze protocol")
        self.assertIn("# Research Index", index)
        self.assertIn("firmware.bin", index)

    def test_with_notes(self):
        state = ResearchState(notes_dir="/tmp/notes")
        state.notes_written = [
            ResearchNote(
                genre="networking",
                title="Socket Initialization",
                slug="socket-initialization",
                path="/tmp/notes/networking/socket-initialization.md",
                content="# Socket Init",
                review_passed=True,
            ),
            ResearchNote(
                genre="crypto",
                title="Key Derivation",
                slug="key-derivation",
                path="/tmp/notes/crypto/key-derivation.md",
                content="# Key Derivation",
                review_passed=False,
            ),
        ]
        index = _generate_index(state, "firmware.bin", "analyze protocol")
        self.assertIn("## Networking", index)
        self.assertIn("## Crypto", index)
        self.assertIn("[[socket-initialization]]", index)
        self.assertIn("[[key-derivation]]", index)
        self.assertIn("(needs review)", index)

    def test_genres_sorted(self):
        state = ResearchState(notes_dir="/tmp/notes")
        state.notes_written = [
            ResearchNote(genre="zz-misc", title="A", slug="a", path="", content=""),
            ResearchNote(genre="aa-init", title="B", slug="b", path="", content=""),
        ]
        index = _generate_index(state, "test.bin", "goal")
        aa_pos = index.index("Aa Init")
        zz_pos = index.index("Zz Misc")
        self.assertLess(aa_pos, zz_pos)


class TestTurnEvents(unittest.TestCase):
    """Test new TurnEvent factory methods for research mode."""

    def test_research_note_saved_event(self):
        ev = TurnEvent.research_note_saved(
            title="Socket Init",
            genre="networking",
            path="/tmp/notes/networking/socket-init.md",
            preview="The binary initializes...",
            review_passed=True,
        )
        self.assertEqual(ev.type, TurnEventType.RESEARCH_NOTE_SAVED)
        self.assertEqual(ev.text, "Socket Init")
        self.assertEqual(ev.metadata["genre"], "networking")
        self.assertTrue(ev.metadata["review_passed"])

    def test_research_note_reviewed_event(self):
        ev = TurnEvent.research_note_reviewed(
            title="Socket Init",
            passed=False,
            feedback="Missing evidence for claim X",
        )
        self.assertEqual(ev.type, TurnEventType.RESEARCH_NOTE_REVIEWED)
        self.assertFalse(ev.metadata["passed"])
        self.assertIn("Missing evidence", ev.metadata["feedback"])


class TestNoteWriting(unittest.TestCase):
    """Test that notes are written to disk correctly."""

    def test_write_note_to_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            notes_dir = os.path.join(tmpdir, "notes")
            os.makedirs(notes_dir)
            genre_dir = os.path.join(notes_dir, "networking")
            os.makedirs(genre_dir)

            note_path = os.path.join(genre_dir, "socket-init.md")
            content = "# Socket Init\n\n## Summary\n\nTest content with [[wiki-link]]."
            with open(note_path, "w", encoding="utf-8") as f:
                f.write(content)

            # Verify written
            with open(note_path, encoding="utf-8") as f:
                read_content = f.read()
            self.assertEqual(read_content, content)
            self.assertIn("[[wiki-link]]", read_content)


class TestSafeNotePath(unittest.TestCase):
    """Test _safe_note_path prevents path traversal attacks.

    Genre and slug come from LLM tool calls. Without validation, a
    malicious prompt could write files outside the notes directory.
    Defense layers:
      1. _slugify strips path separators and dot-sequences.
      2. Null bytes are rejected outright.
      3. Path.resolve() + relative_to() catches anything that slips past.
    """

    def test_normal_genre_and_slug(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _safe_note_path(tmpdir, "networking", "Socket Init")
            self.assertTrue(path.startswith(str(_resolve(tmpdir))))
            self.assertTrue(str(path).endswith(os.path.join("networking", "socket-init.md")))

    def test_traversal_in_genre_is_sanitized(self):
        """../../etc gets sanitized by _slugify into 'etc' — still safe."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _safe_note_path(tmpdir, "../../etc", "passwd")
            # Resolved path is under tmpdir/etc/passwd.md — safe
            self.assertTrue(path.startswith(str(_resolve(tmpdir))))
            self.assertIn(os.path.join("etc", "passwd.md"), path)

    def test_traversal_in_slug_is_sanitized(self):
        """../../../sensitive gets sanitized into 'sensitive' — still safe."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _safe_note_path(tmpdir, "networking", "../../../sensitive")
            self.assertTrue(path.startswith(str(_resolve(tmpdir))))
            self.assertIn("sensitive.md", path)

    def test_only_dots_falls_back_to_untitled(self):
        """A title like '..' or '...' must NOT escape the notes dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # _slugify("..") returns "" → "untitled"
            path = _safe_note_path(tmpdir, "networking", "..")
            self.assertTrue(path.startswith(str(_resolve(tmpdir))))
            self.assertIn("untitled.md", path)

    def test_genre_with_dangerous_chars_sanitized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Slashes and backslashes stripped by _slugify
            path = _safe_note_path(tmpdir, "net/working\\crypto", "title")
            self.assertTrue(path.startswith(str(_resolve(tmpdir))))
            # After tmpdir prefix, no path separators should remain
            rel = os.path.relpath(path, str(_resolve(tmpdir)))
            self.assertNotIn("..", rel)

    def test_drive_letter_genre_sanitized(self):
        """Windows-style 'C:\\Windows' must NOT escape via drive letter.

        _slugify strips non-word characters (including \\, :, .) so the
        drive letter and path separators are removed. Result is a plain
        alphanumeric name like 'cwindowssystem32' — still inside notes_dir.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _safe_note_path(tmpdir, "C:\\Windows\\System32", "evil")
            self.assertTrue(path.startswith(str(_resolve(tmpdir))))
            # The full path is `<tmpdir>/cwindowssystem32/evil.md` — safe.
            self.assertTrue(path.endswith("cwindowssystem32" + os.sep + "evil.md"))

    def test_null_byte_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(ValueError):
                _safe_note_path(tmpdir, "networking\x00", "title")
            with self.assertRaises(ValueError):
                _safe_note_path(tmpdir, "networking", "title\x00.md")

    def test_symlink_escape_blocked(self):
        """If notes_dir contains a symlink pointing outside, traversal is blocked.

        Skip on platforms where unprivileged symlink creation fails
        (Windows requires admin or developer mode).
        """
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks not supported")
        if sys.platform == "win32":
            # Windows: unprivileged symlink creation often fails.
            # Best-effort probe; skip if it doesn't work.
            test_dir = tempfile.mkdtemp()
            try:
                test_link = os.path.join(test_dir, "probe")
                test_target = tempfile.mkdtemp()
                try:
                    os.symlink(test_target, test_link)
                except (OSError, NotImplementedError):
                    self.skipTest("symlink creation requires privileges on this Windows install")
                finally:
                    import shutil

                    shutil.rmtree(test_target, ignore_errors=True)
            finally:
                import shutil

                shutil.rmtree(test_dir, ignore_errors=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            sibling = tempfile.mkdtemp()
            try:
                os.symlink(sibling, os.path.join(tmpdir, "escape"))
                # The symlink name is "escape" — _slugify keeps it.
                # resolve() follows the symlink to sibling/, which is outside
                # notes_root. This MUST raise.
                with self.assertRaises(ValueError):
                    _safe_note_path(tmpdir, "escape", "foo")
            finally:
                import shutil

                shutil.rmtree(sibling, ignore_errors=True)


def _resolve(path: str) -> os.PathLike[str]:
    """Resolve a path to its absolute form (helper for assertions)."""
    import pathlib

    return pathlib.Path(path).resolve()


class TestWriteAndReviewNotePathSafety(unittest.TestCase):
    """Integration test: write_and_review_note must enforce safe paths.

    Uses a mocked SubagentRunner to avoid LLM calls.
    """

    def setUp(self):
        # Stub runner factory — the review pipeline won't actually run
        # beyond the first yield when the path is invalid (ValueError
        # is raised eagerly, before the runner is invoked).
        self.runner = MagicMock()
        self.runner_factory = MagicMock(return_value=self.runner)

    def _drive_one_step(self, state, genre, title, content):
        """Call write_and_review_note, advance one generator step.

        Returns (event, exception) where exception is None if successful,
        a ValueError if path was blocked, or a StopIteration if the
        generator completed naturally.
        """
        gen = write_and_review_note(
            state=state,
            genre=genre,
            title=title,
            content=content,
            related_notes=[],
            runner_factory=self.runner_factory,
        )
        try:
            ev = gen.send(None)
        except ValueError as exc:
            return None, exc
        except StopIteration:
            return None, None  # generator completed — file was written
        return ev, None

    def test_traversal_genre_writes_inside_notes_dir(self):
        """../../etc gets sanitized to 'etc' — file ends up under notes_dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = ResearchState(notes_dir=tmpdir)
            ev, exc = self._drive_one_step(state, "../../etc", "evil", "content")
            # Function should NOT raise — the slugify layer sanitized
            # the input into a safe name.
            self.assertIsNone(exc)
            # File should exist somewhere under tmpdir
            files = []
            for root, _, fnames in os.walk(tmpdir):
                for fn in fnames:
                    files.append(os.path.join(root, fn))
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].endswith(os.path.join("etc", "evil.md")))

    def test_traversal_slug_writes_inside_notes_dir(self):
        """../../../etc/passwd gets sanitized to 'etcpasswd' — file under notes_dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = ResearchState(notes_dir=tmpdir)
            ev, exc = self._drive_one_step(state, "networking", "../../../etc/passwd", "content")
            self.assertIsNone(exc)
            files = []
            for root, _, fnames in os.walk(tmpdir):
                for fn in fnames:
                    files.append(os.path.join(root, fn))
            self.assertEqual(len(files), 1)
            # Slugify stripped '..', '/' → "etcpasswd" becomes the slug
            self.assertTrue(files[0].endswith(os.path.join("networking", "etcpasswd.md")))

    def test_null_byte_genre_raises_value_error(self):
        """Null bytes are an unambiguous attack — they must be rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = ResearchState(notes_dir=tmpdir)
            ev, exc = self._drive_one_step(state, "networking\x00evil", "title", "content")
            self.assertIsInstance(exc, ValueError)

    def test_no_file_written_outside_notes_dir(self):
        """For ANY input, no file must appear outside the notes_dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Sibling dir must remain empty regardless of what we pass
            sibling = tempfile.mkdtemp()
            try:
                state = ResearchState(notes_dir=tmpdir)
                # Drive several malicious inputs
                malicious = [
                    ("../../etc", "evil"),
                    ("networking", "../../../passwd"),
                    ("net/working", "title"),
                    ("networking\x00", "title"),
                ]
                for genre, title in malicious:
                    try:
                        ev, exc = self._drive_one_step(state, genre, title, "x")
                    except Exception:
                        pass
                # Sibling must be empty — no traversal succeeded
                self.assertEqual(os.listdir(sibling), [])
            finally:
                import shutil

                shutil.rmtree(sibling, ignore_errors=True)

    def test_symlink_escape_blocked(self):
        """If a symlink inside notes_dir points outside, traversal is blocked.

        Defense-in-depth check: _slugify can't catch this because the
        symlink name itself is a valid string. resolve() must follow the
        symlink and the containment check must fire.
        """
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks not supported")
        if sys.platform == "win32":
            test_dir = tempfile.mkdtemp()
            try:
                test_link = os.path.join(test_dir, "probe")
                test_target = tempfile.mkdtemp()
                try:
                    os.symlink(test_target, test_link)
                except (OSError, NotImplementedError):
                    self.skipTest("symlink creation requires privileges on this Windows install")
                finally:
                    import shutil

                    shutil.rmtree(test_target, ignore_errors=True)
            finally:
                import shutil

                shutil.rmtree(test_dir, ignore_errors=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            sibling = tempfile.mkdtemp()
            try:
                os.symlink(sibling, os.path.join(tmpdir, "escape"))
                state = ResearchState(notes_dir=tmpdir)
                ev, exc = self._drive_one_step(state, "escape", "foo", "x")
                self.assertIsInstance(exc, ValueError)
                self.assertIn("traversal", str(exc).lower())
                # Sibling must remain empty
                self.assertEqual(os.listdir(sibling), [])
            finally:
                import shutil

                shutil.rmtree(sibling, ignore_errors=True)


def _review_run_result(text: str):
    """Generator mimicking SubagentRunner.run_task's return protocol."""
    if False:  # pragma: no cover - generator marker
        yield None
    return text


class TestWriteAndReviewNoteCancellation(unittest.TestCase):
    """Cancellation must abort the note pipeline between child runs."""

    def _drive(self, runner, loop):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = ResearchState(notes_dir=tmpdir)
            gen = write_and_review_note(
                state=state,
                genre="general",
                title="note",
                content="content",
                related_notes=[],
                runner_factory=lambda: runner,
                loop=loop,
            )
            with self.assertRaises(CancellationError):
                list(gen)

    def test_cancel_after_review_aborts_before_next_child(self):
        loop = MagicMock()
        loop._cancelled = threading.Event()
        calls = []

        class _Runner:
            def run_task(self, prompt, max_turns=20, silent=False):
                calls.append(1)
                if len(calls) == 1:
                    loop._cancelled.set()  # user cancels during the reviewer
                    return _review_run_result("PASS - all claims verified")
                raise AssertionError("pipeline must abort before the next child run")

        self._drive(_Runner(), loop)
        self.assertEqual(len(calls), 1, "no further child run may start after cancellation")

    def test_child_cancellation_error_propagates(self):
        """A CancellationError from a child run must not be swallowed as a pipeline failure."""
        loop = MagicMock()
        loop._cancelled = threading.Event()

        class _Runner:
            def run_task(self, prompt, max_turns=20, silent=False):
                raise CancellationError("cancelled mid child run")

        self._drive(_Runner(), loop)


if __name__ == "__main__":
    unittest.main()
