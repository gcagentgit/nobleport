"""Tests for request-id propagation and structured access logging."""
from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_generates_request_id_when_absent(client):
    r = client.get("/health")
    rid = r.headers.get("X-Request-ID")
    assert rid is not None
    assert len(rid) >= 8


def test_echoes_safe_request_id(client):
    r = client.get("/health", headers={"X-Request-ID": "abc-123:9"})
    assert r.headers.get("X-Request-ID") == "abc-123:9"


def test_replaces_unsafe_request_id(client):
    r = client.get("/health", headers={"X-Request-ID": "bad header value"})
    rid = r.headers.get("X-Request-ID")
    # space is invalid, so we generate a fresh uuid instead.
    assert rid is not None
    assert rid != "bad header value"
    assert " " not in rid


def test_access_log_is_json_with_request_id(client):
    log = logging.getLogger("nobleport.access")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
    try:
        client.get("/health", headers={"X-Request-ID": "rid-log-1"})
    finally:
        log.removeHandler(handler)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert lines, "expected at least one access log line"
    parsed = [json.loads(ln) for ln in lines]
    matches = [p for p in parsed if p.get("request_id") == "rid-log-1"]
    assert matches, f"expected log line with rid-log-1, got {parsed}"
    entry = matches[0]
    assert entry["msg"] == "http_request"
    assert entry["method"] == "GET"
    assert entry["path"] == "/health"
    assert entry["status"] == 200
    assert isinstance(entry["latency_ms"], (int, float))
