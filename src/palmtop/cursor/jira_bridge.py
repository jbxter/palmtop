"""JiraCursorBridge — read Jira tickets and auto-delegate coding work to Cursor.

When the monitor detects a new or updated Jira ticket, this bridge:
1. Reads the full ticket (summary, description, acceptance criteria, labels)
2. Decides if it's a coding task (label, issue type, or description heuristics)
3. Builds a detailed Cursor prompt with context from the ticket
4. Launches a Cursor Cloud Agent
5. On completion, comments on the Jira ticket with the PR URL and status
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from palmtop.cursor.runner import CursorJobManager

log = logging.getLogger(__name__)

# Labels or issue types that trigger auto-delegation
CURSOR_LABELS = {"cursor", "cursor-ready", "automate", "code-task"}
CODE_ISSUE_TYPES = {"bug", "story", "task", "sub-task", "improvement"}

# Keywords in description/summary that suggest coding work
CODE_KEYWORDS = re.compile(
    r"\b(refactor|implement|fix|bug|feature|endpoint|api|test|migration"
    r"|component|function|class|module|deploy|ci|lint|type.?hint"
    r"|docstring|readme|changelog)\b",
    re.I,
)


def is_cursor_eligible(issue: dict) -> bool:
    """Decide whether a Jira issue should be delegated to Cursor.

    Returns True if ANY of these match:
    - Has a label in CURSOR_LABELS
    - Is a coding-related issue type AND description has code keywords
    """
    labels = {l.lower() for l in issue.get("labels", [])}
    if labels & CURSOR_LABELS:
        return True

    issue_type = (issue.get("issue_type") or issue.get("type") or "").lower()
    if issue_type not in CODE_ISSUE_TYPES:
        return False

    text = f"{issue.get('summary', '')} {issue.get('description', '')}"
    return bool(CODE_KEYWORDS.search(text))


def build_cursor_prompt(issue: dict) -> str:
    """Build a detailed Cursor Cloud Agent prompt from a Jira issue."""
    key = issue.get("key", "UNKNOWN")
    summary = issue.get("summary", "No summary")
    description = issue.get("description", "")
    labels = issue.get("labels", [])
    acceptance = issue.get("acceptance_criteria", "")
    priority = issue.get("priority", "")
    issue_type = issue.get("issue_type") or issue.get("type", "")

    parts = [
        f"Jira ticket {key}: {summary}",
        "",
        f"Type: {issue_type}",
    ]
    if priority:
        parts.append(f"Priority: {priority}")
    if labels:
        parts.append(f"Labels: {', '.join(labels)}")

    parts.append("")

    if description:
        # Truncate very long descriptions
        desc = description[:2000]
        if len(description) > 2000:
            desc += "\n... (truncated)"
        parts.append("Description:")
        parts.append(desc)
        parts.append("")

    if acceptance:
        parts.append("Acceptance Criteria:")
        parts.append(acceptance[:1000])
        parts.append("")

    parts.extend(
        [
            "Instructions:",
            "- Read the codebase to understand the relevant files",
            "- Implement the changes described above",
            "- Follow existing code style and conventions",
            "- Add or update tests if applicable",
            f"- Reference {key} in your commit messages",
            f"- Create a PR with a clear description linking to {key}",
        ]
    )

    return "\n".join(parts)


class JiraCursorBridge:
    """Connects Jira monitoring to Cursor delegation.

    Reads issues via the Jira tool, decides if they're code work,
    and launches Cursor agents. Tracks which issues have already
    been delegated to avoid duplicates.
    """

    def __init__(
        self,
        jira_tool,
        cursor_manager: CursorJobManager,
        *,
        send_fn=None,
        user_id: str = "default",
    ) -> None:
        self._jira = jira_tool
        self._cursor = cursor_manager
        self._send_fn = send_fn
        self._user_id = user_id
        self._delegated_keys: set[str] = set()  # already-delegated issue keys

    async def evaluate_and_delegate(self, issue_key: str) -> str | None:
        """Read a Jira issue and delegate to Cursor if eligible.

        Returns:
            Launch reply string if delegated, None if skipped.
        """
        if issue_key in self._delegated_keys:
            log.debug("Skipping %s — already delegated", issue_key)
            return None

        # Read full ticket details
        issue = await self._read_issue(issue_key)
        if not issue:
            log.warning("Could not read issue %s — skipping delegation", issue_key)
            return None

        if not is_cursor_eligible(issue):
            log.debug("Issue %s not cursor-eligible — skipping", issue_key)
            return None

        # Build prompt and delegate
        prompt = build_cursor_prompt(issue)
        log.info("Auto-delegating %s to Cursor: %s", issue_key, issue.get("summary", "")[:80])

        # The trigger is an untrusted Jira ticket (anyone who can label a ticket
        # could fire this), so always require human /approve, regardless of the
        # global [cursor] require_blessing setting (issue #46).
        result = await self._cursor.launch(prompt, user_id=self._user_id, force_approval=True)
        self._delegated_keys.add(issue_key)

        # Notify user
        if self._send_fn:
            try:
                msg = f"🤖 Auto-delegated **{issue_key}** to Cursor\n_{issue.get('summary', '')}_\n\n{result}"
                await self._send_fn(self._user_id, msg)
            except Exception:
                log.warning("Failed to notify about auto-delegation of %s", issue_key)

        return result

    async def _read_issue(self, key: str) -> dict | None:
        """Read full issue details via the Jira tool."""
        try:
            raw = await self._jira.run(f"get {key}")
        except Exception:
            log.debug("Failed to read issue %s", key, exc_info=True)
            return None

        # Parse the tool output into structured data
        return self._parse_issue_output(key, raw)

    def _parse_issue_output(self, key: str, raw: str) -> dict | None:
        """Parse Jira tool text output into a dict."""
        if not raw or "error" in raw.lower()[:50]:
            return None

        issue: dict = {"key": key}

        # Extract fields from the tool's formatted output
        summary_m = re.search(r"Summary:\s*(.+)", raw)
        if summary_m:
            issue["summary"] = summary_m.group(1).strip()
        else:
            # First non-empty line after the key is often the summary
            lines = [l.strip() for l in raw.split("\n") if l.strip()]
            if lines:
                issue["summary"] = lines[0][:200]

        type_m = re.search(r"Type:\s*(.+)", raw)
        if type_m:
            issue["issue_type"] = type_m.group(1).strip()

        priority_m = re.search(r"Priority:\s*(.+)", raw)
        if priority_m:
            issue["priority"] = priority_m.group(1).strip()

        status_m = re.search(r"Status:\s*(.+)", raw)
        if status_m:
            issue["status"] = status_m.group(1).strip()

        labels_m = re.search(r"Labels?:\s*(.+)", raw)
        if labels_m:
            issue["labels"] = [l.strip() for l in labels_m.group(1).split(",")]

        # Description: everything after "Description:" until the next field header
        desc_m = re.search(
            r"Description:\s*\n(.*?)(?=\n(?:Acceptance|Labels?|Priority|Status|Assignee|Reporter):|\Z)",
            raw,
            re.DOTALL,
        )
        if desc_m:
            issue["description"] = desc_m.group(1).strip()

        # Acceptance criteria
        ac_m = re.search(
            r"Acceptance\s+Criteria:\s*\n(.*?)(?=\n(?:Labels?|Priority|Status):|\Z)",
            raw,
            re.DOTALL,
        )
        if ac_m:
            issue["acceptance_criteria"] = ac_m.group(1).strip()

        return issue if issue.get("summary") else None

    async def on_cursor_complete(
        self,
        issue_key: str,
        *,
        status: str,
        pr_url: str | None = None,
        result: str | None = None,
    ) -> None:
        """Comment on the Jira ticket when Cursor finishes."""
        if not pr_url and status != "FINISHED":
            comment = f"Cursor agent completed with status: {status}"
            if result:
                comment += f"\n\n{result[:500]}"
        elif pr_url:
            comment = f"Cursor agent opened a PR: {pr_url}\n\nStatus: {status}"
        else:
            comment = f"Cursor agent finished.\n\n{(result or '')[:500]}"

        try:
            await self._jira.run(f"comment {issue_key} | {comment}")
            log.info("Commented on %s with Cursor result", issue_key)
        except Exception:
            log.warning("Failed to comment on %s", issue_key, exc_info=True)
