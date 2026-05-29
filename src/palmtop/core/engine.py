"""Sovereign engine — align → gate → execute.

PalmtopAgent decides whether to run a task. Aligned tasks go straight to the
LLM. Misaligned tasks are blocked unless a human overrides (interactive) — and
even then the engine offers a realign loop so the human can restate the task in
goal-serving terms, bounded by a max depth. A missing/invalid goals file fails
closed (BLOCKED) rather than executing blind.

Execution itself is delegated to the injected ``llm`` (``generate(task,
alignment) -> str``); the engine never returns a placeholder.
"""

from __future__ import annotations

import logging
from pathlib import Path

from palmtop.core.goal_aligner import GoalAligner
from palmtop.core.orchestration import OrchestrationResult

log = logging.getLogger(__name__)

# Human override grant for explicit "run it anyway" / engine-task approval.
_OVERRIDE_ALIGNMENT = {"is_aligned": True, "score": 1.0, "matched_tags": [], "note": "human override"}


class PalmtopAgent:
    """Goal-gated task orchestrator."""

    MAX_REALIGN_DEPTH = 3

    def __init__(
        self,
        goals_path: str | Path,
        llm: object | None = None,
        aligner: GoalAligner | None = None,
        autonomous: bool = False,
        project_root: Path | None = None,
        data_dir: Path | None = None,
    ) -> None:
        if llm is None:
            raise ValueError("PalmtopAgent requires an LLM provider")
        self._goals_path = Path(goals_path)
        self._llm = llm
        self._autonomous = autonomous
        self._project_root = project_root
        self._data_dir = data_dir
        self.aligner = aligner or GoalAligner(goals_path, use_semantic=False, autonomous=autonomous)

    # ── Orchestration ──────────────────────────────────────────────────────────

    def orchestrate(self, task: str, interactive: bool = False) -> str | None:
        alignment = self.aligner.check_alignment(task)

        # Goals untrustworthy → safe mode. Only an interactive "continue" proceeds.
        if alignment.get("load_status") != "ok":
            status = alignment.get("load_status")
            if interactive and self._prompt_goals_fix() == "continue":
                return self._execute(task, _OVERRIDE_ALIGNMENT)
            return self._blocked(f"Goals unavailable ({status}) — refusing to execute.")

        if alignment.get("is_aligned"):
            return self._execute(task, alignment)

        if not interactive:
            return self._blocked(f"Task not aligned with active goals: {task}")

        return self._handle_misaligned(task, alignment)

    def _handle_misaligned(self, task: str, alignment: dict) -> str | None:
        current = task
        for _ in range(self.MAX_REALIGN_DEPTH):
            decision = (self._prompt_override() or "").strip().lower()
            if decision == "override":
                return self._execute(current, _OVERRIDE_ALIGNMENT)
            if decision == "realign":
                current = input().strip()
                alignment = self.aligner.check_alignment(current)
                if alignment.get("is_aligned"):
                    return self._execute(current, alignment)
                continue
            return self._blocked(f"Task not aligned: {current}")
        return self._blocked(f"Task still not aligned after {self.MAX_REALIGN_DEPTH} attempts: {current}")

    def orchestrate_result(self, task: str, interactive: bool = False) -> OrchestrationResult:
        """Structured, non-interactive orchestration for the channel/CLI path.

        Mirrors orchestrate() but returns an OrchestrationResult instead of a
        string and never prompts — misaligned tasks come back as ``blocked`` for
        the caller (channel runner / CLI) to gate or report.
        """
        alignment = self.aligner.check_alignment(task)

        if alignment.get("load_status") != "ok":
            return OrchestrationResult(
                status="blocked",
                blocked_reason=f"BLOCKED: goals unavailable ({alignment.get('load_status')}).",
                alignment=alignment,
            )
        if alignment.get("is_aligned"):
            return OrchestrationResult(status="executed", output=self._execute(task, alignment), alignment=alignment)
        return OrchestrationResult(
            status="blocked",
            blocked_reason=f"BLOCKED: task not aligned with active goals: {task}",
            alignment=alignment,
        )

    def execute_override(self, task: str) -> str:
        """Run a task with an explicit human override (used after a blessing approval)."""
        return self._execute(task, _OVERRIDE_ALIGNMENT)

    def _execute(self, task: str, alignment: dict) -> str:
        return self._llm.generate(task, alignment)

    @staticmethod
    def _blocked(reason: str) -> str:
        return f"BLOCKED: {reason}"

    # ── Interactive prompts (mocked in tests; used by run_loop) ──────────────────

    def _prompt_override(self) -> str:
        return input("Task not aligned with your goals. [override / realign / abort]? ").strip().lower()

    def _prompt_goals_fix(self) -> str:
        return input("Goals file is missing or unreadable. [continue / abort]? ").strip().lower()

    # ── REPL ────────────────────────────────────────────────────────────────────

    def run_loop(self) -> None:
        """Simple stdin REPL — read tasks, orchestrate interactively."""
        print("Palmtop sovereign engine. Type a task (Ctrl-D to exit).")
        while True:
            try:
                task = input("\ntask> ").strip()
            except EOFError:
                print()
                return
            if not task:
                continue
            result = self.orchestrate(task, interactive=True)
            print(result)
