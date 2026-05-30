"""Launch and poll Cursor Cloud Agents from the agent."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from palmtop.core.blessing import assess_risk, build_approval_summary
from palmtop.cursor.client import TERMINAL_RUN_STATUSES, CursorAgentsClient, CursorAPIError

if TYPE_CHECKING:
    from palmtop.config.settings import CursorConfig
    from palmtop.core.blessing import BlessingGate

log = logging.getLogger(__name__)

CURSOR_TRIGGERS = ("/cursor ", "cursor:")


def parse_cursor_task(text: str) -> str | None:
    """Return task body if message triggers Cursor delegate, else None."""
    raw = text.strip()
    if not raw:
        return None
    lower = raw.lower()
    if lower.startswith("/cursor "):
        return raw[8:].strip()
    if lower.startswith("cursor:"):
        return raw[7:].strip()
    return None


_REPO_RE = re.compile(r"^repo=(\S+)(?:\s+branch=(\S+))?\s+", re.I)
_BRANCH_RE = re.compile(r"^branch=(\S+)\s+", re.I)

# Conservative git ref validation — the branch flows to the Cursor API as
# startingRef, so reject malformed/injection-y refs before they leave.
_REF_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,254})$")


def _valid_ref(ref: str) -> bool:
    """True if `ref` is a safe-looking git branch/tag/SHA."""
    if not ref or ref != ref.strip():
        return False
    if ".." in ref or "@{" in ref or ref.endswith("/") or ref.endswith(".lock"):
        return False
    return bool(_REF_RE.match(ref))


def parse_cursor_query(query: str, cfg: CursorConfig) -> tuple[str, str, str]:
    """Parse tool/command query into (repo_url, branch, prompt)."""
    q = query.strip()
    repo = cfg.default_repo
    branch = cfg.default_branch

    m = _REPO_RE.match(q)
    if m:
        repo = m.group(1)
        if m.group(2):
            branch = m.group(2)
        q = q[m.end() :].strip()
    else:
        bm = _BRANCH_RE.match(q)
        if bm:
            branch = bm.group(1)
            q = q[bm.end() :].strip()

    return repo, branch, q


def normalize_repo_url(url: str) -> str:
    return url.rstrip("/")


def repo_allowed(url: str, allowed: list[str]) -> bool:
    if not allowed:
        return False
    norm = normalize_repo_url(url)
    for entry in allowed:
        if normalize_repo_url(entry) == norm:
            return True
    return False


def append_cursor_audit(data_dir: Path, record: dict) -> None:
    path = data_dir / "cursor_jobs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def extract_pr_url(run: dict) -> str | None:
    git = run.get("git") or {}
    for branch in git.get("branches") or []:
        pr = branch.get("prUrl")
        if pr:
            return pr
    return None


def format_launch_reply(
    *,
    agent_id: str,
    run_id: str,
    agent_url: str | None,
    repo_url: str,
    branch: str,
    prompt: str,
) -> str:
    preview = prompt[:120] + ("..." if len(prompt) > 120 else "")
    lines = [
        "Cursor cloud agent started",
        f"Agent: {agent_id}",
        f"Run: {run_id}",
        f"Repo: {repo_url} @ {branch}",
        f"Task: {preview}",
    ]
    if agent_url:
        lines.append(f"Dashboard: {agent_url}")
    lines.append("")
    lines.append("I'll message you when the run finishes.")
    return "\n".join(lines)


def format_completion_message(
    *,
    agent_id: str,
    run_id: str,
    status: str,
    result: str | None,
    pr_url: str | None,
    agent_url: str | None,
    duration_ms: int | None,
) -> str:
    lines = ["Cursor cloud agent finished", f"Status: {status}"]
    if agent_url:
        lines.append(f"Agent: {agent_url}")
    else:
        lines.append(f"Agent: {agent_id}")
    if pr_url:
        lines.append(f"PR: {pr_url}")
    if duration_ms is not None:
        lines.append(f"Duration: {duration_ms / 1000:.1f}s")
    if result:
        lines.append("")
        summary = result.strip()
        if len(summary) > 1500:
            summary = summary[:1500] + "..."
        lines.append(summary)
    return "\n".join(lines)


@dataclass
class PendingCursorJob:
    agent_id: str
    run_id: str
    user_id: str
    repo_url: str
    branch: str
    prompt: str
    agent_url: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class CursorJobManager:
    """Tracks in-flight cloud runs and polls until terminal."""

    def __init__(
        self,
        client: CursorAgentsClient,
        cfg: CursorConfig,
        data_dir: Path,
        *,
        blessing_gate: BlessingGate | None = None,
        send_fn: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._cfg = cfg
        self._data_dir = data_dir
        self._blessing_gate = blessing_gate
        self._send_fn = send_fn
        self._pending: dict[str, PendingCursorJob] = {}
        self._poll_task: asyncio.Task | None = None

    @property
    def active_count(self) -> int:
        return len(self._pending)

    def set_notify(self, send_fn: Callable[[str, str], Awaitable[None]] | None) -> None:
        self._send_fn = send_fn

    async def launch(
        self,
        query: str,
        *,
        user_id: str = "default",
        alignment: dict | None = None,
    ) -> str:
        if not self._cfg.enabled:
            return "Cursor bridge is disabled. Set [cursor] enabled = true in config.toml."

        repo_url, branch, prompt = parse_cursor_query(query, self._cfg)
        if not prompt:
            return "Usage: /cursor <prompt>  or  cursor: <prompt>  (optional: repo=<url> branch=<ref>)"
        if not repo_url:
            return "No repository configured. Set [cursor] default_repo or pass repo=<url>."
        if not repo_allowed(repo_url, self._cfg.allowed_repos):
            return f"Repository not allowed: {repo_url}"

        # Validate the ref (repo is allow-listed, but the branch wasn't checked).
        if not _valid_ref(branch):
            return f"Invalid branch/ref: {branch!r}"
        # An unreviewed ref of an allowed repo can still carry malicious code, so
        # only the default branch may run autonomously; other refs need approval.
        if branch != self._cfg.default_branch and not self._cfg.require_blessing:
            return (
                f"Refused — autonomous runs (require_blessing=false) are limited to the default "
                f"branch '{self._cfg.default_branch}'; requested '{branch}'. Enable require_blessing "
                f"to run other refs behind /approve."
            )

        if self.active_count >= self._cfg.max_concurrent:
            return (
                f"Too many Cursor jobs running ({self.active_count}/{self._cfg.max_concurrent}). "
                "Wait for one to finish."
            )

        if self._cfg.require_blessing:
            # Fail closed: if approval is required but we have no way to ask
            # (gate or notify channel missing), refuse — never launch unapproved.
            if not self._blessing_gate or not self._send_fn:
                log.error(
                    "Cursor requires approval but no approval channel is available "
                    "(gate=%s, send_fn=%s) — refusing to launch",
                    bool(self._blessing_gate),
                    bool(self._send_fn),
                )
                append_cursor_audit(
                    self._data_dir,
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "user_id": user_id,
                        "status": "refused_no_approval_channel",
                        "repo": repo_url,
                        "branch": branch,
                        "prompt": prompt[:500],
                    },
                )
                return "Cursor job refused — approval is required but no approval channel is configured."
            risk = assess_risk(prompt)
            align = alignment or {"is_aligned": True, "score": 1.0, "matched_tags": []}
            summary = (
                "Cursor cloud agent approval\n"
                + build_approval_summary(prompt, align, risk)
                + f"\nRepo: {repo_url} @ {branch}"
            )
            approved = await self._request_blessing(user_id, summary)
            if not approved:
                append_cursor_audit(
                    self._data_dir,
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "user_id": user_id,
                        "status": "denied",
                        "repo": repo_url,
                        "branch": branch,
                        "prompt": prompt[:500],
                    },
                )
                return "Cursor job denied — not launched."

        try:
            created = await self._client.create_agent(
                prompt,
                repo_url=repo_url,
                starting_ref=branch,
                auto_create_pr=self._cfg.auto_create_pr,
            )
        except CursorAPIError as e:
            log.warning("Cursor create failed: %s", e)
            append_cursor_audit(
                self._data_dir,
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "user_id": user_id,
                    "status": "error",
                    "error": str(e),
                    "repo": repo_url,
                    "prompt": prompt[:500],
                },
            )
            return f"Cursor API error: {e}"

        agent = created.get("agent") or {}
        run = created.get("run") or {}
        agent_id = agent.get("id") or ""
        run_id = run.get("id") or ""
        if not agent_id or not run_id:
            return "Cursor API returned an unexpected response (missing agent/run id)."

        job = PendingCursorJob(
            agent_id=agent_id,
            run_id=run_id,
            user_id=user_id,
            repo_url=repo_url,
            branch=branch,
            prompt=prompt,
            agent_url=agent.get("url"),
        )
        self._pending[run_id] = job
        self._ensure_poller()

        append_cursor_audit(
            self._data_dir,
            {
                "ts": job.started_at,
                "user_id": user_id,
                "status": "launched",
                "agent_id": agent_id,
                "run_id": run_id,
                "repo": repo_url,
                "branch": branch,
                "prompt": prompt[:500],
                "agent_url": agent.get("url"),
            },
        )

        return format_launch_reply(
            agent_id=agent_id,
            run_id=run_id,
            agent_url=agent.get("url"),
            repo_url=repo_url,
            branch=branch,
            prompt=prompt,
        )

    async def _request_blessing(self, user_id: str, summary: str) -> bool:
        gate = self._blessing_gate
        send_fn = self._send_fn
        if not gate or not send_fn:
            # Defensive: the caller already guarantees both are present when
            # blessing is required. If we ever get here, fail closed (deny).
            return False

        # Prepare the gate FIRST so is_pending is True before the user
        # can reply.  This fixes the race where /approve arrived before
        # asyncio.to_thread had started the blocking wait.
        gate.prepare(summary)

        msg = f"\U0001f512 **Cursor approval needed**\n\n{summary}\n\nReply /approve or /deny"
        await send_fn(user_id, msg)

        # Now block in a worker thread — the gate is already armed.
        return await asyncio.to_thread(gate.wait)

    def _ensure_poller(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        try:
            while self._pending:
                await self._poll_once()
                await asyncio.sleep(self._cfg.poll_interval_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Cursor poll loop crashed")

    async def _poll_once(self) -> None:
        now = datetime.now(UTC)
        timeout = self._cfg.timeout_s
        done_ids: list[str] = []

        for run_id, job in list(self._pending.items()):
            started = datetime.fromisoformat(job.started_at.replace("Z", "+00:00"))
            elapsed = (now - started).total_seconds()
            if elapsed > timeout:
                await self._finish_job(
                    run_id,
                    job,
                    status="EXPIRED",
                    result="Polling timed out locally before terminal status.",
                    duration_ms=None,
                )
                done_ids.append(run_id)
                continue

            try:
                run = await self._client.get_run(job.agent_id, run_id)
            except CursorAPIError as e:
                log.warning("Cursor poll failed for %s: %s", run_id, e)
                continue

            status = run.get("status", "")
            if status not in TERMINAL_RUN_STATUSES:
                continue

            await self._finish_job(
                run_id,
                job,
                status=status,
                result=run.get("result"),
                duration_ms=run.get("durationMs"),
                pr_url=extract_pr_url(run),
            )
            done_ids.append(run_id)

        for run_id in done_ids:
            self._pending.pop(run_id, None)

    async def _finish_job(
        self,
        run_id: str,
        job: PendingCursorJob,
        *,
        status: str,
        result: str | None,
        duration_ms: int | None,
        pr_url: str | None = None,
    ) -> None:
        append_cursor_audit(
            self._data_dir,
            {
                "ts": datetime.now(UTC).isoformat(),
                "user_id": job.user_id,
                "status": status.lower(),
                "agent_id": job.agent_id,
                "run_id": run_id,
                "repo": job.repo_url,
                "pr_url": pr_url,
                "duration_ms": duration_ms,
                "result_preview": (result or "")[:500],
            },
        )

        if not self._send_fn:
            return

        msg = format_completion_message(
            agent_id=job.agent_id,
            run_id=run_id,
            status=status,
            result=result,
            pr_url=pr_url,
            agent_url=job.agent_url,
            duration_ms=duration_ms,
        )
        try:
            await self._send_fn(job.user_id, msg)
        except Exception as e:
            log.warning("Failed to notify user %s about Cursor job: %s", job.user_id, e)

    async def close(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        await self._client.close()
