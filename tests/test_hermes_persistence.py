"""DB-backed consent persistence, audit integration, and HTTP host lifecycle."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.audit import list_audit
from app.hermes.consent import DatabaseConsentRegistry
from app.hermes.drivers import WebChatDriver, WhatsAppDriver
from app.hermes.gateway import HermesGateway, LocalEchoCore, make_db_audit
from app.hermes.http_host import HermesHTTPHost
from app.hermes.ratelimit import InMemoryRateLimiter
from app.hermes.sessions import InMemoryWebSessionRegistry


@pytest.mark.asyncio
async def test_database_consent_persists_and_logs_events(db):
    reg = DatabaseConsentRegistry(db, phone_salt="salt")
    phone = "whatsapp:+15551112222"
    assert (await reg.get(phone)).status == "unknown"
    await reg.set(phone, "pending", "first_contact")
    await reg.set(phone, "opted_in", "sms_confirm")
    rec = await reg.get(phone)
    assert rec.status == "opted_in"

    # Immutable event log has both transitions; no raw phone stored.
    rows = await db.fetch_all("SELECT status, source, phone_hash FROM whatsapp_consent_events")
    statuses = [r["status"] for r in rows]
    assert "pending" in statuses and "opted_in" in statuses
    for r in rows:
        assert phone not in r["phone_hash"]


@pytest.mark.asyncio
async def test_make_db_audit_writes_to_audit_log(db):
    audit = make_db_audit(db, phone_salt="salt")
    gw = HermesGateway(core=LocalEchoCore(), audit_hook=audit, phone_salt="salt")
    from app.hermes.gateway import HermesMessage

    await gw.receive(HermesMessage(channel="web", channel_user_id="s1", raw_text="status"))
    rows = await list_audit(db, limit=10)
    assert any(r["event_type"] == "hermes.route" for r in rows)


def test_http_host_mounts_and_runs_lifecycle():
    gw = HermesGateway(core=LocalEchoCore())
    wa = WhatsAppDriver(
        "AC", "tok", "whatsapp:+1", None, validate_signatures=False
    )
    # give whatsapp a real consent registry so handle_inbound works
    from app.hermes.consent import InMemoryConsentRegistry

    wa._consent = InMemoryConsentRegistry()
    web = WebChatDriver(InMemoryWebSessionRegistry(), InMemoryRateLimiter())

    host = HermesHTTPHost(gw)
    host.mount(wa)
    host.mount(web)

    # Both drivers registered with the gateway for push paths.
    assert "whatsapp" in gw._drivers and "web" in gw._drivers

    with TestClient(host.app) as client:  # triggers lifespan start/stop
        # Web routes are mounted and gateway wired via lifespan start().
        assert client.get("/chat").status_code == 200
        sid = client.post("/chat/session").json()["session_id"]
        r = client.post("/chat/message", json={"session_id": sid, "text": "help"})
        assert r.status_code == 200
    # After context exit, drivers stopped without error.


def test_http_host_rejects_non_http_driver():
    from app.hermes.gateway import HermesDriver

    class SocketOnly(HermesDriver):
        name = "sock"

        async def start(self, gateway):
            pass

        async def send(self, channel_user_id, response):
            pass

    gw = HermesGateway(core=LocalEchoCore())
    host = HermesHTTPHost(gw)
    with pytest.raises(TypeError):
        host.mount(SocketOnly())
