"""Lightweight post-generation checks for wrong-date and false-capability claims.

``check_*`` return lists of issue strings (for tracing). ``evaluate_response``
returns the reply, appending a correction only when a clear wrong-date claim is
found — it never rewrites a clean reply.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# "today is Monday", "today's ... Tuesday", etc.
_TODAY_WEEKDAY = re.compile(
    r"\btoday(?:'s|\s+is|\s+was)?\b[^.]*?\b(" + "|".join(_WEEKDAYS) + r")\b",
    re.I,
)

# False self-limitation claims the agent can actually do via tools.
_FALSE_LIMITS = re.compile(
    r"\bI (?:can'?t|cannot|am unable to|don'?t have (?:access|the ability))\b[^.]*?"
    r"\b(internet|web|calendar|email|search|your files)\b",
    re.I,
)


def _now(tz: ZoneInfo | None) -> datetime:
    return datetime.now(tz) if isinstance(tz, ZoneInfo) else datetime.now()


def check_date_claims(reply: str, tz: ZoneInfo | None = None) -> list[str]:
    actual = _now(tz).strftime("%A").lower()
    issues = []
    for m in _TODAY_WEEKDAY.finditer(reply or ""):
        claimed = m.group(1).lower()
        if claimed != actual:
            issues.append(f"Reply claims today is {claimed}, but it is {actual}.")
    return issues


def check_capability_claims(reply: str) -> list[str]:
    return [m.group(0).strip() for m in _FALSE_LIMITS.finditer(reply or "")]


def evaluate_response(reply: str, tz: ZoneInfo | None = None) -> str:
    """Return the reply, appending a date correction only when one is clearly wrong."""
    if not check_date_claims(reply, tz):
        return reply
    now = _now(tz)
    return reply.rstrip() + f"\n\n(Correction: today is {now:%A, %B} {now.day}, {now.year}.)"
