"""Gateway governance: intent classification, write rejection, audit, no bypass."""
from __future__ import annotations

import pytest

from app.hermes.gateway import (
    CoreRequest,
    HermesGateway,
    HermesMessage,
    IntentKind,
    LocalEchoCore,
    OperatorIdentity,
    OperatorRegistry,
    parse_intent,
)


class RecordingCore:
    def __init__(self):
        self.calls: list[CoreRequest] = []

    async def handle(self, request: CoreRequest):
        self.calls.append(request)
        from app.hermes.gateway import CoreResponse

        return CoreResponse(text=f"ok:{request.intent}")


class RecordingAudit:
    def __init__(self):
        self.events: list[dict] = []

    async def __call__(self, event: dict) -> None:
        self.events.append(event)


def test_parse_intent_classification():
    assert parse_intent("status")[1] is IntentKind.READ
    assert parse_intent("/np-status now")[0] == "np-status"
    assert parse_intent("approve 42")[1] is IntentKind.WRITE
    assert parse_intent("frobnicate")[1] is IntentKind.UNKNOWN
    assert parse_intent("")[1] is IntentKind.UNKNOWN


@pytest.mark.asyncio
async def test_public_write_rejected_and_audited_not_dispatched():
    core = RecordingCore()
    audit = RecordingAudit()
    gw = HermesGateway(core=core, audit_hook=audit)

    resp = await gw.receive(
        HermesMessage(channel="web", channel_user_id="sess-1", raw_text="approve 5")
    )
    assert resp.success is False
    assert core.calls == []  # never reached Core
    kinds = [e["kind"] for e in audit.events]
    assert "hermes.write_rejected" in kinds
    assert "hermes.route" not in kinds


@pytest.mark.asyncio
async def test_operator_write_allowed_and_dispatched():
    core = RecordingCore()
    audit = RecordingAudit()
    ops = OperatorRegistry()
    ops.bind("slack", "U1", OperatorIdentity("michael", role="Owner", authority_key_id="k1"))
    gw = HermesGateway(core=core, operators=ops, audit_hook=audit)

    resp = await gw.receive(
        HermesMessage(channel="slack", channel_user_id="U1", raw_text="approve 5")
    )
    assert resp.success is True
    assert len(core.calls) == 1
    assert core.calls[0].intent == "approve"
    assert [e["kind"] for e in audit.events] == ["hermes.route"]


@pytest.mark.asyncio
async def test_read_intent_routes_for_public():
    core = RecordingCore()
    gw = HermesGateway(core=core)
    resp = await gw.receive(
        HermesMessage(channel="web", channel_user_id="s1", raw_text="status")
    )
    assert resp.success is True
    assert core.calls[0].kind is IntentKind.READ


@pytest.mark.asyncio
async def test_whatsapp_audit_uses_hash_not_raw_phone():
    core = RecordingCore()
    audit = RecordingAudit()
    gw = HermesGateway(core=core, audit_hook=audit, phone_salt="s")
    raw = "whatsapp:+15551230000"
    await gw.receive(HermesMessage(channel="whatsapp", channel_user_id=raw, raw_text="status"))
    for e in audit.events:
        assert raw not in str(e)
        assert "user_hash" in e
        assert "user" not in e


@pytest.mark.asyncio
async def test_core_error_is_contained():
    class Boom:
        async def handle(self, request):
            raise RuntimeError("kaboom")

    audit = RecordingAudit()
    gw = HermesGateway(core=Boom(), audit_hook=audit)
    resp = await gw.receive(HermesMessage(channel="web", channel_user_id="s", raw_text="status"))
    assert resp.success is False
    assert "kaboom" not in resp.text
    assert any(e["kind"] == "hermes.core_error" for e in audit.events)


@pytest.mark.asyncio
async def test_local_echo_core_is_read_only_safe():
    gw = HermesGateway(core=LocalEchoCore())
    resp = await gw.receive(HermesMessage(channel="web", channel_user_id="s", raw_text="help"))
    assert resp.success is True
    assert "non-production" in resp.text.lower()
