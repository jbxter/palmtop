"""12 Week Year integration for palmtop (direct REST — no MCP package)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from palmtop.config.settings import Config, TwelveWyConfig
from palmtop.mcp import twelvewy_http as api
from palmtop.tools.base import Tool, ToolRegistry

log = logging.getLogger(__name__)

MCP_NAMES = frozenset({"12wy-reports", "12wy", "twelvewy"})

TOOL_DESCRIPTION = (
    "12 Week Year coaching and plans — coaching brief, onboarding, seasons, "
    "goals, tactics, weekly execution. Use for 12WY check-ins and plan setup."
)

SYSTEM_ADDENDUM = """\

12 Week Year (12WY):
- You have the 12wy-reports tool for the user's 12-week plan (Railway backend).
- For 12WY coaching or check-ins, emit ONLY this line (no preamble): [TOOL:12wy-reports] coaching brief
- You will receive real JSON in the next turn — then summarize it. Never say you are "pulling" or "checking" data.
- Use returned next_actions and encouragement_messages — never invent completion state.
- Plan setup order: vision → season → goals → tactics. Confirm start dates before creating.
- Point users to the web app for calendar OAuth, Model Week, and WAM."""

# Tool names match the 12wy MCP server for routing / JSON {"tool": "..."} compatibility.
TOOL_NAMES = [
    "twelvewy_get_coaching_brief",
    "twelvewy_get_onboarding_status",
    "twelvewy_create_season",
    "twelvewy_update_vision",
    "twelvewy_create_tactic",
    "twelvewy_get_context",
    "twelvewy_list_seasons",
    "twelvewy_get_season",
    "twelvewy_compare_seasons",
    "twelvewy_list_goals",
    "twelvewy_create_goal",
    "twelvewy_get_weekly_plan",
    "twelvewy_complete_plan_item",
    "twelvewy_uncomplete_plan_item",
    "twelvewy_get_vision",
]


def is_twelvewy_server(name: str) -> bool:
    return name.lower() in MCP_NAMES


def twelvewy_env(cfg: TwelveWyConfig) -> dict[str, str]:
    env: dict[str, str] = {}
    if cfg.api_base_url:
        env["TWELVEWY_API_BASE_URL"] = cfg.api_base_url.rstrip("/")
    if cfg.api_key:
        env["TWELVEWY_API_KEY"] = cfg.api_key
    return env


def _apply_config(cfg: TwelveWyConfig) -> None:
    env = twelvewy_env(cfg)
    os.environ.update(env)
    if cfg.api_base_url and cfg.api_key:
        api.configure(cfg.api_base_url, cfg.api_key)


def _json(data: dict) -> str:
    return json.dumps(data, indent=2, default=str)


def _call_tool(tool_name: str, arguments: dict) -> str:
    if tool_name == "twelvewy_get_coaching_brief":
        return _json(api.get("/api/v1/coach"))
    if tool_name == "twelvewy_get_onboarding_status":
        return _json(api.get("/api/v1/onboarding/status"))
    if tool_name == "twelvewy_get_context":
        return _json(api.get("/api/v1/me"))
    if tool_name == "twelvewy_list_seasons":
        return _json(api.get("/api/v1/seasons"))
    if tool_name == "twelvewy_get_vision":
        return _json(api.get("/api/v1/vision"))
    if tool_name == "twelvewy_get_season":
        return _json(api.get(f"/api/v1/seasons/{arguments['period_id']}"))
    if tool_name == "twelvewy_compare_seasons":
        a, b = arguments["period_a_id"], arguments["period_b_id"]
        return _json(api.get(f"/api/v1/seasons/compare?period_a={a}&period_b={b}"))
    if tool_name == "twelvewy_list_goals":
        return _json(api.get(f"/api/v1/seasons/{arguments['period_id']}/goals"))
    if tool_name == "twelvewy_get_weekly_plan":
        pid, wk = arguments["period_id"], arguments["week_number"]
        return _json(api.get(f"/api/v1/seasons/{pid}/weeks/{wk}/plan"))
    if tool_name == "twelvewy_complete_plan_item":
        return _json(api.post(f"/api/v1/plan-items/{arguments['item_id']}/complete"))
    if tool_name == "twelvewy_uncomplete_plan_item":
        return _json(api.post(f"/api/v1/plan-items/{arguments['item_id']}/uncomplete"))
    if tool_name == "twelvewy_create_season":
        body: dict = {"start_date": arguments["start_date"]}
        if arguments.get("name"):
            body["name"] = arguments["name"]
        return _json(api.post("/api/v1/seasons", body))
    if tool_name == "twelvewy_update_vision":
        body = {k: v for k, v in arguments.items() if v}
        return _json(api.put("/api/v1/vision", body))
    if tool_name == "twelvewy_create_goal":
        pid = arguments["period_id"]
        body = {"name": arguments["name"]}
        for key in ("description", "category", "target_value"):
            if arguments.get(key):
                body[key] = arguments[key]
        return _json(api.post(f"/api/v1/seasons/{pid}/goals", body))
    if tool_name == "twelvewy_create_tactic":
        gid = arguments["goal_id"]
        body = {
            "name": arguments["name"],
            "start_week": arguments.get("start_week", 1),
            "end_week": arguments.get("end_week", 12),
            "frequency": arguments.get("frequency", "weekly"),
        }
        for key in ("description", "due_date"):
            if arguments.get(key):
                body[key] = arguments[key]
        return _json(api.post(f"/api/v1/goals/{gid}/tactics", body))
    raise ValueError(f"Unknown 12WY tool: {tool_name}")


class TwelveWyGatewayTool(Tool):
    """Calls the 12WY Railway API directly (no MCP subprocess — Termux-safe)."""

    name = "12wy-reports"
    description = TOOL_DESCRIPTION

    def __init__(self, description: str = "") -> None:
        if description:
            self.description = description
        self._tools_meta = [{"name": n} for n in TOOL_NAMES]

    async def run(self, query: str) -> str:
        query = query.strip()
        if query.startswith("{"):
            try:
                parsed = json.loads(query)
                if "tool" in parsed:
                    tool_name = parsed["tool"]
                    args = parsed.get("args", parsed.get("arguments", {}))
                    return await asyncio.to_thread(_call_tool, tool_name, args)
            except json.JSONDecodeError:
                pass

        tool_name, arguments = self._route_query(query)
        if tool_name:
            log.info("12WY routing to: %s", tool_name)
            try:
                return await asyncio.to_thread(_call_tool, tool_name, arguments)
            except Exception as e:
                log.exception("12WY tool %s failed", tool_name)
                return f"Error calling {tool_name}: {e}"

        return (
            "I have these 12WY tools but couldn't determine which one to use:\n"
            + "\n".join(f"  - {n}" for n in TOOL_NAMES)
            + '\n\nTry being more specific or use JSON: {"tool": "twelvewy_get_coaching_brief", "args": {}}'
        )

    def _route_query(self, query: str) -> tuple[str | None, dict]:
        q = query.lower()

        def _pick(substr: str) -> str | None:
            for n in TOOL_NAMES:
                if substr in n:
                    return n
            return None

        if any(kw in q for kw in [
            "coaching brief", "coach", "check-in", "check in", "morning",
            "onboarding", "setup status", "next action",
        ]):
            return _pick("coaching_brief"), {}

        if "onboarding" in q:
            return _pick("onboarding_status"), {}

        if any(kw in q for kw in ["vision", "long-term", "long term", "three year", "3-year"]):
            if any(kw in q for kw in ["update", "set", "write", "create", "save"]):
                return _pick("update_vision"), {"query": query}
            return _pick("get_vision"), {}

        if any(kw in q for kw in ["new season", "create season", "start season", "next season"]):
            return _pick("create_season"), {"query": query}

        if any(kw in q for kw in ["weekly plan", "this week", "week plan", "tactics this week"]):
            return _pick("get_weekly_plan"), {"query": query}

        if any(kw in q for kw in ["complete", "mark done", "finished", "done with"]) and (
            "tactic" in q or "item" in q or "plan" in q
        ):
            item_match = re.search(r"\b(\d+)\b", query)
            if item_match:
                return _pick("complete_plan_item"), {"item_id": int(item_match.group(1))}

        if any(kw in q for kw in ["context", "execution score", "how am i doing", "progress"]):
            return _pick("get_context"), {}

        if any(kw in q for kw in ["list season", "all season", "seasons"]):
            return _pick("list_seasons"), {}

        if "compare" in q and "season" in q:
            return _pick("compare_seasons"), {"query": query}

        if any(kw in q for kw in ["create goal", "new goal", "add goal"]):
            return _pick("create_goal"), {"query": query}

        if any(kw in q for kw in ["create tactic", "new tactic", "add tactic"]):
            return _pick("create_tactic"), {"query": query}

        if any(kw in q for kw in ["12 week", "12wy", "12 wy", "twelve week"]):
            return _pick("coaching_brief") or _pick("get_context"), {}

        return None, {}


def register_twelvewy(cfg: Config, tools: ToolRegistry) -> str | None:
    """Register 12WY when api_base_url and api_key are configured."""
    tw = cfg.twelvewy
    if not (tw.api_key and tw.api_base_url):
        return None

    _apply_config(tw)
    tools.register(TwelveWyGatewayTool())
    log.info("12WY REST gateway registered (remote: %s)", tw.api_base_url)
    return SYSTEM_ADDENDUM
