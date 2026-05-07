"""Event bus tests — emission ordering and audit-first invariant."""
from __future__ import annotations

import pytest

from app.agent.runtime import Supervisor
from app.db.audit import AuditError
from app.redis_bus.events import NullEventBus


async def test_events_published_after_audit(db, bus):
    sup = Supervisor(db=db, bus=bus)
    await sup.invoke(input_text="hello", thread_id="t-e1")
    types = [e["event"]["type"] for e in bus.published]
    # Lifecycle ordering invariant
    assert types[0] == "agent.invoke.start"
    assert types[-1] == "agent.invoke.end"
    assert "agent.step.start" in types
    assert "agent.step.end" in types
    # Each event names the same thread_id
    for e in bus.published:
        assert e["event"]["thread_id"] == "t-e1"


async def test_no_events_when_audit_fails(db, bus):
    sup = Supervisor(db=db, bus=bus)

    import app.agent.runtime as runtime_mod

    async def boom(*args, **kwargs):
        raise AuditError("nope")

    real = runtime_mod.insert_audit
    runtime_mod.insert_audit = boom  # type: ignore[assignment]
    try:
        with pytest.raises(AuditError):
            await sup.invoke(input_text="x", thread_id="t-e2")
    finally:
        runtime_mod.insert_audit = real  # type: ignore[assignment]

    assert bus.published == []


async def test_null_bus_ping_succeeds_when_redis_disabled():
    bus = NullEventBus()
    assert await bus.ping() is True
