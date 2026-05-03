"""Tests for the audit-first governance contract.

The most important behaviour in this system: if the audit insert fails, the
agent does NOT run. These tests verify both the happy path (audit row
appears before any side effects) and the failure path (no graph execution,
no events published, no metrics recorded).
"""
from __future__ import annotations

import pytest

from app.agent.runtime import Supervisor
from app.db.audit import AuditError, insert_audit, list_audit


async def test_audit_insert_persists_row(db):
    row = await insert_audit(
        db,
        event_type="agent.invoke.requested",
        payload={"input": "hello"},
        thread_id="t1",
        message="hi",
    )
    assert row["id"]
    rows = await list_audit(db, thread_id="t1")
    assert len(rows) == 1
    assert rows[0]["event_type"] == "agent.invoke.requested"
    assert rows[0]["payload"] == {"input": "hello"}


async def test_audit_failure_blocks_invocation(db, bus):
    """If insert_audit raises, Supervisor.invoke must propagate AuditError
    and must not have stepped the graph or published any events."""
    sup = Supervisor(db=db, bus=bus)

    step_called = {"n": 0}

    async def step(state):
        step_called["n"] += 1
        state["completed"] = True
        return state

    sup.set_graph(step)

    # Force the audit insert to fail by passing a non-serialisable payload
    # via metadata. We simulate via monkeypatching insert_audit to raise.
    import app.agent.runtime as runtime_mod

    async def boom(*args, **kwargs):
        raise AuditError("simulated db failure")

    runtime_mod.insert_audit = boom  # type: ignore[assignment]
    try:
        with pytest.raises(AuditError):
            await sup.invoke(input_text="hello", thread_id="t-fail")
    finally:
        # restore for other tests
        from app.db.audit import insert_audit as real
        runtime_mod.insert_audit = real  # type: ignore[assignment]

    assert step_called["n"] == 0, "graph stepped despite audit failure"
    assert bus.published == [], "events published despite audit failure"

    # No metrics either
    rows = await db.fetch_all("SELECT * FROM agent_metrics")
    assert rows == []


async def test_audit_row_written_before_events(db, bus):
    """Audit row must exist before the first event is published."""
    sup = Supervisor(db=db, bus=bus)

    async def step(state):
        # By the time the supervisor calls step(), the audit row must already
        # be in the DB.
        rows = await db.fetch_all(
            "SELECT id FROM audit_log WHERE thread_id = ?", (state["thread_id"],)
        )
        assert rows, "audit row missing before step ran"
        state["completed"] = True
        return state

    sup.set_graph(step)
    result = await sup.invoke(input_text="hi", thread_id="t-order")
    assert result["outcome"] == "ok"

    # Events should have at least the start + step + end markers, all AFTER the
    # audit row.
    types = [e["event"]["type"] for e in bus.published]
    assert "agent.invoke.start" in types
    assert "agent.invoke.end" in types


async def test_invalid_payload_raises_audit_error(db):
    class NotJsonable:
        def __repr__(self):  # noqa: D401
            return "<unserialisable>"

    # `default=str` in the audit insert will stringify most things; the only
    # way to truly break json.dumps is a circular structure.
    obj: dict = {}
    obj["self"] = obj
    with pytest.raises(AuditError):
        await insert_audit(db, event_type="x", payload=obj)
