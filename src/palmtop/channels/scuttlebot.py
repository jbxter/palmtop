"""ScuttleBot integration — multi-agent coordination via IRC backplane.

Connects Palmtop to a ScuttleBot server where multiple AI agents coordinate
in shared IRC channels. Extends the IRC channel with ScuttleBot-specific
protocol features (tool visibility, interruption, task routing).

Zero extra dependencies — uses the same raw asyncio IRC implementation.

Config: [scuttlebot] section in config.toml.

Reference: https://github.com/ConflictHQ/scuttlebot
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from palmtop.core.loop import AgentLoop

from palmtop.channels.irc import IrcChannel, _split_message

log = logging.getLogger(__name__)


class ScuttleBotChannel:
    """ScuttleBot multi-agent coordination channel.

    Implements the Channel protocol (name, start, stop, send_message).
    Connects to a ScuttleBot IRC server and participates in agent fleet
    coordination — streaming tool calls, responding to mentions, and
    supporting interruption protocol.
    """

    def __init__(
        self,
        server: str,
        port: int = 6667,
        nick: str = "palmtop",
        channels: list[str] | None = None,
        password: str = "",
        use_ssl: bool = False,
        broadcast_tools: bool = True,
    ) -> None:
        if not server:
            raise ValueError("ScuttleBot server is required")

        self._server = server
        self._port = port
        self._nick = nick
        self._channels = channels or ["#ops"]
        self._password = password
        self._use_ssl = use_ssl
        self._broadcast_tools = broadcast_tools
        self._agent: AgentLoop | None = None
        self._irc: IrcChannel | None = None
        self._stop_event = asyncio.Event()
        self._paused = False

    @property
    def name(self) -> str:
        return "scuttlebot"

    async def start(self, loop: AgentLoop) -> None:
        """Connect to ScuttleBot server and join coordination channels."""
        self._agent = loop

        # Create an IRC channel instance — ScuttleBot speaks IRC protocol
        self._irc = IrcChannel(
            server=self._server,
            port=self._port,
            nick=self._nick,
            channels=self._channels,
            password=self._password,
            use_ssl=self._use_ssl,
            allowed_users=None,  # All agents in ScuttleBot are trusted
        )

        # Monkey-patch the message handler to add ScuttleBot protocol
        original_on_privmsg = self._irc._on_privmsg
        self._irc._on_privmsg = self._on_privmsg
        self._original_on_privmsg = original_on_privmsg

        log.info(
            "ScuttleBot connecting to %s:%d as %s (channels: %s)",
            self._server,
            self._port,
            self._nick,
            ", ".join(self._channels),
        )

        # Announce presence after connection
        original_handle_line = self._irc._handle_line

        async def _patched_handle_line(line: str) -> None:
            await original_handle_line(line)
            # After join (001 welcome), announce ourselves
            if line and "001" in line:
                await self._announce_presence()

        self._irc._handle_line = _patched_handle_line

        await self._irc.start(loop)

    async def stop(self) -> None:
        """Disconnect from ScuttleBot."""
        log.info("Stopping ScuttleBot channel...")
        self._stop_event.set()
        if self._irc:
            # Announce departure
            for ch in self._channels:
                await self._irc._send_raw(f"PRIVMSG {ch} :[STATUS] {self._nick} going offline")
            await self._irc.stop()

    async def send_message(self, user_id: str, text: str) -> None:
        """Send a message to a ScuttleBot channel or agent."""
        if self._irc:
            await self._irc.send_message(user_id, text)

    # ── ScuttleBot Protocol ──────────────────────────────────────────

    async def broadcast_tool_call(self, tool_name: str, args: dict) -> None:
        """Broadcast a tool call to the coordination channel for visibility."""
        if not self._broadcast_tools or not self._irc or not self._irc._connected:
            return
        # Compact JSON for IRC line limits
        args_str = json.dumps(args, separators=(",", ":"))
        if len(args_str) > 300:
            args_str = args_str[:297] + "..."
        msg = f"[TOOL] {tool_name}({args_str})"
        for ch in self._channels:
            await self._irc._send_raw(f"PRIVMSG {ch} :{msg}")

    async def broadcast_tool_result(self, tool_name: str, result: str) -> None:
        """Broadcast a tool result summary."""
        if not self._broadcast_tools or not self._irc or not self._irc._connected:
            return
        summary = result[:200] + "..." if len(result) > 200 else result
        # Single-line for IRC
        summary = summary.replace("\n", " ")
        msg = f"[RESULT] {tool_name} → {summary}"
        for ch in self._channels:
            await self._irc._send_raw(f"PRIVMSG {ch} :{msg}")

    async def _announce_presence(self) -> None:
        """Announce this agent's presence and capabilities."""
        if not self._irc or not self._irc._connected:
            return
        capabilities = "general-purpose AI agent with tool access"
        for ch in self._channels:
            await self._irc._send_raw(f"PRIVMSG {ch} :[STATUS] {self._nick} online — {capabilities}")

    async def _on_privmsg(self, prefix: str, params: str) -> None:
        """Handle ScuttleBot protocol messages + standard IRC PRIVMSG."""
        sender_nick = prefix.split("!")[0] if "!" in prefix else prefix
        parts = params.split(" :", 1)
        if len(parts) < 2:
            return
        target = parts[0].strip()
        message = parts[1].strip()

        if not message:
            return

        # ── ScuttleBot protocol commands ──

        # Interruption: another agent says "!pause @palmtop" or "!resume @palmtop"
        if message.startswith("!pause") and (f"@{self._nick}" in message or f"{self._nick}" in message.split()):
            self._paused = True
            log.info("ScuttleBot: PAUSED by %s", sender_nick)
            reply = f"[STATUS] {self._nick} paused (requested by {sender_nick})"
            if self._irc:
                await self._irc._send_raw(f"PRIVMSG {target} :{reply}")
            return

        if message.startswith("!resume") and (f"@{self._nick}" in message or f"{self._nick}" in message.split()):
            self._paused = False
            log.info("ScuttleBot: RESUMED by %s", sender_nick)
            reply = f"[STATUS] {self._nick} resumed (requested by {sender_nick})"
            if self._irc:
                await self._irc._send_raw(f"PRIVMSG {target} :{reply}")
            return

        # Status query: "!status @palmtop"
        if message.startswith("!status") and (f"@{self._nick}" in message or f"{self._nick}" in message.split()):
            status = "paused" if self._paused else "active"
            reply = f"[STATUS] {self._nick} is {status}"
            if self._irc:
                await self._irc._send_raw(f"PRIVMSG {target} :{reply}")
            return

        # Ignore other agents' tool broadcasts (don't respond to [TOOL] or [RESULT])
        if message.startswith("[TOOL]") or message.startswith("[RESULT]") or message.startswith("[STATUS]"):
            return

        # If paused, don't process messages
        if self._paused:
            return

        # ── Standard message handling (DM or mention) ──

        is_dm = target.lower() == self._nick.lower()
        is_mention = not is_dm and (
            message.lower().startswith(f"{self._nick.lower()}:")
            or message.lower().startswith(f"{self._nick.lower()},")
            or f"@{self._nick.lower()}" in message.lower()
        )

        if not is_dm and not is_mention:
            return

        # Strip mention prefix
        if is_mention:
            # Remove @nick or nick: prefix
            message = re.sub(
                rf"^@?{re.escape(self._nick)}[,:]\s*",
                "",
                message,
                flags=re.IGNORECASE,
            ).strip()
            # Also handle mid-message @mentions
            message = re.sub(
                rf"@{re.escape(self._nick)}\s*",
                "",
                message,
                flags=re.IGNORECASE,
            ).strip()

        if not message:
            return

        log.info("ScuttleBot %s from %s: %s", "DM" if is_dm else "mention", sender_nick, message[:80])

        if not self._agent:
            return

        try:
            reply = await self._agent.handle(message, user_id=f"scuttlebot:{sender_nick}")
        except Exception:
            log.exception("Agent failed to handle ScuttleBot message from %s", sender_nick)
            return

        if not reply or not reply.strip():
            return

        reply_target = sender_nick if is_dm else target
        for chunk in _split_message(reply):
            if not is_dm:
                chunk = f"{sender_nick}: {chunk}"
            if self._irc:
                await self._irc._send_raw(f"PRIVMSG {reply_target} :{chunk}")
