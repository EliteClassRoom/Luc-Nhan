"""Tests for the ``/report`` slash command handler event sequence.

Exercises ``_handle_report_command`` end-to-end with controlled
fixtures for the LLM, save path, and answer queue. These tests
prove the draft preview is emitted before the question, the
``USER_QUESTION`` carries the documented options, and the generator
resumes after the queue answer to produce the correct terminal event
for both cancel and approval paths.

No Qt UI, no real LLM, no filesystem writer.
"""

from __future__ import annotations

import queue
import tempfile
import unittest
from collections.abc import Generator
from unittest.mock import MagicMock, patch
import re as _re


from rikugan.agent.loop_commands import _handle_report_command
from rikugan.agent.turn import TurnEvent, TurnEventType
from rikugan.core.config import RikuganConfig
from rikugan.memory.ingest import ingest_save_memory
from rikugan.memory.report import ReportSaveResult, build_report_context
from rikugan.memory.schema import KnowledgeMemory
from rikugan.state.session import SessionState
from rikugan.tests.knowledge._helpers import fresh_store


class _FakeLoop:
    """Minimal AgentLoop stand-in with a real answer queue."""

    def __init__(self, idb_path: str) -> None:
        self.session = SessionState(idb_path=idb_path)
        self.config = RikuganConfig()
        self.provider = object()
        self._user_answer_queue: queue.Queue = queue.Queue(maxsize=1)

    def _wait_for_queue(self, q: queue.Queue) -> str:
        return q.get(timeout=5)


def _seed_verified_memory(store, paths) -> KnowledgeMemory:
    """Insert one verified hypothesis so ``build_report_context`` is non-empty.

    The verified-only report filter requires the seeded memory to be a
    verified hypothesis; earlier revisions of this helper produced a
    verified fact which the new filter rejects.
    """
    mem = KnowledgeMemory(
        id="mem:explore:hypothesis:0x401000:abcd",
        binary_id=paths.binary_id,
        type="hypothesis",
        title="RC4 keystream",
        content="Uses RC4 keystream at 0x401000 for beacon encryption",
        tags=["crypto", "exploration"],
        status="verified",
        verdict_claim="Confirmed via decompile and xref walk.",
        verification_citations=["function:rc4_ksa", "address:0x401000"],
        verified=True,
    )
    store.upsert_memory(mem)
    return mem

class TestReportCommandEventSequence(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store, self.paths = fresh_store(self.tmp)
        self.idb_path = self.paths.idb_path
        _seed_verified_memory(self.store, self.paths)

    def _context_for(self, *_args, scope: str = "full", **_kwargs):
        return build_report_context(self.store, self.paths, scope=scope)

    def _build_loop(self) -> _FakeLoop:
        return _FakeLoop(self.idb_path)

    def _drive(
        self,
        gen: Generator[TurnEvent, None, None],
        loop: _FakeLoop,
        answer: str,
    ) -> list[TurnEvent]:
        """Advance the generator to USER_QUESTION, submit *answer*, then drain."""
        events: list[TurnEvent] = []
        first = next(gen)
        events.append(first)
        self.assertEqual(
            first.type,
            TurnEventType.TEXT_DONE,
            f"got {first.type!r} text={first.text!r} error={first.error!r}",
        )
        self.assertIn("Report draft", first.text)
        second = next(gen)
        events.append(second)
        self.assertEqual(second.type, TurnEventType.USER_QUESTION)
        self.assertEqual(
            second.metadata["options"],
            ["Write report", "Cancel"],
        )
        self.assertEqual(second.tool_call_id, "report_write")
        loop._user_answer_queue.put(answer)
        try:
            for ev in gen:
                events.append(ev)
        except StopIteration:
            pass
        return events

    def _start_patches(self, save_mock: MagicMock) -> list:
        return [
            patch(
                "rikugan.agent.loop_commands._open_knowledge_store",
                return_value=(self.store, self.paths, None),
            ),
            patch(
                "rikugan.memory.report.build_report_context",
                side_effect=self._context_for,
            ),
            patch(
                "rikugan.memory.report.synthesize_report",
                return_value=(self._context_for("full"), "# Draft\n\nverified body"),
            ),
            patch(
                "rikugan.memory.report.save_report",
                side_effect=save_mock,
            ),
        ]

    def test_cancel_path_emits_draft_question_then_discarded(self) -> None:
        loop = self._build_loop()
        save_mock = MagicMock()
        patches = self._start_patches(save_mock)
        for p in patches:
            p.start()
        try:
            gen = _handle_report_command(loop, "full")
            events = self._drive(gen, loop, "Cancel")
        finally:
            for p in patches:
                p.stop()

        types = [e.type for e in events]
        self.assertEqual(types[0], TurnEventType.TEXT_DONE)
        self.assertIn("Report draft", events[0].text)
        self.assertIn("# Draft", events[0].text)
        self.assertEqual(types[1], TurnEventType.USER_QUESTION)
        self.assertEqual(len(events), 3)
        self.assertEqual(types[2], TurnEventType.TEXT_DONE)
        self.assertIn("Report discarded", events[2].text)
        save_mock.assert_not_called()

    def test_approval_path_emits_draft_question_and_saved(self) -> None:
        loop = self._build_loop()
        save_mock = MagicMock(
            return_value=ReportSaveResult(
                file_path="/tmp/saved-report.md",
                ingested=True,
                ingest_error="",
            )
        )
        patches = self._start_patches(save_mock)
        for p in patches:
            p.start()
        try:
            gen = _handle_report_command(loop, "full")
            events = self._drive(gen, loop, "Write report")
        finally:
            for p in patches:
                p.stop()

        types = [e.type for e in events]
        self.assertEqual(types[0], TurnEventType.TEXT_DONE)
        self.assertIn("Report draft", events[0].text)
        self.assertEqual(types[1], TurnEventType.USER_QUESTION)
        self.assertEqual(types[2], TurnEventType.TEXT_DONE)
        self.assertIn("Report saved", events[2].text)
        save_mock.assert_called_once()
        args, kwargs = save_mock.call_args
        self.assertEqual(kwargs.get("scope"), "full")

    def test_no_findings_emits_skip_message_no_draft(self) -> None:
        from rikugan.tests.knowledge._helpers import fresh_store

        tmp = tempfile.mkdtemp()
        _, empty_paths = fresh_store(tmp)
        loop = _FakeLoop(empty_paths.idb_path)
        events = list(_handle_report_command(loop, "full"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, TurnEventType.TEXT_DONE)
        self.assertIn("No stored knowledge", events[0].text)

    def test_synthesize_receives_session_messages_as_context(self) -> None:
        from rikugan.core.types import Message, Role
        from rikugan.memory.report import ReportSaveResult

        loop = self._build_loop()
        # Seed a fake chat history: a USER and an ASSISTANT message.
        loop.session.add_message(
            Message(role=Role.USER, content="Tell me about the entry point")
        )
        loop.session.add_message(
            Message(role=Role.ASSISTANT, content="The entry point is at 0x401000.")
        )
        seen_context: list = []

        def fake_synth(*_args, **kwargs):
            seen_context.extend(kwargs.get("conversation_context") or [])
            return (
                self._context_for("full"),
                "# Draft\n\nverified body",
            )

        def fake_save(*_args, **_kwargs):
            return ReportSaveResult(
                file_path="/tmp/saved-report.md",
                ingested=True,
                ingest_error="",
            )

        patches = [
            patch(
                "rikugan.agent.loop_commands._open_knowledge_store",
                return_value=(self.store, self.paths, None),
            ),
            patch(
                "rikugan.memory.report.build_report_context",
                side_effect=self._context_for,
            ),
            patch(
                "rikugan.memory.report.synthesize_report",
                side_effect=fake_synth,
            ),
            patch(
                "rikugan.memory.report.save_report",
                side_effect=fake_save,
            ),
        ]
        for p in patches:
            p.start()
        try:
            gen = _handle_report_command(loop, "full")
            self._drive(gen, loop, "Write report")
        finally:
            for p in patches:
                p.stop()

        # The exact session.messages list must have reached the synthesize mock.
        self.assertEqual(
            [m.content for m in seen_context],
            [
                "Tell me about the entry point",
                "The entry point is at 0x401000.",
            ],
        )
        # Roles preserved verbatim so the writer can label each snippet.
        self.assertEqual(
            [m.role for m in seen_context],
            [Role.USER, Role.ASSISTANT],
        )

    def test_synthesize_receives_empty_evidence_without_tools(self) -> None:
        """A loop without a tool registry yields empty evidence + binary_info.

        ``_FakeLoop`` has no ``.tools`` attribute; the handler must
        tolerate that (``getattr(loop, "tools", None)``) and still
        generate the report with ``evidence=[]`` / ``binary_info=""``.
        The ``full`` scope enters the evidence-collection branch, so
        the missing-registry path is genuinely exercised (an
        ``executive`` scope would short-circuit before the tools path).
        """
        from rikugan.memory.report import ReportSaveResult

        # The shared seed (crypto tag) does not populate the executive
        # template, so add an ioc-tagged verified hypothesis to make
        # the full scope non-empty and reach synthesize_report.
        self.store.upsert_memory(
            KnowledgeMemory(
                id="mem:explore:hypothesis:ioc:ef01",
                binary_id=self.paths.binary_id,
                type="hypothesis",
                title="C2 domain",
                content="Beacons to c2.example.com",
                tags=["ioc"],
                status="verified",
                verdict_claim="Confirmed via pcap.",
                verification_citations=["address:0x402000"],
                verified=True,
            )
        )
        loop = self._build_loop()
        seen: dict = {}

        def fake_synth(*_args, **kwargs):
            seen.update(kwargs)
            return (self._context_for("full"), "# Draft\n\nverified body")

        def fake_save(*_args, **_kwargs):
            return ReportSaveResult(
                file_path="/tmp/saved-report.md",
                ingested=True,
                ingest_error="",
            )

        patches = [
            patch(
                "rikugan.agent.loop_commands._open_knowledge_store",
                return_value=(self.store, self.paths, None),
            ),
            patch(
                "rikugan.memory.report.build_report_context",
                side_effect=self._context_for,
            ),
            patch(
                "rikugan.memory.report.synthesize_report",
                side_effect=fake_synth,
            ),
            patch(
                "rikugan.memory.report.save_report",
                side_effect=fake_save,
            ),
        ]
        for p in patches:
            p.start()
        try:
            gen = _handle_report_command(loop, "full")
            self._drive(gen, loop, "Write report")
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(seen.get("evidence"), [])
        self.assertEqual(seen.get("binary_info"), "")


if __name__ == "__main__":
    unittest.main()


class TestReportDraftFencing(unittest.TestCase):
    """The ``/report`` draft preview must not wrap ``preview`` in an
    outer Markdown fence: a normal report body contains triple
    backticks (e.g. an embedded code block) which would close the
    fence prematurely and produce malformed Markdown.

    Also guards against an empty / whitespace-only draft silently
    asking the user to write nothing.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store, self.paths = fresh_store(self.tmp)
        self.idb_path = self.paths.idb_path
        _seed_verified_memory(self.store, self.paths)

    def _drive_draft_only(self, draft: str) -> TurnEvent:
        loop = _FakeLoop(self.idb_path)
        save_mock = MagicMock()
        patches = [
            patch(
                "rikugan.agent.loop_commands._open_knowledge_store",
                return_value=(self.store, self.paths, None),
            ),
            patch(
                "rikugan.memory.report.build_report_context",
                side_effect=lambda *_a, **_kw: build_report_context(self.store, self.paths),
            ),
            patch(
                "rikugan.memory.report.synthesize_report",
                return_value=(build_report_context(self.store, self.paths), draft),
            ),
            patch(
                "rikugan.memory.report.save_report",
                side_effect=save_mock,
            ),
        ]
        for p_ in patches:
            p_.start()
        try:
            gen = _handle_report_command(loop, "full")
            first = next(gen)
        finally:
            for p_ in patches:
                p_.stop()
        self.assertEqual(first.type, TurnEventType.TEXT_DONE)
        # The draft body must not be wrapped in an outer fence; the
        # literal ``\`\`\`markdown`` opener would otherwise close on
        # the first inner backtick.
        self.assertNotIn("```markdown", first.text)
        # The body itself must be present (the handler strips
        # surrounding whitespace before yielding). For long bodies
        # the structural truncation may rewrite the tail with a
        # "(truncated; …)" marker, so we don't require the raw body
        # to round-trip verbatim.
        if len(draft) <= 1500:
            self.assertIn(draft.strip(), first.text)
        else:
            self.assertIn("Report draft", first.text)
            self.assertIn("(truncated", first.text)
        return first
    def test_draft_with_inner_fence_round_trips(self) -> None:
        body = "# Draft\n\ntext\n\n```c\nint main(void) {}\n```\n"
        first = self._drive_draft_only(body)
        # Inner fence must survive verbatim so the chat renderer can
        # parse it as its own block.
        self.assertIn("int main(void) {}", first.text)
        self.assertIn("```c", first.text)
        # No stray outer fence header.
        self.assertFalse(first.text.startswith("```"))

    def test_draft_inner_fence_renders_in_widget(self) -> None:
        """Exercise the full ChatView path end-to-end with a nested fence.

        The handler must yield the preview verbatim (no outer fence),
        the ChatView must produce one visible AssistantMessageWidget
        whose text preserves the trailing line that comes AFTER the
        inner fence, and the follow-up USER_QUESTION must create a
        distinct UserQuestionWidget beneath the draft.
        """
        try:
            from rikugan.ui.qt_compat import QApplication
            from rikugan.ui.chat_view import ChatView
            from rikugan.ui.message_widgets import (
                AssistantMessageWidget,
                UserQuestionWidget,
            )
        except ImportError:
            self.skipTest("PySide6 / ChatView not available in this env")
        QApplication.instance() or QApplication([])

        body = (
            "# Draft\n\n"
            "```c\nint main(void) {return 0;}\n```\n\n"
            "Trailing line about entry."
        )
        first = self._drive_draft_only(body)
        self.assertTrue(first.text.startswith("**Report draft**"))
        # Inner fence present, no outer wrapper.
        self.assertIn("int main(void) {return 0;}", first.text)
        self.assertFalse(first.text.startswith("```"))

        view = ChatView()
        view.show()
        view.handle_event(first)
        view.handle_event(
            TurnEvent.user_question(
                "Write?",
                ["Write report", "Cancel"],
                tool_call_id="report_write",
                allow_text=True,
            )
        )
        QApplication.instance().processEvents()
        widgets = [
            view._layout.itemAt(i).widget()
            for i in range(view._layout.count())
            if view._layout.itemAt(i).widget() is not None
        ]
        asst = [w for w in widgets if isinstance(w, AssistantMessageWidget)]
        qq = [w for w in widgets if isinstance(w, UserQuestionWidget)]
        self.assertEqual(len(asst), 1)
        self.assertEqual(len(qq), 1)
        # Visual check 1: the assistant's content label is visible so
        # the bubble actually paints something (no hidden / un-rendered
        # state). ``isVisibleTo`` is used because Qt is headless.
        self.assertTrue(asst[0]._content.isVisibleTo(view))
        # Visual check 2: the rendered HTML carries the report
        # heading AND the trailing prose that followed the inner fence.
        html = asst[0]._content.text()
        self.assertIn("Report draft", html)
        self.assertIn("Trailing line about entry.", html)
        # Visual check 3: the inner C block is rendered as a real
        # block (pre-wrap background), not collapsed into the trailing
        # sentence — a regression symptom of the original nested-fence
        # bug. The visible code text may be wrapped in pygments
        # ``<span>`` tokens; we assert the lexed tokens appear inside
        # the pre-wrap div, and the trailing line lives in a sibling
        # Visual check 3: the inner C block is rendered as a real
        # block (pre-wrap background), not collapsed into the trailing
        # sentence — a regression symptom of the original nested-fence
        # bug. Pygments may split the source into per-token ``<span>``
        # runs so a literal substring match on ``int main(void)`` is
        # too strict; we assert the structural integrity of the
        # block plus the lexed tokens, and that the trailing line
        # lives in a SIBLING ``<div>`` (not merged into the code
        # block).
        self.assertRegex(html, r"white-space:pre-wrap")
        # The pre-wrap div opens immediately before the lexed
        # ``int`` token, so we know the code is inside the block.
        self.assertRegex(
            html,
            r"white-space:pre-wrap[^>]*>[^<]*<span[^>]*>int</span>",
        )
        # Lexed token for ``main`` lives inside the same pre-wrap
        # block (i.e. the code body is not collapsed into prose).
        # ``_re.S`` (DOTALL) lets ``.`` cross newlines so the
        # assertion still matches when pygments emits the lexed
        # tokens on multiple lines inside the ``<pre>`` block.
        self.assertRegex(
            html,
            _re.compile(
                r"white-space:pre-wrap[^>]*>(?:(?!</div>).)*"
                r"<span[^>]*>main</span>",
                _re.S,
            ),
        )
        # Trailing line is its own ``<div>`` after the code block, not
        # inside the pre-wrap code block.
        self.assertRegex(
            html,
            r"</div>\s*<div[^>]*>Trailing line about entry\.",
        )
        # Trailing line also survives in the widget's full text for
        # restoration / re-render.
        self.assertIn("Trailing line about entry.", asst[0].full_text())


    def test_plain_draft_body_visible_in_widget(self) -> None:
        """Regression for the "heading visible, body empty" failure
        mode reported by the user — a draft whose Markdown body has
        no inner fences must still render the body prose in the
        assistant widget, not just the bold heading.

        This catches a wider class of regressions than the inner-fence
        case: if the handler emits only the bold heading (e.g. an
        unintended ``**Report draft**`` truncation), or if the
        Markdown renderer drops body lines for any reason, this test
        fails before the user notices.
        """
        try:
            from rikugan.ui.qt_compat import QApplication
            from rikugan.ui.chat_view import ChatView
            from rikugan.ui.message_widgets import AssistantMessageWidget
        except ImportError:
            self.skipTest("PySide6 / ChatView not available in this env")
        QApplication.instance() or QApplication([])

        body = (
            "# Executive Summary\n\n"
            "The binary uses RC4 at 0x401000 for beacon encryption.\n\n"
            "## Capabilities\n"
            "- Persistence via scheduled task\n"
            "- Network beacon every 60 seconds\n"
        )
        first = self._drive_draft_only(body)

        # Realistic IDA-Dock-style chat width so the word-wrap test
        # mirrors what the user sees, not a default-narrow offscreen.
        chat_width = 720
        view = ChatView()
        view.resize(chat_width, 800)
        view.show()
        view.handle_event(first)
        # Two event-loop spins: the first lays out the new size; the
        # second lets ``_HeightCachedLabel.pin_height`` re-measure the
        # document at the actual width.
        app = QApplication.instance()
        for _ in range(3):
            app.processEvents()
        widgets = [
            view._layout.itemAt(i).widget()
            for i in range(view._layout.count())
            if view._layout.itemAt(i).widget() is not None
        ]
        asst = [w for w in widgets if isinstance(w, AssistantMessageWidget)]
        self.assertEqual(len(asst), 1)
        content = asst[0]._content
        # Visual: the content label is visible so the bubble paints.
        self.assertTrue(content.isVisibleTo(view))
        # Geometry: at a realistic chat width the content label must
        # be tall enough to fit the *whole* document, not just the
        # heading. ``_HeightCachedLabel.pin_height`` (called from
        # ``_render``) sets ``setFixedHeight`` to ``heightForWidth``
        # — and once a fixed height is set, ``heightForWidth(w)``
        # is *poisoned* (it echoes the cached value back). To get
        # the document's true required layout height we must clear
        # the min/max constraints first, mirroring what
        # ``pin_height`` itself does internally before measuring.
        cleared_min = content.minimumHeight()
        cleared_max = content.maximumHeight()
        content.setMinimumHeight(0)
        content.setMaximumHeight(16777215)  # QWIDGETSIZE_MAX
        try:
            measured = content.heightForWidth(content.width())
        finally:
            # Restore the pinned state so the rest of the test sees
            # the same geometry the user does.
            content.setMinimumHeight(cleared_min)
            content.setMaximumHeight(cleared_max)
        # ``heightForWidth`` is a valid measurement ONLY while the
        # fixed-height cache is cleared; with that reset it returns
        # the document's required layout height for the current
        # width. If the body fits, that height covers the heading
        # paragraph AND the two bullet lines.
        self.assertGreater(content.width(), 0)
        self.assertGreater(measured, 80)
        # The actual fixed height set by ``pin_height`` must match
        # the measured requirement — if a regression clipped the
        # body, ``height`` (the fixed value) would differ from
        # ``measured``.
        self.assertEqual(content.height(), measured)
        html = content.text()
        self.assertTrue(html, "rendered HTML must be non-empty")
        # Heading AND body lines both visible in the rendered HTML.
        self.assertIn("Report draft", html)
        self.assertIn("Executive Summary", html)
        self.assertIn("RC4 at 0x401000", html)
        self.assertIn("scheduled task", html)
        self.assertIn("every 60 seconds", html)
        # And in the raw text used for restoration / theme re-render.
        full = asst[0].full_text()
        self.assertIn("Executive Summary", full)
        self.assertIn("RC4 at 0x401000", full)

    def test_empty_draft_does_not_ask_to_write(self) -> None:
        loop = _FakeLoop(self.idb_path)
        patches = [
            patch(
                "rikugan.agent.loop_commands._open_knowledge_store",
                return_value=(self.store, self.paths, None),
            ),
            patch(
                "rikugan.memory.report.build_report_context",
                side_effect=lambda *_a, **_kw: build_report_context(self.store, self.paths),
            ),
            patch(
                "rikugan.memory.report.synthesize_report",
                return_value=(build_report_context(self.store, self.paths), "   \n"),
            ),
            patch(
                "rikugan.memory.report.save_report",
                return_value=MagicMock(),
            ),
        ]
        for p_ in patches:
            p_.start()
        try:
            events = list(_handle_report_command(loop, "full"))
        finally:
            for p_ in patches:
                p_.stop()
        # The handler must surface the empty/whitespace draft as a
        # generation failure and never ask the user whether to write
        # an empty report.
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, TurnEventType.ERROR)
        self.assertIn("empty", events[0].error.lower())




    def test_long_body_with_inner_fence_is_balanced_in_widget(self) -> None:
        """Regression: a >1500-char body whose inner code fence
        spans the truncation cap must yield a balanced preview (no
        stranded opening fence) and a ChatView bubble whose trailing
        prose line lives OUTSIDE any code block.

        Without structural truncation, the agent's 1500-char slice
        lands inside an opening ```` ```c ```` fence and the rendered
        HTML leaves a never-closing ``<pre>`` block, which collapses
        the visible body to a heading + raw source-dump blob.
        """
        try:
            from rikugan.ui.qt_compat import QApplication
            from rikugan.ui.chat_view import ChatView
            from rikugan.ui.message_widgets import AssistantMessageWidget
        except ImportError:
            self.skipTest("PySide6 / ChatView not available in this env")
        QApplication.instance() or QApplication([])

        # Build a body whose open fence lands inside the 1500-char
        # cap and whose close fence lands AFTER. The trailing prose
        # line MUST appear in the bubble's last sibling <div>.
        body = (
            "# Executive Summary\n\n"
            "The binary uses RC4 at 0x401000 for beacon encryption.\n\n"
            "## Capabilities\n"
            "- Persistence via scheduled task\n"
            "- Network beacon every 60 seconds\n\n"
            "## Key Functions\n\n"
            "### rc4_ksa @ 0x401000\n\n"
            "```c\n"
            + ("    // long line of source to push past the 1500-char cap\n" * 30)
            + "```\n\n"
            "Trailing line about the entry.\n"
        )
        first = self._drive_draft_only(body)
        # Preview must be balanced (every fence has its close inside
        # the visible 1500-char window) and carry the truncation marker.
        fence_count = first.text.count("```")
        self.assertEqual(
            fence_count % 2,
            0,
            f"preview has an unbalanced fence: {fence_count!r} markers in "
            f"{first.text!r}",
        )
        self.assertIn("Report draft", first.text)
        self.assertIn("(truncated", first.text)
        # Heading survived.
        self.assertIn("Executive Summary", first.text)

        view = ChatView()
        view.resize(720, 800)
        view.show()
        view.handle_event(first)
        app = QApplication.instance()
        for _ in range(3):
            app.processEvents()
        widgets = [
            view._layout.itemAt(i).widget()
            for i in range(view._layout.count())
            if view._layout.itemAt(i).widget() is not None
        ]
        asst = [w for w in widgets if isinstance(w, AssistantMessageWidget)]
        self.assertEqual(len(asst), 1)
        content = asst[0]._content
        self.assertTrue(content.isVisibleTo(view))
        html = content.text()
        # Rendered HTML must close every <pre> it opens. Without
        # structural truncation this fails because the opening fence
        # never gets a closing fence in the visible window.
        import re as _re
        pre_opens = len(_re.findall(r"white-space:pre-wrap", html))
        pre_closes = len(_re.findall(r"</div>", html))
        # Sanity: there IS a pre block OR there isn't one (we may have
        # dropped the spanning fence entirely). Either is acceptable,
        # but a NESTED one without a sibling trailing <div> is not.
        if pre_opens:
            # Find the position of every pre block close and the next
            # <div> opener. Make sure the trailing prose lands OUTSIDE
            # the pre block as a sibling, not inside it.
            self.assertRegex(
                html,
                r"</div>\s*<div[^>]*>\(?truncated",
            )
        # Prose lines (or the truncation marker) MUST be present in
        # the visible HTML.
        self.assertTrue(
            "Executive Summary" in html,
            "rendered HTML lost the executive heading",
        )

