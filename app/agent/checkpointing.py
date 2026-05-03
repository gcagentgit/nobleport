"""Persistent LangGraph checkpointing.

LangGraph's official `langgraph.checkpoint.postgres.PostgresSaver` is the
preferred production backend. If it is installed in the runtime, the
supervisor delegates to it; otherwise we provide an equivalent
`PostgresCheckpointer` here that persists checkpoints to the
`langgraph_checkpoints` table via the project's async DB facade. The same
table works for SQLite in tests.

This is intentionally a small, production-safe checkpointer: it serialises
the supervisor's state dict to JSON, stores the most recent checkpoint per
thread, and lets the runtime resume by loading the latest row. This is
sufficient for the supervisor patterns currently used by NoblePort and is
fully compatible with the LangGraph checkpoint contract surface that the
runtime in `app.agent.runtime` calls.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from app.db.connection import Database


@dataclass
class CheckpointTuple:
    thread_id: str
    checkpoint_id: str
    parent_id: Optional[str]
    state: dict[str, Any]


class PostgresCheckpointer:
    """Persistent checkpointer backed by `langgraph_checkpoints`.

    Despite the class name, it transparently works on SQLite (the sql is
    dialect-portable). The name reflects production intent: in deployment
    the same table lives in Postgres.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    async def put(
        self,
        *,
        thread_id: str,
        state: dict[str, Any],
        parent_id: Optional[str] = None,
        checkpoint_id: Optional[str] = None,
    ) -> CheckpointTuple:
        cid = checkpoint_id or str(uuid.uuid4())
        state_text = json.dumps(state, default=str, sort_keys=True)
        if self.db.dialect == "postgres":
            await self.db.execute(
                "INSERT INTO langgraph_checkpoints (thread_id, checkpoint_id, parent_id, state) "
                "VALUES (?, ?, ?, ?::jsonb)",
                (thread_id, cid, parent_id, state_text),
            )
        else:
            await self.db.execute(
                "INSERT INTO langgraph_checkpoints (thread_id, checkpoint_id, parent_id, state) "
                "VALUES (?, ?, ?, ?)",
                (thread_id, cid, parent_id, state_text),
            )
        return CheckpointTuple(thread_id=thread_id, checkpoint_id=cid, parent_id=parent_id, state=state)

    async def get_latest(self, thread_id: str) -> Optional[CheckpointTuple]:
        row = await self.db.fetch_one(
            "SELECT thread_id, checkpoint_id, parent_id, state FROM langgraph_checkpoints "
            "WHERE thread_id = ? ORDER BY seq DESC LIMIT 1",
            (thread_id,),
        )
        if row is None:
            return None
        state = row["state"]
        if isinstance(state, str):
            state = json.loads(state)
        return CheckpointTuple(
            thread_id=row["thread_id"],
            checkpoint_id=row["checkpoint_id"],
            parent_id=row["parent_id"],
            state=state,
        )

    async def list_all(self, thread_id: str) -> list[CheckpointTuple]:
        rows = await self.db.fetch_all(
            "SELECT thread_id, checkpoint_id, parent_id, state FROM langgraph_checkpoints "
            "WHERE thread_id = ? ORDER BY seq ASC",
            (thread_id,),
        )
        out: list[CheckpointTuple] = []
        for r in rows:
            state = r["state"]
            if isinstance(state, str):
                state = json.loads(state)
            out.append(
                CheckpointTuple(
                    thread_id=r["thread_id"],
                    checkpoint_id=r["checkpoint_id"],
                    parent_id=r["parent_id"],
                    state=state,
                )
            )
        return out


def try_langgraph_postgres_saver(database_url: str):
    """Return an `(saver, kind)` tuple if the official PostgresSaver is
    available, else `(None, reason)`.

    The supervisor calls this at boot. If LangGraph's PostgresSaver is
    importable AND the URL points at Postgres, we hand it through; otherwise
    we fall back to the project-local `PostgresCheckpointer`. The reason is
    surfaced in `/health` so operators can confirm which backend is live.
    """
    if not (database_url.startswith("postgres") or database_url.startswith("postgresql")):
        return None, "database_url is not postgres"
    try:
        from langgraph.checkpoint.postgres import PostgresSaver  # type: ignore

        return PostgresSaver, "langgraph.checkpoint.postgres.PostgresSaver"
    except Exception as e:  # noqa: BLE001
        return None, f"langgraph PostgresSaver unavailable: {e}"
