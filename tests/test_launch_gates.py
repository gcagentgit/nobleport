"""Tests for the Stephanie.ai Live Voice Launch Gate v1.

The launch rule under test: do NOT report PASS for a gate unless the
underlying capability is actually available. Each gate is exercised in
its missing, degraded, and pass-eligible configurations.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.db.connection import get_database
from app.voice import audit_chain, kill_switch, launch_gates


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("STT_PROVIDER", raising=False)
    monkeypatch.delenv("TTS_PROVIDER", raising=False)
    monkeypatch.delenv("AVATAR_ASSET_URL", raising=False)
    monkeypatch.delenv("AVATAR_PROVIDER", raising=False)
    monkeypatch.delenv("PUBLIC_WS_URL", raising=False)
    app = create_app()
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------- shape

def test_metrics_gates_required_shape(client):
    r = client.get("/metrics/gates")
    assert r.status_code == 200
    body = r.json()
    for k in ("voice", "avatar", "websocket", "audit_log", "database", "kill_switch"):
        assert k in body, f"missing required key {k}"
        assert body[k] in ("PASS", "DEGRADED", "FAIL"), f"{k} bad status: {body[k]}"
    assert launch_gates.validate_shape(body) == []


def test_validate_shape_rejects_bad_payload():
    bad_missing = {"voice": "PASS"}
    problems = launch_gates.validate_shape(bad_missing)
    assert any("missing required key" in p for p in problems)

    bad_status = {
        "voice": "MAYBE",
        "avatar": "PASS",
        "websocket": "PASS",
        "audit_log": "PASS",
        "database": "PASS",
        "kill_switch": "PASS",
    }
    problems = launch_gates.validate_shape(bad_status)
    assert any("not in" in p for p in problems)


# ----------------------------------------------------------------- voice gate

def test_voice_fail_when_providers_missing(client):
    body = client.get("/metrics/gates").json()
    assert body["voice"] == "FAIL"
    assert "STT_PROVIDER" in body["detail"]["voice"]["reason"]


def test_voice_fail_when_keys_missing(client, monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "deepgram")
    monkeypatch.setenv("TTS_PROVIDER", "elevenlabs")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    body = client.get("/metrics/gates").json()
    assert body["voice"] == "FAIL"
    assert "missing API key" in body["detail"]["voice"]["reason"]


def test_voice_pass_when_fully_configured(client, monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "deepgram")
    monkeypatch.setenv("TTS_PROVIDER", "elevenlabs")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "k1")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k2")
    body = client.get("/metrics/gates").json()
    assert body["voice"] == "PASS"


# ---------------------------------------------------------------- avatar gate

def test_avatar_fail_no_url(client):
    body = client.get("/metrics/gates").json()
    assert body["avatar"] == "FAIL"


def test_avatar_degraded_url_no_provider(client, monkeypatch):
    monkeypatch.setenv("AVATAR_ASSET_URL", "https://cdn.example.com/stephanie.glb")
    monkeypatch.delenv("AVATAR_PROVIDER", raising=False)
    body = client.get("/metrics/gates").json()
    assert body["avatar"] == "DEGRADED"


def test_avatar_pass_with_provider(client, monkeypatch):
    monkeypatch.setenv("AVATAR_ASSET_URL", "https://cdn.example.com/stephanie.glb")
    monkeypatch.setenv("AVATAR_PROVIDER", "did")
    body = client.get("/metrics/gates").json()
    assert body["avatar"] == "PASS"


# ------------------------------------------------------------- websocket gate

def test_websocket_fail_when_unset(client):
    body = client.get("/metrics/gates").json()
    assert body["websocket"] == "FAIL"


def test_websocket_pass_with_wss(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_WS_URL", "wss://api.nobleport.net/voice/ws")
    body = client.get("/metrics/gates").json()
    assert body["websocket"] == "PASS"


def test_websocket_degraded_for_insecure_remote(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_WS_URL", "ws://api.nobleport.net/voice/ws")
    body = client.get("/metrics/gates").json()
    assert body["websocket"] == "DEGRADED"


def test_websocket_invalid_scheme_fails(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_WS_URL", "https://api.nobleport.net/voice/ws")
    body = client.get("/metrics/gates").json()
    assert body["websocket"] == "FAIL"


# ---------------------------------------------------------------- database gate

def test_database_degraded_on_sqlite_default(client):
    """Tests run against SQLite — that's intentionally DEGRADED, not PASS,
    because the launch spec requires Postgres in production."""
    body = client.get("/metrics/gates").json()
    assert body["database"] == "DEGRADED"


# -------------------------------------------------------------- kill_switch

def test_kill_switch_pass_when_disengaged(client):
    body = client.get("/metrics/gates").json()
    assert body["kill_switch"] == "PASS"


def test_kill_switch_fail_when_engaged(client):
    r = client.post(
        "/voice/kill-switch", json={"engaged": True, "reason": "incident-42"}
    )
    assert r.status_code == 200
    body = client.get("/metrics/gates").json()
    assert body["kill_switch"] == "FAIL"
    assert "incident-42" in body["detail"]["kill_switch"]["reason"]
    # Overall must be FAIL when any required gate is FAIL.
    assert body["overall"] == "FAIL"


def test_kill_switch_blocks_session_start(client):
    client.post("/voice/kill-switch", json={"engaged": True, "reason": "drill"})
    r = client.post("/session", json={"thread_id": "t-ks-1"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "kill_switch_engaged"


def test_kill_switch_makes_ready_false(client):
    client.post("/voice/kill-switch", json={"engaged": True, "reason": "drill"})
    body = client.get("/ready").json()
    assert body["ready"] is False


# --------------------------------------------------------------- overall flag

def test_overall_pass_only_when_all_pass(client, monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "deepgram")
    monkeypatch.setenv("TTS_PROVIDER", "elevenlabs")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "k1")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k2")
    monkeypatch.setenv("AVATAR_ASSET_URL", "https://cdn/avatar")
    monkeypatch.setenv("AVATAR_PROVIDER", "did")
    monkeypatch.setenv("PUBLIC_WS_URL", "wss://api.nobleport.net/voice/ws")
    body = client.get("/metrics/gates").json()
    # Database is DEGRADED on SQLite — so overall must be DEGRADED, NOT PASS.
    assert body["database"] == "DEGRADED"
    assert body["overall"] == "DEGRADED"
