"""Headless engine CLI tests."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from pocket_agent.engine.__main__ import _append_audit, main


def test_audit_log_append(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _append_audit(data_dir, {"task": "x", "blocked": True})
    log = data_dir / "engine_runs.jsonl"
    assert log.is_file()
    row = json.loads(log.read_text(encoding="utf-8").strip())
    assert row["task"] == "x"


def test_cli_autonomous_blocked(tmp_path: Path, monkeypatch) -> None:
    goals = tmp_path / "goals.json"
    goals.write_text(
        json.dumps({"goals": [{"tag": "product", "title": "Ship"}]}),
        encoding="utf-8",
    )

    class FakeLLM:
        def generate(self, task: str, alignment=None) -> str:
            return "should not run"

    from pocket_agent.core.orchestration import OrchestrationResult

    class StubAgent:
        def orchestrate_result(self, task: str, **kwargs) -> OrchestrationResult:
            return OrchestrationResult(
                status="blocked",
                blocked_reason="BLOCKED: misaligned",
            )

    monkeypatch.setattr("pocket_agent.engine.__main__.PocketAgent", lambda **kw: StubAgent())
    with patch("pocket_agent.engine.__main__.resolve_goals_path", return_value=goals):
        code = main(["--task", "weather", "--autonomous", "--config", str(tmp_path / "nope.toml")])
    assert code == 1
