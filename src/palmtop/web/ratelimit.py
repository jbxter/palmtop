"""Rate limiting for the public web channel.

In-memory token-bucket limiter — no Redis, no external deps.
Protects the S21 from abuse on a public-facing endpoint.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class _Bucket:
    """Token bucket for a single key (IP or session)."""
    tokens: float
    last_refill: float
    rate: float      # tokens per second
    capacity: float  # max tokens


def _refill(bucket: _Bucket, now: float) -> None:
    elapsed = now - bucket.last_refill
    bucket.tokens = min(bucket.capacity, bucket.tokens + elapsed * bucket.rate)
    bucket.last_refill = now


class RateLimiter:
    """Multi-tier rate limiter for web endpoints."""

    def __init__(
        self,
        chat_rpm: int = 10,
        chat_rpd: int = 100,
        form_rpm: int = 3,
        form_rpd: int = 10,
        max_concurrent: int = 5,
    ) -> None:
        self._chat_rpm = chat_rpm
        self._chat_rpd = chat_rpd
        self._form_rpm = form_rpm
        self._form_rpd = form_rpd
        self._max_concurrent = max_concurrent
        self._concurrent = 0

        # Separate buckets for per-minute and per-day limits
        self._minute_buckets: dict[str, _Bucket] = {}
        self._day_buckets: dict[str, _Bucket] = {}

    def check_chat(self, key: str) -> bool:
        """Check if a chat message is allowed.  Returns False if rate-limited."""
        now = time.time()

        # Per-minute check
        minute_key = f"chat:min:{key}"
        if not self._check_bucket(
            minute_key, self._minute_buckets,
            rate=self._chat_rpm / 60.0,
            capacity=self._chat_rpm,
            now=now,
        ):
            log.warning("Chat rate limit (per-minute) for %s", key[:16])
            return False

        # Per-day check
        day_key = f"chat:day:{key}"
        if not self._check_bucket(
            day_key, self._day_buckets,
            rate=self._chat_rpd / 86400.0,
            capacity=self._chat_rpd,
            now=now,
        ):
            log.warning("Chat rate limit (per-day) for %s", key[:16])
            return False

        return True

    def check_form(self, key: str) -> bool:
        """Check if a form submission is allowed."""
        now = time.time()

        minute_key = f"form:min:{key}"
        if not self._check_bucket(
            minute_key, self._minute_buckets,
            rate=self._form_rpm / 60.0,
            capacity=self._form_rpm,
            now=now,
        ):
            log.warning("Form rate limit (per-minute) for %s", key[:16])
            return False

        day_key = f"form:day:{key}"
        if not self._check_bucket(
            day_key, self._day_buckets,
            rate=self._form_rpd / 86400.0,
            capacity=self._form_rpd,
            now=now,
        ):
            log.warning("Form rate limit (per-day) for %s", key[:16])
            return False

        return True

    def acquire_stream(self) -> bool:
        """Try to acquire a concurrent stream slot."""
        if self._concurrent >= self._max_concurrent:
            log.warning("Max concurrent chats reached (%d)", self._max_concurrent)
            return False
        self._concurrent += 1
        return True

    def release_stream(self) -> None:
        """Release a concurrent stream slot."""
        self._concurrent = max(0, self._concurrent - 1)

    def _check_bucket(
        self,
        key: str,
        store: dict[str, _Bucket],
        rate: float,
        capacity: float,
        now: float,
    ) -> bool:
        bucket = store.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=capacity, last_refill=now, rate=rate, capacity=capacity)
            store[key] = bucket

        _refill(bucket, now)

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True
        return False

    def cleanup(self) -> int:
        """Remove stale buckets older than 24h.  Call periodically."""
        now = time.time()
        cutoff = now - 86400
        removed = 0
        for store in (self._minute_buckets, self._day_buckets):
            stale = [k for k, b in store.items() if b.last_refill < cutoff]
            for k in stale:
                del store[k]
                removed += 1
        return removed
