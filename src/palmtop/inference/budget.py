"""Cloud-LLM spend guardrail — a daily token-budget circuit-breaker (issue #47).

Every cloud completion costs money. A runaway autonomous/monitor loop, abusive
web chat, or an injection-driven tool loop can otherwise call a paid provider
without bound. ``BudgetGuard`` caps approximate token usage per UTC day and
fails **closed** once the cap is hit: further cloud calls raise
``BudgetExceededError`` (callers degrade gracefully / fall back to local), and
the owner is alerted once.

Token counts are *estimated* from character length (the backends don't surface
provider usage), which is fine for a safety ceiling — this bounds runaway cost,
it is not billing-accurate.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from palmtop.inference.base import InferenceBackend, Message

log = logging.getLogger(__name__)


class BudgetExceededError(RuntimeError):
    """Raised when the daily cloud-token budget is exhausted."""


def estimate_tokens(messages: list[Message], reply: str | None = None) -> int:
    """Rough token estimate (≈ chars/4) for input messages plus an optional reply."""
    chars = sum(len(m.content or "") for m in messages) + len(reply or "")
    return (chars + 3) // 4


@dataclass
class BudgetGuard:
    """Tracks approximate cloud token usage per UTC day and enforces a cap.

    ``daily_token_cap <= 0`` disables the cap. ``on_exceeded(used, cap)`` is
    invoked once per day the first time the cap is hit (best-effort alert).
    """

    daily_token_cap: int = 0
    on_exceeded: Callable[[int, int], None] | None = None
    _day: str = field(default="", init=False)
    _tokens: int = field(default=0, init=False)
    _alerted: bool = field(default=False, init=False)

    def _roll(self) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self._tokens, self._alerted = today, 0, False

    @property
    def tokens_used(self) -> int:
        self._roll()
        return self._tokens

    def check(self) -> None:
        """Raise ``BudgetExceededError`` if the day's cap is already reached."""
        if self.daily_token_cap <= 0:
            return
        self._roll()
        if self._tokens >= self.daily_token_cap:
            if not self._alerted:
                self._alerted = True
                log.warning(
                    "CLOUD BUDGET exhausted: %d/%d tokens today — refusing further cloud calls",
                    self._tokens,
                    self.daily_token_cap,
                )
                if self.on_exceeded:
                    try:
                        self.on_exceeded(self._tokens, self.daily_token_cap)
                    except Exception:
                        log.debug("budget on_exceeded callback failed", exc_info=True)
            raise BudgetExceededError(f"Daily cloud token budget reached ({self._tokens}/{self.daily_token_cap})")

    def record(self, tokens: int) -> None:
        self._roll()
        self._tokens += max(0, tokens)


class BudgetedBackend:
    """Wraps an InferenceBackend, enforcing a shared ``BudgetGuard``."""

    def __init__(self, inner: InferenceBackend, guard: BudgetGuard) -> None:
        self._inner = inner
        self._guard = guard

    async def complete(self, messages: list[Message], max_tokens: int = 512) -> str:
        self._guard.check()  # fail closed before spending
        reply = await self._inner.complete(messages, max_tokens)
        self._guard.record(estimate_tokens(messages, reply))
        return reply

    async def close(self) -> None:
        await self._inner.close()

    def __getattr__(self, name):
        # Expose the inner backend's other attributes (e.g. ``_model``) for logging.
        return getattr(self._inner, name)
