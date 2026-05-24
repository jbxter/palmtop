#!/usr/bin/env python3
"""Composer entry point — sovereign engine (package-backed).

This file does NOT contain stub logic. Implementation lives in:
  - pocket_agent.core.engine.PocketAgent      (align → gate → execute)
  - pocket_agent.inference.engine_llm          (cloud LLM adapter)
  - pocket_agent.core.goal_aligner.GoalAligner (heuristic + semantic judge)
  - pocket_agent.core.alignment_judge          (LLM-based alignment)

Run REPL:
  uv run python docs/engine/core_engine.py

Run headless (no stdin, blocks misaligned tasks):
  POCKET_AUTONOMOUS=1 uv run python -m pocket_agent.engine --task "your task"

Verify imports:
  uv run python docs/engine/core_engine.py --verify
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from pocket_agent.core.engine import PocketAgent  # noqa: E402
from pocket_agent.core.goal_aligner import GoalAligner  # noqa: E402
from pocket_agent.core.goals_paths import resolve_goals_path  # noqa: E402

__all__ = ["PocketAgent", "GoalAligner", "DEFAULT_GOALS", "verify_engine"]

DEFAULT_GOALS = resolve_goals_path(project_root=_ROOT)


def verify_engine() -> bool:
    """Quick contract check — run before trusting autonomy."""
    ok = True
    src = (_ROOT / "src/pocket_agent/core/engine.py").read_text(encoding="utf-8")
    if 'return f"Executing:' in src or 'return "Executing:' in src:
        print("FAIL: engine.py still returns stub Executing placeholder")
        ok = False
    if "any(g['tag']" in src or "tag'] in task" in src:
        print("FAIL: engine.py still uses brittle tag substring matching")
        ok = False
    aligner_src = (_ROOT / "src/pocket_agent/core/goal_aligner.py").read_text(encoding="utf-8")
    if "json.JSONDecodeError" not in aligner_src:
        print("FAIL: goal_aligner.py missing JSON error handling")
        ok = False
    if "SemanticAlignmentJudge" not in aligner_src:
        print("FAIL: goal_aligner.py missing semantic judge")
        ok = False
    if ok:
        print("OK: autonomy contract — gate, cloud LLM, semantic align, safe JSON load")
    return ok


def main() -> None:
    if "--verify" in sys.argv:
        raise SystemExit(0 if verify_engine() else 1)

    goals = DEFAULT_GOALS
    args = [a for a in sys.argv[1:] if a != "--verify"]
    if args:
        goals = Path(args[0])

    # Need a cloud backend to run the REPL
    from pocket_agent.config.settings import Config
    cfg = Config.load(Path("config.toml") if Path("config.toml").exists() else None)
    from pocket_agent.inference.cloud import create_cloud_backend
    from pocket_agent.inference.engine_llm import CloudLLMAdapter

    backend = None
    if cfg.cloud_heavy.api_key:
        backend = create_cloud_backend(
            cfg.cloud_heavy.provider, cfg.cloud_heavy.api_key, cfg.cloud_heavy.model or None
        )
    elif cfg.cloud_light.api_key:
        backend = create_cloud_backend(
            cfg.cloud_light.provider, cfg.cloud_light.api_key, cfg.cloud_light.model or None
        )
    if backend is None:
        print("No cloud API keys configured — set ANTHROPIC_API_KEY or GOOGLE_API_KEY")
        raise SystemExit(1)

    agent = PocketAgent(goals_path=goals, llm=CloudLLMAdapter(backend), project_root=_ROOT)
    agent.run_loop()


if __name__ == "__main__":
    main()
