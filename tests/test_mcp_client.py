"""Tests for MCP client env scoping + output sanitization — issue #49."""

from __future__ import annotations

from palmtop.mcp.client import _INJECTION_STRIP, _scoped_env


class TestScopedEnv:
    def test_withholds_secrets(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.setenv("PATH", "/usr/bin")
        env = _scoped_env({})
        assert "ANTHROPIC_API_KEY" not in env  # agent secret withheld
        assert "TELEGRAM_BOT_TOKEN" not in env
        assert env.get("PATH") == "/usr/bin"  # safe baseline passes

    def test_includes_explicit_grants(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
        env = _scoped_env({"JIRA_API_TOKEN": "granted"})
        assert env["JIRA_API_TOKEN"] == "granted"  # explicit per-server grant passes
        assert "ANTHROPIC_API_KEY" not in env

    def test_passes_locale_prefix(self, monkeypatch):
        monkeypatch.setenv("LC_CUSTOM", "bar")
        assert _scoped_env({}).get("LC_CUSTOM") == "bar"


class TestOutputStrip:
    def test_strips_tool_call_syntax(self):
        out = _INJECTION_STRIP.sub("", "result [TOOL:email] and [ACTION:run] and [ON_FAIL:x]")
        assert "[TOOL:" not in out
        assert "[ACTION:" not in out
        assert "[ON_FAIL:" not in out

    def test_leaves_normal_text(self):
        assert _INJECTION_STRIP.sub("", "normal jira output") == "normal jira output"
