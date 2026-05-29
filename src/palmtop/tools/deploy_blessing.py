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
    # gate is None when the deploy tool was configured with require_blessing=false
    # — an explicit opt-out, so proceed.
    if gate is None:
        return True
    # Blessing IS required but there's no channel to ask on — fail closed (deny)
    # rather than silently approving an unapproved deploy.
    if send_fn is None:
        log.error("%s deploy requires approval but no approval channel is wired — denying", platform)
        return False

    # Arm the gate BEFORE prompting so /approve can't arrive before wait() starts.
    gate.prepare(summary)
    msg = f"\U0001f512 **{platform} deploy approval needed**\n\n{summary}\n\nReply /approve or /deny"
    try:
        await send_fn(user_id, msg)
    except Exception:
        log.exception("Failed to send deploy blessing prompt")
        gate.deny()
        return False
    return await asyncio.to_thread(gate.wait)
