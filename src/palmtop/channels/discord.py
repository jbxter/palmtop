"""Discord channel — DM and server messaging via discord.py.

Listens for messages in Discord DMs or server channels, routes them
through the agent loop, and replies with the agent's response.

Requires: pip install palmtop[discord]  (discord.py >= 2.3)
Config:   [discord] section in config.toml or DISCORD_BOT_TOKEN env var.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import Intents, Message

from palmtop.channels.auth import log_access_policy, sender_allowed

if TYPE_CHECKING:
    from palmtop.core.loop import AgentLoop

log = logging.getLogger(__name__)

# Discord message limit
MAX_MESSAGE_LENGTH = 2000
# How often to refresh the typing indicator (lasts ~10s on Discord)
TYPING_TIMEOUT = 8.0


class DiscordChannel:
    """Discord messaging channel.

    Implements the Channel protocol (name, start, stop, send_message).
    Handles DMs and optionally server channel messages, with user allowlisting.
    """

    def __init__(
        self,
        bot_token: str,
        allowed_users: list[int] | None = None,
        allow_anyone: bool = False,
        guild_id: int | None = None,
        channel_id: int | None = None,
    ) -> None:
        if not bot_token:
            raise ValueError("DISCORD_BOT_TOKEN is required")
        self._bot_token = bot_token
        self._allowed_users = set(allowed_users) if allowed_users else None
        self._allow_anyone = allow_anyone
        log_access_policy(log, "discord", self._allowed_users, allow_anyone=allow_anyone)
        self._guild_id = guild_id
        self._channel_id = channel_id  # restrict to specific text channel
        self._agent: AgentLoop | None = None
        self._stop_event = asyncio.Event()

        # Set up intents
        intents = Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        intents.guild_messages = True

        self._client = discord.Client(intents=intents)
        self._setup_handlers()

    @property
    def name(self) -> str:
        return "discord"

    def _setup_handlers(self) -> None:
        """Register Discord event handlers."""

        @self._client.event
        async def on_ready():
            log.info(
                "Discord bot ready: %s (id=%s, guilds=%d)",
                self._client.user.name,
                self._client.user.id,
                len(self._client.guilds),
            )

        @self._client.event
        async def on_message(message: Message):
            await self._on_message(message)

    async def start(self, loop: AgentLoop) -> None:
        """Start the Discord bot. Blocks until stop() is called."""
        self._agent = loop
        log.info("Starting Discord channel...")

        # Run the bot in a task so we can wait for stop_event
        bot_task = asyncio.create_task(
            self._client.start(self._bot_token),
            name="discord-bot",
        )

        # Wait until stop is requested
        done, _ = await asyncio.wait(
            [bot_task, asyncio.create_task(self._stop_event.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # If stop_event fired, close the bot
        if not bot_task.done():
            await self._client.close()
            try:
                await bot_task
            except Exception:
                pass

        # If bot_task failed, log it
        if bot_task.done() and bot_task.exception():
            log.error("Discord bot crashed: %s", bot_task.exception())

    async def stop(self) -> None:
        """Stop the Discord bot gracefully."""
        log.info("Stopping Discord channel...")
        self._stop_event.set()
        if not self._client.is_closed():
            await self._client.close()

    async def send_message(self, user_id: str, text: str) -> None:
        """Send a DM to a user by their Discord user ID.

        Used for proactive notifications (reminders, alerts, digests).
        """
        try:
            uid = int(user_id)
            user = await self._client.fetch_user(uid)
            if user:
                for chunk in _split_message(text):
                    await user.send(chunk)
                log.info("Sent DM to %s: %s", user_id, text[:60])
        except discord.NotFound:
            log.warning("Discord user %s not found", user_id)
        except discord.Forbidden:
            log.warning("Cannot DM user %s (DMs disabled or bot blocked)", user_id)
        except Exception:
            log.exception("Failed to send Discord DM to %s", user_id)

    # ── Internal ─────────────────────────────────────────────────────

    async def _on_message(self, message: Message) -> None:
        """Handle an incoming Discord message."""
        # Ignore messages from the bot itself
        if message.author == self._client.user:
            return

        # Ignore bot messages
        if message.author.bot:
            return

        # Check user allowlist (fail closed when unconfigured)
        if not sender_allowed(message.author.id, self._allowed_users, allow_anyone=self._allow_anyone):
            log.debug("Rejected message from non-allowed user %s (id=%d)", message.author.name, message.author.id)
            return

        # Guild filter
        if self._guild_id and message.guild and message.guild.id != self._guild_id:
            return

        # Channel filter (for server messages)
        if self._channel_id and message.channel.id != self._channel_id:
            # Still allow DMs through
            if message.guild is not None:
                return

        text = message.content.strip()
        if not text:
            return

        user_id = str(message.author.id)
        log.info(
            "Discord message from %s (id=%s): %s",
            message.author.name,
            user_id,
            text[:80],
        )

        # Show typing indicator while processing
        try:
            async with message.channel.typing():
                reply = await self._agent.handle(text, user_id=f"discord:{user_id}")
        except Exception:
            log.exception("Agent failed to handle Discord message from %s", message.author.name)
            try:
                await message.reply("Something went wrong processing that. Try again?")
            except Exception:
                pass
            return

        if not reply or not reply.strip():
            return

        # Send reply, splitting if needed
        try:
            chunks = _split_message(reply)
            for i, chunk in enumerate(chunks):
                if i == 0:
                    await message.reply(chunk)
                else:
                    await message.channel.send(chunk)
        except discord.HTTPException as e:
            log.warning("Failed to send Discord reply: %s", e)
            # Fallback: try plain text without markdown
            try:
                plain = reply[:MAX_MESSAGE_LENGTH]
                await message.reply(plain)
            except Exception:
                log.exception("Discord reply fallback also failed")


def _split_message(text: str) -> list[str]:
    """Split a message into chunks that fit Discord's 2000 char limit.

    Prefers splitting at newlines, falls back to hard split.
    Preserves code blocks across splits where possible.
    """
    if len(text) <= MAX_MESSAGE_LENGTH:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= MAX_MESSAGE_LENGTH:
            chunks.append(text)
            break

        # Try to find a good split point
        split_at = text.rfind("\n", 0, MAX_MESSAGE_LENGTH)
        if split_at == -1 or split_at < MAX_MESSAGE_LENGTH // 2:
            # No good newline — try space
            split_at = text.rfind(" ", 0, MAX_MESSAGE_LENGTH)
        if split_at == -1:
            # Hard split
            split_at = MAX_MESSAGE_LENGTH

        chunk = text[:split_at]
        text = text[split_at:].lstrip("\n")

        # Handle unclosed code blocks
        if chunk.count("```") % 2 == 1:
            # Odd number of code fences = unclosed block
            chunk += "\n```"
            text = "```\n" + text

        chunks.append(chunk)

    return chunks
