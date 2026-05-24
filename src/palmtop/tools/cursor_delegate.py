"""Delegate work to Cursor Cloud Agents via [TOOL:cursor]."""

from __future__ import annotations

from typing import TYPE_CHECKING

from palmtop.tools.base import Tool

if TYPE_CHECKING:
    from palmtop.cursor.runner import CursorJobManager


class DelegateCursorTool(Tool):
    name = "cursor"
    description = (
        "Delegate a coding task to a Cursor Cloud Agent on an allowed GitHub repo. "
        "Usage: [TOOL:cursor] <prompt>  or  [TOOL:cursor] repo=<url> branch=<ref> <prompt>"
    )

    def __init__(self, manager: "CursorJobManager") -> None:
        self._manager = manager
        self._last_user_id = "default"

    def set_user_id(self, user_id: str) -> None:
        self._last_user_id = user_id

    async def run(self, query: str) -> str:
        return await self._manager.launch(query, user_id=self._last_user_id)
