# nobleport

NoblePort / Stephanie.ai LangGraph supervisor stack.

## Overview

This repo runs a production supervisor with strict audit-first governance:

- **Audit-first**: every state-changing agent action writes a row to
  `audit_log` *before* the agent runs. If the audit insert fails, the
  request fails (HTTP 503) and the graph never executes.
- **Postgres truth layer** for `audit_log`, `agent_metrics`, and
  `langgraph_checkpoints`. SQLite is supported for tests and local dev.
- **Async Redis** event bus publishes lifecycle and step events on
  `agent.events`. Disabled when `REDIS_URL` is unset.
- **Tool sandbox**: only allowlisted executables (default: `pytest`,
  `flake8`) may run; everything else raises `SandboxRejected`. No shell
  passthrough.
- **Gate latency metrics**: invoke latency stored per-thread / per-event
  in `agent_metrics`; `/metrics/gates` returns p50/p95/count.
- **Cooperative kill**: `/agent/kill/{thread_id}` flips a flag that the
  supervisor checks between graph steps. True hard cancellation isn't
  available at the LangGraph version we target — see "Caveats".

## Endpoints

| Method | Path                         | Purpose                                  |
|--------|------------------------------|------------------------------------------|
| GET    | `/health`                    | Liveness + checkpoint-backend identifier |
| GET    | `/ready`                     | Verifies DB and (when configured) Redis  |
| POST   | `/agent/invoke`              | Audit-first invocation                   |
| GET    | `/agent/status/{thread_id}`  | Last-known status / kill flag            |
| POST   | `/agent/kill/{thread_id}`    | Cooperative cancellation request         |
| GET    | `/metrics/gates`             | Latency summary (p50/p95/count)          |

## Configuration

See `.env.example`. Key variables:

- `DATABASE_URL` — `postgresql://...` in production; falls back to
  `sqlite+aiosqlite:///./nobleport.db`.
- `REDIS_URL` — leave unset to disable Redis (NullEventBus).
- `AGENT_EVENT_CHANNEL` — defaults to `agent.events`.
- `SANDBOX_ALLOWED_COMMANDS` — comma-separated executable basenames.

## Local development

```bash
docker compose up --build
```

Brings up `postgres`, `redis`, and `api` with sane defaults. The API
listens on `:8000`.

Without Docker:

```bash
pip install -r requirements.txt
pytest -q
uvicorn app.api.app:app --reload
```

## Hermes channel drivers (Slack / WhatsApp / Web Chat)

**Status: APPROVED ARCHITECTURE BLUEPRINT / TARGET STATE IMPLEMENTATION —
NOT VERIFIED.** The drivers are integrated against the repo's real audit/DB
abstractions but ship with a non-production placeholder Core
(`LocalEchoCore`). Live behaviour is pending NoblePort Core integration and
audit evidence.

`app/hermes/` adds channel ingress that all funnels through a single governed
chokepoint, `HermesGateway`. Drivers never call NoblePort Core / Stephanie
directly — the gateway is the only path, and it enforces:

- **Identity + authority**: senders resolve to a role-scoped
  `OperatorIdentity`. WRITE intents are rejected at the gateway for
  read-only roles (Public / Customer).
- **Audit-first**: every routed message and every rejected write is recorded
  via the existing `audit_log` (`make_db_audit`) before Core is reached.
- **No PII in audit/logs**: phone numbers are stored/logged only as a keyed
  hash (`HERMES_PHONE_HASH_SALT`).

| Channel  | Audience  | Controls |
|----------|-----------|----------|
| Slack    | Operators | Socket Mode; workspace (team) + channel allowlist; event dedup; DM-only default |
| WhatsApp | Customers | Twilio signature verified; consent-gated (STOP/HELP/START); only opted-in numbers routed; outbound refused unless opted-in |
| Web Chat | Public    | Read-only (WRITE stripped at ingress *and* rejected by the gateway); bounded per-session rate limit; idle-expiring sessions |

Backends use the repo's abstractions where available: consent persists to
Postgres/SQLite (`whatsapp_consent` + immutable `whatsapp_consent_events`),
and web rate limiting uses Redis when `REDIS_URL` is set (bounded in-memory
fallback otherwise, marked non-production for multi-replica). Slack/Twilio
SDKs are optional (`requirements.txt`) and imported lazily — install only the
channels you deploy. Inbound Twilio signature validation is stdlib-only.

Configuration: see the Hermes block in `.env.example` (`app/hermes/config.py`
loads it). Behind a TLS-terminating proxy, set `TWILIO_PUBLIC_BASE_URL` (or
`TWILIO_TRUST_FORWARDED=true`) so signatures validate against the public URL.

**Legal note**: consent/keyword handling (STOP/HELP/START, opt-in gating) are
implementation safeguards, not a compliance attestation. TCPA / A2P 10DLC
review gates remain required before customer rollout.

## Caveats

- **LangGraph PostgresSaver**: the supervisor uses
  `langgraph.checkpoint.postgres.PostgresSaver` if it's importable AND
  `DATABASE_URL` points at Postgres; otherwise it uses the project-local
  `PostgresCheckpointer` (same `langgraph_checkpoints` table). The
  active backend is reported by `/health`.
- **Cooperative kill**: cancellation is checked between graph steps. A
  step that runs for minutes won't be interrupted mid-call; keep step
  functions short or wrap long work with `asyncio.wait_for`.
- **Permit / digest scripts** in `scripts/` are unrelated to the
  supervisor and continue to run as before.
