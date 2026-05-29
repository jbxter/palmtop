"""Tests for config/settings.py — TOML parsing, defaults, multi-channel."""

import tempfile
from pathlib import Path

from palmtop.config.settings import Config, detect_runtime


def test_default_config_loads_without_file():
    """Config.load() with a nonexistent path should return defaults."""
    cfg = Config.load(Path("/nonexistent/config.toml"))
    assert cfg.timezone == "America/Los_Angeles"
    assert cfg.data_dir == Path("data")
    assert cfg.channel in ("telegram", "sms")


def test_detect_runtime_dev(monkeypatch):
    """Without TERMUX_VERSION, runtime should be 'dev'."""
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.setenv("PREFIX", "/usr/local")
    assert detect_runtime() == "dev"


def test_detect_runtime_phone(monkeypatch):
    """With TERMUX_VERSION set, runtime should be 'phone'."""
    monkeypatch.setenv("TERMUX_VERSION", "0.118.0")
    assert detect_runtime() == "phone"


def test_detect_runtime_phone_prefix(monkeypatch):
    """With com.termux in PREFIX, runtime should be 'phone'."""
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    assert detect_runtime() == "phone"


def test_load_channel_from_toml():
    """channel = 'sms' in TOML should override default."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write('channel = "sms"\n')
        f.flush()
        cfg = Config.load(Path(f.name))
    assert cfg.channel == "sms"


def test_load_channels_multi():
    """channels = ['telegram', 'sms'] should set multi-channel mode."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write('channels = ["telegram", "sms"]\n')
        f.flush()
        cfg = Config.load(Path(f.name))
    assert cfg.channels == ["telegram", "sms"]
    assert cfg.active_channels == ["telegram", "sms"]


def test_active_channels_fallback():
    """active_channels should fall back to [channel] when channels is empty."""
    cfg = Config()
    cfg.channel = "telegram"
    cfg.channels = []
    assert cfg.active_channels == ["telegram"]


def test_load_timezone():
    """timezone should be read from TOML."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write('timezone = "US/Eastern"\n')
        f.flush()
        cfg = Config.load(Path(f.name))
    assert cfg.timezone == "US/Eastern"


def test_load_telegram_token_from_env(monkeypatch):
    """TELEGRAM_BOT_TOKEN env var should populate telegram config."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    cfg = Config.load(None)
    assert cfg.telegram.bot_token == "123:ABC"


def test_load_data_dir():
    """data_dir should be read from TOML."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write('data_dir = "/tmp/palmtop-test"\n')
        f.flush()
        cfg = Config.load(Path(f.name))
    assert cfg.data_dir == Path("/tmp/palmtop-test")


def test_hermes_config_defaults():
    """Hermes is off by default and gates skills (fails safe)."""
    cfg = Config()
    assert cfg.hermes.enabled is False
    assert cfg.hermes.require_blessing is True
    assert cfg.hermes.sync_memory is False
    assert cfg.hermes.api_url == "http://localhost:8080"


def test_load_hermes_from_toml():
    """[hermes] block should override defaults."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write('[hermes]\nenabled = true\napi_url = "http://hermes.local:9000"\nsync_memory = true\n')
        f.flush()
        cfg = Config.load(Path(f.name))
    assert cfg.hermes.enabled is True
    assert cfg.hermes.api_url == "http://hermes.local:9000"
    assert cfg.hermes.sync_memory is True


def test_hermes_api_key_from_env(monkeypatch):
    """HERMES_API_KEY env var should populate hermes config when unset in TOML."""
    monkeypatch.setenv("HERMES_API_KEY", "hk-test")
    cfg = Config.load(None)
    assert cfg.hermes.api_key == "hk-test"
