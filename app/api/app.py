"""FastAPI surface for the NoblePort supervisor stack.

Exposes:
  GET  /health                       — process liveness
  GET  /ready                        — db + redis (when configured) reachable
  POST /agent/invoke                 — audit-first invocation
  GET  /agent/status/{thread_id}     — last known status / kill flag
  POST /agent/kill/{thread_id}       — cooperative cancellation
  GET  /metrics/gates                — p50/p95/count latency summary
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException

from app import __version__
from app.api.models import (
    GateMetricsResponse,
    HealthResponse,
    InvokeRequest,
    InvokeResponse,
    KillResponse,
    ReadyResponse,
    StatusResponse,
)
from app.config import get_settings
from app.db.audit import AuditError
from app.db.connection import get_database, reset_database
from app.db.metrics import gate_latency_summary
from app.agent.runtime import get_supervisor, reset_supervisor
from app.agent.state import get_thread_status, request_kill
from app.redis_bus.events import get_event_bus, reset_event_bus


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Eagerly establish the DB so /ready and /health reflect reality.
    await get_database()
    get_event_bus()
    await get_supervisor()
    try:
        yield
    finally:
        await reset_supervisor()
        await reset_event_bus()
        await reset_database()


def create_app() -> FastAPI:
    app = FastAPI(
        title="NoblePort Supervisor",
        version=__version__,
        lifespan=lifespan,
    )

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

        ready_flag = db_ok and (redis_ok is None or redis_ok is True)
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
            # Audit-first contract: the agent did NOT execute. Return 503 to
            # signal a governance failure rather than 500 (which would imply
            # the agent ran and crashed).
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

    @app.get("/metrics/gates", response_model=GateMetricsResponse)
    async def metrics_gates() -> GateMetricsResponse:
        db = await get_database()
        summary = await gate_latency_summary(db)
        return GateMetricsResponse(metrics=summary)

    return app


app = create_app()
