"""Production supervisor runtime.

Audit-first guarantee: `Supervisor.invoke` writes the audit row first. If
the audit insert fails (raises `AuditError`), the supervisor refuses to run
the graph. Only after the audit row is durably persisted do we publish
lifecycle events on Redis and step the graph.

The graph itself is intentionally a small, deterministic, dependency-free
state machine that mirrors the LangGraph supervisor contract: a step
function operates on a state dict, the runtime checkpoints between steps,
checks the cooperative kill flag, and emits events. When the LangGraph
package is available it can be plugged in via `Supervisor.set_graph()`; the
default implementation keeps NoblePort runnable in environments where
LangGraph cannot be installed (e.g. CI without network access).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from app.config import get_settings
from app.db.audit import AuditError, insert_audit
from app.db.connection import Database, get_database
from app.db.metrics import record_invoke_latency
from app.redis_bus.events import EventBus, get_event_bus
from app.agent.checkpointing import PostgresCheckpointer, try_langgraph_postgres_saver
from app.agent.state import (
    is_kill_requested,
    set_thread_status,
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


StepFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


async def default_supervisor_step(state: dict[str, Any]) -> dict[str, Any]:
    """Single step of the default deterministic supervisor.

    Increments a counter and echoes the input message. Production deployments
    replace this with `Supervisor.set_graph()` to a LangGraph compiled graph.
    """
    next_state = dict(state)
    next_state["step"] = int(state.get("step", 0)) + 1
    msgs = list(state.get("messages") or [])
    if state.get("input"):
        msgs.append({"role": "user", "content": state["input"]})
    msgs.append({"role": "supervisor", "content": f"ack:{state.get('input', '')}"})
    next_state["messages"] = msgs
    next_state["completed"] = True
    return next_state


class Supervisor:
    """Coordinator that owns audit-first invocation, checkpointing, events."""

    def __init__(
        self,
        db: Database,
        bus: EventBus,
        *,
        step_fn: StepFn = default_supervisor_step,
        max_steps: int = 8,
    ) -> None:
        self.db = db
        self.bus = bus
        self.checkpointer = PostgresCheckpointer(db)
        self.step_fn = step_fn
        self.max_steps = max_steps
        self.settings = get_settings()
        # Surface which checkpoint backend is in use for /health.
        _, info = try_langgraph_postgres_saver(self.settings.database_url)
        self.checkpoint_backend_info = info or "project PostgresCheckpointer"

    def set_graph(self, step_fn: StepFn) -> None:
        """Inject a LangGraph compiled graph (or any async step fn)."""
        self.step_fn = step_fn

    async def _publish(
        self,
        *,
        event_type: str,
        thread_id: str,
        payload: dict[str, Any],
    ) -> None:
        await self.bus.publish(
            self.settings.redis_channel,
            {
                "type": event_type,
                "thread_id": thread_id,
                "payload": payload,
                "ts": _utcnow_iso(),
            },
        )

    async def invoke(
        self,
        *,
        thread_id: Optional[str] = None,
        input_text: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Run the supervisor for one request.

        Order of operations is the load-bearing contract of this whole
        codebase:

          1. Insert the audit row. If this fails, raise — DO NOT execute.
          2. Mark thread state = "running".
          3. Publish "agent.invoke.start".
          4. Step the graph, checkpointing each transition. Check the
             kill flag between steps.
          5. Publish "agent.invoke.end" or "agent.invoke.killed".
          6. Record invoke latency in `agent_metrics`.
        """
        tid = thread_id or str(uuid.uuid4())
        request_id = str(uuid.uuid4())
        meta = dict(metadata or {})

        # 1) AUDIT FIRST — this raises AuditError on failure and the caller
        # treats that as a hard 500. The agent does not execute.
        await insert_audit(
            self.db,
            event_type="agent.invoke.requested",
            payload={
                "request_id": request_id,
                "input": input_text,
                "metadata": meta,
            },
            thread_id=tid,
            message=f"agent invoke requested for thread {tid}",
            audit_id=request_id,
        )

        # 2) thread status
        await set_thread_status(self.db, tid, "running", last_event="agent.invoke.start")

        # 3) start event (publish AFTER audit)
        await self._publish(
            event_type="agent.invoke.start",
            thread_id=tid,
            payload={"request_id": request_id, "input": input_text},
        )

        started = time.perf_counter()
        outcome = "ok"
        result_state: dict[str, Any] = {}
        try:
            # Resume from latest checkpoint if any
            latest = await self.checkpointer.get_latest(tid)
            state: dict[str, Any] = dict(latest.state) if latest else {"messages": [], "step": 0}
            state["input"] = input_text
            state["thread_id"] = tid
            parent_id = latest.checkpoint_id if latest else None

            for _ in range(self.max_steps):
                if await is_kill_requested(self.db, tid):
                    outcome = "killed"
                    await self._publish(
                        event_type="agent.invoke.killed",
                        thread_id=tid,
                        payload={"request_id": request_id, "reason": "cooperative_kill"},
                    )
                    await set_thread_status(self.db, tid, "killed", last_event="agent.invoke.killed")
                    result_state = state
                    break

                await self._publish(
                    event_type="agent.step.start",
                    thread_id=tid,
                    payload={"request_id": request_id, "step": state.get("step")},
                )

                state = await self.step_fn(state)

                cp = await self.checkpointer.put(
                    thread_id=tid, state=state, parent_id=parent_id
                )
                parent_id = cp.checkpoint_id

                await self._publish(
                    event_type="agent.step.end",
                    thread_id=tid,
                    payload={
                        "request_id": request_id,
                        "step": state.get("step"),
                        "checkpoint_id": cp.checkpoint_id,
                    },
                )

                if state.get("completed"):
                    result_state = state
                    break
            else:
                # Max steps without completion is treated as a graceful exit.
                result_state = state
                outcome = "max_steps_exceeded"

        except asyncio.CancelledError:
            outcome = "cancelled"
            await set_thread_status(self.db, tid, "cancelled", last_event="cancelled")
            await self._publish(
                event_type="agent.invoke.cancelled",
                thread_id=tid,
                payload={"request_id": request_id},
            )
            raise
        except Exception as e:  # noqa: BLE001
            outcome = "error"
            await set_thread_status(self.db, tid, "error", last_event=str(e)[:200])
            await self._publish(
                event_type="agent.invoke.error",
                thread_id=tid,
                payload={"request_id": request_id, "error": str(e)},
            )
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            await record_invoke_latency(
                self.db,
                thread_id=tid,
                event_id=request_id,
                metric_ms=elapsed_ms,
                outcome=outcome,
                metric_name="agent_invoke_latency_ms",
            )

        if outcome == "ok":
            await set_thread_status(self.db, tid, "completed", last_event="agent.invoke.end")
            await self._publish(
                event_type="agent.invoke.end",
                thread_id=tid,
                payload={"request_id": request_id, "step": result_state.get("step")},
            )

        return {
            "thread_id": tid,
            "request_id": request_id,
            "outcome": outcome,
            "state": result_state,
        }


_supervisor: Optional[Supervisor] = None


async def get_supervisor() -> Supervisor:
    global _supervisor
    if _supervisor is None:
        db = await get_database()
        bus = get_event_bus()
        _supervisor = Supervisor(db=db, bus=bus)
    return _supervisor


async def reset_supervisor() -> None:
    global _supervisor
    _supervisor = None


# Re-export so callers don't need to import from db.audit directly when
# handling the audit-first contract at the API layer.
__all__ = ["Supervisor", "get_supervisor", "reset_supervisor", "AuditError"]
