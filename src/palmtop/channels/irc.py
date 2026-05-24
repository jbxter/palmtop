"""IRC channel — classic Internet Relay Chat.

Connects to an IRC server, joins specified channels, and routes private
messages and mentions through the agent loop.

Uses raw asyncio sockets with the IRC protocol — no external dependencies.

Config: [irc] section in config.toml or env vars.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from palmtop.core.loop import AgentLoop

log = logging.getLogger(__name__)

# IRC message limit (RFC 2812: 512 bytes including CRLF)
# Safe limit for message body after accounting for protocol overhead
MAX_IRC_LINE = 450


class IrcChannel:
    """IRC messaging channel using raw asyncio sockets.

    Implements the Channel protocol (name, start, stop, send_message).
    Zero external dependencies — implements IRC protocol directly.
    """

    def __init__(
        self,
        server: str,
        port: int = 6667,
        nick: str = "palmtop",
        channels: list[str] | None = None,
        password: str = "",
        use_ssl: bool = False,
        allowed_users: list[str] | None = None,
    ) -> None:
        if not server:
            raise ValueError("IRC server is required")
        self._server = server
        self._port = port
        self._nick = nick
        self._channels = channels or []
        self._password = password
        self._use_ssl = use_ssl
        self._allowed_users = {u.lower() for u in allowed_users} if allowed_users else None
        self._agent: AgentLoop | None = None
        self._stop_event = asyncio.Event()
        self._writer: asyncio.StreamWriter | None = None
        self._reader: asyncio.StreamReader | None = None
        self._connected = False

    @property
    def name(self) -> str:
        return "irc"

    async def start(self, loop: AgentLoop) -> None:
        """Connect to IRC and start listening. Blocks until stop()."""
        self._agent = loop
        log.info("Connecting to IRC: %s:%d as %s", self._server, self._port, self._nick)

        try:
            if self._use_ssl:
                import ssl

                ssl_ctx = ssl.create_default_context()
                self._reader, self._writer = await asyncio.open_connection(self._server, self._port, ssl=ssl_ctx)
            else:
                self._reader, self._writer = await asyncio.open_connection(self._server, self._port)
        except OSError as e:
            log.error("Failed to connect to IRC server: %s", e)
            return

        # Registration
        if self._password:
            await self._send_raw(f"PASS {self._password}")
        await self._send_raw(f"NICK {self._nick}")
        await self._send_raw(f"USER {self._nick} 0 * :Palmtop Agent")

        self._connected = True
        log.info("IRC connection established")

        # Read loop
        try:
            while not self._stop_event.is_set():
                try:
                    line = await asyncio.wait_for(
                        self._reader.readline(),
                        timeout=300.0,  # 5 min timeout for keepalive
                    )
                except TimeoutError:
                    # Send PING to keep connection alive
                    await self._send_raw(f"PING :{self._server}")
                    continue

                if not line:
                    log.warning("IRC connection closed by server")
                    break

                await self._handle_line(line.decode("utf-8", errors="replace").strip())
        except asyncio.CancelledError:
            pass
        except Exception:
            if not self._stop_event.is_set():
                log.exception("IRC read loop error")
        finally:
            self._connected = False
            if self._writer:
                self._writer.close()

    async def stop(self) -> None:
        """Disconnect from IRC."""
        log.info("Stopping IRC channel...")
        self._stop_event.set()
        if self._writer and self._connected:
            try:
                await self._send_raw("QUIT :Shutting down")
                self._writer.close()
            except Exception:
                pass

    async def send_message(self, user_id: str, text: str) -> None:
        """Send a private message to a user or channel."""
        if not self._connected or not self._writer:
            log.warning("IRC not connected — cannot send to %s", user_id)
            return

        for chunk in _split_message(text):
            await self._send_raw(f"PRIVMSG {user_id} :{chunk}")

    # ── Internal ─────────────────────────────────────────────────────

    async def _send_raw(self, line: str) -> None:
        """Send a raw IRC protocol line."""
        if self._writer:
            self._writer.write(f"{line}\r\n".encode())
            await self._writer.drain()

    async def _handle_line(self, line: str) -> None:
        """Parse and handle a single IRC protocol line."""
        if not line:
            return

        # Handle PING (keepalive)
        if line.startswith("PING"):
            pong_arg = line[5:] if len(line) > 5 else ""
            await self._send_raw(f"PONG {pong_arg}")
            return

        # Parse IRC message: :prefix COMMAND params :trailing
        match = re.match(r"^(?::(\S+)\s)?(\S+)\s?(.*)", line)
        if not match:
            return

        prefix = match.group(1) or ""
        command = match.group(2)
        params = match.group(3) or ""

        if command == "001":
            # RPL_WELCOME — we're registered, join channels
            log.info("IRC registered as %s", self._nick)
            for ch in self._channels:
                await self._send_raw(f"JOIN {ch}")
                log.info("Joined IRC channel: %s", ch)

        elif command == "PRIVMSG":
            await self._on_privmsg(prefix, params)

        elif command == "433":
            # Nick already in use
            self._nick = self._nick + "_"
            await self._send_raw(f"NICK {self._nick}")
            log.warning("Nick taken, trying: %s", self._nick)

    async def _on_privmsg(self, prefix: str, params: str) -> None:
        """Handle PRIVMSG — either a DM or a channel message."""
        # Extract sender nick from prefix (nick!user@host)
        sender_nick = prefix.split("!")[0] if "!" in prefix else prefix

        # Parse target and message
        parts = params.split(" :", 1)
        if len(parts) < 2:
            return
        target = parts[0].strip()
        message = parts[1].strip()

        if not message:
            return

        # Check allowlist
        if self._allowed_users and sender_nick.lower() not in self._allowed_users:
            return

        # Determine if this is a DM or channel mention
        is_dm = target.lower() == self._nick.lower()
        is_mention = not is_dm and (
            message.lower().startswith(f"{self._nick.lower()}:") or message.lower().startswith(f"{self._nick.lower()},")
        )

        if not is_dm and not is_mention:
            return  # Channel message not addressed to us

        # Strip bot nick prefix from mentions
        if is_mention:
            message = re.sub(rf"^{re.escape(self._nick)}[,:]\s*", "", message, flags=re.IGNORECASE).strip()

        if not message:
            return

        log.info("IRC %s from %s: %s", "DM" if is_dm else "mention", sender_nick, message[:80])

        if not self._agent:
            return

        try:
            reply = await self._agent.handle(message, user_id=f"irc:{sender_nick}")
        except Exception:
            log.exception("Agent failed to handle IRC message from %s", sender_nick)
            return

        if not reply or not reply.strip():
            return

        # Reply to DMs directly, channel messages in the channel
        reply_target = sender_nick if is_dm else target
        for chunk in _split_message(reply):
            if not is_dm:
                chunk = f"{sender_nick}: {chunk}"
            await self._send_raw(f"PRIVMSG {reply_target} :{chunk}")


def _split_message(text: str) -> list[str]:
    """Split a message into IRC-safe chunks (max ~450 chars per line).

    IRC has a 512-byte line limit including protocol overhead.
    Split at newlines first, then by length.
    """
    lines: list[str] = []

    # First split on newlines (IRC doesn't support multiline)
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if len(line) <= MAX_IRC_LINE:
            lines.append(line)
        else:
            # Split long lines at spaces
            while line:
                if len(line) <= MAX_IRC_LINE:
                    lines.append(line)
                    break
                split_at = line.rfind(" ", 0, MAX_IRC_LINE)
                if split_at == -1:
                    split_at = MAX_IRC_LINE
                lines.append(line[:split_at])
                line = line[split_at:].lstrip()

    return lines if lines else [""]
