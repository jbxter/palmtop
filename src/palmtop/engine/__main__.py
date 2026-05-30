"""Headless / one-shot sovereign engine CLI.

Examples:
  uv run python -m palmtop.engine --task "draft poster copy"
  PALMTOP_AUTONOMOUS=1 uv run python -m palmtop.engine --task "..."
  uv run python -m palmtop.engine --tasks-file tasks.txt --autonomous
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from palmtop.config.settings import Config
from palmtop.core.engine import PalmtopAgent
from palmtop.core.goals_paths import resolve_goals_path


def _audit_log_path(data_dir: Path) -> Path:
    return data_dir / "engine_runs.jsonl"


def _append_audit(data_dir: Path, record: dict) -> None:
    path = _audit_log_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Palmtop sovereign engine")
    parser.add_argument("--task", help="Single task to run")
    parser.add_argument("--tasks-file", help="Newline-separated tasks (batch)")
    parser.add_argument("--goals", type=Path, help="Path to twy_goals.json")
    parser.add_argument(
        "--autonomous",
        action="store_true",
        help="No stdin prompts; block misaligned / safe-mode tasks",
    )
    parser.add_argument("--repl", action="store_true", help="Interactive task loop")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    args = parser.parse_args(argv)

    autonomous = args.autonomous or os.environ.get("PALMTOP_AUTONOMOUS", "").lower() in (
        "1",
        "true",
        "yes",
    )

    cfg = Config.load(args.config if args.config.exists() else None)

    # Safety floor (issue #25): clamp config to the minimum guardrail policy and
    # refuse --autonomous unless an operator marker permits it. The floor is from
    # code defaults + env the agent can't set on a process it doesn't launch.
    from palmtop.core.safety import SafetyFloor, audit_safety, clamp_config, goals_path_is_agent_writable

    floor = SafetyFloor.load()
    clamps = clamp_config(cfg, floor)
    if clamps:
        audit_safety(cfg.data_dir, {"event": "config_clamp", "clamps": clamps, "allow_unsafe": floor.allow_unsafe})
    if autonomous and not floor.autonomous_permitted():
        print(
            "Refusing --autonomous / PALMTOP_AUTONOMOUS: not permitted by the safety floor. "
            "Set PALMTOP_ALLOW_AUTONOMOUS=1 (or PALMTOP_ALLOW_UNSAFE=1) to run autonomously.",
            file=sys.stderr,
        )
        audit_safety(cfg.data_dir, {"event": "autonomous_refused"})
        return 1

    root = Path(".").resolve()
    goals = args.goals or resolve_goals_path(cfg.data_dir, root)
    if goals_path_is_agent_writable(goals, cfg.data_dir):
        print(
            f"SAFETY: goals at {goals} sit inside the agent-writable docs sandbox; "
            "move them to docs/plans (outside data_dir).",
            file=sys.stderr,
        )
        audit_safety(cfg.data_dir, {"event": "goals_in_sandbox", "path": str(goals)})

    # Build cloud LLM from configured backends
    from palmtop.inference.engine_llm import CloudLLMAdapter

    engine_llm = None
    if cfg.cloud_heavy.api_key:
        from palmtop.inference.cloud import create_cloud_backend

        heavy = create_cloud_backend(cfg.cloud_heavy.provider, cfg.cloud_heavy.api_key, cfg.cloud_heavy.model or None)
        engine_llm = CloudLLMAdapter(heavy)
    elif cfg.cloud_light.api_key:
        from palmtop.inference.cloud import create_cloud_backend

        light = create_cloud_backend(cfg.cloud_light.provider, cfg.cloud_light.api_key, cfg.cloud_light.model or None)
        engine_llm = CloudLLMAdapter(light)

    if engine_llm is None:
        print(
            "No cloud API keys configured — engine requires ANTHROPIC_API_KEY or GOOGLE_API_KEY",
            file=sys.stderr,
        )
        return 1

    try:
        agent = PalmtopAgent(
            goals_path=goals,
            llm=engine_llm,
            data_dir=cfg.data_dir,
            project_root=root,
            autonomous=autonomous,
        )
    except (ValueError, ConnectionError) as e:
        print(e, file=sys.stderr)
        return 1

    if args.repl or (not args.task and not args.tasks_file):
        agent.run_loop()
        return 0

    tasks: list[str] = []
    if args.task:
        tasks.append(args.task)
    if args.tasks_file:
        path = Path(args.tasks_file)
        if not path.is_file():
            print(f"Tasks file not found: {path}", file=sys.stderr)
            return 1
        tasks.extend(
            ln.strip()
            for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        )

    exit_code = 0
    for task in tasks:
        result = agent.orchestrate_result(task, interactive=not autonomous)
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "task": task,
            "autonomous": autonomous,
            "result": result.message(),
            "status": result.status,
            "blocked": result.status == "blocked",
        }
        _append_audit(cfg.data_dir, record)
        msg = result.message()
        if msg:
            print(msg)
        if autonomous and result.status in ("blocked", "skipped", "error"):
            exit_code = 1
        print("---")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
