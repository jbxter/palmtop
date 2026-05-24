"""Human approval for high-risk deploy actions (Vercel, Railway)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from palmtop.core.blessing import BlessingGate

log = logging.getLogger(__name__)


async def request_deploy_blessing(
    gate: "BlessingGate | None",
    send_fn: Callable[[str, str], Awaitable[None]] | None,
    user_id: str,
    *,
    platform: str,
    summary: str,
) -> bool:
    """Send approval prompt to Telegram and block until /approve or /deny."""
    if not gate or not send_fn:
        return True

    async def _send_approval() -> None:
        msg = (
            f"\U0001f512 **{platform} deploy approval needed**\n\n"
            f"{summary}\n\n"
            "Reply /approve or /deny"
        )
        await send_fn(user_id, msg)

    await _send_approval()
    return await asyncio.to_thread(gate.request, summary)
