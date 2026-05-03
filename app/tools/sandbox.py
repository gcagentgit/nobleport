"""Allowlisted command execution.

The supervisor must NEVER pass arbitrary strings through `shell=True` or pull
binaries by string concatenation. Commands arrive as a tokenised argv list
with the executable as `argv[0]`. Only executables whose basename matches
the configured allowlist may run; everything else raises `SandboxRejected`.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import shutil
from dataclasses import dataclass
from typing import Optional, Sequence

from app.config import get_settings


class SandboxRejected(PermissionError):
    """Raised when a command is not on the allowlist."""


@dataclass
class SandboxResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool


def _basename(token: str) -> str:
    return os.path.basename(token)


def is_allowed(argv: Sequence[str], allowed: Optional[Sequence[str]] = None) -> bool:
    if not argv:
        return False
    settings = get_settings()
    allowed = list(allowed) if allowed is not None else list(settings.allowed_commands)
    return _basename(argv[0]) in {a.strip() for a in allowed if a.strip()}


def parse_command(command: str | Sequence[str]) -> list[str]:
    """Parse a command into a safe argv list. Strings are tokenised with
    `shlex.split(posix=True)`. We never invoke a shell; the result is passed
    directly to `asyncio.create_subprocess_exec`.
    """
    if isinstance(command, str):
        return shlex.split(command, posix=True)
    return list(command)


async def run_command(
    command: str | Sequence[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    timeout_seconds: Optional[int] = None,
    allowed: Optional[Sequence[str]] = None,
) -> SandboxResult:
    """Run a command from the allowlist.

    Raises `SandboxRejected` if the command is not allowed. Never invokes a
    shell. Captures stdout/stderr. Enforces a wall-clock timeout.
    """
    settings = get_settings()
    argv = parse_command(command)
    if not is_allowed(argv, allowed=allowed):
        raise SandboxRejected(
            f"command not on allowlist: {argv[0] if argv else ''!r}"
        )

    # Resolve the executable path explicitly to avoid relying on PATH search
    # behaviour deep in the subprocess machinery.
    resolved = shutil.which(argv[0]) or argv[0]
    real_argv = [resolved, *argv[1:]]

    timeout = timeout_seconds if timeout_seconds is not None else settings.sandbox_timeout_seconds

    loop = asyncio.get_event_loop()
    started = loop.time()
    proc = await asyncio.create_subprocess_exec(
        *real_argv,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    timed_out = False
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        try:
            stdout_b, stderr_b = await proc.communicate()
        except Exception:
            stdout_b, stderr_b = b"", b""

    duration_ms = (loop.time() - started) * 1000.0
    return SandboxResult(
        command=real_argv,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
        duration_ms=duration_ms,
        timed_out=timed_out,
    )
