"""Test fixtures.

Each test gets a fresh in-memory SQLite database and a fresh NullEventBus.
We do not require a live Postgres or Redis to validate the supervisor
contract; the same code paths run against Postgres in deployment via the
abstractions in `app.db.connection` and `app.redis_bus.events`.
"""
from __future__ import annotations

from pathlib import Path

import pytest_asyncio

import app.config as config_mod
import app.db.connection as conn_mod
import app.redis_bus.events as events_mod
import app.agent.runtime as runtime_mod
from app.db.connection import Database
from app.redis_bus.events import NullEventBus, set_event_bus


@pytest_asyncio.fixture
async def tmp_db_url(tmp_path: Path):
    db_file = tmp_path / "test.db"
    url = f"sqlite+aiosqlite:///{db_file}"
    yield url


@pytest_asyncio.fixture(autouse=True)
async def _isolate_globals(monkeypatch, tmp_path):
    """Reset module-level singletons between tests so each test gets a clean
    DB / event bus / supervisor.
    """
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("SANDBOX_ALLOWED_COMMANDS", "pytest,flake8,echo")
    config_mod.reset_settings()

    # Reset cached singletons
    conn_mod._db = None
    events_mod._bus = None
    runtime_mod._supervisor = None
    yield
    # Best-effort teardown
    if conn_mod._db is not None:
        try:
            await conn_mod._db.close()
        except Exception:
            pass
        conn_mod._db = None
    if events_mod._bus is not None:
        try:
            await events_mod._bus.close()
        except Exception:
            pass
        events_mod._bus = None
    runtime_mod._supervisor = None


@pytest_asyncio.fixture
async def db() -> Database:
    from app.db.connection import get_database

    return await get_database()


@pytest_asyncio.fixture
async def bus() -> NullEventBus:
    bus = NullEventBus()
    set_event_bus(bus)
    return bus
