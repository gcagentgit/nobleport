"""Cooperative kill + thread state tests."""
from __future__ import annotations

from app.agent.runtime import Supervisor
from app.agent.state import (
    get_thread_status,
    is_kill_requested,
    request_kill,
    set_thread_status,
)


async def test_request_kill_pre_empts_unknown_thread(db):
    res = await request_kill(db, "never-seen")
    assert res["status"] == "kill_requested"
    assert await is_kill_requested(db, "never-seen") is True


async def test_kill_flag_stops_graph_between_steps(db, bus):
    """The supervisor must check the kill flag between steps. We arrange
    for the first step to call request_kill and verify the second step is
    skipped, with status set to 'killed'."""
    sup = Supervisor(db=db, bus=bus, max_steps=4)

    seen = {"steps": 0}

    async def step(state):
        seen["steps"] += 1
        if seen["steps"] == 1:
            await request_kill(db, state["thread_id"])
            # Don't mark completed; let the kill flag stop the next iteration
            return state
        state["completed"] = True
        return state

    sup.set_graph(step)
    res = await sup.invoke(input_text="run", thread_id="t-kill")
    assert seen["steps"] == 1
    assert res["outcome"] == "killed"

    status = await get_thread_status(db, "t-kill")
    assert status is not None
    assert status["status"] == "killed"
    assert status["kill_requested"] is True

    # Killed event must have been published AFTER the audit row was written.
    types = [e["event"]["type"] for e in bus.published]
    assert "agent.invoke.killed" in types
    assert "agent.invoke.end" not in types


async def test_status_reflects_completion(db, bus):
    sup = Supervisor(db=db, bus=bus)
    await sup.invoke(input_text="hi", thread_id="t-ok")
    s = await get_thread_status(db, "t-ok")
    assert s is not None
    assert s["status"] == "completed"
    assert s["kill_requested"] is False


async def test_set_thread_status_preserves_kill_flag(db):
    await request_kill(db, "t-keep-kill")
    await set_thread_status(db, "t-keep-kill", "running")
    s = await get_thread_status(db, "t-keep-kill")
    assert s["kill_requested"] is True
    assert s["status"] == "running"
