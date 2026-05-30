"""Guardrail safety floor — the minimum policy that config/flags can't drop below.

The agent's runtime guardrails (deploy/cursor approval `require_blessing`, the
12WY alignment gate, channel allow-lists, autonomous mode) are otherwise chosen
entirely by launch parameters — CLI flags, env vars, and a caller-named config
path. Any capability that lets the running agent influence how it's (re)launched
could therefore select weaker parameters and disable its own guardrails (issue
#25).

The floor closes that by:
  - loading minimum policy from **secure code defaults**, tightened only by
    operator-controlled sources the agent's own tools can't write — process env
    (`PALMTOP_*`); the agent cannot set the environment of a process it doesn't
    launch, and `FileTool` is confined to `data_dir/docs`,
  - **clamping** the effective config up to the floor at startup (config/flags
    may only make the agent SAFER, never less safe),
  - requiring an explicit operator marker (`PALMTOP_ALLOW_UNSAFE=1`) to run
    below the floor, and audit-logging any such launch.

This is not OS-level tamper resistance (the agent runs as the same user); the
achievable guarantee is: no agent *tool* can lower a guardrail, and any launch
below the floor needs an operator signal + an audit trail.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from palmtop.config.settings import Config

log = logging.getLogger(__name__)

# Channel configs that carry an `allow_anyone` public-access flag.
_ALLOW_ANYONE_FIELDS = (
    "telegram",
    "discord",
    "slack",
    "matrix",
    "irc",
    "xmpp",
    "whatsapp",
    "sms",
    "email",
)

# Deploy/cursor configs that carry a `require_blessing` approval flag.
_REQUIRE_BLESSING_FIELDS = ("cursor", "vercel", "railway")


def _env_flag(env: dict, name: str, default: bool) -> bool:
    v = env.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class SafetyFloor:
    """Minimum guardrail policy. Config may only be clamped *toward* these."""

    require_blessing: bool = True  # *.require_blessing can't be disabled below this
    allow_autonomous: bool = False  # --autonomous / PALMTOP_AUTONOMOUS needs this (or allow_unsafe)
    pin_alignment_hard: bool = False  # if True, alignment.mode is forced to "hard"
    forbid_public_channels: bool = False  # if True, allow_anyone is forced off
    allow_unsafe: bool = False  # operator escape hatch: permit running below the floor (audited)

    @classmethod
    def load(cls, env: dict | None = None) -> SafetyFloor:
        """Load the floor from secure defaults, tightened by operator env vars."""
        e = env if env is not None else os.environ
        return cls(
            require_blessing=_env_flag(e, "PALMTOP_FLOOR_REQUIRE_BLESSING", True),
            allow_autonomous=_env_flag(e, "PALMTOP_ALLOW_AUTONOMOUS", False),
            pin_alignment_hard=_env_flag(e, "PALMTOP_FLOOR_REQUIRE_ALIGNMENT", False),
            forbid_public_channels=_env_flag(e, "PALMTOP_FLOOR_NO_PUBLIC_CHANNELS", False),
            allow_unsafe=_env_flag(e, "PALMTOP_ALLOW_UNSAFE", False),
        )

    def autonomous_permitted(self) -> bool:
        """Whether --autonomous / PALMTOP_AUTONOMOUS may be honored."""
        return self.allow_autonomous or self.allow_unsafe


def clamp_config(cfg: Config, floor: SafetyFloor) -> list[str]:
    """Force `cfg` to be no weaker than `floor`. Returns human-readable clamps.

    With ``floor.allow_unsafe`` set, nothing is clamped — but the violations are
    still returned (prefixed ``ALLOWED-UNSAFE``) so the caller can audit/alert.
    """
    violations: list[str] = []

    def record(field: str, frm, to) -> None:
        violations.append(f"{field}: {frm!r} -> {to!r}")

    if floor.require_blessing:
        for name in _REQUIRE_BLESSING_FIELDS:
            sub = getattr(cfg, name, None)
            if sub is not None and getattr(sub, "require_blessing", True) is False:
                record(f"{name}.require_blessing", False, True)
                if not floor.allow_unsafe:
                    sub.require_blessing = True

    if floor.forbid_public_channels:
        for name in _ALLOW_ANYONE_FIELDS:
            sub = getattr(cfg, name, None)
            if sub is not None and getattr(sub, "allow_anyone", False) is True:
                record(f"{name}.allow_anyone", True, False)
                if not floor.allow_unsafe:
                    sub.allow_anyone = False

    if floor.pin_alignment_hard and getattr(cfg.alignment, "mode", "hard") != "hard":
        record("alignment.mode", cfg.alignment.mode, "hard")
        if not floor.allow_unsafe:
            cfg.alignment.mode = "hard"

    if violations:
        tag = "ALLOWED-UNSAFE" if floor.allow_unsafe else "CLAMPED"
        for v in violations:
            log.warning("SAFETY FLOOR %s — %s", tag, v)

    return [(("ALLOWED-UNSAFE " if floor.allow_unsafe else "") + v) for v in violations]


def goals_path_is_agent_writable(goals_path: str | Path, data_dir: str | Path) -> bool:
    """True if the goals file sits inside the agent-writable FileTool sandbox.

    FileTool is confined to ``data_dir/docs``; a goals file (or its cache
    sibling) under that tree can be overwritten by the agent to defeat the
    alignment guard, so it must not be trusted as authoritative.
    """
    sandbox = (Path(data_dir) / "docs").resolve()
    try:
        Path(goals_path).resolve().relative_to(sandbox)
        return True
    except ValueError:
        return False


def audit_safety(data_dir: str | Path, record: dict) -> None:
    """Append a safety event to ``data_dir/safety_audit.jsonl``.

    This lives in ``data_dir`` (not ``data_dir/docs``), so it is outside the
    FileTool sandbox and the agent's tools cannot rewrite the audit trail.
    """
    path = Path(data_dir) / "safety_audit.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now(UTC).isoformat(), **record}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        log.debug("Failed to write safety audit", exc_info=True)
