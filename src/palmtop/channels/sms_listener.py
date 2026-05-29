"""SMS + RCS listener — runs alongside Telegram, not instead of it.

Polls the phone's native SMS inbox via Termux API every few seconds.
Also monitors Android notifications for RCS/chat messages from Google
Messages, since RCS bypasses the SMS content provider entirely.

New messages get routed through the same AgentLoop. Replies go back
as SMS, so they show up in Google Messages / Samsung Messages / etc.

Usage:
    listener = SmsListener(agent, allowed_numbers=["+15551234567"])
    listener.start()  # call from inside the running event loop

Authorization is by phone number (allowed_numbers) and fails closed: with
no allow-list set, no sender is accepted unless allow_anyone=True. RCS
notifications carry a display name, not a number, so the sender's number is
resolved (from the title or the device's own contacts) and checked against
allowed_numbers; the display name alone never grants access. allowed_sender_names
helps map a known contact name to its number for replies, not to authorize.

Requires Termux:API notification access for RCS (Android Settings →
Apps → Special access → Notification access → Termux:API).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from palmtop.channels.auth import log_access_policy, sender_allowed

if TYPE_CHECKING:
    from palmtop.core.loop import AgentLoop

log = logging.getLogger(__name__)

POLL_INTERVAL = 5  # seconds
NOTIFICATION_PROBE_TIMEOUT = 5  # fast fail when access missing / API hung
NOTIFICATION_POLL_TIMEOUT = 15
RCS_RETRY_INTERVAL = 300  # re-probe notification access every 5 minutes
MAX_SMS_LENGTH = 1600  # standard multi-part SMS limit

# Package names for Android messaging apps (RCS notification source)
MESSAGING_PACKAGES = {
    "com.google.android.apps.messaging",  # Google Messages
    "com.samsung.android.messaging",  # Samsung Messages
}


def _run_termux(cmd: list[str], timeout: int = 10) -> str:
    """Run a Termux API command synchronously."""
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {result.stderr.strip()}")
    return result.stdout


def _fetch_sms(limit: int = 20) -> list[dict]:
    raw = _run_termux(["termux-sms-list", "-l", str(limit), "-t", "inbox"])
    if not raw.strip():
        return []
    return json.loads(raw)


def _fetch_notifications(*, timeout: int = NOTIFICATION_POLL_TIMEOUT) -> list[dict]:
    """Fetch current Android notifications via Termux API."""
    raw = _run_termux(["termux-notification-list"], timeout=timeout)
    if not raw.strip():
        return []
    return json.loads(raw)


def _probe_notification_access() -> list[dict]:
    """Quick probe for notification listener access (short timeout)."""
    return _fetch_notifications(timeout=NOTIFICATION_PROBE_TIMEOUT)


def _fetch_contacts() -> dict[str, str]:
    """Build a lowercase contact-name → phone-number mapping."""
    raw = _run_termux(["termux-contact-list"], timeout=15)
    if not raw.strip():
        return {}
    contacts = json.loads(raw)
    return {c.get("name", "").lower(): c.get("number", "") for c in contacts if c.get("name") and c.get("number")}


def _send_sms(number: str, body: str) -> None:
    """Send SMS, splitting long messages."""
    # Strip HTML tags — SMS is plain text
    clean = re.sub(r"<[^>]+>", "", body)
    # Split into chunks if needed
    chunks = []
    while clean:
        if len(clean) <= MAX_SMS_LENGTH:
            chunks.append(clean)
            break
        split_at = clean.rfind("\n", 0, MAX_SMS_LENGTH)
        if split_at == -1:
            split_at = MAX_SMS_LENGTH
        chunks.append(clean[:split_at])
        clean = clean[split_at:].lstrip("\n")

    for chunk in chunks:
        _run_termux(["termux-sms-send", "-n", number, chunk])


class SmsListener:
    """Background SMS + RCS listener that coexists with Telegram."""

    def __init__(
        self,
        agent: AgentLoop,
        *,
        allowed_numbers: list[str] | None = None,
        allowed_sender_names: list[str] | None = None,
        allow_anyone: bool = False,
        telegram_send_fn: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._agent = agent
        self._allowed = {self._normalize(n) for n in allowed_numbers} if allowed_numbers else None
        self._allowed_names = {n.lower().strip() for n in allowed_sender_names} if allowed_sender_names else None
        self._allow_anyone = allow_anyone
        log_access_policy(log, "sms", self._allowed, allow_anyone=allow_anyone)
        self._telegram_send = telegram_send_fn
        self._seen: set[str] = set()
        self._task: asyncio.Task | None = None
        self._seeded = False
        self._contacts: dict[str, str] = {}  # name→number cache
        self._contacts_loaded = False
        self._rcs_available = False  # set True once notification list works
        self._last_rcs_probe = 0.0

    def start(self) -> None:
        """Start the SMS poll loop as a background asyncio task."""
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._poll_loop())
        log.info("SMS listener started (poll every %ds)", POLL_INTERVAL)

    async def _poll_loop(self) -> None:
        loop = asyncio.get_running_loop()

        # Seed with existing SMS so we don't reply to old ones
        try:
            existing = await loop.run_in_executor(None, _fetch_sms, 50)
            for msg in existing:
                self._seen.add(self._msg_id(msg))
            log.info("SMS seeded with %d existing messages", len(self._seen))
        except Exception:
            log.warning("SMS seed failed — will retry", exc_info=True)

        # Seed existing notifications so we don't reply to stale RCS
        await self._try_enable_rcs(loop, seed=True)

        # Load contacts for name→number resolution (RCS notifications
        # show contact names, not phone numbers)
        try:
            self._contacts = await loop.run_in_executor(None, _fetch_contacts)
            self._contacts_loaded = True
            log.info("Loaded %d contacts for RCS number resolution", len(self._contacts))
        except Exception:
            log.debug("Contact list unavailable", exc_info=True)

        self._seeded = True

        while True:
            try:
                await self._check_inbox()
            except Exception:
                log.debug("SMS poll error", exc_info=True)
            if self._rcs_available:
                try:
                    await self._check_rcs()
                except subprocess.TimeoutExpired:
                    log.warning(
                        "termux-notification-list timed out (%ds) — RCS poll skipped",
                        NOTIFICATION_POLL_TIMEOUT,
                    )
                    self._rcs_available = False
                except Exception:
                    log.debug("RCS poll error", exc_info=True)
            elif time.monotonic() - self._last_rcs_probe >= RCS_RETRY_INTERVAL:
                await self._try_enable_rcs(loop, seed=True)
            await asyncio.sleep(POLL_INTERVAL)

    async def _try_enable_rcs(self, loop: asyncio.AbstractEventLoop, *, seed: bool) -> None:
        """Probe Termux notification access; seed seen RCS keys on success."""
        self._last_rcs_probe = time.monotonic()
        try:
            notifs = await loop.run_in_executor(None, _probe_notification_access)
        except subprocess.TimeoutExpired:
            log.warning(
                "termux-notification-list timed out (%ds) — RCS disabled. "
                "In Android Settings → Special app access → Notification access, "
                "enable Termux:API (or run `termux-notification-list` in Termux to "
                "accept the prompt). Disable battery restrictions on Termux:API.",
                NOTIFICATION_PROBE_TIMEOUT,
            )
            return
        except RuntimeError as exc:
            log.warning("RCS disabled — %s", exc)
            return
        except Exception:
            log.warning(
                "Notification access unavailable — RCS disabled. "
                "Grant notification access to Termux:API in Android Settings.",
                exc_info=True,
            )
            return

        if seed:
            for n in notifs:
                if n.get("packageName") in MESSAGING_PACKAGES:
                    self._seen.add(f"rcs-{n.get('key', n.get('id', ''))}")
        self._rcs_available = True
        log.info("RCS notification listener active (%d notifications)", len(notifs))

    async def _check_inbox(self) -> None:
        loop = asyncio.get_running_loop()
        messages = await loop.run_in_executor(None, _fetch_sms)

        for msg in messages:
            mid = self._msg_id(msg)
            if mid in self._seen:
                continue
            self._seen.add(mid)

            if not self._seeded:
                continue  # skip messages from before we started

            number = msg.get("number", "") or msg.get("address", "")
            body = (msg.get("body") or "").strip()
            if not body or not number:
                continue

            if not self._number_allowed(number):
                log.debug("SMS from %s ignored (not in allowed list)", number)
                continue

            log.info("SMS from %s: %s", number, body[:80])

            # Route through agent
            try:
                reply = await self._agent.handle(body, user_id=f"sms:{number}")
            except Exception:
                log.exception("Agent error handling SMS from %s", number)
                reply = "Sorry, I hit an error processing that. Try again?"

            # Send reply via SMS
            log.info("SMS reply to %s: %s", number, reply[:80])
            try:
                await loop.run_in_executor(None, _send_sms, number, reply)
            except Exception:
                log.warning("SMS send failed to %s", number, exc_info=True)
                # Try Telegram as fallback notification
                if self._telegram_send:
                    try:
                        await self._telegram_send(
                            list(self._allowed)[0] if self._allowed else "default",
                            f"SMS reply failed to {number}. Reply was:\n{reply[:500]}",
                        )
                    except Exception:
                        pass

    @staticmethod
    def _normalize(number: str) -> str:
        """Normalize phone number for comparison."""
        digits = re.sub(r"[^\d+]", "", number)
        # Ensure +1 prefix for US numbers
        if digits and not digits.startswith("+"):
            if len(digits) == 10:
                digits = "+1" + digits
            elif len(digits) == 11 and digits.startswith("1"):
                digits = "+" + digits
        return digits

    @staticmethod
    def _msg_id(msg: dict) -> str:
        return f"{msg.get('_id', '')}-{msg.get('received', '')}-{msg.get('number', '')}"

    # ── RCS via notification listener ───────────────────────────────

    async def _check_rcs(self) -> None:
        """Check for RCS messages via Android notification listener."""
        loop = asyncio.get_running_loop()
        notifications = await loop.run_in_executor(None, _fetch_notifications)

        for notif in notifications:
            pkg = notif.get("packageName", "")
            if pkg not in MESSAGING_PACKAGES:
                continue

            nkey = f"rcs-{notif.get('key', notif.get('id', ''))}"
            if nkey in self._seen:
                continue
            self._seen.add(nkey)

            title = (notif.get("title") or "").strip()
            body = (notif.get("content") or "").strip()
            if not body:
                continue

            if not self._rcs_sender_allowed(title):
                log.debug("RCS from '%s' ignored (not an allowed sender)", title)
                continue

            number = self._resolve_sender_number(title)
            if not number:
                log.warning(
                    "RCS from '%s' — allowed sender but no reply number (add phone contact or allowed_numbers)",
                    title,
                )
                continue

            log.info("RCS from %s (%s): %s", title, number, body[:80])

            # Route through agent
            try:
                reply = await self._agent.handle(body, user_id=f"sms:{number}")
            except Exception:
                log.exception("Agent error handling RCS from %s", number)
                reply = "Sorry, I hit an error processing that. Try again?"

            # Reply via SMS (works for both SMS and RCS on the sending side)
            log.info("RCS reply to %s: %s", number, reply[:80])
            try:
                await loop.run_in_executor(None, _send_sms, number, reply)
            except Exception:
                log.warning("Reply failed to %s", number, exc_info=True)
                if self._telegram_send:
                    try:
                        await self._telegram_send(
                            list(self._allowed)[0] if self._allowed else "default",
                            f"📱 SMS reply failed to {number}. Reply was:\n{reply[:500]}",
                        )
                    except Exception:
                        pass

    def _number_allowed(self, number: str) -> bool:
        # Fail closed: an unset allow-list authorizes no one (unless allow_anyone).
        return sender_allowed(self._normalize(number), self._allowed, allow_anyone=self._allow_anyone)

    def _title_matches_allowed_name(self, title: str) -> bool:
        if not self._allowed_names:
            return False
        t = title.lower().strip()
        for name in self._allowed_names:
            if t == name or t.startswith(name + " ") or t.startswith(name + "("):
                return True
        return False

    def _rcs_sender_allowed(self, title: str) -> bool:
        """Authorize an RCS sender by VERIFIED phone number only.

        RCS notifications carry the sender's display name (the notification
        title), which is attacker-controllable, so it must never be the
        authorization token on its own. We authorize only when a phone number
        can be extracted directly from the notification title and that number
        is on the allow-list. Fails closed otherwise.
        """
        if self._allow_anyone:
            return True
        number = self._extract_number(title)
        return bool(number and self._number_allowed(number))

    def _resolve_sender_number(self, title: str) -> str | None:
        """Map notification title to a phone number for SMS reply."""
        raw = self._extract_number(title)
        if raw:
            return self._normalize(raw)

        key = title.lower().strip()
        if self._contacts_loaded and key in self._contacts:
            return self._normalize(self._contacts[key])

        if self._contacts_loaded and self._allowed_names:
            for contact_name, phone in self._contacts.items():
                if contact_name in self._allowed_names or any(
                    contact_name == n or contact_name.startswith(n + " ") for n in self._allowed_names
                ):
                    if key == contact_name or key.startswith(contact_name + " "):
                        return self._normalize(phone)

        # NOTE: deliberately no "title matches an allowed name → assume the
        # single allowed number" fallback. That grants access from a display
        # name with no number verification at all, and the title is spoofable.
        return None

    @staticmethod
    def _extract_number(text: str) -> str | None:
        """Return the text if it looks like a phone number, else None."""
        cleaned = re.sub(r"[\s\-\(\)]", "", text)
        if re.match(r"^\+?\d{10,15}$", cleaned):
            return cleaned
        return None

    async def send(self, number: str, text: str) -> None:
        """Send an SMS proactively (for monitor alerts, etc.)."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _send_sms, number, text)
