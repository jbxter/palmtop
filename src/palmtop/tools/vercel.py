"""Vercel deployments via REST API (VERCEL_TOKEN)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable

import httpx

from palmtop.tools.base import Tool
from palmtop.tools.deploy_blessing import request_deploy_blessing

if TYPE_CHECKING:
    from palmtop.config.settings import VercelConfig
    from palmtop.core.blessing import BlessingGate

log = logging.getLogger(__name__)

_PHONE_LIMITS = httpx.Limits(max_connections=5, max_keepalive_connections=2)


class VercelDeployTool(Tool):
    name = "vercel"
    description = (
        "Deploy or inspect Vercel projects. Usage:\n"
        "  [TOOL:vercel] status\n"
        "  [TOOL:vercel] projects\n"
        "  [TOOL:vercel] deploy [branch]\n"
        "  [TOOL:vercel] get <deployment_id>"
    )

    def __init__(
        self,
        cfg: "VercelConfig",
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
                base_url="https://api.vercel.com",
                timeout=30.0,
                limits=_PHONE_LIMITS,
                headers={
                    "Authorization": f"Bearer {self._cfg.api_token}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    def _team_params(self) -> dict[str, str]:
        if self._cfg.team_id:
            return {"teamId": self._cfg.team_id}
        return {}

    async def verify_auth(self) -> str | None:
        try:
            client = self._get_client()
            resp = await client.get("/v2/user", params=self._team_params())
            if resp.status_code == 200:
                user = resp.json().get("user") or resp.json()
                username = user.get("username") or user.get("email") or "ok"
                log.info("Vercel auth verified ✓ (%s)", username)
                return None
            if resp.status_code == 401:
                return "Vercel auth failed (401) — check VERCEL_TOKEN"
            return f"Vercel auth check returned {resp.status_code}: {resp.text[:200]}"
        except httpx.ConnectError:
            return "Can't reach Vercel API — check network"
        except Exception as e:
            return f"Vercel auth check failed: {e}"

    async def run(self, query: str) -> str:
        parts = query.strip().split(None, 1)
        if not parts:
            return "Usage: status | projects | deploy [branch] | get <deployment_id>"

        action = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        try:
            if action == "status":
                return await self._status()
            if action == "projects":
                return await self._projects()
            if action == "deploy":
                return await self._deploy(rest or self._cfg.default_branch)
            if action == "get":
                return await self._get_deployment(rest)
            return f"Unknown action '{action}'. Usage: status | projects | deploy [branch] | get <id>"
        except Exception as e:
            log.exception("Vercel operation failed")
            return f"Vercel error: {e}"

    async def _status(self) -> str:
        err = await self.verify_auth()
        if err:
            return err
        lines = ["Vercel connected."]
        if self._cfg.default_project_id:
            lines.append(f"Default project: {self._cfg.default_project_id}")
        elif self._cfg.default_project_name:
            lines.append(f"Default project name: {self._cfg.default_project_name}")
        else:
            lines.append("Set default_project_id or default_project_name in [vercel] config.")
        if self._cfg.default_branch:
            lines.append(f"Default branch: {self._cfg.default_branch}")
        return "\n".join(lines)

    async def _projects(self) -> str:
        client = self._get_client()
        resp = await client.get(
            "/v9/projects",
            params={**self._team_params(), "limit": "10"},
        )
        if resp.status_code != 200:
            return f"Vercel list projects failed ({resp.status_code}): {resp.text[:200]}"

        projects = resp.json().get("projects", [])
        if not projects:
            return "No Vercel projects found for this token."

        lines = ["Vercel projects:"]
        for p in projects:
            pid = p.get("id", "")
            name = p.get("name", "")
            lines.append(f"  {name} ({pid})")
        return "\n".join(lines)

    async def _deploy(self, branch: str) -> str:
        project_id = self._cfg.default_project_id
        project_name = self._cfg.default_project_name
        if not project_id and not project_name:
            return (
                "No default Vercel project configured. "
                "Set [vercel] default_project_id or default_project_name in config.toml."
            )

        summary = (
            f"Target: {self._cfg.default_target}\n"
            f"Project: {project_id or project_name}\n"
            f"Branch: {branch or self._cfg.default_branch}"
        )
        if self._cfg.require_blessing:
            approved = await request_deploy_blessing(
                self._blessing_gate,
                self._send_fn,
                self._user_id,
                platform="Vercel",
                summary=summary,
            )
            if not approved:
                return "Vercel deploy denied — not started."

        body: dict = {
            "target": self._cfg.default_target,
            "withLatestCommit": True,
        }
        if project_id:
            body["project"] = project_id
            body["name"] = project_name or project_id
        else:
            body["name"] = project_name

        if branch:
            body["gitSource"] = {
                "type": "github",
                "ref": branch,
            }

        client = self._get_client()
        resp = await client.post(
            "/v13/deployments",
            params=self._team_params(),
            json=body,
        )
        if resp.status_code not in (200, 201):
            return f"Vercel deploy failed ({resp.status_code}): {resp.text[:300]}"

        data = resp.json()
        dep_id = data.get("id", "")
        url = data.get("url") or data.get("alias") or ""
        status = data.get("readyState") or data.get("status") or "queued"
        lines = [f"Vercel deployment created: {dep_id}", f"Status: {status}"]
        if url:
            lines.append(f"URL: https://{url}" if "://" not in str(url) else f"URL: {url}")
        inspector = data.get("inspectorUrl")
        if inspector:
            lines.append(f"Inspector: {inspector}")
        return "\n".join(lines)

    async def _get_deployment(self, deployment_id: str) -> str:
        if not deployment_id:
            return "Need a deployment id. Usage: get <deployment_id>"

        client = self._get_client()
        resp = await client.get(
            f"/v13/deployments/{deployment_id}",
            params=self._team_params(),
        )
        if resp.status_code == 404:
            return f"Deployment {deployment_id} not found."
        if resp.status_code != 200:
            return f"Vercel get failed ({resp.status_code}): {resp.text[:200]}"

        data = resp.json()
        dep_id = data.get("id", deployment_id)
        status = data.get("readyState") or data.get("status") or "unknown"
        url = data.get("url") or ""
        lines = [f"Deployment {dep_id}", f"Status: {status}"]
        if url:
            lines.append(f"URL: https://{url}" if "://" not in str(url) else f"URL: {url}")
        return "\n".join(lines)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
