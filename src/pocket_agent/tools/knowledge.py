from __future__ import annotations

import logging

from pocket_agent.knowledge.store import KnowledgeBase
from pocket_agent.tools.base import Tool

log = logging.getLogger(__name__)


class KnowledgeTool(Tool):
    name = "kb"
    description = (
        "Your personal knowledge base. Usage:\n"
        "  [TOOL:kb] search <query>\n"
        "  [TOOL:kb] add <title> | <content> | <tags>\n"
        "  [TOOL:kb] tag <tag_name>\n"
        "  [TOOL:kb] recent\n"
        "  [TOOL:kb] get <id>\n"
        "  [TOOL:kb] arch <query>  — search your own codebase architecture"
    )

    def __init__(self, kb: KnowledgeBase) -> None:
        self._kb = kb

    async def run(self, query: str) -> str:
        parts = query.strip().split(None, 1)
        if not parts:
            return "Usage: search <query> | add <title>|<content>|<tags> | tag <tag> | recent | get <id>"

        action = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if action == "search":
            return await self._search(rest)
        elif action == "add":
            return await self._add(rest)
        elif action == "tag":
            return await self._by_tag(rest)
        elif action == "recent":
            return await self._recent()
        elif action == "get":
            return await self._get(rest)
        elif action == "delete":
            return await self._delete(rest)
        elif action == "arch":
            return await self._arch_search(rest)
        else:
            return await self._search(query)

    async def _search(self, query: str) -> str:
        if not query:
            return "Need a search query."
        entries = await self._kb.search(query)
        if not entries:
            return f"No results for: {query}"
        return _format_entries(entries)

    async def _add(self, text: str) -> str:
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 2:
            return "Format: add Title | Content | optional tags"
        title = parts[0]
        content = parts[1]
        tags = parts[2] if len(parts) > 2 else ""
        eid = await self._kb.add(title, content, tags)
        return f"Added to knowledge base (#{eid}): {title}"

    async def _by_tag(self, tag: str) -> str:
        if not tag:
            return "Need a tag name."
        entries = await self._kb.list_by_tag(tag)
        if not entries:
            return f"No entries tagged: {tag}"
        return _format_entries(entries)

    async def _recent(self) -> str:
        entries = await self._kb.list_recent(10)
        if not entries:
            return "Knowledge base is empty."
        count = await self._kb.count()
        return f"Knowledge base ({count} entries):\n\n" + _format_entries(entries)

    async def _get(self, text: str) -> str:
        try:
            eid = int(text.strip().lstrip("#"))
        except ValueError:
            return "Need an entry ID number."
        entry = await self._kb.get(eid)
        if not entry:
            return f"No entry #{eid}."
        tags = f"\nTags: {entry.tags}" if entry.tags else ""
        source = f"\nSource: {entry.source}" if entry.source else ""
        return f"#{entry.id} — {entry.title}{tags}{source}\n\n{entry.content}"

    async def _delete(self, text: str) -> str:
        try:
            eid = int(text.strip().lstrip("#"))
        except ValueError:
            return "Need an entry ID number."
        if await self._kb.delete(eid):
            return f"Deleted entry #{eid}."
        return f"No entry #{eid}."

    async def _arch_search(self, query: str) -> str:
        """Search architecture-tagged KB entries (indexed codebase)."""
        if not query.strip():
            # List all architecture entries
            entries = await self._kb.list_by_tag("architecture")
            if not entries:
                return "No architecture entries indexed yet. Run: python scripts/index_architecture.py"
            header = f"Architecture index ({len(entries)} modules):\n\n"
            lines = []
            for e in entries:
                lines.append(f"#{e.id} — {e.title}")
            return header + "\n".join(lines)

        entries = await self._kb.search_by_tag(query, "architecture")
        if not entries:
            return f"No architecture matches for: {query}"
        return _format_entries(entries)


def _format_entries(entries) -> str:
    lines = []
    for e in entries:
        tags = f" [{e.tags}]" if e.tags else ""
        preview = e.content[:120].replace("\n", " ")
        if len(e.content) > 120:
            preview += "..."
        lines.append(f"#{e.id} — {e.title}{tags}\n  {preview}")
    return "\n\n".join(lines)
