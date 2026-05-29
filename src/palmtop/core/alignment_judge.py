"""LLM-based semantic alignment judge.

GoalAligner falls back to this when heuristic matching is inconclusive: it asks
a cloud LLM whether a task meaningfully serves any active goal and returns a
structured verdict. It returns ``None`` when unavailable (no backend or a failed
call) so callers can fail closed rather than guess.
"""

from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

_JUDGE_PROMPT = """You decide whether a task serves any of the user's active goals.

Goals (tag — title):
{goals}

Task: {task}

Reply with ONLY a JSON object, no prose:
{{"aligned": true|false, "goal_tag": "<tag or null>", "confidence": 0.0-1.0, "reason": "<one sentence>"}}"""


class SemanticAlignmentJudge:
    """Judge task↔goal alignment with a cloud LLM.

    ``llm`` may be any object exposing a synchronous ``generate(prompt) -> str``
    or ``complete(messages) -> str``. Constructed without a backend, ``judge``
    always returns ``None`` (unavailable).
    """

    def __init__(self, llm: object | None = None) -> None:
        self._llm = llm

    def judge(self, task: str, goals: list[dict]) -> dict | None:
        if not self._llm:
            return None
        goals_text = "\n".join(f"- {g.get('tag', '')} — {g.get('title', '')}" for g in goals)
        prompt = _JUDGE_PROMPT.format(goals=goals_text, task=task)
        try:
            raw = self._invoke(prompt)
        except Exception:
            log.debug("Semantic judge LLM call failed", exc_info=True)
            return None
        return self._parse(raw)

    def _invoke(self, prompt: str) -> str:
        llm = self._llm
        if hasattr(llm, "generate"):
            return llm.generate(prompt)
        if hasattr(llm, "complete"):
            return llm.complete([{"role": "user", "content": prompt}])
        raise AttributeError("LLM backend exposes neither generate() nor complete()")

    @staticmethod
    def _parse(raw: str) -> dict | None:
        if not raw:
            return None
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if "aligned" not in data:
            return None
        return {
            "aligned": bool(data.get("aligned")),
            "goal_tag": data.get("goal_tag"),
            "confidence": float(data.get("confidence", 0.0) or 0.0),
            "reason": str(data.get("reason", "")),
        }
