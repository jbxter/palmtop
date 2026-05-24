"""WhatsApp Business Cloud API channel.

Receives messages via webhook, sends replies through the Meta Graph API.
Uses the official WhatsApp Business Cloud API — free tier allows 1,000
service conversations/month.

Requires: httpx (already a project dependency for email channel).

Config: [whatsapp] section in config.toml or env vars.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from palmtop.core.loop import AgentLoop

log = logging.getLogger(__name__)

# WhatsApp message limit — UX degrades past 4096 despite 65536 technical limit
MAX_WA_MESSAGE = 4096

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"


class WhatsAppChannel:
    """WhatsApp Business Cloud API channel.

    Implements the Channel protocol (name, start, stop, send_message).
    Receives inbound messages via webhook, sends outbound via Graph API.
    """

    def __init__(
        self,
        phone_number_id: str,
        access_token: str,
        verify_token: str,
        app_secret: str = "",
        allowed_numbers: list[str] | None = None,
        webhook_port: int = 8080,
        webhook_path: str = "/webhook/whatsapp",
    ) -> None:
        if not phone_number_id:
            raise ValueError("WhatsApp phone_number_id is required")
        if not access_token:
            raise ValueError("WhatsApp access_token is required")
        if not verify_token:
            raise ValueError("WhatsApp verify_token is required")

        self._phone_number_id = phone_number_id
        self._access_token = access_token
        self._verify_token = verify_token
        self._app_secret = app_secret
        self._allowed_numbers = set(allowed_numbers) if allowed_numbers else None
        self._webhook_port = webhook_port
        self._webhook_path = webhook_path
        self._agent: AgentLoop | None = None
        self._client: httpx.AsyncClient | None = None
        self._server: asyncio.Server | None = None
        self._stop_event = asyncio.Event()

    @property
    def name(self) -> str:
        return "whatsapp"

    async def start(self, loop: AgentLoop) -> None:
        """Start webhook server and listen for messages."""
        self._agent = loop
        self._client = httpx.AsyncClient(
            base_url=GRAPH_API_BASE,
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=30.0,
        )

        log.info(
            "WhatsApp channel starting (phone_number_id=%s, webhook_port=%d)",
            self._phone_number_id,
            self._webhook_port,
        )

        from starlette.applications import Starlette
        from starlette.routing import Route

        app = Starlette(
            routes=[
                Route(self._webhook_path, self._handle_verify, methods=["GET"]),
                Route(self._webhook_path, self._handle_webhook, methods=["POST"]),
            ]
        )

        import uvicorn

        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=self._webhook_port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        await server.serve()

    async def stop(self) -> None:
        """Stop the webhook server."""
        log.info("Stopping WhatsApp channel...")
        self._stop_event.set()
        if self._client:
            await self._client.aclose()
            self._client = None

    async def send_message(self, user_id: str, text: str) -> None:
        """Send a text message to a WhatsApp number."""
        if not self._client:
            log.warning("WhatsApp client not ready — cannot send to %s", user_id)
            return

        for chunk in _split_message(text):
            payload = {
                "messaging_product": "whatsapp",
                "to": user_id,
                "type": "text",
                "text": {"body": chunk},
            }
            try:
                resp = await self._client.post(
                    f"/{self._phone_number_id}/messages",
                    json=payload,
                )
                if resp.status_code != 200:
                    log.error("WhatsApp send failed (%d): %s", resp.status_code, resp.text[:200])
            except httpx.HTTPError as e:
                log.error("WhatsApp send error: %s", e)

    # ── Webhook handlers ────────────────────────────────────────────

    async def _handle_verify(self, request):
        """Handle webhook verification challenge from Meta."""
        from starlette.responses import PlainTextResponse

        mode = request.query_params.get("hub.mode")
        token = request.query_params.get("hub.verify_token")
        challenge = request.query_params.get("hub.challenge")

        if mode == "subscribe" and token == self._verify_token:
            log.info("WhatsApp webhook verified")
            return PlainTextResponse(challenge or "")
        return PlainTextResponse("Forbidden", status_code=403)

    async def _handle_webhook(self, request):
        """Handle inbound webhook notification from Meta."""
        from starlette.responses import PlainTextResponse

        body = await request.body()

        # Verify signature if app_secret is configured
        if self._app_secret:
            signature = request.headers.get("x-hub-signature-256", "")
            expected = (
                "sha256="
                + hmac.new(
                    self._app_secret.encode(),
                    body,
                    hashlib.sha256,
                ).hexdigest()
            )
            if not hmac.compare_digest(signature, expected):
                log.warning("WhatsApp webhook signature mismatch")
                return PlainTextResponse("Invalid signature", status_code=403)

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return PlainTextResponse("Bad request", status_code=400)

        # Process messages asynchronously
        asyncio.create_task(self._process_notification(data))

        # Always return 200 quickly to acknowledge receipt
        return PlainTextResponse("OK")

    async def _process_notification(self, data: dict) -> None:
        """Process a webhook notification payload."""
        try:
            entries = data.get("entry", [])
            for entry in entries:
                changes = entry.get("changes", [])
                for change in changes:
                    value = change.get("value", {})
                    messages = value.get("messages", [])
                    for message in messages:
                        await self._handle_message(message)
        except Exception:
            log.exception("Error processing WhatsApp notification")

    async def _handle_message(self, message: dict) -> None:
        """Handle a single inbound message."""
        sender = message.get("from", "")
        msg_type = message.get("type", "")

        if not sender:
            return

        # Check allowlist
        if self._allowed_numbers and sender not in self._allowed_numbers:
            log.debug("WhatsApp message from non-allowed number: %s", sender)
            return

        # Extract text content
        text = ""
        if msg_type == "text":
            text = message.get("text", {}).get("body", "")
        elif msg_type == "interactive":
            # Button/list replies
            interactive = message.get("interactive", {})
            if interactive.get("type") == "button_reply":
                text = interactive.get("button_reply", {}).get("title", "")
            elif interactive.get("type") == "list_reply":
                text = interactive.get("list_reply", {}).get("title", "")
        else:
            # Unsupported message type — acknowledge but skip
            log.debug("WhatsApp unsupported message type: %s from %s", msg_type, sender)
            return

        if not text.strip():
            return

        log.info("WhatsApp message from %s: %s", sender, text[:80])

        if not self._agent:
            return

        # Mark as read
        await self._mark_read(message.get("id", ""))

        try:
            reply = await self._agent.handle(text, user_id=f"whatsapp:{sender}")
        except Exception:
            log.exception("Agent failed to handle WhatsApp message from %s", sender)
            return

        if reply and reply.strip():
            await self.send_message(sender, reply)

    async def _mark_read(self, message_id: str) -> None:
        """Send read receipt for a message."""
        if not self._client or not message_id:
            return
        try:
            await self._client.post(
                f"/{self._phone_number_id}/messages",
                json={
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": message_id,
                },
            )
        except httpx.HTTPError:
            pass  # Non-critical


def _split_message(text: str) -> list[str]:
    """Split message into WhatsApp-friendly chunks.

    WhatsApp supports up to 65,536 chars but UX degrades past ~4096.
    Split at paragraph boundaries first, then by length.
    """
    if len(text) <= MAX_WA_MESSAGE:
        return [text]

    chunks: list[str] = []
    current = ""

    for paragraph in text.split("\n\n"):
        if not current:
            current = paragraph
        elif len(current) + 2 + len(paragraph) <= MAX_WA_MESSAGE:
            current += "\n\n" + paragraph
        else:
            chunks.append(current)
            current = paragraph

    if current:
        chunks.append(current)

    # Handle any single chunk that's still too long
    final: list[str] = []
    for chunk in chunks:
        while len(chunk) > MAX_WA_MESSAGE:
            split_at = chunk.rfind("\n", 0, MAX_WA_MESSAGE)
            if split_at == -1:
                split_at = chunk.rfind(" ", 0, MAX_WA_MESSAGE)
            if split_at == -1:
                split_at = MAX_WA_MESSAGE
            final.append(chunk[:split_at])
            chunk = chunk[split_at:].lstrip()
        if chunk:
            final.append(chunk)

    return final if final else [""]
