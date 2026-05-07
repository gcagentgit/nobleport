"""Per-thread runtime state (status, kill flag).

LangGraph at the version pinned for this project does not expose mid-flight
hard cancellation. Following the requirements, we implement a cooperative
kill flag stored in `agent_thread_state`. The supervisor checks the flag at
each step and exits gracefully if set. This is documented as the limitation
in the project README.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.db.connection import Database


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


async def _upsert(
    db: Database, thread_id: str, *, status: str, kill_requested: Optional[bool] = None,
    last_event: Optional[str] = None,
) -> None:
    existing = await db.fetch_one(
        "SELECT thread_id, kill_requested FROM agent_thread_state WHERE thread_id = ?",
        (thread_id,),
    )
    now = _utcnow_iso()
    if existing is None:
        kr = 1 if kill_requested else 0
        if db.dialect == "postgres":
            await db.execute(
                "INSERT INTO agent_thread_state (thread_id, status, kill_requested, last_event, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (thread_id, status, bool(kr), last_event, now),
            )
        else:
            await db.execute(
                "INSERT INTO agent_thread_state (thread_id, status, kill_requested, last_event, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (thread_id, status, kr, last_event, now),
            )
    else:
        # Preserve a previously-set kill flag unless explicitly updated.
        if kill_requested is None:
            kr = existing["kill_requested"]
            if db.dialect == "sqlite":
                kr = int(bool(kr))
        else:
            kr = bool(kill_requested) if db.dialect == "postgres" else int(bool(kill_requested))
        await db.execute(
            "UPDATE agent_thread_state SET status = ?, kill_requested = ?, last_event = ?, updated_at = ? "
            "WHERE thread_id = ?",
            (status, kr, last_event, now, thread_id),
        )


async def set_thread_status(
    db: Database, thread_id: str, status: str, *, last_event: Optional[str] = None
) -> None:
    await _upsert(db, thread_id, status=status, last_event=last_event)


async def request_kill(db: Database, thread_id: str) -> dict[str, str]:
    """Mark a thread for cooperative cancellation.

    Returns the new status. Idempotent: calling on an already-killed thread
    returns the current state.
    """
    existing = await db.fetch_one(
        "SELECT thread_id, status FROM agent_thread_state WHERE thread_id = ?",
        (thread_id,),
    )
    if existing is None:
        # Allow kill of a thread we have not seen yet — treat as a pre-emptive
        # cancel so the next invocation observes it.
        await _upsert(
            db,
            thread_id,
            status="kill_requested",
            kill_requested=True,
            last_event="kill_requested",
        )
        return {"thread_id": thread_id, "status": "kill_requested", "previous_status": "unknown"}

    await _upsert(
        db,
        thread_id,
        status="kill_requested",
        kill_requested=True,
        last_event="kill_requested",
    )
    return {
        "thread_id": thread_id,
        "status": "kill_requested",
        "previous_status": existing["status"],
    }


async def is_kill_requested(db: Database, thread_id: str) -> bool:
    row = await db.fetch_one(
        "SELECT kill_requested FROM agent_thread_state WHERE thread_id = ?",
        (thread_id,),
    )
    if row is None:
        return False
    return bool(row["kill_requested"])


async def get_thread_status(db: Database, thread_id: str) -> Optional[dict]:
    row = await db.fetch_one(
        "SELECT thread_id, status, kill_requested, last_event, updated_at "
        "FROM agent_thread_state WHERE thread_id = ?",
        (thread_id,),
    )
    if row is None:
        return None
    row["kill_requested"] = bool(row["kill_requested"])
    return row
