"""Async DB connection that supports Postgres (asyncpg) in production and
SQLite (aiosqlite) in tests / local dev.

The wrapper intentionally exposes a tiny surface (execute / fetch_one /
fetch_all) so the rest of the codebase doesn't need to know which dialect is
in use. Tables are created via `migrate()` on startup; this is sufficient for
this project — there is no historical schema to upgrade.
"""
from __future__ import annotations

import asyncio
from typing import Any, Iterable, Optional, Sequence

from app.config import get_settings


class Database:
    """Minimal async DB facade."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._lock = asyncio.Lock()
        self._sqlite_conn = None  # aiosqlite connection
        self._pg_pool = None  # asyncpg pool
        self.dialect: str
        if url.startswith("sqlite"):
            self.dialect = "sqlite"
        elif url.startswith("postgres") or url.startswith("postgresql"):
            self.dialect = "postgres"
        else:
            raise ValueError(f"Unsupported DATABASE_URL: {url!r}")

    # ------------------------------------------------------------------ connect
    async def connect(self) -> None:
        if self.dialect == "sqlite":
            import aiosqlite

            path = self.url.split("///", 1)[1] if "///" in self.url else ":memory:"
            # aiosqlite treats ':memory:' specially. Use shared cache to allow
            # access from multiple coroutines on the same connection object.
            self._sqlite_conn = await aiosqlite.connect(path)
            await self._sqlite_conn.execute("PRAGMA foreign_keys = ON;")
            await self._sqlite_conn.commit()
        else:
            import asyncpg  # type: ignore

            # Strip optional dialect qualifier (e.g. postgresql+asyncpg://)
            url = self.url.replace("postgresql+asyncpg://", "postgresql://")
            url = url.replace("postgres+asyncpg://", "postgresql://")
            self._pg_pool = await asyncpg.create_pool(dsn=url, min_size=1, max_size=10)

    async def close(self) -> None:
        if self._sqlite_conn is not None:
            await self._sqlite_conn.close()
            self._sqlite_conn = None
        if self._pg_pool is not None:
            await self._pg_pool.close()
            self._pg_pool = None

    # ----------------------------------------------------------------- execute
    def _convert_params(self, sql: str) -> str:
        """Translate `?` (sqlite) <-> `$1, $2 ...` (postgres) when needed.

        Source code uses `?` placeholders. For postgres, rewrite them to `$N`.
        """
        if self.dialect == "postgres" and "?" in sql:
            out_parts: list[str] = []
            n = 0
            for ch in sql:
                if ch == "?":
                    n += 1
                    out_parts.append(f"${n}")
                else:
                    out_parts.append(ch)
            return "".join(out_parts)
        return sql

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        sql_translated = self._convert_params(sql)
        if self.dialect == "sqlite":
            assert self._sqlite_conn is not None
            async with self._lock:
                await self._sqlite_conn.execute(sql_translated, tuple(params))
                await self._sqlite_conn.commit()
        else:
            assert self._pg_pool is not None
            async with self._pg_pool.acquire() as conn:
                await conn.execute(sql_translated, *params)

    async def execute_many(self, sql: str, params_seq: Iterable[Sequence[Any]]) -> None:
        sql_translated = self._convert_params(sql)
        if self.dialect == "sqlite":
            assert self._sqlite_conn is not None
            async with self._lock:
                await self._sqlite_conn.executemany(sql_translated, [tuple(p) for p in params_seq])
                await self._sqlite_conn.commit()
        else:
            assert self._pg_pool is not None
            async with self._pg_pool.acquire() as conn:
                await conn.executemany(sql_translated, [tuple(p) for p in params_seq])

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[dict[str, Any]]:
        sql_translated = self._convert_params(sql)
        if self.dialect == "sqlite":
            assert self._sqlite_conn is not None
            async with self._lock:
                cur = await self._sqlite_conn.execute(sql_translated, tuple(params))
                row = await cur.fetchone()
                cols = [d[0] for d in cur.description] if cur.description else []
                await cur.close()
            return dict(zip(cols, row)) if row else None
        else:
            assert self._pg_pool is not None
            async with self._pg_pool.acquire() as conn:
                row = await conn.fetchrow(sql_translated, *params)
                return dict(row) if row else None

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        sql_translated = self._convert_params(sql)
        if self.dialect == "sqlite":
            assert self._sqlite_conn is not None
            async with self._lock:
                cur = await self._sqlite_conn.execute(sql_translated, tuple(params))
                rows = await cur.fetchall()
                cols = [d[0] for d in cur.description] if cur.description else []
                await cur.close()
            return [dict(zip(cols, row)) for row in rows]
        else:
            assert self._pg_pool is not None
            async with self._pg_pool.acquire() as conn:
                rows = await conn.fetch(sql_translated, *params)
                return [dict(r) for r in rows]

    # --------------------------------------------------------------- migrate
    async def migrate(self) -> None:
        """Create the audit_log, agent_metrics, and checkpoint tables."""
        from app.db.schema import sqlite_schema, postgres_schema

        statements = sqlite_schema() if self.dialect == "sqlite" else postgres_schema()
        for stmt in statements:
            await self.execute(stmt)


_db: Optional[Database] = None


async def get_database() -> Database:
    global _db
    if _db is None:
        settings = get_settings()
        _db = Database(settings.database_url)
        await _db.connect()
        await _db.migrate()
    return _db


async def reset_database() -> None:
    """Test helper — close and discard the cached database."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None
