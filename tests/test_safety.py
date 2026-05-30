"""Tests for the guardrail safety floor — issue #25."""

from __future__ import annotations

from palmtop.config.settings import Config
from palmtop.core.safety import (
    SafetyFloor,
    clamp_config,
    goals_path_is_agent_writable,
)


class TestSafetyFloor:
    def test_defaults_are_secure(self):
        f = SafetyFloor.load({})
        assert f.require_blessing is True
        assert f.allow_autonomous is False
        assert f.allow_unsafe is False
        assert f.autonomous_permitted() is False

    def test_env_overrides(self):
        f = SafetyFloor.load({"PALMTOP_ALLOW_AUTONOMOUS": "1", "PALMTOP_ALLOW_UNSAFE": "true"})
        assert f.allow_autonomous is True
        assert f.allow_unsafe is True
        assert f.autonomous_permitted() is True

    def test_autonomous_permitted_via_unsafe(self):
        assert SafetyFloor(allow_unsafe=True).autonomous_permitted() is True


class TestClampConfig:
    def test_clamps_require_blessing_up(self):
        cfg = Config()
        cfg.cursor.require_blessing = False
        cfg.vercel.require_blessing = False
        clamps = clamp_config(cfg, SafetyFloor(require_blessing=True))
        assert cfg.cursor.require_blessing is True
        assert cfg.vercel.require_blessing is True
        assert any("cursor.require_blessing" in c for c in clamps)

    def test_allow_unsafe_records_but_does_not_clamp(self):
        cfg = Config()
        cfg.cursor.require_blessing = False
        clamps = clamp_config(cfg, SafetyFloor(require_blessing=True, allow_unsafe=True))
        assert cfg.cursor.require_blessing is False  # left as-is under the escape hatch
        assert any("ALLOWED-UNSAFE" in c for c in clamps)  # but recorded for audit

    def test_forbid_public_channels(self):
        cfg = Config()
        cfg.telegram.allow_anyone = True
        cfg.sms.allow_anyone = True
        clamp_config(cfg, SafetyFloor(forbid_public_channels=True))
        assert cfg.telegram.allow_anyone is False
        assert cfg.sms.allow_anyone is False

    def test_pin_alignment_hard(self):
        cfg = Config()
        cfg.alignment.mode = "soft"
        clamp_config(cfg, SafetyFloor(pin_alignment_hard=True))
        assert cfg.alignment.mode == "hard"

    def test_no_clamp_when_already_safe(self):
        # Default config already meets the default floor.
        assert clamp_config(Config(), SafetyFloor()) == []


class TestGoalsLocation:
    def test_sandbox_goals_flagged(self):
        assert goals_path_is_agent_writable("data/docs/plans/twy_goals.json", "data") is True
        assert goals_path_is_agent_writable("data/docs/.twy_goals.cache.json", "data") is True

    def test_goals_outside_sandbox_ok(self):
        assert goals_path_is_agent_writable("docs/plans/twy_goals.json", "data") is False
