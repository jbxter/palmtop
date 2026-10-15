"""Tests for config/settings.py — TOML parsing, defaults, multi-channel."""

import asyncio
import tempfile
from pathlib import Path

from palmtop.config.settings import (
    Config,
    detect_runtime,
    http_limits,
    install_worker_pool,
    read_cgroup_memory_bytes,
    resolve_profile,
    usable_ram_bytes,
)
from palmtop.inference.local import LocalBackend

_GB = 1024**3
_EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"


def _dev(monkeypatch) -> None:
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.setenv("PREFIX", "/usr/local")


def test_default_config_loads_without_file():
    """Config.load() with a nonexistent path should return defaults."""
    cfg = Config.load(Path("/nonexistent/config.toml"))
    assert cfg.timezone == "America/Los_Angeles"
    assert cfg.data_dir == Path("data")
    assert cfg.channel in ("telegram", "sms")


def test_admin_host_defaults_to_loopback():
    """/health is unauthenticated, so the admin server must bind loopback by default (#30)."""
    assert Config().admin.host == "127.0.0.1"
    assert Config.load(Path("/nonexistent/config.toml")).admin.host == "127.0.0.1"


def test_admin_host_override_from_toml():
    """Operators can still opt into a broader bind explicitly."""
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write('[admin]\nhost = "0.0.0.0"\n')
        path = Path(f.name)
    cfg = Config.load(path)
    assert cfg.admin.host == "0.0.0.0"


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


def test_phone_profile_keeps_caps(monkeypatch):
    monkeypatch.setenv("TERMUX_VERSION", "0.118.0")
    cfg = Config.load(None, cpu=8, ram_bytes=32 * _GB)
    assert cfg.profile.name == "phone"
    assert cfg.channel == "sms"
    assert cfg.inference.n_threads == 4
    assert cfg.inference.n_ctx == 4096
    assert cfg.engine.max_children == 4
    assert cfg.engine.max_parallel_research == 3
    limits = http_limits(cfg.profile)
    assert limits.max_connections == 5
    assert limits.max_keepalive_connections == 2


def test_compact_profile_stays_on_telegram(monkeypatch):
    _dev(monkeypatch)
    cfg = Config.load(None, cpu=4, ram_bytes=4 * _GB)
    assert cfg.profile.name == "compact"
    assert cfg.channel == "telegram"
    assert cfg.engine.max_children == 4
    assert cfg.inference.n_ctx == 4096
    assert cfg.profile.worker_pool == 4


def test_unknown_ram_stays_compact(monkeypatch):
    _dev(monkeypatch)
    cfg = Config.load(None, cpu=8, ram_bytes=None)
    assert cfg.profile.name == "compact"


def test_host_8gb_raises_parallelism_not_context(monkeypatch):
    _dev(monkeypatch)
    cfg = Config.load(None, cpu=8, ram_bytes=8 * _GB)
    assert cfg.profile.name == "host"
    assert cfg.inference.n_ctx == 4096
    assert cfg.inference.n_threads == 7
    assert cfg.engine.max_children == 8
    assert cfg.engine.max_parallel_research == 6
    assert cfg.profile.worker_pool == 6
    limits = http_limits(cfg.profile)
    assert limits.max_connections == 20
    assert limits.max_keepalive_connections == 10


def test_host_16gb_raises_context(monkeypatch):
    _dev(monkeypatch)
    cfg = Config.load(None, cpu=8, ram_bytes=16 * _GB)
    assert cfg.profile.name == "host"
    assert cfg.inference.n_ctx == 8192


def test_toml_max_children_is_not_replaced(monkeypatch):
    _dev(monkeypatch)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write("[engine]\nmax_children = 2\n")
        f.flush()
        cfg = Config.load(Path(f.name), cpu=8, ram_bytes=32 * _GB)
    assert cfg.engine.max_children == 2
    assert cfg.engine.max_parallel_research == 6


def test_toml_n_threads_is_not_replaced(monkeypatch):
    _dev(monkeypatch)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write("[inference]\nn_threads = 4\n")
        f.flush()
        cfg = Config.load(Path(f.name), cpu=8, ram_bytes=32 * _GB)
    assert cfg.inference.n_threads == 4
    assert cfg.inference.n_ctx == 8192


def test_cgroup_limit_selects_compact(tmp_path):
    limit = tmp_path / "memory.max"
    limit.write_text(str(1 * _GB), encoding="utf-8")
    assert read_cgroup_memory_bytes((limit,)) == 1 * _GB
    ram = usable_ram_bytes(physical=32 * _GB, cgroup=read_cgroup_memory_bytes((limit,)))
    assert resolve_profile("dev", 8, ram).name == "compact"


def test_cgroup_unlimited_is_ignored(tmp_path):
    unlimited = tmp_path / "memory.max"
    unlimited.write_text("max", encoding="utf-8")
    huge = tmp_path / "memory.limit_in_bytes"
    huge.write_text(str(1 << 62), encoding="utf-8")
    assert read_cgroup_memory_bytes((unlimited, huge)) is None


def test_example_config_does_not_pin_threads_or_context():
    for line in _EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        assert not stripped.startswith("n_ctx")
        assert not stripped.startswith("n_threads")


async def test_host_worker_pool_stays_at_or_under_8():
    pool = install_worker_pool(resolve_profile("dev", 32, 32 * _GB).worker_pool)
    try:
        assert pool._max_workers <= 8
        assert pool._max_workers == 6
    finally:
        pool.shutdown(wait=False)


async def test_local_generation_is_serialized():
    backend = LocalBackend.__new__(LocalBackend)
    backend._gen_lock = asyncio.Lock()
    state = {"current": 0, "peak": 0}

    async def fake(messages, max_tokens):
        state["current"] += 1
        state["peak"] = max(state["peak"], state["current"])
        await asyncio.sleep(0.05)
        state["current"] -= 1
        return "ok"

    backend._generate = fake
    await asyncio.gather(backend.complete([]), backend.complete([]))
    assert state["peak"] == 1
