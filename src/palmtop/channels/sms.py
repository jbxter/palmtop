from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from typing import TYPE_CHECKING

from palmtop.channels.auth import log_access_policy, sender_allowed

if TYPE_CHECKING:
    from palmtop.core.loop import AgentLoop

log = logging.getLogger(__name__)

POLL_INTERVAL = 3  # seconds


def _normalize_number(number: str) -> str:
    """Normalize a phone number for comparison (mirror of SmsListener)."""
    digits = re.sub(r"[^\d+]", "", number)
    if digits and not digits.startswith("+"):
        if len(digits) == 10:
            digits = "+1" + digits
        elif len(digits) == 11 and digits.startswith("1"):
            digits = "+" + digits
    return digits


def _run_termux(cmd: list[str], timeout: int = 10) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {result.stderr.strip()}")
    return result.stdout


def _fetch_sms(limit: int = 10) -> list[dict]:
    raw = _run_termux(["termux-sms-list", "-l", str(limit), "-t", "inbox"])
    if not raw.strip():
        return []
    return json.loads(raw)


def _send_sms(number: str, body: str) -> None:
    _run_termux(["termux-sms-send", "-n", number, body])


class SmsChannel:
    def __init__(
        self,
        agent: AgentLoop,
        *,
        allowed_numbers: list[str] | None = None,
        allow_anyone: bool = False,
    ) -> None:
        self._agent = agent
        self._seen: set[str] = set()
        self._allowed = {_normalize_number(n) for n in allowed_numbers} if allowed_numbers else None
        self._allow_anyone = allow_anyone
        log_access_policy(log, "sms", self._allowed, allow_anyone=allow_anyone)

    def run(self, async_init=None) -> None:
        log.info("Starting SMS polling (every %ds)...", POLL_INTERVAL)
        asyncio.run(self._run(async_init))

    async def _run(self, async_init=None) -> None:
        if async_init:
            await async_init()
        await self._poll_loop()

    async def _poll_loop(self) -> None:
        # Seed seen set with current inbox so we don't reply to old messages
        for msg in _fetch_sms(50):
            self._seen.add(self._msg_id(msg))
        log.info("Seeded %d existing messages", len(self._seen))

        while True:
            try:
                await self._check_inbox()
            except Exception:
                log.exception("SMS poll error")
            await asyncio.sleep(POLL_INTERVAL)

    async def _check_inbox(self) -> None:
        messages = await asyncio.get_running_loop().run_in_executor(None, _fetch_sms)
        for msg in messages:
            mid = self._msg_id(msg)
            if mid in self._seen:
                continue
            self._seen.add(mid)

            number = msg.get("number", "")
            body = msg.get("body", "").strip()
            if not body:
                continue

            # Authorize the sender (fail closed when no allow-list is set)
            if not sender_allowed(_normalize_number(number), self._allowed, allow_anyone=self._allow_anyone):
                log.warning("Rejected SMS from unauthorized number %s", number)
                continue

            log.info("SMS from %s: %s", number, body[:80])
            reply = await self._agent.handle(body, user_id=f"sms:{_normalize_number(number)}")

            log.info("Replying to %s: %s", number, reply[:80])
            await asyncio.get_running_loop().run_in_executor(None, _send_sms, number, reply)

    @staticmethod
    def _msg_id(msg: dict) -> str:
        return f"{msg.get('received', '')}-{msg.get('number', '')}-{msg.get('body', '')[:64]}"
