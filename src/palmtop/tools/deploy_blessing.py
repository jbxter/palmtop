"""Human approval for high-risk deploy actions (Vercel, Railway)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from palmtop.core.blessing import BlessingGate

log = logging.getLogger(__name__)


async def request_deploy_blessing(
    gate: BlessingGate | None,
    send_fn: Callable[[str, str], Awaitable[None]] | None,
    user_id: str,
    *,
    platform: str,
    summary: str,
) -> bool:
    """Send approval prompt to Telegram and block until /approve or /deny."""
    if not gate or not send_fn:
        return True

    # Arm the gate BEFORE prompting so /approve can't arrive before wait() starts.
    gate.prepare(summary)
    msg = f"\U0001f512 **{platform} deploy approval needed**\n\n{summary}\n\nReply /approve or /deny"
    await send_fn(user_id, msg)
    return await asyncio.to_thread(gate.wait)
