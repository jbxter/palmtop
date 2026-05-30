"""Tests for the cloud-LLM spend circuit-breaker — issue #47."""

from __future__ import annotations

import pytest

from palmtop.inference.base import Message
from palmtop.inference.budget import (
    BudgetedBackend,
    BudgetExceededError,
    BudgetGuard,
    estimate_tokens,
)


def test_estimate_tokens():
    # 40 input chars + 40 reply chars = 80 chars ≈ 20 tokens
    assert estimate_tokens([Message(role="user", content="a" * 40)], "b" * 40) == 20


def test_unlimited_when_cap_zero():
    g = BudgetGuard(daily_token_cap=0)
    g.check()
    g.record(10_000_000)
    g.check()  # still fine — cap disabled


def test_check_raises_when_over():
    g = BudgetGuard(daily_token_cap=100)
    g.check()  # under
    g.record(100)
    with pytest.raises(BudgetExceededError):
        g.check()


def test_on_exceeded_called_once():
    calls: list = []
    g = BudgetGuard(daily_token_cap=10, on_exceeded=lambda u, c: calls.append((u, c)))
    g.record(10)
    for _ in range(3):
        with pytest.raises(BudgetExceededError):
            g.check()
    assert len(calls) == 1  # alerted once per day


class _Inner:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, max_tokens=512) -> str:
        self.calls += 1
        return "reply"

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_budgeted_backend_records_then_blocks():
    g = BudgetGuard(daily_token_cap=5)  # tiny cap: one call exceeds it
    inner = _Inner()
    b = BudgetedBackend(inner, g)

    out = await b.complete([Message(role="user", content="hello world here")], max_tokens=10)
    assert out == "reply"
    assert inner.calls == 1
    assert g.tokens_used > 0

    # Now over budget — the next call is refused *before* reaching the inner backend.
    with pytest.raises(BudgetExceededError):
        await b.complete([Message(role="user", content="again")])
    assert inner.calls == 1
