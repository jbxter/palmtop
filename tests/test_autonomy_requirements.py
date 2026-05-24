"""Regression tests for the four autonomy gaps (stub must never return)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from palmtop.core.engine import PalmtopAgent
from palmtop.core.goal_aligner import GoalAligner


class FakeLLM:
    def generate(self, task: str, alignment=None) -> str:
        return "real cloud output"

    def health(self) -> bool:
        return True

    def complete(self, messages: list[dict[str, str]]) -> str:
        return "ok"


def test_gap1_never_returns_executing_stub(tmp_path: Path) -> None:
    """① Must not return 'Executing:' placeholder text."""
    goals = tmp_path / "g.json"
    goals.write_text(
        json.dumps({"goals": [{"tag": "product", "title": "Ship"}]}),
        encoding="utf-8",
    )

    out = PalmtopAgent(
        goals_path=goals,
        llm=FakeLLM(),
        aligner=GoalAligner(goals, use_semantic=False),
    ).orchestrate("ship product release", interactive=False)
    assert out is not None
    assert not out.lower().startswith("executing:")


def test_gap1_blocks_misaligned_without_llm(tmp_path: Path) -> None:
    """① Misaligned tasks must not reach the LLM."""
    goals = tmp_path / "g.json"
    goals.write_text(
        json.dumps({"goals": [{"tag": "product", "title": "Ship"}]}),
        encoding="utf-8",
    )
    llm = MagicMock()
    llm.generate = MagicMock(side_effect=AssertionError("LLM must not run"))
    llm.health = MagicMock(return_value=True)

    result = PalmtopAgent(
        goals_path=goals,
        llm=llm,
        aligner=GoalAligner(goals, use_semantic=False),
    ).orchestrate_result("weather in paris", interactive=False)

    assert result.status == "blocked"
    assert result.blocked_reason and result.blocked_reason.startswith("BLOCKED:")
    llm.generate.assert_not_called()


def test_gap2_requires_llm_provider() -> None:
    """② PalmtopAgent must require an explicit LLM provider."""
    with pytest.raises(ValueError, match="requires an LLM provider"):
        PalmtopAgent(goals_path=Path("/nonexistent/goals.json"))


def test_gap3_semantic_not_tag_substring_only(tmp_path: Path) -> None:
    """③ Semantic judge can align without exact tag in task text."""
    p = tmp_path / "wp.json"
    p.write_text(
        json.dumps({"goals": [{"tag": "wheatpaste", "title": "Guerrilla street posters"}]}),
        encoding="utf-8",
    )
    mock_judge = MagicMock()
    mock_judge.judge.return_value = {
        "aligned": True,
        "goal_tag": "wheatpaste",
        "confidence": 0.9,
        "reason": "Poster copy serves wheatpaste goal",
    }
    r = GoalAligner(p, semantic_judge=mock_judge).check_alignment(
        "write the zine layout tonight"
    )
    assert r["is_aligned"]
    assert r["method"] == "semantic"
    mock_judge.judge.assert_called_once()


def test_gap4_malformed_json_does_not_raise(tmp_path: Path) -> None:
    """④ Corrupt goals file must not crash check_alignment."""
    p = tmp_path / "bad.json"
    p.write_text("{broken", encoding="utf-8")
    r = GoalAligner(p, use_semantic=False).check_alignment("any task")
    assert r["engine_mode"] == "SAFE_MODE"
    assert r["load_status"] == "invalid"
