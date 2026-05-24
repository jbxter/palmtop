"""Channel protocol and base class for Palmtop messaging channels.

Every channel implements the same interface so the runner can manage
them uniformly — start them as concurrent tasks, route messages, and
shut them down cleanly.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from palmtop.core.loop import AgentLoop


@runtime_checkable
class Channel(Protocol):
    """Protocol that all channels must satisfy."""

    @property
    def name(self) -> str:
        """Short identifier for logging and routing (e.g. 'telegram', 'sms')."""
        ...

    async def start(self, loop: AgentLoop) -> None:
        """Start receiving messages. Runs until stop() is called or cancelled.

        This coroutine should not return until the channel is shutting down.
        The runner will cancel it on SIGTERM/SIGINT.
        """
        ...

    async def stop(self) -> None:
        """Gracefully shut down the channel.

        Release connections, flush buffers, etc. Called after the start()
        task is cancelled or after it returns.
        """
        ...

    async def send_message(self, user_id: str, text: str) -> None:
        """Send a message to a user on this channel.

        Used for proactive notifications (reminders, alerts, digests).
        """
        ...
