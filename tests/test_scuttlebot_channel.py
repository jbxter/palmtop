"""Tests for the ScuttleBot multi-agent coordination channel."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from palmtop.channels.scuttlebot import ScuttleBotChannel


class TestScuttleBotInit:
    def test_requires_server(self):
        with pytest.raises(ValueError, match="server"):
            ScuttleBotChannel(server="")

    def test_basic_init(self):
        ch = ScuttleBotChannel(server="localhost")
        assert ch.name == "scuttlebot"
        assert ch._nick == "palmtop"
        assert ch._channels == ["#ops"]
        assert ch._broadcast_tools is True
        assert ch._paused is False

    def test_custom_config(self):
        ch = ScuttleBotChannel(
            server="scuttlebot.local",
            port=6697,
            nick="myagent",
            channels=["#engineering", "#support"],
            use_ssl=True,
            broadcast_tools=False,
        )
        assert ch._server == "scuttlebot.local"
        assert ch._port == 6697
        assert ch._nick == "myagent"
        assert ch._channels == ["#engineering", "#support"]
        assert ch._use_ssl is True
        assert ch._broadcast_tools is False


class TestScuttleBotProtocol:
    @pytest.fixture
    def channel(self):
        ch = ScuttleBotChannel(
            server="localhost",
            nick="palmtop",
            channels=["#ops"],
        )
        ch._agent = AsyncMock()
        ch._agent.handle = AsyncMock(return_value="Agent reply!")
        # Mock the underlying IRC channel
        ch._irc = MagicMock()
        ch._irc._connected = True
        ch._irc._send_raw = AsyncMock()
        ch._irc._nick = "palmtop"
        return ch

    @pytest.mark.asyncio
    async def test_handles_dm(self, channel):
        await channel._on_privmsg(
            "alice!user@host",
            "palmtop :Hello agent",
        )
        channel._agent.handle.assert_called_once()
        call_args = channel._agent.handle.call_args
        assert call_args[0][0] == "Hello agent"
        assert call_args[1]["user_id"] == "scuttlebot:alice"

    @pytest.mark.asyncio
    async def test_handles_channel_mention(self, channel):
        await channel._on_privmsg(
            "alice!user@host",
            "#ops :palmtop: help with deployment",
        )
        channel._agent.handle.assert_called_once()
        assert channel._agent.handle.call_args[0][0] == "help with deployment"

    @pytest.mark.asyncio
    async def test_handles_at_mention(self, channel):
        await channel._on_privmsg(
            "alice!user@host",
            "#ops :@palmtop please check status",
        )
        channel._agent.handle.assert_called_once()
        assert "check status" in channel._agent.handle.call_args[0][0]

    @pytest.mark.asyncio
    async def test_ignores_non_addressed(self, channel):
        await channel._on_privmsg(
            "alice!user@host",
            "#ops :just chatting about stuff",
        )
        channel._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_tool_broadcasts(self, channel):
        await channel._on_privmsg(
            "other_agent!user@host",
            '#ops :[TOOL] search({"query":"test"})',
        )
        channel._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_result_broadcasts(self, channel):
        await channel._on_privmsg(
            "other_agent!user@host",
            "#ops :[RESULT] search → Found 5 results",
        )
        channel._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_status_broadcasts(self, channel):
        await channel._on_privmsg(
            "other_agent!user@host",
            "#ops :[STATUS] other_agent online",
        )
        channel._agent.handle.assert_not_called()


class TestScuttleBotInterruption:
    @pytest.fixture
    def channel(self):
        ch = ScuttleBotChannel(
            server="localhost",
            nick="palmtop",
            channels=["#ops"],
        )
        ch._agent = AsyncMock()
        ch._agent.handle = AsyncMock(return_value="Reply")
        ch._irc = MagicMock()
        ch._irc._connected = True
        ch._irc._send_raw = AsyncMock()
        ch._irc._nick = "palmtop"
        return ch

    @pytest.mark.asyncio
    async def test_pause_command(self, channel):
        await channel._on_privmsg(
            "orchestrator!user@host",
            "#ops :!pause @palmtop",
        )
        assert channel._paused is True
        # Should send status message
        channel._irc._send_raw.assert_called()
        sent = channel._irc._send_raw.call_args[0][0]
        assert "paused" in sent.lower()

    @pytest.mark.asyncio
    async def test_resume_command(self, channel):
        channel._paused = True
        await channel._on_privmsg(
            "orchestrator!user@host",
            "#ops :!resume @palmtop",
        )
        assert channel._paused is False
        channel._irc._send_raw.assert_called()
        sent = channel._irc._send_raw.call_args[0][0]
        assert "resumed" in sent.lower()

    @pytest.mark.asyncio
    async def test_status_command(self, channel):
        await channel._on_privmsg(
            "orchestrator!user@host",
            "#ops :!status @palmtop",
        )
        channel._irc._send_raw.assert_called()
        sent = channel._irc._send_raw.call_args[0][0]
        assert "active" in sent.lower()

    @pytest.mark.asyncio
    async def test_paused_ignores_messages(self, channel):
        channel._paused = True
        await channel._on_privmsg(
            "alice!user@host",
            "palmtop :Hello",
        )
        channel._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_pause_only_for_this_agent(self, channel):
        """!pause directed at another agent should be ignored."""
        await channel._on_privmsg(
            "orchestrator!user@host",
            "#ops :!pause @other_agent",
        )
        assert channel._paused is False


class TestScuttleBotBroadcast:
    @pytest.fixture
    def channel(self):
        ch = ScuttleBotChannel(
            server="localhost",
            nick="palmtop",
            channels=["#ops"],
            broadcast_tools=True,
        )
        ch._irc = MagicMock()
        ch._irc._connected = True
        ch._irc._send_raw = AsyncMock()
        return ch

    @pytest.mark.asyncio
    async def test_broadcast_tool_call(self, channel):
        await channel.broadcast_tool_call("web_search", {"query": "test"})
        channel._irc._send_raw.assert_called_once()
        sent = channel._irc._send_raw.call_args[0][0]
        assert "[TOOL]" in sent
        assert "web_search" in sent

    @pytest.mark.asyncio
    async def test_broadcast_tool_result(self, channel):
        await channel.broadcast_tool_result("web_search", "Found 5 results about testing")
        channel._irc._send_raw.assert_called_once()
        sent = channel._irc._send_raw.call_args[0][0]
        assert "[RESULT]" in sent
        assert "web_search" in sent

    @pytest.mark.asyncio
    async def test_broadcast_disabled(self, channel):
        channel._broadcast_tools = False
        await channel.broadcast_tool_call("search", {"q": "test"})
        channel._irc._send_raw.assert_not_called()

    @pytest.mark.asyncio
    async def test_broadcast_truncates_long_args(self, channel):
        long_args = {"query": "x" * 500}
        await channel.broadcast_tool_call("search", long_args)
        sent = channel._irc._send_raw.call_args[0][0]
        assert "..." in sent


class TestScuttleBotSendMessage:
    @pytest.mark.asyncio
    async def test_delegates_to_irc(self):
        ch = ScuttleBotChannel(server="localhost")
        ch._irc = MagicMock()
        ch._irc.send_message = AsyncMock()
        await ch.send_message("alice", "Hello!")
        ch._irc.send_message.assert_called_once_with("alice", "Hello!")

    @pytest.mark.asyncio
    async def test_send_when_not_connected(self):
        ch = ScuttleBotChannel(server="localhost")
        # No IRC client
        await ch.send_message("alice", "hi")
        # Should not raise


class TestScuttleBotStop:
    @pytest.mark.asyncio
    async def test_stop_announces_and_disconnects(self):
        ch = ScuttleBotChannel(server="localhost", channels=["#ops"])
        ch._irc = MagicMock()
        ch._irc._connected = True
        ch._irc._send_raw = AsyncMock()
        ch._irc.stop = AsyncMock()
        await ch.stop()
        assert ch._stop_event.is_set()
        # Should announce departure
        ch._irc._send_raw.assert_called()
        sent = ch._irc._send_raw.call_args[0][0]
        assert "offline" in sent.lower()
        # Should stop IRC
        ch._irc.stop.assert_called_once()
