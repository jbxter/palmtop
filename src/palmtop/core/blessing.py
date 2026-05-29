"""Human-in-the-loop approval gate for high-risk autonomous actions.

The agent can act on its own — launch a Cursor job, trigger a deploy, run an
auto-created skill — but anything risky routes through a ``BlessingGate``: the
channel sends the user an approval prompt and the calling coroutine blocks until
the user replies ``/approve`` or ``/deny``.

Only one approval is pending at a time, which matches the single-user assistant
model. The calling coroutine arms the gate and blocks off the event loop:

    gate.prepare(summary)                         # arm before the user can reply
    await send_fn(user_id, prompt)                # tell the user
    approved = await asyncio.to_thread(gate.wait)  # block in a worker thread

A channel command handler resolves it from the event loop with ``approve()`` /
``deny()`` (see ``channels/telegram.py``). Because ``wait()`` runs off the loop
and ``approve()``/``deny()`` run on it, a ``threading.Event`` bridges the two.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


# ── Risk assessment ──────────────────────────────────────────────────────────

# Patterns that mark an action as higher-risk. They don't block anything on
# their own — they annotate the summary the human sees so the approve/deny
# decision is informed.
_RISK_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)\b(delete|drop|destroy|wipe|truncate)\b|rm\s+-rf"), "destructive operation"),
    (re.compile(r"(?i)force[-\s]?push|reset\s+--hard|--force\b"), "force/overwrite operation"),
    (re.compile(r"(?i)\b(prod|production|live)\b"), "touches production"),
    (re.compile(r"(?i)\b(secret|credential|api[-\s]?key|token|password)\b|\.env\b"), "handles secrets"),
    (re.compile(r"(?i)\b(deploy|release|publish|ship)\b"), "deploys/publishes"),
    (re.compile(r"(?i)\b(send|email|post|message|notify|dm)\b"), "sends an outbound message"),
    (re.compile(r"(?i)\b(payment|charge|transfer|invoice|refund)\b"), "moves money"),
]

# Reasons severe enough to bump an action to "high" risk on their own.
_HIGH_REASONS = {
    "destructive operation",
    "force/overwrite operation",
    "touches production",
    "moves money",
}


@dataclass
class RiskAssessment:
    level: str  # "low" | "medium" | "high"
    reasons: list[str] = field(default_factory=list)

    @property
    def is_risky(self) -> bool:
        return self.level != "low"


def assess_risk(text: str) -> RiskAssessment:
    """Heuristically rate how risky a proposed action is, for the approval prompt."""
    reasons: list[str] = []
    for pattern, label in _RISK_PATTERNS:
        if pattern.search(text or "") and label not in reasons:
            reasons.append(label)
    if not reasons:
        return RiskAssessment(level="low")
    level = "high" if any(r in _HIGH_REASONS for r in reasons) else "medium"
    return RiskAssessment(level=level, reasons=reasons)


def build_approval_summary(
    action: str,
    alignment: dict | None = None,
    risk: RiskAssessment | None = None,
) -> str:
    """Render a human-readable approval prompt body.

    ``action`` is the proposed action (a Cursor prompt, a skill's intended
    effect, etc.). ``alignment`` is the optional 12WY goal-alignment dict
    (``is_aligned`` / ``score`` / ``matched_tags``). ``risk`` comes from
    ``assess_risk``; it is computed from ``action`` when omitted.
    """
    if risk is None:
        risk = assess_risk(action)

    preview = (action or "").strip()
    if len(preview) > 280:
        preview = preview[:280] + "…"

    lines = [f"Action: {preview}", f"Risk: {risk.level}"]
    if risk.reasons:
        lines.append("Flags: " + ", ".join(risk.reasons))

    if alignment is not None:
        aligned = alignment.get("is_aligned", True)
        score = alignment.get("score")
        tags = alignment.get("matched_tags") or []
        align_line = f"Goal-aligned: {'yes' if aligned else 'no'}"
        if isinstance(score, int | float):
            align_line += f" (score {score:.2f})"
        if tags:
            align_line += " — " + ", ".join(str(t) for t in tags)
        lines.append(align_line)

    return "\n".join(lines)


# ── The gate ──────────────────────────────────────────────────────────────────


class BlessingGate:
    """A one-at-a-time, thread-safe human approval gate.

    The caller arms the gate with ``prepare()`` (or the one-shot ``request()``)
    and blocks in a worker thread on ``wait()``. A channel command handler
    resolves the pending request from the event loop with ``approve()`` or
    ``deny()``. A timeout — or a ``wait()`` with nothing pending — resolves to
    denial, so the gate fails closed.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._pending = False
        self._approved = False
        self._summary = ""

    @property
    def is_pending(self) -> bool:
        return self._pending

    @property
    def summary(self) -> str:
        return self._summary

    def prepare(self, summary: str = "") -> None:
        """Arm the gate so ``is_pending`` is True before the user can reply.

        Arming before sending the prompt closes the race where ``/approve``
        arrives before the blocking ``wait()`` has started.
        """
        with self._lock:
            self._summary = summary
            self._approved = False
            self._pending = True
            self._event.clear()
        log.info("Blessing gate armed: %s", summary[:80] if summary else "(no summary)")

    def wait(self, timeout: float | None = None) -> bool:
        """Block until ``approve()``/``deny()`` (or timeout). Return True if approved.

        Must be called off the event loop (via ``asyncio.to_thread``) because it
        blocks. Nothing pending, or a timeout, resolves to denial.
        """
        if not self._pending:
            return False
        signaled = self._event.wait(timeout)
        with self._lock:
            approved = self._approved if signaled else False
            self._pending = False
            self._event.clear()
        if not signaled:
            log.warning("Blessing gate timed out after %ss — treating as denied", timeout)
        return approved

    def request(self, summary: str = "", timeout: float | None = None) -> bool:
        """Convenience: ``prepare()`` + ``wait()`` in one blocking call."""
        self.prepare(summary)
        return self.wait(timeout)

    def approve(self) -> None:
        with self._lock:
            if not self._pending:
                return
            self._approved = True
        self._event.set()
        log.info("Blessing approved")

    def deny(self) -> None:
        with self._lock:
            if not self._pending:
                return
            self._approved = False
        self._event.set()
        log.info("Blessing denied")
