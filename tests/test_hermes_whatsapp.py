"""WhatsApp driver: signature validation, consent transitions, outbound opt-in."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.hermes.consent import InMemoryConsentRegistry
from app.hermes.drivers import (
    CONSENT_CONFIRMED,
    CONSENT_PROMPT,
    OPT_OUT_ACK,
    WA_HELP_TEXT,
    WhatsAppDriver,
)
from app.hermes.gateway import CoreRequest, HermesGateway
from app.hermes.twilio_sig import compute_signature, validate_signature

AUTH = "test_auth_token"


class RecordingCore:
    def __init__(self):
        self.calls: list[CoreRequest] = []

    async def handle(self, request: CoreRequest):
        from app.hermes.gateway import CoreResponse

        self.calls.append(request)
        return CoreResponse(text="core-reply")


class RecordingSender:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, *, from_: str, to: str, body: str) -> None:
        self.sent.append({"from": from_, "to": to, "body": body})


def _driver(consent=None, sender=None, gateway=None, **kw):
    consent = consent or InMemoryConsentRegistry()
    d = WhatsAppDriver(
        "AC_sid", AUTH, "whatsapp:+15550000000", consent, sender=sender or RecordingSender(), **kw
    )
    d._gateway = gateway
    return d


# --------------------------------------------------------------- signature
def test_signature_roundtrip():
    url = "https://example.com/twilio/whatsapp"
    params = {"From": "whatsapp:+1555", "Body": "hi", "MessageSid": "SM1"}
    sig = compute_signature(AUTH, url, params)
    assert validate_signature(AUTH, url, params, sig)
    assert not validate_signature(AUTH, url, params, "wrong")
    assert not validate_signature(AUTH, url, {**params, "Body": "tampered"}, sig)


def test_signature_empty_rejected():
    assert not validate_signature(AUTH, "https://x/y", {}, "")


# --------------------------------------------------------------- consent gate
@pytest.mark.asyncio
async def test_unknown_number_gets_prompt_not_routed():
    core = RecordingCore()
    d = _driver(gateway=HermesGateway(core=core))
    reply = await d.handle_inbound("whatsapp:+1999", "hello")
    assert reply == CONSENT_PROMPT
    assert core.calls == []
    rec = await d._consent.get("whatsapp:+1999")
    assert rec.status == "pending"


@pytest.mark.asyncio
async def test_pending_yes_opts_in_then_routes():
    core = RecordingCore()
    consent = InMemoryConsentRegistry()
    d = _driver(consent=consent, gateway=HermesGateway(core=core))
    await d.handle_inbound("whatsapp:+1999", "hi")  # -> pending
    reply = await d.handle_inbound("whatsapp:+1999", "YES")
    assert reply == CONSENT_CONFIRMED
    assert (await consent.get("whatsapp:+1999")).status == "opted_in"
    # Now a real message routes to Core.
    routed = await d.handle_inbound("whatsapp:+1999", "status")
    assert routed == "core-reply"
    assert len(core.calls) == 1


@pytest.mark.asyncio
async def test_pending_non_confirm_reprompts_not_routed():
    core = RecordingCore()
    d = _driver(gateway=HermesGateway(core=core))
    await d.handle_inbound("whatsapp:+1", "hi")
    reply = await d.handle_inbound("whatsapp:+1", "what do you do")
    assert reply == CONSENT_PROMPT
    assert core.calls == []


@pytest.mark.asyncio
async def test_stop_opts_out_always():
    consent = InMemoryConsentRegistry()
    d = _driver(consent=consent)
    await consent.set("whatsapp:+1", "opted_in", "test")
    reply = await d.handle_inbound("whatsapp:+1", "STOP")
    assert reply == OPT_OUT_ACK
    assert (await consent.get("whatsapp:+1")).status == "opted_out"


@pytest.mark.asyncio
async def test_help_returns_info_not_routed():
    core = RecordingCore()
    d = _driver(gateway=HermesGateway(core=core))
    assert await d.handle_inbound("whatsapp:+1", "HELP") == WA_HELP_TEXT
    assert core.calls == []


@pytest.mark.asyncio
async def test_opted_out_silent_drop_and_restart():
    consent = InMemoryConsentRegistry()
    d = _driver(consent=consent)
    await consent.set("whatsapp:+1", "opted_out", "test")
    assert await d.handle_inbound("whatsapp:+1", "hello?") == ""  # silent drop
    assert await d.handle_inbound("whatsapp:+1", "START") == CONSENT_PROMPT
    assert (await consent.get("whatsapp:+1")).status == "pending"


# --------------------------------------------------------------- outbound
@pytest.mark.asyncio
async def test_outbound_refused_unless_opted_in():
    consent = InMemoryConsentRegistry()
    sender = RecordingSender()
    d = _driver(consent=consent, sender=sender)
    from app.hermes.gateway import HermesResponse

    await d.send("whatsapp:+1", HermesResponse(text="ping"))  # unknown -> refused
    assert sender.sent == []
    await consent.set("whatsapp:+1", "opted_in", "test")
    await d.send("whatsapp:+1", HermesResponse(text="ping"))
    assert len(sender.sent) == 1
    assert sender.sent[0]["to"] == "whatsapp:+1"


@pytest.mark.asyncio
async def test_consent_transitions_are_audited_with_hash():
    events = []

    async def audit(e):
        events.append(e)

    consent = InMemoryConsentRegistry(audit_hook=audit, phone_salt="salt")
    await consent.set("whatsapp:+15551234567", "pending", "first_contact")
    assert events and events[0]["kind"] == "whatsapp.consent_transition"
    assert "+15551234567" not in str(events[0])
    assert events[0]["to"] == "pending"


# --------------------------------------------------------------- HTTP router
def test_router_rejects_bad_signature():
    d = _driver(gateway=HermesGateway(core=RecordingCore()))
    app = FastAPI()
    app.include_router(d.router())
    client = TestClient(app)
    r = client.post(
        "/twilio/whatsapp",
        data={"From": "whatsapp:+1", "Body": "hi", "MessageSid": "SM1"},
        headers={"X-Twilio-Signature": "bogus"},
    )
    assert r.status_code == 403


def test_router_accepts_valid_signature_and_returns_twiml():
    d = _driver(gateway=HermesGateway(core=RecordingCore()), public_base_url="http://testserver")
    app = FastAPI()
    app.include_router(d.router())
    client = TestClient(app)
    url = "http://testserver/twilio/whatsapp"
    params = {"From": "whatsapp:+1888", "Body": "hi", "MessageSid": "SM2"}
    sig = compute_signature(AUTH, url, params)
    r = client.post("/twilio/whatsapp", data=params, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 200
    assert "<Message>" in r.text  # first-contact consent prompt
    assert "xml" in r.headers["content-type"]


def test_router_signature_can_be_disabled_for_tests():
    d = _driver(gateway=HermesGateway(core=RecordingCore()), validate_signatures=False)
    app = FastAPI()
    app.include_router(d.router())
    client = TestClient(app)
    r = client.post("/twilio/whatsapp", data={"From": "whatsapp:+1", "Body": "STOP", "MessageSid": "S"})
    assert r.status_code == 200
    assert "opted out" in r.text.lower()
