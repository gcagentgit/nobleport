"""Tool sandbox: only allowlisted commands run; everything else is rejected."""
from __future__ import annotations

import pytest

from app.tools.sandbox import (
    SandboxRejected,
    is_allowed,
    parse_command,
    run_command,
)


def test_is_allowed_basic():
    assert is_allowed(["pytest", "-q"], allowed=["pytest", "flake8"])
    assert is_allowed(["/usr/bin/pytest", "-q"], allowed=["pytest"])
    assert not is_allowed(["rm", "-rf", "/"], allowed=["pytest"])
    assert not is_allowed([], allowed=["pytest"])


def test_parse_command_no_shell():
    assert parse_command("pytest -q tests") == ["pytest", "-q", "tests"]
    # A shell metacharacter in a string command is preserved as a token, NOT
    # interpreted; this is what makes the sandbox safe.
    tokens = parse_command("pytest -q ; rm -rf /")
    assert ";" in tokens
    assert tokens[0] == "pytest"


async def test_run_allowed_command_succeeds(monkeypatch):
    monkeypatch.setenv("SANDBOX_ALLOWED_COMMANDS", "echo")
    import app.config as cfg
    cfg.reset_settings()

    res = await run_command(["echo", "hello"])
    assert res.returncode == 0
    assert "hello" in res.stdout


async def test_run_disallowed_command_rejected(monkeypatch):
    monkeypatch.setenv("SANDBOX_ALLOWED_COMMANDS", "pytest,flake8")
    import app.config as cfg
    cfg.reset_settings()

    with pytest.raises(SandboxRejected) as exc:
        await run_command(["sh", "-c", "echo pwned"])
    assert "allowlist" in str(exc.value)


async def test_run_string_command_rejects_disallowed(monkeypatch):
    monkeypatch.setenv("SANDBOX_ALLOWED_COMMANDS", "pytest")
    import app.config as cfg
    cfg.reset_settings()

    # Even a string with shell metacharacters is tokenised — argv[0] is what
    # matters.
    with pytest.raises(SandboxRejected):
        await run_command("rm -rf /; pytest")


async def test_explicit_allowed_overrides_env(monkeypatch):
    monkeypatch.setenv("SANDBOX_ALLOWED_COMMANDS", "pytest")
    import app.config as cfg
    cfg.reset_settings()

    # caller-supplied allowed list grants echo
    res = await run_command(["echo", "ok"], allowed=["echo"])
    assert res.returncode == 0
