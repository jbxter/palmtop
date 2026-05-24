"""MCP client — connects to any MCP server over stdio.

Discovers tools via tools/list and exposes them as regular Tool instances
so the agent loop doesn't need to know they're MCP-backed.

Prefer Python-based servers (uvx, python -m) for Termux compatibility.
Prerequisite checks run at startup — if the command binary isn't found,
the server is skipped with a clear log message.

Usage:
    config = MCPServerConfig("atlassian", ["python", "-m", "pocket_agent.mcp.atlassian_server"])
    client = MCPClient(config)
    await client.connect()
    tools = client.get_tools()  # list[Tool] ready for ToolRegistry
    await client.close()
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field

from pocket_agent.tools.base import Tool

log = logging.getLogger(__name__)


def check_mcp_prerequisites(command: list[str]) -> str | None:
    """Check if the command's executable is available. Returns error message or None."""
    if not command:
        return "Empty command"
    exe = command[0]
    # uvx/uv — check availability
    if exe in ("uvx", "uv"):
        if not shutil.which(exe):
            return f"'{exe}' not found. Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
    elif not shutil.which(exe):
        return f"'{exe}' not found in PATH"
    return None


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server connection."""
    name: str                           # display name, e.g. "atlassian"
    command: list[str]                  # e.g. ["npx", "@anthropic/atlassian-mcp"]
    env: dict[str, str] = field(default_factory=dict)  # extra env vars
    cwd: str | None = None              # working directory for the subprocess


class MCPClient:
    """Manages a stdio connection to a single MCP server.

    Supports lazy connection — call connect() explicitly or let the first
    tool call trigger it automatically.
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._tools_meta: list[dict] = []
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._connected = False

    @property
    def name(self) -> str:
        return self._config.name

    async def ensure_connected(self) -> None:
        """Connect if not already connected."""
        if not self._connected:
            await self.connect()

    async def connect(self) -> None:
        """Launch the MCP server subprocess and initialize."""
        # Pre-flight: check that the command executable exists
        prereq_error = check_mcp_prerequisites(self._config.command)
        if prereq_error:
            raise RuntimeError(f"MCP '{self._config.name}' can't start: {prereq_error}")

        import os
        import sys
        env = {**os.environ, **self._config.env}

        # Replace bare "python" with sys.executable so the subprocess uses
        # the same interpreter/venv that's running the agent
        command = list(self._config.command)
        if command and command[0] in ("python", "python3"):
            command[0] = sys.executable

        log.info("Starting MCP server: %s (%s)", self._config.name, " ".join(command))
        cwd = self._config.cwd
        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"MCP '{self._config.name}': command not found: {self._config.command[0]}"
            )
        except PermissionError:
            raise RuntimeError(
                f"MCP '{self._config.name}': permission denied running: {self._config.command[0]}"
            )

        # Check if process died immediately (bad command, missing package, etc.)
        await asyncio.sleep(0.5)
        if self._process.returncode is not None:
            stderr = ""
            if self._process.stderr:
                stderr = (await self._process.stderr.read()).decode(errors="replace")[:500]
            raise RuntimeError(
                f"MCP '{self._config.name}' exited immediately (code {self._process.returncode})"
                + (f": {stderr}" if stderr else "")
            )

        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())

        # Initialize handshake — longer timeout for first run (uvx downloads packages)
        try:
            init_result = await self._request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pocket-agent", "version": "0.1.0"},
            }, timeout=90.0)
        except (TimeoutError, RuntimeError) as e:
            await self._kill_process()
            raise RuntimeError(f"MCP '{self._config.name}' handshake failed: {e}")

        log.info("MCP %s initialized: %s", self._config.name, init_result.get("serverInfo", {}))

        # Send initialized notification (no response expected)
        await self._notify("notifications/initialized", {})

        # Discover tools
        tools_result = await self._request("tools/list", {}, timeout=60.0)
        self._tools_meta = tools_result.get("tools", [])
        self._connected = True
        log.info("MCP %s: discovered %d tools", self._config.name, len(self._tools_meta))

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call an MCP tool and return the text result."""
        await self.ensure_connected()
        result = await self._request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })

        # Extract text from content array
        content = result.get("content", [])
        texts = []
        for item in content:
            if item.get("type") == "text":
                texts.append(item["text"])

        response = "\n".join(texts) if texts else json.dumps(result, indent=2)

        if result.get("isError"):
            log.warning("MCP tool %s returned error: %s", tool_name, response[:200])

        return response

    def get_tools(self) -> list[Tool]:
        """Return Tool instances for each discovered MCP tool."""
        tools = []
        for meta in self._tools_meta:
            tool = MCPToolBridge(
                client=self,
                tool_name=meta["name"],
                tool_description=meta.get("description", ""),
                input_schema=meta.get("inputSchema", {}),
            )
            tools.append(tool)
        return tools

    async def _request(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        """Send a JSON-RPC request and wait for the response."""
        self._request_id += 1
        rid = self._request_id

        msg = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params,
        }

        future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future

        line = json.dumps(msg) + "\n"
        self._process.stdin.write(line.encode())
        await self._process.stdin.drain()

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise TimeoutError(f"MCP {self._config.name}: timeout on {method}")

    async def _notify(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        line = json.dumps(msg) + "\n"
        self._process.stdin.write(line.encode())
        await self._process.stdin.drain()

    async def _read_loop(self) -> None:
        """Read responses from the MCP server's stdout."""
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("MCP %s non-JSON stdout: %s", self._config.name, line[:200])
                    continue

                rid = msg.get("id")
                if rid is not None and rid in self._pending:
                    future = self._pending.pop(rid)
                    if "error" in msg:
                        future.set_exception(
                            RuntimeError(f"MCP error: {msg['error'].get('message', msg['error'])}")
                        )
                    else:
                        future.set_result(msg.get("result", {}))
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("MCP %s reader loop failed", self._config.name)

    async def _stderr_loop(self) -> None:
        """Log stderr from the MCP server for debugging."""
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                if text:
                    log.info("MCP %s stderr: %s", self._config.name, text)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass  # stderr logging is best-effort

    async def _kill_process(self) -> None:
        """Force-kill the subprocess (used on failed startup)."""
        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()
        if self._process:
            try:
                self._process.kill()
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except Exception:
                pass

    async def close(self) -> None:
        """Shut down the MCP server."""
        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()
        if self._process:
            try:
                self._process.stdin.close()
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except Exception:
                self._process.kill()
            log.info("MCP %s shut down", self._config.name)


class MCPToolBridge(Tool):
    """Bridges an MCP tool into the agent's Tool interface.

    Parses natural-language queries into the MCP tool's expected arguments
    based on the input schema. For simple tools with a single string param,
    passes the query directly. For complex schemas, tries JSON first, then
    maps the query to the most likely parameter.
    """

    def __init__(
        self,
        client: MCPClient,
        tool_name: str,
        tool_description: str,
        input_schema: dict,
    ) -> None:
        self._client = client
        self._tool_name = tool_name
        self._schema = input_schema
        # Use server_name:tool_name for uniqueness across MCP servers
        self.name = f"{client.name}:{tool_name}"
        self.description = tool_description

    async def run(self, query: str) -> str:
        """Execute the MCP tool.

        The query can be:
        - Raw JSON: {"issueIdOrKey": "PROJ-123", "cloudId": "..."}
        - Natural text: mapped to the most likely string parameter
        """
        arguments = self._parse_query(query)
        try:
            return await self._client.call_tool(self._tool_name, arguments)
        except Exception as e:
            log.exception("MCP tool %s failed", self._tool_name)
            return f"Error calling {self._tool_name}: {e}"

    def _parse_query(self, query: str) -> dict:
        """Convert a query string into tool arguments."""
        query = query.strip()

        # Try parsing as JSON first
        if query.startswith("{"):
            try:
                return json.loads(query)
            except json.JSONDecodeError:
                pass

        # Get schema properties
        props = self._schema.get("properties", {})
        required = self._schema.get("required", [])

        # If schema has a single required string param, use the query directly
        string_params = [
            k for k, v in props.items()
            if v.get("type") == "string" and k in required
        ]
        if len(string_params) == 1:
            return {string_params[0]: query}

        # Try common param names
        for name in ("query", "searchString", "jql", "cql", "q", "text"):
            if name in props:
                return {name: query}

        # For issueIdOrKey style — if the query looks like a key (e.g., PROJ-123)
        if "issueIdOrKey" in props and query.replace("-", "").replace("_", "").isalnum():
            return {"issueIdOrKey": query}

        # Fallback: stuff it into the first required string param
        for key in required:
            if props.get(key, {}).get("type") == "string":
                return {key: query}

        # Last resort: return as-is
        return {"query": query}

    async def close(self) -> None:
        pass  # Client handles cleanup
