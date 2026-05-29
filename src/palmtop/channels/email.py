"""Email channel — inbound email via AgentMail polling.

Polls the AgentMail inbox for new messages at a configurable interval.
When a new message arrives, routes it to the agent loop and replies to
the sender with the agent's response.

Requires: AGENTMAIL_API_KEY env var or [email] section in config.toml.
No extra dependencies — uses httpx (already a core dep).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from email.utils import parseaddr
from typing import TYPE_CHECKING

import httpx

from palmtop.channels.auth import log_access_policy, sender_allowed

if TYPE_CHECKING:
    from palmtop.core.loop import AgentLoop

log = logging.getLogger(__name__)

# Default polling interval in seconds
DEFAULT_POLL_INTERVAL = 30
# How many messages to fetch per poll
POLL_BATCH_SIZE = 20

BASE_URL = "https://api.agentmail.to"


class EmailChannel:
    """Inbound email channel using AgentMail.

    Polls the inbox for new messages, routes them through the agent loop,
    and replies to the sender with the agent's response.

    Implements the Channel protocol (name, start, stop, send_message).
    """

    def __init__(
        self,
        api_key: str,
        inbox_id: str = "",
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        allowed_senders: list[str] | None = None,
        allow_anyone: bool = False,
    ) -> None:
        if not api_key:
            raise ValueError("AGENTMAIL_API_KEY is required for email channel")
        self._api_key = api_key
        self._inbox_id = inbox_id
        self._poll_interval = poll_interval
        self._allowed_senders = (
            {parseaddr(s)[1].lower() or s.lower() for s in allowed_senders} if allowed_senders else None
        )
        self._allow_anyone = allow_anyone
        log_access_policy(log, "email", self._allowed_senders, allow_anyone=allow_anyone)
        self._client: httpx.AsyncClient | None = None
        self._stop_event = asyncio.Event()
        self._seen: set[str] = set()
        self._email_address: str = ""
        self._agent: AgentLoop | None = None

    @property
    def name(self) -> str:
        return "email"

    async def start(self, loop: AgentLoop) -> None:
        """Start polling for inbound email. Blocks until stop() is called."""
        self._agent = loop
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=20.0,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        # Resolve inbox
        await self._resolve_inbox()
        if not self._inbox_id:
            log.error("Email channel: no inbox available — stopping")
            return

        # Seed seen set with existing messages to avoid replying to old mail
        await self._seed_seen()

        log.info(
            "Email channel started: %s (polling every %ds, %d existing messages seeded)",
            self._email_address or self._inbox_id,
            self._poll_interval,
            len(self._seen),
        )

        # Poll loop
        while not self._stop_event.is_set():
            try:
                await self._poll_inbox()
            except httpx.HTTPError as e:
                log.warning("Email poll HTTP error: %s", e)
            except Exception:
                log.exception("Email poll error")

            # Wait for interval or stop signal
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval,
                )
                break  # stop_event was set
            except TimeoutError:
                continue  # interval elapsed, poll again

    async def stop(self) -> None:
        """Stop polling and close connections."""
        log.info("Stopping email channel...")
        self._stop_event.set()
        if self._client:
            await self._client.aclose()
            self._client = None

    async def send_message(self, user_id: str, text: str) -> None:
        """Send an email to a user (proactive notification).

        user_id is the recipient email address.
        """
        if not self._client or not self._inbox_id:
            log.warning("Email channel not ready — cannot send to %s", user_id)
            return

        # Build HTML body
        html = _plain_to_html(text)

        payload = {
            "to": [user_id],
            "subject": _extract_subject(text),
            "text": text,
            "html": html,
        }
        try:
            resp = await self._client.post(
                f"/inboxes/{self._inbox_id}/messages/send",
                json=payload,
            )
            if resp.status_code in (200, 201):
                log.info("Email sent to %s", user_id)
            else:
                log.warning("Failed to send email to %s (%d): %s", user_id, resp.status_code, resp.text[:200])
        except httpx.HTTPError as e:
            log.warning("Email send failed: %s", e)

    # ── Internal ─────────────────────────────────────────────────────

    async def _resolve_inbox(self) -> None:
        """Resolve inbox_id if not set, cache email address."""
        if not self._client:
            return

        if not self._inbox_id:
            resp = await self._client.get("/inboxes", params={"limit": 1})
            if resp.status_code == 200:
                inboxes = resp.json().get("inboxes", [])
                if inboxes:
                    self._inbox_id = inboxes[0]["inbox_id"]
                    self._email_address = inboxes[0].get("email_address", "")
                else:
                    log.error("No AgentMail inboxes found")
            else:
                log.error("Failed to list inboxes: %s", resp.status_code)
        else:
            resp = await self._client.get(f"/inboxes/{self._inbox_id}")
            if resp.status_code == 200:
                self._email_address = resp.json().get("email_address", "")
            else:
                log.warning("Failed to get inbox %s: %s", self._inbox_id, resp.status_code)

    async def _seed_seen(self) -> None:
        """Load existing message IDs so we don't reply to old messages on startup."""
        if not self._client or not self._inbox_id:
            return

        resp = await self._client.get(
            f"/inboxes/{self._inbox_id}/messages",
            params={"limit": 50},
        )
        if resp.status_code == 200:
            messages = resp.json().get("messages", [])
            for m in messages:
                msg_id = m.get("message_id", "")
                if msg_id:
                    self._seen.add(msg_id)

    async def _poll_inbox(self) -> None:
        """Check for new messages and route them through the agent."""
        if not self._client or not self._inbox_id or not self._agent:
            return

        resp = await self._client.get(
            f"/inboxes/{self._inbox_id}/messages",
            params={"limit": POLL_BATCH_SIZE},
        )
        if resp.status_code != 200:
            log.warning("Inbox poll failed (%d)", resp.status_code)
            return

        messages = resp.json().get("messages", [])
        new_messages = []
        for m in messages:
            msg_id = m.get("message_id", "")
            if not msg_id or msg_id in self._seen:
                continue
            self._seen.add(msg_id)
            new_messages.append(m)

        for msg in new_messages:
            await self._handle_message(msg)

    async def _handle_message(self, msg: dict) -> None:
        """Process a single inbound email through the agent loop."""
        sender = msg.get("from", "")
        subject = msg.get("subject", "(no subject)")
        msg_id = msg.get("message_id", "")
        body = msg.get("extracted_text") or msg.get("text") or ""

        # Filter by allowed senders (fail closed when unconfigured). Compare the
        # parsed address exactly — substring matching would admit lookalikes like
        # me@example.com.evil.com or a spoofed display name.
        sender_addr = parseaddr(sender)[1].lower()
        if not sender_allowed(sender_addr, self._allowed_senders, allow_anyone=self._allow_anyone):
            log.debug("Email from non-allowed sender %s — skipping", sender)
            return

        if not body.strip():
            log.debug("Empty email body from %s (subject: %s) — skipping", sender, subject)
            return

        log.info("New email from %s: %s", sender, subject)

        # Build a context-rich prompt for the agent
        prompt = _format_email_for_agent(sender, subject, body)

        # Route through agent
        try:
            reply = await self._agent.handle(prompt, user_id=f"email:{sender}")
        except Exception:
            log.exception("Agent failed to handle email from %s", sender)
            return

        if not reply or not reply.strip():
            log.debug("Empty agent reply for email from %s — not replying", sender)
            return

        # Reply to the email
        await self._reply_to_message(msg_id, reply)

    async def _reply_to_message(self, message_id: str, body: str) -> None:
        """Send a reply to a specific message."""
        if not self._client or not self._inbox_id:
            return

        html = _plain_to_html(body)

        try:
            resp = await self._client.post(
                f"/inboxes/{self._inbox_id}/messages/{message_id}/reply",
                json={"text": body, "html": html, "reply_all": False},
            )
            if resp.status_code in (200, 201):
                log.info("Replied to message %s", message_id[:12])
            else:
                log.warning("Reply failed (%d): %s", resp.status_code, resp.text[:200])
        except httpx.HTTPError as e:
            log.warning("Reply HTTP error: %s", e)

    @property
    def email_address(self) -> str:
        """The inbox email address (available after start)."""
        return self._email_address


def _format_email_for_agent(sender: str, subject: str, body: str) -> str:
    """Format an inbound email as a prompt for the agent loop."""
    # Truncate very long emails
    if len(body) > 4000:
        body = body[:4000] + "\n\n[... truncated]"

    return f"[EMAIL from {sender}]\nSubject: {subject}\n---\n{body}\n---\nPlease reply to this email."


def _extract_subject(text: str) -> str:
    """Extract a subject line from agent reply text (first line, max 78 chars)."""
    first_line = text.strip().split("\n", 1)[0]
    if len(first_line) <= 78:
        return first_line
    return first_line[:75] + "..."


def _plain_to_html(text: str) -> str:
    """Convert plain text to simple HTML (paragraphs + line breaks)."""
    import html

    escaped = html.escape(text)
    paragraphs = escaped.split("\n\n")
    html_parts = [f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs if p.strip()]
    return "\n".join(html_parts)


def _format_time(iso: str) -> str:
    """Format ISO timestamp to readable short form."""
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%b %d %H:%M")
    except (ValueError, TypeError):
        return iso[:16]
