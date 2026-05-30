"""Tests for FileTool path confinement, incl. the goals denylist — issue #25."""

from __future__ import annotations

from palmtop.tools.files import FileTool


def test_refuses_alignment_goals_and_cache(tmp_path):
    t = FileTool(tmp_path)
    # The agent must not be able to write/read its own alignment goals or cache.
    assert t._resolve("plans/twy_goals.json") is None
    assert t._resolve("plans/goals.json") is None
    assert t._resolve(".twy_goals.cache.json") is None
    assert t._resolve("plans/.goals.cache.json") is None


def test_allows_normal_docs(tmp_path):
    t = FileTool(tmp_path)
    assert t._resolve("plans/q3-notes.md") is not None
    assert t._resolve("data.json") is not None


def test_still_blocks_traversal_and_bad_ext(tmp_path):
    t = FileTool(tmp_path)
    assert t._resolve("../config.toml") is None  # parent traversal
    assert t._resolve("/etc/passwd") is None  # absolute
    assert t._resolve("notes.exe") is None  # disallowed extension
