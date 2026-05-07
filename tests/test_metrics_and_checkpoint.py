"""Metrics + checkpoint persistence tests."""
from __future__ import annotations

import pytest

from app.agent.checkpointing import PostgresCheckpointer
from app.agent.runtime import Supervisor
from app.db.metrics import gate_latency_summary, record_invoke_latency


async def test_invoke_records_latency_metric(db, bus):
    sup = Supervisor(db=db, bus=bus)
    res = await sup.invoke(input_text="hello", thread_id="t-m1")
    assert res["outcome"] == "ok"

    rows = await db.fetch_all(
        "SELECT thread_id, event_id, metric_name, metric_ms, outcome FROM agent_metrics"
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["thread_id"] == "t-m1"
    assert r["event_id"] == res["request_id"]
    assert r["metric_name"] == "agent_invoke_latency_ms"
    assert r["outcome"] == "ok"
    assert r["metric_ms"] >= 0.0


async def test_gate_latency_summary(db):
    for ms in (10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0):
        await record_invoke_latency(
            db,
            thread_id="t",
            event_id=None,
            metric_ms=ms,
            outcome="ok",
        )
    summary = await gate_latency_summary(db)
    assert "agent_invoke_latency_ms" in summary
    s = summary["agent_invoke_latency_ms"]
    assert s["count"] == 10
    assert s["min_ms"] == 10.0
    assert s["max_ms"] == 100.0
    assert s["p50_ms"] == pytest.approx(55.0, abs=0.001)
    # p95 of 10..100 with linear interpolation: rank = 0.95*9 = 8.55 →
    # 90 + 0.55*10 = 95.5
    assert s["p95_ms"] == pytest.approx(95.5, abs=0.001)


async def test_checkpoint_round_trip(db):
    cp = PostgresCheckpointer(db)
    t = "thread-x"
    a = await cp.put(thread_id=t, state={"step": 1, "messages": ["a"]})
    await cp.put(thread_id=t, state={"step": 2, "messages": ["a", "b"]}, parent_id=a.checkpoint_id)
    latest = await cp.get_latest(t)
    assert latest is not None
    assert latest.state["step"] == 2
    assert latest.parent_id == a.checkpoint_id

    all_cps = await cp.list_all(t)
    assert len(all_cps) == 2


async def test_supervisor_resumes_from_checkpoint(db, bus):
    sup = Supervisor(db=db, bus=bus)
    res1 = await sup.invoke(input_text="first", thread_id="t-resume")
    assert res1["outcome"] == "ok"

    # Latest checkpoint must reflect step >= 1
    latest = await sup.checkpointer.get_latest("t-resume")
    assert latest is not None
    assert latest.state.get("step", 0) >= 1

    res2 = await sup.invoke(input_text="second", thread_id="t-resume")
    assert res2["outcome"] == "ok"
    # New step count strictly greater than the first run.
    assert res2["state"]["step"] > res1["state"]["step"]
