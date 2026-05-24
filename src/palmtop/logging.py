"""Structured JSON logging for Palmtop.

When log_format = "json" in config, all log output becomes newline-delimited
JSON parseable by Loki, ELK, Datadog, etc.

Usage:
    from palmtop.logging import configure_logging
    configure_logging(format="json", level="info")
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Add extra fields if present
        if hasattr(record, "channel"):
            log_entry["channel"] = record.channel
        if hasattr(record, "user_id"):
            log_entry["user_id"] = record.user_id
        if hasattr(record, "duration_ms"):
            log_entry["duration_ms"] = record.duration_ms
        if hasattr(record, "tokens"):
            log_entry["tokens"] = record.tokens
        if hasattr(record, "model"):
            log_entry["model"] = record.model
        if hasattr(record, "tool"):
            log_entry["tool"] = record.tool

        # Include exception info
        if record.exc_info and record.exc_info[1]:
            log_entry["error"] = str(record.exc_info[1])
            log_entry["error_type"] = type(record.exc_info[1]).__name__

        return json.dumps(log_entry, default=str)


def configure_logging(
    *,
    format: str = "text",
    level: str = "info",
) -> None:
    """Configure logging for the entire application.

    Args:
        format: "text" for human-readable, "json" for structured.
        level: Logging level (debug, info, warning, error).
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(log_level)

    # Remove existing handlers
    root.handlers.clear()

    handler = logging.StreamHandler()
    handler.setLevel(log_level)

    if format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(name)s %(levelname)s %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    root.addHandler(handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)


class Metrics:
    """Simple in-memory metrics collector.

    Tracks request counts, token usage, and timing. Thread-safe via GIL.
    """

    def __init__(self) -> None:
        self.requests_total: int = 0
        self.tokens_prompt: int = 0
        self.tokens_completion: int = 0
        self.tool_calls_total: int = 0
        self.tool_errors_total: int = 0
        self.request_durations: list[float] = []  # last 1000
        self._by_model: dict[str, dict[str, int]] = {}
        self._by_tool: dict[str, dict[str, int]] = {}
        self._start_time = time.time()

    def record_request(
        self, *, duration_ms: float, model: str = "", prompt_tokens: int = 0, completion_tokens: int = 0
    ) -> None:
        """Record a completed LLM request."""
        self.requests_total += 1
        self.tokens_prompt += prompt_tokens
        self.tokens_completion += completion_tokens

        # Keep last 1000 durations for percentile calculation
        self.request_durations.append(duration_ms)
        if len(self.request_durations) > 1000:
            self.request_durations = self.request_durations[-1000:]

        if model:
            if model not in self._by_model:
                self._by_model[model] = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}
            self._by_model[model]["requests"] += 1
            self._by_model[model]["prompt_tokens"] += prompt_tokens
            self._by_model[model]["completion_tokens"] += completion_tokens

    def record_tool_call(self, tool_name: str, *, success: bool = True) -> None:
        """Record a tool invocation."""
        self.tool_calls_total += 1
        if not success:
            self.tool_errors_total += 1

        if tool_name not in self._by_tool:
            self._by_tool[tool_name] = {"calls": 0, "errors": 0}
        self._by_tool[tool_name]["calls"] += 1
        if not success:
            self._by_tool[tool_name]["errors"] += 1

    def estimate_cost(self) -> float:
        """Rough cost estimate in USD based on token usage.

        Uses approximate pricing:
        - Claude: $3/M input, $15/M output
        - Gemini: $0.15/M input, $0.60/M output
        """
        # Rough average — actual cost depends on model mix
        input_cost = self.tokens_prompt * 1.5 / 1_000_000  # ~$1.50/M average
        output_cost = self.tokens_completion * 7.5 / 1_000_000  # ~$7.50/M average
        return round(input_cost + output_cost, 4)

    def to_dict(self) -> dict[str, Any]:
        """Export metrics as a dict (for /admin/stats or JSON logging)."""
        uptime = time.time() - self._start_time
        p50, p99 = 0.0, 0.0
        if self.request_durations:
            sorted_d = sorted(self.request_durations)
            p50 = sorted_d[len(sorted_d) // 2]
            p99 = sorted_d[int(len(sorted_d) * 0.99)]

        return {
            "uptime_seconds": int(uptime),
            "requests_total": self.requests_total,
            "tokens": {
                "prompt": self.tokens_prompt,
                "completion": self.tokens_completion,
                "total": self.tokens_prompt + self.tokens_completion,
            },
            "cost_estimate_usd": self.estimate_cost(),
            "latency_ms": {"p50": round(p50, 1), "p99": round(p99, 1)},
            "tools": {
                "calls_total": self.tool_calls_total,
                "errors_total": self.tool_errors_total,
                "by_tool": self._by_tool,
            },
            "by_model": self._by_model,
        }
