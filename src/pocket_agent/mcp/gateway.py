"""MCP Gateway Tool — exposes an MCP server as a single Tool for the agent.

Rather than registering every MCP tool individually (which requires
upfront connection), this registers one gateway per MCP server.
The gateway lazy-connects, discovers tools, and routes queries to
the best matching MCP tool.

Supports fallback: if MCP connection fails at runtime, the gateway
can delegate to REST-based Tool instances (e.g. jira.py, confluence.py).

Usage in config.toml:
    [[mcp.servers]]
    name = "atlassian"
    command = ["python", "-m", "pocket_agent.mcp.atlassian_server"]
    description = "Jira and Confluence"
"""
from __future__ import annotations

import json
import logging
import time

from pocket_agent.mcp.client import MCPClient, MCPServerConfig
from pocket_agent.tools.base import Tool

log = logging.getLogger(__name__)

# How long to stay on the REST fallback before retrying MCP (seconds)
MCP_RETRY_INTERVAL = 300  # 5 minutes


class MCPGatewayTool(Tool):
    """A single Tool that wraps an entire MCP server.

    On first call, connects to the MCP server and discovers all its tools.
    Then uses the query to route to the best matching tool.

    Supports optional fallback_tools — if MCP connection or calls fail,
    the query is routed to the fallback REST tools instead. After
    MCP_RETRY_INTERVAL seconds the gateway will try MCP again instead
    of staying on the REST fallback permanently.
    """

    def __init__(
        self,
        config: MCPServerConfig,
        tool_description: str = "",
        fallback_tools: list[Tool] | None = None,
    ) -> None:
        self._mcp_config = config
        self._client: MCPClient | None = None
        self._tools_meta: list[dict] = []
        self._fallback_tools = fallback_tools or []
        self._using_fallback = False
        self._fallback_since: float = 0.0  # monotonic time of last MCP failure

        self.name = config.name
        self.description = tool_description or f"MCP server: {config.name}"

    async def _ensure_connected(self) -> MCPClient:
        if self._client is None or not self._client._connected:
            self._client = MCPClient(self._mcp_config)
            await self._client.connect()
            self._tools_meta = self._client._tools_meta
            log.info(
                "MCP gateway '%s' connected — %d tools available",
                self.name, len(self._tools_meta),
            )
        return self._client

    async def _enter_fallback(self) -> None:
        """Mark the gateway as using REST fallback."""
        if not self._using_fallback:
            self._using_fallback = True
            log.info("MCP '%s' switching to REST fallback tools", self.name)
        self._fallback_since = time.monotonic()

    def _should_retry_mcp(self) -> bool:
        """Check if enough time has passed to retry the MCP connection."""
        if not self._using_fallback:
            return False
        return (time.monotonic() - self._fallback_since) >= MCP_RETRY_INTERVAL

    async def _run_fallback(self, query: str) -> str:
        """Route query to the best matching fallback REST tool.

        The REST tools (JiraTool, ConfluenceTool) parse queries like:
            "create PROJ | Summary | Desc"
            "search some text"
        So we need to clean the query into a format they expect —
        strip "jira"/"issue"/"ticket" noise from the action prefix.
        """
        if not self._fallback_tools:
            return f"MCP '{self.name}' is unavailable and no fallback configured."

        await self._enter_fallback()

        q = query.lower()

        # Determine which fallback tool to use
        target_tool = None
        for tool in self._fallback_tools:
            if tool.name in q or q.startswith(tool.name):
                target_tool = tool
                break
        if not target_tool:
            for tool in self._fallback_tools:
                if tool.name == "jira" and any(kw in q for kw in [
                    "issue", "ticket", "sprint", "jira", "my issues",
                    "assigned", "backlog", "board", "project", "create",
                ]):
                    target_tool = tool
                    break
                if tool.name == "confluence" and any(kw in q for kw in [
                    "page", "wiki", "confluence", "doc", "space",
                ]):
                    target_tool = tool
                    break
        if not target_tool:
            target_tool = self._fallback_tools[0]

        # Clean query for REST tool — strip gateway-level noise words
        # so "create issue in PROJ | Summary" → "create PROJ | Summary"
        clean = self._clean_query_for_rest(query, target_tool.name)
        return await target_tool.run(clean)

    @staticmethod
    def _clean_query_for_rest(query: str, tool_name: str) -> str:
        """Strip noise words between the action verb and the arguments.

        Gateway queries arrive as natural language from the LLM, but REST
        tools expect compact format: 'create PROJ | Summary | Desc'.
        """
        text = query.strip()
        q = text.lower()

        # If query starts with the tool name itself, strip it
        # e.g. "jira create PROJ | Summary" → "create PROJ | Summary"
        for name in (tool_name, "jira", "confluence", "atlassian"):
            if q.startswith(name + " "):
                text = text[len(name):].strip()
                q = text.lower()
                break

        # For create/comment commands, strip noise between action and args
        if q.startswith("create"):
            rest = text[6:].strip()  # after "create"
            for prefix in (
                # Jira noise
                "a jira issue in ", "a jira ticket in ", "jira issue in ",
                "jira ticket in ", "an issue in ", "a ticket in ",
                "a task in ", "issue in ", "ticket in ", "task in ",
                "a jira issue ", "a jira ticket ", "jira issue ",
                "issue ", "ticket ", "task ",
                # Confluence noise
                "a confluence page in space ", "confluence page in space ",
                "a wiki page in space ", "wiki page in space ",
                "a confluence page in ", "confluence page in ",
                "a wiki page in ", "wiki page in ",
                "a page in space ", "page in space ",
                "a page in ", "page in ",
                "a confluence page ", "confluence page ",
                "a wiki page ", "wiki page ",
                "a page ", "page ",
                # Generic
                "a ", "an ",
            ):
                if rest.lower().startswith(prefix):
                    rest = rest[len(prefix):].strip()
                    break
            return f"create {rest}"

        # For get/show/read commands, strip noise
        if any(q.startswith(v) for v in ("get ", "show ", "read ")):
            verb = text.split()[0]
            rest = text[len(verb):].strip()
            for prefix in (
                "confluence page ", "wiki page ", "page ",
                "jira issue ", "issue ", "ticket ",
            ):
                if rest.lower().startswith(prefix):
                    rest = rest[len(prefix):].strip()
                    break
            return f"get {rest}"

        return text

    async def run(self, query: str) -> str:
        # If on REST fallback, periodically retry MCP
        if self._using_fallback:
            if self._should_retry_mcp():
                log.info("MCP '%s' retry interval elapsed — attempting reconnect", self.name)
                try:
                    self._client = None  # force fresh connection
                    await self._ensure_connected()
                    self._using_fallback = False
                    log.info("MCP '%s' reconnected — switching back from REST", self.name)
                except Exception:
                    log.info("MCP '%s' retry failed — staying on REST fallback", self.name)
                    self._fallback_since = time.monotonic()
                    return await self._run_fallback(query)
            else:
                return await self._run_fallback(query)

        try:
            client = await self._ensure_connected()
        except Exception as e:
            log.exception("MCP gateway '%s' connection failed", self.name)
            if self._fallback_tools:
                log.info("Falling back to REST tools for '%s'", self.name)
                return await self._run_fallback(query)
            return f"Couldn't connect to {self.name}: {e}"

        query = query.strip()

        # Try parsing as explicit JSON with a tool name
        # e.g. {"tool": "searchJiraIssuesUsingJql", "args": {"jql": "...", "cloudId": "..."}}
        if query.startswith("{"):
            try:
                parsed = json.loads(query)
                if "tool" in parsed:
                    tool_name = parsed["tool"]
                    args = parsed.get("args", parsed.get("arguments", {}))
                    return await client.call_tool(tool_name, args)
            except json.JSONDecodeError:
                pass

        # Route by matching the query against tool descriptions
        tool_name, arguments = self._route_query(query)
        if tool_name:
            log.info("MCP gateway '%s' routing to: %s", self.name, tool_name)
            try:
                return await client.call_tool(tool_name, arguments)
            except Exception as e:
                log.warning("MCP tool %s failed: %s — trying REST fallback", tool_name, e)
                if self._fallback_tools:
                    await self._enter_fallback()
                    return await self._run_fallback(query)
                return f"Error calling {tool_name}: {e}"

        # No route matched — try fallback if available
        if self._fallback_tools:
            return await self._run_fallback(query)

        # Last resort: list available tools
        tool_names = [t["name"] for t in self._tools_meta]
        return (
            f"I have these {self.name} tools but couldn't determine which one to use:\n"
            + "\n".join(f"  - {n}" for n in tool_names)
            + f"\n\nTry being more specific or use JSON: "
            f'{{"tool": "toolName", "args": {{...}}}}'
        )

    def _route_query(self, query: str) -> tuple[str | None, dict]:
        """Match a natural-language query to the best MCP tool.

        Two-pass routing: first check keyword-specific matches across ALL tools,
        then fall back to generic search matching. This prevents jira_search from
        stealing confluence queries just because it appears first in the tool list.
        """
        import re
        q = query.lower()
        is_atlassian = "jira" in self.name.lower() or "atlassian" in self.name.lower()
        is_twelvewy = any(
            n in self.name.lower() for n in ("12wy", "twelvewy")
        )

        # Helper to find a tool by name substring
        def _find_tool(substr: str) -> tuple[str, dict] | None:
            for meta in self._tools_meta:
                if substr in meta["name"].lower():
                    return meta["name"], meta.get("inputSchema", {})
            return None

        # --- Pass 1: Keyword-specific routing (checks query intent first) ---

        if is_twelvewy:
            if any(kw in q for kw in [
                "coaching brief", "coach", "check-in", "check in", "morning",
                "onboarding", "setup status", "next action",
            ]):
                t = _find_tool("coaching_brief") or _find_tool("get_coaching")
                if t:
                    return t[0], {}

            if "onboarding" in q:
                t = _find_tool("onboarding_status") or _find_tool("get_onboarding")
                if t:
                    return t[0], {}

            if any(kw in q for kw in ["vision", "long-term", "long term", "three year", "3-year"]):
                if any(kw in q for kw in ["update", "set", "write", "create", "save"]):
                    t = _find_tool("update_vision")
                    if t:
                        return t[0], self._build_args(t[1], query)
                t = _find_tool("get_vision")
                if t:
                    return t[0], {}

            if any(kw in q for kw in ["new season", "create season", "start season", "next season"]):
                t = _find_tool("create_season")
                if t:
                    return t[0], self._build_args(t[1], query)

            if any(kw in q for kw in ["weekly plan", "this week", "week plan", "tactics this week"]):
                t = _find_tool("get_weekly_plan") or _find_tool("weekly_plan")
                if t:
                    return t[0], self._build_args(t[1], query)

            if any(kw in q for kw in ["complete", "mark done", "finished", "done with"]) and (
                "tactic" in q or "item" in q or "plan" in q
            ):
                t = _find_tool("complete_plan_item")
                if t:
                    item_match = re.search(r"\b(\d+)\b", query)
                    if item_match:
                        return t[0], {"item_id": int(item_match.group(1))}

            if any(kw in q for kw in ["context", "execution score", "how am i doing", "progress"]):
                t = _find_tool("get_context")
                if t:
                    return t[0], {}

            if any(kw in q for kw in ["list season", "all season", "seasons"]):
                t = _find_tool("list_seasons")
                if t:
                    return t[0], {}

            if "compare" in q and "season" in q:
                t = _find_tool("compare_seasons")
                if t:
                    return t[0], self._build_args(t[1], query)

            if any(kw in q for kw in ["create goal", "new goal", "add goal"]):
                t = _find_tool("create_goal")
                if t:
                    return t[0], self._build_args(t[1], query)

            if any(kw in q for kw in ["create tactic", "new tactic", "add tactic"]):
                t = _find_tool("create_tactic")
                if t:
                    return t[0], self._build_args(t[1], query)

            if any(kw in q for kw in ["12 week", "12wy", "12 wy", "twelve week"]):
                t = _find_tool("coaching_brief") or _find_tool("get_context")
                if t:
                    return t[0], {}

        # Confluence keywords take priority when present
        if any(kw in q for kw in ["confluence", "wiki", "page", "documentation"]):
            if "spaces" in q or "list spaces" in q:
                t = _find_tool("confluence_list") or _find_tool("space")
                if t:
                    return t[0], {}

            if any(kw in q for kw in ["get", "read", "show", "access"]) or "search" in q:
                t = _find_tool("confluence_search") or _find_tool("cql")
                if t:
                    search_text = q
                    for strip in ["search", "confluence", "wiki", "for", "find", "page",
                                  "access", "can you", "now", "?"]:
                        search_text = search_text.replace(strip, "")
                    search_text = search_text.strip()
                    if search_text:
                        return t[0], self._build_args(t[1], f'text ~ "{search_text}"')
                    # No search text — list spaces instead
                    spaces = _find_tool("confluence_list") or _find_tool("space")
                    if spaces:
                        return spaces[0], {}
                    return t[0], self._build_args(t[1], 'type = "page" ORDER BY lastmodified DESC')

            if ("get" in q or "read" in q or "show" in q) and "page" in q:
                t = _find_tool("confluence_get")
                if t:
                    args = self._parse_confluence_get(query)
                    return t[0], args

            if any(kw in q for kw in ["create", "new", "add"]) and any(kw in q for kw in ["page", "doc"]):
                t = _find_tool("confluence_create")
                if t:
                    args = self._parse_confluence_create(query)
                    return t[0], args

            if any(kw in q for kw in ["update", "edit", "change", "modify", "replace"]) and any(kw in q for kw in ["page", "doc", "content"]):
                t = _find_tool("confluence_update")
                if t:
                    args = self._parse_confluence_update(query)
                    return t[0], args

        # Jira-specific routing
        if is_atlassian:
            if any(kw in q for kw in ["my issues", "assigned to me", "my tickets"]):
                t = _find_tool("jira_search") or _find_tool("searchjira")
                if t:
                    return t[0], self._build_args(t[1], "assignee = currentUser() ORDER BY updated DESC")

            if any(kw in q for kw in ["search", "find", "look up"]) and "issue" in q:
                t = _find_tool("jira_search") or _find_tool("searchjira")
                if t:
                    search_text = q
                    for strip in ["search", "find", "look up", "jira", "issues", "for", "in"]:
                        search_text = search_text.replace(strip, "")
                    search_text = search_text.strip()
                    if search_text:
                        return t[0], self._build_args(t[1], f'text ~ "{search_text}" ORDER BY updated DESC')
                    return t[0], self._build_args(t[1], "ORDER BY updated DESC")

            if any(kw in q for kw in ["get issue", "show issue", "details"]):
                t = _find_tool("jira_get")
                if t:
                    key_match = re.search(r"[A-Z]+-\d+", query)
                    if key_match:
                        return t[0], {"issueIdOrKey": key_match.group()}

            if "transition" in q or "move" in q or "status" in q:
                t = _find_tool("transition")
                if t:
                    key_match = re.search(r"[A-Z]+-\d+", query)
                    if key_match:
                        return t[0], {"issueIdOrKey": key_match.group()}

            if "create" in q and ("issue" in q or "ticket" in q or "task" in q):
                t = _find_tool("jira_create")
                if t:
                    args = self._parse_jira_create(query)
                    return t[0], args

            if "project" in q and ("list" in q or "show" in q or "what" in q):
                t = _find_tool("jira_list") or _find_tool("project")
                if t:
                    return t[0], {}

        # --- Pass 2: Generic search fallback (last resort) ---
        if any(kw in q for kw in ["search", "find", "look up"]):
            for meta in self._tools_meta:
                if "search" in meta["name"].lower():
                    return meta["name"], self._build_args(meta.get("inputSchema", {}), query)

        return None, {}

    def _parse_jira_create(self, query: str) -> dict:
        """Extract project, summary, and description from a create-issue query.

        Handles multiple formats the LLM might produce:
          - "create PROJ | Summary | Description"
          - "create issue in PROJ | Summary | Description"
          - "create a ticket in PROJECT-KEY with summary Build page"
          - "create PROJ Summary text here"
          - kwargs: 'project="PROJ", summary="Fix bug", description="Details"'
        """
        import re

        text = query.strip()
        # Strip leading action words
        for prefix in (
            "create issue in ", "create a ticket in ", "create ticket in ",
            "create an issue in ", "create a task in ", "create task in ",
            "create issue ", "create ticket ", "create task ",
            "create a jira issue in ", "create jira issue ",
            "create ",
        ):
            if text.lower().startswith(prefix):
                text = text[len(prefix):].strip()
                break

        # ── Format 1: kwargs-style ──
        # LLM sometimes emits: project="PROJ", summary="Fix the bug", ...
        # or: PROJ, summary="Fix the bug", issuetype="Task", ...
        if 'summary=' in text or 'summary =' in text:
            args = self._parse_kwargs(text)
            if args.get("summary"):
                return args

        # ── Format 2: pipe-delimited ──
        #   "PROJ | Summary | Description"           (3 parts)
        #   "PROJ | Type | Summary | Description"    (4 parts)
        if "|" in text:
            parts = [p.strip() for p in text.split("|")]
            project = self._extract_project_key(parts[0])
            issue_types = {"task", "bug", "story", "epic", "sub-task", "subtask", "improvement"}
            if len(parts) >= 4 and parts[1].lower() in issue_types:
                issuetype = parts[1]
                summary = parts[2]
                description = "|".join(parts[3:])
            else:
                issuetype = ""
                summary = parts[1] if len(parts) > 1 else ""
                description = "|".join(parts[2:]) if len(parts) > 2 else ""
        else:
            # ── Format 3: natural language ──
            key_match = re.search(r"\b([A-Z][A-Z0-9]{1,9})\b", text)
            project = key_match.group(1) if key_match else ""
            if key_match:
                remainder = text[key_match.end():].strip()
                for strip in ("with summary ", "titled ", "title ", "summary "):
                    if remainder.lower().startswith(strip):
                        remainder = remainder[len(strip):].strip()
                        break
                summary = remainder.strip('"\'')
            else:
                summary = text
            issuetype = ""
            description = ""

        args = {}
        if project:
            args["project"] = project.upper()
        if summary:
            args["summary"] = summary
        if description:
            args["description"] = description
        if issuetype:
            args["issuetype"] = issuetype
        return args

    @staticmethod
    def _parse_kwargs(text: str) -> dict:
        """Parse kwargs-style LLM output into a Jira fields dict.

        Handles formats like:
          project="JB", summary="Fix the bug", issuetype="Task", description="Details here"
          JB, summary="Fix bug", description="More info"
        """
        import re

        args: dict[str, str] = {}

        # Extract all key="value" pairs (handles quoted values with commas inside)
        for m in re.finditer(r'(\w+)\s*=\s*"([^"]*)"', text):
            key = m.group(1).lower()
            val = m.group(2).strip()
            if key in ("project", "summary", "issuetype", "description", "labels"):
                args[key] = val

        # If no project= kwarg, look for a bare project key at the start
        if "project" not in args:
            # e.g. "JB, summary=..." — grab the leading word before the first comma
            lead = text.split(",")[0].strip().strip('"\'')
            key_match = re.match(r"^([A-Z][A-Z0-9]{1,9})\b", lead)
            if key_match:
                args["project"] = key_match.group(1)

        if "project" in args:
            args["project"] = args["project"].upper()

        return args

    @staticmethod
    def _extract_project_key(text: str) -> str:
        """Pull a Jira project key out of text like 'project PROJ' or just 'PROJ'."""
        import re
        text = text.strip()
        # Strip noise words
        for prefix in ("project ", "in project ", "in "):
            if text.lower().startswith(prefix):
                text = text[len(prefix):].strip()
                break
        # Find the uppercase key
        match = re.search(r"\b([A-Z][A-Z0-9]{1,9})\b", text)
        return match.group(1) if match else text.split()[0] if text else ""

    def _parse_confluence_get(self, query: str) -> dict:
        """Extract pageId (or title + space) from a get-page query.

        Handles:
          - "get page 12345"
          - "get confluence page 12345"
          - "show page Meeting Notes in space JB"
          - "read page #98765"
        """
        import re
        text = query.strip()
        # Strip action + noise
        for prefix in (
            "get confluence page ", "show confluence page ",
            "read confluence page ", "get wiki page ",
            "get page ", "show page ", "read page ",
            "get ", "show ", "read ",
        ):
            if text.lower().startswith(prefix):
                text = text[len(prefix):].strip()
                break

        text = text.lstrip("#")
        args: dict = {}

        # Check for "in space XX" suffix
        space_match = re.search(r"\bin\s+space\s+([A-Z][A-Z0-9]{0,9})\b", text, re.IGNORECASE)
        if space_match:
            args["space"] = space_match.group(1).upper()
            text = text[:space_match.start()].strip()

        # If it's a numeric page ID, use it directly
        if text.isdigit():
            args["pageId"] = text
        else:
            # Treat as page title
            args["pageId"] = text.strip('"\'')

        return args

    def _parse_confluence_create(self, query: str) -> dict:
        """Extract space, title, and body from a create-page query.

        Handles:
          - "create page in JB | Meeting Notes | <p>Content here</p>"
          - "create a confluence page in space JB titled Meeting Notes"
          - "create page | JB | Title | Body content"
          - "new wiki page in JB | Sprint Retro | <h1>Retro</h1><p>Notes</p>"
        """
        import re
        text = query.strip()
        # Strip action prefixes
        for prefix in (
            "create a confluence page in space ", "create confluence page in space ",
            "create a wiki page in space ", "create wiki page in space ",
            "create a confluence page in ", "create confluence page in ",
            "create a wiki page in ", "create wiki page in ",
            "create a page in space ", "create page in space ",
            "create a new page in space ", "create new page in space ",
            "create a page in ", "create page in ",
            "create a new page in ", "create new page in ",
            "new confluence page in space ", "new wiki page in space ",
            "new confluence page in ", "new wiki page in ",
            "new page in space ", "new page in ",
            "add a page in space ", "add page in space ",
            "add a page in ", "add page in ",
            "create a confluence page ", "create confluence page ",
            "create a wiki page ", "create wiki page ",
            "create a page ", "create page ",
            "new page ", "add page ",
            "create ", "new ", "add ",
        ):
            if text.lower().startswith(prefix):
                text = text[len(prefix):].strip()
                break

        args: dict = {}

        # Pipe-delimited: "JB | Title | Body"
        if "|" in text:
            parts = [p.strip() for p in text.split("|")]
            # Drop empty leading parts (from "create page | SPACE | Title")
            while parts and not parts[0]:
                parts.pop(0)
            if len(parts) >= 3:
                # "SPACE | Title | Body" — join any extra pipes back into body
                args["space"] = self._extract_space_key(parts[0])
                args["title"] = parts[1]
                args["body"] = " | ".join(parts[2:])
            elif len(parts) == 2:
                # Could be "SPACE | Title" or "Title | Body"
                first = parts[0].strip()
                if re.match(r"^[A-Z][A-Z0-9]{0,9}$", first):
                    args["space"] = first
                    args["title"] = parts[1]
                else:
                    args["title"] = first
                    args["body"] = parts[1]
        else:
            # Natural language: extract space key + "titled X" pattern
            space_match = re.search(r"\b([A-Z][A-Z0-9]{0,9})\b", text)
            if space_match:
                args["space"] = space_match.group(1)
                remainder = text[space_match.end():].strip()
            else:
                remainder = text

            for strip in ("titled ", "title ", "called "):
                if remainder.lower().startswith(strip):
                    remainder = remainder[len(strip):].strip()
                    break

            args["title"] = remainder.strip('"\'')

        # Wrap bare text body in a paragraph tag for storage format
        if "body" in args and not args["body"].strip().startswith("<"):
            args["body"] = f"<p>{args['body']}</p>"

        return {k: v for k, v in args.items() if v}

    def _parse_confluence_update(self, query: str) -> dict:
        """Extract pageId, title, and body from an update-page query.

        Handles:
          - "update page 12345 | New Title | <p>New content</p>"
          - "edit confluence page #98765 | Updated Title | New body"
          - "update page 12345 with title New Title and body <p>...</p>"
        """
        import re
        text = query.strip()
        # Strip action prefixes
        for prefix in (
            "update confluence page ", "edit confluence page ",
            "modify confluence page ", "change confluence page ",
            "update wiki page ", "edit wiki page ",
            "update page ", "edit page ",
            "modify page ", "change page ",
            "replace content of page ", "replace page ",
            "update ", "edit ", "modify ", "change ",
        ):
            if text.lower().startswith(prefix):
                text = text[len(prefix):].strip()
                break

        text = text.lstrip("#")
        args: dict = {}

        # Pipe-delimited: "12345 | Title | Body"
        if "|" in text:
            parts = [p.strip() for p in text.split("|")]
            args["pageId"] = parts[0].strip().lstrip("#")
            if len(parts) >= 2:
                args["title"] = parts[1]
            if len(parts) >= 3:
                args["body"] = parts[2]
        else:
            # Try to extract page ID (numeric)
            id_match = re.match(r"(\d+)\b", text)
            if id_match:
                args["pageId"] = id_match.group(1)
                remainder = text[id_match.end():].strip()

                for strip in ("with title ", "titled ", "title "):
                    if remainder.lower().startswith(strip):
                        remainder = remainder[len(strip):].strip()
                        break

                # Look for "and body ..." or "body ..."
                body_match = re.search(r"\b(?:and\s+)?body\s+(.+)", remainder, re.IGNORECASE)
                if body_match:
                    args["body"] = body_match.group(1).strip()
                    args["title"] = remainder[:body_match.start()].strip().strip('"\'')
                else:
                    args["title"] = remainder.strip('"\'')

        # Wrap bare text body in storage format
        if "body" in args and not args["body"].strip().startswith("<"):
            args["body"] = f"<p>{args['body']}</p>"

        return {k: v for k, v in args.items() if v}

    @staticmethod
    def _extract_space_key(text: str) -> str:
        """Pull a Confluence space key from text like 'space JB' or just 'JB'."""
        import re
        text = text.strip()
        for prefix in ("space ", "in space ", "in "):
            if text.lower().startswith(prefix):
                text = text[len(prefix):].strip()
                break
        match = re.search(r"\b([A-Z][A-Z0-9]{0,9})\b", text)
        return match.group(1) if match else text.split()[0].upper() if text else ""

    def _build_args(self, schema: dict, query_value: str) -> dict:
        """Build arguments dict, mapping query to the right parameter."""
        props = schema.get("properties", {})
        required = schema.get("required", [])

        # Find the main query parameter
        for param_name in ("jql", "cql", "query", "searchString", "q"):
            if param_name in props:
                return {param_name: query_value}

        # Fallback to first required string
        for key in required:
            if props.get(key, {}).get("type") == "string":
                return {key: query_value}

        return {"query": query_value}

    async def close(self) -> None:
        if self._client:
            await self._client.close()
