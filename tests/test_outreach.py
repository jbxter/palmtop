"""Tests for intake auto-outreach hardening — issue #34.

Covers: qualification fails closed (no auto-send on LLM error) and the daily
cap bounds spam amplification to attacker-supplied addresses.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from palmtop.web.outreach import LeadInfo, LeadOutreach


def _outreach(*, llm, daily_cap=25):
    email = MagicMock()
    email.send_email = AsyncMock(return_value="msg_1")
    return LeadOutreach(
        llm=llm, email_tool=email, notify_fn=AsyncMock(), notify_user_id="u1", daily_cap=daily_cap
    ), email


_LEAD = LeadInfo(name="Alice", email="alice@example.com", project="Build a thing")


class TestQualifyFailsClosed:
    @pytest.mark.asyncio
    async def test_qualify_returns_false_on_llm_error(self):
        llm = AsyncMock()
        llm.complete = AsyncMock(side_effect=RuntimeError("LLM down"))
        outreach, _ = _outreach(llm=llm)
        assert await outreach._qualify(_LEAD) is False

    @pytest.mark.asyncio
    async def test_process_lead_does_not_send_on_qualify_error(self):
        llm = AsyncMock()
        llm.complete = AsyncMock(side_effect=RuntimeError("LLM down"))
        outreach, email = _outreach(llm=llm)
        sent = await outreach.process_lead(_LEAD)
        assert sent is False
        email.send_email.assert_not_called()


class TestDailyCap:
    def test_within_daily_cap_counts_then_blocks(self):
        outreach, _ = _outreach(llm=AsyncMock(), daily_cap=2)
        assert outreach._within_daily_cap() is True
        assert outreach._within_daily_cap() is True
        assert outreach._within_daily_cap() is False  # cap reached

    @pytest.mark.asyncio
    async def test_process_lead_blocked_when_cap_reached(self):
        # Qualifies, but the cap is 0 → must not send.
        llm = AsyncMock()
        llm.complete = AsyncMock(return_value="QUALIFIED")
        outreach, email = _outreach(llm=llm, daily_cap=0)
        sent = await outreach.process_lead(_LEAD)
        assert sent is False
        email.send_email.assert_not_called()
