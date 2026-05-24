"""Vercel deploy tool tests (mocked HTTP)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from palmtop.config.settings import VercelConfig
from palmtop.tools.vercel import VercelDeployTool


@pytest.fixture
def cfg() -> VercelConfig:
    return VercelConfig(
        enabled=True,
        api_token="test-token",
        default_project_id="prj_test",
        default_project_name="my-app",
        default_target="production",
        default_branch="main",
        require_blessing=False,
    )


@pytest.mark.asyncio
async def test_verify_auth_ok(cfg: VercelConfig) -> None:
    tool = VercelDeployTool(cfg)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"user": {"username": "operator"}}

    with patch.object(tool, "_get_client") as get_client:
        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_resp)
        get_client.return_value = client
        assert await tool.verify_auth() is None


@pytest.mark.asyncio
async def test_deploy_creates_deployment(cfg: VercelConfig) -> None:
    tool = VercelDeployTool(cfg)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "id": "dpl_abc",
        "url": "my-app.vercel.app",
        "readyState": "QUEUED",
    }

    with patch.object(tool, "_get_client") as get_client:
        client = AsyncMock()
        client.post = AsyncMock(return_value=mock_resp)
        get_client.return_value = client
        reply = await tool.run("deploy main")
        assert "dpl_abc" in reply
        assert "QUEUED" in reply
        client.post.assert_called_once()
        body = client.post.call_args.kwargs["json"]
        assert body["project"] == "prj_test"
        assert body["withLatestCommit"] is True


@pytest.mark.asyncio
async def test_deploy_denied_when_blessing_rejected(cfg: VercelConfig) -> None:
    cfg.require_blessing = True
    gate = MagicMock()
    gate.request = MagicMock(return_value=False)
    tool = VercelDeployTool(cfg, blessing_gate=gate)
    tool.set_notify(AsyncMock())

    with patch(
        "palmtop.tools.vercel.request_deploy_blessing",
        new_callable=AsyncMock,
        return_value=False,
    ):
        reply = await tool.run("deploy")
    assert "denied" in reply.lower()


@pytest.mark.asyncio
async def test_projects_list(cfg: VercelConfig) -> None:
    tool = VercelDeployTool(cfg)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "projects": [{"id": "prj_1", "name": "alpha"}],
    }

    with patch.object(tool, "_get_client") as get_client:
        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_resp)
        get_client.return_value = client
        reply = await tool.run("projects")
        assert "alpha" in reply
        assert "prj_1" in reply
