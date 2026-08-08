"""Web chat session store.

Sessions are opaque, cryptographically-random tokens with an idle timeout.
They carry no user identity or PII — they exist only to scope rate limiting
and to make the public widget stateful enough to converse.

:class:`InMemoryWebSessionRegistry` is bounded (oldest sessions evicted past a
cap) and expires idle sessions. It is single-process; for multi-replica
deployments back sessions onto Redis or signed cookies (see README). That
caveat is why it is marked NON-PRODUCTION for horizontally-scaled runs.
"""
from __future__ import annotations

import secrets
import time
from collections import OrderedDict


class InMemoryWebSessionRegistry:
    """Bounded, idle-expiring in-memory session store."""

    def __init__(self, idle_timeout_seconds: int = 3600, max_sessions: int = 50_000) -> None:
        self._sessions: "OrderedDict[str, float]" = OrderedDict()
        self._idle = idle_timeout_seconds
        self._max = max_sessions

    def create(self) -> str:
        sid = secrets.token_urlsafe(24)
        now = time.time()
        self._sessions[sid] = now
        self._sessions.move_to_end(sid)
        self._evict(now)
        return sid

    def exists(self, sid: str) -> bool:
        if not sid:
            return False
        last = self._sessions.get(sid)
        if last is None:
            return False
        now = time.time()
        if now - last > self._idle:
            self._sessions.pop(sid, None)
            return False
        # Sliding idle window: touch on access.
        self._sessions[sid] = now
        self._sessions.move_to_end(sid)
        return True

    def destroy(self, sid: str) -> None:
        self._sessions.pop(sid, None)

    def _evict(self, now: float) -> None:
        # Drop expired sessions first (cheap amortised cleanup).
        expired = [s for s, t in self._sessions.items() if now - t > self._idle]
        for s in expired:
            self._sessions.pop(s, None)
        # Hard cap: evict oldest until under the limit.
        while len(self._sessions) > self._max:
            self._sessions.popitem(last=False)


# Backwards-friendly alias matching the blueprint's name.
WebSessionRegistry = InMemoryWebSessionRegistry
