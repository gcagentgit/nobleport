"""FastAPI surface for the NoblePort supervisor stack and Stephanie.ai
Live Voice Launch Gate v1.

Supervisor endpoints:
  GET  /health                       — process liveness + checkpoint backend
  GET  /ready                        — db + redis + kill-switch reachable
  POST /agent/invoke                 — audit-first invocation
  GET  /agent/status/{thread_id}     — last known status / kill flag
  POST /agent/kill/{thread_id}       — cooperative cancellation
  GET  /metrics/latency              — p50/p95/count latency summary

Launch gate endpoints (Stephanie.ai voice):
  GET  /metrics/gates                — voice/avatar/websocket/audit_log/database/kill_switch
  POST /session                      — start a new voice session
  GET  /session/{session_id}         — fetch session state
  POST /session/{session_id}/end     — end the session
  POST /message                      — exchange a message in a session
  GET  /audit                        — verified audit hash chain
  POST /avatar/state/{session_id}    — update avatar state machine
  POST /voice/fallback/{session_id}  — engage text-mode fallback
  GET  /voice/kill-switch            — read launch kill switch
  POST /voice/kill-switch            — engage / disengage launch kill switch
  WS   /voice/ws/{session_id}        — bidirectional voice/text channel
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from app import __version__
from app.api.models import (
    AuditChainEntry,
    AuditResponse,
    AvatarStateRequest,
    FallbackRequest,
    GateMetricsResponse,
    GateStatusResponse,
    HealthResponse,
    InvokeRequest,
    InvokeResponse,
    KillResponse,
    KillSwitchRequest,
    KillSwitchState,
    MessageRequest,
    MessageResponse,
    ReadyResponse,
    SessionResponse,
    StartSessionRequest,
    StatusResponse,
)
from app.config import get_settings
from app.db.audit import AuditError
from app.db.connection import get_database, reset_database
from app.db.metrics import gate_latency_summary
from app.agent.runtime import get_supervisor, reset_supervisor
from app.agent.state import get_thread_status, request_kill
from app.redis_bus.events import get_event_bus, reset_event_bus
from app.voice import audit_chain, brain, kill_switch, launch_gates, sessions
from app.voice.logging_mw import RequestIdLoggingMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_database()
    get_event_bus()
    await get_supervisor()
    try:
        yield
    finally:
        await reset_supervisor()
        await reset_event_bus()
        await reset_database()


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def create_app() -> FastAPI:
    app = FastAPI(
        title="NoblePort Supervisor + Stephanie.ai Voice Launch Gate",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(RequestIdLoggingMiddleware)

    # --------------------------------------------------------------- supervisor
    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        sup = await get_supervisor()
        return HealthResponse(
            status="ok",
            version=__version__,
            checkpoint_backend=sup.checkpoint_backend_info,
        )

    @app.get("/ready", response_model=ReadyResponse)
    async def ready() -> ReadyResponse:
        settings = get_settings()
        detail: dict[str, Any] = {}
        # DB check
        db_ok = False
        try:
            db = await get_database()
            await db.fetch_one("SELECT 1 AS ok")
            db_ok = True
        except Exception as e:  # noqa: BLE001
            detail["db_error"] = str(e)

        # Redis check (only when configured)
        redis_ok: bool | None
        if settings.enable_redis:
            try:
                bus = get_event_bus()
                redis_ok = await bus.ping()
                if not redis_ok:
                    detail["redis_error"] = "ping returned false"
            except Exception as e:  # noqa: BLE001
                redis_ok = False
                detail["redis_error"] = str(e)
        else:
            redis_ok = None
            detail["redis"] = "not configured (REDIS_URL unset) — skipping check"

        # Kill switch must be disengaged for /ready to be true.
        ks_engaged = False
        try:
            db = await get_database()
            ks_engaged = await kill_switch.is_engaged(db)
            if ks_engaged:
                detail["kill_switch"] = "engaged"
        except Exception as e:  # noqa: BLE001
            detail["kill_switch_error"] = str(e)

        ready_flag = db_ok and (redis_ok is None or redis_ok is True) and not ks_engaged
        return ReadyResponse(ready=ready_flag, db=db_ok, redis=redis_ok, detail=detail)

    @app.post("/agent/invoke", response_model=InvokeResponse)
    async def invoke(req: InvokeRequest) -> InvokeResponse:
        sup = await get_supervisor()
        try:
            result = await sup.invoke(
                thread_id=req.thread_id,
                input_text=req.input,
                metadata=req.metadata,
            )
        except AuditError as e:
            raise HTTPException(
                status_code=503,
                detail={"code": "audit_failure", "message": str(e)},
            )
        return InvokeResponse(**result)

    @app.get("/agent/status/{thread_id}", response_model=StatusResponse)
    async def status(thread_id: str) -> StatusResponse:
        db = await get_database()
        row = await get_thread_status(db, thread_id)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown thread_id")
        return StatusResponse(
            thread_id=row["thread_id"],
            status=row["status"],
            kill_requested=bool(row["kill_requested"]),
            last_event=row.get("last_event"),
            updated_at=str(row.get("updated_at")) if row.get("updated_at") is not None else None,
        )

    @app.post("/agent/kill/{thread_id}", response_model=KillResponse)
    async def kill(thread_id: str) -> KillResponse:
        db = await get_database()
        result = await request_kill(db, thread_id)
        return KillResponse(**result)

    @app.get("/metrics/latency", response_model=GateMetricsResponse)
    async def metrics_latency() -> GateMetricsResponse:
        db = await get_database()
        summary = await gate_latency_summary(db)
        return GateMetricsResponse(metrics=summary)

    # ------------------------------------------------------------ launch gate
    @app.get("/metrics/gates", response_model=GateStatusResponse)
    async def metrics_gates() -> GateStatusResponse:
        db = await get_database()
        report = await launch_gates.evaluate_all(db)
        return GateStatusResponse(**report)

    @app.post("/session", response_model=SessionResponse)
    async def start_session(req: StartSessionRequest) -> SessionResponse:
        db = await get_database()
        if await kill_switch.is_engaged(db):
            raise HTTPException(
                status_code=503,
                detail={"code": "kill_switch_engaged", "message": "launch kill switch is engaged"},
            )
        thread_id = req.thread_id or _new_thread_id()
        session = await sessions.start_session(
            db,
            thread_id=thread_id,
            mode=req.mode,
            fallback_text=req.fallback_text,
        )
        return SessionResponse(**session)

    @app.get("/session/{session_id}", response_model=SessionResponse)
    async def get_session(session_id: str) -> SessionResponse:
        db = await get_database()
        sess = await sessions.get_session(db, session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail="unknown session_id")
        return SessionResponse(**sess)

    @app.post("/session/{session_id}/end", response_model=SessionResponse)
    async def end_session(session_id: str) -> SessionResponse:
        db = await get_database()
        sess = await sessions.get_session(db, session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail="unknown session_id")
        ended = await sessions.end_session(db, session_id)
        return SessionResponse(**ended)

    @app.post("/avatar/state/{session_id}", response_model=SessionResponse)
    async def avatar_state(session_id: str, req: AvatarStateRequest) -> SessionResponse:
        db = await get_database()
        sess = await sessions.get_session(db, session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail="unknown session_id")
        try:
            updated = await sessions.update_avatar_state(db, session_id, state=req.state)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return SessionResponse(**updated)

    @app.post("/voice/fallback/{session_id}", response_model=SessionResponse)
    async def voice_fallback(session_id: str, req: FallbackRequest) -> SessionResponse:
        db = await get_database()
        sess = await sessions.get_session(db, session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail="unknown session_id")
        updated = await sessions.engage_fallback(db, session_id, reason=req.reason)
        return SessionResponse(**updated)

    @app.post("/message", response_model=MessageResponse)
    async def message(req: MessageRequest, request: Request) -> MessageResponse:
        db = await get_database()
        if await kill_switch.is_engaged(db):
            raise HTTPException(
                status_code=503,
                detail={"code": "kill_switch_engaged", "message": "launch kill switch is engaged"},
            )
        sess = await sessions.get_session(db, req.session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail="unknown session_id")
        if sess["ended"]:
            raise HTTPException(status_code=409, detail="session has ended")

        rid = _request_id(request)
        thread_id = sess["thread_id"]

        # Persist user transcript first (audit-first; transcript writes append
        # to the hash chain).
        user_t = await sessions.append_transcript(
            db,
            session_id=req.session_id,
            thread_id=thread_id,
            role="user",
            content=req.content,
            source=req.source,
            request_id=rid,
        )

        # Move avatar to listening then speaking around the brain step.
        await sessions.update_avatar_state(db, req.session_id, state="listening")
        response = brain.respond(req.content)
        await sessions.update_avatar_state(db, req.session_id, state="speaking")

        assistant_t = await sessions.append_transcript(
            db,
            session_id=req.session_id,
            thread_id=thread_id,
            role="assistant",
            content=response.reply,
            source="tts" if not sess["fallback_text"] else "text",
            request_id=rid,
        )

        # Avatar returns to idle after speaking. Sessions in fallback stay in
        # fallback state.
        if sess["fallback_text"]:
            final_state = "fallback"
        else:
            final_state = "idle"
        await sessions.update_avatar_state(db, req.session_id, state=final_state)

        return MessageResponse(
            session_id=req.session_id,
            thread_id=thread_id,
            request_id=rid,
            user_transcript_id=user_t["id"],
            assistant_transcript_id=assistant_t["id"],
            domain=response.domain,
            reply=response.reply,
            next_action=response.next_action,
            requires_human_approval=response.requires_human_approval,
            disclaimers=list(response.disclaimers),
            avatar_state=final_state,
        )

    @app.get("/audit", response_model=AuditResponse)
    async def audit_index(limit: int = 100) -> AuditResponse:
        db = await get_database()
        result = await audit_chain.verify_chain(db)
        entries_raw = await audit_chain.list_chain(db, limit=limit)
        entries = [
            AuditChainEntry(
                seq=e["seq"],
                audit_id=e["audit_id"],
                prev_hash=e["prev_hash"],
                entry_hash=e["entry_hash"],
                event_type=e["event_type"],
                payload=e.get("payload"),
                created_at=str(e["created_at"]),
            )
            for e in entries_raw
        ]
        return AuditResponse(
            ok=bool(result.get("ok")),
            length=int(result.get("length", len(entries))),
            head=result.get("head"),
            entries=entries,
        )

    @app.get("/voice/kill-switch", response_model=KillSwitchState)
    async def get_kill_switch() -> KillSwitchState:
        db = await get_database()
        state = await kill_switch.get_state(db)
        return KillSwitchState(**state)

    @app.post("/voice/kill-switch", response_model=KillSwitchState)
    async def set_kill_switch(req: KillSwitchRequest) -> KillSwitchState:
        db = await get_database()
        state = await kill_switch.set_state(db, engaged=req.engaged, reason=req.reason)
        await audit_chain.append_audit(
            db,
            event_type="launch.kill_switch.changed",
            payload={"engaged": state["engaged"], "reason": state["reason"]},
        )
        return KillSwitchState(**state)

    @app.websocket("/voice/ws/{session_id}")
    async def voice_ws(websocket: WebSocket, session_id: str) -> None:
        await websocket.accept()
        db = await get_database()
        sess = await sessions.get_session(db, session_id)
        if sess is None:
            await websocket.send_json(
                {"type": "error", "code": "unknown_session", "session_id": session_id}
            )
            await websocket.close()
            return
        if await kill_switch.is_engaged(db):
            await websocket.send_json({"type": "error", "code": "kill_switch_engaged"})
            await websocket.close()
            return
        await websocket.send_json(
            {"type": "ready", "session_id": session_id, "thread_id": sess["thread_id"]}
        )
        try:
            while True:
                msg = await websocket.receive_json()
                if not isinstance(msg, dict) or "content" not in msg:
                    await websocket.send_json(
                        {"type": "error", "code": "bad_message", "expected": "{content: string}"}
                    )
                    continue
                content = str(msg.get("content", ""))[:32_768]
                source = str(msg.get("source", "stt"))
                if source not in ("stt", "text"):
                    source = "stt"
                # refresh session
                sess = await sessions.get_session(db, session_id) or sess
                await sessions.append_transcript(
                    db,
                    session_id=session_id,
                    thread_id=sess["thread_id"],
                    role="user",
                    content=content,
                    source=source,
                )
                await sessions.update_avatar_state(db, session_id, state="listening")
                response = brain.respond(content)
                await sessions.update_avatar_state(db, session_id, state="speaking")
                await sessions.append_transcript(
                    db,
                    session_id=session_id,
                    thread_id=sess["thread_id"],
                    role="assistant",
                    content=response.reply,
                    source="tts" if not sess["fallback_text"] else "text",
                )
                final_state = "fallback" if sess["fallback_text"] else "idle"
                await sessions.update_avatar_state(db, session_id, state=final_state)
                await websocket.send_json(
                    {
                        "type": "message",
                        "domain": response.domain,
                        "reply": response.reply,
                        "next_action": response.next_action,
                        "requires_human_approval": response.requires_human_approval,
                        "disclaimers": list(response.disclaimers),
                        "avatar_state": final_state,
                    }
                )
        except WebSocketDisconnect:
            return

    return app


def _new_thread_id() -> str:
    import uuid

    return str(uuid.uuid4())


app = create_app()
