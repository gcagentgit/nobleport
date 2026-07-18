"""WhatsApp consent state and audited transitions.

Consent is the load-bearing control for the customer-facing WhatsApp channel:
no number reaches the gateway (and therefore Core) unless it is ``opted_in``,
and no outbound message is sent unless the number is ``opted_in``. Every state
transition is audited via NoblePort Core's audit layer using a *hashed* phone
reference — raw phone numbers never enter logs or audit payloads.

Two implementations are provided:

  * :class:`DatabaseConsentRegistry` — production path. Persists current state
    plus an immutable transition event log using the repo's ``Database``
    abstraction (Postgres in prod, SQLite in tests).
  * :class:`InMemoryConsentRegistry` — NON-PRODUCTION reference for local runs
    / unit tests. State is lost on restart and not shared across processes.

These are implementation safeguards, not a legal-compliance attestation.
Opt-in/keyword handling still requires legal review before customer rollout.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from app.hermes.gateway import hash_identifier

log = logging.getLogger("hermes.consent")

CONSENT_STATES = ("unknown", "pending", "opted_in", "opted_out")

AuditHook = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class ConsentRecord:
    phone: str
    status: str = "unknown"  # unknown | pending | opted_in | opted_out
    updated_at: float = field(default_factory=time.time)
    source: str = ""


class ConsentRegistry:
    """Common transition-audit behaviour shared by both backends."""

    def __init__(self, audit_hook: Optional[AuditHook] = None, *, phone_salt: str = "") -> None:
        self._audit = audit_hook
        self._phone_salt = phone_salt

    async def get(self, phone: str) -> ConsentRecord:  # pragma: no cover - overridden
        raise NotImplementedError

    async def _persist(self, record: ConsentRecord) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError

    async def set(self, phone: str, status: str, source: str) -> ConsentRecord:
        if status not in CONSENT_STATES:
            raise ValueError(f"invalid consent status: {status!r}")
        prev = await self.get(phone)
        record = ConsentRecord(
            phone=phone, status=status, source=source, updated_at=time.time()
        )
        await self._persist(record)
        await self._audit_transition(prev.status, record, source)
        return record

    async def _audit_transition(self, prev_status: str, record: ConsentRecord, source: str) -> None:
        if not self._audit:
            return
        try:
            await self._audit(
                {
                    "kind": "whatsapp.consent_transition",
                    "phone_hash": hash_identifier(record.phone, self._phone_salt),
                    "from": prev_status,
                    "to": record.status,
                    "source": source,
                    "at": record.updated_at,
                }
            )
        except Exception as e:  # noqa: BLE001 - audit is best-effort, never blocks
            log.warning("consent audit failed (non-fatal): %s", e)


class InMemoryConsentRegistry(ConsentRegistry):
    """NON-PRODUCTION in-memory consent store.

    Bounded by number of distinct phone numbers seen. Not shared across
    processes and not durable — use :class:`DatabaseConsentRegistry` in
    production.
    """

    def __init__(self, audit_hook: Optional[AuditHook] = None, *, phone_salt: str = "") -> None:
        super().__init__(audit_hook, phone_salt=phone_salt)
        self._records: dict[str, ConsentRecord] = {}

    async def get(self, phone: str) -> ConsentRecord:
        return self._records.get(phone) or ConsentRecord(phone=phone)

    async def _persist(self, record: ConsentRecord) -> Optional[str]:
        self._records[record.phone] = record
        return None


class DatabaseConsentRegistry(ConsentRegistry):
    """Postgres/SQLite-backed consent store with an immutable event log.

    Current state lives in ``whatsapp_consent`` (keyed by hashed phone). Every
    transition also appends to ``whatsapp_consent_events`` so the consent
    history is reconstructable for review. Raw phone numbers are never stored
    — only the keyed hash — so the tables carry no PII.
    """

    def __init__(
        self,
        db: Any,
        audit_hook: Optional[AuditHook] = None,
        *,
        phone_salt: str = "",
    ) -> None:
        super().__init__(audit_hook, phone_salt=phone_salt)
        self._db = db

    def _key(self, phone: str) -> str:
        return hash_identifier(phone, self._phone_salt)

    async def get(self, phone: str) -> ConsentRecord:
        row = await self._db.fetch_one(
            "SELECT status, source, updated_at FROM whatsapp_consent WHERE phone_hash = ?",
            (self._key(phone),),
        )
        if not row:
            return ConsentRecord(phone=phone)
        return ConsentRecord(
            phone=phone,
            status=row["status"],
            source=row.get("source") or "",
            updated_at=float(row["updated_at"]) if row.get("updated_at") is not None else time.time(),
        )

    async def _persist(self, record: ConsentRecord) -> Optional[str]:
        import uuid

        key = self._key(record.phone)
        existing = await self._db.fetch_one(
            "SELECT phone_hash FROM whatsapp_consent WHERE phone_hash = ?", (key,)
        )
        if existing is None:
            await self._db.execute(
                "INSERT INTO whatsapp_consent (phone_hash, status, source, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (key, record.status, record.source, record.updated_at),
            )
        else:
            await self._db.execute(
                "UPDATE whatsapp_consent SET status = ?, source = ?, updated_at = ? "
                "WHERE phone_hash = ?",
                (record.status, record.source, record.updated_at, key),
            )
        event_id = str(uuid.uuid4())
        await self._db.execute(
            "INSERT INTO whatsapp_consent_events (id, phone_hash, status, source, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_id, key, record.status, record.source, record.updated_at),
        )
        return event_id
