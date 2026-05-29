from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import Forbidden, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from palmtop.channels.auth import log_access_policy, sender_allowed

if TYPE_CHECKING:
    from palmtop.core.loop import AgentLoop

log = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096
# Minimum interval between message edits (Telegram rate-limits edits)
EDIT_INTERVAL = 1.5
# How often to refresh the typing indicator (expires after ~5s)
TYPING_INTERVAL = 4.0


_PLACEHOLDER = "\x00TG{}\x00"
_ALLOWED_TAGS = frozenset({"b", "i", "u", "s", "pre", "code", "a"})
_CODE_FENCE = re.compile(r"```(?:\w*\n)?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BARE_URL = re.compile(r"(?<![\"'>])(https?://[^\s<]+[^\s<.,;:!?\])])")


def _stash_code_blocks(text: str, placeholders: dict[str, str]) -> str:
    def _fence(m: re.Match[str]) -> str:
        key = _PLACEHOLDER.format(len(placeholders))
        placeholders[key] = f"<pre>{html.escape(m.group(1))}</pre>"
        return key

    return _CODE_FENCE.sub(_fence, text)


def _stash_inline_code(text: str, placeholders: dict[str, str]) -> str:
    def _inline(m: re.Match[str]) -> str:
        key = _PLACEHOLDER.format(len(placeholders))
        placeholders[key] = f"<code>{html.escape(m.group(1))}</code>"
        return key

    return _INLINE_CODE.sub(_inline, text)


def _restore_placeholders(text: str, placeholders: dict[str, str]) -> str:
    for key, value in placeholders.items():
        text = text.replace(key, value)
    return text


def md_to_telegram_html(text: str) -> str:
    """Convert assistant Markdown to Telegram-safe HTML."""
    if not text:
        return ""

    placeholders: dict[str, str] = {}
    text = _stash_code_blocks(text, placeholders)
    text = _stash_inline_code(text, placeholders)

    def _stash_link(m: re.Match[str]) -> str:
        key = _PLACEHOLDER.format(len(placeholders))
        placeholders[key] = f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>'
        return key

    text = _MD_LINK.sub(_stash_link, text)
    text = html.escape(text, quote=False)
    text = _restore_placeholders(text, placeholders)

    # Autolink bare URLs in plain text (skip inside existing tags)
    def _autolink(m: re.Match[str]) -> str:
        url = m.group(1)
        return f'<a href="{html.escape(url, quote=True)}">{html.escape(url)}</a>'

    text = _BARE_URL.sub(_autolink, text)

    # Headers → bold with a line separator
    text = re.sub(
        r"^#{1,3}\s+(.+)$",
        r"\n<b>\1</b>\n─────────────────────",
        text,
        flags=re.MULTILINE,
    )

    # Bold / italic (non-greedy, no newlines)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"<i>\1</i>", text)

    # Bullet points with bold labels
    text = re.sub(
        r"^[\*\-]\s+<b>([^<]+)</b>:?\s*",
        r"\n▸ <b>\1</b>\n  ",
        text,
        flags=re.MULTILINE,
    )
    text = re.sub(r"^[\*\-]\s+", "▸ ", text, flags=re.MULTILINE)

    def _numbered(m: re.Match[str]) -> str:
        n = int(m.group(1))
        circled = "①②③④⑤⑥⑦⑧⑨⑩"
        return (circled[n - 1] if 1 <= n <= 10 else f"{n}.") + " "

    text = re.sub(r"^(\d+)\.\s+", _numbered, text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def sanitize_telegram_html(text: str) -> str:
    """Strip unsupported tags and close any unclosed Telegram HTML tags."""
    if not text:
        return ""

    # Remove unsupported tags, keep inner text
    def _strip_bad(m: re.Match[str]) -> str:
        tag = m.group(2).lower()
        if tag in _ALLOWED_TAGS:
            return m.group(0)
        return ""

    text = re.sub(r"<(/?)(\w+)(?:\s[^>]*)?>", _strip_bad, text)

    stack: list[str] = []
    out: list[str] = []
    last = 0
    for m in _PAIRED_TAGS.finditer(text):
        out.append(text[last : m.start()])
        is_close = m.group(1) == "/"
        tag = m.group(2).lower()
        if tag not in _ALLOWED_TAGS:
            last = m.end()
            continue
        if is_close:
            if stack and stack[-1] == tag:
                stack.pop()
                out.append(m.group(0))
            # else drop stray close tag
        else:
            stack.append(tag)
            out.append(m.group(0))
        last = m.end()
    out.append(text[last:])
    result = "".join(out)
    if stack:
        result += "".join(f"</{t}>" for t in reversed(stack))
    return result


def prepare_telegram_message(text: str) -> str:
    """Full pipeline: Markdown → HTML → sanitize for Telegram."""
    return sanitize_telegram_html(md_to_telegram_html(text))


class _TypingIndicator:
    """Context manager that sends ChatAction.TYPING every few seconds."""

    def __init__(self, chat_id: int, bot) -> None:
        self._chat_id = chat_id
        self._bot = bot
        self._task: asyncio.Task | None = None

    async def _loop(self) -> None:
        try:
            while True:
                await self._bot.send_chat_action(chat_id=self._chat_id, action=ChatAction.TYPING)
                await asyncio.sleep(TYPING_INTERVAL)
        except asyncio.CancelledError:
            pass

    async def __aenter__(self) -> _TypingIndicator:
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


class TelegramChannel:
    def __init__(
        self,
        bot_token: str,
        agent: AgentLoop,
        allowed_users: list[int] | None = None,
        allow_anyone: bool = False,
        stt=None,
        tts=None,
        data_dir=None,
        blessing_gate=None,
    ) -> None:
        if not bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        self._agent = agent
        self._allowed_users = set(allowed_users) if allowed_users else None
        self._allow_anyone = allow_anyone
        log_access_policy(log, "telegram", self._allowed_users, allow_anyone=allow_anyone)
        self._stt = stt
        self._tts = tts
        self._voice_tmp = (data_dir or Path("data")) / "voice_tmp"
        self._voice_mode_users: set[int] = set()  # users with /voice toggled on
        self._blessing_gate = blessing_gate
        # concurrent_updates lets /approve and /deny be processed while
        # /cursor or /engine is blocking on the blessing gate.  Without
        # this, the sequential update queue starves the approval handler.
        self._app = Application.builder().token(bot_token).concurrent_updates(True).build()
        self._app.add_handler(CommandHandler("engine", self._on_engine))
        self._app.add_handler(CommandHandler("claude", self._on_engine))
        self._app.add_handler(CommandHandler("cursor", self._on_cursor))
        self._app.add_handler(CommandHandler("voice", self._on_voice_toggle))
        self._app.add_handler(CommandHandler("approve", self._on_approve))
        self._app.add_handler(CommandHandler("deny", self._on_deny))
        self._app.add_handler(MessageHandler(filters.VOICE, self._on_voice))
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))

    async def _on_engine(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        uid = user.id if user else None
        if not sender_allowed(uid, self._allowed_users, allow_anyone=self._allow_anyone):
            return
        task = " ".join(context.args) if context.args else ""
        log.info("Engine command from %s: %s", uid, task[:80])

        async with _TypingIndicator(update.effective_chat.id, context.bot):
            reply = await self._agent.run_sovereign_engine(task, user_id=str(uid), source="telegram")
        await self._send_reply(update.message, reply)

    async def _on_cursor(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        uid = user.id if user else None
        if not sender_allowed(uid, self._allowed_users, allow_anyone=self._allow_anyone):
            return
        task = " ".join(context.args) if context.args else ""
        log.info("Cursor command from %s: %s", uid, task[:80])

        async with _TypingIndicator(update.effective_chat.id, context.bot):
            reply = await self._agent.run_cursor_delegate(task, user_id=str(uid), source="telegram")
        await self._send_reply(update.message, reply)

    async def _on_voice_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Toggle voice replies on/off for this user."""
        user = update.effective_user
        uid = user.id if user else None
        if not sender_allowed(uid, self._allowed_users, allow_anyone=self._allow_anyone):
            return

        if not self._tts:
            await update.message.reply_text("Voice replies aren't configured — TTS is disabled.")
            return

        if uid in self._voice_mode_users:
            self._voice_mode_users.discard(uid)
            await update.message.reply_text("🔇 Voice replies off.")
            log.info("Voice mode OFF for user %s", uid)
        else:
            self._voice_mode_users.add(uid)
            await update.message.reply_text("🔊 Voice replies on — I'll send audio with every response.")
            log.info("Voice mode ON for user %s", uid)

    async def _on_approve(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Approve a pending engine/cursor blessing request."""
        user = update.effective_user
        uid = user.id if user else None
        if not sender_allowed(uid, self._allowed_users, allow_anyone=self._allow_anyone):
            return
        log.info(
            "/approve from %s (gate pending: %s)",
            uid,
            self._blessing_gate.is_pending if self._blessing_gate else "no gate",
        )
        if self._blessing_gate and self._blessing_gate.is_pending:
            self._blessing_gate.approve()
            await update.message.reply_text("✅ Approved — continuing.")
        else:
            await update.message.reply_text("No pending approval request.")

    async def _on_deny(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Deny a pending engine/cursor blessing request."""
        user = update.effective_user
        uid = user.id if user else None
        if not sender_allowed(uid, self._allowed_users, allow_anyone=self._allow_anyone):
            return
        log.info(
            "/deny from %s (gate pending: %s)",
            uid,
            self._blessing_gate.is_pending if self._blessing_gate else "no gate",
        )
        if self._blessing_gate and self._blessing_gate.is_pending:
            self._blessing_gate.deny()
            await update.message.reply_text("❌ Denied — execution blocked.")
        else:
            await update.message.reply_text("No pending approval request.")

    async def _on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming voice messages — transcribe, respond with text + voice."""
        user = update.effective_user
        uid = user.id if user else None
        if not sender_allowed(uid, self._allowed_users, allow_anyone=self._allow_anyone):
            return

        if not self._stt:
            await update.message.reply_text("Voice messages aren't set up yet. Send a text message instead.")
            return

        ogg_path = None
        try:
            # Download the voice file
            self._voice_tmp.mkdir(parents=True, exist_ok=True)
            voice = update.message.voice
            tg_file = await voice.get_file()
            ogg_path = self._voice_tmp / f"{update.message.message_id}.ogg"
            await tg_file.download_to_drive(str(ogg_path))
            log.info("Voice from %s: %.1fs, %s", uid, voice.duration, ogg_path.name)

            # Transcribe with typing indicator
            async with _TypingIndicator(update.effective_chat.id, context.bot):
                transcript = await self._stt.transcribe(ogg_path)

            if not transcript:
                await update.message.reply_text(
                    "I couldn't make out that voice message — could you try again or type it out?"
                )
                return

            # Echo what we heard so the user can verify
            preview = transcript[:150] + ("..." if len(transcript) > 150 else "")
            await update.message.reply_text(f"🎤 Heard: {preview}")

            # Process through the normal message path and capture the reply
            reply_text = ""
            if hasattr(self._agent, "handle_stream"):
                # Collect the final reply from the streaming path
                async with _TypingIndicator(update.effective_chat.id, context.bot):
                    async for event, data in self._agent.handle_stream(transcript, user_id=str(uid), source="telegram"):
                        if event == "done":
                            reply_text = data
                        elif event == "error":
                            reply_text = data
                # Send text reply
                if reply_text:
                    await self._send_reply(update.message, reply_text)
            else:
                async with _TypingIndicator(update.effective_chat.id, context.bot):
                    reply_text = await self._agent.handle(transcript, user_id=str(uid), source="telegram")
                await self._send_reply(update.message, reply_text)

            # Synthesize and send voice reply
            if reply_text and self._tts:
                await self._send_voice_reply(update, context, reply_text)

        except Exception:
            log.exception("Voice handling failed")
            await update.message.reply_text(
                "Something went wrong processing that voice message. Try sending it as text."
            )
        finally:
            if ogg_path and ogg_path.exists():
                ogg_path.unlink(missing_ok=True)

    async def _send_voice_reply(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> None:
        """Synthesize text to speech and send as a Telegram voice message."""
        audio_path = None
        try:
            audio_path = await self._tts.synthesize(text)
            if not audio_path:
                return

            with open(audio_path, "rb") as f:
                if audio_path.suffix == ".ogg":
                    # OGG Opus → inline voice bubble
                    await context.bot.send_voice(
                        chat_id=update.effective_chat.id,
                        voice=f,
                        reply_to_message_id=update.message.message_id,
                        read_timeout=30,
                        write_timeout=30,
                        connect_timeout=15,
                    )
                else:
                    # WAV/MP3 fallback → audio file (playable, not voice bubble)
                    await context.bot.send_audio(
                        chat_id=update.effective_chat.id,
                        audio=f,
                        title="the agent",
                        reply_to_message_id=update.message.message_id,
                        read_timeout=30,
                        write_timeout=30,
                        connect_timeout=15,
                    )
            log.info("Sent voice reply: %s", audio_path.name)

        except Exception:
            log.warning("Voice reply failed — text reply was already sent", exc_info=True)
        finally:
            if audio_path and audio_path.exists():
                audio_path.unlink(missing_ok=True)

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        uid = user.id if user else None
        text = update.message.text
        log.info("Message from %s (id=%s): %s", user.first_name if user else "?", uid, text[:80])

        if not sender_allowed(uid, self._allowed_users, allow_anyone=self._allow_anyone):
            log.warning(
                "Rejected message from unauthorized user %s (id=%s)",
                user.first_name if user else "?",
                uid,
            )
            return

        # Try streaming path first, fall back to non-streaming
        reply_text = ""
        if hasattr(self._agent, "handle_stream"):
            reply_text = await self._on_message_stream(update, context, text, str(uid))
        else:
            async with _TypingIndicator(update.effective_chat.id, context.bot):
                reply_text = await self._agent.handle(text, user_id=str(uid), source="telegram")
            await self._send_reply(update.message, reply_text)

        # Voice reply if the user toggled /voice on
        if reply_text and uid in self._voice_mode_users and self._tts:
            await self._send_voice_reply(update, context, reply_text)

    async def _on_message_stream(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
        user_id: str,
    ) -> str:
        """Handle a message with streaming — edit placeholder as chunks arrive.

        Returns the final reply text (for voice synthesis if /voice is on).
        """
        chat_id = update.effective_chat.id
        bot = context.bot
        sent_message = None
        last_edit = 0.0
        last_text = ""
        final_reply = ""

        async with _TypingIndicator(chat_id, bot):
            async for event, data in self._agent.handle_stream(text, user_id=user_id, source="telegram"):
                if event == "status":
                    # Status updates — if we haven't sent a message yet, that's fine
                    # The typing indicator handles the "thinking" UX
                    log.debug("Stream status: %s", data)

                elif event == "chunk":
                    now = time.monotonic()
                    # Only edit if enough time has passed and text actually changed
                    if now - last_edit < EDIT_INTERVAL:
                        continue
                    # Don't edit if text is too short (avoids flickering)
                    if len(data) < 20:
                        continue
                    display = _truncate_for_edit(data)
                    if display == last_text:
                        continue

                    try:
                        if sent_message is None:
                            sent_message = await update.message.reply_text(
                                display + " ▍",
                                parse_mode=None,  # plain text during streaming
                            )
                        else:
                            await sent_message.edit_text(
                                display + " ▍",
                                parse_mode=None,
                            )
                        last_text = display
                        last_edit = now
                    except Forbidden:
                        log.warning("Bot blocked by user — aborting stream")
                        return ""
                    except TimedOut:
                        log.debug("Telegram timed out on edit, skipping")
                    except Exception:
                        log.debug("Edit failed (rate limit or unchanged), skipping")

                elif event == "done":
                    final_reply = data

                elif event == "error":
                    final_reply = data

        # Final formatted message — replace the streaming placeholder
        if final_reply:
            await self._deliver_formatted(
                final_reply,
                edit_message=sent_message,
                reply_to=update.message,
            )

        return final_reply

    async def _deliver_formatted(
        self,
        text: str,
        *,
        edit_message=None,
        reply_to=None,
    ) -> None:
        """Send formatted HTML with plain-text fallback (monitor/digest/stream)."""
        formatted = prepare_telegram_message(text)
        chunks = _split_message(formatted)
        try:
            if edit_message is not None:
                await edit_message.edit_text(chunks[0], parse_mode=ParseMode.HTML)
                target = reply_to
                for chunk in chunks[1:]:
                    if target:
                        await target.reply_text(chunk, parse_mode=ParseMode.HTML)
            elif reply_to is not None:
                for chunk in chunks:
                    await reply_to.reply_text(chunk, parse_mode=ParseMode.HTML)
        except Exception:
            log.warning("HTML send failed, falling back to plain text", exc_info=True)
            plain = text[:MAX_MESSAGE_LENGTH]
            if edit_message is not None:
                try:
                    await edit_message.edit_text(plain)
                    return
                except Exception:
                    pass
            if reply_to is not None:
                await reply_to.reply_text(plain)

    async def _send_reply(self, message, text: str) -> None:
        await self._deliver_formatted(text, reply_to=message)

    async def send_message(self, user_id: str, text: str) -> None:
        bot = self._app.bot
        formatted = prepare_telegram_message(text)
        try:
            for chunk in _split_message(formatted):
                await bot.send_message(
                    chat_id=int(user_id),
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                )
            log.info("Sent message to %s: %s", user_id, text[:60])
        except Exception:
            log.warning("HTML proactive send failed, using plain text for %s", user_id)
            try:
                await bot.send_message(
                    chat_id=int(user_id),
                    text=text[:MAX_MESSAGE_LENGTH],
                )
            except Exception:
                log.exception("Failed to send message to %s", user_id)

    @property
    def name(self) -> str:
        """Channel identifier for the multi-channel runner."""
        return "telegram"

    async def start(self, loop: AgentLoop) -> None:
        """Start Telegram polling as part of a multi-channel runner.

        This uses the lower-level async API so the event loop is shared
        with other channels. Blocks until stop() is called.
        """
        log.info("Starting Telegram polling (multi-channel mode)...")
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        self._stop_event = asyncio.Event()
        await self._stop_event.wait()

    async def stop(self) -> None:
        """Stop Telegram polling and shut down cleanly."""
        log.info("Stopping Telegram channel...")
        if hasattr(self, "_stop_event"):
            self._stop_event.set()
        try:
            if self._app.updater and self._app.updater.running:
                await self._app.updater.stop()
            if self._app.running:
                await self._app.stop()
            await self._app.shutdown()
        except Exception:
            log.debug("Telegram shutdown error (non-fatal)", exc_info=True)

    def run(
        self,
        on_start: Callable[[], None] | None = None,
        async_init: Callable[[], asyncio.coroutines] | None = None,
    ) -> None:
        """Run in single-channel mode (owns the event loop). Backward compat."""
        log.info("Starting Telegram polling...")
        if on_start or async_init:

            async def _post_init(app):
                if async_init:
                    await async_init()
                if on_start:
                    on_start()

            self._app.post_init = _post_init
        self._app.run_polling(drop_pending_updates=True)


def _truncate_for_edit(text: str) -> str:
    """Truncate streaming text to fit Telegram's message limit."""
    if len(text) <= MAX_MESSAGE_LENGTH - 10:  # room for cursor
        return text
    return text[: MAX_MESSAGE_LENGTH - 10]


def _split_message(text: str) -> list[str]:
    if len(text) <= MAX_MESSAGE_LENGTH:
        return [text]
    chunks = []
    while text:
        if len(text) <= MAX_MESSAGE_LENGTH:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, MAX_MESSAGE_LENGTH)
        if split_at == -1:
            split_at = MAX_MESSAGE_LENGTH
        chunk = text[:split_at]
        remainder = text[split_at:].lstrip("\n")

        # Repair unclosed HTML tags at the split boundary
        chunk, reopen = _close_open_tags(chunk)
        if reopen:
            remainder = reopen + remainder

        chunks.append(chunk)
        text = remainder
    return chunks


# Tags that Telegram supports and that we might split across
_PAIRED_TAGS = re.compile(r"<(/?)(\w+)(?:\s[^>]*)?>")


def _close_open_tags(chunk: str) -> tuple[str, str]:
    """Close any HTML tags left open at the end of *chunk*.

    Returns (patched_chunk, reopen_prefix) where *reopen_prefix* contains
    the opening tags that should be prepended to the next chunk so nesting
    is preserved across the split.
    """
    tag_stack: list[str] = []
    for m in _PAIRED_TAGS.finditer(chunk):
        is_close = m.group(1) == "/"
        tag = m.group(2).lower()
        if tag not in ("b", "i", "u", "s", "pre", "code"):
            continue
        if is_close:
            if tag_stack and tag_stack[-1] == tag:
                tag_stack.pop()
        else:
            tag_stack.append(tag)

    if not tag_stack:
        return chunk, ""

    # Close tags in reverse order at end of chunk
    closing = "".join(f"</{t}>" for t in reversed(tag_stack))
    # Reopen in original order at start of next chunk
    reopening = "".join(f"<{t}>" for t in tag_stack)
    return chunk + closing, reopening
