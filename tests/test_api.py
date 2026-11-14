"""End-to-end API tests using FastAPI's test client."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "checkpoint_backend" in body


def test_ready_db_ok_no_redis(client):
    r = client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["db"] is True
    assert body["redis"] is None  # not configured


def test_invoke_happy_path(client):
    r = client.post(
        "/agent/invoke",
        json={"input": "hello supervisor", "thread_id": "t-api-1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["thread_id"] == "t-api-1"
    assert body["outcome"] == "ok"
    assert body["state"]["step"] >= 1


def test_invoke_validation_rejects_bad_thread_id(client):
    r = client.post(
        "/agent/invoke",
        json={"input": "x", "thread_id": "bad space"},
    )
    assert r.status_code == 422


def test_invoke_validation_rejects_empty_input(client):
    r = client.post("/agent/invoke", json={"input": ""})
    assert r.status_code == 422


def test_status_unknown_thread_404(client):
    r = client.get("/agent/status/never-existed")
    assert r.status_code == 404


def test_status_after_invoke(client):
    client.post("/agent/invoke", json={"input": "hi", "thread_id": "t-api-2"})
    r = client.get("/agent/status/t-api-2")
    assert r.status_code == 200
    body = r.json()
    assert body["thread_id"] == "t-api-2"
    assert body["status"] == "completed"
    assert body["kill_requested"] is False


def test_kill_creates_state_for_unknown_thread(client):
    r = client.post("/agent/kill/t-api-kill")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "kill_requested"

    s = client.get("/agent/status/t-api-kill").json()
    assert s["kill_requested"] is True


def test_metrics_latency_returns_summary(client):
    """Latency lives at /metrics/latency now so /metrics/gates can carry the
    voice launch gate status (voice/avatar/websocket/audit_log/database/
    kill_switch) required by the Live Voice Launch Gate v1 spec.
    """
    client.post("/agent/invoke", json={"input": "a", "thread_id": "t-api-m1"})
    client.post("/agent/invoke", json={"input": "b", "thread_id": "t-api-m2"})
    r = client.get("/metrics/latency")
    assert r.status_code == 200
    body = r.json()
    assert "agent_invoke_latency_ms" in body["metrics"]
    s = body["metrics"]["agent_invoke_latency_ms"]
    assert s["count"] >= 2
    assert s["p50_ms"] >= 0.0
    assert s["p95_ms"] >= s["p50_ms"]


def test_audit_failure_returns_503(client, monkeypatch):
    """If insert_audit raises AuditError, /agent/invoke must return 503 and
    the agent must not have run."""
    from app.db.audit import AuditError
    import app.agent.runtime as runtime_mod

    async def boom(*args, **kwargs):
        raise AuditError("simulated")

    real = runtime_mod.insert_audit
    runtime_mod.insert_audit = boom  # type: ignore[assignment]
    try:
        r = client.post("/agent/invoke", json={"input": "x", "thread_id": "t-api-fail"})
    finally:
        runtime_mod.insert_audit = real  # type: ignore[assignment]

    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "audit_failure"

    # no metrics, no thread state
    s = client.get("/agent/status/t-api-fail")
    assert s.status_code == 404
