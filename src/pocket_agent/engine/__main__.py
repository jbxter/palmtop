"""Headless / one-shot sovereign engine CLI.

Examples:
  uv run python -m pocket_agent.engine --task "draft poster copy"
  POCKET_AUTONOMOUS=1 uv run python -m pocket_agent.engine --task "..."
  uv run python -m pocket_agent.engine --tasks-file tasks.txt --autonomous
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from pocket_agent.config.settings import Config
from pocket_agent.core.engine import PocketAgent
from pocket_agent.core.goals_paths import resolve_goals_path


def _audit_log_path(data_dir: Path) -> Path:
    return data_dir / "engine_runs.jsonl"


def _append_audit(data_dir: Path, record: dict) -> None:
    path = _audit_log_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pocket Agent sovereign engine")
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

    autonomous = args.autonomous or os.environ.get("POCKET_AUTONOMOUS", "").lower() in (
        "1",
        "true",
        "yes",
    )

    cfg = Config.load(args.config if args.config.exists() else None)
    root = Path(".").resolve()
    goals = args.goals or resolve_goals_path(cfg.data_dir, root)

    # Build cloud LLM from configured backends
    from pocket_agent.inference.engine_llm import CloudLLMAdapter

    engine_llm = None
    if cfg.cloud_heavy.api_key:
        from pocket_agent.inference.cloud import create_cloud_backend
        heavy = create_cloud_backend(
            cfg.cloud_heavy.provider, cfg.cloud_heavy.api_key, cfg.cloud_heavy.model or None
        )
        engine_llm = CloudLLMAdapter(heavy)
    elif cfg.cloud_light.api_key:
        from pocket_agent.inference.cloud import create_cloud_backend
        light = create_cloud_backend(
            cfg.cloud_light.provider, cfg.cloud_light.api_key, cfg.cloud_light.model or None
        )
        engine_llm = CloudLLMAdapter(light)

    if engine_llm is None:
        print("No cloud API keys configured — engine requires ANTHROPIC_API_KEY or GOOGLE_API_KEY", file=sys.stderr)
        return 1

    try:
        agent = PocketAgent(
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
