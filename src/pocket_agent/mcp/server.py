"""Minimal MCP server exposing the knowledge base over stdio.

Run:  python -m pocket_agent.mcp.server [data_dir]

Add to Claude Desktop config (claude_desktop_config.json):
{
  "mcpServers": {
    "pocket-agent-kb": {
      "command": "ssh",
      "args": ["-p", "8022", "user@<s21-tailscale-ip>",
               "cd ~/projects/s21-agent && .venv/bin/python -m pocket_agent.mcp.server"]
    }
  }
}
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

from pocket_agent.knowledge.store import KnowledgeBase

log = logging.getLogger(__name__)

SERVER_INFO = {
    "name": "pocket-agent-kb",
    "version": "0.1.0",
}

TOOLS = [
    {
        "name": "kb_search",
        "description": "Search the knowledge base by keyword or topic",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "kb_add",
        "description": "Add an entry to the knowledge base",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Entry title"},
                "content": {"type": "string", "description": "Entry content"},
                "tags": {"type": "string", "description": "Comma-separated tags", "default": ""},
                "source": {"type": "string", "description": "Source URL or reference", "default": ""},
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "kb_get",
        "description": "Get a knowledge base entry by ID",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "Entry ID"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "kb_list",
        "description": "List recent knowledge base entries, optionally filtered by tag",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Filter by tag", "default": ""},
                "limit": {"type": "integer", "description": "Max results", "default": 20},
            },
        },
    },
    {
        "name": "kb_delete",
        "description": "Delete a knowledge base entry by ID",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "Entry ID"},
            },
            "required": ["id"],
        },
    },
]


def _jsonrpc_response(id, result):
    return {"jsonrpc": "2.0", "id": id, "result": result}


def _jsonrpc_error(id, code, message):
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


def _entry_to_dict(e):
    return {
        "id": e.id,
        "title": e.title,
        "content": e.content,
        "tags": e.tags,
        "source": e.source,
        "created_at": e.created_at,
    }


class MCPServer:
    def __init__(self, kb: KnowledgeBase) -> None:
        self._kb = kb

    async def handle(self, request: dict) -> dict:
        method = request.get("method", "")
        rid = request.get("id")
        params = request.get("params", {})

        if method == "initialize":
            return _jsonrpc_response(rid, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            })

        elif method == "notifications/initialized":
            return None

        elif method == "tools/list":
            return _jsonrpc_response(rid, {"tools": TOOLS})

        elif method == "tools/call":
            tool_name = params.get("name", "")
            args = params.get("arguments", {})
            try:
                result = await self._call_tool(tool_name, args)
                return _jsonrpc_response(rid, {
                    "content": [{"type": "text", "text": result}],
                })
            except Exception as e:
                return _jsonrpc_response(rid, {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "isError": True,
                })

        elif method == "ping":
            return _jsonrpc_response(rid, {})

        else:
            return _jsonrpc_error(rid, -32601, f"Unknown method: {method}")

    async def _call_tool(self, name: str, args: dict) -> str:
        if name == "kb_search":
            entries = await self._kb.search(args["query"])
            if not entries:
                return "No results found."
            return json.dumps([_entry_to_dict(e) for e in entries], indent=2)

        elif name == "kb_add":
            eid = await self._kb.add(
                args["title"], args["content"],
                args.get("tags", ""), args.get("source", ""),
            )
            return f"Added entry #{eid}: {args['title']}"

        elif name == "kb_get":
            entry = await self._kb.get(args["id"])
            if not entry:
                return f"No entry #{args['id']}"
            return json.dumps(_entry_to_dict(entry), indent=2)

        elif name == "kb_list":
            tag = args.get("tag", "")
            limit = args.get("limit", 20)
            if tag:
                entries = await self._kb.list_by_tag(tag, limit)
            else:
                entries = await self._kb.list_recent(limit)
            if not entries:
                return "No entries."
            return json.dumps([_entry_to_dict(e) for e in entries], indent=2)

        elif name == "kb_delete":
            ok = await self._kb.delete(args["id"])
            return f"Deleted entry #{args['id']}" if ok else f"No entry #{args['id']}"

        else:
            raise ValueError(f"Unknown tool: {name}")


async def run_stdio(data_dir: Path) -> None:
    kb = KnowledgeBase(data_dir / "knowledge.db")
    await kb.init()
    server = MCPServer(kb)

    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

    buf = b""
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            break
        buf += chunk

        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue

            response = await server.handle(request)
            if response is not None:
                out = json.dumps(response) + "\n"
                sys.stdout.write(out)
                sys.stdout.flush()

    await kb.close()


def main():
    data_dir = Path("data")
    if len(sys.argv) > 1:
        data_dir = Path(sys.argv[1])

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    asyncio.run(run_stdio(data_dir))


if __name__ == "__main__":
    main()
