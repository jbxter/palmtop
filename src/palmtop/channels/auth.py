"""Shared sender authorization for messaging channels.

The per-channel allow-list is the primary trust boundary protecting the
privileged ``AgentLoop`` — which can read/send the owner's email, touch the
calendar, run deploys, and (via ``engine:``/``cursor:``) execute code.

Authorization fails **closed**: a channel with no allow-list configured
rejects *every* sender rather than accepting the whole internet. To run a
deliberately public bot, set ``allow_anyone=True`` on that channel, which
accepts all senders and logs a loud warning at startup.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Any


def sender_allowed(
    sender: Any,
    allowed: Collection[Any] | None,
    *,
    allow_anyone: bool,
) -> bool:
    """Return ``True`` iff ``sender`` is authorized to drive the agent.

    Fails closed: when no allow-list is configured (``allowed`` is empty or
    ``None``) and ``allow_anyone`` is ``False``, returns ``False``. Set
    ``allow_anyone=True`` to accept every sender.
    """
    if allow_anyone:
        return True
    if not allowed:
        return False
    return sender in allowed


def owner_key(user_id: str, source: str = "") -> str:
    """Channel-qualified identity used for owner checks.

    Channels that already prefix ``user_id`` (e.g. ``slack:U1``, ``sms:+1...``)
    pass it through unchanged; bare-ID channels (Telegram) pass ``source`` so the
    result matches how owners are listed in config (e.g. ``telegram:123``).
    """
    return f"{source}:{user_id}" if source else user_id


def log_access_policy(
    log: logging.Logger,
    channel: str,
    allowed: Collection[Any] | None,
    *,
    allow_anyone: bool,
) -> None:
    """Log the effective inbound access policy once, at construction.

    Emits a warning for the two risky states (open to everyone, or
    misconfigured so that nothing is authorized) and an info line for the
    normal case.
    """
    if allow_anyone:
        log.warning(
            "%s: allow_anyone=true — ACCEPTING MESSAGES FROM ANYONE. This channel "
            "can drive the agent, which has full access to your tools and data. "
            "Configure an allow-list unless a fully public bot is intended.",
            channel,
        )
    elif allowed:
        log.info("%s: authorizing %d configured sender(s)", channel, len(allowed))
    else:
        log.warning(
            "%s: no allow-list configured — refusing ALL inbound messages. Set the "
            "allow-list (e.g. allowed_users / allowed_numbers) to authorize senders, "
            "or set allow_anyone=true to accept everyone.",
            channel,
        )
