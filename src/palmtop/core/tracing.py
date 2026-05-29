"""Per-turn tracing. Records routing, generations, tool calls, and the reply.

A no-op when ``enabled`` is False (the default), so it's free to leave wired
into the agent loop. When enabled, events accumulate on the per-turn Trace and
are logged at debug — a hook point for richer observability backends later.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

log = logging.getLogger(__name__)


class Trace:
    """Accumulates events for a single conversation turn."""

    def __init__(self, enabled: bool, user_id: str, user_text: str) -> None:
        self.enabled = enabled
        self.user_id = user_id
        self.user_text = user_text
        self.events: list[dict] = []

    def _add(self, kind: str, **data: object) -> None:
        if self.enabled:
            self.events.append({"kind": kind, **data})

    def record_route(self, route: str, backend: str) -> None:
        self._add("route", route=route, backend=backend)

    def record_generation(self, *, model: str, input_messages: list, output: str) -> None:
        self._add("generation", model=model, output=(output or "")[:500])

    def record_tool_call(
        self,
        tool: str,
        query: str,
        result: str,
        *,
        error_kind: str | None = None,
        success: bool = True,
        retried: bool = False,
    ) -> None:
        self._add("tool_call", tool=tool, success=success, error_kind=error_kind, retried=retried)

    def record_tool_auto(self, label: str, query: str, result: str) -> None:
        self._add("tool_auto", label=label)

    def record_evaluator(self, issues: list) -> None:
        self._add("evaluator", issues=list(issues))

    def record_reply(self, reply: str) -> None:
        self._add("reply", reply=(reply or "")[:500])


class Tracer:
    def __init__(self, enabled: bool = False, backend: str | None = None, data_dir: object | None = None) -> None:
        self._enabled = enabled
        self._backend = backend
        self._data_dir = data_dir

    @property
    def enabled(self) -> bool:
        return self._enabled

    @contextmanager
    def trace_turn(self, user_id: str, user_text: str) -> Iterator[Trace]:
        trace = Trace(self._enabled, user_id, user_text)
        try:
            yield trace
        finally:
            if self._enabled:
                log.debug("trace turn user=%s events=%d", user_id, len(trace.events))
