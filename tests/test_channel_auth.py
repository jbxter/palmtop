"""Tests for fail-closed channel authorization.

Covers the shared helper (palmtop.channels.auth) and the channel-level
behavior for the auth-hardening fixes:
  - empty/unset allow-list must reject (fail closed), not accept everyone
  - allow_anyone=True is the explicit opt-in for a public bot
  - legacy SMS channel now authorizes the sender
  - RCS is authorized by phone number, never by a spoofable display name
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from palmtop.channels.auth import log_access_policy, owner_key, sender_allowed


class TestSenderAllowed:
    def test_fails_closed_when_unset(self):
        # No allow-list configured and no opt-in → reject.
        assert sender_allowed("alice", None, allow_anyone=False) is False
        assert sender_allowed("alice", set(), allow_anyone=False) is False

    def test_allow_anyone_accepts_everything(self):
        assert sender_allowed("anyone", None, allow_anyone=True) is True
        assert sender_allowed("anyone", set(), allow_anyone=True) is True
        assert sender_allowed(999, {1, 2}, allow_anyone=True) is True

    def test_membership_when_configured(self):
        assert sender_allowed("alice", {"alice", "bob"}, allow_anyone=False) is True
        assert sender_allowed("eve", {"alice", "bob"}, allow_anyone=False) is False

    def test_works_with_int_ids(self):
        # Telegram/Discord use integer IDs.
        assert sender_allowed(123, {123, 456}, allow_anyone=False) is True
        assert sender_allowed(789, {123, 456}, allow_anyone=False) is False


class TestLogAccessPolicy:
    def test_warns_when_unconfigured(self, caplog):
        log = logging.getLogger("test.auth.unconfigured")
        with caplog.at_level(logging.WARNING):
            log_access_policy(log, "telegram", None, allow_anyone=False)
        assert any("refusing ALL inbound" in r.message for r in caplog.records)

    def test_warns_loudly_when_open(self, caplog):
        log = logging.getLogger("test.auth.open")
        with caplog.at_level(logging.WARNING):
            log_access_policy(log, "telegram", None, allow_anyone=True)
        assert any("ANYONE" in r.message for r in caplog.records)

    def test_info_when_configured(self, caplog):
        log = logging.getLogger("test.auth.configured")
        with caplog.at_level(logging.INFO):
            log_access_policy(log, "telegram", {1, 2}, allow_anyone=False)
        # No warning about refusing/open when an allow-list is set.
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)


class TestIrcFailClosed:
    """IRC stands in for the shared guard used by every standard channel."""

    def _channel(self, **kwargs):
        from palmtop.channels.irc import IrcChannel

        ch = IrcChannel(server="irc.test", nick="palmtop", **kwargs)
        ch._agent = AsyncMock()
        ch._agent.handle = AsyncMock(return_value="reply")
        ch._writer = AsyncMock()
        ch._writer.write = MagicMock()
        ch._writer.drain = AsyncMock()
        ch._connected = True
        return ch

    @pytest.mark.asyncio
    async def test_unconfigured_rejects(self):
        ch = self._channel()  # no allowed_users
        await ch._on_privmsg("alice!u@h", "palmtop :hello")
        ch._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_allow_anyone_accepts(self):
        ch = self._channel(allow_anyone=True)
        await ch._on_privmsg("stranger!u@h", "palmtop :hello")
        ch._agent.handle.assert_called_once()

    @pytest.mark.asyncio
    async def test_allowlisted_user_accepted(self):
        ch = self._channel(allowed_users=["alice"])
        await ch._on_privmsg("alice!u@h", "palmtop :hello")
        ch._agent.handle.assert_called_once()


class TestLegacySmsFailClosed:
    """Legacy SmsChannel (single-channel phone default) — issue #27."""

    async def _run_check(self, monkeypatch, *, number, **kwargs):
        from palmtop.channels import sms as sms_mod
        from palmtop.channels.sms import SmsChannel

        ch = SmsChannel(MagicMock(), **kwargs)
        ch._agent = AsyncMock()
        ch._agent.handle = AsyncMock(return_value="reply")

        msg = {"_id": "1", "received": "t", "number": number, "body": "hello"}
        monkeypatch.setattr(sms_mod, "_fetch_sms", lambda *a, **k: [msg])
        monkeypatch.setattr(sms_mod, "_send_sms", lambda *a, **k: None)
        await ch._check_inbox()
        return ch

    @pytest.mark.asyncio
    async def test_unconfigured_rejects(self, monkeypatch):
        ch = await self._run_check(monkeypatch, number="+15551234567")
        ch._agent.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_allowlisted_number_accepted(self, monkeypatch):
        ch = await self._run_check(monkeypatch, number="+15551234567", allowed_numbers=["+15551234567"])
        ch._agent.handle.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_allowlisted_number_rejected(self, monkeypatch):
        ch = await self._run_check(monkeypatch, number="+19998887777", allowed_numbers=["+15551234567"])
        ch._agent.handle.assert_not_called()


class TestSmsListenerNumberAuth:
    """SmsListener number/RCS authorization — issues #26 (fail-open) & #32 (RCS)."""

    def _listener(self, **kwargs):
        from palmtop.channels.sms_listener import SmsListener

        return SmsListener(MagicMock(), **kwargs)

    def test_number_allowed_fails_closed(self):
        sl = self._listener()  # no allowed_numbers
        assert sl._number_allowed("+15551234567") is False

    def test_number_allowed_membership(self):
        sl = self._listener(allowed_numbers=["+15551234567"])
        assert sl._number_allowed("+15551234567") is True
        assert sl._number_allowed("+19998887777") is False

    def test_number_allow_anyone(self):
        sl = self._listener(allow_anyone=True)
        assert sl._number_allowed("+10000000000") is True

    def test_rcs_rejects_spoofable_display_name(self):
        # A title that is just a display name (no resolvable number) must NOT
        # authorize, even if it matches an allowed_sender_names entry.
        sl = self._listener(
            allowed_numbers=["+15551234567"],
            allowed_sender_names=["the owner"],
        )
        assert sl._rcs_sender_allowed("The Owner") is False

    def test_rcs_accepts_allowed_number_in_title(self):
        sl = self._listener(allowed_numbers=["+15551234567"])
        assert sl._rcs_sender_allowed("+15551234567") is True

    def test_rcs_rejects_contact_name_mapped_number_for_auth(self):
        sl = self._listener(allowed_numbers=["+15551234567"])
        sl._contacts_loaded = True
        sl._contacts = {"the owner": "+15551234567"}
        assert sl._rcs_sender_allowed("The Owner") is False

    def test_rcs_rejects_unknown_number_in_title(self):
        sl = self._listener(allowed_numbers=["+15551234567"])
        assert sl._rcs_sender_allowed("+19998887777") is False


class TestOwnerKey:
    """Channel-qualified identity used to match against [agent] owners."""

    def test_bare_id_qualified_with_source(self):
        # Telegram passes a bare id + source → "telegram:123".
        assert owner_key("123", "telegram") == "telegram:123"

    def test_prefixed_id_passthrough(self):
        # Other channels already prefix; no source → unchanged.
        assert owner_key("slack:U1") == "slack:U1"
        assert owner_key("sms:+15551234567") == "sms:+15551234567"


class TestOwnerGate:
    """engine:/cursor: restricted to configured owners — issue #29."""

    def _loop(self, owners):
        # The full palmtop.core engine package isn't always present in a
        # checkout; skip (don't error) when AgentLoop can't be imported.
        pytest.importorskip("palmtop.core.loop")
        from palmtop.core.loop import AgentLoop

        return AgentLoop(AsyncMock(), owner_ids=owners)

    @pytest.mark.asyncio
    async def test_fails_closed_without_owners(self):
        loop = self._loop(set())
        reply = await loop.run_sovereign_engine("do x", user_id="123", source="telegram")
        assert "Not authorized" in reply

    @pytest.mark.asyncio
    async def test_non_owner_refused(self):
        loop = self._loop({"telegram:999"})
        reply = await loop.run_sovereign_engine("do x", user_id="123", source="telegram")
        assert "Not authorized" in reply

    @pytest.mark.asyncio
    async def test_owner_passes_gate(self):
        # Owner clears the gate; falls through to the "engine disabled" message
        # (no sovereign engine wired) — the point is it is NOT "Not authorized".
        loop = self._loop({"telegram:123"})
        reply = await loop.run_sovereign_engine("do x", user_id="123", source="telegram")
        assert "Not authorized" not in reply

    @pytest.mark.asyncio
    async def test_prefixed_channel_owner_matches_without_source(self):
        # Channels other than Telegram already pass a channel-qualified user_id.
        loop = self._loop({"slack:U1"})
        reply = await loop.run_cursor_delegate("do x", user_id="slack:U1")
        assert "Not authorized" not in reply

    @pytest.mark.asyncio
    async def test_engine_prefix_path_enforces_owner(self):
        # The "engine: ..." prefix typed as a normal message is gated too.
        loop = self._loop(set())
        reply = await loop.handle("engine: do x", user_id="sms:+15551234567")
        assert "Not authorized" in reply


class TestTelegramApprovalOwner:
    """/approve and /deny are restricted to owners when owners are set (#31)."""

    def _channel(self, owner_ids):
        pytest.importorskip("telegram")
        from palmtop.channels.telegram import TelegramChannel

        return TelegramChannel("123:abc", MagicMock(), allowed_users=[123], owner_ids=owner_ids)

    def test_owner_only_when_owners_configured(self):
        ch = self._channel({"telegram:123"})
        assert ch._can_approve(123) is True
        assert ch._can_approve(999) is False  # admitted by allow-list, but not an owner

    def test_falls_back_to_allowlist_when_no_owners(self):
        # No owners configured → defer to the channel allow-list (caller-enforced).
        ch = self._channel(set())
        assert ch._can_approve(123) is True
        assert ch._can_approve(999) is True
