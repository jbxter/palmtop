"""XMPP/Jabber channel using slixmpp.

Connects to any XMPP server (Prosody, ejabberd, etc.) and routes 1:1
messages and MUC mentions through the agent loop.

Requires: slixmpp (optional dependency via `palmtop[xmpp]`).

Config: [xmpp] section in config.toml or env vars.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import slixmpp

if TYPE_CHECKING:
    from palmtop.core.loop import AgentLoop

log = logging.getLogger(__name__)

# XMPP message limit — no hard protocol limit, but keep responses reasonable
MAX_XMPP_MESSAGE = 4096


class XmppChannel:
    """XMPP/Jabber messaging channel using slixmpp.

    Implements the Channel protocol (name, start, stop, send_message).
    Connects to an XMPP server, handles 1:1 messages and MUC mentions.
    """

    def __init__(
        self,
        jid: str,
        password: str,
        allowed_jids: list[str] | None = None,
        mucs: list[str] | None = None,
        muc_nick: str = "palmtop",
    ) -> None:
        if not jid:
            raise ValueError("XMPP JID is required")
        if not password:
            raise ValueError("XMPP password is required")

        self._jid = jid
        self._password = password
        self._allowed_jids = {j.lower() for j in allowed_jids} if allowed_jids else None
        self._mucs = mucs or []
        self._muc_nick = muc_nick
        self._agent: AgentLoop | None = None
        self._client: slixmpp.ClientXMPP | None = None
        self._stop_event = asyncio.Event()

    @property
    def name(self) -> str:
        return "xmpp"

    async def start(self, loop: AgentLoop) -> None:
        """Connect to XMPP server and listen for messages."""
        self._agent = loop

        self._client = slixmpp.ClientXMPP(self._jid, self._password)
        self._client.register_plugin("xep_0030")  # Service Discovery
        self._client.register_plugin("xep_0045")  # MUC
        self._client.register_plugin("xep_0199")  # Ping
        self._client.register_plugin("xep_0085")  # Chat State Notifications

        # Register event handlers
        self._client.add_event_handler("session_start", self._on_session_start)
        self._client.add_event_handler("message", self._on_message)
        self._client.add_event_handler("groupchat_message", self._on_muc_message)

        log.info("Connecting to XMPP as %s", self._jid)

        self._client.connect()

        # slixmpp uses its own asyncio event loop integration
        # Block until stop is requested
        await self._stop_event.wait()

    async def stop(self) -> None:
        """Disconnect from XMPP."""
        log.info("Stopping XMPP channel...")
        self._stop_event.set()
        if self._client:
            self._client.disconnect()

    async def send_message(self, user_id: str, text: str) -> None:
        """Send a message to a JID."""
        if not self._client:
            log.warning("XMPP not connected — cannot send to %s", user_id)
            return

        for chunk in _split_message(text):
            self._client.send_message(
                mto=user_id,
                mbody=chunk,
                mtype="chat",
            )

    # ── Internal ─────────────────────────────────────────────────────

    async def _on_session_start(self, event) -> None:
        """Handle successful session start."""
        log.info("XMPP session started as %s", self._jid)
        self._client.send_presence()
        await self._client.get_roster()

        # Join MUC rooms
        for muc in self._mucs:
            self._client.plugin["xep_0045"].join_muc(muc, self._muc_nick)
            log.info("Joined XMPP MUC: %s", muc)

    async def _on_message(self, msg) -> None:
        """Handle incoming 1:1 message."""
        if msg["type"] not in ("chat", "normal"):
            return

        # Ignore our own messages
        if msg["from"].bare == self._jid:
            return

        sender_jid = msg["from"].bare
        text = msg["body"].strip() if msg["body"] else ""

        if not text:
            return

        # Check allowlist
        if self._allowed_jids and sender_jid.lower() not in self._allowed_jids:
            log.debug("XMPP message from non-allowed JID: %s", sender_jid)
            return

        log.info("XMPP message from %s: %s", sender_jid, text[:80])

        if not self._agent:
            return

        # Send typing indicator
        self._client.send_message(
            mto=sender_jid,
            mtype="chat",
            mhtml="",
            mbody="",
        )
        # Chat state: composing
        chat_state_msg = self._client.make_message(mto=sender_jid, mtype="chat")
        chat_state_msg["chat_state"] = "composing"
        chat_state_msg.send()

        try:
            reply = await self._agent.handle(text, user_id=f"xmpp:{sender_jid}")
        except Exception:
            log.exception("Agent failed to handle XMPP message from %s", sender_jid)
            return

        # Chat state: active (done typing)
        active_msg = self._client.make_message(mto=sender_jid, mtype="chat")
        active_msg["chat_state"] = "active"
        active_msg.send()

        if reply and reply.strip():
            for chunk in _split_message(reply):
                self._client.send_message(
                    mto=sender_jid,
                    mbody=chunk,
                    mtype="chat",
                )

    async def _on_muc_message(self, msg) -> None:
        """Handle MUC (group chat) message — only respond to mentions."""
        # Ignore our own messages
        if msg["mucnick"] == self._muc_nick:
            return

        text = msg["body"].strip() if msg["body"] else ""
        if not text:
            return

        # Only respond if mentioned
        mention_prefixes = (
            f"{self._muc_nick}:",
            f"{self._muc_nick},",
            f"@{self._muc_nick}",
        )
        is_mention = any(text.lower().startswith(p.lower()) for p in mention_prefixes)
        if not is_mention:
            return

        # Strip mention prefix
        for prefix in mention_prefixes:
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix) :].strip()
                break

        if not text:
            return

        # Get sender JID from MUC nick
        sender_nick = msg["mucnick"]
        room_jid = msg["from"].bare

        # Check allowlist against nick (can't always resolve to bare JID in MUC)
        if self._allowed_jids:
            # Try to get real JID from MUC presence
            try:
                real_jid = self._client.plugin["xep_0045"].get_jid_property(room_jid, sender_nick, "jid")
                if real_jid and real_jid.bare.lower() not in self._allowed_jids:
                    return
            except Exception:
                # If we can't resolve JID, skip for safety
                return

        log.info("XMPP MUC mention from %s in %s: %s", sender_nick, room_jid, text[:80])

        if not self._agent:
            return

        try:
            reply = await self._agent.handle(text, user_id=f"xmpp:{sender_nick}@{room_jid}")
        except Exception:
            log.exception("Agent failed to handle XMPP MUC message")
            return

        if reply and reply.strip():
            for chunk in _split_message(reply):
                self._client.send_message(
                    mto=room_jid,
                    mbody=f"{sender_nick}: {chunk}",
                    mtype="groupchat",
                )


def _split_message(text: str) -> list[str]:
    """Split message into XMPP-friendly chunks.

    No hard protocol limit but keep messages under 4096 for readability.
    """
    if len(text) <= MAX_XMPP_MESSAGE:
        return [text]

    chunks: list[str] = []
    current = ""

    for paragraph in text.split("\n\n"):
        if not current:
            current = paragraph
        elif len(current) + 2 + len(paragraph) <= MAX_XMPP_MESSAGE:
            current += "\n\n" + paragraph
        else:
            chunks.append(current)
            current = paragraph

    if current:
        chunks.append(current)

    # Handle oversized chunks
    final: list[str] = []
    for chunk in chunks:
        while len(chunk) > MAX_XMPP_MESSAGE:
            split_at = chunk.rfind("\n", 0, MAX_XMPP_MESSAGE)
            if split_at == -1:
                split_at = chunk.rfind(" ", 0, MAX_XMPP_MESSAGE)
            if split_at == -1:
                split_at = MAX_XMPP_MESSAGE
            final.append(chunk[:split_at])
            chunk = chunk[split_at:].lstrip()
        if chunk:
            final.append(chunk)

    return final if final else [""]
