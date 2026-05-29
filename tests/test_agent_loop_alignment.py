"""AgentLoop alignment tests — the agent never blocks, engine does."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("palmtop.core.goal_aligner")

from palmtop.core.goal_aligner import GoalAligner
from palmtop.core.loop import AgentLoop
from palmtop.core.tracing import Tracer


@pytest.fixture
def goals_file(tmp_path: Path) -> Path:
    p = tmp_path / "goals.json"
    p.write_text(
        json.dumps({"goals": [{"tag": "product", "title": "Ship engine"}]}),
        encoding="utf-8",
    )
    return p


@pytest.mark.asyncio
async def test_misaligned_still_gets_reply(goals_file: Path) -> None:
    """the agent never blocks — misaligned messages still reach the LLM."""
    backend = AsyncMock()
    backend.complete = AsyncMock(return_value="Here's the weather in Tokyo.")
    aligner = GoalAligner(goals_file, use_semantic=False)
    loop = AgentLoop(
        backend,
        goal_aligner=aligner,
        alignment_mode="hard",
        tracer=Tracer(enabled=False),
    )
    reply = await loop.handle("weather in tokyo today")
    backend.complete.assert_called()
    assert reply == "Here's the weather in Tokyo."


@pytest.mark.asyncio
async def test_aligned_task_passes(goals_file: Path) -> None:
    """Aligned tasks reach the LLM normally."""
    backend = AsyncMock()
    backend.complete = AsyncMock(return_value="Product shipped.")
    aligner = GoalAligner(goals_file, use_semantic=False)
    loop = AgentLoop(
        backend,
        goal_aligner=aligner,
        alignment_mode="hard",
        tracer=Tracer(enabled=False),
    )
    reply = await loop.handle("ship product release today")
    backend.complete.assert_called()
    assert reply == "Product shipped."


@pytest.mark.asyncio
async def test_no_aligner_passes(goals_file: Path) -> None:
    """Without an aligner configured, everything passes."""
    backend = AsyncMock()
    backend.complete = AsyncMock(return_value="Sure thing.")
    loop = AgentLoop(
        backend,
        goal_aligner=None,
        tracer=Tracer(enabled=False),
    )
    reply = await loop.handle("random question")
    backend.complete.assert_called()
    assert reply == "Sure thing."
