"""Tests for the email channel — AgentMail polling + routing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from palmtop.channels.email import (
    DEFAULT_POLL_INTERVAL,
    EmailChannel,
    _extract_subject,
    _format_email_for_agent,
    _plain_to_html,
)


class TestEmailChannelInit:
    def test_requires_api_key(self):
        with pytest.raises(ValueError, match="AGENTMAIL_API_KEY"):
            EmailChannel(api_key="")

    def test_basic_init(self):
        ch = EmailChannel(api_key="am_test123", inbox_id="inbox_abc")
        assert ch.name == "email"
        assert ch._poll_interval == DEFAULT_POLL_INTERVAL
        assert ch._allowed_senders is None

    def test_allowed_senders_lowercase(self):
        ch = EmailChannel(
            api_key="am_test",
            allowed_senders=["Alice@Example.COM", "bob@test.org"],
        )
        assert ch._allowed_senders == {"alice@example.com", "bob@test.org"}

    def test_custom_poll_interval(self):
        ch = EmailChannel(api_key="am_test", poll_interval=60)
        assert ch._poll_interval == 60


class TestEmailChannelProtocol:
    def test_name_property(self):
        ch = EmailChannel(api_key="am_test")
        assert ch.name == "email"

    def test_email_address_before_start(self):
        ch = EmailChannel(api_key="am_test")
        assert ch.email_address == ""


class TestResolveInbox:
    @pytest.fixture
    def channel(self):
        return EmailChannel(api_key="am_test123")

    @pytest.mark.asyncio
    async def test_resolve_from_api(self, channel):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            return_value=MagicMock(
                status_code=200,
                json=lambda: {"inboxes": [{"inbox_id": "inbox_resolved", "email_address": "bot@agentmail.to"}]},
            )
        )
        channel._client = mock_client
        await channel._resolve_inbox()
        assert channel._inbox_id == "inbox_resolved"
        assert channel._email_address == "bot@agentmail.to"

    @pytest.mark.asyncio
    async def test_resolve_no_inboxes(self, channel):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            return_value=MagicMock(
                status_code=200,
                json=lambda: {"inboxes": []},
            )
        )
        channel._client = mock_client
        await channel._resolve_inbox()
        assert channel._inbox_id == ""

    @pytest.mark.asyncio
    async def test_resolve_with_existing_inbox_id(self):
        ch = EmailChannel(api_key="am_test", inbox_id="inbox_preset")
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            return_value=MagicMock(
                status_code=200,
                json=lambda: {"email_address": "preset@agentmail.to"},
            )
        )
        ch._client = mock_client
        await ch._resolve_inbox()
        assert ch._inbox_id == "inbox_preset"
        assert ch._email_address == "preset@agentmail.to"


class TestSeedSeen:
    @pytest.mark.asyncio
    async def test_seeds_existing_messages(self):
        ch = EmailChannel(api_key="am_test", inbox_id="inbox_abc")
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            return_value=MagicMock(
                status_code=200,
                json=lambda: {
                    "messages": [
                        {"message_id": "msg_1"},
                        {"message_id": "msg_2"},
                        {"message_id": "msg_3"},
                    ]
                },
            )
        )
        ch._client = mock_client
        await ch._seed_seen()
        assert ch._seen == {"msg_1", "msg_2", "msg_3"}


class TestPollInbox:
    @pytest.mark.asyncio
    async def test_new_messages_routed_to_agent(self):
        ch = EmailChannel(api_key="am_test", inbox_id="inbox_abc")
        ch._seen = {"msg_old"}
        ch._agent = AsyncMock()
        ch._agent.handle = AsyncMock(return_value="Thanks for your email!")

        # Mock inbox listing with one new message
        inbox_response = MagicMock(
            status_code=200,
            json=lambda: {
                "messages": [
                    {"message_id": "msg_old"},
                    {
                        "message_id": "msg_new",
                        "from": "user@example.com",
                        "subject": "Hello",
                        "text": "Hi there, can you help?",
                    },
                ]
            },
        )
        # Mock reply endpoint
        reply_response = MagicMock(status_code=201, json=lambda: {"message_id": "msg_reply"}, text="")

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=inbox_response)
        mock_client.post = AsyncMock(return_value=reply_response)
        ch._client = mock_client

        await ch._poll_inbox()

        # Agent was called with the new message
        ch._agent.handle.assert_called_once()
        call_args = ch._agent.handle.call_args
        assert "user@example.com" in call_args[0][0]
        assert "Hi there, can you help?" in call_args[0][0]
        assert call_args[1]["user_id"] == "email:user@example.com"

        # Reply was sent
        mock_client.post.assert_called_once()
        assert "msg_new" in mock_client.post.call_args[0][0]

    @pytest.mark.asyncio
    async def test_skips_already_seen(self):
        ch = EmailChannel(api_key="am_test", inbox_id="inbox_abc")
        ch._seen = {"msg_1", "msg_2"}
        ch._agent = AsyncMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            return_value=MagicMock(
                status_code=200,
                json=lambda: {
                    "messages": [
                        {"message_id": "msg_1"},
                        {"message_id": "msg_2"},
                    ]
                },
            )
        )
        ch._client = mock_client

        await ch._poll_inbox()
        ch._agent.handle.assert_not_called()


class TestAllowedSenders:
    @pytest.mark.asyncio
    async def test_blocks_non_allowed_sender(self):
        ch = EmailChannel(
            api_key="am_test",
            inbox_id="inbox_abc",
            allowed_senders=["boss@company.com"],
        )
        ch._agent = AsyncMock()
        ch._client = AsyncMock()

        msg = {
            "message_id": "msg_blocked",
            "from": "stranger@evil.com",
            "subject": "Hey",
            "text": "Please do something",
        }
        await ch._handle_message(msg)
        ch._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_allows_matching_sender(self):
        ch = EmailChannel(
            api_key="am_test",
            inbox_id="inbox_abc",
            allowed_senders=["boss@company.com"],
        )
        ch._agent = AsyncMock()
        ch._agent.handle = AsyncMock(return_value="Got it!")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            return_value=MagicMock(status_code=201, json=lambda: {"message_id": "r1"}, text="")
        )
        ch._client = mock_client

        msg = {
            "message_id": "msg_allowed",
            "from": "boss@company.com",
            "subject": "Task",
            "text": "Please handle this",
        }
        await ch._handle_message(msg)
        ch._agent.handle.assert_called_once()


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_proactive_email(self):
        ch = EmailChannel(api_key="am_test", inbox_id="inbox_abc")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            return_value=MagicMock(status_code=201, json=lambda: {"message_id": "msg_sent"}, text="")
        )
        ch._client = mock_client

        await ch.send_message("recipient@test.com", "Your reminder: meeting at 3pm")

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]
        assert payload["to"] == ["recipient@test.com"]
        assert "reminder" in payload["text"].lower() or "meeting" in payload["text"].lower()

    @pytest.mark.asyncio
    async def test_send_when_not_ready(self):
        ch = EmailChannel(api_key="am_test")
        # No client initialized
        await ch.send_message("test@test.com", "hello")
        # Should not raise, just log warning


class TestStopChannel:
    @pytest.mark.asyncio
    async def test_stop_sets_event(self):
        ch = EmailChannel(api_key="am_test", inbox_id="inbox_abc")
        mock_client = AsyncMock()
        mock_client.aclose = AsyncMock()
        ch._client = mock_client

        await ch.stop()
        assert ch._stop_event.is_set()
        assert ch._client is None
        mock_client.aclose.assert_called_once()


class TestHelpers:
    def test_format_email_for_agent(self):
        result = _format_email_for_agent("alice@test.com", "Meeting", "Let's meet at 3pm")
        assert "[EMAIL from alice@test.com]" in result
        assert "Subject: Meeting" in result
        assert "Let's meet at 3pm" in result
        assert "Please reply" in result

    def test_format_email_truncates_long_body(self):
        long_body = "x" * 5000
        result = _format_email_for_agent("a@b.com", "Test", long_body)
        assert "[... truncated]" in result
        assert len(result) < 5500

    def test_extract_subject_short(self):
        assert _extract_subject("Hello world") == "Hello world"

    def test_extract_subject_long(self):
        long_text = "A" * 100 + "\nMore content"
        result = _extract_subject(long_text)
        assert len(result) <= 78
        assert result.endswith("...")

    def test_extract_subject_multiline(self):
        assert _extract_subject("First line\nSecond line") == "First line"

    def test_plain_to_html_basic(self):
        result = _plain_to_html("Hello\nworld")
        assert "<p>" in result
        assert "Hello" in result

    def test_plain_to_html_escapes(self):
        result = _plain_to_html("<script>alert('xss')</script>")
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_plain_to_html_paragraphs(self):
        result = _plain_to_html("Para one\n\nPara two")
        assert result.count("<p>") == 2
