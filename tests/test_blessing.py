"""Tests for core/blessing.py — risk assessment, summary, and the approval gate."""

from __future__ import annotations

import threading
import time

from palmtop.core.blessing import (
    BlessingGate,
    RiskAssessment,
    assess_risk,
    build_approval_summary,
)

# ── assess_risk ──────────────────────────────────────────────────────────────


class TestAssessRisk:
    def test_benign_action_is_low(self):
        r = assess_risk("look up the weather in Austin")
        assert r.level == "low"
        assert not r.is_risky
        assert r.reasons == []

    def test_outbound_message_is_medium(self):
        r = assess_risk("send a Slack message to the team")
        assert r.level == "medium"
        assert r.is_risky
        assert "sends an outbound message" in r.reasons

    def test_destructive_is_high(self):
        r = assess_risk("delete the staging records")
        assert r.level == "high"
        assert "destructive operation" in r.reasons

    def test_production_is_high(self):
        r = assess_risk("push the change to production")
        assert r.level == "high"
        assert "touches production" in r.reasons

    def test_money_is_high(self):
        r = assess_risk("issue a refund to the customer")
        assert r.level == "high"
        assert "moves money" in r.reasons

    def test_reasons_are_deduped(self):
        r = assess_risk("delete delete delete the database")
        assert r.reasons.count("destructive operation") == 1

    def test_handles_empty_and_none(self):
        assert assess_risk("").level == "low"
        assert assess_risk(None).level == "low"  # type: ignore[arg-type]


# ── build_approval_summary ────────────────────────────────────────────────────


class TestBuildApprovalSummary:
    def test_contains_action_and_risk(self):
        s = build_approval_summary("deploy to production")
        assert "Action: deploy to production" in s
        assert "Risk: high" in s
        assert "touches production" in s

    def test_truncates_long_action(self):
        s = build_approval_summary("x" * 500)
        action_line = s.splitlines()[0]
        assert action_line.endswith("…")
        assert len(action_line) < 320

    def test_includes_alignment_when_given(self):
        s = build_approval_summary(
            "ship the feature",
            {"is_aligned": True, "score": 0.91, "matched_tags": ["q2-launch"]},
        )
        assert "Goal-aligned: yes" in s
        assert "score 0.91" in s
        assert "q2-launch" in s

    def test_alignment_not_aligned(self):
        s = build_approval_summary("random task", {"is_aligned": False})
        assert "Goal-aligned: no" in s

    def test_no_alignment_line_when_absent(self):
        s = build_approval_summary("simple task")
        assert "Goal-aligned" not in s

    def test_uses_provided_risk(self):
        risk = RiskAssessment(level="medium", reasons=["custom flag"])
        s = build_approval_summary("do it", risk=risk)
        assert "Risk: medium" in s
        assert "custom flag" in s


# ── BlessingGate ──────────────────────────────────────────────────────────────


class TestBlessingGate:
    def test_starts_not_pending(self):
        g = BlessingGate()
        assert not g.is_pending

    def test_prepare_arms_gate(self):
        g = BlessingGate()
        g.prepare("do the thing")
        assert g.is_pending
        assert g.summary == "do the thing"

    def test_approve_then_wait_returns_true(self):
        g = BlessingGate()
        g.prepare("x")
        g.approve()
        assert g.wait(timeout=1.0) is True
        assert not g.is_pending  # resolved

    def test_deny_then_wait_returns_false(self):
        g = BlessingGate()
        g.prepare("x")
        g.deny()
        assert g.wait(timeout=1.0) is False
        assert not g.is_pending

    def test_wait_without_pending_is_false(self):
        g = BlessingGate()
        assert g.wait(timeout=0.01) is False

    def test_timeout_fails_closed(self):
        g = BlessingGate()
        g.prepare("x")
        assert g.wait(timeout=0.01) is False
        assert not g.is_pending

    def test_approve_does_nothing_when_not_pending(self):
        g = BlessingGate()
        g.approve()  # should not raise or arm anything
        assert not g.is_pending

    def test_wait_blocks_until_approved_from_another_thread(self):
        g = BlessingGate()
        g.prepare("x")

        def approver():
            time.sleep(0.05)
            g.approve()

        threading.Thread(target=approver, daemon=True).start()
        assert g.wait(timeout=2.0) is True

    def test_request_one_shot_resolved_from_thread(self):
        g = BlessingGate()

        def approver():
            for _ in range(200):
                if g.is_pending:
                    g.deny()
                    return
                time.sleep(0.005)

        threading.Thread(target=approver, daemon=True).start()
        assert g.request("x", timeout=2.0) is False
