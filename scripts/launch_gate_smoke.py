#!/usr/bin/env python3
"""Stephanie.ai Live Voice Launch Gate v1 — smoke test.

Hits /health, /ready, and /metrics/gates and validates the gate-status
result shape exactly. Exits non-zero unless every required gate is PASS
and dependency endpoints return ok. DEGRADED or FAIL counts as launch-not-
ready, by spec.

Usage:
  python scripts/launch_gate_smoke.py [--base-url URL]

The default base URL is api.nobleport.net (per the launch spec). Override
with --base-url for staging, local Docker compose, or a tunnel.

Exit codes:
  0  — all gates PASS, /health and /ready ok
  1  — at least one gate is DEGRADED or FAIL, OR an endpoint is unreachable
  2  — invalid response shape (treat as launch-blocking)
"""
from __future__ import annotations

import argparse
import json
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest


REQUIRED_KEYS = ("voice", "avatar", "websocket", "audit_log", "database", "kill_switch")
ALLOWED_STATUSES = ("PASS", "DEGRADED", "FAIL")


def _get(url: str, timeout: float = 10.0) -> tuple[int, Any, str | None]:
    """Return (status_code, parsed_json_or_none, error_message_or_none)."""
    try:
        with urlrequest.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - explicit URL
            raw = resp.read().decode("utf-8")
            try:
                body = json.loads(raw)
            except json.JSONDecodeError as e:
                return resp.status, None, f"invalid JSON: {e}: {raw[:200]!r}"
            return resp.status, body, None
    except urlerror.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            body = None
        return e.code, body, f"HTTP {e.code} {e.reason}"
    except urlerror.URLError as e:
        # DNS failures, connection refused, etc.
        reason = getattr(e, "reason", e)
        return 0, None, f"unreachable: {reason}"
    except Exception as e:  # noqa: BLE001
        return 0, None, f"unexpected error: {e}"


def validate_shape(payload: dict[str, Any]) -> list[str]:
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
            problems.append(f"{k}: status {v!r} not in {ALLOWED_STATUSES}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stephanie.ai launch-gate smoke test")
    parser.add_argument(
        "--base-url",
        default="https://api.nobleport.net",
        help="API base URL (default: https://api.nobleport.net)",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help="Treat DEGRADED gates as launch-acceptable (default: not allowed).",
    )
    args = parser.parse_args(argv)

    base = args.base_url.rstrip("/")
    print(f"[smoke] target: {base}")

    # 1) /health — required to be ok.
    code, body, err = _get(f"{base}/health", timeout=args.timeout)
    print(f"[smoke] GET /health -> {code} {body if body else err}")
    health_ok = code == 200 and isinstance(body, dict) and body.get("status") == "ok"

    # 2) /ready — must be ready=true.
    code, body, err = _get(f"{base}/ready", timeout=args.timeout)
    print(f"[smoke] GET /ready  -> {code} {body if body else err}")
    ready_ok = code == 200 and isinstance(body, dict) and body.get("ready") is True

    # 3) /metrics/gates — required result shape AND all PASS.
    code, body, err = _get(f"{base}/metrics/gates", timeout=args.timeout)
    print(f"[smoke] GET /metrics/gates -> {code} {body if body else err}")
    if code != 200 or not isinstance(body, dict):
        print(f"[smoke] FAIL: /metrics/gates not reachable or returned non-JSON: {err}")
        return 1

    problems = validate_shape(body)
    if problems:
        print("[smoke] FAIL: /metrics/gates payload does not match required shape:")
        for p in problems:
            print(f"  - {p}")
        return 2

    statuses = {k: body[k] for k in REQUIRED_KEYS}
    fails = [k for k, v in statuses.items() if v == "FAIL"]
    degraded = [k for k, v in statuses.items() if v == "DEGRADED"]

    print("[smoke] gate statuses:")
    for k in REQUIRED_KEYS:
        print(f"  {k}: {body[k]}")

    overall_ok = (
        health_ok
        and ready_ok
        and not fails
        and (args.allow_degraded or not degraded)
    )

    if fails:
        print(f"[smoke] FAIL: gates failed: {', '.join(fails)}")
    if degraded:
        msg = "DEGRADED" if not args.allow_degraded else "DEGRADED (allowed)"
        print(f"[smoke] {msg}: {', '.join(degraded)}")
    if not health_ok:
        print("[smoke] FAIL: /health not ok")
    if not ready_ok:
        print("[smoke] FAIL: /ready not true")

    if overall_ok:
        print("[smoke] LAUNCH READY")
        return 0
    print("[smoke] NOT LAUNCH READY")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
