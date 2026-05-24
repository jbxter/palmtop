"""Multi-channel runner — starts all configured channels as concurrent tasks.

Each channel runs in its own asyncio task. A crash in one channel doesn't
bring down the others: the runner logs the failure and optionally restarts
the crashed channel after a backoff delay.

Usage:
    runner = ChannelRunner(agent_loop)
    runner.add(telegram_channel)
    runner.add(sms_channel)
    await runner.run(async_init=startup_fn, on_start=on_start_fn)
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from palmtop.channels.base import Channel
    from palmtop.core.loop import AgentLoop

log = logging.getLogger(__name__)

# How long to wait before restarting a crashed channel
RESTART_DELAY_INITIAL = 5.0
RESTART_DELAY_MAX = 60.0
RESTART_DELAY_MULTIPLIER = 2.0
MAX_RESTARTS = 5


class ChannelRunner:
    """Manages multiple channels running concurrently in a single event loop."""

    def __init__(self, agent: AgentLoop) -> None:
        self._agent = agent
        self._channels: list[Channel] = []
        self._tasks: dict[str, asyncio.Task] = {}
        self._shutdown_event = asyncio.Event()

    def add(self, channel: Channel) -> None:
        """Register a channel to be started when run() is called."""
        self._channels.append(channel)
        log.info("Channel registered: %s", channel.name)

    @property
    def channels(self) -> list[Channel]:
        """All registered channels."""
        return list(self._channels)

    def get_channel(self, name: str) -> Channel | None:
        """Look up a channel by name."""
        for ch in self._channels:
            if ch.name == name:
                return ch
        return None

    async def run(
        self,
        *,
        async_init: Callable[[], Awaitable[None]] | None = None,
        on_start: Callable[[], None] | None = None,
        on_shutdown: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Start all channels and block until shutdown signal.

        Args:
            async_init: Async function to run before starting channels (DB init, etc.)
            on_start: Sync callback after all channels are started (digests, monitors, etc.)
            on_shutdown: Async cleanup after all channels stop.
        """
        loop = asyncio.get_running_loop()

        # Wire signal handlers for graceful shutdown
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._signal_shutdown)

        # Phase 1: Async initialization
        if async_init:
            await async_init()

        # Phase 2: Start all channels
        if not self._channels:
            log.warning("No channels registered — nothing to run")
            return

        for channel in self._channels:
            task = asyncio.create_task(
                self._run_channel(channel),
                name=f"channel:{channel.name}",
            )
            self._tasks[channel.name] = task
            log.info("Started channel: %s", channel.name)

        # Phase 3: Post-start callbacks
        if on_start:
            on_start()

        log.info(
            "All channels running: %s",
            ", ".join(ch.name for ch in self._channels),
        )

        # Block until shutdown signal
        await self._shutdown_event.wait()

        # Phase 4: Graceful shutdown
        log.info("Shutting down all channels...")
        await self._shutdown_all()

        if on_shutdown:
            await on_shutdown()

        log.info("Channel runner stopped")

    async def _run_channel(self, channel: Channel) -> None:
        """Run a single channel with crash recovery."""
        restart_count = 0
        delay = RESTART_DELAY_INITIAL

        while not self._shutdown_event.is_set():
            try:
                await channel.start(self._agent)
                # start() returned normally — channel shut itself down
                log.info("Channel %s stopped cleanly", channel.name)
                return
            except asyncio.CancelledError:
                log.info("Channel %s cancelled", channel.name)
                return
            except Exception:
                restart_count += 1
                if restart_count > MAX_RESTARTS:
                    log.error(
                        "Channel %s crashed %d times — giving up",
                        channel.name,
                        restart_count,
                    )
                    return
                log.exception(
                    "Channel %s crashed (attempt %d/%d) — restarting in %.0fs",
                    channel.name,
                    restart_count,
                    MAX_RESTARTS,
                    delay,
                )
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=delay,
                    )
                    # If we get here, shutdown was requested during backoff
                    return
                except TimeoutError:
                    # Backoff expired, restart the channel
                    delay = min(delay * RESTART_DELAY_MULTIPLIER, RESTART_DELAY_MAX)

        # Ensure stop is called
        try:
            await channel.stop()
        except Exception:
            log.debug("Error stopping channel %s", channel.name, exc_info=True)

    async def _shutdown_all(self) -> None:
        """Cancel all channel tasks and wait for them to finish."""
        # Cancel all tasks
        for name, task in self._tasks.items():
            if not task.done():
                task.cancel()

        # Wait for all to finish
        if self._tasks:
            results = await asyncio.gather(
                *self._tasks.values(),
                return_exceptions=True,
            )
            for (name, _), result in zip(self._tasks.items(), results):
                if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                    log.warning("Channel %s shutdown error: %s", name, result)

        # Call stop() on each channel
        for channel in self._channels:
            try:
                await channel.stop()
            except Exception:
                log.debug("Error in %s.stop()", channel.name, exc_info=True)

    def _signal_shutdown(self) -> None:
        """Handle SIGINT/SIGTERM."""
        log.info("Shutdown signal received")
        self._shutdown_event.set()
