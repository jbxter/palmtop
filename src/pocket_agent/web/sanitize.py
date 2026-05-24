"""Input/output sanitization for the public web channel.

SECURITY: This module sits between the public internet and the LLM.
It strips anything that could be used for prompt injection, tool-call
injection, or XSS in the chat widget.
"""

from __future__ import annotations

import re

# Patterns that look like the agent's internal tool-call format
_TOOL_CALL_PATTERN = re.compile(
    r"\[TOOL:\w+\]|\[ACTION:\w+\]|\[ON_FAIL:\w+\]",
    re.IGNORECASE,
)

# Patterns that look like system prompt injection
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(previous|above|all)\s+(instructions?|prompts?|rules?)", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+", re.IGNORECASE),
    re.compile(r"new\s+instructions?:", re.IGNORECASE),
    re.compile(r"system\s*:\s*", re.IGNORECASE),
    re.compile(r"\bDAN\b.*\bmode\b", re.IGNORECASE),
    re.compile(r"act\s+as\s+(if\s+)?(you\s+)?(are|were)\s+", re.IGNORECASE),
]

# HTML/script tags
_HTML_TAG = re.compile(r"<[^>]+>")
_SCRIPT_PATTERN = re.compile(r"<script[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)


def sanitize_input(text: str, max_length: int = 1000) -> str:
    """Clean visitor input before passing to the LLM.

    - Strips HTML tags (prevents XSS if reflected)
    - Strips tool-call patterns (prevents tool injection)
    - Truncates to max_length
    - Strips leading/trailing whitespace
    - Returns empty string if input is effectively empty
    """
    if not text or not text.strip():
        return ""

    # Strip HTML
    clean = _SCRIPT_PATTERN.sub("", text)
    clean = _HTML_TAG.sub("", clean)

    # Strip tool-call patterns
    clean = _TOOL_CALL_PATTERN.sub("", clean)

    # Truncate
    clean = clean[:max_length].strip()

    # Normalize whitespace (collapse runs of whitespace to single space)
    clean = re.sub(r"\s+", " ", clean)

    return clean


def sanitize_output(text: str) -> str:
    """Clean LLM output before sending to the visitor.

    - Strips any tool-call patterns (in case the LLM hallucinates them)
    - Strips HTML tags (prevent XSS in the chat widget)
    - Strips internal system references
    """
    if not text:
        return ""

    clean = text

    # Remove any tool-call patterns the LLM might hallucinate
    clean = _TOOL_CALL_PATTERN.sub("", clean)

    # Remove HTML tags
    clean = _SCRIPT_PATTERN.sub("", clean)
    clean = _HTML_TAG.sub("", clean)

    # Strip references to internal systems the agent shouldn't mention
    # (belt-and-suspenders — the system prompt already forbids this)
    for pattern in [
        re.compile(r"\[TOOL:.*?\].*", re.IGNORECASE),
        re.compile(r"```tool.*?```", re.IGNORECASE | re.DOTALL),
    ]:
        clean = pattern.sub("", clean)

    return clean.strip()


def is_suspicious(text: str) -> bool:
    """Check if input contains prompt injection patterns.

    Returns True if the input looks like a prompt injection attempt.
    The caller can choose to flag, log, or block the message.
    """
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            return True
    return False
