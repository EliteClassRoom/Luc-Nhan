"""Tests for /case command parsing and dispatch."""

from __future__ import annotations

from pathlib import Path

from lucnhan.core.config import LucNhanConfig
from lucnhan.memory.authority import MemoryAuthorityIssuer
from lucnhan.memory.case_commands import dispatch_case_command, parse_case_command
from lucnhan.memory.case_repository import CaseRepository
from lucnhan.memory.case_service import CaseMemoryService
from lucnhan.memory.manager import MemoryWorkspaceManager
from lucnhan.memory.workspace import (
    FilesystemIdentity,
    IdentityRequest,
)


class TestParseCaseCommand:
    def test_create(self) -> None:
        cmd = parse_case_command("/case create Malware Campaign 2026")
        assert cmd is not None
        assert cmd.action == "create"
        assert "Malware" in cmd.args
        assert "Campaign" in cmd.args

    def test_list(self) -> None:
        cmd = parse_case_command("/case list")
        assert cmd is not None
        assert cmd.action == "list"
        assert cmd.args == ()

    def test_use_none(self) -> None:
        cmd = parse_case_command("/case use none")
        assert cmd is not None
        assert cmd.action == "use"
        assert cmd.args == ("none",)

    def test_not_case_command(self) -> None:
        assert parse_case_command("/memory") is None
        assert parse_case_command("hello") is None

    def test_quoted_name(self) -> None:
        cmd = parse_case_command('/case create "My Case Name"')
        assert cmd is not None
        assert cmd.action == "create"


class TestDispatchCaseCommand:
    def _setup(self, tmp_path: Path):
        config = LucNhanConfig()
        config._config_dir = str(tmp_path)
        manager = MemoryWorkspaceManager(config)
        manager.bind(
            IdentityRequest(
                source_kind="idb",
                idb_path=str(tmp_path / "a.i64"),
                db_instance_id="uuid-a",
                display_name="a.i64",
                filesystem_identity=FilesystemIdentity("vol", "1"),
            )
        )
        cases = CaseRepository(manager._registry, manager.locator)
        case_service = CaseMemoryService(cases)
        issuer = MemoryAuthorityIssuer()
        context = manager.run_context()
        return cases, case_service, manager, issuer, context

    def test_create_and_list(self, tmp_path: Path) -> None:
        cases, case_service, manager, issuer, context = self._setup(tmp_path)

        cmd = parse_case_command("/case create Test Campaign")
        result = dispatch_case_command(
            cmd,
            case_repository=cases,
            case_service=case_service,
            manager=manager,
            authority=issuer.issue(context),
            context=context,
        )
        assert "Created" in result

        cmd2 = parse_case_command("/case list")
        result2 = dispatch_case_command(
            cmd2,
            case_repository=cases,
            case_service=case_service,
            manager=manager,
            authority=issuer.issue(context),
            context=context,
        )
        assert "Test Campaign" in result2

    def test_use_none(self, tmp_path: Path) -> None:
        cases, case_service, manager, issuer, context = self._setup(tmp_path)
        cmd = parse_case_command("/case use none")
        result = dispatch_case_command(
            cmd,
            case_repository=cases,
            case_service=case_service,
            manager=manager,
            authority=issuer.issue(context),
            context=context,
        )
        assert "Cleared" in result

    def test_delete_after_a_vanished_case_reports_not_found(self, tmp_path: Path) -> None:
        """Another tab can delete between resolution and the mutation."""
        from unittest.mock import patch

        cases, case_service, manager, issuer, context = self._setup(tmp_path)
        created = dispatch_case_command(
            parse_case_command("/case create Race"),
            case_repository=cases,
            case_service=case_service,
            manager=manager,
            authority=issuer.issue(context),
            context=context,
        )
        case_id = created.split()[-1].rstrip(".")

        real_get = cases.get_case
        calls = {"n": 0}

        def flaky_get(cid):
            calls["n"] += 1
            return real_get(cid) if calls["n"] == 1 else None

        with patch.object(cases, "get_case", side_effect=flaky_get):
            result = dispatch_case_command(
                parse_case_command(f"/case delete {case_id}"),
                case_repository=cases,
                case_service=case_service,
                manager=manager,
                authority=issuer.issue(context),
                context=context,
            )

        assert "not found" in result.lower()
