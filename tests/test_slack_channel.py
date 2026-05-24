"""Tests for the Slack channel — Bolt + Socket Mode integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("slack_bolt", reason="slack-bolt not installed")

from palmtop.channels.slack import (  # noqa: E402
    MAX_MESSAGE_LENGTH,
    SlackChannel,
    _split_message,
    _strip_mention,
)


class TestSlackChannelInit:
    def test_requires_bot_token(self):
        with pytest.raises(ValueError, match="SLACK_BOT_TOKEN"):
            SlackChannel(bot_token="", app_token="xapp-test")

    def test_requires_app_token(self):
        with pytest.raises(ValueError, match="SLACK_APP_TOKEN"):
            SlackChannel(bot_token="xoxb-test", app_token="")

    @patch("palmtop.channels.slack.AsyncApp")
    def test_basic_init(self, mock_app_cls):
        ch = SlackChannel(bot_token="xoxb-test", app_token="xapp-test")
        assert ch.name == "slack"
        assert ch._allowed_users is None

    @patch("palmtop.channels.slack.AsyncApp")
    def test_allowed_users(self, mock_app_cls):
        ch = SlackChannel(
            bot_token="xoxb-test",
            app_token="xapp-test",
            allowed_users=["U123", "U456"],
        )
        assert ch._allowed_users == {"U123", "U456"}


class TestSlackChannelProtocol:
    @patch("palmtop.channels.slack.AsyncApp")
    def test_name_property(self, mock_app_cls):
        ch = SlackChannel(bot_token="xoxb-test", app_token="xapp-test")
        assert ch.name == "slack"


class TestOnMessage:
    @pytest.fixture
    def channel(self):
        with patch("palmtop.channels.slack.AsyncApp"):
            ch = SlackChannel(
                bot_token="xoxb-test",
                app_token="xapp-test",
                allowed_users=["U12345"],
            )
            ch._agent = AsyncMock()
            ch._agent.handle = AsyncMock(return_value="Agent reply!")
            return ch

    @pytest.mark.asyncio
    async def test_ignores_bot_messages(self, channel):
        event = {"bot_id": "B123", "user": "U12345", "text": "hi"}
        say = AsyncMock()
        await channel._on_message(event, say, None)
        channel._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_subtype_messages(self, channel):
        event = {"subtype": "message_changed", "user": "U12345", "text": "hi"}
        say = AsyncMock()
        await channel._on_message(event, say, None)
        channel._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_non_dm(self, channel):
        event = {"user": "U12345", "text": "hello", "channel_type": "channel"}
        say = AsyncMock()
        await channel._on_message(event, say, None)
        channel._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_non_allowed_user(self, channel):
        event = {"user": "U99999", "text": "hello", "channel_type": "im"}
        say = AsyncMock()
        await channel._on_message(event, say, None)
        channel._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_processes_allowed_dm(self, channel):
        event = {
            "user": "U12345",
            "text": "What's on my calendar?",
            "channel_type": "im",
            "ts": "1234567890.123456",
        }
        say = AsyncMock()
        await channel._on_message(event, say, None)

        channel._agent.handle.assert_called_once()
        call_args = channel._agent.handle.call_args
        assert call_args[0][0] == "What's on my calendar?"
        assert call_args[1]["user_id"] == "slack:U12345"
        say.assert_called_once_with(text="Agent reply!", thread_ts="1234567890.123456")

    @pytest.mark.asyncio
    async def test_ignores_empty_text(self, channel):
        event = {"user": "U12345", "text": "   ", "channel_type": "im"}
        say = AsyncMock()
        await channel._on_message(event, say, None)
        channel._agent.handle.assert_not_called()


class TestOnMention:
    @pytest.fixture
    def channel(self):
        with patch("palmtop.channels.slack.AsyncApp"):
            ch = SlackChannel(
                bot_token="xoxb-test",
                app_token="xapp-test",
                allowed_users=["U12345"],
            )
            ch._agent = AsyncMock()
            ch._agent.handle = AsyncMock(return_value="Mention reply!")
            return ch

    @pytest.mark.asyncio
    async def test_processes_mention(self, channel):
        event = {
            "user": "U12345",
            "text": "<@U99BOT> search for python docs",
            "ts": "111.222",
        }
        say = AsyncMock()
        await channel._on_mention(event, say, None)

        channel._agent.handle.assert_called_once()
        call_args = channel._agent.handle.call_args
        assert call_args[0][0] == "search for python docs"
        assert call_args[1]["user_id"] == "slack:U12345"

    @pytest.mark.asyncio
    async def test_ignores_empty_after_strip(self, channel):
        event = {"user": "U12345", "text": "<@U99BOT>", "ts": "111.222"}
        say = AsyncMock()
        await channel._on_mention(event, say, None)
        channel._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_non_allowed_user(self, channel):
        event = {"user": "U99999", "text": "<@U99BOT> do thing", "ts": "111.222"}
        say = AsyncMock()
        await channel._on_mention(event, say, None)
        channel._agent.handle.assert_not_called()


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_sends_dm(self):
        with patch("palmtop.channels.slack.AsyncApp"):
            ch = SlackChannel(bot_token="xoxb-test", app_token="xapp-test")

        with patch("palmtop.channels.slack.AsyncWebClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.conversations_open = AsyncMock(return_value={"channel": {"id": "C123DM"}})
            mock_client.chat_postMessage = AsyncMock()
            mock_client_cls.return_value = mock_client

            await ch.send_message("U12345", "Your reminder: standup in 5min")

            mock_client.conversations_open.assert_called_once_with(users=["U12345"])
            mock_client.chat_postMessage.assert_called_once_with(
                channel="C123DM",
                text="Your reminder: standup in 5min",
                mrkdwn=True,
            )


class TestStripMention:
    def test_strips_mention(self):
        assert _strip_mention("<@U12345> hello world") == "hello world"

    def test_strips_mention_no_space(self):
        assert _strip_mention("<@UABC>hello") == "hello"

    def test_no_mention(self):
        assert _strip_mention("just a message") == "just a message"

    def test_empty_after_strip(self):
        assert _strip_mention("<@U12345>") == ""

    def test_mention_in_middle(self):
        # Only strips leading mention
        assert _strip_mention("hey <@U12345> help") == "hey <@U12345> help"


class TestSplitMessage:
    def test_short_message_unchanged(self):
        assert _split_message("Hello") == ["Hello"]

    def test_splits_at_newline(self):
        text = "A" * 3900 + "\n" + "B" * 200
        chunks = _split_message(text)
        assert len(chunks) == 2
        assert all(len(c) <= MAX_MESSAGE_LENGTH for c in chunks)

    def test_hard_split(self):
        text = "A" * 5000
        chunks = _split_message(text)
        assert len(chunks) == 2
        assert chunks[0] == "A" * MAX_MESSAGE_LENGTH
        assert chunks[1] == "A" * 1000

    def test_preserves_code_blocks(self):
        text = "```\n" + "line\n" * 800 + "```"
        assert len(text) > MAX_MESSAGE_LENGTH
        chunks = _split_message(text)
        assert len(chunks) >= 2
        assert chunks[0].endswith("```")
        assert chunks[1].startswith("```\n")


class TestStopChannel:
    @pytest.mark.asyncio
    async def test_stop_sets_event(self):
        with patch("palmtop.channels.slack.AsyncApp"):
            ch = SlackChannel(bot_token="xoxb-test", app_token="xapp-test")
            ch._handler = AsyncMock()
            ch._handler.close_async = AsyncMock()

            await ch.stop()
            assert ch._stop_event.is_set()
            ch._handler.close_async.assert_called_once()
