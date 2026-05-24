from __future__ import annotations

import aiosqlite
import logging
from pathlib import Path

from pocket_agent.inference.base import Message

log = logging.getLogger(__name__)

MAX_HISTORY = 20  # recent messages to keep in the context window
SUMMARY_THRESHOLD = 30  # summarize when this many messages accumulate since last summary
MAX_RAW_KEEP = 200  # hard cap — delete raw messages beyond this per user

# Prompt for the summarizer — turns a block of conversation into a compact
# paragraph the LLM can use as context without eating the whole window.
SUMMARIZE_PROMPT = """\
Summarize this conversation between a user and the agent (an AI assistant). \
Focus on:
- Decisions that were made
- Facts the user shared (preferences, goals, context)
- Work that was completed or is in progress
- Any open questions or pending items

Be concise — aim for 3-5 sentences. Write in past tense as a narrator, \
not as a participant. Do NOT include tool errors, transient system status, \
or troubleshooting steps.

Conversation:
{conversation}

Summary:"""


class ConversationMemory:
    """Conversation memory with rolling summaries.

    Architecture:
    - `messages` table: raw message log, hard-capped at MAX_RAW_KEEP per user
    - `summaries` table: compressed digests of older conversation blocks
    - `get_history()` returns: [oldest summary] + [recent summaries] + [recent raw messages]
      This gives the LLM a sense of continuity without burning context on old turns.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._msg_count_since_summary: dict[str, int] = {}

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id)
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                summary TEXT NOT NULL,
                msg_id_start INTEGER NOT NULL,
                msg_id_end INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_summaries_user ON summaries(user_id, id)
        """)
        await self._db.commit()
        log.info("Conversation memory ready: %s", self._db_path)

    async def append(self, user_id: str, role: str, content: str) -> None:
        await self._db.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content),
        )
        await self._db.commit()

        # Track messages since last summary
        count = self._msg_count_since_summary.get(user_id, 0) + 1
        self._msg_count_since_summary[user_id] = count

        # Hard-prune beyond MAX_RAW_KEEP
        await self._prune(user_id)

    def needs_summary(self, user_id: str) -> bool:
        """Check if enough messages have accumulated to warrant summarization."""
        return self._msg_count_since_summary.get(user_id, 0) >= SUMMARY_THRESHOLD

    async def get_unsummarized_messages(self, user_id: str) -> tuple[list[Message], int, int]:
        """Get messages that haven't been summarized yet.

        Returns (messages, first_msg_id, last_msg_id).
        """
        # Find the highest message ID covered by a summary
        cursor = await self._db.execute(
            "SELECT MAX(msg_id_end) FROM summaries WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        last_summarized = row[0] if row and row[0] else 0

        # Get messages after that point, but leave the most recent ones unsummarized
        # (they'll be in the raw window)
        cursor = await self._db.execute(
            """SELECT id, role, content FROM messages
               WHERE user_id = ? AND id > ?
               ORDER BY id ASC""",
            (user_id, last_summarized),
        )
        rows = await cursor.fetchall()

        if len(rows) <= MAX_HISTORY:
            # Not enough messages beyond the raw window to summarize
            return [], 0, 0

        # Summarize everything except the last MAX_HISTORY messages
        to_summarize = rows[:-MAX_HISTORY]
        messages = [Message(role=r[1], content=r[2]) for r in to_summarize]
        first_id = to_summarize[0][0]
        last_id = to_summarize[-1][0]
        return messages, first_id, last_id

    async def store_summary(self, user_id: str, summary: str, msg_id_start: int, msg_id_end: int) -> None:
        """Store a conversation summary covering a range of message IDs."""
        await self._db.execute(
            """INSERT INTO summaries (user_id, summary, msg_id_start, msg_id_end)
               VALUES (?, ?, ?, ?)""",
            (user_id, summary, msg_id_start, msg_id_end),
        )
        await self._db.commit()
        self._msg_count_since_summary[user_id] = 0
        log.info(
            "Stored conversation summary for %s (msgs %d–%d, %d chars)",
            user_id, msg_id_start, msg_id_end, len(summary),
        )

    async def get_history(self, user_id: str, limit: int = MAX_HISTORY) -> list[Message]:
        """Build a context-efficient history: summaries + recent messages.

        Returns a list of Messages structured as:
        1. A system-like message with concatenated summaries (if any)
        2. The most recent `limit` raw messages

        This gives the LLM continuity beyond the raw window.
        """
        messages: list[Message] = []

        # Load summaries (most recent 5 — covers ~150 messages of history)
        cursor = await self._db.execute(
            """SELECT summary FROM (
                   SELECT summary, id FROM summaries
                   WHERE user_id = ? ORDER BY id DESC LIMIT 5
               ) ORDER BY id ASC""",
            (user_id,),
        )
        summary_rows = await cursor.fetchall()
        if summary_rows:
            combined = "\n\n".join(r[0] for r in summary_rows)
            messages.append(Message(
                role="user",
                content=f"[Previous conversation context]\n{combined}\n[End of context — recent messages follow]",
            ))
            messages.append(Message(
                role="assistant",
                content="Understood — I have context from our earlier conversation. Continuing.",
            ))

        # Load recent raw messages
        cursor = await self._db.execute(
            """SELECT role, content FROM (
                   SELECT role, content, id FROM messages
                   WHERE user_id = ? ORDER BY id DESC LIMIT ?
               ) ORDER BY id ASC""",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        messages.extend(Message(role=r, content=c) for r, c in rows)

        return messages

    async def search_history(self, user_id: str, query: str, limit: int = 10) -> list[Message]:
        """Search raw message history by keyword."""
        cursor = await self._db.execute(
            """SELECT role, content FROM messages
               WHERE user_id = ? AND content LIKE ?
               ORDER BY id DESC LIMIT ?""",
            (user_id, f"%{query}%", limit),
        )
        rows = await cursor.fetchall()
        return [Message(role=r, content=c) for r, c in rows]

    async def _prune(self, user_id: str, keep: int = MAX_RAW_KEEP) -> None:
        """Delete messages beyond the most recent `keep` per user.

        Uses a cutoff-ID approach instead of NOT IN correlated subquery —
        much faster on large tables since it only needs one index scan.
        """
        try:
            cursor = await self._db.execute(
                """SELECT id FROM messages WHERE user_id = ?
                   ORDER BY id DESC LIMIT 1 OFFSET ?""",
                (user_id, keep),
            )
            row = await cursor.fetchone()
            if row:
                await self._db.execute(
                    "DELETE FROM messages WHERE user_id = ? AND id < ?",
                    (user_id, row[0]),
                )
                await self._db.commit()
        except Exception:
            log.debug("Prune failed (non-fatal)", exc_info=True)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
