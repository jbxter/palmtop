"""Cursor Cloud Agents API client tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from pocket_agent.cursor.client import CursorAgentsClient, CursorAPIError


@pytest.mark.asyncio
async def test_create_agent_success() -> None:
    client = CursorAgentsClient("cursor_test_key")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b'{"agent":{"id":"bc-1","url":"https://cursor.com/agents/bc-1"},"run":{"id":"run-1","status":"CREATING"}}'
    mock_resp.json.return_value = {
        "agent": {"id": "bc-1", "url": "https://cursor.com/agents/bc-1"},
        "run": {"id": "run-1", "status": "CREATING"},
    }
    client._client.request = AsyncMock(return_value=mock_resp)

    data = await client.create_agent(
        "Fix tests",
        repo_url="https://github.com/org/repo",
        starting_ref="main",
    )
    assert data["agent"]["id"] == "bc-1"
    call = client._client.request.call_args
    assert call.args[0] == "POST"
    assert call.args[1] == "/v1/agents"
    body = call.kwargs["json"]
    assert body["prompt"]["text"] == "Fix tests"
    assert body["repos"][0]["url"] == "https://github.com/org/repo"
    await client.close()


@pytest.mark.asyncio
async def test_create_agent_auth_error() -> None:
    client = CursorAgentsClient("bad-key")
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = "unauthorized"
    mock_resp.content = b"unauthorized"
    client._client.request = AsyncMock(return_value=mock_resp)

    with pytest.raises(CursorAPIError, match="401"):
        await client.create_agent("x", repo_url="https://github.com/a/b")
    await client.close()


@pytest.mark.asyncio
async def test_get_run_terminal() -> None:
    client = CursorAgentsClient("cursor_test_key")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"{}"
    mock_resp.json.return_value = {
        "id": "run-1",
        "status": "FINISHED",
        "result": "Done.",
        "git": {"branches": [{"prUrl": "https://github.com/org/repo/pull/9"}]},
    }
    client._client.request = AsyncMock(return_value=mock_resp)

    run = await client.get_run("bc-1", "run-1")
    assert run["status"] == "FINISHED"
    assert run["git"]["branches"][0]["prUrl"].endswith("/pull/9")
    await client.close()
