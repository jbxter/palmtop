"""Tests for the deploy approval (blessing) gate — issue #31.

Covers fail-closed behavior: blessing is bypassed only on an explicit opt-out
(gate=None), and is denied (never silently approved) when required but no
approval channel is wired.
"""

from __future__ import annotations

import pytest

from palmtop.core.blessing import BlessingGate
from palmtop.tools.deploy_blessing import request_deploy_blessing


@pytest.mark.asyncio
async def test_opt_out_when_gate_none():
    # gate=None means require_blessing=false — proceed without approval.
    ok = await request_deploy_blessing(None, None, "u1", platform="vercel", summary="deploy")
    assert ok is True


@pytest.mark.asyncio
async def test_fails_closed_when_no_send_fn():
    # Blessing required (gate present) but no channel to ask on → deny.
    gate = BlessingGate()
    ok = await request_deploy_blessing(gate, None, "u1", platform="vercel", summary="deploy prod")
    assert ok is False


@pytest.mark.asyncio
async def test_approved_when_owner_approves():
    gate = BlessingGate()

    async def approving_send(_uid, _msg):
        gate.approve()  # gate was armed by request_deploy_blessing before this runs

    ok = await request_deploy_blessing(gate, approving_send, "u1", platform="railway", summary="deploy")
    assert ok is True


@pytest.mark.asyncio
async def test_denied_when_owner_denies():
    gate = BlessingGate()

    async def denying_send(_uid, _msg):
        gate.deny()

    ok = await request_deploy_blessing(gate, denying_send, "u1", platform="railway", summary="deploy")
    assert ok is False


@pytest.mark.asyncio
async def test_send_failure_denies():
    gate = BlessingGate()

    async def failing_send(_uid, _msg):
        raise RuntimeError("telegram down")

    ok = await request_deploy_blessing(gate, failing_send, "u1", platform="vercel", summary="deploy")
    assert ok is False
