"""Pressure tests for core engine orchestration (cloud LLM mocked)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pocket_agent.core.engine import PocketAgent
from pocket_agent.core.goal_aligner import GoalAligner


@pytest.fixture
def goals_file(tmp_path: Path) -> Path:
    p = tmp_path / "goals.json"
    p.write_text(
        json.dumps({"goals": [{"tag": "product", "title": "Ship engine"}]}),
        encoding="utf-8",
    )
    return p


class FakeLLM:
    def generate(self, task: str, alignment: dict | None = None) -> str:
        return f"done:{task}"

    def health(self) -> bool:
        return True

    def complete(self, messages: list[dict[str, str]]) -> str:
        return "ok"


def test_orchestrate_aligned_skips_prompt(goals_file: Path) -> None:
    agent = PocketAgent(
        goals_path=goals_file,
        llm=FakeLLM(),
        aligner=GoalAligner(goals_file, use_semantic=False),
    )
    with patch.object(agent, "_prompt_override") as mock_prompt:
        out = agent.orchestrate("ship product milestone", interactive=True)
    mock_prompt.assert_not_called()
    assert out == "done:ship product milestone"


def test_orchestrate_blocked_non_interactive(goals_file: Path) -> None:
    agent = PocketAgent(
        goals_path=goals_file,
        llm=FakeLLM(),
        aligner=GoalAligner(goals_file, use_semantic=False),
    )
    out = agent.orchestrate("weather in tokyo", interactive=False)
    assert out is not None
    assert out.startswith("BLOCKED:")


def test_orchestrate_override_runs_llm(goals_file: Path) -> None:
    agent = PocketAgent(
        goals_path=goals_file,
        llm=FakeLLM(),
        aligner=GoalAligner(goals_file, use_semantic=False),
    )
    with patch.object(agent, "_prompt_override", return_value="override"):
        out = agent.orchestrate("random unrelated task", interactive=True)
    assert out == "done:random unrelated task"


def test_realign_max_depth(goals_file: Path) -> None:
    agent = PocketAgent(
        goals_path=goals_file,
        llm=FakeLLM(),
        aligner=GoalAligner(goals_file, use_semantic=False),
    )
    with patch.object(agent, "_prompt_override", return_value="realign"):
        with patch("builtins.input", side_effect=["still bad", "still bad", "still bad"]):
            out = agent.orchestrate("nope", interactive=True)
    assert out is not None
    assert out.startswith("BLOCKED:")


def test_requires_llm_provider() -> None:
    """PocketAgent raises ValueError when no LLM is provided."""
    with pytest.raises(ValueError, match="requires an LLM provider"):
        PocketAgent(goals_path=Path("/tmp/test_goals.json"))


def test_goals_missing_blocks_execution(tmp_path: Path) -> None:
    agent = PocketAgent(
        goals_path=tmp_path / "nope.json",
        llm=FakeLLM(),
        autonomous=True,
    )
    out = agent.orchestrate("anything")
    assert out is not None
    assert out.startswith("BLOCKED:")


def test_goals_missing_continue_override(tmp_path: Path) -> None:
    agent = PocketAgent(goals_path=tmp_path / "nope.json", llm=FakeLLM())
    with patch.object(agent, "_prompt_goals_fix", return_value="continue"):
        out = agent.orchestrate("emergency task", interactive=True)
    assert out == "done:emergency task"


def test_pocket_agent_rejects_bad_goals_path_constructor(tmp_path: Path) -> None:
    agent = PocketAgent(goals_path=tmp_path / "missing.json", llm=FakeLLM(), autonomous=True)
    r = agent.aligner.check_alignment("test")
    assert r["engine_mode"] == "SAFE_MODE"
