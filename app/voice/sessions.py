"""Voice session lifecycle and transcript persistence.

A session is a single voice interaction with Stephanie.ai. The session row
records the avatar state machine and whether fallback text mode is engaged.
Every message exchanged is appended to voice_transcripts and reflected in
the audit hash chain.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.db.connection import Database
from app.voice import audit_chain


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


VALID_AVATAR_STATES = {"idle", "speaking", "listening", "error", "fallback"}


def _bool_for_dialect(db: Database, value: bool) -> object:
    return bool(value) if db.dialect == "postgres" else (1 if value else 0)


async def start_session(
    db: Database,
    *,
    thread_id: str,
    mode: str = "voice",
    fallback_text: bool = False,
) -> dict[str, Any]:
    """Create a new voice session row. Mirrored to audit_chain."""
    sid = str(uuid.uuid4())
    now = _utcnow_iso()
    await db.execute(
        "INSERT INTO voice_sessions (session_id, thread_id, mode, avatar_state, "
        "fallback_text, ended, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sid,
            thread_id,
            mode,
            "idle",
            _bool_for_dialect(db, fallback_text),
            _bool_for_dialect(db, False),
            now,
            now,
        ),
    )
    await audit_chain.append_audit(
        db,
        event_type="voice.session.started",
        payload={
            "session_id": sid,
            "thread_id": thread_id,
            "mode": mode,
            "fallback_text": fallback_text,
        },
    )
    return {
        "session_id": sid,
        "thread_id": thread_id,
        "mode": mode,
        "avatar_state": "idle",
        "fallback_text": fallback_text,
        "ended": False,
        "created_at": now,
        "updated_at": now,
    }


async def get_session(db: Database, session_id: str) -> Optional[dict[str, Any]]:
    row = await db.fetch_one(
        "SELECT session_id, thread_id, mode, avatar_state, fallback_text, ended, "
        "created_at, updated_at FROM voice_sessions WHERE session_id = ?",
        (session_id,),
    )
    if row is None:
        return None
    row["fallback_text"] = bool(row["fallback_text"])
    row["ended"] = bool(row["ended"])
    if row.get("created_at") is not None:
        row["created_at"] = str(row["created_at"])
    if row.get("updated_at") is not None:
        row["updated_at"] = str(row["updated_at"])
    return row


async def update_avatar_state(
    db: Database, session_id: str, *, state: str
) -> dict[str, Any]:
    if state not in VALID_AVATAR_STATES:
        raise ValueError(
            f"invalid avatar_state {state!r}; must be one of {sorted(VALID_AVATAR_STATES)}"
        )
    now = _utcnow_iso()
    await db.execute(
        "UPDATE voice_sessions SET avatar_state = ?, updated_at = ? WHERE session_id = ?",
        (state, now, session_id),
    )
    sess = await get_session(db, session_id)
    if sess is None:
        raise LookupError(f"session {session_id!r} not found")
    return sess


async def engage_fallback(db: Database, session_id: str, *, reason: str) -> dict[str, Any]:
    now = _utcnow_iso()
    await db.execute(
        "UPDATE voice_sessions SET fallback_text = ?, avatar_state = ?, updated_at = ? "
        "WHERE session_id = ?",
        (_bool_for_dialect(db, True), "fallback", now, session_id),
    )
    await audit_chain.append_audit(
        db,
        event_type="voice.session.fallback_engaged",
        payload={"session_id": session_id, "reason": reason},
    )
    sess = await get_session(db, session_id)
    if sess is None:
        raise LookupError(f"session {session_id!r} not found")
    return sess


async def end_session(db: Database, session_id: str) -> dict[str, Any]:
    now = _utcnow_iso()
    await db.execute(
        "UPDATE voice_sessions SET ended = ?, avatar_state = ?, updated_at = ? "
        "WHERE session_id = ?",
        (_bool_for_dialect(db, True), "idle", now, session_id),
    )
    await audit_chain.append_audit(
        db,
        event_type="voice.session.ended",
        payload={"session_id": session_id},
    )
    sess = await get_session(db, session_id)
    if sess is None:
        raise LookupError(f"session {session_id!r} not found")
    return sess


async def append_transcript(
    db: Database,
    *,
    session_id: str,
    thread_id: str,
    role: str,
    content: str,
    source: str,
    request_id: Optional[str] = None,
) -> dict[str, Any]:
    """Persist a transcript line. Source is stt | tts | text | system."""
    tid = str(uuid.uuid4())
    now = _utcnow_iso()
    await db.execute(
        "INSERT INTO voice_transcripts (id, session_id, thread_id, role, content, "
        "source, request_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, session_id, thread_id, role, content, source, request_id, now),
    )
    await audit_chain.append_audit(
        db,
        event_type="voice.transcript.appended",
        payload={
            "id": tid,
            "session_id": session_id,
            "thread_id": thread_id,
            "role": role,
            "source": source,
            "request_id": request_id,
            "content_length": len(content),
        },
    )
    return {
        "id": tid,
        "session_id": session_id,
        "thread_id": thread_id,
        "role": role,
        "content": content,
        "source": source,
        "request_id": request_id,
        "created_at": now,
    }


async def list_transcripts(
    db: Database, *, session_id: str, limit: int = 200
) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT id, session_id, thread_id, role, content, source, request_id, created_at "
        "FROM voice_transcripts WHERE session_id = ? ORDER BY created_at ASC LIMIT ?",
        (session_id, limit),
    )
    for row in rows:
        if row.get("created_at") is not None:
            row["created_at"] = str(row["created_at"])
    return rows
