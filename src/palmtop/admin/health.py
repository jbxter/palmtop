"""Lightweight health/admin HTTP server.

Runs on a configurable port (default 8000) and exposes:
  GET /          → 200 OK (Docker healthcheck)
  GET /health    → JSON health status (uptime, channels, memory stats)
  GET /admin/stats → detailed stats (protected by bearer token)

This server starts as a background asyncio task alongside the channels.
It has no dependency on Starlette — just raw asyncio HTTP for minimal
footprint on the S21.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Started at import time; uptime = now - _START
_START = time.time()


@dataclass
class HealthState:
    """Shared mutable state that channels and tools update."""

    channels_active: list[str] = field(default_factory=list)
    last_message_at: float = 0.0
    messages_handled: int = 0
    data_dir: Path = field(default_factory=lambda: Path("data"))
    admin_token: str = ""  # empty = admin routes disabled


def _uptime() -> float:
    return time.time() - _START


def _format_uptime(seconds: float) -> str:
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _db_size(path: Path) -> int | None:
    """Return file size in bytes, or None if missing."""
    if path.exists():
        return path.stat().st_size
    return None


def health_json(state: HealthState) -> dict[str, Any]:
    """Build the /health response payload."""
    uptime_s = _uptime()
    return {
        "status": "ok",
        "uptime": _format_uptime(uptime_s),
        "uptime_seconds": int(uptime_s),
        "channels": state.channels_active,
        "last_message_at": state.last_message_at or None,
        "messages_handled": state.messages_handled,
        "databases": {
            "conversations": _db_size(state.data_dir / "conversations.db"),
            "memories": _db_size(state.data_dir / "memories.db"),
            "plans": _db_size(state.data_dir / "plans.db"),
            "knowledge": _db_size(state.data_dir / "knowledge.db"),
        },
    }


def stats_json(state: HealthState) -> dict[str, Any]:
    """Build the /admin/stats response (extends health with more detail)."""
    base = health_json(state)
    base["admin"] = True
    return base


class HealthServer:
    """Minimal async HTTP server for health checks and admin stats."""

    def __init__(self, state: HealthState, host: str = "0.0.0.0", port: int = 8000) -> None:
        self._state = state
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        """Start the HTTP server as a background task."""
        self._server = await asyncio.start_server(
            self._handle_connection,
            self._host,
            self._port,
        )
        log.info("Health server listening on %s:%d", self._host, self._port)

    async def stop(self) -> None:
        """Shut down the server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Parse a single HTTP request and respond."""
        try:
            # Read request line + headers (up to 8KB)
            data = await asyncio.wait_for(reader.read(8192), timeout=5.0)
            if not data:
                writer.close()
                return

            request = data.decode("utf-8", errors="replace")
            lines = request.split("\r\n")
            if not lines:
                writer.close()
                return

            # Parse method and path
            parts = lines[0].split(" ")
            if len(parts) < 2:
                writer.close()
                return

            path = parts[1]

            # Extract headers
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ": " in line:
                    k, v = line.split(": ", 1)
                    headers[k.lower()] = v

            # Route
            if path == "/" or path == "/health":
                body = json.dumps(health_json(self._state))
                await self._respond(writer, 200, body)
            elif path == "/admin/stats":
                if not self._check_auth(headers):
                    await self._respond(writer, 401, '{"error": "unauthorized"}')
                else:
                    body = json.dumps(stats_json(self._state))
                    await self._respond(writer, 200, body)
            else:
                await self._respond(writer, 404, '{"error": "not found"}')

        except (TimeoutError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            log.debug("Health server request error", exc_info=True)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _check_auth(self, headers: dict[str, str]) -> bool:
        """Verify bearer token for admin routes."""
        if not self._state.admin_token:
            return False  # admin disabled when no token configured
        auth = headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return hmac.compare_digest(auth[7:].strip(), self._state.admin_token)
        return False

    async def _respond(self, writer: asyncio.StreamWriter, status: int, body: str) -> None:
        """Write an HTTP response."""
        status_text = {200: "OK", 401: "Unauthorized", 404: "Not Found"}.get(status, "")
        response = (
            f"HTTP/1.1 {status} {status_text}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f"{body}"
        )
        writer.write(response.encode())
        await writer.drain()
