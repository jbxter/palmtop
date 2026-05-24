"""AgentMail email tool for the agent.

Gives the agent a real email inbox — he can read, send, reply to, and
manage email. Uses the AgentMail REST API (https://api.agentmail.to).

Config (config.toml):
    [email]
    api_key = "am_..."          # AgentMail API key
    inbox_id = "inbox_..."      # default inbox to use
    # OR set env vars: AGENTMAIL_API_KEY, AGENTMAIL_INBOX_ID
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx

from palmtop.tools.base import Tool

log = logging.getLogger(__name__)

BASE_URL = "https://api.agentmail.to"


class EmailTool(Tool):
    name = "email"
    description = (
        "Read and send email. Usage:\n"
        "  [TOOL:email] inbox — list messages in the inbox\n"
        "  [TOOL:email] read <message_id> — read a specific message\n"
        "  [TOOL:email] thread <thread_id> — read a full thread\n"
        "  [TOOL:email] send <to> | <subject> | <body> — send a new email\n"
        "  [TOOL:email] reply <message_id> | <body> — reply to a message\n"
        "  [TOOL:email] forward <message_id> | <to> — forward a message\n"
        "  [TOOL:email] threads — list conversation threads\n"
        "  [TOOL:email] search <query> — search messages (uses labels)\n"
        "  [TOOL:email] whoami — show inbox address"
    )

    def __init__(self, api_key: str, inbox_id: str = "") -> None:
        self._api_key = api_key
        self._inbox_id = inbox_id
        self._client: httpx.AsyncClient | None = None
        self._email_address: str = ""

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=BASE_URL,
                timeout=20.0,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        return self._client

    async def init(self) -> None:
        """Resolve inbox_id if not set, cache email address.

        Uses a short-lived client for the init request. The shared
        self._client is created lazily on first real use.
        """
        async with httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=10.0,
            headers={"Authorization": f"Bearer {self._api_key}"},
        ) as client:
            if not self._inbox_id:
                resp = await client.get("/inboxes", params={"limit": 1})
                if resp.status_code == 200:
                    inboxes = resp.json().get("inboxes", [])
                    if inboxes:
                        self._inbox_id = inboxes[0]["inbox_id"]
                        self._email_address = inboxes[0].get("email_address", "")
                        log.info("Email inbox: %s (%s)", self._inbox_id, self._email_address)
                    else:
                        log.warning("No AgentMail inboxes found — create one first")
                else:
                    log.warning("Failed to list inboxes: %s", resp.status_code)
            else:
                resp = await client.get(f"/inboxes/{self._inbox_id}")
                if resp.status_code == 200:
                    self._email_address = resp.json().get("email_address", "")
                    log.info("Email inbox: %s (%s)", self._inbox_id, self._email_address)
                else:
                    log.warning("Failed to get inbox %s: %s", self._inbox_id, resp.status_code)

    async def run(self, query: str) -> str:
        if not self._inbox_id:
            return "Email not configured — no inbox available."

        parts = query.strip().split(None, 1)
        if not parts:
            return self.description

        action = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        try:
            if action == "inbox":
                return await self._list_messages(rest)
            elif action == "read":
                return await self._read_message(rest)
            elif action == "thread":
                return await self._read_thread(rest)
            elif action == "threads":
                return await self._list_threads(rest)
            elif action == "send":
                return await self._send(rest)
            elif action == "reply":
                return await self._reply(rest)
            elif action == "forward":
                return await self._forward(rest)
            elif action == "search":
                return await self._search(rest)
            elif action == "whoami":
                return f"the agent's email: {self._email_address}"
            else:
                # Default: treat as inbox listing
                return await self._list_messages("")
        except Exception as e:
            log.exception("Email operation failed")
            return f"Email error: {e}"

    async def _list_messages(self, filter_str: str) -> str:
        client = self._get_client()
        params: dict = {"limit": 15}
        if filter_str:
            params["labels"] = [filter_str]

        resp = await client.get(f"/inboxes/{self._inbox_id}/messages", params=params)
        if resp.status_code != 200:
            return f"Failed to list messages ({resp.status_code}): {resp.text[:200]}"

        messages = resp.json().get("messages", [])
        if not messages:
            return "Inbox is empty." if not filter_str else f"No messages with label '{filter_str}'."

        lines = [f"Inbox ({len(messages)} messages):"]
        for m in messages:
            msg_id = m.get("message_id", "")[:12]
            subject = m.get("subject", "(no subject)")
            sender = m.get("from", "unknown")
            sent_at = _format_time(m.get("sent_at", ""))
            labels = m.get("labels", [])
            label_str = f" [{', '.join(labels)}]" if labels else ""
            lines.append(f"  {msg_id} | {sent_at} | {sender} | {subject}{label_str}")
        return "\n".join(lines)

    async def _read_message(self, message_id: str) -> str:
        message_id = message_id.strip()
        if not message_id:
            return "Need a message ID."

        client = self._get_client()
        resp = await client.get(f"/inboxes/{self._inbox_id}/messages/{message_id}")
        if resp.status_code == 404:
            return f"Message {message_id} not found."
        if resp.status_code != 200:
            return f"Failed to read message ({resp.status_code}): {resp.text[:200]}"

        return _format_message(resp.json())

    async def _read_thread(self, thread_id: str) -> str:
        thread_id = thread_id.strip()
        if not thread_id:
            return "Need a thread ID."

        client = self._get_client()
        resp = await client.get(f"/inboxes/{self._inbox_id}/threads/{thread_id}")
        if resp.status_code == 404:
            return f"Thread {thread_id} not found."
        if resp.status_code != 200:
            return f"Failed to read thread ({resp.status_code}): {resp.text[:200]}"

        data = resp.json()
        subject = data.get("subject", "(no subject)")
        messages = data.get("messages", [])
        lines = [f"Thread: {subject} ({len(messages)} messages)\n"]
        for m in messages:
            lines.append(_format_message(m))
            lines.append("---")
        return "\n".join(lines)

    async def _list_threads(self, filter_str: str) -> str:
        client = self._get_client()
        params: dict = {"limit": 15}
        if filter_str:
            params["labels"] = [filter_str]

        resp = await client.get(f"/inboxes/{self._inbox_id}/threads", params=params)
        if resp.status_code != 200:
            return f"Failed to list threads ({resp.status_code}): {resp.text[:200]}"

        threads = resp.json().get("threads", [])
        if not threads:
            return "No threads." if not filter_str else f"No threads with label '{filter_str}'."

        lines = [f"Threads ({len(threads)}):"]
        for t in threads:
            tid = t.get("thread_id", "")[:12]
            subject = t.get("subject", "(no subject)")
            updated = _format_time(t.get("updated_at", ""))
            msg_count = len(t.get("messages", []))
            labels = t.get("labels", [])
            label_str = f" [{', '.join(labels)}]" if labels else ""
            lines.append(f"  {tid} | {updated} | {subject} ({msg_count} msgs){label_str}")
        return "\n".join(lines)

    async def _send(self, text: str) -> str:
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 3:
            return "Format: send <to> | <subject> | <body>"
        to = parts[0]
        subject = parts[1]
        body = parts[2]

        # Wrap in branded HTML template
        from palmtop.brand import build_email_html
        email_html = build_email_html(body)

        client = self._get_client()
        payload = {
            "to": [to],
            "subject": subject,
            "text": body,
            "html": email_html,
        }
        resp = await client.post(
            f"/inboxes/{self._inbox_id}/messages/send",
            json=payload,
        )
        if resp.status_code not in (200, 201):
            return f"Failed to send ({resp.status_code}): {resp.text[:200]}"

        data = resp.json()
        return f"Sent to {to}: {subject} (message_id: {data.get('message_id', '?')})"

    async def _reply(self, text: str) -> str:
        parts = [p.strip() for p in text.split("|", 1)]
        if len(parts) < 2:
            return "Format: reply <message_id> | <reply body>"
        message_id = parts[0]
        body = parts[1]

        # Wrap in branded HTML template
        from palmtop.brand import build_email_html
        email_html = build_email_html(body)

        client = self._get_client()
        resp = await client.post(
            f"/inboxes/{self._inbox_id}/messages/{message_id}/reply",
            json={"text": body, "html": email_html, "reply_all": True},
        )
        if resp.status_code not in (200, 201):
            return f"Failed to reply ({resp.status_code}): {resp.text[:200]}"

        data = resp.json()
        return f"Reply sent (message_id: {data.get('message_id', '?')})"

    async def _forward(self, text: str) -> str:
        parts = [p.strip() for p in text.split("|", 1)]
        if len(parts) < 2:
            return "Format: forward <message_id> | <to>"
        message_id = parts[0]
        to = parts[1]

        client = self._get_client()
        resp = await client.post(
            f"/inboxes/{self._inbox_id}/messages/{message_id}/forward",
            json={"to": [to]},
        )
        if resp.status_code not in (200, 201):
            return f"Failed to forward ({resp.status_code}): {resp.text[:200]}"

        data = resp.json()
        return f"Forwarded to {to} (message_id: {data.get('message_id', '?')})"

    async def _search(self, query: str) -> str:
        """Search by listing messages with a label filter.

        AgentMail doesn't have a full-text search endpoint, so this
        filters by label. For broader search, list all and filter.
        """
        if not query:
            return "Need a search term."
        # Use label-based filtering
        return await self._list_messages(query)

    # ── Structured API methods (for auto-reply and watchers) ──────────

    @property
    def email_address(self) -> str:
        return self._email_address

    async def get_inbox_messages(self, limit: int = 15) -> list[dict]:
        """Return raw message dicts from the inbox (structured, not formatted)."""
        if not self._inbox_id:
            return []
        client = self._get_client()
        resp = await client.get(
            f"/inboxes/{self._inbox_id}/messages",
            params={"limit": limit},
        )
        if resp.status_code != 200:
            log.warning("Failed to list messages (%d)", resp.status_code)
            return []
        return resp.json().get("messages", [])

    async def get_thread_messages(self, thread_id: str) -> dict | None:
        """Return raw thread dict including all messages."""
        if not self._inbox_id or not thread_id:
            return None
        client = self._get_client()
        resp = await client.get(f"/inboxes/{self._inbox_id}/threads/{thread_id}")
        if resp.status_code != 200:
            log.warning("Failed to read thread %s (%d)", thread_id, resp.status_code)
            return None
        return resp.json()

    async def send_reply_text(self, message_id: str, body: str) -> str | None:
        """Reply to a message. Returns new message_id on success, None on failure."""
        if not self._inbox_id:
            return None
        from palmtop.brand import build_email_html
        email_html = build_email_html(body)

        client = self._get_client()
        resp = await client.post(
            f"/inboxes/{self._inbox_id}/messages/{message_id}/reply",
            json={"text": body, "html": email_html, "reply_all": True},
        )
        if resp.status_code not in (200, 201):
            log.warning("Failed to reply to %s (%d): %s", message_id, resp.status_code, resp.text[:200])
            return None
        return resp.json().get("message_id")

    async def send_email(
        self, to: str, subject: str, body: str, html: str = "",
    ) -> str | None:
        """Send a new email programmatically. Returns message_id on success.

        Args:
            to: Recipient email address.
            subject: Email subject line.
            body: Plain-text body (always included as fallback).
            html: Optional HTML body.  If empty, auto-generates branded HTML.
        """
        if not self._inbox_id:
            return None
        if not html:
            from palmtop.brand import build_email_html
            html = build_email_html(body)
        client = self._get_client()
        payload: dict = {"to": [to], "subject": subject, "text": body, "html": html}
        resp = await client.post(
            f"/inboxes/{self._inbox_id}/messages/send",
            json=payload,
        )
        if resp.status_code not in (200, 201):
            log.warning("Failed to send to %s (%d): %s", to, resp.status_code, resp.text[:200])
            return None
        return resp.json().get("message_id")

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()


def _format_time(iso: str) -> str:
    """Format ISO timestamp to readable short form."""
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%b %d %H:%M")
    except (ValueError, TypeError):
        return iso[:16]


def _format_message(m: dict) -> str:
    """Format a single message for display."""
    msg_id = m.get("message_id", "?")
    subject = m.get("subject", "(no subject)")
    sender = m.get("from", "unknown")
    to = ", ".join(m.get("to", []))
    cc = ", ".join(m.get("cc", []))
    sent_at = _format_time(m.get("sent_at", ""))
    labels = m.get("labels", [])

    lines = [
        f"From: {sender}",
        f"To: {to}",
    ]
    if cc:
        lines.append(f"Cc: {cc}")
    lines.append(f"Date: {sent_at}")
    lines.append(f"Subject: {subject}")
    if labels:
        lines.append(f"Labels: {', '.join(labels)}")
    lines.append(f"ID: {msg_id}")

    # Prefer extracted_text (reply/forward stripped), fall back to text, then html
    body = m.get("extracted_text") or m.get("text") or ""
    if not body:
        html = m.get("extracted_html") or m.get("html") or ""
        if html:
            import re
            body = re.sub(r"<[^>]+>", " ", html)
            body = re.sub(r"\s+", " ", body).strip()

    if body:
        lines.append("")
        lines.append(body[:3000])
        if len(body) > 3000:
            lines.append("... (truncated)")

    attachments = m.get("attachments", [])
    if attachments:
        lines.append(f"\nAttachments ({len(attachments)}):")
        for a in attachments:
            name = a.get("filename", "unnamed")
            ct = a.get("content_type", "")
            lines.append(f"  - {name} ({ct})")

    return "\n".join(lines)
