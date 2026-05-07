"""Async Redis publisher for agent lifecycle and step events.

Audit-first invariant: callers MUST insert an audit row before publishing. We
do not bypass that, but we do not re-implement it here either — this module
only handles transport. The supervisor runtime is responsible for ordering.

If `REDIS_URL` is unset (typical for tests / CI), `get_event_bus` returns a
`NullEventBus` that records published events in-memory so behaviour can still
be asserted without a live Redis. Production sets `REDIS_URL` and gets the
real publisher.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from app.config import get_settings


class EventBus(Protocol):
    async def publish(self, channel: str, event: dict[str, Any]) -> int: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...
    @property
    def published(self) -> list[dict[str, Any]]: ...


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class NullEventBus:
    """In-memory bus used when REDIS_URL is not configured.

    Records every published event so tests can assert on emission. `ping()`
    always succeeds — it represents "no Redis configured, so nothing to
    fail". `/ready` only checks Redis when configured.
    """

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    async def publish(self, channel: str, event: dict[str, Any]) -> int:
        async with self._lock:
            self._events.append({"channel": channel, "event": event, "ts": _utcnow_iso()})
        return 1

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    @property
    def published(self) -> list[dict[str, Any]]:
        return list(self._events)


class RedisEventBus:
    """Real async Redis publisher backed by `redis.asyncio`."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._client: Any = None
        self._published: list[dict[str, Any]] = []

    async def _connect(self) -> None:
        if self._client is not None:
            return
        import redis.asyncio as aioredis  # type: ignore

        self._client = aioredis.from_url(self.url, encoding="utf-8", decode_responses=True)

    async def publish(self, channel: str, event: dict[str, Any]) -> int:
        await self._connect()
        payload = json.dumps(event, default=str, sort_keys=True)
        n = await self._client.publish(channel, payload)
        self._published.append({"channel": channel, "event": event, "ts": _utcnow_iso()})
        return int(n)

    async def ping(self) -> bool:
        try:
            await self._connect()
            return bool(await self._client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except AttributeError:
                # older redis-py uses .close()
                await self._client.close()
            self._client = None

    @property
    def published(self) -> list[dict[str, Any]]:
        return list(self._published)


_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    """Singleton event bus selected at process boot."""
    global _bus
    if _bus is None:
        settings = get_settings()
        if settings.enable_redis:
            _bus = RedisEventBus(settings.redis_url)
        else:
            _bus = NullEventBus()
    return _bus


async def reset_event_bus() -> None:
    """Test helper — close and discard the cached bus."""
    global _bus
    if _bus is not None:
        await _bus.close()
        _bus = None


def set_event_bus(bus: EventBus) -> None:
    """Allow tests / wiring to inject a specific bus."""
    global _bus
    _bus = bus
