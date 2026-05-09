"""End-to-end tests for the voice session / message flow."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _start(client, **kwargs):
    body = {"thread_id": "t-vs-1"}
    body.update(kwargs)
    r = client.post("/session", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_session_lifecycle_idle_speaking_idle(client):
    sess = _start(client)
    sid = sess["session_id"]
    assert sess["avatar_state"] == "idle"
    assert sess["fallback_text"] is False
    assert sess["ended"] is False

    r = client.post("/message", json={"session_id": sid, "content": "hello stephanie"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["domain"] == "greeting"
    assert body["avatar_state"] == "idle"
    assert body["requires_human_approval"] is False

    r = client.get(f"/session/{sid}")
    assert r.status_code == 200
    assert r.json()["avatar_state"] == "idle"


def test_message_returns_request_id_header(client):
    sess = _start(client, thread_id="t-vs-rid")
    r = client.post(
        "/message",
        json={"session_id": sess["session_id"], "content": "hello"},
        headers={"X-Request-ID": "rid-123"},
    )
    assert r.status_code == 200
    assert r.headers.get("X-Request-ID") == "rid-123"
    assert r.json()["request_id"] == "rid-123"


def test_investor_message_requires_human_approval(client):
    sess = _start(client, thread_id="t-vs-investor")
    r = client.post(
        "/message",
        json={"session_id": sess["session_id"], "content": "I'm an accredited investor, can I subscribe?"},
    )
    body = r.json()
    assert body["domain"] == "investor_compliance"
    assert body["requires_human_approval"] is True
    assert body["next_action"] == "escalate_to_human"
    assert body["disclaimers"], "investor reply must include disclaimers"


def test_defi_message_includes_disclaimer(client):
    sess = _start(client, thread_id="t-vs-defi")
    r = client.post(
        "/message",
        json={"session_id": sess["session_id"], "content": "What's the NBPT yield?"},
    )
    body = r.json()
    assert body["domain"] == "defi_nbpt"
    assert body["disclaimers"], "NBPT/DeFi reply must include disclaimers"


def test_unknown_message_routes_to_human(client):
    sess = _start(client, thread_id="t-vs-unknown")
    r = client.post(
        "/message",
        json={"session_id": sess["session_id"], "content": "What's the weather on mars?"},
    )
    body = r.json()
    assert body["domain"] == "unknown"
    assert body["requires_human_approval"] is True


def test_avatar_state_machine_endpoint(client):
    sess = _start(client, thread_id="t-vs-avatar")
    sid = sess["session_id"]
    for state in ("listening", "speaking", "error", "idle"):
        r = client.post(f"/avatar/state/{sid}", json={"state": state})
        assert r.status_code == 200, (state, r.text)
        assert r.json()["avatar_state"] == state
    r = client.post(f"/avatar/state/{sid}", json={"state": "blinking"})
    assert r.status_code == 422


def test_fallback_engages_text_mode(client):
    sess = _start(client, thread_id="t-vs-fallback")
    sid = sess["session_id"]
    r = client.post(f"/voice/fallback/{sid}", json={"reason": "tts_error"})
    assert r.status_code == 200
    assert r.json()["fallback_text"] is True
    assert r.json()["avatar_state"] == "fallback"

    r = client.post("/message", json={"session_id": sid, "content": "hi"})
    assert r.status_code == 200
    assert r.json()["avatar_state"] == "fallback"


def test_message_after_session_end_409(client):
    sess = _start(client, thread_id="t-vs-end")
    sid = sess["session_id"]
    client.post(f"/session/{sid}/end")
    r = client.post("/message", json={"session_id": sid, "content": "hello"})
    assert r.status_code == 409


def test_audit_endpoint_reflects_session_activity(client):
    sess = _start(client, thread_id="t-vs-audit")
    sid = sess["session_id"]
    client.post("/message", json={"session_id": sid, "content": "hello"})
    r = client.get("/audit")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    event_types = [e["event_type"] for e in body["entries"]]
    assert "voice.session.started" in event_types
    assert "voice.transcript.appended" in event_types
    assert body["length"] == len(body["entries"])
