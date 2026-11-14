"""Global launch kill switch.

Singleton row at id=1 in launch_kill_switch. When engaged, /ready and the
launch gate report FAIL for kill_switch and the API refuses to start new
voice sessions or accept new messages on existing ones.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.db.connection import Database


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


async def get_state(db: Database) -> dict:
    row = await db.fetch_one(
        "SELECT engaged, reason, updated_at FROM launch_kill_switch WHERE id = 1"
    )
    if row is None:
        return {"engaged": False, "reason": None, "updated_at": None}
    return {
        "engaged": bool(row["engaged"]),
        "reason": row.get("reason"),
        "updated_at": str(row["updated_at"]) if row.get("updated_at") is not None else None,
    }


async def set_state(db: Database, *, engaged: bool, reason: Optional[str]) -> dict:
    now = _utcnow_iso()
    existing = await db.fetch_one(
        "SELECT id FROM launch_kill_switch WHERE id = 1"
    )
    if db.dialect == "postgres":
        engaged_val: object = bool(engaged)
    else:
        engaged_val = 1 if engaged else 0
    if existing is None:
        await db.execute(
            "INSERT INTO launch_kill_switch (id, engaged, reason, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (1, engaged_val, reason, now),
        )
    else:
        await db.execute(
            "UPDATE launch_kill_switch SET engaged = ?, reason = ?, updated_at = ? "
            "WHERE id = 1",
            (engaged_val, reason, now),
        )
    return await get_state(db)


async def is_engaged(db: Database) -> bool:
    state = await get_state(db)
    return bool(state["engaged"])
