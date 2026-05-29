"""Tests for the WhatsApp Business Cloud API channel."""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import AsyncMock, MagicMock

import pytest

from palmtop.channels.whatsapp import (
    MAX_WA_MESSAGE,
    WhatsAppChannel,
    _split_message,
)


class _FakeRequest:
    """Minimal stand-in for a Starlette Request used by the webhook handlers."""

    def __init__(self, *, body: bytes = b"", headers: dict | None = None, query: dict | None = None):
        self._body = body
        self.headers = headers or {}
        self.query_params = query or {}

    async def body(self) -> bytes:
        return self._body


class TestWhatsAppChannelInit:
    def test_requires_phone_number_id(self):
        with pytest.raises(ValueError, match="phone_number_id"):
            WhatsAppChannel(phone_number_id="", access_token="tok", verify_token="vtok")

    def test_requires_access_token(self):
        with pytest.raises(ValueError, match="access_token"):
            WhatsAppChannel(phone_number_id="123", access_token="", verify_token="vtok")

    def test_requires_verify_token(self):
        with pytest.raises(ValueError, match="verify_token"):
            WhatsAppChannel(phone_number_id="123", access_token="tok", verify_token="")

    def test_basic_init(self):
        ch = WhatsAppChannel(
            phone_number_id="123456",
            access_token="access_tok",
            verify_token="verify_tok",
        )
        assert ch.name == "whatsapp"
        assert ch._phone_number_id == "123456"
        assert ch._allowed_numbers is None

    def test_custom_config(self):
        ch = WhatsAppChannel(
            phone_number_id="123456",
            access_token="tok",
            verify_token="vtok",
            allowed_numbers=["+15551234567", "+15559876543"],
            webhook_port=9090,
            webhook_path="/wa",
        )
        assert ch._allowed_numbers == {"+15551234567", "+15559876543"}
        assert ch._webhook_port == 9090
        assert ch._webhook_path == "/wa"


class TestWhatsAppHandleMessage:
    @pytest.fixture
    def channel(self):
        ch = WhatsAppChannel(
            phone_number_id="123",
            access_token="tok",
            verify_token="vtok",
            allowed_numbers=["+15551234567"],
        )
        ch._agent = AsyncMock()
        ch._agent.handle = AsyncMock(return_value="Agent reply!")
        ch._client = AsyncMock()
        ch._client.post = AsyncMock(return_value=MagicMock(status_code=200))
        return ch

    @pytest.mark.asyncio
    async def test_handles_text_message(self, channel):
        message = {
            "from": "+15551234567",
            "type": "text",
            "id": "msg_001",
            "text": {"body": "Hello agent"},
        }
        await channel._handle_message(message)
        channel._agent.handle.assert_called_once()
        call_args = channel._agent.handle.call_args
        assert call_args[0][0] == "Hello agent"
        assert call_args[1]["user_id"] == "whatsapp:+15551234567"

    @pytest.mark.asyncio
    async def test_rejects_non_allowed_number(self, channel):
        message = {
            "from": "+19999999999",
            "type": "text",
            "id": "msg_002",
            "text": {"body": "Hello"},
        }
        await channel._handle_message(message)
        channel._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_unsupported_type(self, channel):
        message = {
            "from": "+15551234567",
            "type": "image",
            "id": "msg_003",
        }
        await channel._handle_message(message)
        channel._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_reply(self, channel):
        message = {
            "from": "+15551234567",
            "type": "text",
            "id": "msg_004",
            "text": {"body": "Hi"},
        }
        await channel._handle_message(message)
        # Should have sent a reply via the API
        send_calls = [c for c in channel._client.post.call_args_list if "/123/messages" in str(c)]
        assert len(send_calls) >= 1

    @pytest.mark.asyncio
    async def test_marks_as_read(self, channel):
        message = {
            "from": "+15551234567",
            "type": "text",
            "id": "msg_005",
            "text": {"body": "Hi"},
        }
        await channel._handle_message(message)
        # One call for read receipt, one for reply
        assert channel._client.post.call_count >= 2

    @pytest.mark.asyncio
    async def test_handles_interactive_button_reply(self, channel):
        message = {
            "from": "+15551234567",
            "type": "interactive",
            "id": "msg_006",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": "btn_1", "title": "Yes please"},
            },
        }
        await channel._handle_message(message)
        channel._agent.handle.assert_called_once()
        assert channel._agent.handle.call_args[0][0] == "Yes please"


class TestWhatsAppSendMessage:
    @pytest.mark.asyncio
    async def test_sends_text(self):
        ch = WhatsAppChannel(
            phone_number_id="123",
            access_token="tok",
            verify_token="vtok",
        )
        ch._client = AsyncMock()
        ch._client.post = AsyncMock(return_value=MagicMock(status_code=200))

        await ch.send_message("+15551234567", "Hello!")
        ch._client.post.assert_called_once()
        call_args = ch._client.post.call_args
        payload = call_args[1]["json"]
        assert payload["to"] == "+15551234567"
        assert payload["text"]["body"] == "Hello!"

    @pytest.mark.asyncio
    async def test_send_when_not_connected(self):
        ch = WhatsAppChannel(
            phone_number_id="123",
            access_token="tok",
            verify_token="vtok",
        )
        # No client
        await ch.send_message("+15551234567", "hi")
        # Should not raise


class TestWhatsAppProcessNotification:
    @pytest.fixture
    def channel(self):
        ch = WhatsAppChannel(
            phone_number_id="123",
            access_token="tok",
            verify_token="vtok",
            allowed_numbers=["+15551234567"],
        )
        ch._agent = AsyncMock()
        ch._agent.handle = AsyncMock(return_value="Reply")
        ch._client = AsyncMock()
        ch._client.post = AsyncMock(return_value=MagicMock(status_code=200))
        return ch

    @pytest.mark.asyncio
    async def test_processes_full_notification(self, channel):
        data = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "+15551234567",
                                        "type": "text",
                                        "id": "msg_100",
                                        "text": {"body": "Test"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        await channel._process_notification(data)
        channel._agent.handle.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_empty_notification(self, channel):
        await channel._process_notification({})
        channel._agent.handle.assert_not_called()


class TestWhatsAppWebhookSignature:
    """Webhook authenticity — issue #28 (fail closed without app_secret)."""

    def _channel(self, app_secret: str = ""):
        # The webhook handlers construct Starlette responses; skip (don't fail)
        # when the optional `web` dependency isn't installed (e.g. CI --extra dev).
        pytest.importorskip("starlette")
        ch = WhatsAppChannel(
            phone_number_id="123",
            access_token="tok",
            verify_token="vtok",
            app_secret=app_secret,
            allowed_numbers=["+15551234567"],
        )
        ch._agent = AsyncMock()
        ch._agent.handle = AsyncMock(return_value="reply")
        return ch

    @pytest.mark.asyncio
    async def test_rejects_when_app_secret_unset(self):
        ch = self._channel(app_secret="")
        resp = await ch._handle_webhook(_FakeRequest(body=b'{"entry": []}'))
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_rejects_bad_signature(self):
        ch = self._channel(app_secret="s3cret")
        req = _FakeRequest(body=b'{"entry": []}', headers={"x-hub-signature-256": "sha256=deadbeef"})
        resp = await ch._handle_webhook(req)
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_accepts_valid_signature(self):
        secret = "s3cret"
        ch = self._channel(app_secret=secret)
        body = b'{"entry": []}'
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        req = _FakeRequest(body=body, headers={"x-hub-signature-256": sig})
        resp = await ch._handle_webhook(req)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_verify_token_match(self):
        ch = self._channel(app_secret="s")
        req = _FakeRequest(query={"hub.mode": "subscribe", "hub.verify_token": "vtok", "hub.challenge": "ping"})
        resp = await ch._handle_verify(req)
        assert resp.status_code == 200
        assert resp.body == b"ping"

    @pytest.mark.asyncio
    async def test_verify_token_rejects_wrong(self):
        ch = self._channel(app_secret="s")
        req = _FakeRequest(query={"hub.mode": "subscribe", "hub.verify_token": "WRONG", "hub.challenge": "ping"})
        resp = await ch._handle_verify(req)
        assert resp.status_code == 403


class TestWhatsAppSplitMessage:
    def test_short_message(self):
        assert _split_message("Hello") == ["Hello"]

    def test_message_at_limit(self):
        msg = "A" * MAX_WA_MESSAGE
        assert _split_message(msg) == [msg]

    def test_long_message_splits_at_paragraph(self):
        p1 = "First paragraph. " * 100  # ~1700 chars
        p2 = "Second paragraph. " * 100
        p3 = "Third paragraph. " * 100
        text = f"{p1}\n\n{p2}\n\n{p3}"
        chunks = _split_message(text)
        assert len(chunks) >= 2
        assert all(len(c) <= MAX_WA_MESSAGE for c in chunks)

    def test_very_long_no_breaks(self):
        text = "A" * (MAX_WA_MESSAGE + 100)
        chunks = _split_message(text)
        assert len(chunks) == 2
        assert all(len(c) <= MAX_WA_MESSAGE for c in chunks)

    def test_preserves_content(self):
        text = "Hello\n\nWorld"
        chunks = _split_message(text)
        assert "".join(chunks).replace("\n\n", "") == "HelloWorld"
