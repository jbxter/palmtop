"""Supervisor harness: spawn plans, waves, blessings, and fallbacks."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from palmtop.core.harness import (
    _PLANNER_MARKER,
    _RESEARCH_ROLE,
    _SYNTH_MARKER,
    SupervisorHarness,
    is_read_query,
    parse_spawn_plan,
)
from palmtop.core.orchestration import OrchestrationResult
from palmtop.core.sovereign_runner import run_sovereign_engine
from palmtop.inference.budget import BudgetExceededError
from palmtop.tools.base import Tool, ToolRegistry


class ScriptLLM:
    def __init__(self, planner: str, research: dict[str, str] | None = None, operator: str = "", synth: str = "done"):
        self.planner = planner
        self.research = research or {}
        self.operator = operator
        self.writer = ""
        self.deployer = ""
        self.synth = synth
        self.calls: list[str] = []
        self.synth_prompt = ""
        self._lock = threading.Lock()
        self.open_research = 0
        self.max_research = 0
        self.operator_saw_research = -1
        self._release = threading.Event()

    def generate(self, task: str, alignment: dict | None = None) -> str:
        with self._lock:
            self.calls.append(task)
        if _PLANNER_MARKER in task:
            return self.planner
        if _SYNTH_MARKER in task:
            self.synth_prompt = task
            return self.synth
        if _RESEARCH_ROLE in task:
            with self._lock:
                self.open_research += 1
                self.max_research = max(self.max_research, self.open_research)
                if self.open_research >= 2:
                    self._release.set()
            if "SLOW" in task:
                time.sleep(0.4)
            elif len(self.research) >= 2:
                self._release.wait(timeout=2)
            with self._lock:
                self.open_research -= 1
            for key, value in self.research.items():
                if key in task:
                    return value
            return "research notes"
        if "Role: operator" in task:
            with self._lock:
                self.operator_saw_research = self.open_research
            return self.operator
        if "Role: writer" in task:
            return self.writer or "direct answer"
        if "Role: deployer" in task:
            return self.deployer or "direct answer"
        return "direct answer"


class Allow:
    def __init__(self, bad: str = "evil"):
        self.bad = bad

    def check_alignment(self, task: str, *, heuristic_only: bool = False) -> dict:
        ok = self.bad not in (task or "")
        return {"is_aligned": ok, "load_status": "ok", "score": 1.0 if ok else 0.0}


class Gate:
    def __init__(self, approved: bool = True, pending: bool = False):
        self._approved = approved
        self.is_pending = pending
        self.prepared = ""
        self.wait_calls = 0

    def prepare(self, summary: str) -> None:
        self.prepared = summary
        self.is_pending = True

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_calls += 1
        self.is_pending = False
        return self._approved


class Cursor:
    def __init__(self, max_concurrent: int = 1):
        self.calls: list[dict] = []
        self.active_count = 0
        self._cfg = type("Cfg", (), {"max_concurrent": max_concurrent})()

    async def launch(self, query: str, **kwargs):
        self.calls.append({"query": query, **kwargs})
        self.active_count += 1
        return "Cursor cloud agent started"


class RecordingTool(Tool):
    def __init__(self, name: str, result: str = "ok"):
        self.name = name
        self.description = name
        self.queries: list[str] = []
        self.user_id = ""
        self._result = result

    def set_user_id(self, user_id: str) -> None:
        self.user_id = user_id

    async def run(self, query: str) -> str:
        self.queries.append(query)
        return self._result


def _plan(*children: dict) -> str:
    return json.dumps({"children": list(children)})


def _harness(llm: ScriptLLM, **kwargs) -> SupervisorHarness:
    return SupervisorHarness(llm, aligner=kwargs.pop("aligner", Allow()), **kwargs)


async def _send(box: list[str]):
    async def send(user_id: str, text: str) -> None:
        box.append(text)

    return send


def test_is_read_query() -> None:
    assert is_read_query("search open bugs")
    assert is_read_query("my issues")
    assert is_read_query("get PROJ-1")
    assert not is_read_query("create PROJ | title | body")
    assert not is_read_query("comment PROJ-1 | hi")


def test_parse_spawn_plan_rules() -> None:
    assert parse_spawn_plan("not json") is None
    assert parse_spawn_plan(_plan({"id": "r1", "role": "research", "task": "only"})) is None
    plan = parse_spawn_plan(
        _plan(
            {"id": "r1", "role": "research", "task": "one"},
            {"id": "s", "role": "supervisor", "task": "nest"},
            {"id": "c1", "role": "coder", "task": "ship it"},
        )
    )
    assert plan is not None
    assert [c.role for c in plan.children] == ["research", "coder"]
    wide = parse_spawn_plan(
        _plan(*[{"id": f"r{i}", "role": "research", "task": f"part {i}"} for i in range(6)]),
        max_children=4,
    )
    assert wide is not None
    assert len(wide.children) == 4


async def test_bad_planner_output_falls_back() -> None:
    llm = ScriptLLM("I will just answer in prose")
    reply = await _harness(llm).run("ship the milestone", {"is_aligned": True})
    assert reply == "direct answer"
    assert len(llm.calls) == 2
    assert _RESEARCH_ROLE not in llm.calls[1]


async def test_single_research_step_falls_back() -> None:
    llm = ScriptLLM(_plan({"id": "r1", "role": "research", "task": "only look"}))
    reply = await _harness(llm).run("ship the milestone", {"is_aligned": True})
    assert reply == "direct answer"
    assert not any(_RESEARCH_ROLE in call for call in llm.calls)


async def test_research_overlaps_and_operator_waits() -> None:
    llm = ScriptLLM(
        _plan(
            {"id": "a", "role": "research", "task": "alpha research"},
            {"id": "b", "role": "research", "task": "beta research"},
            {"id": "o", "role": "operator", "task": "look up the calendar"},
        ),
        research={"alpha": "alpha notes", "beta": "beta notes"},
        operator="[TOOL:search] palmtop",
        synth="combined",
    )
    search = RecordingTool("search", "hits")
    registry = ToolRegistry()
    registry.register(search)
    gate = Gate(approved=True)
    messages: list[str] = []
    reply = await _harness(llm, tools=registry, blessing_gate=gate).run(
        "ship the milestone",
        {"is_aligned": True},
        user_id="owner-1",
        send_fn=await _send(messages),
    )
    assert llm.max_research >= 2
    assert llm.operator_saw_research == 0
    assert search.queries == ["palmtop"]
    assert search.user_id == "owner-1"
    assert reply == "combined"
    assert any(msg.startswith("Running ") and "research" in msg for msg in messages)
    assert any(msg.startswith("Running ") and "operator" in msg for msg in messages)
    assert "alpha notes" in llm.synth_prompt or "beta notes" in llm.synth_prompt


async def test_operator_refuses_writes_and_blocked_tools() -> None:
    llm = ScriptLLM(
        _plan({"id": "o", "role": "operator", "task": "look up jira"}),
        operator=(
            "[TOOL:email] send hi\n"
            "[TOOL:cursor] do the thing\n"
            "[TOOL:jira] create PROJ | title | body\n"
            "[TOOL:jira] search open bugs\n"
            "[TOOL:search] latest news\n"
        ),
        synth="from tools",
    )
    email = RecordingTool("email")
    cursor_tool = RecordingTool("cursor")
    jira = RecordingTool("jira", "PROJ-1")
    search = RecordingTool("search", "news")
    registry = ToolRegistry()
    for tool in (email, cursor_tool, jira, search):
        registry.register(tool)
    reply = await _harness(llm, tools=registry, blessing_gate=Gate(True)).run(
        "ship the milestone",
        {"is_aligned": True},
        user_id="owner-1",
        send_fn=await _send([]),
    )
    assert email.queries == []
    assert cursor_tool.queries == []
    assert jira.queries == ["search open bugs"]
    assert search.queries == ["latest news"]
    assert "refused: email" in llm.synth_prompt
    assert "refused: jira create" in llm.synth_prompt
    assert reply == "from tools"


async def test_misaligned_child_is_dropped() -> None:
    llm = ScriptLLM(
        _plan(
            {"id": "ok1", "role": "research", "task": "ship the milestone"},
            {"id": "bad", "role": "research", "task": "evil detour"},
        ),
        research={"ship the milestone": "good notes"},
        synth="kept the good one",
    )
    reply = await _harness(llm, aligner=Allow("evil")).run("ship the milestone", {"is_aligned": True})
    assert "Skipped (not aligned): bad" in reply
    assert not any("evil detour" in call for call in llm.calls)
    assert any(_RESEARCH_ROLE in call for call in llm.calls)


async def test_busy_gate_skips_side_effects() -> None:
    cursor = Cursor()
    llm = ScriptLLM(
        _plan(
            {"id": "r1", "role": "research", "task": "ship the milestone"},
            {"id": "c1", "role": "coder", "task": "ship the milestone in code"},
        ),
        research={"ship the milestone": "notes"},
        synth="research only",
    )
    gate = Gate(approved=True, pending=True)
    reply = await _harness(llm, blessing_gate=gate, cursor=cursor).run(
        "ship the milestone",
        {"is_aligned": True},
        send_fn=await _send([]),
    )
    assert cursor.calls == []
    assert gate.wait_calls == 0
    assert gate.prepared == ""
    assert "already pending" in reply
    assert "c1" in reply
    assert reply.startswith("research only")


async def test_denied_plan_runs_research_and_skips_coder() -> None:
    cursor = Cursor()
    llm = ScriptLLM(
        _plan(
            {"id": "r1", "role": "research", "task": "ship the milestone"},
            {"id": "c1", "role": "coder", "task": "ship the milestone in code"},
        ),
        research={"ship the milestone": "notes"},
        synth="research only",
    )
    reply = await _harness(llm, blessing_gate=Gate(False), cursor=cursor).run(
        "ship the milestone",
        {"is_aligned": True},
        send_fn=await _send([]),
    )
    assert cursor.calls == []
    assert "approval denied" in reply
    assert "notes" in llm.synth_prompt


async def test_approved_coder_launches_once() -> None:
    cursor = Cursor()
    llm = ScriptLLM(
        _plan(
            {"id": "r1", "role": "research", "task": "ship the milestone"},
            {"id": "c1", "role": "coder", "task": "repo=https://github.com/org/repo fix the readme"},
        ),
        research={"ship the milestone": "notes"},
        synth="launched",
    )
    reply = await _harness(llm, blessing_gate=Gate(True), cursor=cursor).run(
        "ship the milestone",
        {"is_aligned": True},
        user_id="owner-1",
        send_fn=await _send([]),
    )
    assert reply == "launched"
    assert len(cursor.calls) == 1
    assert cursor.calls[0]["approval_granted"] is True
    assert cursor.calls[0]["query"].startswith("repo=https://github.com/org/repo")
    assert "launch receipt" in llm.synth_prompt or "Cursor cloud agent started" in llm.synth_prompt


async def test_child_spawn_plan_is_not_executed() -> None:
    nested = _plan({"id": "x", "role": "coder", "task": "hack the repo"})
    cursor = Cursor()
    llm = ScriptLLM(
        _plan(
            {"id": "r1", "role": "research", "task": "ship the milestone"},
            {"id": "r2", "role": "research", "task": "review the milestone"},
        ),
        research={"ship the milestone": nested, "review the milestone": "plain"},
        synth="no nest",
    )
    reply = await _harness(llm, cursor=cursor).run("ship the milestone", {"is_aligned": True})
    assert reply == "no nest"
    assert cursor.calls == []
    assert sum(1 for call in llm.calls if _PLANNER_MARKER in call) == 1
    assert sum(1 for call in llm.calls if _RESEARCH_ROLE in call) == 2


async def test_denied_side_effects_without_research_fall_back() -> None:
    cursor = Cursor()
    llm = ScriptLLM(_plan({"id": "c1", "role": "coder", "task": "ship the milestone in code"}))
    reply = await _harness(llm, blessing_gate=Gate(False), cursor=cursor).run(
        "ship the milestone",
        {"is_aligned": True},
        send_fn=await _send([]),
    )
    assert cursor.calls == []
    assert "direct answer" in reply
    assert "approval denied" in reply
    assert not any(_SYNTH_MARKER in call for call in llm.calls)


async def test_child_timeout_still_synthesizes() -> None:
    llm = ScriptLLM(
        _plan(
            {"id": "slow", "role": "research", "task": "SLOW ship the milestone"},
            {"id": "fast", "role": "research", "task": "fast ship the milestone"},
        ),
        research={"fast ship": "fast notes"},
        synth="partial",
    )
    reply = await _harness(llm, cloud_timeout_s=0.05, deadline_s=5).run(
        "ship the milestone",
        {"is_aligned": True},
    )
    assert reply == "partial"
    assert 'status="timeout"' in llm.synth_prompt
    assert "fast notes" in llm.synth_prompt


async def test_tool_output_is_truncated_and_fenced(tmp_path: Path) -> None:
    blob = "Z" * 5_000
    llm = ScriptLLM(
        _plan({"id": "o", "role": "operator", "task": "look up the web"}),
        operator="[TOOL:search] palmtop",
        synth="short",
    )
    search = RecordingTool("search", blob)
    registry = ToolRegistry()
    registry.register(search)
    await _harness(llm, tools=registry, blessing_gate=Gate(True), data_dir=tmp_path).run(
        "ship the milestone",
        {"is_aligned": True},
        user_id="owner-1",
        send_fn=await _send([]),
    )
    assert "<untrusted-child" in llm.synth_prompt
    assert "</untrusted-child>" in llm.synth_prompt
    assert "Z" * 3_000 not in llm.synth_prompt
    audit = (tmp_path / "engine_runs.jsonl").read_text(encoding="utf-8")
    assert "Z" * 3_000 not in audit
    record = json.loads(audit.strip().splitlines()[-1])
    assert record["child_id"] == "o"
    assert record["role"] == "operator"
    assert len(record["output"]) <= 2_000


async def test_budget_error_does_not_fail_the_turn() -> None:
    class BudgetLLM(ScriptLLM):
        def generate(self, task: str, alignment: dict | None = None) -> str:
            if _RESEARCH_ROLE in task and "beta" in task:
                raise BudgetExceededError("cap")
            return super().generate(task, alignment)

    llm = BudgetLLM(
        _plan(
            {"id": "a", "role": "research", "task": "alpha research"},
            {"id": "b", "role": "research", "task": "beta research"},
        ),
        research={"alpha": "alpha notes"},
        synth="still here",
    )
    reply = await _harness(llm).run("ship the milestone", {"is_aligned": True})
    assert reply == "still here"
    assert 'status="budget"' in llm.synth_prompt
    assert "alpha notes" in llm.synth_prompt


async def test_channel_path_uses_align_only(tmp_path: Path) -> None:
    llm = ScriptLLM("not a plan")

    class Sov:
        def __init__(self):
            self.harness = _harness(llm)
            self.orchestrated = False

        def align_only(self, task: str) -> OrchestrationResult:
            return OrchestrationResult(status="ready", alignment={"is_aligned": True, "score": 1.0})

        def orchestrate_result(self, task: str) -> OrchestrationResult:
            self.orchestrated = True
            raise AssertionError("harness path must not execute inside orchestrate_result")

    sov = Sov()
    reply = await run_sovereign_engine(sov, "ship the milestone", data_dir=tmp_path, user_id="u1")
    assert reply == "direct answer"
    assert sov.orchestrated is False


async def test_approved_writer_runs_only_allowed_sends() -> None:
    email = RecordingTool("email", "sent")
    registry = ToolRegistry()
    registry.register(email)
    llm = ScriptLLM(_plan({"id": "w", "role": "writer", "task": "email the status"}))
    llm.writer = "[TOOL:email] send bob@example.com | hello\n[TOOL:email] search inbox\n"
    gate = Gate(True)
    reply = await _harness(llm, writer_tools=registry, blessing_gate=gate).run(
        "ship the milestone",
        {"is_aligned": True},
        user_id="owner-1",
        send_fn=await _send([]),
    )
    assert email.queries == ["send bob@example.com | hello"]
    assert email.user_id == "owner-1"
    assert "send bob@example.com | hello" in gate.prepared
    assert reply == "done"


async def test_writer_refuses_remind_list_and_confluence() -> None:
    remind = RecordingTool("remind", "set")
    confluence = RecordingTool("confluence", "page")
    registry = ToolRegistry()
    registry.register(remind)
    registry.register(confluence)
    llm = ScriptLLM(_plan({"id": "w", "role": "writer", "task": "remind the owner"}))
    llm.writer = "[TOOL:remind] list\n[TOOL:confluence] create a page\n[TOOL:remind] 30m follow up\n"
    await _harness(llm, writer_tools=registry, blessing_gate=Gate(True)).run(
        "ship the milestone",
        {"is_aligned": True},
        send_fn=await _send([]),
    )
    assert remind.queries == ["30m follow up"]
    assert confluence.queries == []


async def test_misaligned_writer_call_is_dropped() -> None:
    email = RecordingTool("email", "sent")
    registry = ToolRegistry()
    registry.register(email)
    llm = ScriptLLM(_plan({"id": "w", "role": "writer", "task": "email the status"}))
    llm.writer = "[TOOL:email] send evil@example.com | hello\n"
    gate = Gate(True)
    reply = await _harness(llm, writer_tools=registry, aligner=Allow("evil"), blessing_gate=gate).run(
        "ship the milestone",
        {"is_aligned": True},
        send_fn=await _send([]),
    )
    assert email.queries == []
    assert gate.wait_calls == 0
    assert "Skipped (not aligned): w" in reply


async def test_denied_or_pending_gate_skips_writer_and_deployer() -> None:
    email = RecordingTool("email", "sent")
    vercel = RecordingTool("vercel", "deployed")
    writers = ToolRegistry()
    writers.register(email)
    deploys = ToolRegistry()
    deploys.register(vercel)
    llm = ScriptLLM(
        _plan(
            {"id": "w", "role": "writer", "task": "email the status"},
            {"id": "d", "role": "deployer", "task": "ship the milestone"},
        )
    )
    llm.writer = "[TOOL:email] send bob@example.com | hello"
    llm.deployer = "[TOOL:vercel] deploy main"

    denied = await _harness(llm, writer_tools=writers, deploy_tools=deploys, blessing_gate=Gate(False)).run(
        "ship the milestone",
        {"is_aligned": True},
        send_fn=await _send([]),
    )
    assert email.queries == []
    assert vercel.queries == []
    assert "approval denied" in denied

    email.queries.clear()
    busy_gate = Gate(True, pending=True)
    busy = await _harness(llm, writer_tools=writers, deploy_tools=deploys, blessing_gate=busy_gate).run(
        "ship the milestone",
        {"is_aligned": True},
        send_fn=await _send([]),
    )
    assert email.queries == []
    assert vercel.queries == []
    assert busy_gate.wait_calls == 0
    assert "already pending" in busy


async def test_unknown_deploy_provider_is_refused() -> None:
    vercel = RecordingTool("vercel", "deployed")
    deploys = ToolRegistry()
    deploys.register(vercel)
    llm = ScriptLLM(_plan({"id": "d", "role": "deployer", "task": "ship the milestone"}))
    llm.deployer = "[TOOL:fly] deploy"
    gate = Gate(True)
    reply = await _harness(llm, deploy_tools=deploys, blessing_gate=gate).run(
        "ship the milestone",
        {"is_aligned": True},
        send_fn=await _send([]),
    )
    assert vercel.queries == []
    assert gate.wait_calls == 0
    assert "deployed" not in reply


async def test_approved_vercel_deploy_uses_config_and_skips_second_blessing() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from palmtop.config.settings import VercelConfig
    from palmtop.tools.vercel import VercelDeployTool

    cfg = VercelConfig(
        enabled=True,
        api_token="test-token",
        default_project_id="prj_test",
        default_project_name="my-app",
        default_target="production",
        default_branch="main",
        require_blessing=True,
    )
    tool = VercelDeployTool(cfg, blessing_gate=MagicMock())
    tool.set_notify(AsyncMock())
    deploys = ToolRegistry()
    deploys.register(tool)
    llm = ScriptLLM(
        _plan(
            {"id": "d1", "role": "deployer", "task": "ship the milestone"},
            {"id": "d2", "role": "deployer", "task": "ship the milestone again"},
        )
    )
    llm.deployer = "[TOOL:vercel] deploy feature-x"
    gate = Gate(True)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "dpl_1", "url": "my-app.vercel.app", "readyState": "QUEUED"}

    with (
        patch("palmtop.tools.vercel.request_deploy_blessing", new_callable=AsyncMock) as bless,
        patch.object(tool, "_get_client") as get_client,
    ):
        client = AsyncMock()
        client.post = AsyncMock(return_value=mock_resp)
        get_client.return_value = client
        reply = await _harness(llm, deploy_tools=deploys, blessing_gate=gate).run(
            "ship the milestone",
            {"is_aligned": True},
            send_fn=await _send([]),
        )

    bless.assert_not_called()
    client.post.assert_called_once()
    assert client.post.call_args.kwargs["json"]["project"] == "prj_test"
    assert "prj_test" in gate.prepared
    assert "Target: production" in gate.prepared
    assert "not an unmerged pull request" in gate.prepared
    assert "dpl_1" in reply or reply == "done"


async def test_chat_deploy_still_asks_for_approval() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from palmtop.config.settings import VercelConfig
    from palmtop.tools.deploy_blessing import plan_approval
    from palmtop.tools.vercel import VercelDeployTool

    cfg = VercelConfig(
        enabled=True,
        api_token="test-token",
        default_project_id="prj_test",
        default_target="production",
        require_blessing=True,
    )
    tool = VercelDeployTool(cfg, blessing_gate=MagicMock())
    tool.set_notify(AsyncMock())
    with patch(
        "palmtop.tools.vercel.request_deploy_blessing",
        new_callable=AsyncMock,
        return_value=False,
    ) as bless:
        reply = await tool.run("deploy")
    bless.assert_awaited()
    assert "denied" in reply.lower()
    assert plan_approval.get() is False
