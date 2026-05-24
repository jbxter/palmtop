"""Cursor delegate runner and the agent trigger tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pocket_agent.config.settings import CursorConfig
from pocket_agent.core.loop import AgentLoop
from pocket_agent.cursor.runner import (
    CursorJobManager,
    append_cursor_audit,
    parse_cursor_query,
    parse_cursor_task,
    repo_allowed,
)
from pocket_agent.tools.cursor_delegate import DelegateCursorTool


def test_parse_cursor_task_prefixes() -> None:
    assert parse_cursor_task("cursor: fix readme") == "fix readme"
    assert parse_cursor_task("/cursor add tests") == "add tests"
    assert parse_cursor_task("hello") is None


def test_parse_cursor_query_repo_branch() -> None:
    cfg = CursorConfig(
        default_repo="https://github.com/org/default",
        default_branch="develop",
    )
    repo, branch, prompt = parse_cursor_query(
        "repo=https://github.com/org/other branch=feat/x ship it",
        cfg,
    )
    assert repo == "https://github.com/org/other"
    assert branch == "feat/x"
    assert prompt == "ship it"


def test_repo_allowed_normalizes_trailing_slash() -> None:
    allowed = ["https://github.com/org/repo"]
    assert repo_allowed("https://github.com/org/repo/", allowed)
    assert not repo_allowed("https://github.com/org/other", allowed)


@pytest.mark.asyncio
async def test_launch_rejects_disallowed_repo(tmp_path: Path) -> None:
    client = MagicMock()
    client.create_agent = AsyncMock()
    cfg = CursorConfig(
        enabled=True,
        allowed_repos=["https://github.com/org/allowed"],
        default_repo="https://github.com/org/blocked",
    )
    mgr = CursorJobManager(client, cfg, tmp_path)
    reply = await mgr.launch("do something", user_id="u1")
    assert "not allowed" in reply
    client.create_agent.assert_not_called()


@pytest.mark.asyncio
async def test_launch_writes_audit_and_tracks_job(tmp_path: Path) -> None:
    client = MagicMock()
    client.close = AsyncMock()
    client.create_agent = AsyncMock(
        return_value={
            "agent": {"id": "bc-9", "url": "https://cursor.com/agents/bc-9"},
            "run": {"id": "run-9", "status": "CREATING"},
        }
    )
    cfg = CursorConfig(
        enabled=True,
        allowed_repos=["https://github.com/org/repo"],
        default_repo="https://github.com/org/repo",
        require_blessing=False,
        poll_interval_s=1,
    )
    mgr = CursorJobManager(client, cfg, tmp_path, blessing_gate=None)
    reply = await mgr.launch("fix lint", user_id="42")
    assert "bc-9" in reply
    assert mgr.active_count == 1
    audit = (tmp_path / "cursor_jobs.jsonl").read_text(encoding="utf-8")
    assert "launched" in audit
    assert "bc-9" in audit
    await mgr.close()


@pytest.mark.asyncio
async def test_handle_routes_cursor_prefix(tmp_path: Path) -> None:
    backend = AsyncMock()
    cursor_mgr = MagicMock()
    cursor_mgr.launch = AsyncMock(return_value="Cursor cloud agent started")
    loop = AgentLoop(backend, cursor_manager=cursor_mgr, data_dir=tmp_path)
    reply = await loop.handle("cursor: fix tests", user_id="u1")
    assert "started" in reply
    cursor_mgr.launch.assert_called_once()
    backend.complete.assert_not_called()


@pytest.mark.asyncio
async def test_delegate_tool_calls_manager(tmp_path: Path) -> None:
    mgr = MagicMock()
    mgr.launch = AsyncMock(return_value="ok")
    tool = DelegateCursorTool(mgr)
    tool.set_user_id("99")
    result = await tool.run("add readme")
    assert result == "ok"
    mgr.launch.assert_called_once_with("add readme", user_id="99")


def test_append_cursor_audit(tmp_path: Path) -> None:
    append_cursor_audit(tmp_path, {"status": "launched", "agent_id": "bc-1"})
    lines = (tmp_path / "cursor_jobs.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["agent_id"] == "bc-1"
