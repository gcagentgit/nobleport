"""Launch gate evaluator.

Each gate returns one of: PASS | DEGRADED | FAIL with a concrete reason.
PASS is reserved for capabilities that are actually configured and
verifiable from this process. DEGRADED is for "configured but using a
fallback that is not production-ready" (e.g. SQLite or NullEventBus).
FAIL is for missing/broken integrations.

The launch rule from product: do NOT report PASS unless the underlying
capability is actually available. Tests assert exactly this — see
tests/test_launch_gates.py.
"""
from __future__ import annotations

import os
from typing import Any

from app.config import get_settings
from app.db.connection import Database
from app.redis_bus.events import get_event_bus, RedisEventBus
from app.voice import audit_chain, kill_switch


GateStatus = str  # "PASS" | "DEGRADED" | "FAIL"


def _gate(status: GateStatus, reason: str, **extra: Any) -> dict[str, Any]:
    out = {"status": status, "reason": reason}
    out.update(extra)
    return out


# ---------------------------------------------------------------- voice gate
def evaluate_voice() -> dict[str, Any]:
    """Voice gate requires real STT and TTS provider configuration.

    PASS only when STT_PROVIDER and TTS_PROVIDER env vars are set AND the
    provider's API key env var is also set. We do NOT call the provider
    here — that's the smoke script's job — but a missing key guarantees
    FAIL because the capability cannot work in production.
    """
    stt_provider = os.environ.get("STT_PROVIDER", "").strip().lower()
    tts_provider = os.environ.get("TTS_PROVIDER", "").strip().lower()
    if not stt_provider or not tts_provider:
        return _gate(
            "FAIL",
            "STT_PROVIDER and TTS_PROVIDER must be set; user-speaks-and-Stephanie-answers is not configured",
            stt_provider=stt_provider or None,
            tts_provider=tts_provider or None,
        )

    # Validate that the provider's API key env var is present. We accept any
    # non-empty value here; the smoke script verifies the key actually works.
    stt_key_env = f"{stt_provider.upper()}_API_KEY"
    tts_key_env = f"{tts_provider.upper()}_API_KEY"
    missing = [k for k in (stt_key_env, tts_key_env) if not os.environ.get(k)]
    if missing:
        return _gate(
            "FAIL",
            f"missing API key env vars: {', '.join(missing)}",
            stt_provider=stt_provider,
            tts_provider=tts_provider,
            missing=missing,
        )
    return _gate(
        "PASS",
        "STT and TTS providers configured with API keys",
        stt_provider=stt_provider,
        tts_provider=tts_provider,
    )


# --------------------------------------------------------------- avatar gate
def evaluate_avatar() -> dict[str, Any]:
    """Avatar gate requires a configured asset URL and provider.

    DEGRADED is allowed when the asset URL is set but the provider isn't,
    because the avatar can render an idle state without lipsync — the spec
    requires the page to load a video/avatar fast and not freeze. FAIL when
    no asset is configured at all.
    """
    asset_url = os.environ.get("AVATAR_ASSET_URL", "").strip()
    provider = os.environ.get("AVATAR_PROVIDER", "").strip().lower()
    if not asset_url:
        return _gate(
            "FAIL",
            "AVATAR_ASSET_URL is not set; the launch UI has no avatar to load",
        )
    if not provider:
        return _gate(
            "DEGRADED",
            "AVATAR_ASSET_URL set but AVATAR_PROVIDER missing; lipsync unavailable, idle-only fallback",
            asset_url=asset_url,
        )
    return _gate(
        "PASS",
        "avatar asset and provider configured",
        provider=provider,
        asset_url=asset_url,
    )


# ------------------------------------------------------------ websocket gate
def evaluate_websocket() -> dict[str, Any]:
    """WebSocket gate requires the public WS URL.

    The application exposes /voice/ws on this process; production needs
    PUBLIC_WS_URL set so the client can reach it through the gateway. The
    real liveness of that URL is verified by the smoke script.
    """
    public_ws = os.environ.get("PUBLIC_WS_URL", "").strip()
    if not public_ws:
        return _gate(
            "FAIL",
            "PUBLIC_WS_URL is not set; clients have no stable websocket endpoint",
        )
    if not (public_ws.startswith("ws://") or public_ws.startswith("wss://")):
        return _gate(
            "FAIL",
            "PUBLIC_WS_URL must start with ws:// or wss://",
            public_ws=public_ws,
        )
    if public_ws.startswith("ws://") and "localhost" not in public_ws and "127.0.0.1" not in public_ws:
        return _gate(
            "DEGRADED",
            "non-localhost WebSocket using ws:// (insecure); production should use wss://",
            public_ws=public_ws,
        )
    return _gate("PASS", "PUBLIC_WS_URL configured", public_ws=public_ws)


# ------------------------------------------------------------ audit log gate
async def evaluate_audit_log(db: Database) -> dict[str, Any]:
    """Audit log gate requires a verifiable hash chain.

    A FAIL state is reported if the chain table is unreachable or if any
    entry's stored hash doesn't match its recomputed value.
    """
    try:
        result = await audit_chain.verify_chain(db)
    except Exception as e:  # noqa: BLE001
        return _gate(
            "FAIL",
            f"audit_chain table unreachable: {e}",
        )
    if not result.get("ok"):
        return _gate(
            "FAIL",
            "audit_chain integrity check failed",
            detail=result,
        )
    return _gate(
        "PASS",
        "audit hash chain verified",
        length=result.get("length", 0),
        head=result.get("head"),
    )


# -------------------------------------------------------------- database gate
async def evaluate_database(db: Database) -> dict[str, Any]:
    """Database gate. PASS only on Postgres with a successful query.

    SQLite returns DEGRADED — the supervisor and gates run, but the launch
    spec requires Postgres in production.
    """
    try:
        await db.fetch_one("SELECT 1 AS ok")
    except Exception as e:  # noqa: BLE001
        return _gate("FAIL", f"database query failed: {e}", dialect=db.dialect)

    if db.dialect == "sqlite":
        return _gate(
            "DEGRADED",
            "SQLite fallback in use; production launch requires Postgres (DATABASE_URL=postgresql://...)",
            dialect="sqlite",
        )
    return _gate("PASS", "Postgres reachable", dialect="postgres")


# ---------------------------------------------------------- kill switch gate
async def evaluate_kill_switch(db: Database) -> dict[str, Any]:
    """Kill switch gate. PASS when the kill switch is wired up and NOT engaged.

    If engaged, reports FAIL with the engagement reason — operations should
    treat this as the launch being intentionally held.
    """
    try:
        state = await kill_switch.get_state(db)
    except Exception as e:  # noqa: BLE001
        return _gate("FAIL", f"kill_switch table unreachable: {e}")
    if state["engaged"]:
        return _gate(
            "FAIL",
            f"kill switch engaged: {state.get('reason') or 'no reason recorded'}",
            engaged=True,
            updated_at=state.get("updated_at"),
        )
    return _gate("PASS", "kill switch wired and disengaged", engaged=False)


# ---------------------------------------------------------------- redis gate
def _evaluate_redis_summary() -> str:
    """Side-channel: the database gate is the spec-required field, but we
    also expose redis health under detail.redis. This returns a short
    string so the launch detail block is informative without breaking the
    required result shape.
    """
    settings = get_settings()
    if not settings.enable_redis:
        return "DEGRADED: REDIS_URL unset; events run on NullEventBus"
    bus = get_event_bus()
    if not isinstance(bus, RedisEventBus):
        return "DEGRADED: REDIS_URL set but bus is not RedisEventBus"
    return "PASS: RedisEventBus instantiated (liveness verified by /ready)"


# ----------------------------------------------------------- summary helper
async def evaluate_all(db: Database) -> dict[str, Any]:
    """Compute all six required gates and a detail blob.

    The required keys (voice, avatar, websocket, audit_log, database,
    kill_switch) appear at the top level with bare PASS/DEGRADED/FAIL
    strings so the result matches the spec shape exactly. Reasons live in
    `detail.<gate>` so machine checks against the required shape stay
    clean while humans can still read why.
    """
    voice = evaluate_voice()
    avatar = evaluate_avatar()
    ws = evaluate_websocket()
    audit = await evaluate_audit_log(db)
    database = await evaluate_database(db)
    ks = await evaluate_kill_switch(db)
    redis_summary = _evaluate_redis_summary()

    overall_components = [voice, avatar, ws, audit, database, ks]
    if any(c["status"] == "FAIL" for c in overall_components):
        overall = "FAIL"
    elif any(c["status"] == "DEGRADED" for c in overall_components):
        overall = "DEGRADED"
    else:
        overall = "PASS"

    return {
        "voice": voice["status"],
        "avatar": avatar["status"],
        "websocket": ws["status"],
        "audit_log": audit["status"],
        "database": database["status"],
        "kill_switch": ks["status"],
        "overall": overall,
        "detail": {
            "voice": voice,
            "avatar": avatar,
            "websocket": ws,
            "audit_log": audit,
            "database": database,
            "kill_switch": ks,
            "redis": redis_summary,
        },
    }


REQUIRED_KEYS = ("voice", "avatar", "websocket", "audit_log", "database", "kill_switch")
ALLOWED_STATUSES = ("PASS", "DEGRADED", "FAIL")


def validate_shape(payload: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems with a /metrics/gates payload.

    Empty list = shape is valid. Used by the smoke test script and the
    test suite — the same code rules out drift on both sides.
    """
    problems: list[str] = []
    for k in REQUIRED_KEYS:
        if k not in payload:
            problems.append(f"missing required key: {k}")
            continue
        v = payload[k]
        if not isinstance(v, str):
            problems.append(f"{k}: expected string, got {type(v).__name__}")
            continue
        if v not in ALLOWED_STATUSES:
            problems.append(
                f"{k}: status {v!r} not in {ALLOWED_STATUSES}"
            )
    return problems
