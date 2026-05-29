"""Matrix channel — messaging via matrix-nio (Element compatible).

Connects to a Matrix homeserver, listens for messages in joined rooms,
routes them through the agent loop, and replies in the same room.

Works with matrix.org and self-hosted homeservers.

Requires: pip install palmtop[matrix]  (matrix-nio >= 0.24)
Config:   [matrix] section in config.toml or env vars:
          MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from nio import AsyncClient, MatrixRoom, RoomMessageText

from palmtop.channels.auth import log_access_policy, sender_allowed

if TYPE_CHECKING:
    from palmtop.core.loop import AgentLoop

log = logging.getLogger(__name__)

# Matrix doesn't have a strict char limit like Discord/Telegram, but
# very long messages are unwieldy. Split around 4000 for readability.
MAX_MESSAGE_LENGTH = 4000


class MatrixChannel:
    """Matrix messaging channel via matrix-nio.

    Implements the Channel protocol (name, start, stop, send_message).
    Listens for text messages in joined rooms and responds.
    """

    def __init__(
        self,
        homeserver: str,
        user_id: str,
        access_token: str,
        allowed_users: list[str] | None = None,
        allowed_rooms: list[str] | None = None,
        allow_anyone: bool = False,
    ) -> None:
        if not homeserver:
            raise ValueError("MATRIX_HOMESERVER is required")
        if not user_id:
            raise ValueError("MATRIX_USER_ID is required")
        if not access_token:
            raise ValueError("MATRIX_ACCESS_TOKEN is required")

        self._homeserver = homeserver
        self._user_id = user_id
        self._access_token = access_token
        self._allowed_users = set(allowed_users) if allowed_users else None
        self._allowed_rooms = set(allowed_rooms) if allowed_rooms else None
        self._allow_anyone = allow_anyone
        log_access_policy(log, "matrix", self._allowed_users, allow_anyone=allow_anyone)
        self._agent: AgentLoop | None = None
        self._stop_event = asyncio.Event()
        self._client: AsyncClient | None = None
        self._synced = False  # Skip messages from initial sync

    @property
    def name(self) -> str:
        return "matrix"

    async def start(self, loop: AgentLoop) -> None:
        """Connect to Matrix and start syncing. Blocks until stop()."""
        self._agent = loop

        self._client = AsyncClient(self._homeserver, self._user_id)
        self._client.access_token = self._access_token

        # Register message callback
        self._client.add_event_callback(self._on_message, RoomMessageText)

        log.info("Connecting to Matrix: %s as %s", self._homeserver, self._user_id)

        # Do initial sync to get current state (skip old messages)
        resp = await self._client.sync(timeout=10000)
        if hasattr(resp, "next_batch"):
            self._synced = True
            log.info("Matrix initial sync complete — listening for new messages")
        else:
            log.warning("Matrix initial sync issue: %s", resp)

        # Sync loop — runs until stopped
        while not self._stop_event.is_set():
            try:
                await self._client.sync(timeout=30000)
            except Exception:
                if self._stop_event.is_set():
                    break
                log.exception("Matrix sync error — retrying in 5s")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=5.0)
                    break
                except TimeoutError:
                    continue

        log.info("Matrix channel stopped")

    async def stop(self) -> None:
        """Disconnect from Matrix."""
        log.info("Stopping Matrix channel...")
        self._stop_event.set()
        if self._client:
            await self._client.close()

    async def send_message(self, user_id: str, text: str) -> None:
        """Send a message to a user or room.

        If user_id starts with '!' it's treated as a room_id.
        Otherwise, tries to find or create a DM room with the user.
        """
        if not self._client:
            log.warning("Matrix client not ready — cannot send to %s", user_id)
            return

        try:
            if user_id.startswith("!"):
                # Direct room_id
                room_id = user_id
            else:
                # Find DM room with user (or create one)
                room_id = await self._find_dm_room(user_id)
                if not room_id:
                    log.warning("Could not find/create DM room with %s", user_id)
                    return

            for chunk in _split_message(text):
                await self._client.room_send(
                    room_id=room_id,
                    message_type="m.room.message",
                    content={
                        "msgtype": "m.text",
                        "body": chunk,
                        "format": "org.matrix.custom.html",
                        "formatted_body": _text_to_html(chunk),
                    },
                )
            log.info("Sent Matrix message to %s", user_id)
        except Exception:
            log.exception("Failed to send Matrix message to %s", user_id)

    # ── Internal ─────────────────────────────────────────────────────

    async def _on_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        """Handle an incoming Matrix text message."""
        # Skip messages before initial sync completed
        if not self._synced:
            return

        # Skip our own messages
        if event.sender == self._user_id:
            return

        # Room filter
        if self._allowed_rooms and room.room_id not in self._allowed_rooms:
            return

        # User filter (fail closed when unconfigured)
        if not sender_allowed(event.sender, self._allowed_users, allow_anyone=self._allow_anyone):
            log.debug("Matrix message from non-allowed user %s — skipping", event.sender)
            return

        text = event.body.strip()
        if not text:
            return

        log.info("Matrix message from %s in %s: %s", event.sender, room.display_name, text[:80])

        if not self._agent:
            return

        # Show typing indicator
        if self._client:
            try:
                await self._client.room_typing(room.room_id, typing_state=True, timeout=30000)
            except Exception:
                pass

        # Process through agent
        try:
            reply = await self._agent.handle(text, user_id=f"matrix:{event.sender}")
        except Exception:
            log.exception("Agent failed to handle Matrix message from %s", event.sender)
            return
        finally:
            # Stop typing
            if self._client:
                try:
                    await self._client.room_typing(room.room_id, typing_state=False)
                except Exception:
                    pass

        if not reply or not reply.strip():
            return

        # Send reply
        try:
            for chunk in _split_message(reply):
                await self._client.room_send(
                    room_id=room.room_id,
                    message_type="m.room.message",
                    content={
                        "msgtype": "m.text",
                        "body": chunk,
                        "format": "org.matrix.custom.html",
                        "formatted_body": _text_to_html(chunk),
                    },
                )
        except Exception:
            log.exception("Failed to send Matrix reply in %s", room.room_id)

    async def _find_dm_room(self, user_id: str) -> str | None:
        """Find an existing DM room with a user, or create one."""
        if not self._client:
            return None

        # Check joined rooms for a DM with this user
        for room_id, room in self._client.rooms.items():
            if room.member_count == 2 and user_id in [m.user_id for m in room.users.values()]:
                return room_id

        # Create new DM room
        try:
            resp = await self._client.room_create(
                is_direct=True,
                invite=[user_id],
            )
            if hasattr(resp, "room_id"):
                return resp.room_id
        except Exception:
            log.warning("Failed to create DM room with %s", user_id)

        return None


def _split_message(text: str) -> list[str]:
    """Split a message into chunks for readability."""
    if len(text) <= MAX_MESSAGE_LENGTH:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= MAX_MESSAGE_LENGTH:
            chunks.append(text)
            break

        split_at = text.rfind("\n", 0, MAX_MESSAGE_LENGTH)
        if split_at == -1 or split_at < MAX_MESSAGE_LENGTH // 2:
            split_at = text.rfind(" ", 0, MAX_MESSAGE_LENGTH)
        if split_at == -1:
            split_at = MAX_MESSAGE_LENGTH

        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    return chunks


def _text_to_html(text: str) -> str:
    """Convert plain text to Matrix-safe HTML (basic formatting)."""
    import html as html_mod
    import re

    escaped = html_mod.escape(text)

    # Code blocks
    escaped = re.sub(
        r"```(\w*)\n(.*?)```",
        lambda m: f"<pre><code>{m.group(2)}</code></pre>",
        escaped,
        flags=re.DOTALL,
    )
    # Inline code
    escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
    # Bold
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", escaped)
    # Italic
    escaped = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"<em>\1</em>", escaped)
    # Line breaks
    escaped = escaped.replace("\n", "<br>")

    return escaped
