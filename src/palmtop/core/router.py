"""Fast inference-tier routing — pick a tier with a heuristic, no LLM call."""

from __future__ import annotations

import re

# Cues that a turn needs the heavier, more capable tier.
_HEAVY_HINTS = re.compile(
    r"\b(analy[sz]e|compare|explain\s+why|reason\b|strateg|architect|design|"
    r"debug|refactor|prove|derive|optimi[sz]e|trade-?off|essay|draft|"
    r"summar(?:y|ize|ise)|review|plan)\b",
    re.I,
)


def route_fast(text: str, has_tool_hints: bool = False) -> str:
    """Return the inference tier for a turn: ``"light"`` or ``"heavy"``.

    Tool-driven turns stay light — the tool does the work and a small model just
    formats the result. Otherwise length and complexity cues bump to heavy.
    """
    t = (text or "").strip()
    if has_tool_hints:
        return "light"
    if len(t.split()) > 60:
        return "heavy"
    if _HEAVY_HINTS.search(t):
        return "heavy"
    return "light"
