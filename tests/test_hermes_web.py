"""Web chat driver: read-only enforcement, rate limiting, session expiry."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.hermes.drivers import WebChatDriver, _WebError
from app.hermes.gateway import CoreRequest, HermesGateway
from app.hermes.ratelimit import InMemoryRateLimiter
from app.hermes.sessions import InMemoryWebSessionRegistry


class RecordingCore:
    def __init__(self):
        self.calls: list[CoreRequest] = []

    async def handle(self, request: CoreRequest):
        from app.hermes.gateway import CoreResponse

        self.calls.append(request)
        return CoreResponse(text=f"answer:{request.intent}")


def _driver(sessions=None, limiter=None, core=None):
    sessions = sessions or InMemoryWebSessionRegistry()
    limiter = limiter or InMemoryRateLimiter(per_min=100, per_hour=1000)
    d = WebChatDriver(sessions, limiter)
    d._gateway = HermesGateway(core=core or RecordingCore())
    return d


@pytest.mark.asyncio
async def test_read_intent_routes():
    core = RecordingCore()
    d = _driver(core=core)
    sid = d._sessions.create()
    out = await d.handle_message(sid, "status", {})
    assert out["reply"] == "answer:status"
    assert len(core.calls) == 1


@pytest.mark.asyncio
async def test_write_intent_stripped_at_ingress():
    core = RecordingCore()
    d = _driver(core=core)
    sid = d._sessions.create()
    out = await d.handle_message(sid, "approve 5", {})
    assert "can only answer questions" in out["reply"].lower()
    assert core.calls == []  # never routed


@pytest.mark.asyncio
async def test_unknown_session_rejected():
    d = _driver()
    with pytest.raises(_WebError) as ei:
        await d.handle_message("nope", "status", {})
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_empty_and_oversized_text_rejected():
    d = _driver()
    sid = d._sessions.create()
    with pytest.raises(_WebError) as e1:
        await d.handle_message(sid, "   ", {})
    assert e1.value.status_code == 400
    with pytest.raises(_WebError) as e2:
        await d.handle_message(sid, "x" * 501, {})
    assert e2.value.status_code == 413


@pytest.mark.asyncio
async def test_rate_limit_enforced():
    d = _driver(limiter=InMemoryRateLimiter(per_min=2, per_hour=100))
    sid = d._sessions.create()
    assert (await d.handle_message(sid, "status", {}))["reply"]
    assert (await d.handle_message(sid, "status", {}))["reply"]
    with pytest.raises(_WebError) as ei:
        await d.handle_message(sid, "status", {})
    assert ei.value.status_code == 429


@pytest.mark.asyncio
async def test_rate_limit_is_per_session():
    limiter = InMemoryRateLimiter(per_min=1, per_hour=100)
    d = _driver(limiter=limiter)
    s1 = d._sessions.create()
    s2 = d._sessions.create()
    assert (await d.handle_message(s1, "status", {}))["reply"]
    # s1 exhausted, but s2 has its own budget
    assert (await d.handle_message(s2, "status", {}))["reply"]


def test_session_expiry():
    reg = InMemoryWebSessionRegistry(idle_timeout_seconds=0)
    sid = reg.create()
    time.sleep(0.01)
    assert reg.exists(sid) is False


def test_session_bounded_eviction():
    reg = InMemoryWebSessionRegistry(idle_timeout_seconds=3600, max_sessions=3)
    ids = [reg.create() for _ in range(5)]
    # Oldest evicted; only the most recent <= max survive.
    alive = [s for s in ids if reg.exists(s)]
    assert len(alive) <= 3
    assert ids[-1] in alive


def test_widget_and_session_endpoints():
    d = _driver()
    app = FastAPI()
    app.include_router(d.router())
    client = TestClient(app)
    assert client.get("/chat").status_code == 200
    r = client.post("/chat/session")
    assert r.status_code == 200
    assert r.json()["session_id"]
    assert "hermes_sid" in r.cookies


def test_message_endpoint_read_only_via_http():
    d = _driver()
    # secure_cookies default True; TestClient uses http so accept body session_id.
    app = FastAPI()
    app.include_router(d.router())
    client = TestClient(app)
    sid = client.post("/chat/session").json()["session_id"]
    r = client.post("/chat/message", json={"session_id": sid, "text": "delete everything"})
    assert r.status_code == 200
    assert "can only answer questions" in r.json()["reply"].lower()
