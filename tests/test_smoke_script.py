"""Tests for scripts/launch_gate_smoke.py shape validation.

The actual HTTP calls require a live deployment; we exercise the same
shape-validation helper used by the script so drift between server and
client is caught.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SMOKE_PATH = ROOT / "scripts" / "launch_gate_smoke.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location("launch_gate_smoke", SMOKE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["launch_gate_smoke"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_smoke_validate_shape_accepts_valid_payload():
    mod = _load_smoke_module()
    valid = {
        "voice": "PASS",
        "avatar": "PASS",
        "websocket": "PASS",
        "audit_log": "PASS",
        "database": "PASS",
        "kill_switch": "PASS",
    }
    assert mod.validate_shape(valid) == []


def test_smoke_validate_shape_rejects_missing_keys():
    mod = _load_smoke_module()
    problems = mod.validate_shape({"voice": "PASS"})
    assert problems
    assert any("missing required key" in p for p in problems)


def test_smoke_validate_shape_rejects_wrong_status():
    mod = _load_smoke_module()
    payload = {
        "voice": "OK",  # invalid — must be PASS/DEGRADED/FAIL
        "avatar": "PASS",
        "websocket": "PASS",
        "audit_log": "PASS",
        "database": "PASS",
        "kill_switch": "PASS",
    }
    problems = mod.validate_shape(payload)
    assert any("not in" in p for p in problems)


def test_smoke_validate_shape_rejects_non_string_status():
    mod = _load_smoke_module()
    payload = {
        "voice": True,
        "avatar": "PASS",
        "websocket": "PASS",
        "audit_log": "PASS",
        "database": "PASS",
        "kill_switch": "PASS",
    }
    problems = mod.validate_shape(payload)
    assert any("expected string" in p for p in problems)
