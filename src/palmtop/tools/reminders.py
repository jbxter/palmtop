from __future__ import annotations

import asyncio
import aiosqlite
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Awaitable
from zoneinfo import ZoneInfo

from palmtop.tools.base import Tool

log = logging.getLogger(__name__)


class ReminderTool(Tool):
    name = "remind"
    description = (
        "Set reminders that notify via Telegram. Usage:\n"
        "  [TOOL:remind] 2025-06-15 14:00 Call the venue about AV setup\n"
        "  [TOOL:remind] 30m Follow up with Sarah\n"
        "  [TOOL:remind] 2h Review the contract\n"
        "  [TOOL:remind] list\n"
        "  [TOOL:remind] remove <id>"
    )

    def __init__(self, db_path: Path, timezone: str = "America/Los_Angeles") -> None:
        self._db_path = db_path
        self._tz = ZoneInfo(timezone)
        self._db: aiosqlite.Connection | None = None
        self._notify_fn: Callable[[str, str], Awaitable[None]] | None = None
        self._check_task: asyncio.Task | None = None

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL DEFAULT 'default',
                remind_at TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_reminders_status
            ON reminders(status, remind_at)
        """)
        await self._db.commit()
        log.info("Reminders ready: %s", self._db_path)

    def set_notify(self, fn: Callable[[str, str], Awaitable[None]]) -> None:
        self._notify_fn = fn

    def start_background_check(self) -> None:
        if self._check_task is None:
            self._check_task = asyncio.create_task(self._check_loop())
            log.info("Reminder check loop started")

    async def _check_loop(self) -> None:
        while True:
            try:
                await self._fire_due()
            except Exception:
                log.exception("Reminder check error")
            await asyncio.sleep(30)

    async def _fire_due(self) -> None:
        now = datetime.now(self._tz).strftime("%Y-%m-%d %H:%M")
        cursor = await self._db.execute(
            """SELECT id, user_id, message FROM reminders
               WHERE status = 'pending' AND remind_at <= ?""",
            (now,),
        )
        rows = await cursor.fetchall()
        for rid, user_id, message in rows:
            log.info("Firing reminder #%d: %s", rid, message[:60])
            if self._notify_fn:
                try:
                    await self._notify_fn(user_id, f"⏰ Reminder: {message}")
                except Exception:
                    log.exception("Failed to send reminder #%d", rid)
                    continue
            await self._db.execute(
                "UPDATE reminders SET status = 'fired' WHERE id = ?", (rid,)
            )
            await self._db.commit()

    async def run(self, query: str) -> str:
        text = query.strip()

        if text.lower() == "list":
            return await self._list()
        if text.lower().startswith(("remove ", "delete ", "cancel ")):
            return await self._remove(text.split(None, 1)[1])

        return await self._add(text)

    async def _add(self, text: str) -> str:
        remind_at, message = _parse_time(text, self._tz)
        if not remind_at:
            return "Couldn't parse the time. Use: YYYY-MM-DD HH:MM message, or 30m/2h message"
        if not message:
            return "Need a reminder message after the time."

        cursor = await self._db.execute(
            "INSERT INTO reminders (remind_at, message) VALUES (?, ?)",
            (remind_at, message),
        )
        await self._db.commit()
        friendly = _friendly_datetime(remind_at)
        return f"✅ Reminder set: {message} — {friendly}"

    async def _list(self) -> str:
        cursor = await self._db.execute(
            """SELECT id, remind_at, message FROM reminders
               WHERE status = 'pending' ORDER BY remind_at""",
        )
        rows = await cursor.fetchall()
        if not rows:
            return "No pending reminders."
        lines = ["⏰ Pending reminders", "─" * 28]
        for rid, at, msg in rows:
            friendly = _friendly_datetime(at)
            lines.append(f"  {friendly:<18} {msg}")
        return "\n".join(lines)

    async def _remove(self, text: str) -> str:
        try:
            rid = int(text.strip().lstrip("#"))
        except ValueError:
            return "Need a reminder ID number."
        cursor = await self._db.execute("SELECT message FROM reminders WHERE id = ?", (rid,))
        row = await cursor.fetchone()
        if not row:
            return f"No reminder with ID #{rid}."
        await self._db.execute("DELETE FROM reminders WHERE id = ?", (rid,))
        await self._db.commit()
        return f"Removed reminder: {row[0]}"

    async def close(self) -> None:
        if self._check_task:
            self._check_task.cancel()
        if self._db:
            await self._db.close()


def _parse_time(text: str, tz: ZoneInfo | None = None) -> tuple[str | None, str]:
    text = text.strip()
    _now = datetime.now(tz) if tz else datetime.now()

    # Relative: 30m, 2h, 1d
    if text and text[0].isdigit():
        parts = text.split(None, 1)
        token = parts[0].lower()
        message = parts[1] if len(parts) > 1 else ""

        if token.endswith("m") and token[:-1].isdigit():
            dt = _now + timedelta(minutes=int(token[:-1]))
            return dt.strftime("%Y-%m-%d %H:%M"), message
        elif token.endswith("h") and token[:-1].isdigit():
            dt = _now + timedelta(hours=int(token[:-1]))
            return dt.strftime("%Y-%m-%d %H:%M"), message
        elif token.endswith("d") and token[:-1].isdigit():
            dt = _now + timedelta(days=int(token[:-1]))
            return dt.strftime("%Y-%m-%d %H:%M"), message

        # Absolute: 2025-06-15 14:00 message
        if len(token) == 10 and "-" in token:
            rest = parts[1] if len(parts) > 1 else ""
            rest_parts = rest.split(None, 1)
            if rest_parts and ":" in rest_parts[0] and len(rest_parts[0]) <= 5:
                time_str = rest_parts[0]
                message = rest_parts[1] if len(rest_parts) > 1 else ""
                return f"{token} {time_str}", message
            return f"{token} 09:00", rest

    return None, text


def _friendly_datetime(dt_str: str) -> str:
    """Convert '2025-06-15 14:00' to 'Sun, Jun 15 2:00 PM'."""
    try:
        dt = datetime.strptime(dt_str.strip(), "%Y-%m-%d %H:%M")
        try:
            return dt.strftime("%a, %b %d %-I:%M %p")
        except ValueError:
            # %-I not supported on all platforms
            return dt.strftime("%a, %b %d %I:%M %p").replace(" 0", " ", 1)
    except ValueError:
        return dt_str
