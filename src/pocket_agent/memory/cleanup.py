#!/usr/bin/env python3
"""One-shot cleanup: remove poisoned memories from the database.

Poisoned memories are facts that were extracted from tool errors,
evaluator replacement text, or fabricated narratives. They contain
transient system status observations that should never have been
stored as long-term facts.

No project imports — runs with any Python 3.8+ using only stdlib.

Usage:
    python cleanup.py [path/to/memories.db]
    python cleanup.py --dry-run [path/to/memories.db]

    # default: looks for ./data/memories.db
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Substrings that indicate a memory was poisoned by tool errors or
# evaluator replacement text. Case-insensitive matching.
POISON_PATTERNS = [
    # From evaluator replacement text
    "permissions or access issue",
    "integration side",
    "check the connection config",
    "check the logs or the integration",
    # From fabricated narratives the agent invented
    "410 gone",
    "410 api",
    "api legacy error",
    "api migration",
    "deprecated endpoint",
    "deprecated api",
    "endpoint migration",
    "sunset",
    "integration is currently suspended",
    "integration tool has a",
    "internal pathing",
    "requires an update",
    # From capability hallucinations
    "fundamentally broken",
    "permanently broken",
    "spinning its wheels",
    "i will act as your",
    "i don't have permissions to rewrite",
    "stop trying",
    "move to plan b",
    # Fabricated Atlassian-specific claims
    "cql syntax was",
    "jql syntax was",
    "hard-coded into the tool",
    "wired to always return",
    "coded to always",
]


def find_poisoned(db_path: Path) -> list[tuple[int, str, str]]:
    """Return (id, category, content) for all poisoned memories."""
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        "SELECT id, category, content FROM memories ORDER BY id"
    )
    poisoned = []
    for row_id, cat, content in cursor:
        content_lower = content.lower()
        for pattern in POISON_PATTERNS:
            if pattern in content_lower:
                poisoned.append((row_id, cat, content))
                break
    conn.close()
    return poisoned


def delete_poisoned(db_path: Path, ids: list[int]) -> int:
    """Delete memories by ID. Returns count deleted."""
    if not ids:
        return 0
    conn = sqlite3.connect(db_path)
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", ids)
    conn.commit()
    deleted = conn.total_changes
    conn.close()
    return deleted


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    if args:
        p = Path(args[0])
        # Accept either a direct .db path or a directory containing memories.db
        db_path = p if p.suffix == ".db" else p / "memories.db"
    else:
        db_path = Path("data") / "memories.db"

    if not db_path.exists():
        print(f"No database found at {db_path}")
        sys.exit(1)

    poisoned = find_poisoned(db_path)

    if not poisoned:
        print("No poisoned memories found. Database is clean.")
        return

    print(f"Found {len(poisoned)} poisoned memories:\n")
    for row_id, cat, content in poisoned:
        print(f"  [{row_id}] ({cat}) {content[:120]}")

    if dry_run:
        print(f"\n--dry-run: would delete {len(poisoned)} memories.")
        return

    ids = [row_id for row_id, _, _ in poisoned]
    deleted = delete_poisoned(db_path, ids)
    print(f"\nDeleted {deleted} poisoned memories.")


if __name__ == "__main__":
    main()
