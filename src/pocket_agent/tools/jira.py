from __future__ import annotations

import logging
from base64 import b64encode

import httpx

from pocket_agent.tools.base import Tool

log = logging.getLogger(__name__)


class JiraTool(Tool):
    name = "jira"
    description = (
        "Manage Jira issues. Usage:\n"
        "  [TOOL:jira] search <JQL or text query>\n"
        "  [TOOL:jira] get <issue key, e.g. PROJ-123>\n"
        "  [TOOL:jira] create <project key> | <summary> | <description>\n"
        "  [TOOL:jira] comment <issue key> | <comment text>\n"
        "  [TOOL:jira] transition <issue key> | <status name, e.g. Done>\n"
        "  [TOOL:jira] my issues"
    )

    def __init__(self, domain: str, email: str, api_token: str) -> None:
        self._domain = domain
        self._base_url = f"https://{domain}/rest/api/3"
        self._auth = b64encode(f"{email}:{api_token}".encode()).decode()
        self._client: httpx.AsyncClient | None = None
        self._verified = False

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=15.0,
                headers={
                    "Authorization": f"Basic {self._auth}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        return self._client

    async def verify_auth(self) -> str | None:
        """Test connectivity. Returns None on success, error message on failure."""
        try:
            client = self._get_client()
            resp = await client.get(f"{self._base_url}/myself")
            if resp.status_code == 200:
                name = resp.json().get("displayName", "unknown")
                log.info("Jira REST auth verified ✓ (%s @ %s)", name, self._domain)
                self._verified = True
                return None
            elif resp.status_code == 401:
                msg = f"Jira auth failed (401) — bad email or API token for {self._domain}"
                log.error(msg)
                return msg
            elif resp.status_code == 403:
                msg = f"Jira auth forbidden (403) — token may lack permissions on {self._domain}"
                log.error(msg)
                return msg
            else:
                msg = f"Jira returned {resp.status_code} on auth check: {resp.text[:200]}"
                log.warning(msg)
                return msg
        except httpx.ConnectError:
            msg = f"Can't reach {self._domain} — check domain name and network"
            log.error(msg)
            return msg
        except Exception as e:
            msg = f"Jira auth check failed: {e}"
            log.error(msg)
            return msg

    async def run(self, query: str) -> str:
        parts = query.strip().split(None, 1)
        if not parts:
            return "Usage: search <query> | get <key> | create <proj>|<summary>|<desc> | comment <key>|<text> | my issues"

        action = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        try:
            if action == "search":
                return await self._search(rest)
            elif action == "get":
                return await self._get(rest)
            elif action == "create":
                return await self._create(rest)
            elif action == "comment":
                return await self._comment(rest)
            elif action == "transition":
                return await self._transition(rest)
            elif action == "my":
                return await self._my_issues()
            else:
                return await self._search(query)
        except Exception as e:
            log.exception("Jira operation failed")
            return f"Jira error: {e}"

    async def _search(self, query: str) -> str:
        if not query:
            return "Need a search query."
        # If it looks like JQL, use it directly; otherwise wrap in text search
        if any(op in query.upper() for op in ["=", "IN ", "ORDER BY", "AND ", "OR "]):
            jql = query
        else:
            jql = f'text ~ "{query}" ORDER BY updated DESC'

        client = self._get_client()
        resp = await client.get(
            f"{self._base_url}/search",
            params={"jql": jql, "maxResults": 10, "fields": "summary,status,assignee,priority,updated"},
        )
        if resp.status_code != 200:
            return f"Jira search failed ({resp.status_code}): {resp.text[:200]}"

        issues = resp.json().get("issues", [])
        if not issues:
            return f"No issues found for: {query}"

        return _format_issues(issues)

    async def _get(self, key: str) -> str:
        key = key.strip().upper()
        if not key:
            return "Need an issue key (e.g. PROJ-123)."

        client = self._get_client()
        resp = await client.get(
            f"{self._base_url}/issue/{key}",
            params={"fields": "summary,status,assignee,priority,description,comment,updated"},
        )
        if resp.status_code == 404:
            return f"Issue {key} not found."
        if resp.status_code != 200:
            return f"Jira error ({resp.status_code}): {resp.text[:200]}"

        issue = resp.json()
        fields = issue.get("fields", {})
        summary = fields.get("summary", "")
        status = fields.get("status", {}).get("name", "")
        assignee = fields.get("assignee", {})
        assignee_name = assignee.get("displayName", "Unassigned") if assignee else "Unassigned"
        priority = fields.get("priority", {}).get("name", "")

        desc = ""
        desc_field = fields.get("description")
        if desc_field and isinstance(desc_field, dict):
            desc = _extract_adf_text(desc_field)
        elif isinstance(desc_field, str):
            desc = desc_field

        lines = [
            f"{key} — {summary}",
            f"Status: {status} | Priority: {priority} | Assignee: {assignee_name}",
        ]
        if desc:
            lines.append(f"\n{desc[:500]}")

        comments = fields.get("comment", {}).get("comments", [])
        if comments:
            lines.append(f"\nRecent comments ({len(comments)}):")
            for c in comments[-3:]:
                author = c.get("author", {}).get("displayName", "?")
                body = _extract_adf_text(c.get("body", {})) if isinstance(c.get("body"), dict) else str(c.get("body", ""))
                lines.append(f"  {author}: {body[:150]}")

        return "\n".join(lines)

    async def _create(self, text: str) -> str:
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 2:
            return "Format: create PROJECT-KEY | Summary | Optional description"
        project_key = _extract_project_key(parts[0])
        if not project_key:
            return f"Couldn't find a valid project key in: {parts[0]}"
        summary = parts[1]
        description = parts[2] if len(parts) > 2 else ""

        body = {
            "fields": {
                "project": {"key": project_key},
                "summary": summary,
                "issuetype": {"name": "Task"},
            }
        }
        if description:
            body["fields"]["description"] = {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}],
            }

        client = self._get_client()
        resp = await client.post(f"{self._base_url}/issue", json=body)
        if resp.status_code not in (200, 201):
            return f"Failed to create issue ({resp.status_code}): {resp.text[:200]}"

        data = resp.json()
        return f"Created {data['key']}: {summary}"

    async def _comment(self, text: str) -> str:
        parts = [p.strip() for p in text.split("|", 1)]
        if len(parts) < 2:
            return "Format: comment PROJ-123 | Your comment text"
        key = parts[0].upper()
        comment_text = parts[1]

        body = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": comment_text}]}],
            }
        }

        client = self._get_client()
        resp = await client.post(f"{self._base_url}/issue/{key}/comment", json=body)
        if resp.status_code not in (200, 201):
            return f"Failed to add comment ({resp.status_code}): {resp.text[:200]}"

        return f"Comment added to {key}."

    async def _transition(self, text: str) -> str:
        parts = [p.strip() for p in text.split("|", 1)]
        if len(parts) < 2:
            return "Format: transition PROJ-123 | Done"
        key = parts[0].upper()
        target_status = parts[1]

        client = self._get_client()
        resp = await client.get(f"{self._base_url}/issue/{key}/transitions")
        if resp.status_code != 200:
            return f"Failed to get transitions for {key} ({resp.status_code}): {resp.text[:200]}"

        transitions = resp.json().get("transitions", [])
        if not transitions:
            return f"No transitions available for {key} — check workflow permissions."

        target_lower = target_status.lower()
        for t in transitions:
            if t["name"].lower() == target_lower or t["to"]["name"].lower() == target_lower:
                post_resp = await client.post(
                    f"{self._base_url}/issue/{key}/transitions",
                    json={"transition": {"id": t["id"]}},
                )
                if post_resp.status_code != 204:
                    return f"Transition failed ({post_resp.status_code}): {post_resp.text[:200]}"
                return f"Transitioned {key} to {t['to']['name']}."

        available = [f"{t['to']['name']} (id:{t['id']})" for t in transitions]
        return f"Can't transition {key} to '{target_status}'. Available: {', '.join(available)}"

    async def _my_issues(self) -> str:
        return await self._search("assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC")

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()


class ConfluenceTool(Tool):
    name = "confluence"
    description = (
        "Search and read Confluence pages. Usage:\n"
        "  [TOOL:confluence] search <query>\n"
        "  [TOOL:confluence] get <page ID or title>\n"
        "  [TOOL:confluence] spaces"
    )

    def __init__(self, domain: str, email: str, api_token: str) -> None:
        self._domain = domain
        self._base_url = f"https://{domain}/wiki"
        self._auth = b64encode(f"{email}:{api_token}".encode()).decode()
        self._client: httpx.AsyncClient | None = None
        self._verified = False

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=15.0,
                headers={
                    "Authorization": f"Basic {self._auth}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        return self._client

    async def verify_auth(self) -> str | None:
        """Test connectivity. Returns None on success, error message on failure."""
        try:
            client = self._get_client()
            resp = await client.get(f"{self._base_url}/rest/api/space", params={"limit": 1})
            if resp.status_code == 200:
                count = len(resp.json().get("results", []))
                log.info("Confluence REST auth verified ✓ (%d spaces, %s)", count, self._domain)
                self._verified = True
                return None
            elif resp.status_code == 401:
                msg = f"Confluence auth failed (401) — bad credentials for {self._domain}"
                log.error(msg)
                return msg
            elif resp.status_code == 403:
                msg = f"Confluence auth forbidden (403) — token may lack permissions on {self._domain}"
                log.error(msg)
                return msg
            else:
                msg = f"Confluence returned {resp.status_code} on auth check: {resp.text[:200]}"
                log.warning(msg)
                return msg
        except httpx.ConnectError:
            msg = f"Can't reach {self._domain}/wiki — check domain name and network"
            log.error(msg)
            return msg
        except Exception as e:
            msg = f"Confluence auth check failed: {e}"
            log.error(msg)
            return msg

    async def run(self, query: str) -> str:
        parts = query.strip().split(None, 1)
        if not parts:
            return "Usage: search <query> | get <page id or title> | spaces"

        action = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        try:
            if action == "search":
                return await self._search(rest)
            elif action == "get":
                return await self._get(rest)
            elif action == "spaces":
                return await self._spaces()
            else:
                return await self._search(query)
        except Exception as e:
            log.exception("Confluence operation failed")
            return f"Confluence error: {e}"

    async def _search(self, query: str) -> str:
        if not query:
            return "Need a search query."

        client = self._get_client()
        resp = await client.get(
            f"{self._base_url}/rest/api/content/search",
            params={"cql": f'text ~ "{query}"', "limit": 10, "expand": "metadata.labels"},
        )
        if resp.status_code != 200:
            return f"Confluence search failed ({resp.status_code}): {resp.text[:200]}"

        results = resp.json().get("results", [])
        if not results:
            return f"No pages found for: {query}"

        lines = []
        for r in results:
            title = r.get("title", "")
            page_id = r.get("id", "")
            space = r.get("_expandable", {}).get("space", "").split("/")[-1]
            lines.append(f"#{page_id} — {title} (space: {space})")
        return "\n".join(lines)

    async def _get(self, identifier: str) -> str:
        identifier = identifier.strip()
        if not identifier:
            return "Need a page ID or title."

        client = self._get_client()
        if identifier.isdigit():
            resp = await client.get(
                f"{self._base_url}/rest/api/content/{identifier}",
                params={"expand": "body.storage,version"},
            )
        else:
            resp = await client.get(
                f"{self._base_url}/rest/api/content",
                params={"title": identifier, "expand": "body.storage,version", "limit": 1},
            )
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                if not results:
                    return f"No page titled: {identifier}"
                page = results[0]
                return _format_confluence_page(page)
            return f"Confluence error ({resp.status_code}): {resp.text[:200]}"

        if resp.status_code == 404:
            return f"Page {identifier} not found."
        if resp.status_code != 200:
            return f"Confluence error ({resp.status_code}): {resp.text[:200]}"

        return _format_confluence_page(resp.json())

    async def _spaces(self) -> str:
        client = self._get_client()
        resp = await client.get(f"{self._base_url}/rest/api/space", params={"limit": 25})
        if resp.status_code != 200:
            return f"Failed to list spaces ({resp.status_code})"

        spaces = resp.json().get("results", [])
        if not spaces:
            return "No spaces found."

        lines = ["Confluence spaces:"]
        for s in spaces:
            lines.append(f"  {s['key']} — {s.get('name', '')}")
        return "\n".join(lines)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()


def _extract_project_key(text: str) -> str:
    """Pull a Jira project key from text that may have noise words.

    Handles:  'PROJ', 'project PROJ', 'issue in PROJ', 'in project PROJ'
    Returns the uppercase key or empty string if none found.
    """
    import re
    text = text.strip()
    # Strip common prefixes
    for prefix in (
        "project ", "in project ", "in ",
        "issue in ", "ticket in ", "task in ",
        "issue ", "ticket ", "task ",
    ):
        if text.lower().startswith(prefix):
            text = text[len(prefix):].strip()
            break
    # Find an uppercase project key (1-10 uppercase alphanumeric chars)
    match = re.search(r"\b([A-Z][A-Z0-9]{1,9})\b", text)
    if match:
        return match.group(1)
    # Fallback: take the first word and uppercase it
    first = text.split()[0] if text else ""
    return first.upper() if first.isalpha() else ""


def _format_issues(issues: list) -> str:
    lines = []
    for i in issues:
        key = i.get("key", "")
        fields = i.get("fields", {})
        summary = fields.get("summary", "")
        status = fields.get("status", {}).get("name", "")
        priority = fields.get("priority", {}).get("name", "") if fields.get("priority") else ""
        assignee = fields.get("assignee", {})
        assignee_name = assignee.get("displayName", "Unassigned") if assignee else "Unassigned"
        lines.append(f"{key} [{status}] {summary} ({assignee_name})")
    return "\n".join(lines)


def _extract_adf_text(node: dict) -> str:
    if not isinstance(node, dict):
        return str(node)
    if node.get("type") == "text":
        return node.get("text", "")
    parts = []
    for child in node.get("content", []):
        parts.append(_extract_adf_text(child))
    return " ".join(parts)


def _format_confluence_page(page: dict) -> str:
    title = page.get("title", "")
    page_id = page.get("id", "")
    version = page.get("version", {}).get("number", "")
    body_html = page.get("body", {}).get("storage", {}).get("value", "")

    import re
    text = re.sub(r"<[^>]+>", " ", body_html)
    text = re.sub(r"\s+", " ", text).strip()

    lines = [f"#{page_id} — {title} (v{version})", ""]
    lines.append(text[:2000])
    if len(text) > 2000:
        lines.append("... (truncated)")
    return "\n".join(lines)
