"""Tests for the XMPP/Jabber channel."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

slixmpp = pytest.importorskip("slixmpp")

from palmtop.channels.xmpp import (  # noqa: E402
    MAX_XMPP_MESSAGE,
    XmppChannel,
    _split_message,
)


class TestXmppChannelInit:
    def test_requires_jid(self):
        with pytest.raises(ValueError, match="JID"):
            XmppChannel(jid="", password="pass")

    def test_requires_password(self):
        with pytest.raises(ValueError, match="password"):
            XmppChannel(jid="bot@server.org", password="")

    def test_basic_init(self):
        ch = XmppChannel(jid="bot@server.org", password="secret")
        assert ch.name == "xmpp"
        assert ch._jid == "bot@server.org"
        assert ch._muc_nick == "palmtop"
        assert ch._allowed_jids is None

    def test_custom_config(self):
        ch = XmppChannel(
            jid="bot@server.org",
            password="secret",
            allowed_jids=["Alice@server.org", "Bob@other.org"],
            mucs=["room@conference.server.org"],
            muc_nick="mybot",
        )
        assert ch._allowed_jids == {"alice@server.org", "bob@other.org"}
        assert ch._mucs == ["room@conference.server.org"]
        assert ch._muc_nick == "mybot"


class TestXmppOnMessage:
    @pytest.fixture
    def channel(self):
        ch = XmppChannel(
            jid="bot@server.org",
            password="secret",
            allowed_jids=["alice@server.org"],
        )
        ch._agent = AsyncMock()
        ch._agent.handle = AsyncMock(return_value="Agent reply!")

        # Mock the slixmpp client
        ch._client = MagicMock()
        ch._client.send_message = MagicMock()
        ch._client.make_message = MagicMock(return_value=MagicMock())
        return ch

    @pytest.mark.asyncio
    async def test_handles_chat_message(self, channel):
        msg = MagicMock()
        msg.__getitem__ = lambda self, key: {
            "type": "chat",
            "from": MagicMock(bare="alice@server.org"),
            "body": "Hello bot",
        }[key]
        await channel._on_message(msg)
        channel._agent.handle.assert_called_once()
        call_args = channel._agent.handle.call_args
        assert call_args[0][0] == "Hello bot"
        assert call_args[1]["user_id"] == "xmpp:alice@server.org"

    @pytest.mark.asyncio
    async def test_ignores_own_messages(self, channel):
        msg = MagicMock()
        msg.__getitem__ = lambda self, key: {
            "type": "chat",
            "from": MagicMock(bare="bot@server.org"),
            "body": "Echo",
        }[key]
        await channel._on_message(msg)
        channel._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_non_allowed_jid(self, channel):
        msg = MagicMock()
        msg.__getitem__ = lambda self, key: {
            "type": "chat",
            "from": MagicMock(bare="stranger@server.org"),
            "body": "Hello",
        }[key]
        await channel._on_message(msg)
        channel._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_groupchat_type(self, channel):
        msg = MagicMock()
        msg.__getitem__ = lambda self, key: {
            "type": "groupchat",
            "from": MagicMock(bare="alice@server.org"),
            "body": "Hello",
        }[key]
        await channel._on_message(msg)
        channel._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_reply(self, channel):
        msg = MagicMock()
        msg.__getitem__ = lambda self, key: {
            "type": "chat",
            "from": MagicMock(bare="alice@server.org"),
            "body": "Hi",
        }[key]
        await channel._on_message(msg)
        # Should send reply
        send_calls = channel._client.send_message.call_args_list
        # At least the typing indicator + reply
        assert len(send_calls) >= 2


class TestXmppOnMucMessage:
    @pytest.fixture
    def channel(self):
        ch = XmppChannel(
            jid="bot@server.org",
            password="secret",
            muc_nick="palmtop",
        )
        ch._agent = AsyncMock()
        ch._agent.handle = AsyncMock(return_value="MUC reply!")
        ch._client = MagicMock()
        ch._client.send_message = MagicMock()
        ch._client.plugin = {"xep_0045": MagicMock()}
        return ch

    @pytest.mark.asyncio
    async def test_responds_to_mention(self, channel):
        msg = MagicMock()
        msg.__getitem__ = lambda self, key: {
            "mucnick": "alice",
            "from": MagicMock(bare="room@conference.server.org"),
            "body": "palmtop: what time is it?",
        }[key]
        await channel._on_muc_message(msg)
        channel._agent.handle.assert_called_once()
        assert channel._agent.handle.call_args[0][0] == "what time is it?"

    @pytest.mark.asyncio
    async def test_ignores_non_mention(self, channel):
        msg = MagicMock()
        msg.__getitem__ = lambda self, key: {
            "mucnick": "alice",
            "from": MagicMock(bare="room@conference.server.org"),
            "body": "just chatting",
        }[key]
        await channel._on_muc_message(msg)
        channel._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_own_messages(self, channel):
        msg = MagicMock()
        msg.__getitem__ = lambda self, key: {
            "mucnick": "palmtop",
            "from": MagicMock(bare="room@conference.server.org"),
            "body": "palmtop: echo",
        }[key]
        await channel._on_muc_message(msg)
        channel._agent.handle.assert_not_called()


class TestXmppSendMessage:
    def test_sends_message(self):
        ch = XmppChannel(jid="bot@server.org", password="secret")
        ch._client = MagicMock()
        ch._client.send_message = MagicMock()
        import asyncio

        asyncio.run(ch.send_message("alice@server.org", "Hello!"))
        ch._client.send_message.assert_called_once_with(
            mto="alice@server.org",
            mbody="Hello!",
            mtype="chat",
        )

    def test_send_when_not_connected(self):
        ch = XmppChannel(jid="bot@server.org", password="secret")
        import asyncio

        asyncio.run(ch.send_message("alice@server.org", "hi"))
        # Should not raise


class TestXmppStop:
    @pytest.mark.asyncio
    async def test_stop_disconnects(self):
        ch = XmppChannel(jid="bot@server.org", password="secret")
        ch._client = MagicMock()
        ch._client.disconnect = MagicMock()
        await ch.stop()
        assert ch._stop_event.is_set()
        ch._client.disconnect.assert_called_once()


class TestXmppSplitMessage:
    def test_short_message(self):
        assert _split_message("Hello") == ["Hello"]

    def test_at_limit(self):
        msg = "A" * MAX_XMPP_MESSAGE
        assert _split_message(msg) == [msg]

    def test_long_splits_at_paragraph(self):
        p1 = "Word " * 500  # 2500 chars
        p2 = "More " * 500  # 2500 chars
        text = f"{p1}\n\n{p2}"
        chunks = _split_message(text)
        assert len(chunks) == 2
        assert all(len(c) <= MAX_XMPP_MESSAGE for c in chunks)

    def test_very_long_no_breaks(self):
        text = "A" * (MAX_XMPP_MESSAGE + 100)
        chunks = _split_message(text)
        assert len(chunks) == 2

    def test_empty_message(self):
        assert _split_message("") == [""]
