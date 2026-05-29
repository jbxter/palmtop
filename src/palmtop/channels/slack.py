"""Slack channel — DMs and mentions via Slack Bolt + Socket Mode.

Listens for direct messages and @mentions in Slack, routes them through
the agent loop, and replies in-thread with the agent's response.

Socket Mode means no public URL is needed — ideal for self-hosted setups.

Requires: pip install palmtop[slack]  (slack-bolt >= 1.18)
Config:   [slack] section in config.toml or env vars:
          SLACK_BOT_TOKEN, SLACK_APP_TOKEN
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from palmtop.channels.auth import log_access_policy, sender_allowed

if TYPE_CHECKING:
    from palmtop.core.loop import AgentLoop

log = logging.getLogger(__name__)

# Slack message limit (text field)
MAX_MESSAGE_LENGTH = 4000  # safe limit (actual is ~40k but blocks differ)


class SlackChannel:
    """Slack messaging channel via Socket Mode.

    Implements the Channel protocol (name, start, stop, send_message).
    Handles DMs and @mentions, replies in threads.
    """

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        allowed_users: list[str] | None = None,
        allow_anyone: bool = False,
    ) -> None:
        if not bot_token:
            raise ValueError("SLACK_BOT_TOKEN is required")
        if not app_token:
            raise ValueError("SLACK_APP_TOKEN is required (Socket Mode)")
        self._bot_token = bot_token
        self._app_token = app_token
        self._allowed_users = set(allowed_users) if allowed_users else None
        self._allow_anyone = allow_anyone
        log_access_policy(log, "slack", self._allowed_users, allow_anyone=allow_anyone)
        self._agent: AgentLoop | None = None
        self._stop_event = asyncio.Event()
        self._handler: AsyncSocketModeHandler | None = None

        # Create Bolt async app
        self._app = AsyncApp(token=bot_token)
        self._setup_handlers()

    @property
    def name(self) -> str:
        return "slack"

    def _setup_handlers(self) -> None:
        """Register Slack event handlers."""

        @self._app.event("message")
        async def handle_message(event, say, client):
            await self._on_message(event, say, client)

        @self._app.event("app_mention")
        async def handle_mention(event, say, client):
            await self._on_mention(event, say, client)

    async def start(self, loop: AgentLoop) -> None:
        """Start Slack Socket Mode. Blocks until stop() is called."""
        self._agent = loop
        log.info("Starting Slack channel (Socket Mode)...")

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        await self._handler.connect_async()
        log.info("Slack bot connected via Socket Mode")

        # Block until stop is requested
        await self._stop_event.wait()

    async def stop(self) -> None:
        """Disconnect from Slack."""
        log.info("Stopping Slack channel...")
        self._stop_event.set()
        if self._handler:
            await self._handler.close_async()

    async def send_message(self, user_id: str, text: str) -> None:
        """Send a DM to a user by their Slack user ID.

        Used for proactive notifications (reminders, alerts, digests).
        Opens a DM conversation if needed.
        """
        client = AsyncWebClient(token=self._bot_token)
        try:
            # Open DM channel
            resp = await client.conversations_open(users=[user_id])
            channel_id = resp["channel"]["id"]

            # Send message, splitting if needed
            for chunk in _split_message(text):
                await client.chat_postMessage(
                    channel=channel_id,
                    text=chunk,
                    mrkdwn=True,
                )
            log.info("Sent Slack DM to %s: %s", user_id, text[:60])
        except Exception:
            log.exception("Failed to send Slack DM to %s", user_id)

    # ── Internal ─────────────────────────────────────────────────────

    async def _on_message(self, event: dict, say, client) -> None:
        """Handle a direct message event."""
        # Skip bot messages and message_changed events
        if event.get("bot_id") or event.get("subtype"):
            return

        user_id = event.get("user", "")
        text = event.get("text", "").strip()
        channel_type = event.get("channel_type", "")

        # Only handle DMs (im = direct message)
        if channel_type != "im":
            return

        if not text or not user_id:
            return

        # Check allowlist (fail closed when unconfigured)
        if not sender_allowed(user_id, self._allowed_users, allow_anyone=self._allow_anyone):
            log.debug("Rejected Slack message from non-allowed user %s", user_id)
            return

        log.info("Slack DM from %s: %s", user_id, text[:80])
        await self._process_and_reply(text, user_id, event, say)

    async def _on_mention(self, event: dict, say, client) -> None:
        """Handle an @mention in a channel."""
        if event.get("bot_id") or event.get("subtype"):
            return

        user_id = event.get("user", "")
        text = event.get("text", "").strip()

        if not text or not user_id:
            return

        # Check allowlist (fail closed when unconfigured)
        if not sender_allowed(user_id, self._allowed_users, allow_anyone=self._allow_anyone):
            log.debug("Rejected Slack mention from non-allowed user %s", user_id)
            return

        # Strip the bot mention from the text
        # Mentions look like: <@U12345> what's on my calendar
        text = _strip_mention(text)
        if not text:
            return

        log.info("Slack mention from %s: %s", user_id, text[:80])
        await self._process_and_reply(text, user_id, event, say)

    async def _process_and_reply(self, text: str, user_id: str, event: dict, say) -> None:
        """Route message through agent and reply."""
        if not self._agent:
            return

        # Get thread context
        thread_ts = event.get("thread_ts") or event.get("ts")

        try:
            reply = await self._agent.handle(text, user_id=f"slack:{user_id}")
        except Exception:
            log.exception("Agent failed to handle Slack message from %s", user_id)
            try:
                await say(
                    text="Something went wrong processing that. Try again?",
                    thread_ts=thread_ts,
                )
            except Exception:
                pass
            return

        if not reply or not reply.strip():
            return

        # Reply in thread
        try:
            chunks = _split_message(reply)
            for chunk in chunks:
                await say(text=chunk, thread_ts=thread_ts)
        except Exception:
            log.exception("Failed to send Slack reply")

    @property
    def bot_token(self) -> str:
        """Expose token for wiring (e.g. admin health checks)."""
        return self._bot_token


def _strip_mention(text: str) -> str:
    """Strip the leading bot mention from text.

    Input:  "<@U12345> what's on my calendar?"
    Output: "what's on my calendar?"
    """
    import re

    return re.sub(r"^<@[A-Z0-9]+>\s*", "", text).strip()


def _split_message(text: str) -> list[str]:
    """Split a message into chunks that fit Slack's posting limit.

    Prefers splitting at newlines, then spaces, then hard split.
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
            split_at = text.rfind(" ", 0, MAX_MESSAGE_LENGTH)
        if split_at == -1:
            split_at = MAX_MESSAGE_LENGTH

        chunk = text[:split_at]
        text = text[split_at:].lstrip("\n")

        # Handle unclosed code blocks (triple backtick)
        if chunk.count("```") % 2 == 1:
            chunk += "\n```"
            text = "```\n" + text

        chunks.append(chunk)

    return chunks
