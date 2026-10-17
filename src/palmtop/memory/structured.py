from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from palmtop.memory.factlog import CATEGORIES, Entry, FactLog, FactLogError, FactLogFullError

log = logging.getLogger(__name__)

__all__ = [
    "CATEGORIES",
    "EXTRACT_PROMPT",
    "UNTRUSTED_FACT_TAG",
    "Memory",
    "StructuredMemory",
    "format_memory_context",
    "parse_extraction",
]

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
    log_id: str = ""  # fact-log id (empty when the signed log is off)
    agent_id: str = ""  # writer; empty for rows written without the log
    local: bool = True  # False for facts that came from another writer


_COLS = "id, user_id, category, content, created_at, log_id, agent_id"


class StructuredMemory:
    """Structured facts. With a ``FactLog`` the log is the source of truth and
    ``memories.db`` (incl. FTS5) is a projection rebuilt from it."""

    def __init__(self, db_path: Path, fact_log: FactLog | None = None) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._log = fact_log
        self._lock = asyncio.Lock()
        self._cursors: dict[str, str] = {}
        self._refused: str | None = None
        self._full_alerted = False
        self._synced = -1  # FactLog.version the projection has caught up to

    def _row(self, r) -> Memory:
        local = self._log is None or not r[6] or r[6] == self._log.agent_id
        return Memory(
            id=r[0], user_id=r[1], category=r[2], content=r[3], created_at=r[4],
            log_id=r[5] or "", agent_id=r[6] or "", local=local,
        )  # fmt: skip

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
        cols = {r[1] for r in await (await self._db.execute("PRAGMA table_info(memories)")).fetchall()}
        for col in ("log_id", "agent_id"):
            if col not in cols:
                await self._db.execute(f"ALTER TABLE memories ADD COLUMN {col} TEXT")
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_memories_log ON memories(log_id)")
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS fact_cursor (
                agent_id TEXT PRIMARY KEY,
                last_hash TEXT NOT NULL
            )
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
        if self._log is not None:
            await self._ensure_fresh()
        log.info("Structured memory ready: %s (fts=%s)", self._db_path, self._has_fts)

    # ── signed fact log: projection ──────────────────────────────────
    async def _ensure_fresh(self) -> None:
        """Verify the log and replay anything the projection hasn't applied.

        A broken chain / bad signature sets ``_refused``: search serves nothing and
        ``store`` raises until the owner fixes the log. Nothing is repaired.
        """
        if self._log is None:
            return
        try:
            async with self._lock:
                self._log.refresh()
                if self._log.version != self._synced or self._refused:
                    await self._sync_projection()
        except FactLogError as e:
            if self._refused != str(e):
                log.error("Fact log refused: %s", e)
            self._refused = str(e)
        else:
            self._refused = None

    async def _sync_projection(self) -> None:
        fl = self._log
        entries = fl.entries()
        await self._load_cursors()
        await self._attest_legacy(entries)
        entries = fl.entries()
        retracted = fl.retracted()
        # Revocation: rows from writers whose key is no longer allowed leave the projection.
        for agent in {e.agent for e in entries if not fl.is_allowed(e.key)}:
            await self._delete_where("agent_id = ?", (agent,))
            await self._db.execute("DELETE FROM fact_cursor WHERE agent_id = ?", (agent,))
            self._cursors.pop(agent, None)
        chains: dict[str, list[Entry]] = {}
        for e in entries:
            if fl.is_allowed(e.key):
                chains.setdefault(e.agent, []).append(e)
        for agent, chain in chains.items():
            last = self._cursors.get(agent, "")
            start = 0
            if last:
                idx = next((i for i, e in enumerate(chain) if e.hash == last), None)
                if idx is None:
                    raise FactLogError(f"projection cursor for writer {agent} is not in the log")
                start = idx + 1
            for e in chain[start:]:
                await self._apply(e, retracted)
            if chain[start:]:
                await self._set_cursor(agent, chain[-1].hash)
        for target in retracted:
            await self._delete_where("log_id = ?", (target,))
        await self._db.commit()
        self._synced = fl.version

    async def _load_cursors(self) -> None:
        rows = await (await self._db.execute("SELECT agent_id, last_hash FROM fact_cursor")).fetchall()
        self._cursors = {a: h for a, h in rows}

    async def _set_cursor(self, agent: str, last_hash: str) -> None:
        await self._db.execute(
            "INSERT INTO fact_cursor(agent_id, last_hash) VALUES (?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET last_hash = excluded.last_hash",
            (agent, last_hash),
        )
        self._cursors[agent] = last_hash

    async def _apply(self, e: Entry, retracted: set[str]) -> int | None:
        """Project one verified line. Idempotent. Returns the row id for a fact."""
        if e.type != "fact" or e.id in retracted:
            return None
        b = e.body
        row = await (await self._db.execute("SELECT id FROM memories WHERE log_id = ?", (e.id,))).fetchone()
        if row:
            return row[0]
        if e.agent == self._log.agent_id:
            # Adopt a pre-log row (legacy attestation) instead of inserting a twin.
            row = await (
                await self._db.execute(
                    "SELECT id FROM memories WHERE log_id IS NULL "
                    "AND user_id = ? AND category = ? AND content = ? LIMIT 1",
                    (b["user_id"], b["category"], b["content"]),
                )
            ).fetchone()
            if row:
                await self._db.execute(
                    "UPDATE memories SET log_id = ?, agent_id = ? WHERE id = ?", (e.id, e.agent, row[0])
                )
                return row[0]
        created = (b["orig_ts"] if b["legacy"] and b["orig_ts"] else b["ts"]).replace("T", " ")[:19]
        cur = await self._db.execute(
            "INSERT INTO memories (user_id, category, content, created_at, log_id, agent_id) VALUES (?, ?, ?, ?, ?, ?)",
            (b["user_id"], b["category"], b["content"], created, e.id, e.agent),
        )
        await self._fts_insert(cur.lastrowid, b["content"])
        return cur.lastrowid

    async def _attest_legacy(self, entries: list[Entry]) -> None:
        """Sign pre-log ``memories.db`` rows once, as a legacy attestation made *now*
        (the original created_at is carried as ``orig_ts``, not backdated)."""
        fl = self._log
        rows = await (
            await self._db.execute(
                "SELECT id, user_id, category, content, created_at FROM memories WHERE log_id IS NULL"
            )
        ).fetchall()
        if not rows:
            return
        have = {
            (e.body["user_id"], e.body["category"], e.body["content"])
            for e in entries
            if e.type == "fact" and e.agent == fl.agent_id
        }  # crash-safe: a line already appended is adopted by _apply, not re-signed
        n = 0
        for _id, user_id, category, content, created_at in rows:
            if category not in CATEGORIES:
                category = "fact"
            if (user_id, category, content) in have:
                continue
            fl.append_fact(user_id, category, content, legacy=True, orig_ts=str(created_at) if created_at else None)
            have.add((user_id, category, content))
            n += 1
        if n:
            log.info("Fact log: attested %d legacy memories", n)

    async def _fts_insert(self, row_id: int, content: str) -> None:
        if self._has_fts:
            try:
                await self._db.execute("INSERT INTO memories_fts(rowid, content) VALUES (?, ?)", (row_id, content))
            except Exception:
                log.debug("FTS insert failed (non-fatal)", exc_info=True)

    async def _delete_where(self, where: str, params: tuple) -> None:
        if self._has_fts:
            try:
                await self._db.execute(
                    f"INSERT INTO memories_fts(memories_fts, rowid, content) "
                    f"SELECT 'delete', id, content FROM memories WHERE {where}",
                    params,
                )
            except Exception:
                log.debug("FTS delete failed (non-fatal)", exc_info=True)
        await self._db.execute(f"DELETE FROM memories WHERE {where}", params)

    # ── write / read API (unchanged signatures) ──────────────────────
    async def store(self, user_id: str, category: str, content: str) -> int:
        if category not in CATEGORIES:
            category = "fact"
        content = content.strip()
        if self._log is not None:
            return await self._store_logged(user_id, category, content)
        if await self._is_duplicate(user_id, category, content):
            log.debug("Skipping duplicate memory: %s", content[:60])
            return -1
        cursor = await self._db.execute(
            "INSERT INTO memories (user_id, category, content) VALUES (?, ?, ?)",
            (user_id, category, content),
        )
        row_id = cursor.lastrowid
        await self._fts_insert(row_id, content)
        await self._db.commit()
        log.info("Stored %s memory: %s", category, content[:80])
        return row_id

    async def _store_logged(self, user_id: str, category: str, content: str) -> int:
        await self._ensure_fresh()
        if self._refused:
            raise FactLogError(self._refused)
        async with self._lock:
            if await self._is_duplicate(user_id, category, content):
                log.debug("Skipping duplicate memory: %s", content[:60])
                return -1
            if self._cursors.get(self._log.agent_id, "") != self._log.tip():
                await self._sync_projection()  # a previous append never reached SQLite
            v0 = self._log.version
            current = v0 == self._synced
            try:
                entry = self._log.append_fact(user_id, category, content)
            except FactLogFullError as e:
                if not self._full_alerted:  # alert once, like the cloud budget cap
                    self._full_alerted = True
                    log.warning("FACT LOG FULL: %s", e)
                raise
            row_id = await self._apply(entry, set())
            await self._set_cursor(entry.agent, entry.hash)
            await self._db.commit()
            if current and self._log.version == v0 + 1:  # no outside lines slipped in
                self._synced = self._log.version
        log.info("Stored %s memory: %s", category, content[:80])
        return row_id

    async def _is_duplicate(self, user_id: str, category: str, content: str) -> bool:
        sql = "SELECT 1 FROM memories WHERE user_id = ? AND category = ? AND content = ?"
        params: tuple = (user_id, category, content)
        if self._log is not None:  # duplicates are per writer; peers' rows are kept alongside
            sql += " AND agent_id = ?"
            params += (self._log.agent_id,)
        cursor = await self._db.execute(sql + " LIMIT 1", params)
        return await cursor.fetchone() is not None

    async def recall(self, user_id: str, category: str | None = None, limit: int = 50) -> list[Memory]:
        await self._ensure_fresh()
        if self._refused:
            return []
        if category:
            cursor = await self._db.execute(
                f"""SELECT {_COLS}
                   FROM memories WHERE user_id = ? AND category = ?
                   ORDER BY id DESC LIMIT ?""",
                (user_id, category, limit),
            )
        else:
            cursor = await self._db.execute(
                f"""SELECT {_COLS}
                   FROM memories WHERE user_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (user_id, limit),
            )
        rows = await cursor.fetchall()
        return [self._row(r) for r in rows]

    async def search(self, user_id: str, query: str, limit: int = 20) -> list[Memory]:
        await self._ensure_fresh()
        if self._refused:
            return []
        if self._has_fts and query:
            # FTS5 MATCH is much faster than LIKE on large tables
            try:
                cursor = await self._db.execute(
                    f"""SELECT {", ".join("m." + c.strip() for c in _COLS.split(","))}
                       FROM memories m
                       JOIN memories_fts f ON m.id = f.rowid
                       WHERE m.user_id = ? AND f.content MATCH ?
                       ORDER BY m.id DESC LIMIT ?""",
                    (user_id, query, limit),
                )
                rows = await cursor.fetchall()
                return [self._row(r) for r in rows]
            except Exception:
                log.debug("FTS search failed, falling back to LIKE", exc_info=True)
        # Fallback: LIKE search
        cursor = await self._db.execute(
            f"""SELECT {_COLS}
               FROM memories WHERE user_id = ? AND content LIKE ?
               ORDER BY id DESC LIMIT ?""",
            (user_id, f"%{query}%", limit),
        )
        rows = await cursor.fetchall()
        return [self._row(r) for r in rows]

    async def forget(self, memory_id: int) -> None:
        """Delete a fact. With the log this appends a tombstone first; no line is removed."""
        if self._log is not None:
            await self._ensure_fresh()
            if self._refused:
                raise FactLogError(self._refused)
        async with self._lock:
            if self._log is not None:
                row = await (
                    await self._db.execute("SELECT log_id, user_id FROM memories WHERE id = ?", (memory_id,))
                ).fetchone()
                if row and row[0]:
                    self._log.append_tombstone(row[1], row[0])
            await self._delete_where("id = ?", (memory_id,))
            await self._db.commit()

    async def correct(self, memory_id: int, content: str) -> int:
        """Replace a fact: tombstone the old line, append a new one. Returns the new row id."""
        row = await (
            await self._db.execute("SELECT user_id, category FROM memories WHERE id = ?", (memory_id,))
        ).fetchone()
        if row is None:
            return -1
        await self.forget(memory_id)
        return await self.store(row[0], row[1], content)

    async def close(self) -> None:
        if self._db:
            await self._db.close()


UNTRUSTED_FACT_TAG = "untrusted-fact"
PEER_FACT_LIMIT = 500


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def _fence_fact(m: Memory) -> str:
    """Wrap a peer fact like harness child output; the body can't close its own fence."""
    body = m.content[:PEER_FACT_LIMIT].replace("<", "‹")
    return f'<{UNTRUSTED_FACT_TAG} agent="{m.agent_id}" id="{m.log_id}">\n{body}\n</{UNTRUSTED_FACT_TAG}>'


def format_memory_context(memories: list[Memory]) -> str:
    """Prompt text for recalled facts. Local facts first as ``[category] content``; allowed
    peer facts follow inside an untrusted fence. A peer fact whose normalized text equals a
    local fact is omitted (both stay stored); different wording stays visible, fenced."""
    local = [m for m in memories if m.local]
    seen = {_normalize(m.content) for m in local}
    lines = [f"[{m.category}] {m.content}" for m in local]
    for m in memories:
        if m.local or _normalize(m.content) in seen:
            continue
        seen.add(_normalize(m.content))
        lines.append(_fence_fact(m))
    return "\n".join(lines)


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
