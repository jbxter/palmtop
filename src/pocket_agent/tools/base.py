from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Tool names may include hyphens (e.g. 12wy-reports)
_TOOL_NAME = r"[\w-]+"
TOOL_CALL_PATTERN = re.compile(
    rf"\[TOOL:({_TOOL_NAME})\]\s*(.+?)(?=\n|\[TOOL:|\[/TOOL\]|\Z)",
)

TOOL_INSTRUCTIONS = """\

Tools:
You have access to the following tools. You MUST use them when the user asks \
about their calendar, asks you to search for something, or wants a reminder set. \
Do NOT say you can't access their calendar or the web — you CAN, using these tools.

{tool_list}

Format — include this marker in your response to call a tool:
[TOOL:tool_name] your query or parameters here

Examples:
- User asks "what's on my calendar today?" → [TOOL:calendar] show today
- User asks "do I have anything on Friday?" → [TOOL:calendar] show 2025-06-20
- User asks "look up coworking spaces in Austin" → [TOOL:search] coworking spaces Austin
- User asks "remind me to call Sarah in 2 hours" → [TOOL:remind] 2h Call Sarah
- Create a Jira ticket → [TOOL:atlassian] create PROJ | Short summary here | Longer description with details
- Create a typed Jira ticket → [TOOL:atlassian] create PROJ | Bug | Login fails on mobile | Users see a 500 error when tapping Sign In

After you use a tool, you'll receive the results in a follow-up message — then answer \
from that data. Do not narrate that you are fetching or about to check; emit the \
[TOOL:...] line and stop. You can use multiple tools in one response."""


TOOL_HINT_KEYWORDS = {
    "calendar": [
        "calendar", "schedule", "event", "meeting", "appointment",
        "what's on", "whats on", "do i have", "am i free", "busy",
        "block", "book", "party", "dinner", "lunch", "brunch",
    ],
    "search": [
        "search the web", "search online", "find out about",
        "latest news on", "google search", "web search",
        "look up online",
    ],
    "remind": [
        "remind", "reminder", "don't forget", "dont forget",
        "alert me", "notify me", "ping me",
    ],
    "kb": [
        "knowledge base", "in my notes", "in my kb",
        "what do i have on", "what did i save",
        "your architecture", "your codebase", "how are you built",
        "your source code", "your own code",
    ],
    "jira": [
        "jira", "ticket", "sprint", "backlog",
        "my issues", "my tasks", "assigned to me",
        "atlassian.net/browse", "atlassian.net/jira",
        "jira board", "jira project", "issue tracker",
        "open issues", "search issues", "search tickets",
    ],
    "confluence": [
        "confluence", "wiki page", "documentation page",
        "wiki/spaces", "atlassian.net/wiki",
        "create a page", "update the page", "edit the page",
        "write to confluence", "publish to confluence",
        "search pages", "search confluence",
    ],
    "email": [
        "email", "mail", "inbox", "send an email", "send email",
        "check my email", "check email", "read email",
        "reply to", "forward to", "new message",
        "my email address", "email address",
    ],
    "files": [
        "save this to a file", "save to file", "write to file",
        "create a file", "create a doc", "create a document",
        "save this as", "export to file", "write a report",
        "update the doc", "update the file", "append to file",
        "my files", "my documents", "list files", "list docs",
        "show my files", "show my docs",
    ],
    "12wy-reports": [
        "12 week year", "12wy", "12 wy", "twelve week",
        "execution score", "weekly plan", "my tactics", "my season",
        "12-week", "coaching brief", "onboarding status",
    ],
    "cursor": [
        "cursor agent", "cloud agent", "delegate to cursor",
        "open a pr", "create a pr for", "fix in the repo",
        "work on the codebase", "cursor cloud",
    ],
    "vercel": [
        "vercel", "deploy to vercel", "vercel deploy",
        "production deploy", "preview deploy vercel",
        ".vercel.app",
    ],
    "railway": [
        "railway", "deploy to railway", "railway deploy",
        "railway service", "up.railway.app",
    ],
}


# Generic tools that should be suppressed when a specific tool matches.
# e.g. "search my jira issues" should run jira, not web search.
_GENERIC_TOOLS = {"search"}

# Specific tools — if any of these match, generic tools are dropped.
_SPECIFIC_TOOLS = {
    "jira", "confluence", "email", "kb", "calendar", "remind", "files",
    "12wy-reports", "cursor", "vercel", "railway",
}


def detect_tool_hints(text: str) -> list[tuple[str, str]]:
    text_lower = text.lower()
    hints = []
    for tool_name, keywords in TOOL_HINT_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                hints.append((tool_name, text))
                break

    if not hints:
        return hints

    # If any specific tool matched, suppress generic tools (web search)
    has_specific = any(name in _SPECIFIC_TOOLS for name, _ in hints)
    if has_specific:
        hints = [(name, txt) for name, txt in hints if name not in _GENERIC_TOOLS]

    return hints


@dataclass
class ToolResult:
    tool: str
    query: str
    result: str
    success: bool
    error_kind: str | None = None   # auth, syntax, not_found, timeout, rate_limit,
                                    # connection, server, not_configured, unknown
    retry_hint: str | None = None   # "simplify", "retry", or None
    raw_response: str = ""          # original unprocessed response (for tracing)
    retried: bool = False           # True if this result came from a retry attempt
    original_error: str = ""        # first-attempt error if retried


# ---------------------------------------------------------------------------
# Error classification — pattern-match tool result strings
# ---------------------------------------------------------------------------

_ERROR_PATTERNS: list[tuple[re.Pattern, str, str | None]] = [
    # (compiled regex, error_kind, retry_hint)
    (re.compile(r"(?i)error[\s(]*40[13]"), "auth", None),
    (re.compile(r"(?i)error[\s(]*404"), "not_found", None),
    (re.compile(r"(?i)error[\s(]*429"), "rate_limit", "retry"),
    (re.compile(r"(?i)error[\s(]*5\d\d"), "server", "retry"),
    (re.compile(r"(?i)parse error|syntax error|malformed"), "syntax", "simplify"),
    (re.compile(r"(?i)Error in the JQL|Expecting\s.*?\bbut got\b"), "syntax", "simplify"),
    (re.compile(r"(?i)Error in the CQL|CQL syntax"), "syntax", "simplify"),
    (re.compile(r"(?i)timed?\s*out|timeout|deadline exceeded"), "timeout", "retry"),
    (re.compile(r"(?i)connection\s*(?:refused|reset|closed|error)"), "connection", "retry"),
    (re.compile(r"(?i)not configured|not available|disabled"), "not_configured", None),
    (re.compile(r"(?i)unknown tool|no such tool"), "not_found", None),
]


def classify_result(tool_name: str, query: str, result: str) -> ToolResult:
    """Wrap a raw tool result string into a classified ToolResult."""
    for pattern, kind, hint in _ERROR_PATTERNS:
        if pattern.search(result):
            return ToolResult(
                tool=tool_name, query=query, result=result,
                success=False, error_kind=kind,
                retry_hint=hint, raw_response=result,
            )

    # Generic error heuristic — tools prefix errors with "Error:" or "Toolname error"
    r_lower = result.lower()
    if (
        r_lower.startswith("error:")
        or f"{tool_name} error" in r_lower
        or r_lower.startswith("failed:")
    ):
        return ToolResult(
            tool=tool_name, query=query, result=result,
            success=False, error_kind="unknown",
            raw_response=result,
        )

    return ToolResult(
        tool=tool_name, query=query, result=result,
        success=True, raw_response=result,
    )


def simplify_query(query: str) -> str:
    """Strip action prefixes and complex syntax for a retry attempt."""
    q = query
    # Remove action prefixes: "search jira issues:", "find confluence pages:"
    q = re.sub(
        r"^(?:search|find|get|list|show)\s+(?:jira|confluence)?\s*"
        r"(?:issues?|pages?|tickets?|docs?)?\s*:?\s*",
        "", q, flags=re.IGNORECASE,
    ).strip()
    # Remove JQL/CQL operators
    q = re.sub(r"\b(?:AND|OR|NOT|ORDER\s+BY|ASC|DESC)\b", " ", q, flags=re.IGNORECASE)
    # Remove field=value references (project = "X", status != Done)
    q = re.sub(r"\w+\s*[=~!<>]+\s*", "", q)
    # Remove quotes
    q = re.sub(r'["\']', "", q)
    # Remove JQL function calls like currentUser()
    q = re.sub(r"\w+\(\)", "", q)
    # Collapse whitespace
    q = re.sub(r"\s+", " ", q).strip()
    return q if q else query


def build_retry_query(tool_name: str, query: str, error_kind: str) -> str | None:
    """Build a tool-specific retry query.

    Returns a new query string, or None if no retry strategy applies.
    Uses tool-aware strategies: Jira syntax errors become JQL text
    searches, Confluence errors become CQL text searches, etc.
    """
    terms = simplify_query(query)
    if terms == query and error_kind == "syntax":
        return None  # simplification didn't change anything

    if error_kind == "syntax":
        # Tool-specific structured retries
        if tool_name in ("jira", "atlassian"):
            # Wrap in JQL text search — searches summary, description, comments
            safe = terms.replace('"', '\\"')
            return f'text ~ "{safe}" ORDER BY updated DESC'

        if tool_name == "confluence":
            safe = terms.replace('"', '\\"')
            return f'text ~ "{safe}" ORDER BY lastModified DESC'

        # Generic tools — just use the simplified text
        return terms

    if error_kind in ("timeout", "rate_limit", "server", "connection"):
        # Transient errors — retry same query (backoff handled by caller)
        return query

    return None


# ---------------------------------------------------------------------------
# Error guidance — contextual hints for the LLM to explain failures
# ---------------------------------------------------------------------------

ERROR_GUIDANCE: dict[str, str] = {
    "auth": "Credentials or permissions issue — the user may need to check their API token.",
    "syntax": "The query syntax was invalid. A simpler search was attempted.",
    "not_found": "The requested item doesn't exist or may have been moved.",
    "timeout": "The service took too long to respond.",
    "rate_limit": "Too many requests — the service is rate-limiting.",
    "server": "The service is having internal issues (5xx error).",
    "connection": "Could not connect to the service — it may be down.",
    "not_configured": "This integration isn't set up yet.",
}


# ---------------------------------------------------------------------------
# Default fallback registry — automatic cross-tool pivots
# ---------------------------------------------------------------------------

# Maps tool_name → list of (fallback_tool, query_template) pairs.
# {query} is replaced with the simplified original query.
# The loop auto-injects these when a plain [TOOL:] call fails and
# no explicit [ON_FAIL:] was declared.
DEFAULT_FALLBACKS: dict[str, list[tuple[str, str]]] = {
    "jira": [
        ("confluence", "search {query}"),
        ("web_search", "site:atlassian.net {query}"),
    ],
    "confluence": [
        ("jira", "search {query}"),
        ("web_search", "site:atlassian.net {query}"),
    ],
    "atlassian": [
        ("web_search", "site:atlassian.net {query}"),
    ],
    "calendar": [
        ("files", "read notes/calendar-fallback.md"),
    ],
}


def get_default_fallbacks(tool_name: str, query: str) -> list[ActionStep]:
    """Build ActionSteps from the default fallback registry for a failed tool."""
    entries = DEFAULT_FALLBACKS.get(tool_name.lower(), [])
    if not entries:
        return []

    terms = simplify_query(query) or query
    steps = []
    for fb_tool, template in entries:
        fb_query = template.replace("{query}", terms)
        steps.append(ActionStep(fb_tool, fb_query))
    return steps


class Tool:
    name: str = ""
    description: str = ""

    async def run(self, query: str) -> str:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool
        log.info("Registered tool: %s", tool.name)

    def get(self, name: str) -> Tool | None:
        resolved = resolve_tool_name(self, name)
        return self._tools.get(resolved) if resolved else None

    def format_instructions(self) -> str:
        if not self._tools:
            return ""
        lines = []
        for t in self._tools.values():
            lines.append(f"- {t.name}: {t.description}")
        tool_list = "\n".join(lines)
        return TOOL_INSTRUCTIONS.format(tool_list=tool_list) + ACTION_INSTRUCTIONS

    async def close(self) -> None:
        for t in self._tools.values():
            await t.close()


def extract_tool_calls(text: str) -> list[tuple[str, str]]:
    return [(m.group(1).lower(), m.group(2).strip()) for m in TOOL_CALL_PATTERN.finditer(text)]


def resolve_tool_name(registry: ToolRegistry, name: str) -> str | None:
    """Map parsed tool name to a registered tool (handles aliases and prefixes)."""
    if name in registry._tools:
        return name
    lower = name.lower()
    for key in registry._tools:
        if key.lower() == lower:
            return key
    # e.g. model emitted [TOOL:12wy] for 12wy-reports
    for key in registry._tools:
        kl = key.lower()
        if kl.startswith(lower) or lower.startswith(kl):
            return key
    return None


# ---------------------------------------------------------------------------
# ACTION chains — multi-step intent with fallbacks
# ---------------------------------------------------------------------------

@dataclass
class ActionStep:
    tool: str
    query: str


@dataclass
class ActionChain:
    primary: ActionStep
    fallbacks: list[ActionStep]


_ACTION_RE = re.compile(rf"^\[ACTION:({_TOOL_NAME})\]\s*(.+)")
_ON_FAIL_RE = re.compile(rf"^\[ON_FAIL:({_TOOL_NAME})\]\s*(.+)")


def extract_action_chains(text: str) -> list[ActionChain]:
    """Extract ACTION chains with ON_FAIL fallbacks from LLM output.

    Format:
        [ACTION:jira] search issues about deployment
        [ON_FAIL:confluence] search pages about deployment
        [ON_FAIL:files] write notes/fallback.md | Could not query Jira

    Each ACTION starts a new chain. ON_FAIL lines attach as fallbacks
    to the preceding ACTION. Max 3 fallbacks per chain.
    """
    chains: list[ActionChain] = []
    current_primary: ActionStep | None = None
    current_fallbacks: list[ActionStep] = []

    for line in text.splitlines():
        line = line.strip()

        am = _ACTION_RE.match(line)
        if am:
            if current_primary:
                chains.append(ActionChain(current_primary, current_fallbacks))
            current_primary = ActionStep(am.group(1).lower(), am.group(2).strip())
            current_fallbacks = []
            continue

        fm = _ON_FAIL_RE.match(line)
        if fm and current_primary:
            if len(current_fallbacks) < 3:
                current_fallbacks.append(ActionStep(fm.group(1).lower(), fm.group(2).strip()))

    if current_primary:
        chains.append(ActionChain(current_primary, current_fallbacks))

    return chains


ACTION_INSTRUCTIONS = """

Action chains (proactive problem-solving):
When you anticipate a tool call might fail, declare an ACTION chain with \
fallbacks instead of a plain [TOOL:] call. The system will try each step \
in order and move to the next if one fails — no extra round trip needed.

[ACTION:tool_name] primary query
[ON_FAIL:other_tool] fallback query if the first fails
[ON_FAIL:files] write notes/result.md | Document what you found or couldn't find

Use ACTION chains when:
- A query might hit an auth or syntax error and you have an alternative source
- You want to save results to a file as a backup
- You're checking multiple systems for the same information

Use regular [TOOL:] calls for straightforward requests with no fallback needed."""
