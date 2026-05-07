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
