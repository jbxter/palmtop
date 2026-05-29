"""12 Week Year goal alignment — the gate the sovereign engine consults.

A task is "aligned" if it serves one of the user's active goals. Matching runs
in two passes:

1. Heuristic — a goal's ``tag`` matched as a whole word (full score), or enough
   significant words from its ``title`` (partial score). Word-boundary matching
   avoids false positives like tag "product" matching "production".
2. Semantic — when the heuristic is inconclusive and a judge is available, an
   LLM decides (see SemanticAlignmentJudge).

The loader fails closed: a missing, invalid, or empty goals file drops the
engine into SAFE_MODE (nothing is considered aligned). A successful load is
cached so a later corrupt/mid-edit file can fall back to the last good goals.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from palmtop.core.alignment_judge import SemanticAlignmentJudge
from palmtop.core.goals_paths import goals_cache_path

log = logging.getLogger(__name__)

# Minimum score (0..1) for a task to count as aligned.
ALIGN_THRESHOLD = 0.5

# Generic words ignored when matching goal titles against a task.
_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "for",
    "to",
    "of",
    "in",
    "on",
    "at",
    "by",
    "my",
    "our",
    "your",
    "this",
    "that",
    "with",
    "from",
    "into",
    "per",
    "via",
}


class GoalAligner:
    def __init__(
        self,
        goals_path: str | Path,
        semantic_judge: object | None = None,
        use_semantic: bool = True,
        autonomous: bool = False,
        llm: object | None = None,
    ) -> None:
        self._goals_path = Path(goals_path)
        self._use_semantic = use_semantic
        self._autonomous = autonomous
        if semantic_judge is None and use_semantic and llm is not None:
            semantic_judge = SemanticAlignmentJudge(llm)
        self._judge = semantic_judge

    # ── Public API ────────────────────────────────────────────────────────────

    def check_alignment(self, task: str) -> dict:
        task = (task or "").strip()
        goals, load_status, source = self._load_goals()
        meta = {"goals_source": source}

        # Fail closed when goals can't be trusted.
        if load_status != "ok":
            return self._result(
                False,
                0.0,
                "none",
                [],
                [],
                load_status,
                meta,
                note=f"Goals unavailable ({load_status}) — safe mode, autonomous execution blocked.",
            )

        if not task:
            return self._result(False, 0.0, "heuristic", [], [], load_status, meta, note="Empty task.")

        per_goal, matched_tags, score = self._heuristic(task, goals)
        if score >= ALIGN_THRESHOLD:
            label = ", ".join(matched_tags) if matched_tags else "title keywords"
            return self._result(
                True,
                score,
                "heuristic",
                matched_tags,
                per_goal,
                load_status,
                meta,
                note=f"Heuristic match: {label}.",
            )

        if self._use_semantic and self._judge is not None:
            verdict = self._judge.judge(task, goals)
            if verdict is None:
                res = self._result(
                    False,
                    score,
                    "semantic",
                    [],
                    per_goal,
                    load_status,
                    meta,
                    note="Semantic judge unavailable — failing closed.",
                )
                res["semantic_unavailable"] = True
                return res
            aligned = bool(verdict.get("aligned"))
            tag = verdict.get("goal_tag")
            confidence = float(verdict.get("confidence", 0.0) or 0.0)
            tags = [tag] if aligned and tag else []
            return self._result(
                aligned,
                confidence if aligned else score,
                "semantic",
                tags,
                per_goal,
                load_status,
                meta,
                note=str(verdict.get("reason", "")),
            )

        return self._result(False, score, "heuristic", [], per_goal, load_status, meta, note="No goal matched.")

    # ── Matching ────────────────────────────────────────────────────────────────

    def _heuristic(self, task: str, goals: list[dict]) -> tuple[list[dict], list[str], float]:
        task_l = task.lower()
        per_goal: list[dict] = []
        matched_tags: list[str] = []
        best = 0.0

        for goal in goals:
            tag = goal["tag"]
            title = goal["title"]
            gscore = 0.0

            if tag and _word_in(tag.lower(), task_l):
                gscore = 1.0
            else:
                words = _significant_words(title)
                if words:
                    hits = sum(1 for w in words if _word_in(w, task_l))
                    if hits:
                        gscore = hits / len(words)

            aligned = gscore >= ALIGN_THRESHOLD
            per_goal.append({"tag": tag, "title": title, "aligned": aligned, "score": round(gscore, 3)})
            if aligned and tag:
                matched_tags.append(tag)
            best = max(best, gscore)

        return per_goal, matched_tags, best

    # ── Loading ───────────────────────────────────────────────────────────────

    def _load_goals(self) -> tuple[list[dict], str, str]:
        path = self._goals_path
        if not path.is_file():
            return [], "missing", "none"

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cached = self._read_cache()
            if cached is not None:
                goals = _extract_goals(cached)
                if goals:
                    return goals, "ok", "cache"
            return [], "invalid", "none"

        goals = _extract_goals(raw)
        if not goals:
            return [], "empty", "file"

        self._write_cache(raw)
        return goals, "ok", "file"

    def _read_cache(self) -> object | None:
        cache = goals_cache_path(self._goals_path)
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _write_cache(self, raw: object) -> None:
        cache = goals_cache_path(self._goals_path)
        try:
            cache.write_text(json.dumps(raw), encoding="utf-8")
        except OSError:
            log.debug("Could not write goals cache to %s", cache, exc_info=True)

    # ── Result shape ──────────────────────────────────────────────────────────

    @staticmethod
    def _result(
        is_aligned: bool,
        score: float,
        method: str,
        matched_tags: list[str],
        per_goal: list[dict],
        load_status: str,
        meta: dict,
        *,
        note: str = "",
    ) -> dict:
        return {
            "is_aligned": is_aligned,
            "score": round(float(score), 3),
            "method": method,
            "matched_tags": matched_tags,
            "per_goal": per_goal,
            "load_status": load_status,
            "engine_mode": "NORMAL" if load_status == "ok" else "SAFE_MODE",
            "meta": meta,
            "note": note,
        }


def _word_in(word: str, text: str) -> bool:
    """True if ``word`` appears in ``text`` as a whole word (case handled by caller)."""
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


def _significant_words(title: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", title.lower())
    return [w for w in words if len(w) >= 4 and w not in _STOPWORDS]


def _extract_goals(raw: object) -> list[dict]:
    """Normalize goals from either a bare list or a {"goals": [...]} wrapper.

    Skips entries that aren't dicts or carry neither a tag nor a title.
    """
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get("goals", [])
    else:
        items = []

    goals: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag", "")).strip()
        title = str(item.get("title", "")).strip()
        if not tag and not title:
            continue
        goals.append({"tag": tag, "title": title})
    return goals
