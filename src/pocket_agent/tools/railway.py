"""Railway service deploys via GraphQL API (RAILWAY_TOKEN)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import httpx

from pocket_agent.tools.base import Tool
from pocket_agent.tools.deploy_blessing import request_deploy_blessing

if TYPE_CHECKING:
    from pocket_agent.config.settings import RailwayConfig
    from pocket_agent.core.blessing import BlessingGate

log = logging.getLogger(__name__)

_GRAPHQL_URL = "https://backboard.railway.com/graphql/v2"
_PHONE_LIMITS = httpx.Limits(max_connections=5, max_keepalive_connections=2)


class RailwayDeployTool(Tool):
    name = "railway"
    description = (
        "Deploy or inspect Railway services. Usage:\n"
        "  [TOOL:railway] status\n"
        "  [TOOL:railway] deploy\n"
        "  [TOOL:railway] deployments\n"
        "  [TOOL:railway] get <deployment_id>"
    )

    def __init__(
        self,
        cfg: "RailwayConfig",
        *,
        blessing_gate: "BlessingGate | None" = None,
    ) -> None:
        self._cfg = cfg
        self._blessing_gate = blessing_gate
        self._send_fn: Callable[[str, str], Awaitable[None]] | None = None
        self._user_id = "default"
        self._client: httpx.AsyncClient | None = None

    def set_user_id(self, user_id: str) -> None:
        self._user_id = user_id

    def set_notify(self, send_fn: Callable[[str, str], Awaitable[None]] | None) -> None:
        self._send_fn = send_fn

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                limits=_PHONE_LIMITS,
                headers={
                    "Authorization": f"Bearer {self._cfg.api_token}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        client = self._get_client()
        resp = await client.post(
            _GRAPHQL_URL,
            json={"query": query, "variables": variables or {}},
        )
        if resp.status_code == 401:
            raise RuntimeError("Railway auth failed (401) — check RAILWAY_TOKEN")
        if resp.status_code != 200:
            raise RuntimeError(f"Railway API HTTP {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        if payload.get("errors"):
            msgs = "; ".join(
                e.get("message", str(e)) for e in payload["errors"][:3]
            )
            raise RuntimeError(f"Railway GraphQL error: {msgs}")
        return payload.get("data") or {}

    async def verify_auth(self) -> str | None:
        try:
            data = await self._graphql("query { me { id email } }")
            me = data.get("me") or {}
            if me.get("id"):
                log.info("Railway auth verified ✓ (%s)", me.get("email") or me["id"])
                return None
            return "Railway auth check returned no user — token may be invalid"
        except httpx.ConnectError:
            return "Can't reach Railway API — check network"
        except Exception as e:
            return f"Railway auth check failed: {e}"

    async def run(self, query: str) -> str:
        parts = query.strip().split(None, 1)
        if not parts:
            return "Usage: status | deploy | deployments | get <deployment_id>"

        action = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        try:
            if action == "status":
                return await self._status()
            if action == "deploy":
                return await self._deploy()
            if action == "deployments":
                return await self._list_deployments()
            if action == "get":
                return await self._get_deployment(rest)
            return f"Unknown action '{action}'. Usage: status | deploy | deployments | get <id>"
        except Exception as e:
            log.exception("Railway operation failed")
            return f"Railway error: {e}"

    async def _status(self) -> str:
        err = await self.verify_auth()
        if err:
            return err
        lines = ["Railway connected."]
        if self._cfg.default_service_id:
            lines.append(f"Service: {self._cfg.default_service_id}")
        else:
            lines.append("Set default_service_id in [railway] config.")
        if self._cfg.default_environment_id:
            lines.append(f"Environment: {self._cfg.default_environment_id}")
        else:
            lines.append("Set default_environment_id in [railway] config.")
        if self._cfg.default_project_id:
            lines.append(f"Project: {self._cfg.default_project_id}")
        return "\n".join(lines)

    async def _deploy(self) -> str:
        service_id = self._cfg.default_service_id
        environment_id = self._cfg.default_environment_id
        if not service_id or not environment_id:
            return (
                "Railway deploy needs default_service_id and default_environment_id "
                "in config.toml (copy IDs from the Railway dashboard URL or API)."
            )

        summary = f"Service: {service_id}\nEnvironment: {environment_id}"
        if self._cfg.require_blessing:
            approved = await request_deploy_blessing(
                self._blessing_gate,
                self._send_fn,
                self._user_id,
                platform="Railway",
                summary=summary,
            )
            if not approved:
                return "Railway deploy denied — not started."

        mutation = """
        mutation serviceInstanceDeploy($serviceId: String!, $environmentId: String!) {
          serviceInstanceDeploy(serviceId: $serviceId, environmentId: $environmentId)
        }
        """
        await self._graphql(
            mutation,
            {"serviceId": service_id, "environmentId": environment_id},
        )
        return (
            f"Railway deploy triggered for service {service_id} "
            f"(environment {environment_id}). "
            "Use [TOOL:railway] deployments to check status."
        )

    async def _list_deployments(self) -> str:
        project_id = self._cfg.default_project_id
        service_id = self._cfg.default_service_id
        if not project_id or not service_id:
            return "Set default_project_id and default_service_id for deployments listing."

        query = """
        query deployments($projectId: String!, $serviceId: String!) {
          deployments(
            first: 5
            input: { projectId: $projectId, serviceId: $serviceId }
          ) {
            edges {
              node {
                id
                status
                url
                createdAt
              }
            }
          }
        }
        """
        data = await self._graphql(
            query,
            {"projectId": project_id, "serviceId": service_id},
        )
        edges = (data.get("deployments") or {}).get("edges") or []
        if not edges:
            return "No recent Railway deployments found."

        lines = ["Recent Railway deployments:"]
        for edge in edges:
            node = edge.get("node") or {}
            lines.append(
                f"  {node.get('id', '?')} [{node.get('status', '?')}] {node.get('url') or ''}"
            )
        return "\n".join(lines)

    async def _get_deployment(self, deployment_id: str) -> str:
        if not deployment_id:
            return "Need a deployment id. Usage: get <deployment_id>"

        query = """
        query deployment($id: String!) {
          deployment(id: $id) {
            id
            status
            url
            createdAt
          }
        }
        """
        data = await self._graphql(query, {"id": deployment_id})
        dep = data.get("deployment")
        if not dep:
            return f"Deployment {deployment_id} not found."
        lines = [
            f"Deployment {dep.get('id', deployment_id)}",
            f"Status: {dep.get('status', 'unknown')}",
        ]
        if dep.get("url"):
            lines.append(f"URL: {dep['url']}")
        return "\n".join(lines)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
