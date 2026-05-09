"""Tests for the tamper-evident audit hash chain."""
from __future__ import annotations

import pytest

from app.db.connection import get_database
from app.voice import audit_chain


pytestmark = pytest.mark.asyncio


async def test_genesis_chain_verifies_empty():
    db = await get_database()
    result = await audit_chain.verify_chain(db)
    assert result["ok"] is True
    assert result["length"] == 0
    assert result["head"] == audit_chain.GENESIS_HASH


async def test_append_links_to_previous_head():
    db = await get_database()
    a = await audit_chain.append_audit(
        db, event_type="t.one", payload={"k": 1}
    )
    b = await audit_chain.append_audit(
        db, event_type="t.two", payload={"k": 2}
    )
    assert a["prev_hash"] == audit_chain.GENESIS_HASH
    assert b["prev_hash"] == a["entry_hash"]
    assert b["entry_hash"] != a["entry_hash"]
    result = await audit_chain.verify_chain(db)
    assert result["ok"] is True
    assert result["length"] == 2
    assert result["head"] == b["entry_hash"]


async def test_verify_detects_payload_tampering():
    db = await get_database()
    await audit_chain.append_audit(db, event_type="t.one", payload={"k": 1})
    await audit_chain.append_audit(db, event_type="t.two", payload={"k": 2})
    # Tamper with the first entry's payload directly.
    await db.execute(
        "UPDATE audit_chain SET payload = ? WHERE seq = 1", ('{"k":99}',)
    )
    result = await audit_chain.verify_chain(db)
    assert result["ok"] is False
    assert result["reason"] == "entry_hash_mismatch"
    assert result["at_seq"] == 1


async def test_verify_detects_link_tampering():
    db = await get_database()
    await audit_chain.append_audit(db, event_type="t.one", payload={"k": 1})
    await audit_chain.append_audit(db, event_type="t.two", payload={"k": 2})
    # Replace the second entry's prev_hash with garbage.
    await db.execute(
        "UPDATE audit_chain SET prev_hash = ? WHERE seq = 2",
        ("0" * 64,),
    )
    result = await audit_chain.verify_chain(db)
    assert result["ok"] is False
    assert result["reason"] in ("prev_hash_mismatch", "entry_hash_mismatch")


async def test_compute_entry_hash_is_deterministic():
    h1 = audit_chain.compute_entry_hash(
        prev_hash="a" * 64,
        audit_id="aid",
        event_type="t",
        payload_json='{"k":1}',
        created_at="2026-05-09T00:00:00.000000Z",
    )
    h2 = audit_chain.compute_entry_hash(
        prev_hash="a" * 64,
        audit_id="aid",
        event_type="t",
        payload_json='{"k":1}',
        created_at="2026-05-09T00:00:00.000000Z",
    )
    assert h1 == h2
    h3 = audit_chain.compute_entry_hash(
        prev_hash="b" * 64,
        audit_id="aid",
        event_type="t",
        payload_json='{"k":1}',
        created_at="2026-05-09T00:00:00.000000Z",
    )
    assert h1 != h3
