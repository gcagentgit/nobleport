"""Schema definitions for sqlite and postgres dialects.

Audit-first governance: every state-changing agent action must produce a row
in `audit_log` *before* execution. `agent_metrics` records timing and outcome
for the gate-latency endpoint. `langgraph_checkpoints` is the persistent
checkpoint store used by the supervisor when the official PostgresSaver isn't
available.
"""
from __future__ import annotations


def sqlite_schema() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            thread_id TEXT,
            message TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_audit_log_thread ON audit_log(thread_id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at)",
        """
        CREATE TABLE IF NOT EXISTS agent_metrics (
            id TEXT PRIMARY KEY,
            thread_id TEXT,
            event_id TEXT,
            metric_name TEXT NOT NULL,
            metric_ms REAL NOT NULL,
            outcome TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_agent_metrics_thread ON agent_metrics(thread_id)",
        "CREATE INDEX IF NOT EXISTS idx_agent_metrics_name ON agent_metrics(metric_name)",
        """
        CREATE TABLE IF NOT EXISTS langgraph_checkpoints (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL,
            checkpoint_id TEXT NOT NULL,
            parent_id TEXT,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE (thread_id, checkpoint_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_lgc_thread ON langgraph_checkpoints(thread_id)",
        """
        CREATE TABLE IF NOT EXISTS agent_thread_state (
            thread_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            kill_requested INTEGER NOT NULL DEFAULT 0,
            last_event TEXT,
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS voice_sessions (
            session_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            avatar_state TEXT NOT NULL DEFAULT 'idle',
            fallback_text INTEGER NOT NULL DEFAULT 0,
            ended INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_voice_sessions_thread ON voice_sessions(thread_id)",
        """
        CREATE TABLE IF NOT EXISTS voice_transcripts (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT NOT NULL,
            request_id TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_voice_transcripts_session ON voice_transcripts(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_voice_transcripts_thread ON voice_transcripts(thread_id)",
        """
        CREATE TABLE IF NOT EXISTS audit_chain (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            audit_id TEXT NOT NULL UNIQUE,
            prev_hash TEXT NOT NULL,
            entry_hash TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_audit_chain_audit ON audit_chain(audit_id)",
        """
        CREATE TABLE IF NOT EXISTS launch_kill_switch (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            engaged INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
    ]


def postgres_schema() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            payload JSONB NOT NULL,
            thread_id TEXT,
            message TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_audit_log_thread ON audit_log(thread_id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at)",
        """
        CREATE TABLE IF NOT EXISTS agent_metrics (
            id TEXT PRIMARY KEY,
            thread_id TEXT,
            event_id TEXT,
            metric_name TEXT NOT NULL,
            metric_ms DOUBLE PRECISION NOT NULL,
            outcome TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_agent_metrics_thread ON agent_metrics(thread_id)",
        "CREATE INDEX IF NOT EXISTS idx_agent_metrics_name ON agent_metrics(metric_name)",
        """
        CREATE TABLE IF NOT EXISTS langgraph_checkpoints (
            seq BIGSERIAL PRIMARY KEY,
            thread_id TEXT NOT NULL,
            checkpoint_id TEXT NOT NULL,
            parent_id TEXT,
            state JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (thread_id, checkpoint_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_lgc_thread ON langgraph_checkpoints(thread_id)",
        """
        CREATE TABLE IF NOT EXISTS agent_thread_state (
            thread_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            kill_requested BOOLEAN NOT NULL DEFAULT FALSE,
            last_event TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS voice_sessions (
            session_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            avatar_state TEXT NOT NULL DEFAULT 'idle',
            fallback_text BOOLEAN NOT NULL DEFAULT FALSE,
            ended BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_voice_sessions_thread ON voice_sessions(thread_id)",
        """
        CREATE TABLE IF NOT EXISTS voice_transcripts (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT NOT NULL,
            request_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_voice_transcripts_session ON voice_transcripts(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_voice_transcripts_thread ON voice_transcripts(thread_id)",
        """
        CREATE TABLE IF NOT EXISTS audit_chain (
            seq BIGSERIAL PRIMARY KEY,
            audit_id TEXT NOT NULL UNIQUE,
            prev_hash TEXT NOT NULL,
            entry_hash TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_audit_chain_audit ON audit_chain(audit_id)",
        """
        CREATE TABLE IF NOT EXISTS launch_kill_switch (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            engaged BOOLEAN NOT NULL DEFAULT FALSE,
            reason TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
    ]
