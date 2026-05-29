"""Resolution and caching paths for 12 Week Year goal files.

The goal file (``twy_goals.json``) is the source of truth for what the
autonomous engine is allowed to act on. These helpers locate it and provide a
sibling cache path so a mid-edit or corrupt goal file can fall back to the last
successfully parsed version instead of dropping into safe mode.
"""

from __future__ import annotations

from pathlib import Path

# Goal filenames to look for, in priority order.
_GOALS_FILENAMES = ("twy_goals.json", "goals.json")

# Directories (relative to the project root) searched in order.
_SEARCH_DIRS = ("docs/plans", "data/docs/plans", "data")


def resolve_goals_path(project_root: Path | None = None, explicit: str | Path = "") -> Path:
    """Resolve the active goals file path.

    ``explicit`` wins if provided. Otherwise the first existing file across the
    known locations is returned; if none exist, the canonical default
    (``docs/plans/twy_goals.json``) is returned so callers have a stable path to
    report as missing.
    """
    if explicit:
        return Path(explicit)

    root = Path(project_root) if project_root else Path.cwd()
    for directory in _SEARCH_DIRS:
        for name in _GOALS_FILENAMES:
            candidate = root / directory / name
            if candidate.is_file():
                return candidate
    return root / _SEARCH_DIRS[0] / _GOALS_FILENAMES[0]


def goals_cache_path(goals_path: str | Path) -> Path:
    """Path to the last-good cache for ``goals_path``.

    Stored as a hidden sibling (``.<stem>.cache.json``) so the aligner can
    recover the last valid goals if the live file becomes unreadable.
    """
    p = Path(goals_path)
    return p.parent / f".{p.stem}.cache.json"
