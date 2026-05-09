"""Tamper-evident audit hash chain.

Each appended entry's hash is computed over the previous entry's hash plus
the canonical-JSON serialisation of the new entry. A consumer can verify
the whole chain by recomputing the hashes in order. Breaking any earlier
entry invalidates every subsequent entry, so the chain detects backdating,
deletion, or in-place edits.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.db.connection import Database


GENESIS_HASH = "0" * 64


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _canonical(payload: Any) -> str:
    return json.dumps(payload, default=str, sort_keys=True, separators=(",", ":"))


def compute_entry_hash(
    *,
    prev_hash: str,
    audit_id: str,
    event_type: str,
    payload_json: str,
    created_at: str,
) -> str:
    h = hashlib.sha256()
    h.update(prev_hash.encode("utf-8"))
    h.update(b"\x1f")
    h.update(audit_id.encode("utf-8"))
    h.update(b"\x1f")
    h.update(event_type.encode("utf-8"))
    h.update(b"\x1f")
    h.update(payload_json.encode("utf-8"))
    h.update(b"\x1f")
    h.update(created_at.encode("utf-8"))
    return h.hexdigest()


async def _last_hash(db: Database) -> str:
    row = await db.fetch_one(
        "SELECT entry_hash FROM audit_chain ORDER BY seq DESC LIMIT 1"
    )
    if row is None:
        return GENESIS_HASH
    return row["entry_hash"]


async def append_audit(
    db: Database,
    *,
    event_type: str,
    payload: dict[str, Any],
    audit_id: Optional[str] = None,
) -> dict[str, Any]:
    """Append a single tamper-evident entry. Returns the persisted row."""
    aid = audit_id or str(uuid.uuid4())
    created_at = _utcnow_iso()
    payload_json = _canonical(payload)
    prev = await _last_hash(db)
    entry_hash = compute_entry_hash(
        prev_hash=prev,
        audit_id=aid,
        event_type=event_type,
        payload_json=payload_json,
        created_at=created_at,
    )
    if db.dialect == "postgres":
        await db.execute(
            "INSERT INTO audit_chain (audit_id, prev_hash, entry_hash, event_type, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?::jsonb, ?)",
            (aid, prev, entry_hash, event_type, payload_json, created_at),
        )
    else:
        await db.execute(
            "INSERT INTO audit_chain (audit_id, prev_hash, entry_hash, event_type, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (aid, prev, entry_hash, event_type, payload_json, created_at),
        )
    return {
        "audit_id": aid,
        "prev_hash": prev,
        "entry_hash": entry_hash,
        "event_type": event_type,
        "payload": payload,
        "created_at": created_at,
    }


async def list_chain(
    db: Database, *, limit: int = 200
) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT seq, audit_id, prev_hash, entry_hash, event_type, payload, created_at "
        "FROM audit_chain ORDER BY seq ASC LIMIT ?",
        (limit,),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                row["payload"] = json.loads(payload)
            except (TypeError, ValueError):
                pass
        out.append(row)
    return out


async def verify_chain(db: Database) -> dict[str, Any]:
    """Recompute every hash in order. Returns ok=True only if every entry's
    stored hash matches the recomputed value AND the prev_hash links match.
    """
    rows = await db.fetch_all(
        "SELECT seq, audit_id, prev_hash, entry_hash, event_type, payload, created_at "
        "FROM audit_chain ORDER BY seq ASC"
    )
    expected_prev = GENESIS_HASH
    for row in rows:
        payload = row["payload"]
        # Re-canonicalise: the row may store either the raw JSON string
        # (sqlite) or a dict (postgres jsonb -> dict).
        if isinstance(payload, str):
            try:
                payload_obj = json.loads(payload)
            except (TypeError, ValueError):
                payload_obj = payload
        else:
            payload_obj = payload
        payload_json = _canonical(payload_obj)
        if row["prev_hash"] != expected_prev:
            return {
                "ok": False,
                "reason": "prev_hash_mismatch",
                "at_seq": row["seq"],
                "expected_prev": expected_prev,
                "actual_prev": row["prev_hash"],
            }
        recomputed = compute_entry_hash(
            prev_hash=row["prev_hash"],
            audit_id=row["audit_id"],
            event_type=row["event_type"],
            payload_json=payload_json,
            created_at=str(row["created_at"]),
        )
        if recomputed != row["entry_hash"]:
            return {
                "ok": False,
                "reason": "entry_hash_mismatch",
                "at_seq": row["seq"],
                "expected": recomputed,
                "actual": row["entry_hash"],
            }
        expected_prev = row["entry_hash"]
    return {"ok": True, "length": len(rows), "head": expected_prev}
