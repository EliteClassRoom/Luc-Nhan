"""Tests for lucnhan.agent.bulk_renamer pure logic (no LLM/threading)."""

from __future__ import annotations

import pytest

from lucnhan.agent.bulk_renamer import (
    BulkRenamerEngine,
    RenameJob,
    RenameStatus,
)

# ---------------------------------------------------------------------------
# RenameStatus enum
# ---------------------------------------------------------------------------


class TestRenameStatus:
    def test_string_inheritance(self) -> None:
        """Statuses are string-typed so they survive JSON round-trips."""
        assert RenameStatus.PENDING == "pending"
        assert RenameStatus.COMPLETED == "completed"
        assert RenameStatus.FAILED == "failed"

    def test_all_statuses_present(self) -> None:
        expected = {"pending", "decompiling", "analyzing", "renaming", "completed", "skipped", "failed"}
        actual = {s.value for s in RenameStatus}
        assert actual == expected


# ---------------------------------------------------------------------------
# RenameJob dataclass
# ---------------------------------------------------------------------------


class TestRenameJob:
    def test_defaults(self) -> None:
        job = RenameJob(address=0x401000, current_name="sub_401000")
        assert job.address == 0x401000
        assert job.current_name == "sub_401000"
        assert job.new_name == ""
        assert job.status == RenameStatus.PENDING
        assert job.error == ""

    def test_custom_status(self) -> None:
        job = RenameJob(
            address=0x401000,
            current_name="sub_401000",
            new_name="init_main",
            status=RenameStatus.COMPLETED,
        )
        assert job.new_name == "init_main"
        assert job.status == RenameStatus.COMPLETED

    def test_equality(self) -> None:
        """Dataclass equality is field-by-field."""
        a = RenameJob(address=0x1, current_name="sub_1")
        b = RenameJob(address=0x1, current_name="sub_1")
        assert a == b

    def test_inequality(self) -> None:
        a = RenameJob(address=0x1, current_name="sub_1")
        b = RenameJob(address=0x2, current_name="sub_2")
        assert a != b


# ---------------------------------------------------------------------------
# BulkRenamer.should_skip
# ---------------------------------------------------------------------------


class TestShouldSkip:
    @pytest.mark.parametrize(
        "name",
        [
            "sub_401000",
            "sub_DEADBEEF",
            "sub_1a2b3c",
            "FUN_401000",
            "FUN_abc123",
            "func_401000",
            "unnamed_401000",
            "loc_401000",
        ],
    )
    def test_auto_generated_names_are_skipped(self, name: str) -> None:
        """Names matching auto-name patterns should be processed (skipped == True)."""
        assert BulkRenamerEngine.should_skip(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "main",
            "init_main",
            "parse_buffer",
            "substr",  # contains "sub" but not the sub_<hex> pattern
            "subroutine_a",  # "sub" but not "sub_<hex>"
            "sub_",  # missing hex
            "sub_g",  # "g" is not hex
            "my_sub_123",  # not anchored at start
            "",
        ]
    )
    def test_human_names_are_not_skipped(self, name: str) -> None:
        """Names that look human-assigned should NOT be processed."""
        assert BulkRenamerEngine.should_skip(name) is False

    def test_does_not_modify_input(self) -> None:
        """Pure function — must not mutate the argument."""
        name = "sub_401000"
        original_id = id(name)
        BulkRenamerEngine.should_skip(name)
        assert id(name) == original_id

    def test_returns_bool(self) -> None:
        """Static type contract — callers rely on bool truthiness."""
        assert isinstance(BulkRenamerEngine.should_skip("sub_401000"), bool)
        assert isinstance(BulkRenamerEngine.should_skip("main"), bool)
