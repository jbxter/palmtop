"""Tests for logging.py — structured JSON logging and metrics."""

import json
import logging

from palmtop.logging import JsonFormatter, Metrics, configure_logging


class TestJsonFormatter:
    def test_formats_as_json(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="palmtop.test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="hello world",
            args=None,
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["level"] == "info"
        assert data["msg"] == "hello world"
        assert data["logger"] == "palmtop.test"
        assert "ts" in data

    def test_includes_extra_fields(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="palmtop.channels.telegram",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="message received",
            args=None,
            exc_info=None,
        )
        record.channel = "telegram"
        record.user_id = "12345"
        record.duration_ms = 250.5
        output = formatter.format(record)
        data = json.loads(output)
        assert data["channel"] == "telegram"
        assert data["user_id"] == "12345"
        assert data["duration_ms"] == 250.5

    def test_includes_exception_info(self):
        formatter = JsonFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys

            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="palmtop",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="failed",
            args=None,
            exc_info=exc_info,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["error"] == "test error"
        assert data["error_type"] == "ValueError"


class TestConfigureLogging:
    def test_text_format(self):
        configure_logging(format="text", level="info")
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert not isinstance(root.handlers[0].formatter, JsonFormatter)

    def test_json_format(self):
        configure_logging(format="json", level="info")
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)

    def test_level_setting(self):
        configure_logging(format="text", level="debug")
        root = logging.getLogger()
        assert root.level == logging.DEBUG


class TestMetrics:
    def test_record_request(self):
        m = Metrics()
        m.record_request(duration_ms=100.0, model="claude-4", prompt_tokens=500, completion_tokens=200)
        assert m.requests_total == 1
        assert m.tokens_prompt == 500
        assert m.tokens_completion == 200

    def test_tracks_by_model(self):
        m = Metrics()
        m.record_request(duration_ms=50.0, model="claude-4", prompt_tokens=100, completion_tokens=50)
        m.record_request(duration_ms=30.0, model="gemini-2", prompt_tokens=80, completion_tokens=40)
        assert m._by_model["claude-4"]["requests"] == 1
        assert m._by_model["gemini-2"]["prompt_tokens"] == 80

    def test_record_tool_call(self):
        m = Metrics()
        m.record_tool_call("web_search", success=True)
        m.record_tool_call("web_search", success=False)
        m.record_tool_call("calendar", success=True)
        assert m.tool_calls_total == 3
        assert m.tool_errors_total == 1
        assert m._by_tool["web_search"]["errors"] == 1

    def test_cost_estimate(self):
        m = Metrics()
        m.record_request(duration_ms=100.0, model="claude-4", prompt_tokens=1000, completion_tokens=500)
        cost = m.estimate_cost()
        assert cost > 0
        assert isinstance(cost, float)

    def test_to_dict(self):
        m = Metrics()
        m.record_request(duration_ms=100.0, model="claude-4", prompt_tokens=500, completion_tokens=200)
        m.record_tool_call("search", success=True)
        data = m.to_dict()
        assert data["requests_total"] == 1
        assert data["tokens"]["total"] == 700
        assert data["tools"]["calls_total"] == 1
        assert "cost_estimate_usd" in data
        assert "latency_ms" in data

    def test_duration_capped_at_1000(self):
        m = Metrics()
        for i in range(1500):
            m.record_request(duration_ms=float(i), model="test")
        assert len(m.request_durations) == 1000
