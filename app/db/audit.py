"""Audit-first governance.

The single source of truth: any state-changing agent action MUST insert a row
into `audit_log` *before* the action runs. If the insert fails — for any
reason — the caller raises `AuditError` and the agent does not execute.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.db.connection import Database


class AuditError(RuntimeError):
    """Raised when the audit insert fails. Blocks downstream execution."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


async def insert_audit(
    db: Database,
    *,
    event_type: str,
    payload: dict[str, Any],
    thread_id: Optional[str] = None,
    message: Optional[str] = None,
    audit_id: Optional[str] = None,
) -> dict[str, Any]:
    """Insert a single audit record.

    Returns the inserted row as a dict. Raises AuditError on any failure —
    callers must treat AuditError as a hard block on agent execution.
    """
    rid = audit_id or str(uuid.uuid4())
    now = _utcnow_iso()
    try:
        payload_text = json.dumps(payload, default=str, sort_keys=True)
    except (TypeError, ValueError) as e:
        raise AuditError(f"audit payload is not JSON-serialisable: {e}") from e

    try:
        if db.dialect == "postgres":
            await db.execute(
                "INSERT INTO audit_log (id, event_type, payload, thread_id, message, created_at) "
                "VALUES (?, ?, ?::jsonb, ?, ?, ?)",
                (rid, event_type, payload_text, thread_id, message, now),
            )
        else:
            await db.execute(
                "INSERT INTO audit_log (id, event_type, payload, thread_id, message, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (rid, event_type, payload_text, thread_id, message, now),
            )
    except Exception as e:  # noqa: BLE001 - we re-raise as AuditError
        raise AuditError(f"failed to insert audit record: {e}") from e

    return {
        "id": rid,
        "event_type": event_type,
        "payload": payload,
        "thread_id": thread_id,
        "message": message,
        "created_at": now,
    }


async def list_audit(
    db: Database, *, thread_id: Optional[str] = None, limit: int = 100
) -> list[dict[str, Any]]:
    if thread_id is not None:
        rows = await db.fetch_all(
            "SELECT id, event_type, payload, thread_id, message, created_at "
            "FROM audit_log WHERE thread_id = ? ORDER BY created_at DESC LIMIT ?",
            (thread_id, limit),
        )
    else:
        rows = await db.fetch_all(
            "SELECT id, event_type, payload, thread_id, message, created_at "
            "FROM audit_log ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    for row in rows:
        if isinstance(row.get("payload"), str):
            try:
                row["payload"] = json.loads(row["payload"])
            except (TypeError, ValueError):
                pass
    return rows
