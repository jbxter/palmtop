"""Structured result of a sovereign-engine orchestration.

PalmtopAgent.orchestrate_result returns this so callers (the CLI, the channel
runner) can branch on ``status`` without parsing strings, while ``message()``
gives the human-facing text.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Terminal statuses an orchestration can end in.
EXECUTED = "executed"
BLOCKED = "blocked"
SKIPPED = "skipped"
ERROR = "error"


@dataclass
class OrchestrationResult:
    status: str
    output: str = ""
    blocked_reason: str = ""
    alignment: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == EXECUTED

    def message(self) -> str:
        """Human-facing text for this result."""
        if self.status == EXECUTED:
            return self.output
        if self.status == BLOCKED:
            return self.blocked_reason or "BLOCKED: task not aligned with active goals."
        return self.output or self.blocked_reason or f"({self.status})"
