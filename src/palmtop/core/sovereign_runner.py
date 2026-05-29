"""Run the sovereign engine from a chat channel (no stdin).

Bridges the engine into the async agent loop: a misaligned engine task can't
drop to a stdin prompt, so it routes through the blessing gate (human /approve)
instead. The gate is duck-typed (prepare/wait) so this module stays independent
of core.blessing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

# Message prefixes that route a turn to the sovereign engine. Colon forms are
# checked before bare-space forms so "engine: x" strips the colon, not " engine".
_ENGINE_PREFIXES = ("/engine ", "/claude ", "engine:", "claude:", "engine ", "claude ")


def parse_engine_task(text: str) -> str | None:
    """Return the task body if the message triggers the engine, else None.

    Triggers: ``/engine <task>``, ``engine <task>``, ``engine: <task>`` and the
    ``/claude`` / ``claude`` / ``claude:`` aliases.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    low = raw.lower()
    for prefix in _ENGINE_PREFIXES:
        if low.startswith(prefix):
            return raw[len(prefix) :].strip()
    return None


def _append_engine_audit(data_dir: Path | None, record: dict) -> None:
    if data_dir is None:
        return
    path = Path(data_dir) / "engine_runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


async def run_sovereign_engine(
    sovereign: object,
    task: str,
    *,
    data_dir: Path | None = None,
    user_id: str = "default",
    blessing_gate: object | None = None,
    send_fn: Callable[[str, str], Awaitable[None]] | None = None,
) -> str:
    """Run ``task`` through the sovereign engine, gating misaligned tasks.

    Aligned tasks execute and return their output. A blocked (misaligned) task
    is surfaced for human approval via the blessing gate; on approval it executes
    anyway, on denial it stays blocked. Every run is appended to
    ``engine_runs.jsonl`` under ``data_dir``.
    """
    if sovereign is None:
        return "Sovereign engine is not configured."

    # orchestrate_result() is synchronous (and may call an LLM) — run off-loop.
    result = await asyncio.to_thread(sovereign.orchestrate_result, task)
    alignment = getattr(result, "alignment", None) or {}

    _append_engine_audit(
        data_dir,
        {
            "ts": datetime.now(UTC).isoformat(),
            "user_id": user_id,
            "task": task[:500],
            "status": getattr(result, "status", "unknown"),
            "aligned": alignment.get("is_aligned"),
        },
    )

    if getattr(result, "status", "") != "blocked":
        return result.message() or "Engine produced no output."

    # Blocked — offer a human override through the blessing gate.
    if blessing_gate is not None and send_fn is not None:
        summary = f"Engine task approval\nTask: {task[:280]}\n\n{result.message()}"
        blessing_gate.prepare(summary)
        await send_fn(user_id, f"\U0001f512 Engine task approval needed\n\n{summary}\n\nReply /approve or /deny")
        approved = await asyncio.to_thread(blessing_gate.wait)
        if approved and hasattr(sovereign, "execute_override"):
            output = await asyncio.to_thread(sovereign.execute_override, task)
            _append_engine_audit(
                data_dir,
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "user_id": user_id,
                    "task": task[:500],
                    "status": "override_executed",
                },
            )
            return output
        if not approved:
            return "Engine task denied — not executed."

    return result.message()
