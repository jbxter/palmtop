"""Railway deploy tool tests (mocked HTTP)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from palmtop.config.settings import RailwayConfig
from palmtop.tools.railway import RailwayDeployTool


@pytest.fixture
def cfg() -> RailwayConfig:
    return RailwayConfig(
        enabled=True,
        api_token="test-token",
        default_project_id="proj-1",
        default_service_id="svc-1",
        default_environment_id="env-1",
        require_blessing=False,
    )


@pytest.mark.asyncio
async def test_verify_auth_ok(cfg: RailwayConfig) -> None:
    tool = RailwayDeployTool(cfg)
    with patch.object(tool, "_graphql", new_callable=AsyncMock) as gql:
        gql.return_value = {"me": {"id": "user-1", "email": "a@b.co"}}
        assert await tool.verify_auth() is None


@pytest.mark.asyncio
async def test_deploy_triggers_mutation(cfg: RailwayConfig) -> None:
    tool = RailwayDeployTool(cfg)
    with patch.object(tool, "_graphql", new_callable=AsyncMock) as gql:
        gql.return_value = {}
        reply = await tool.run("deploy")
        assert "triggered" in reply.lower()
        gql.assert_called_once()
        assert "serviceInstanceDeploy" in gql.call_args[0][0]


@pytest.mark.asyncio
async def test_deploy_requires_ids(cfg: RailwayConfig) -> None:
    cfg.default_service_id = ""
    tool = RailwayDeployTool(cfg)
    reply = await tool.run("deploy")
    assert "default_service_id" in reply


@pytest.mark.asyncio
async def test_list_deployments(cfg: RailwayConfig) -> None:
    tool = RailwayDeployTool(cfg)
    with patch.object(tool, "_graphql", new_callable=AsyncMock) as gql:
        gql.return_value = {
            "deployments": {
                "edges": [
                    {"node": {"id": "dep-1", "status": "SUCCESS", "url": "https://x.up.railway.app"}},
                ]
            }
        }
        reply = await tool.run("deployments")
        assert "dep-1" in reply
        assert "SUCCESS" in reply


@pytest.mark.asyncio
async def test_deploy_denied_when_blessing_rejected(cfg: RailwayConfig) -> None:
    cfg.require_blessing = True
    tool = RailwayDeployTool(cfg, blessing_gate=MagicMock())
    tool.set_notify(AsyncMock())

    with patch(
        "palmtop.tools.railway.request_deploy_blessing",
        new_callable=AsyncMock,
        return_value=False,
    ):
        reply = await tool.run("deploy")
    assert "denied" in reply.lower()
