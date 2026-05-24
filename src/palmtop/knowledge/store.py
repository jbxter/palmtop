from __future__ import annotations

import aiosqlite
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class KBEntry:
    id: int
    title: str
    content: str
    tags: str
    source: str
    created_at: str


def _sanitize_fts5(query: str) -> str:
    """Sanitize a query for FTS5 MATCH — quote each token to escape special chars."""
    # Strip FTS5 operators and special characters, keep words
    words = []
    for token in query.split():
        # Remove anything that isn't alphanumeric, underscore, or hyphen
        clean = "".join(c for c in token if c.isalnum() or c in ("_", "-"))
        if clean:
            words.append(f'"{clean}"')
    return " ".join(words) if words else '""'


class KnowledgeBase:
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
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self._db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts
            USING fts5(title, content, tags, content=entries, content_rowid=id)
        """)
        # Triggers to keep FTS in sync
        await self._db.execute("""
            CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
                INSERT INTO entries_fts(rowid, title, content, tags)
                VALUES (new.id, new.title, new.content, new.tags);
            END
        """)
        await self._db.execute("""
            CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
                INSERT INTO entries_fts(entries_fts, rowid, title, content, tags)
                VALUES ('delete', old.id, old.title, old.content, old.tags);
            END
        """)
        await self._db.execute("""
            CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN
                INSERT INTO entries_fts(entries_fts, rowid, title, content, tags)
                VALUES ('delete', old.id, old.title, old.content, old.tags);
                INSERT INTO entries_fts(rowid, title, content, tags)
                VALUES (new.id, new.title, new.content, new.tags);
            END
        """)
        await self._db.commit()
        log.info("Knowledge base ready: %s", self._db_path)

    async def add(self, title: str, content: str, tags: str = "", source: str = "") -> int:
        cursor = await self._db.execute(
            "INSERT INTO entries (title, content, tags, source) VALUES (?, ?, ?, ?)",
            (title.strip(), content.strip(), tags.strip(), source.strip()),
        )
        await self._db.commit()
        log.info("KB added #%d: %s", cursor.lastrowid, title[:60])
        return cursor.lastrowid

    async def search(self, query: str, limit: int = 10) -> list[KBEntry]:
        safe_query = _sanitize_fts5(query)
        cursor = await self._db.execute(
            """SELECT e.id, e.title, e.content, e.tags, e.source, e.created_at
               FROM entries_fts f
               JOIN entries e ON f.rowid = e.id
               WHERE entries_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (safe_query, limit),
        )
        rows = await cursor.fetchall()
        return [KBEntry(*r) for r in rows]

    async def get(self, entry_id: int) -> KBEntry | None:
        cursor = await self._db.execute(
            "SELECT id, title, content, tags, source, created_at FROM entries WHERE id = ?",
            (entry_id,),
        )
        row = await cursor.fetchone()
        return KBEntry(*row) if row else None

    async def list_by_tag(self, tag: str, limit: int = 20) -> list[KBEntry]:
        cursor = await self._db.execute(
            """SELECT id, title, content, tags, source, created_at
               FROM entries WHERE tags LIKE ?
               ORDER BY id DESC LIMIT ?""",
            (f"%{tag.strip()}%", limit),
        )
        rows = await cursor.fetchall()
        return [KBEntry(*r) for r in rows]

    async def search_by_tag(self, query: str, tag: str, limit: int = 10) -> list[KBEntry]:
        """FTS search filtered to entries with a specific tag."""
        safe_query = _sanitize_fts5(query)
        cursor = await self._db.execute(
            """SELECT e.id, e.title, e.content, e.tags, e.source, e.created_at
               FROM entries_fts f
               JOIN entries e ON f.rowid = e.id
               WHERE entries_fts MATCH ? AND e.tags LIKE ?
               ORDER BY rank
               LIMIT ?""",
            (safe_query, f"%{tag.strip()}%", limit),
        )
        rows = await cursor.fetchall()
        return [KBEntry(*r) for r in rows]

    async def list_recent(self, limit: int = 20) -> list[KBEntry]:
        cursor = await self._db.execute(
            """SELECT id, title, content, tags, source, created_at
               FROM entries ORDER BY id DESC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [KBEntry(*r) for r in rows]

    async def update(self, entry_id: int, title: str | None = None,
                     content: str | None = None, tags: str | None = None) -> bool:
        entry = await self.get(entry_id)
        if not entry:
            return False
        await self._db.execute(
            """UPDATE entries SET title = ?, content = ?, tags = ? WHERE id = ?""",
            (
                title.strip() if title else entry.title,
                content.strip() if content else entry.content,
                tags.strip() if tags else entry.tags,
                entry_id,
            ),
        )
        await self._db.commit()
        return True

    async def delete(self, entry_id: int) -> bool:
        cursor = await self._db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        await self._db.commit()
        return cursor.rowcount > 0

    async def count(self) -> int:
        cursor = await self._db.execute("SELECT COUNT(*) FROM entries")
        row = await cursor.fetchone()
        return row[0]

    async def close(self) -> None:
        if self._db:
            await self._db.close()
