"""Tests for the multi-channel runner."""

import asyncio

import pytest

from palmtop.channels.runner import ChannelRunner


class FakeAgentLoop:
    """Minimal stand-in for AgentLoop."""

    _send_fn = None

    async def handle(self, text: str, user_id: str = "") -> str:
        return f"echo: {text}"


class FakeChannel:
    """Minimal channel that satisfies the protocol."""

    def __init__(self, name: str, *, crash_after: int = 0):
        self._name = name
        self._started = False
        self._stopped = False
        self._messages: list[tuple[str, str]] = []
        self._crash_after = crash_after
        self._start_count = 0
        self._stop_event = asyncio.Event()

    @property
    def name(self) -> str:
        return self._name

    async def start(self, loop) -> None:
        self._started = True
        self._start_count += 1
        if self._crash_after and self._start_count <= self._crash_after:
            raise RuntimeError(f"Simulated crash #{self._start_count}")
        await self._stop_event.wait()

    async def stop(self) -> None:
        self._stopped = True
        self._stop_event.set()

    async def send_message(self, user_id: str, text: str) -> None:
        self._messages.append((user_id, text))


@pytest.mark.asyncio
async def test_runner_starts_and_stops_channels():
    """Runner should start all channels and shut them down on signal."""
    agent = FakeAgentLoop()
    runner = ChannelRunner(agent)

    ch1 = FakeChannel("alpha")
    ch2 = FakeChannel("beta")
    runner.add(ch1)
    runner.add(ch2)

    # Schedule shutdown after a brief delay
    async def _delayed_shutdown():
        await asyncio.sleep(0.1)
        runner._signal_shutdown()

    asyncio.create_task(_delayed_shutdown())
    await runner.run()

    assert ch1._started
    assert ch2._started
    assert ch1._stopped
    assert ch2._stopped


@pytest.mark.asyncio
async def test_runner_async_init_called():
    """async_init should be called before channels start."""
    agent = FakeAgentLoop()
    runner = ChannelRunner(agent)

    init_called = False

    async def _init():
        nonlocal init_called
        init_called = True

    ch = FakeChannel("test")
    runner.add(ch)

    async def _delayed_shutdown():
        await asyncio.sleep(0.1)
        runner._signal_shutdown()

    asyncio.create_task(_delayed_shutdown())
    await runner.run(async_init=_init)

    assert init_called
    assert ch._started


@pytest.mark.asyncio
async def test_runner_channel_crash_isolation(monkeypatch):
    """A crash in one channel should not stop the other."""
    import palmtop.channels.runner as runner_mod

    # Speed up backoff for testing
    monkeypatch.setattr(runner_mod, "RESTART_DELAY_INITIAL", 0.1)

    agent = FakeAgentLoop()
    runner = ChannelRunner(agent)

    # This channel crashes on first start, then works on restart
    crasher = FakeChannel("crasher", crash_after=1)
    stable = FakeChannel("stable")
    runner.add(crasher)
    runner.add(stable)

    async def _delayed_shutdown():
        # Wait long enough for the crasher to restart after backoff
        await asyncio.sleep(0.5)
        runner._signal_shutdown()

    asyncio.create_task(_delayed_shutdown())
    await runner.run()

    # Stable channel should have started fine
    assert stable._started
    # Crasher should have been restarted at least once
    assert crasher._start_count >= 2


@pytest.mark.asyncio
async def test_runner_get_channel():
    """get_channel() should find registered channels by name."""
    agent = FakeAgentLoop()
    runner = ChannelRunner(agent)

    ch = FakeChannel("telegram")
    runner.add(ch)

    assert runner.get_channel("telegram") is ch
    assert runner.get_channel("nonexistent") is None


@pytest.mark.asyncio
async def test_runner_on_start_callback():
    """on_start callback should fire after channels are running."""
    agent = FakeAgentLoop()
    runner = ChannelRunner(agent)

    on_start_called = False

    def _on_start():
        nonlocal on_start_called
        on_start_called = True

    ch = FakeChannel("test")
    runner.add(ch)

    async def _delayed_shutdown():
        await asyncio.sleep(0.1)
        runner._signal_shutdown()

    asyncio.create_task(_delayed_shutdown())
    await runner.run(on_start=_on_start)

    assert on_start_called
