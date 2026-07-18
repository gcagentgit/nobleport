"""Bounded, optionally-persistent per-session rate limiting.

The public web channel is unauthenticated, so it needs abuse protection that
does not grow without bound (a naive ``dict`` keyed by session leaks memory)
and that survives across processes when a shared store is available.

  * :class:`RedisRateLimiter` — production path when ``REDIS_URL`` is set.
    Uses atomic ``INCR`` + ``EXPIRE`` on time-bucketed keys, so counters are
    shared across API replicas and self-evict via TTL.
  * :class:`InMemoryRateLimiter` — fallback. Fixed-window counters with hard
    eviction of expired buckets and a cap on tracked keys. Single-process
    only; marked NON-PRODUCTION for multi-replica deployments.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional, Protocol

log = logging.getLogger("hermes.ratelimit")


class RateLimiter(Protocol):
    async def allow(self, key: str) -> bool: ...


class InMemoryRateLimiter:
    """Fixed-window rate limiter with bounded memory. Single process only."""

    def __init__(self, per_min: int = 5, per_hour: int = 30, max_keys: int = 10_000) -> None:
        self._per_min = per_min
        self._per_hour = per_hour
        self._max_keys = max_keys
        self._minute: dict[tuple[str, int], int] = {}
        self._hour: dict[tuple[str, int], int] = {}
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        now = int(time.time())
        min_bucket = now // 60
        hour_bucket = now // 3600
        async with self._lock:
            self._evict(min_bucket, hour_bucket)
            mc = self._minute.get((key, min_bucket), 0)
            hc = self._hour.get((key, hour_bucket), 0)
            if mc >= self._per_min or hc >= self._per_hour:
                return False
            self._minute[(key, min_bucket)] = mc + 1
            self._hour[(key, hour_bucket)] = hc + 1
            return True

    def _evict(self, min_bucket: int, hour_bucket: int) -> None:
        if len(self._minute) > self._max_keys:
            self._minute = {k: v for k, v in self._minute.items() if k[1] >= min_bucket - 1}
        if len(self._hour) > self._max_keys:
            self._hour = {k: v for k, v in self._hour.items() if k[1] >= hour_bucket - 1}


class RedisRateLimiter:
    """Cross-process fixed-window limiter backed by ``redis.asyncio``.

    Counters live in keys ``hermes:rl:{key}:m:{bucket}`` / ``:h:{bucket}`` and
    expire automatically, so no explicit cleanup is required.
    """

    def __init__(self, client: Any, per_min: int = 5, per_hour: int = 30) -> None:
        self._client = client
        self._per_min = per_min
        self._per_hour = per_hour

    @classmethod
    def from_url(cls, url: str, per_min: int = 5, per_hour: int = 30) -> "RedisRateLimiter":
        import redis.asyncio as aioredis  # type: ignore

        client = aioredis.from_url(url, encoding="utf-8", decode_responses=True)
        return cls(client, per_min=per_min, per_hour=per_hour)

    async def _incr(self, redis_key: str, ttl: int) -> int:
        # INCR then set TTL only on first hit (INCR returns 1). Avoids
        # resetting the window on every request.
        value = await self._client.incr(redis_key)
        if value == 1:
            await self._client.expire(redis_key, ttl)
        return int(value)

    async def allow(self, key: str) -> bool:
        now = int(time.time())
        min_bucket = now // 60
        hour_bucket = now // 3600
        try:
            mc = await self._incr(f"hermes:rl:{key}:m:{min_bucket}", 120)
            hc = await self._incr(f"hermes:rl:{key}:h:{hour_bucket}", 7200)
        except Exception as e:  # noqa: BLE001 - fail closed on limiter outage
            log.warning("[ratelimit] redis error, denying request: %s", e)
            return False
        return mc <= self._per_min and hc <= self._per_hour


def build_rate_limiter(
    redis_url: Optional[str], *, per_min: int = 5, per_hour: int = 30
) -> RateLimiter:
    """Return a Redis-backed limiter when a URL is configured, else in-memory."""
    if redis_url:
        try:
            return RedisRateLimiter.from_url(redis_url, per_min=per_min, per_hour=per_hour)
        except Exception as e:  # noqa: BLE001
            log.warning("[ratelimit] falling back to in-memory limiter: %s", e)
    return InMemoryRateLimiter(per_min=per_min, per_hour=per_hour)
