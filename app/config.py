"""Runtime configuration loaded from environment variables.

Defaults are chosen so tests and local runs work without a Postgres or Redis
service available. Production overrides via DATABASE_URL / REDIS_URL.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    redis_channel: str
    allowed_commands: tuple[str, ...]
    sandbox_timeout_seconds: int
    enable_redis: bool

    @classmethod
    def from_env(cls) -> "Settings":
        database_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./nobleport.db")
        redis_url = os.environ.get("REDIS_URL", "")
        channel = os.environ.get("AGENT_EVENT_CHANNEL", "agent.events")
        allowed = tuple(
            c.strip()
            for c in os.environ.get("SANDBOX_ALLOWED_COMMANDS", "pytest,flake8").split(",")
            if c.strip()
        )
        timeout = int(os.environ.get("SANDBOX_TIMEOUT_SECONDS", "60"))
        enable_redis = bool(redis_url)
        return cls(
            database_url=database_url,
            redis_url=redis_url,
            redis_channel=channel,
            allowed_commands=allowed,
            sandbox_timeout_seconds=timeout,
            enable_redis=enable_redis,
        )


_cached: Settings | None = None


def get_settings() -> Settings:
    global _cached
    if _cached is None:
        _cached = Settings.from_env()
    return _cached


def reset_settings() -> None:
    """Test helper to re-read environment variables."""
    global _cached
    _cached = None
