"""Cloud-backed LLM adapter for the sovereign engine.

Wraps the agent's async InferenceBackend (Anthropic, Google) into the sync
LLMProvider protocol that PocketAgent expects.

Handles:
  - Retry with exponential backoff on transient failures
  - Fallback to a secondary backend (e.g. heavy → light)
  - Context truncation on token limit errors
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

MAX_TASK_CHARS = 16_000

_MAX_RETRIES = 3
_RETRY_DELAYS = (2, 4, 8)

ENGINE_SYSTEM_CONTEXT = """\
You are the Pocket Agent core engine — a sovereign, local-first orchestrator.
Vision: Sovereign Creative Execution.
Every task has been checked against the user's 12-week year objectives before you see it.
Be direct, modular in your reasoning, and bias toward execution that advances stated goals."""


class CloudLLMAdapter:
    """Wraps an async InferenceBackend to match the sync LLMProvider protocol.

    Lets the sovereign engine use the agent's cloud backends (Gemini, Claude)
    as its inference provider with retry, fallback, and graceful degradation.
    """

    def __init__(self, backend: Any, fallback: Any | None = None) -> None:
        self._backend = backend
        self._fallback = fallback

    def health(self) -> bool:
        return True  # Cloud is always "reachable" — errors are per-request

    def generate(self, task: str, alignment: dict[str, Any] | None = None) -> str:
        from pocket_agent.inference.base import Message

        task = task[:MAX_TASK_CHARS]
        user_body = task
        if alignment:
            tags = ", ".join(alignment.get("matched_tags") or []) or "none"
            user_body = (
                f"{task}\n\n[12WY alignment score: {alignment.get('score', 0)} | "
                f"tags: {tags}]"
            )
        messages = [
            Message(role="system", content=ENGINE_SYSTEM_CONTEXT),
            Message(role="user", content=user_body),
        ]

        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                return self._run_async(
                    self._backend.complete(messages, max_tokens=2048)
                )
            except (ConnectionError, TimeoutError, OSError) as e:
                last_error = e
                if attempt < _MAX_RETRIES - 1:
                    delay = _RETRY_DELAYS[attempt]
                    log.warning(
                        "Engine LLM attempt %d/%d failed (%s), retrying in %ds",
                        attempt + 1, _MAX_RETRIES, e, delay,
                    )
                    time.sleep(delay)
                    continue
            except RuntimeError as e:
                last_error = e
                err_lower = str(e).lower()
                # Context window overflow — truncate and retry once
                if ("context" in err_lower or "token" in err_lower) and attempt == 0:
                    log.warning("Context overflow detected, truncating and retrying")
                    messages[-1] = Message(
                        role="user",
                        content=messages[-1].content[: MAX_TASK_CHARS // 2],
                    )
                    continue
                break  # non-retryable RuntimeError

        # Primary exhausted — try fallback backend
        if self._fallback and last_error:
            log.warning(
                "Primary LLM failed after %d attempts, trying fallback: %s",
                _MAX_RETRIES, last_error,
            )
            try:
                return self._run_async(
                    self._fallback.complete(messages, max_tokens=2048)
                )
            except Exception as fallback_err:
                log.error("Fallback LLM also failed: %s", fallback_err)
                raise last_error from fallback_err

        if last_error:
            raise last_error
        raise RuntimeError("Engine LLM generate failed with no error (shouldn't happen)")

    def complete(self, messages: list[dict[str, str]]) -> str:
        from pocket_agent.inference.base import Message
        msgs = [Message(role=m["role"], content=m["content"]) for m in messages]
        return self._run_async(self._backend.complete(msgs, max_tokens=2048))

    def _run_async(self, coro: Any) -> str:
        """Run an async coroutine from sync context, handling nested loops."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result(timeout=120)
        return asyncio.run(coro)
