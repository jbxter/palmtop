"""Cursor Cloud Agents API v1 client (httpx, no SDK)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

_PHONE_LIMITS = httpx.Limits(max_connections=5, max_keepalive_connections=2)

TERMINAL_RUN_STATUSES = frozenset({"FINISHED", "ERROR", "CANCELLED", "EXPIRED"})


class CursorAPIError(Exception):
    """API request failed before or during agent execution."""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class CursorAgentsClient:
    """Thin wrapper around https://api.cursor.com/v1/agents."""

    BASE_URL = "https://api.cursor.com"

    def __init__(self, api_key: str, *, timeout: float = 60.0) -> None:
        self._api_key = api_key.strip()
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            auth=httpx.BasicAuth(self._api_key, ""),
            timeout=timeout,
            limits=_PHONE_LIMITS,
            headers={"Content-Type": "application/json"},
        )

    async def create_agent(
        self,
        prompt: str,
        *,
        repo_url: str,
        starting_ref: str = "main",
        auto_create_pr: bool = True,
        skip_reviewer_request: bool = True,
    ) -> dict[str, Any]:
        """Create a cloud agent and enqueue its initial run."""
        body: dict[str, Any] = {
            "prompt": {"text": prompt},
            "repos": [{"url": repo_url, "startingRef": starting_ref}],
            "autoCreatePR": auto_create_pr,
            "skipReviewerRequest": skip_reviewer_request,
        }
        return await self._request("POST", "/v1/agents", json=body)

    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/agents/{agent_id}")

    async def get_run(self, agent_id: str, run_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/agents/{agent_id}/runs/{run_id}")

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as e:
            raise CursorAPIError("Cursor API timed out", retryable=True) from e
        except httpx.RequestError as e:
            raise CursorAPIError(f"Cursor API connection error: {e}", retryable=True) from e

        if resp.status_code == 401:
            raise CursorAPIError("Invalid CURSOR_API_KEY (401)", status_code=401)
        if resp.status_code == 403:
            raise CursorAPIError("Cursor API forbidden — check repo access (403)", status_code=403)
        if resp.status_code == 409:
            detail = resp.text[:200]
            raise CursorAPIError(f"Cursor API conflict (409): {detail}", status_code=409)
        if resp.status_code == 429:
            raise CursorAPIError("Cursor API rate limited (429)", status_code=429, retryable=True)
        if resp.status_code >= 500:
            raise CursorAPIError(
                f"Cursor API server error ({resp.status_code})",
                status_code=resp.status_code,
                retryable=True,
            )
        if resp.status_code >= 400:
            detail = resp.text[:300]
            raise CursorAPIError(
                f"Cursor API error {resp.status_code}: {detail}",
                status_code=resp.status_code,
            )

        if not resp.content:
            return {}
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()
