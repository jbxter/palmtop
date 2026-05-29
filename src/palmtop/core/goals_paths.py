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

# Directories are searched relative to the project root and (optionally) a configurable data_dir.


def resolve_goals_path(
    data_dir: Path | None = None,
    project_root: Path | None = None,
    explicit: str | Path = "",
) -> Path:
    """Resolve the active goals file path.

    ``explicit`` wins if provided. Otherwise searches for goal files in:
    - <project_root>/docs/plans
    - <data_dir>/docs/plans
    - <data_dir>

    If none exist, returns <project_root>/docs/plans/twy_goals.json so callers
    have a stable path to report as missing.
    """
    if explicit:
        return Path(explicit)

    root = Path(project_root) if project_root else Path.cwd()
    ddir = Path(data_dir) if data_dir else (root / "data")
    if not ddir.is_absolute():
        ddir = root / ddir

    search_dirs = (root / "docs/plans", ddir / "docs/plans", ddir)
    for directory in search_dirs:
        for name in _GOALS_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return root / "docs/plans" / _GOALS_FILENAMES[0]


def goals_cache_path(goals_path: str | Path) -> Path:
    """Path to the last-good cache for ``goals_path``.

    Stored as a hidden sibling (``.<stem>.cache.json``) so the aligner can
    recover the last valid goals if the live file becomes unreadable.
    """
    p = Path(goals_path)
    return p.parent / f".{p.stem}.cache.json"
