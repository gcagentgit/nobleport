"""Latency / outcome metrics for the supervisor gates.

Metrics are stored in `agent_metrics` keyed by thread_id and event_id where
available. The `gate_latency_summary` helper computes p50, p95, and count for
each metric_name. Percentiles are computed in Python over the full sample for
portability across SQLite and Postgres — for very large datasets a Postgres-
side `percentile_cont` would be preferable; document that as a follow-up.
"""
from __future__ import annotations

import math
import uuid
from typing import Any, Optional

from app.db.connection import Database


async def record_invoke_latency(
    db: Database,
    *,
    thread_id: Optional[str],
    event_id: Optional[str],
    metric_ms: float,
    outcome: str,
    metric_name: str = "agent_invoke_latency_ms",
) -> str:
    """Persist a latency measurement; returns the metric row id."""
    mid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO agent_metrics (id, thread_id, event_id, metric_name, metric_ms, outcome) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (mid, thread_id, event_id, metric_name, float(metric_ms), outcome),
    )
    return mid


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear-interpolation percentile (NIST-style). pct in [0, 100]."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_vals[int(rank)]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (rank - lo)


async def gate_latency_summary(db: Database) -> dict[str, Any]:
    """Return p50/p95/count grouped by metric_name."""
    rows = await db.fetch_all(
        "SELECT metric_name, metric_ms FROM agent_metrics ORDER BY metric_name"
    )
    by_name: dict[str, list[float]] = {}
    for r in rows:
        by_name.setdefault(r["metric_name"], []).append(float(r["metric_ms"]))

    summary: dict[str, Any] = {}
    for name, vals in by_name.items():
        vals.sort()
        summary[name] = {
            "count": len(vals),
            "p50_ms": round(_percentile(vals, 50.0), 3),
            "p95_ms": round(_percentile(vals, 95.0), 3),
            "min_ms": round(vals[0], 3),
            "max_ms": round(vals[-1], 3),
        }
    return summary
