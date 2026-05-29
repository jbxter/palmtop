"""Automated lead qualification and outreach.

When a lead submits the intake form, this module:
1. Qualifies the lead using the LLM (real project? budget? scope?)
2. If qualified, drafts a personalized follow-up email
3. Wraps it in a branded HTML template
4. Sends it from the agent's email inbox with relevant booking links
5. Reports the outreach via notification (e.g. Telegram)

SECURITY: Runs in the app layer (trusted code), NOT in the sandboxed
WebAgent.  The LLM is used for content generation only — no tool dispatch.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from palmtop.brand import build_email_html
from palmtop.inference.base import InferenceBackend, Message
from palmtop.persona import PersonaConfig
from palmtop.tools.email import EmailTool

log = logging.getLogger(__name__)

# Safety throttle: cap auto-outreach emails per UTC day. The recipient is an
# unverified, attacker-supplied address, so this bounds spam amplification even
# if per-IP form rate limits are evaded (e.g. rotating IPs).
DEFAULT_DAILY_OUTREACH_CAP = 25


# ── LLM prompt templates ─────────────────────────────────────────────

QUALIFY_PROMPT = """\
You are evaluating a lead from a freelance software development practice.  \
Determine if this is a qualified lead worth an automated follow-up email.

QUALIFIED if ANY of these are true:
- They describe a real software project (not just "testing" or gibberish)
- They mention a budget or timeline
- Their project aligns with software development services

NOT QUALIFIED if:
- The project description is nonsense, spam, or clearly fake
- They're asking for free help with no project intent
- The submission looks like a bot or automated spam

Lead info:
Name: {name}
Email: {email}
Project: {project}
Budget: {budget}
Timeline: {timeline}
Referral: {referral}

Respond with EXACTLY one word: QUALIFIED or UNQUALIFIED\
"""

DRAFT_EMAIL_PROMPT = """\
You are {agent_name} — writing a follow-up email to a new lead who \
just submitted a project inquiry.

Tone: casual, warm — never corporate or salesy.  Write like a real \
person, not a template.

The email should:
1. Thank them by first name for reaching out
2. Reference their specific project briefly (show you actually read it)
3. Give a short, honest reaction — sounds interesting, doable, etc.
4. Mention that {owner_name} would love to chat more and suggest they \
book some time (don't include the actual links — those will be added \
as styled buttons below your text automatically)
5. Sign off warmly — just your name, no title

DO NOT include any URLs, links, or booking links in your text — they \
are added automatically in the email template.

Lead info:
Name: {name}
Project: {project}
Budget: {budget}
Timeline: {timeline}

Write ONLY the email body — no subject line, no headers, no markdown \
formatting, no links.  Keep it concise: 3-4 short paragraphs max.  \
Use plain paragraph breaks between paragraphs (no bullet points).\
"""


@dataclass
class LeadInfo:
    """Data from the intake form submission."""

    name: str
    email: str
    project: str
    budget: str = ""
    timeline: str = ""
    referral: str = ""


class LeadOutreach:
    """Qualifies intake leads and sends automated outreach emails."""

    def __init__(
        self,
        llm: InferenceBackend,
        email_tool: EmailTool,
        notify_fn: Callable[[str, str], Awaitable[None]] | None = None,
        notify_user_id: str = "",
        persona: PersonaConfig | None = None,
        daily_cap: int = DEFAULT_DAILY_OUTREACH_CAP,
    ) -> None:
        self._llm = llm
        self._email = email_tool
        self._notify_fn = notify_fn
        self._notify_user_id = notify_user_id
        self._persona = persona or PersonaConfig()
        self._daily_cap = daily_cap
        self._sent_day = ""  # UTC date string of the current count window
        self._sent_count = 0

    def _within_daily_cap(self) -> bool:
        """Reserve one auto-outreach slot for today; False if the cap is reached."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if today != self._sent_day:
            self._sent_day = today
            self._sent_count = 0
        if self._sent_count >= self._daily_cap:
            return False
        self._sent_count += 1
        return True

    @property
    def _booking_options(self) -> list[dict]:
        """Booking link dicts from persona config."""
        if not self._persona.booking:
            return []
        return [
            {
                "name": b.name,
                "duration": b.duration,
                "url": b.url,
                "desc": b.desc,
            }
            for b in self._persona.booking
        ]

    async def process_lead(self, lead: LeadInfo) -> bool:
        """Qualify a lead and send outreach if qualified.

        Returns True if outreach email was sent, False otherwise.
        """
        # Step 1: Qualify
        qualified = await self._qualify(lead)
        if not qualified:
            log.info(
                "Lead %s <%s> not qualified — skipping outreach",
                lead.name,
                lead.email,
            )
            await self._notify(
                f"ℹ️ Lead from {lead.name} <{lead.email}> was not "
                f"qualified for auto-outreach.\n"
                f"Project: {lead.project[:200]}"
            )
            return False

        # Step 1b: Daily cap — bounds spam amplification to attacker-supplied
        # addresses even if per-IP form limits are evaded.
        if not self._within_daily_cap():
            log.warning("Daily auto-outreach cap (%d) reached — not emailing %s", self._daily_cap, lead.email)
            await self._notify(
                f"⚠️ Daily auto-outreach cap ({self._daily_cap}) reached — did NOT email "
                f"{lead.name} <{lead.email}>. Follow up manually if it's a real lead."
            )
            return False

        # Step 2: Draft personalized email body (plain text)
        email_body = await self._draft_email(lead)
        if not email_body:
            log.warning("Failed to draft outreach email for %s", lead.email)
            return False

        # Step 3: Build branded HTML version (with booking CTAs)
        booking = self._booking_options
        email_html = build_email_html(
            email_body,
            booking_options=booking or None,
            persona=self._persona,
        )

        # Step 4: Send from the agent's email inbox
        subject = f"Hey {lead.name.split()[0]} — got your project inquiry"
        msg_id = await self._email.send_email(
            to=lead.email,
            subject=subject,
            body=email_body,
            html=email_html,
        )

        if not msg_id:
            log.warning("Failed to send outreach to %s", lead.email)
            return False

        log.info(
            "OUTREACH AUDIT sent to=%s name=%s msg=%s (%d/%d today)",
            lead.email,
            lead.name,
            msg_id,
            self._sent_count,
            self._daily_cap,
        )

        # Step 5: Notify about the auto-outreach
        await self._notify(
            f"\U0001f4e7 **Auto-outreach sent** to {lead.name} <{lead.email}>\n\n"
            f"**Subject:** {subject}\n\n"
            f"{email_body[:500]}"
        )

        return True

    async def _qualify(self, lead: LeadInfo) -> bool:
        """Use LLM to determine if this lead is worth following up on."""
        prompt = QUALIFY_PROMPT.format(
            name=lead.name,
            email=lead.email,
            project=lead.project,
            budget=lead.budget or "Not specified",
            timeline=lead.timeline or "Not specified",
            referral=lead.referral or "Not specified",
        )
        try:
            result = await self._llm.complete(
                [Message(role="user", content=prompt)],
                max_tokens=10,
            )
            return "QUALIFIED" in result.upper()
        except Exception:
            log.exception("Lead qualification LLM call failed")
            # Fail closed: a qualification error must NOT auto-send to an
            # unverified, attacker-supplied address. The owner is still notified
            # of the unqualified lead and can follow up manually.
            return False

    async def _draft_email(self, lead: LeadInfo) -> str | None:
        """Use LLM to draft a personalized follow-up email."""
        owner = self._persona.owner_name or "the team"
        prompt = DRAFT_EMAIL_PROMPT.format(
            agent_name=self._persona.name,
            owner_name=owner,
            name=lead.name,
            project=lead.project,
            budget=lead.budget or "Not specified",
            timeline=lead.timeline or "Not specified",
        )
        try:
            result = await self._llm.complete(
                [Message(role="user", content=prompt)],
                max_tokens=800,
            )
            return result.strip()
        except Exception:
            log.exception("Email draft LLM call failed for %s", lead.email)
            return None

    async def _notify(self, text: str) -> None:
        """Send a notification via the configured channel."""
        if self._notify_fn and self._notify_user_id:
            try:
                await self._notify_fn(self._notify_user_id, text)
            except Exception:
                log.warning("Failed to send outreach notification", exc_info=True)
