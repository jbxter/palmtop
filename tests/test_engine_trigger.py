"""the agent → sovereign engine trigger tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("palmtop.core.loop")

from palmtop.core.loop import AgentLoop
from palmtop.core.orchestration import OrchestrationResult
from palmtop.core.sovereign_runner import parse_engine_task, run_sovereign_engine


def test_parse_engine_task_prefixes() -> None:
    assert parse_engine_task("engine: ship product milestone") == "ship product milestone"
    assert parse_engine_task("engine run weekly review") == "run weekly review"
    assert parse_engine_task("/engine draft copy") == "draft copy"
    assert parse_engine_task("/claude draft copy") == "draft copy"
    assert parse_engine_task("claude: summarize inbox") == "summarize inbox"
    assert parse_engine_task("hello julian") is None


@pytest.mark.asyncio
async def test_handle_routes_engine_prefix(tmp_path: Path) -> None:
    backend = AsyncMock()
    sovereign = MagicMock()
    sovereign.orchestrate_result = MagicMock(
        return_value=OrchestrationResult(
            status="executed",
            output="engine output",
            alignment={"is_aligned": True, "score": 1.0, "method": "heuristic"},
        )
    )
    loop = AgentLoop(
        backend,
        sovereign_engine=sovereign,
        data_dir=tmp_path,
    )
    reply = await loop.handle("engine: test task", user_id="u1")
    assert "engine output" in reply
    sovereign.orchestrate_result.assert_called_once()
    backend.complete.assert_not_called()


@pytest.mark.asyncio
async def test_run_sovereign_engine_blocked(tmp_path: Path) -> None:
    sovereign = MagicMock()
    sovereign.orchestrate_result = MagicMock(
        return_value=OrchestrationResult(
            status="blocked",
            blocked_reason="BLOCKED: off trajectory",
            alignment={"is_aligned": False, "score": 0, "method": "heuristic"},
        )
    )
    reply = await run_sovereign_engine(sovereign, "random task", data_dir=tmp_path, user_id="u1")
    assert "BLOCKED" in reply
    assert (tmp_path / "engine_runs.jsonl").is_file()
