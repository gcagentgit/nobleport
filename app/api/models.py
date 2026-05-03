"""Hardened request/response models for the public API."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class InvokeRequest(BaseModel):
    """`POST /agent/invoke` body."""

    input: str = Field(..., min_length=1, max_length=32_768, description="User input")
    thread_id: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Optional caller-supplied thread id; auto-generated if missing.",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("thread_id")
    @classmethod
    def _validate_thread_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        # Restrict thread ids to a safe character class to avoid surprises in
        # logs / Redis channel names.
        import re

        if not re.fullmatch(r"[A-Za-z0-9_\-:.]+", v):
            raise ValueError("thread_id must match [A-Za-z0-9_\\-:.]+")
        return v


class InvokeResponse(BaseModel):
    thread_id: str
    request_id: str
    outcome: str
    state: dict[str, Any]


class StatusResponse(BaseModel):
    thread_id: str
    status: str
    kill_requested: bool
    last_event: Optional[str]
    updated_at: Optional[str]


class KillResponse(BaseModel):
    thread_id: str
    status: str
    previous_status: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    version: str
    checkpoint_backend: str


class ReadyResponse(BaseModel):
    ready: bool
    db: bool
    redis: Optional[bool]
    detail: dict[str, Any] = Field(default_factory=dict)


class GateMetricsResponse(BaseModel):
    metrics: dict[str, Any]
