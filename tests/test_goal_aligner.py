"""Pressure tests for GoalAligner edge cases."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from palmtop.core.goal_aligner import ALIGN_THRESHOLD, GoalAligner
from palmtop.core.goals_paths import goals_cache_path


@pytest.fixture
def goals_file(tmp_path: Path) -> Path:
    data = {
        "vision": "Test Vision",
        "week": 6,
        "goals": [
            {"tag": "revenue", "title": "Grow consulting pipeline"},
            {"tag": "health", "title": "Train four times per week"},
            {"tag": "product", "title": "Ship palmtop core engine"},
        ],
    }
    p = tmp_path / "twy_goals.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_tag_match_full_score(goals_file: Path) -> None:
    r = GoalAligner(goals_file, use_semantic=False).check_alignment(
        "Close a revenue deal today"
    )
    assert r["is_aligned"]
    assert r["score"] == 1.0
    assert r["method"] == "heuristic"
    assert "revenue" in r["matched_tags"]


def test_no_match(goals_file: Path) -> None:
    r = GoalAligner(goals_file, use_semantic=False).check_alignment(
        "What is the weather in Paris?"
    )
    assert not r["is_aligned"]
    assert r["score"] < ALIGN_THRESHOLD


def test_substring_tag_false_positive(goals_file: Path) -> None:
    p = goals_file.parent / "short_tag.json"
    p.write_text(
        json.dumps({"goals": [{"tag": "he", "title": "Something unrelated"}]}),
        encoding="utf-8",
    )
    r = GoalAligner(p, use_semantic=False).check_alignment("improve my health routine")
    assert not r["is_aligned"]


def test_product_not_matching_production(goals_file: Path) -> None:
    r = GoalAligner(goals_file, use_semantic=False).check_alignment(
        "Review production schedule for factory"
    )
    product = next(g for g in r["per_goal"] if g["tag"] == "product")
    assert not product["aligned"]


def test_title_keyword_partial_score(goals_file: Path) -> None:
    r = GoalAligner(goals_file, use_semantic=False).check_alignment(
        "Work on the consulting pipeline outreach"
    )
    rev = next(g for g in r["per_goal"] if g["tag"] == "revenue")
    assert rev["aligned"]


def test_missing_file_fails_closed() -> None:
    r = GoalAligner("/nonexistent/twy_goals.json", use_semantic=False).check_alignment(
        "revenue task"
    )
    assert r["load_status"] == "missing"
    assert r["engine_mode"] == "SAFE_MODE"
    assert not r["is_aligned"]


def test_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    r = GoalAligner(p, use_semantic=False).check_alignment("revenue")
    assert r["load_status"] == "invalid"
    assert r["engine_mode"] == "SAFE_MODE"


def test_invalid_json_uses_cache(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(
        json.dumps({"goals": [{"tag": "wheatpaste", "title": "Street art campaign"}]}),
        encoding="utf-8",
    )
    aligner = GoalAligner(good, use_semantic=False)
    aligner.check_alignment("probe")
    cache = goals_cache_path(good)
    assert cache.is_file()

    good.write_text("{broken", encoding="utf-8")
    r = GoalAligner(good, use_semantic=False).check_alignment("wheatpaste work")
    assert r["load_status"] == "ok"
    assert r["meta"].get("goals_source") == "cache"


def test_empty_goals_list(tmp_path: Path) -> None:
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"goals": [], "week": 1}), encoding="utf-8")
    r = GoalAligner(p, use_semantic=False).check_alignment("revenue push")
    assert r["load_status"] == "empty"
    assert r["engine_mode"] == "SAFE_MODE"


def test_malformed_goal_entries_skipped(goals_file: Path) -> None:
    p = goals_file.parent / "mixed.json"
    p.write_text(
        json.dumps({
            "goals": [
                "not-a-dict",
                {"tag": "revenue", "title": "Pipeline"},
                None,
                {"title": "no tag field"},
            ]
        }),
        encoding="utf-8",
    )
    r = GoalAligner(p, use_semantic=False).check_alignment("revenue call")
    assert r["is_aligned"]


def test_empty_task(goals_file: Path) -> None:
    r = GoalAligner(goals_file, use_semantic=False).check_alignment("   ")
    assert not r["is_aligned"]


def test_semantic_wheatpaste_poster(tmp_path: Path) -> None:
    p = tmp_path / "wp.json"
    p.write_text(
        json.dumps({
            "goals": [{"tag": "wheatpaste", "title": "Guerrilla poster street campaign"}]
        }),
        encoding="utf-8",
    )
    mock_judge = MagicMock()
    mock_judge.judge.return_value = {
        "aligned": True,
        "goal_tag": "wheatpaste",
        "confidence": 0.88,
        "reason": "Poster copy serves street art goal",
    }
    aligner = GoalAligner(p, semantic_judge=mock_judge, use_semantic=True)
    r = aligner.check_alignment("write the zine layout tonight")
    assert r["is_aligned"]
    assert r["method"] == "semantic"
    assert "wheatpaste" in r["matched_tags"]
    mock_judge.judge.assert_called_once()


def test_semantic_rejects_unrelated(tmp_path: Path) -> None:
    p = tmp_path / "wp.json"
    p.write_text(
        json.dumps({"goals": [{"tag": "wheatpaste", "title": "Street art"}]}),
        encoding="utf-8",
    )
    mock_judge = MagicMock()
    mock_judge.judge.return_value = {
        "aligned": False,
        "goal_tag": None,
        "confidence": 0.1,
        "reason": "Weather is unrelated",
    }
    r = GoalAligner(p, semantic_judge=mock_judge).check_alignment(
        "What is the weather in Paris?"
    )
    assert not r["is_aligned"]
    assert r["method"] == "semantic"


def test_semantic_unavailable_autonomous(tmp_path: Path) -> None:
    p = tmp_path / "g.json"
    p.write_text(
        json.dumps({"goals": [{"tag": "product", "title": "Ship"}]}),
        encoding="utf-8",
    )
    mock_judge = MagicMock()
    mock_judge.judge.return_value = None
    r = GoalAligner(p, semantic_judge=mock_judge, autonomous=True).check_alignment(
        "unrelated fluff"
    )
    assert not r["is_aligned"]
    assert r.get("semantic_unavailable")


def test_list_format_goals(tmp_path: Path) -> None:
    p = tmp_path / "list.json"
    p.write_text(json.dumps([{"tag": "health", "title": "Run daily"}]), encoding="utf-8")
    r = GoalAligner(p, use_semantic=False).check_alignment("health checkup")
    assert r["is_aligned"]
