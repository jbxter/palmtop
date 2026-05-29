"""Thin async HTTP client for the Hermes Agent API.

This isolates every assumption about Hermes' wire format in one place. The
endpoint shapes below are PROVISIONAL — Phase 0 includes a spike to confirm the
real memory/skills API surface, and the only file that should need to change
afterward is this one. Everything else (memory bridge, skill import) talks to
Hermes through these methods, never raw HTTP.

Expected surface (to be confirmed by the spike):
  GET    /health
  GET    /memories?since=<cursor>&limit=<n>   -> {"memories": [...]} | [...]
  POST   /memories            (upsert a record) -> the stored record (with id)
  DELETE /memories/{id}
  GET    /skills                               -> {"skills": [...]} | [...]
  GET    /skills/{id}                          -> {...}
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

# Match the connection ceiling the inference backends use, so a phone runtime
# doesn't open an unbounded number of sockets.
_LIMITS = httpx.Limits(max_connections=5, max_keepalive_connections=2)


class HermesAPIError(RuntimeError):
    """Raised when the Hermes API returns a non-2xx response."""


class HermesClient:
    """Async client for a Hermes Agent runtime.

    No API key is required for a local Hermes instance; pass one for hosted
    deployments (sent as a Bearer token).
    """

    def __init__(self, base_url: str = "http://localhost:8080", api_key: str = "", *, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout, limits=_LIMITS)

    def _headers(self) -> dict[str, str]:
        h = {"content-type": "application/json"}
        if self._api_key:
            h["authorization"] = f"Bearer {self._api_key}"
        return h

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            raise HermesAPIError(f"Hermes API {resp.status_code}: {resp.text[:200]}")

    async def health(self) -> bool:
        """Return True if the Hermes runtime is reachable and healthy."""
        try:
            resp = await self._client.get(f"{self._base_url}/health", headers=self._headers())
        except httpx.HTTPError as e:
            log.debug("Hermes health check failed: %s", e)
            return False
        return resp.status_code == 200

    # ── Memory ────────────────────────────────────────────────────────────────

    async def list_memories(self, *, since: str | None = None, limit: int = 200) -> list[dict]:
        """List memories, optionally only those changed after the ``since`` cursor."""
        params: dict[str, str | int] = {"limit": limit}
        if since:
            params["since"] = since
        resp = await self._client.get(f"{self._base_url}/memories", headers=self._headers(), params=params)
        self._raise_for_status(resp)
        return _as_list(resp.json(), key="memories")

    async def upsert_memory(self, record: dict) -> dict:
        """Create or update a memory in Hermes. Returns the stored record (with its id)."""
        resp = await self._client.post(f"{self._base_url}/memories", headers=self._headers(), json=record)
        self._raise_for_status(resp)
        return resp.json()

    async def delete_memory(self, hermes_id: str) -> None:
        """Delete a memory in Hermes by its remote id (propagates a Palmtop tombstone)."""
        resp = await self._client.delete(f"{self._base_url}/memories/{hermes_id}", headers=self._headers())
        self._raise_for_status(resp)

    # ── Skills ──────────────────────────────────────────────────────────────────

    async def list_skills(self) -> list[dict]:
        """List skills the Hermes runtime exposes."""
        resp = await self._client.get(f"{self._base_url}/skills", headers=self._headers())
        self._raise_for_status(resp)
        return _as_list(resp.json(), key="skills")

    async def get_skill(self, skill_id: str) -> dict:
        """Fetch a single skill's definition."""
        resp = await self._client.get(f"{self._base_url}/skills/{skill_id}", headers=self._headers())
        self._raise_for_status(resp)
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()


def _as_list(data: object, *, key: str) -> list[dict]:
    """Normalize a response that may be a bare list or wrapped under ``key``."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        inner = data.get(key)
        if isinstance(inner, list):
            return inner
    return []
