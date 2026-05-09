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


# ----------------------------------------------------------- voice launch gate

class StartSessionRequest(BaseModel):
    thread_id: Optional[str] = Field(
        default=None, max_length=128, description="Optional caller-supplied thread id"
    )
    mode: str = Field(default="voice", pattern=r"^(voice|text)$")
    fallback_text: bool = Field(default=False)

    @field_validator("thread_id")
    @classmethod
    def _validate_thread_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        import re

        if not re.fullmatch(r"[A-Za-z0-9_\-:.]+", v):
            raise ValueError("thread_id must match [A-Za-z0-9_\\-:.]+")
        return v


class SessionResponse(BaseModel):
    session_id: str
    thread_id: str
    mode: str
    avatar_state: str
    fallback_text: bool
    ended: bool
    created_at: str
    updated_at: str


class MessageRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=128)
    content: str = Field(..., min_length=1, max_length=32_768)
    source: str = Field(default="text", pattern=r"^(stt|text)$")


class MessageResponse(BaseModel):
    session_id: str
    thread_id: str
    request_id: str
    user_transcript_id: str
    assistant_transcript_id: str
    domain: str
    reply: str
    next_action: str
    requires_human_approval: bool
    disclaimers: list[str]
    avatar_state: str


class AvatarStateRequest(BaseModel):
    state: str = Field(..., pattern=r"^(idle|speaking|listening|error|fallback)$")


class FallbackRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=512)


class KillSwitchRequest(BaseModel):
    engaged: bool
    reason: Optional[str] = Field(default=None, max_length=512)


class KillSwitchState(BaseModel):
    engaged: bool
    reason: Optional[str]
    updated_at: Optional[str]


class GateStatusResponse(BaseModel):
    voice: str
    avatar: str
    websocket: str
    audit_log: str
    database: str
    kill_switch: str
    overall: str
    detail: dict[str, Any]


class AuditChainEntry(BaseModel):
    seq: int
    audit_id: str
    prev_hash: str
    entry_hash: str
    event_type: str
    payload: Any
    created_at: str


class AuditResponse(BaseModel):
    ok: bool
    length: int
    head: Optional[str]
    entries: list[AuditChainEntry]
