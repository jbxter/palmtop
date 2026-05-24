"""Tests for the Matrix channel — matrix-nio integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("nio", reason="matrix-nio not installed")

from palmtop.channels.matrix import (  # noqa: E402
    MAX_MESSAGE_LENGTH,
    MatrixChannel,
    _split_message,
    _text_to_html,
)


class TestMatrixChannelInit:
    def test_requires_homeserver(self):
        with pytest.raises(ValueError, match="MATRIX_HOMESERVER"):
            MatrixChannel(homeserver="", user_id="@bot:mx.org", access_token="tok")

    def test_requires_user_id(self):
        with pytest.raises(ValueError, match="MATRIX_USER_ID"):
            MatrixChannel(homeserver="https://mx.org", user_id="", access_token="tok")

    def test_requires_access_token(self):
        with pytest.raises(ValueError, match="MATRIX_ACCESS_TOKEN"):
            MatrixChannel(homeserver="https://mx.org", user_id="@bot:mx.org", access_token="")

    @patch("palmtop.channels.matrix.AsyncClient")
    def test_basic_init(self, mock_client_cls):
        ch = MatrixChannel(
            homeserver="https://matrix.org",
            user_id="@palmtop:matrix.org",
            access_token="syt_test123",
        )
        assert ch.name == "matrix"
        assert ch._allowed_users is None
        assert ch._allowed_rooms is None

    @patch("palmtop.channels.matrix.AsyncClient")
    def test_allowed_users(self, mock_client_cls):
        ch = MatrixChannel(
            homeserver="https://mx.org",
            user_id="@bot:mx.org",
            access_token="tok",
            allowed_users=["@alice:mx.org", "@bob:mx.org"],
        )
        assert ch._allowed_users == {"@alice:mx.org", "@bob:mx.org"}

    @patch("palmtop.channels.matrix.AsyncClient")
    def test_allowed_rooms(self, mock_client_cls):
        ch = MatrixChannel(
            homeserver="https://mx.org",
            user_id="@bot:mx.org",
            access_token="tok",
            allowed_rooms=["!room1:mx.org"],
        )
        assert ch._allowed_rooms == {"!room1:mx.org"}


class TestMatrixOnMessage:
    @pytest.fixture
    def channel(self):
        with patch("palmtop.channels.matrix.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.room_typing = AsyncMock()
            mock_client.room_send = AsyncMock()
            mock_cls.return_value = mock_client

            ch = MatrixChannel(
                homeserver="https://mx.org",
                user_id="@bot:mx.org",
                access_token="tok",
                allowed_users=["@alice:mx.org"],
            )
            ch._client = mock_client
            ch._synced = True
            ch._agent = AsyncMock()
            ch._agent.handle = AsyncMock(return_value="Hello from agent!")
            return ch

    @pytest.mark.asyncio
    async def test_ignores_own_messages(self, channel):
        room = MagicMock()
        room.room_id = "!room:mx.org"
        event = MagicMock()
        event.sender = "@bot:mx.org"
        event.body = "hello"

        await channel._on_message(room, event)
        channel._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_before_sync(self, channel):
        channel._synced = False
        room = MagicMock()
        event = MagicMock()
        event.sender = "@alice:mx.org"
        event.body = "hello"

        await channel._on_message(room, event)
        channel._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_non_allowed_user(self, channel):
        room = MagicMock()
        room.room_id = "!room:mx.org"
        event = MagicMock()
        event.sender = "@stranger:evil.org"
        event.body = "hack the planet"

        await channel._on_message(room, event)
        channel._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_non_allowed_room(self, channel):
        channel._allowed_rooms = {"!allowed:mx.org"}
        room = MagicMock()
        room.room_id = "!forbidden:mx.org"
        event = MagicMock()
        event.sender = "@alice:mx.org"
        event.body = "hello"

        await channel._on_message(room, event)
        channel._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_processes_allowed_message(self, channel):
        room = MagicMock()
        room.room_id = "!room:mx.org"
        room.display_name = "DM"
        event = MagicMock()
        event.sender = "@alice:mx.org"
        event.body = "What's on my calendar?"

        await channel._on_message(room, event)

        channel._agent.handle.assert_called_once()
        call_args = channel._agent.handle.call_args
        assert call_args[0][0] == "What's on my calendar?"
        assert call_args[1]["user_id"] == "matrix:@alice:mx.org"

        # Check room_send was called with reply
        channel._client.room_send.assert_called_once()
        send_args = channel._client.room_send.call_args[1]
        assert send_args["room_id"] == "!room:mx.org"
        assert send_args["content"]["body"] == "Hello from agent!"

    @pytest.mark.asyncio
    async def test_typing_indicator(self, channel):
        room = MagicMock()
        room.room_id = "!room:mx.org"
        room.display_name = "DM"
        event = MagicMock()
        event.sender = "@alice:mx.org"
        event.body = "hello"

        await channel._on_message(room, event)

        # Should have sent typing=True before and typing=False after
        typing_calls = channel._client.room_typing.call_args_list
        assert len(typing_calls) == 2
        assert typing_calls[0][1]["typing_state"] is True
        assert typing_calls[1][1]["typing_state"] is False


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_to_room_id(self):
        with patch("palmtop.channels.matrix.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.room_send = AsyncMock()
            mock_cls.return_value = mock_client

            ch = MatrixChannel(
                homeserver="https://mx.org",
                user_id="@bot:mx.org",
                access_token="tok",
            )
            ch._client = mock_client

            await ch.send_message("!room123:mx.org", "Hello room!")
            mock_client.room_send.assert_called_once()
            args = mock_client.room_send.call_args[1]
            assert args["room_id"] == "!room123:mx.org"
            assert args["content"]["body"] == "Hello room!"


class TestStopChannel:
    @pytest.mark.asyncio
    async def test_stop_sets_event(self):
        with patch("palmtop.channels.matrix.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.close = AsyncMock()
            mock_cls.return_value = mock_client

            ch = MatrixChannel(
                homeserver="https://mx.org",
                user_id="@bot:mx.org",
                access_token="tok",
            )
            ch._client = mock_client

            await ch.stop()
            assert ch._stop_event.is_set()
            mock_client.close.assert_called_once()


class TestSplitMessage:
    def test_short_message(self):
        assert _split_message("Hi") == ["Hi"]

    def test_splits_long_message(self):
        text = "A" * 5000
        chunks = _split_message(text)
        assert len(chunks) == 2
        assert all(len(c) <= MAX_MESSAGE_LENGTH for c in chunks)

    def test_splits_at_newline(self):
        text = "A" * 3900 + "\n" + "B" * 200
        chunks = _split_message(text)
        assert len(chunks) == 2
        assert chunks[0] == "A" * 3900


class TestTextToHtml:
    def test_bold(self):
        assert "<strong>bold</strong>" in _text_to_html("**bold**")

    def test_italic(self):
        assert "<em>italic</em>" in _text_to_html("*italic*")

    def test_inline_code(self):
        assert "<code>code</code>" in _text_to_html("`code`")

    def test_escapes_html(self):
        result = _text_to_html("<script>alert('xss')</script>")
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_newlines(self):
        assert "<br>" in _text_to_html("line1\nline2")
