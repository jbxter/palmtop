"""Tests for secrets.py — dotenvx integration helpers."""

import shutil

from palmtop.secrets import _check_dotenvx, _find_dotenvx, _install_hint


class TestFindDotenvx:
    def test_returns_path_when_available(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda x: "/usr/local/bin/dotenvx" if x == "dotenvx" else None)
        assert _find_dotenvx() == "/usr/local/bin/dotenvx"

    def test_returns_none_when_missing(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda x: None)
        assert _find_dotenvx() is None


class TestInstallHint:
    def test_macos(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        assert "brew" in _install_hint()

    def test_termux(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setenv("TERMUX_VERSION", "0.118")
        assert "npm" in _install_hint()

    def test_linux(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.delenv("TERMUX_VERSION", raising=False)
        assert "curl" in _install_hint()


class TestCheckDotenvx:
    def test_found_directly(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda x: "/usr/local/bin/dotenvx" if x == "dotenvx" else None)
        assert _check_dotenvx() == "/usr/local/bin/dotenvx"

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda x: None)
        assert _check_dotenvx() is None
