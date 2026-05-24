"""MCP server wrapping Atlassian APIs (Jira + Confluence) over stdio.

Pure Python — no Rust, no Node.js, no fastmcp. Uses atlassian-python-api
which installs cleanly on Termux/aarch64-android.

Run directly:
    python -m pocket_agent.mcp.atlassian_server

Or via config.toml:
    [[mcp.servers]]
    name = "atlassian"
    command = ["python", "-m", "pocket_agent.mcp.atlassian_server"]

Env vars:
    JIRA_URL            — e.g. https://yourcompany.atlassian.net
    JIRA_USERNAME       — your email
    JIRA_API_TOKEN      — Atlassian API token
    CONFLUENCE_URL      — e.g. https://yourcompany.atlassian.net/wiki
    CONFLUENCE_USERNAME — usually same as JIRA_USERNAME
    CONFLUENCE_API_TOKEN — usually same as JIRA_API_TOKEN
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

log = logging.getLogger(__name__)

SERVER_INFO = {
    "name": "pocket-agent-atlassian",
    "version": "0.1.0",
}

TOOLS = [
    {
        "name": "jira_search",
        "description": "Search Jira issues using JQL or text",
        "inputSchema": {
            "type": "object",
            "properties": {
                "jql": {"type": "string", "description": "JQL query or text to search for"},
            },
            "required": ["jql"],
        },
    },
    {
        "name": "jira_get_issue",
        "description": "Get details of a specific Jira issue by key",
        "inputSchema": {
            "type": "object",
            "properties": {
                "issueIdOrKey": {"type": "string", "description": "Issue key, e.g. PROJ-123"},
            },
            "required": ["issueIdOrKey"],
        },
    },
    {
        "name": "jira_create_issue",
        "description": "Create a new Jira issue",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project key, e.g. PROJ"},
                "summary": {"type": "string", "description": "Issue summary"},
                "description": {"type": "string", "description": "Issue description", "default": ""},
                "issuetype": {"type": "string", "description": "Issue type", "default": "Task"},
            },
            "required": ["project", "summary"],
        },
    },
    {
        "name": "jira_add_comment",
        "description": "Add a comment to a Jira issue",
        "inputSchema": {
            "type": "object",
            "properties": {
                "issueIdOrKey": {"type": "string", "description": "Issue key"},
                "comment": {"type": "string", "description": "Comment text"},
            },
            "required": ["issueIdOrKey", "comment"],
        },
    },
    {
        "name": "jira_list_projects",
        "description": "List visible Jira projects",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "jira_transition_issue",
        "description": "Transition a Jira issue to a new status",
        "inputSchema": {
            "type": "object",
            "properties": {
                "issueIdOrKey": {"type": "string", "description": "Issue key"},
                "status": {"type": "string", "description": "Target status name"},
            },
            "required": ["issueIdOrKey", "status"],
        },
    },
    {
        "name": "confluence_search",
        "description": "Search Confluence pages using CQL or text",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cql": {"type": "string", "description": "CQL query or text to search for"},
            },
            "required": ["cql"],
        },
    },
    {
        "name": "confluence_get_page",
        "description": "Get a Confluence page by ID or title",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pageId": {"type": "string", "description": "Page ID (numeric) or title"},
                "space": {"type": "string", "description": "Space key (required if using title)", "default": ""},
            },
            "required": ["pageId"],
        },
    },
    {
        "name": "confluence_list_spaces",
        "description": "List Confluence spaces",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "confluence_create_page",
        "description": "Create a new Confluence page in a space",
        "inputSchema": {
            "type": "object",
            "properties": {
                "space": {"type": "string", "description": "Space key, e.g. JB"},
                "title": {"type": "string", "description": "Page title"},
                "body": {"type": "string", "description": "Page content in Confluence storage format (XHTML). Use <p>, <h1>-<h6>, <ul>/<li>, <table>, <ac:structured-macro> etc."},
                "parentId": {"type": "string", "description": "Parent page ID (optional — omit for top-level page)", "default": ""},
            },
            "required": ["space", "title", "body"],
        },
    },
    {
        "name": "confluence_update_page",
        "description": "Update an existing Confluence page's title and/or content",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pageId": {"type": "string", "description": "Page ID (numeric)"},
                "title": {"type": "string", "description": "New page title (required — pass existing title if unchanged)"},
                "body": {"type": "string", "description": "New page content in Confluence storage format (XHTML)"},
                "minorEdit": {"type": "boolean", "description": "If true, marks as minor edit (no notification)", "default": False},
            },
            "required": ["pageId", "title", "body"],
        },
    },
]


def _jsonrpc_response(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _jsonrpc_error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _text_result(text: str, is_error: bool = False) -> dict:
    result = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def _extract_text(adf_node) -> str:
    """Extract plain text from Atlassian Document Format (ADF)."""
    if not isinstance(adf_node, dict):
        return str(adf_node) if adf_node else ""
    if adf_node.get("type") == "text":
        return adf_node.get("text", "")
    parts = []
    for child in adf_node.get("content", []):
        parts.append(_extract_text(child))
    return " ".join(parts)


class AtlassianMCPServer:
    def __init__(self) -> None:
        self._jira = None
        self._confluence = None
        self._init_clients()

    def _init_clients(self) -> None:
        try:
            from atlassian import Jira, Confluence  # noqa: F811
        except ImportError:
            log.error(
                "atlassian-python-api is not installed. "
                "Install it with: pip install pocket-agent[atlassian]  "
                "or: pip install atlassian-python-api"
            )
            return

        jira_url = os.getenv("JIRA_URL", "")
        jira_user = os.getenv("JIRA_USERNAME", "")
        jira_token = os.getenv("JIRA_API_TOKEN", "")

        conf_url = os.getenv("CONFLUENCE_URL", "")
        conf_user = os.getenv("CONFLUENCE_USERNAME", jira_user)
        conf_token = os.getenv("CONFLUENCE_API_TOKEN", jira_token)

        if jira_url and jira_user and jira_token:
            is_cloud = "atlassian.net" in jira_url
            self._jira = Jira(url=jira_url, username=jira_user, password=jira_token, cloud=is_cloud)
            log.info("Jira client init: %s (cloud=%s)", jira_url, is_cloud)
            # Verify auth with a lightweight call
            try:
                self._jira.myself()
                log.info("Jira auth verified ✓")
            except Exception as e:
                log.error("Jira auth FAILED — check credentials: %s", e)
                self._jira = None
        else:
            missing = [k for k, v in [("JIRA_URL", jira_url), ("JIRA_USERNAME", jira_user), ("JIRA_API_TOKEN", jira_token)] if not v]
            log.warning("Jira not configured (missing: %s)", ", ".join(missing))

        if conf_url and conf_user and conf_token:
            is_cloud = "atlassian.net" in conf_url
            self._confluence = Confluence(url=conf_url, username=conf_user, password=conf_token, cloud=is_cloud)
            log.info("Confluence client init: %s (cloud=%s)", conf_url, is_cloud)
            # Verify auth with a lightweight call
            try:
                spaces = self._confluence.get_all_spaces(limit=1)
                count = len(spaces.get("results", [])) if isinstance(spaces, dict) else 0
                log.info("Confluence auth verified ✓ (%d spaces visible)", count)
            except Exception as e:
                log.error("Confluence auth FAILED — check credentials: %s", e)
                self._confluence = None
        else:
            missing = [k for k, v in [("CONFLUENCE_URL", conf_url), ("CONFLUENCE_USERNAME", conf_user), ("CONFLUENCE_API_TOKEN", conf_token)] if not v]
            log.warning("Confluence not configured (missing: %s)", ", ".join(missing))

    async def handle(self, request: dict) -> dict | None:
        method = request.get("method", "")
        rid = request.get("id")
        params = request.get("params", {})

        if method == "initialize":
            return _jsonrpc_response(rid, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            })

        elif method == "notifications/initialized":
            return None

        elif method == "tools/list":
            # Only list tools for configured services
            available = []
            for tool in TOOLS:
                if tool["name"].startswith("jira_") and self._jira:
                    available.append(tool)
                elif tool["name"].startswith("confluence_") and self._confluence:
                    available.append(tool)
            return _jsonrpc_response(rid, {"tools": available})

        elif method == "tools/call":
            tool_name = params.get("name", "")
            args = params.get("arguments", {})
            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None, self._call_tool, tool_name, args
                )
                return _jsonrpc_response(rid, _text_result(result))
            except Exception as e:
                log.exception("Tool %s failed", tool_name)
                return _jsonrpc_response(rid, _text_result(f"Error: {e}", is_error=True))

        elif method == "ping":
            return _jsonrpc_response(rid, {})

        else:
            return _jsonrpc_error(rid, -32601, f"Unknown method: {method}")

    def _call_tool(self, name: str, args: dict) -> str:
        if name == "jira_search":
            return self._jira_search(args.get("jql", ""))
        elif name == "jira_get_issue":
            key = args.get("issueIdOrKey", "").strip()
            if not key:
                return "Missing required field: issueIdOrKey (e.g. 'PROJ-123')"
            return self._jira_get_issue(key)
        elif name == "jira_create_issue":
            return self._jira_create_issue(args)
        elif name == "jira_add_comment":
            key = args.get("issueIdOrKey", "").strip()
            comment = args.get("comment", "").strip()
            if not key:
                return "Missing required field: issueIdOrKey"
            if not comment:
                return "Missing required field: comment"
            return self._jira_add_comment(key, comment)
        elif name == "jira_list_projects":
            return self._jira_list_projects()
        elif name == "jira_transition_issue":
            key = args.get("issueIdOrKey", "").strip()
            status = args.get("status", "").strip()
            if not key:
                return "Missing required field: issueIdOrKey"
            if not status:
                return "Missing required field: status"
            return self._jira_transition_issue(key, status)
        elif name == "confluence_search":
            return self._confluence_search(args.get("cql", ""))
        elif name == "confluence_get_page":
            page_id = args.get("pageId", "").strip()
            if not page_id:
                return "Missing required field: pageId (numeric ID or page title)"
            return self._confluence_get_page(page_id, args.get("space", ""))
        elif name == "confluence_list_spaces":
            return self._confluence_list_spaces()
        elif name == "confluence_create_page":
            return self._confluence_create_page(args)
        elif name == "confluence_update_page":
            return self._confluence_update_page(args)
        else:
            raise ValueError(f"Unknown tool: {name}")

    # --- Jira tools ---

    def _jira_search(self, jql: str) -> str:
        if not self._jira:
            return "Jira not configured."
        jql = self._to_jql(jql)

        try:
            result = self._jira.jql(jql, limit=10, fields="summary,status,assignee,priority,updated")
        except Exception as e:
            err = str(e)
            if "Error in the JQL Query" in err:
                # JQL detection was wrong — extract the search intent and retry
                clean = self._strip_jql_noise(jql)
                jql = f'text ~ "{clean}" ORDER BY updated DESC'
                log.info("JQL parse error, retrying as text search: %s", jql)
                result = self._jira.jql(jql, limit=10, fields="summary,status,assignee,priority,updated")
            else:
                raise

        issues = result.get("issues", [])
        if not issues:
            return f"No issues found for: {jql}"

        lines = []
        for i in issues:
            key = i["key"]
            f = i.get("fields", {})
            summary = f.get("summary", "")
            status = f.get("status", {}).get("name", "")
            assignee = f.get("assignee")
            assignee_name = assignee.get("displayName", "Unassigned") if assignee else "Unassigned"
            lines.append(f"{key} [{status}] {summary} ({assignee_name})")
        return "\n".join(lines)

    # Valid JQL field names — if the query starts with one, treat as raw JQL
    _JQL_FIELDS = {
        "assignee", "reporter", "creator", "project", "status", "type",
        "issuetype", "priority", "resolution", "summary", "description",
        "text", "labels", "component", "fixversion", "affectedversion",
        "sprint", "created", "updated", "due", "resolved", "key",
        "issuekey", "id", "filter", "watchers", "voter",
    }

    def _to_jql(self, raw: str) -> str:
        """Convert input to valid JQL — wrap in text search if it's natural language."""
        raw = raw.strip()
        if not raw:
            return "ORDER BY updated DESC"

        # Strip common prefixes the gateway/LLM might prepend
        for prefix in ("search ", "find ", "look up ", "jira ", "search for "):
            if raw.lower().startswith(prefix):
                raw = raw[len(prefix):].strip()
                break

        # Check if it looks like real JQL: first token is a known field name
        first_token = raw.split()[0].lower().rstrip("=<>!~")
        has_operator = any(op in raw for op in ["=", "~", " IN ", " IS "])
        if first_token in self._JQL_FIELDS and has_operator:
            return raw

        # ORDER BY without a preceding clause — just use it
        if raw.upper().startswith("ORDER BY"):
            return raw

        # Not JQL — wrap as text search
        clean = self._strip_jql_noise(raw)
        return f'text ~ "{clean}" ORDER BY updated DESC'

    _CQL_FIELDS = {
        "text", "title", "type", "space", "label", "ancestor",
        "parent", "creator", "contributor", "macro", "content",
        "id", "lastmodified", "created",
    }

    def _to_cql(self, raw: str) -> str:
        """Convert input to valid CQL — wrap in text search if it's natural language."""
        raw = raw.strip()
        if not raw:
            return 'type = "page" ORDER BY lastmodified DESC'

        for prefix in ("search ", "find ", "look up ", "confluence ", "search for "):
            if raw.lower().startswith(prefix):
                raw = raw[len(prefix):].strip()
                break

        first_token = raw.split()[0].lower().rstrip("=<>!~")
        has_operator = any(op in raw for op in ["=", "~", " IN ", " IS "])
        if first_token in self._CQL_FIELDS and has_operator:
            return raw

        if raw.upper().startswith("ORDER BY"):
            return raw

        clean = self._strip_jql_noise(raw)
        return f'text ~ "{clean}"'

    @staticmethod
    def _strip_jql_noise(text: str) -> str:
        """Remove JQL syntax artifacts to extract the search intent."""
        import re
        # Remove operators, quotes, parens
        text = re.sub(r'[=~<>!()"]', " ", text)
        # Remove JQL keywords
        for kw in ("ORDER BY", "AND", "OR", "NOT", "text", "project", "ASC", "DESC", "updated"):
            text = re.sub(rf"\b{kw}\b", " ", text, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", text).strip()

    def _jira_get_issue(self, key: str) -> str:
        if not self._jira:
            return "Jira not configured."
        issue = self._jira.get_issue(key, fields="summary,status,assignee,priority,description,comment,updated")
        f = issue.get("fields", {})
        summary = f.get("summary", "")
        status = f.get("status", {}).get("name", "")
        assignee = f.get("assignee")
        assignee_name = assignee.get("displayName", "Unassigned") if assignee else "Unassigned"
        priority = f.get("priority", {}).get("name", "") if f.get("priority") else ""

        desc = ""
        desc_field = f.get("description")
        if isinstance(desc_field, dict):
            desc = _extract_text(desc_field)
        elif isinstance(desc_field, str):
            desc = desc_field

        lines = [
            f"{key} — {summary}",
            f"Status: {status} | Priority: {priority} | Assignee: {assignee_name}",
        ]
        if desc:
            lines.append(f"\n{desc[:500]}")

        comments = f.get("comment", {}).get("comments", [])
        if comments:
            lines.append(f"\nRecent comments ({len(comments)}):")
            for c in comments[-3:]:
                author = c.get("author", {}).get("displayName", "?")
                body = _extract_text(c.get("body", {})) if isinstance(c.get("body"), dict) else str(c.get("body", ""))
                lines.append(f"  {author}: {body[:150]}")

        return "\n".join(lines)

    def _jira_create_issue(self, args: dict) -> str:
        if not self._jira:
            return "Jira not configured."
        project = args.get("project", "").strip()
        summary = args.get("summary", "").strip()
        if not project:
            return "Missing required field: project (e.g. 'PROJ')"
        if not summary:
            return "Missing required field: summary"
        # Jira enforces a 255-char limit on summary
        if len(summary) > 255:
            summary = summary[:252] + "..."
        issuetype = args.get("issuetype", "Task").strip()
        # Guard against non-string issuetype (LLM may pass a dict)
        if not isinstance(issuetype, str):
            issuetype = str(issuetype) if issuetype else "Task"
        fields = {
            "project": {"key": project.upper()},
            "summary": summary,
            "issuetype": {"name": issuetype},
        }
        desc = args.get("description", "")
        if desc:
            # Ensure description is a string
            if not isinstance(desc, str):
                desc = str(desc)
            fields["description"] = {
                "type": "doc", "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": desc}]}],
            }
        labels = args.get("labels")
        if labels:
            if isinstance(labels, str):
                labels = [l.strip() for l in labels.split(",") if l.strip()]
            if isinstance(labels, list):
                fields["labels"] = labels
        result = self._jira.create_issue(fields=fields)
        return f"Created {result['key']}: {summary}"

    def _jira_add_comment(self, key: str, comment: str) -> str:
        if not self._jira:
            return "Jira not configured."
        self._jira.issue_add_comment(key, comment)
        return f"Comment added to {key}."

    def _jira_list_projects(self) -> str:
        if not self._jira:
            return "Jira not configured."
        projects = self._jira.get_all_projects()
        if not projects:
            return "No projects found."
        lines = []
        for p in projects[:25]:
            lines.append(f"{p['key']} — {p.get('name', '')}")
        return "\n".join(lines)

    def _jira_transition_issue(self, key: str, target_status: str) -> str:
        if not self._jira:
            return "Jira not configured."
        result = self._jira.get_issue_transitions(key)
        transitions = result.get("transitions", []) if isinstance(result, dict) else result
        if not transitions:
            return f"No transitions available for {key} — check workflow permissions."
        target_lower = target_status.lower()
        for t in transitions:
            if t["name"].lower() == target_lower or t["to"]["name"].lower() == target_lower:
                self._jira.issue_transition(key, t["id"])
                return f"Transitioned {key} to {t['to']['name']}."

        available = [f"{t['to']['name']} (id:{t['id']})" for t in transitions]
        return f"Can't transition {key} to '{target_status}'. Available: {', '.join(available)}"

    # --- Confluence tools ---

    def _confluence_search(self, cql: str) -> str:
        if not self._confluence:
            return "Confluence not configured."
        cql = self._to_cql(cql)

        results = self._confluence.cql(cql, limit=10).get("results", [])
        if not results:
            return f"No pages found for: {cql}"

        lines = []
        for r in results:
            content = r.get("content", r)
            title = content.get("title", r.get("title", ""))
            page_id = content.get("id", r.get("id", ""))
            space = content.get("_expandable", {}).get("space", "").split("/")[-1]
            lines.append(f"#{page_id} — {title} (space: {space})")
        return "\n".join(lines)

    def _confluence_get_page(self, page_id: str, space: str = "") -> str:
        if not self._confluence:
            return "Confluence not configured."
        import re

        if page_id.isdigit():
            page = self._confluence.get_page_by_id(page_id, expand="body.storage,version")
        elif space:
            page = self._confluence.get_page_by_title(space, page_id, expand="body.storage,version")
        else:
            # Search by title
            results = self._confluence.cql(f'title = "{page_id}"', limit=1).get("results", [])
            if not results:
                return f"No page found: {page_id}"
            content = results[0].get("content", results[0])
            real_id = content.get("id", "")
            if real_id:
                page = self._confluence.get_page_by_id(real_id, expand="body.storage,version")
            else:
                return f"No page found: {page_id}"

        if not page:
            return f"Page not found: {page_id}"

        title = page.get("title", "")
        pid = page.get("id", "")
        version = page.get("version", {}).get("number", "")
        body_html = page.get("body", {}).get("storage", {}).get("value", "")

        text = re.sub(r"<[^>]+>", " ", body_html)
        text = re.sub(r"\s+", " ", text).strip()

        lines = [f"#{pid} — {title} (v{version})", ""]
        lines.append(text[:2000])
        if len(text) > 2000:
            lines.append("... (truncated)")
        return "\n".join(lines)

    def _confluence_list_spaces(self) -> str:
        if not self._confluence:
            return "Confluence not configured."
        spaces = self._confluence.get_all_spaces(limit=25)
        results = spaces.get("results", []) if isinstance(spaces, dict) else spaces
        if not results:
            return "No spaces found."
        lines = []
        for s in results:
            lines.append(f"{s['key']} — {s.get('name', '')}")
        return "\n".join(lines)

    def _confluence_create_page(self, args: dict) -> str:
        if not self._confluence:
            return "Confluence not configured."
        space = args.get("space", "").strip()
        title = args.get("title", "").strip()
        body = args.get("body", "").strip()
        if not space:
            return "Missing required field: space (e.g. 'JB')"
        if not title:
            return "Missing required field: title"
        if not body:
            return "Missing required field: body (HTML content)"
        space = space.upper()
        parent_id = args.get("parentId", "") or None

        result = self._confluence.create_page(
            space=space,
            title=title,
            body=body,
            parent_id=parent_id,
            representation="storage",
        )
        page_id = result.get("id", "")
        url = result.get("_links", {}).get("base", "") + result.get("_links", {}).get("webui", "")
        return f"Created page #{page_id}: {title}\n{url}"

    def _confluence_update_page(self, args: dict) -> str:
        if not self._confluence:
            return "Confluence not configured."
        page_id = args.get("pageId", "").strip()
        title = args.get("title", "").strip()
        body = args.get("body", "").strip()
        if not page_id:
            return "Missing required field: pageId (numeric page ID)"
        if not title:
            return "Missing required field: title"
        if not body:
            return "Missing required field: body (HTML content)"
        minor_edit = args.get("minorEdit", False)

        result = self._confluence.update_page(
            page_id=page_id,
            title=title,
            body=body,
            representation="storage",
            minor_edit=minor_edit,
        )
        version = result.get("version", {}).get("number", "?")
        url = result.get("_links", {}).get("base", "") + result.get("_links", {}).get("webui", "")
        return f"Updated page #{page_id}: {title} (v{version})\n{url}"


async def run_stdio() -> None:
    server = AtlassianMCPServer()

    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

    buf = b""
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            break
        buf += chunk

        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue

            response = await server.handle(request)
            if response is not None:
                out = json.dumps(response) + "\n"
                sys.stdout.write(out)
                sys.stdout.flush()


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
