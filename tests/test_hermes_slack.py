"""Slack driver: allowlists, DM-only default, dedup, routing, block rendering."""
from __future__ import annotations

import pytest

from app.hermes.drivers import SlackDriver, slack_blocks
from app.hermes.gateway import (
    CoreRequest,
    HermesGateway,
    HermesResponse,
    OperatorIdentity,
    OperatorRegistry,
)


class RecordingCore:
    def __init__(self):
        self.calls: list[CoreRequest] = []

    async def handle(self, request: CoreRequest):
        from app.hermes.gateway import CoreResponse

        self.calls.append(request)
        return CoreResponse(text=f"ok:{request.intent}")


def _gateway(core=None, ops=None):
    return HermesGateway(core=core or RecordingCore(), operators=ops)


async def _started(driver: SlackDriver, gw: HermesGateway) -> SlackDriver:
    # Attach the gateway without importing slack_bolt (start() would).
    driver._gateway = gw
    return driver


@pytest.mark.asyncio
async def test_dm_only_default_ignores_channel_messages():
    core = RecordingCore()
    d = await _started(SlackDriver("xoxb", "xapp"), _gateway(core))
    resp = await d.handle_message_event(
        {"channel_type": "channel", "channel": "C1", "user": "U1", "text": "status", "ts": "1"}
    )
    assert resp is None
    assert core.calls == []


@pytest.mark.asyncio
async def test_dm_is_routed():
    core = RecordingCore()
    d = await _started(SlackDriver("xoxb", "xapp"), _gateway(core))
    resp = await d.handle_message_event(
        {"channel_type": "im", "channel": "D1", "user": "U1", "text": "status", "ts": "1"}
    )
    assert resp is not None
    assert len(core.calls) == 1


@pytest.mark.asyncio
async def test_channel_allowlist_enforced():
    core = RecordingCore()
    d = await _started(SlackDriver("xoxb", "xapp", allowed_channel_ids={"C_OK"}), _gateway(core))
    blocked = await d.handle_message_event(
        {"channel_type": "channel", "channel": "C_NO", "user": "U1", "text": "status", "ts": "1"}
    )
    assert blocked is None
    allowed = await d.handle_message_event(
        {"channel_type": "channel", "channel": "C_OK", "user": "U1", "text": "status", "ts": "2"}
    )
    assert allowed is not None
    assert len(core.calls) == 1


@pytest.mark.asyncio
async def test_team_allowlist_enforced():
    core = RecordingCore()
    d = await _started(
        SlackDriver("xoxb", "xapp", allowed_team_ids={"T_OK"}), _gateway(core)
    )
    blocked = await d.handle_message_event(
        {"channel_type": "im", "channel": "D1", "team": "T_BAD", "user": "U1", "text": "status", "ts": "1"}
    )
    assert blocked is None
    ok = await d.handle_message_event(
        {"channel_type": "im", "channel": "D1", "team": "T_OK", "user": "U1", "text": "status", "ts": "2"}
    )
    assert ok is not None


@pytest.mark.asyncio
async def test_bot_and_subtype_events_ignored():
    d = await _started(SlackDriver("xoxb", "xapp"), _gateway())
    assert await d.handle_message_event({"channel_type": "im", "bot_id": "B1", "text": "x"}) is None
    assert (
        await d.handle_message_event(
            {"channel_type": "im", "subtype": "message_changed", "text": "x"}
        )
        is None
    )


@pytest.mark.asyncio
async def test_deduplication_by_client_msg_id():
    core = RecordingCore()
    d = await _started(SlackDriver("xoxb", "xapp"), _gateway(core))
    ev = {
        "channel_type": "im",
        "channel": "D1",
        "user": "U1",
        "text": "status",
        "ts": "1",
        "client_msg_id": "abc-123",
    }
    first = await d.handle_message_event(dict(ev))
    second = await d.handle_message_event(dict(ev))  # Slack retry
    assert first is not None
    assert second is None
    assert len(core.calls) == 1


@pytest.mark.asyncio
async def test_operator_binding_grants_write():
    core = RecordingCore()
    ops = OperatorRegistry()
    ops.bind("slack", "U_OWNER", OperatorIdentity("owner", role="Owner"))
    d = await _started(SlackDriver("xoxb", "xapp"), _gateway(core, ops))
    resp = await d.handle_message_event(
        {"channel_type": "im", "channel": "D1", "user": "U_OWNER", "text": "approve 9", "ts": "1"}
    )
    assert resp is not None and resp.success is True
    assert core.calls[0].kind.value == "write"


def test_slack_blocks_render_and_escape():
    resp = HermesResponse(
        text="Pending <b> & stuff",
        ui_hints={"action_buttons": [{"label": "OK", "intent": "approve", "args": {"request_id": "7"}}]},
    )
    blocks = slack_blocks(resp)
    assert blocks[0]["text"]["text"] == "Pending &lt;b&gt; &amp; stuff"
    assert blocks[1]["elements"][0]["value"] == "approve|7"
    assert blocks[1]["elements"][0]["style"] == "primary"


def test_slack_blocks_empty_without_buttons():
    assert slack_blocks(HermesResponse(text="hi")) == []
