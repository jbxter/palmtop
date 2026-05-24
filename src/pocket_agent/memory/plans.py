from __future__ import annotations

import aiosqlite
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

PLAN_PATTERN = re.compile(
    r"\[PLAN(?::(\w+))?\]\s*(.+?)(?=\n\[PLAN|\n\[/PLAN\]|\Z)",
    re.DOTALL,
)
STEP_PATTERN = re.compile(r"^\s*[-*]\s*\[([ xX])\]\s*(.+)$", re.MULTILINE)
CLOSE_PATTERN = re.compile(r"\[/PLAN\]")

PLAN_INSTRUCTIONS = """\

Plans:
When the user asks you to plan, organize, or break something into steps, \
output a plan using this format:

[PLAN] Title of the plan
- [ ] First step
- [ ] Second step
- [ ] Third step

To update step status in an existing plan, use:
[PLAN:update] Title of the plan
- [x] Completed step
- [ ] Still pending step

To close a completed plan:
[/PLAN] Title of the plan

Keep plans focused — 3 to 8 steps. You can always add steps later. \
Reference active plans naturally when relevant."""


@dataclass
class PlanStep:
    id: int
    description: str
    status: str  # pending, done
    position: int


@dataclass
class Plan:
    id: int
    user_id: str
    title: str
    status: str  # active, completed
    created_at: str
    updated_at: str = ""
    steps: list[PlanStep] = field(default_factory=list)


class PlanMemory:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS plan_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                position INTEGER NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (plan_id) REFERENCES plans(id)
            )
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_plans_user
            ON plans(user_id, status)
        """)
        await self._db.commit()
        log.info("Plan memory ready: %s", self._db_path)

    async def create_plan(
        self, user_id: str, title: str, steps: list[str]
    ) -> Plan:
        existing = await self._find_plan(user_id, title)
        if existing:
            return await self._update_plan_steps(existing, steps)

        cursor = await self._db.execute(
            "INSERT INTO plans (user_id, title) VALUES (?, ?)",
            (user_id, title.strip()),
        )
        plan_id = cursor.lastrowid
        plan_steps = []
        for i, desc in enumerate(steps):
            await self._db.execute(
                "INSERT INTO plan_steps (plan_id, description, position) VALUES (?, ?, ?)",
                (plan_id, desc.strip(), i),
            )
            plan_steps.append(PlanStep(id=0, description=desc.strip(), status="pending", position=i))
        await self._db.commit()
        log.info("Created plan: %s (%d steps)", title, len(steps))
        return Plan(id=plan_id, user_id=user_id, title=title.strip(),
                    status="active", created_at="", steps=plan_steps)

    async def _find_plan(self, user_id: str, title: str) -> Plan | None:
        cursor = await self._db.execute(
            """SELECT id, user_id, title, status, created_at FROM plans
               WHERE user_id = ? AND status = 'active'
               AND LOWER(title) = LOWER(?)""",
            (user_id, title.strip()),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        plan = Plan(id=row[0], user_id=row[1], title=row[2], status=row[3], created_at=row[4])
        plan.steps = await self._load_steps(plan.id)
        return plan

    async def _load_steps(self, plan_id: int) -> list[PlanStep]:
        cursor = await self._db.execute(
            """SELECT id, description, status, position FROM plan_steps
               WHERE plan_id = ? ORDER BY position""",
            (plan_id,),
        )
        rows = await cursor.fetchall()
        return [PlanStep(id=r[0], description=r[1], status=r[2], position=r[3]) for r in rows]

    async def _update_plan_steps(self, plan: Plan, step_texts: list[str]) -> Plan:
        for step_text in step_texts:
            matched = False
            for existing in plan.steps:
                if _steps_match(existing.description, step_text):
                    matched = True
                    break
            if not matched:
                pos = len(plan.steps)
                await self._db.execute(
                    "INSERT INTO plan_steps (plan_id, description, position) VALUES (?, ?, ?)",
                    (plan.id, step_text.strip(), pos),
                )
                plan.steps.append(PlanStep(id=0, description=step_text.strip(), status="pending", position=pos))
        await self._db.execute(
            "UPDATE plans SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (plan.id,)
        )
        await self._db.commit()
        log.info("Updated plan: %s", plan.title)
        return plan

    async def mark_steps_done(self, plan_id: int, descriptions: list[str]) -> None:
        for desc in descriptions:
            await self._db.execute(
                """UPDATE plan_steps SET status = 'done', updated_at = CURRENT_TIMESTAMP
                   WHERE plan_id = ? AND LOWER(description) LIKE ?""",
                (plan_id, f"%{desc.strip().lower()[:40]}%"),
            )
        await self._db.execute(
            "UPDATE plans SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (plan_id,)
        )
        await self._db.commit()

    async def close_plan(self, user_id: str, title: str) -> None:
        await self._db.execute(
            """UPDATE plans SET status = 'completed', updated_at = CURRENT_TIMESTAMP
               WHERE user_id = ? AND LOWER(title) = LOWER(?) AND status = 'active'""",
            (user_id, title.strip()),
        )
        await self._db.commit()
        log.info("Closed plan: %s", title)

    async def get_active_plans(self, user_id: str) -> list[Plan]:
        # Single query: join plans + steps to avoid N+1
        cursor = await self._db.execute(
            """SELECT p.id, p.user_id, p.title, p.status, p.created_at,
                      p.updated_at,
                      s.id, s.description, s.status, s.position
               FROM plans p
               LEFT JOIN plan_steps s ON s.plan_id = p.id
               WHERE p.user_id = ? AND p.status = 'active'
               ORDER BY p.updated_at DESC, s.position ASC""",
            (user_id,),
        )
        rows = await cursor.fetchall()
        plans_map: dict[int, Plan] = {}
        plan_order: list[int] = []
        for r in rows:
            pid = r[0]
            if pid not in plans_map:
                plans_map[pid] = Plan(
                    id=pid, user_id=r[1], title=r[2], status=r[3],
                    created_at=r[4], updated_at=r[5] or "",
                )
                plan_order.append(pid)
            if r[6] is not None:  # has a step
                plans_map[pid].steps.append(
                    PlanStep(id=r[6], description=r[7], status=r[8], position=r[9])
                )
        return [plans_map[pid] for pid in plan_order]

    async def close(self) -> None:
        if self._db:
            await self._db.close()


def _steps_match(existing: str, new: str) -> bool:
    return existing.strip().lower()[:40] == new.strip().lower()[:40]


def format_plans_for_context(plans: list[Plan]) -> str:
    if not plans:
        return ""
    lines = ["Active plans:"]
    for plan in plans:
        lines.append(f"\n📋 {plan.title}")
        for step in plan.steps:
            mark = "x" if step.status == "done" else " "
            lines.append(f"  [{mark}] {step.description}")
    return "\n".join(lines)


def extract_plans_from_reply(reply: str) -> list[dict]:
    results = []

    for match in PLAN_PATTERN.finditer(reply):
        action = (match.group(1) or "create").lower()
        body = match.group(2)
        title_line, _, rest = body.partition("\n")
        title = title_line.strip()

        steps_done = []
        steps_pending = []
        for sm in STEP_PATTERN.finditer(rest):
            checked = sm.group(1).lower() == "x"
            desc = sm.group(2).strip()
            if checked:
                steps_done.append(desc)
            else:
                steps_pending.append(desc)

        results.append({
            "action": action,
            "title": title,
            "steps_pending": steps_pending,
            "steps_done": steps_done,
        })

    for match in CLOSE_PATTERN.finditer(reply):
        start = match.end()
        rest = reply[start:start + 200].strip().split("\n")[0].strip()
        if rest:
            results.append({"action": "close", "title": rest})

    return results
