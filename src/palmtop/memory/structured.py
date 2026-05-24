from __future__ import annotations

import aiosqlite
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

CATEGORIES = ("preference", "contact", "project", "fact")

EXTRACT_PROMPT = """\
You are a memory extraction system. Read the conversation below and extract \
any facts worth remembering long-term. Focus on:
- User preferences (likes, dislikes, how they want things done)
- People and contacts (names, roles, relationships)
- Projects and goals (what they're working on, deadlines, status)
- General facts (location, schedule patterns, important dates)

DO NOT extract any of the following — these are transient, not permanent facts:
- Tool errors, API failures, or integration issues (e.g. "got a 410 error", \
"permissions issue on the integration side", "tool returned an error")
- System status observations (e.g. "connection is down", "server unavailable")
- Troubleshooting steps or diagnostic suggestions from the assistant
- Anything prefixed with ⚠️ — these are ephemeral system notices, not facts

Return one memory per line in this exact format:
CATEGORY: content

Categories: preference, contact, project, fact

Only extract things explicitly stated by the user. Do not infer or guess. \
Do not extract the assistant's observations about its own capabilities or errors. \
If there is nothing worth remembering, reply with NONE.

Conversation:
User: {user_msg}
Assistant: {assistant_msg}

Memories to extract:"""


@dataclass
class Memory:
    id: int
    user_id: str
    category: str
    content: str
    created_at: str


class StructuredMemory:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_user
            ON memories(user_id, category)
        """)
        # FTS5 full-text index for fast keyword search
        try:
            await self._db.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(content, content=memories, content_rowid=id)
            """)
            # Populate FTS from existing rows (no-op if already synced)
            await self._db.execute("""
                INSERT OR IGNORE INTO memories_fts(rowid, content)
                SELECT id, content FROM memories
            """)
        except Exception:
            log.debug("FTS5 not available — falling back to LIKE search", exc_info=True)
            self._has_fts = False
        else:
            self._has_fts = True
        await self._db.commit()
        log.info("Structured memory ready: %s (fts=%s)", self._db_path, self._has_fts)

    async def store(self, user_id: str, category: str, content: str) -> int:
        if category not in CATEGORIES:
            category = "fact"
        content = content.strip()
        if await self._is_duplicate(user_id, category, content):
            log.debug("Skipping duplicate memory: %s", content[:60])
            return -1
        cursor = await self._db.execute(
            "INSERT INTO memories (user_id, category, content) VALUES (?, ?, ?)",
            (user_id, category, content),
        )
        row_id = cursor.lastrowid
        # Keep FTS index in sync
        if self._has_fts:
            try:
                await self._db.execute(
                    "INSERT INTO memories_fts(rowid, content) VALUES (?, ?)",
                    (row_id, content),
                )
            except Exception:
                log.debug("FTS insert failed (non-fatal)", exc_info=True)
        await self._db.commit()
        log.info("Stored %s memory: %s", category, content[:80])
        return row_id

    async def _is_duplicate(self, user_id: str, category: str, content: str) -> bool:
        cursor = await self._db.execute(
            """SELECT 1 FROM memories
               WHERE user_id = ? AND category = ? AND content = ?
               LIMIT 1""",
            (user_id, category, content),
        )
        return await cursor.fetchone() is not None

    async def recall(
        self, user_id: str, category: str | None = None, limit: int = 50
    ) -> list[Memory]:
        if category:
            cursor = await self._db.execute(
                """SELECT id, user_id, category, content, created_at
                   FROM memories WHERE user_id = ? AND category = ?
                   ORDER BY id DESC LIMIT ?""",
                (user_id, category, limit),
            )
        else:
            cursor = await self._db.execute(
                """SELECT id, user_id, category, content, created_at
                   FROM memories WHERE user_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (user_id, limit),
            )
        rows = await cursor.fetchall()
        return [Memory(id=r[0], user_id=r[1], category=r[2], content=r[3], created_at=r[4]) for r in rows]

    async def search(self, user_id: str, query: str, limit: int = 20) -> list[Memory]:
        if self._has_fts and query:
            # FTS5 MATCH is much faster than LIKE on large tables
            try:
                cursor = await self._db.execute(
                    """SELECT m.id, m.user_id, m.category, m.content, m.created_at
                       FROM memories m
                       JOIN memories_fts f ON m.id = f.rowid
                       WHERE m.user_id = ? AND f.content MATCH ?
                       ORDER BY m.id DESC LIMIT ?""",
                    (user_id, query, limit),
                )
                rows = await cursor.fetchall()
                return [Memory(id=r[0], user_id=r[1], category=r[2], content=r[3], created_at=r[4]) for r in rows]
            except Exception:
                log.debug("FTS search failed, falling back to LIKE", exc_info=True)
        # Fallback: LIKE search
        cursor = await self._db.execute(
            """SELECT id, user_id, category, content, created_at
               FROM memories WHERE user_id = ? AND content LIKE ?
               ORDER BY id DESC LIMIT ?""",
            (user_id, f"%{query}%", limit),
        )
        rows = await cursor.fetchall()
        return [Memory(id=r[0], user_id=r[1], category=r[2], content=r[3], created_at=r[4]) for r in rows]

    async def forget(self, memory_id: int) -> None:
        if self._has_fts:
            try:
                await self._db.execute(
                    "INSERT INTO memories_fts(memories_fts, rowid, content) "
                    "SELECT 'delete', id, content FROM memories WHERE id = ?",
                    (memory_id,),
                )
            except Exception:
                log.debug("FTS delete failed (non-fatal)", exc_info=True)
        await self._db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()


def parse_extraction(raw: str) -> list[tuple[str, str]]:
    results = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if line.upper() == "NONE" or not line:
            continue
        if ":" not in line:
            continue
        cat, _, content = line.partition(":")
        cat = cat.strip().lower().rstrip("s")
        if cat not in CATEGORIES:
            cat = "fact"
        content = content.strip()
        if content:
            results.append((cat, content))
    return results
