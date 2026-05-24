from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


@runtime_checkable
class InferenceBackend(Protocol):
    async def complete(self, messages: list[Message], max_tokens: int = 512) -> str: ...

    async def close(self) -> None: ...
