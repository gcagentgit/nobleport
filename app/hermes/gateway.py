"""Hermes Gateway — the single governance chokepoint for all channels.

Every inbound message from every channel (Slack / WhatsApp / Web) is turned
into a :class:`HermesMessage` and handed to :meth:`HermesGateway.receive`.
The gateway, and only the gateway:

  1. Resolves the sender to an :class:`OperatorIdentity` (role-scoped).
  2. Parses and classifies the intent (READ vs WRITE).
  3. Enforces governance: Public / Customer identities may not issue WRITE
     intents. Write intents from read-only identities are rejected here at
     the gateway (in addition to any defense-in-depth stripping the driver
     performs at ingress).
  4. Writes an audit record via NoblePort Core's audit layer *before* the
     request reaches Core business logic (audit-first, mirroring
     ``app.db.audit``).
  5. Dispatches the governed request to a :class:`CoreClient`.

Drivers never touch Core or Stephanie directly — that bypass would defeat
the audit-first and role-scoping guarantees. This module has zero
third-party dependencies so the governance core is importable and testable
without any channel SDK installed.
"""
from __future__ import annotations

import abc
import enum
import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol

log = logging.getLogger("hermes.gateway")


# ---------------------------------------------------------------------------
# Intent parsing / classification
# ---------------------------------------------------------------------------

class IntentKind(enum.Enum):
    READ = "read"
    WRITE = "write"
    UNKNOWN = "unknown"


# Read-only intents anyone (including Public web) may issue.
READ_INTENTS: frozenset[str] = frozenset(
    {
        "help",
        "status",
        "query",
        "scan",
        "payment-status",
        "permits",
        "info",
        "hours",
        "services",
    }
)

# State-changing intents. Only operator-role identities may issue these; the
# gateway rejects them for Public / Customer identities.
WRITE_INTENTS: frozenset[str] = frozenset(
    {
        "approve",
        "reject",
        "kill",
        "cancel",
        "tier",
        "set-tier",
        "create",
        "update",
        "delete",
        "schedule",
        "pay",
        "refund",
        "dispatch",
        "assign",
        "close",
    }
)


def parse_intent(text: str) -> tuple[str, IntentKind, str]:
    """Parse free text into ``(intent, kind, remainder)``.

    The first whitespace-delimited token is treated as the intent verb. The
    classification is conservative: an unrecognised verb is ``UNKNOWN`` (the
    gateway treats UNKNOWN as read-only for Public but still audits it), and
    anything in :data:`WRITE_INTENTS` is ``WRITE``.
    """
    stripped = (text or "").strip()
    if not stripped:
        return "", IntentKind.UNKNOWN, ""
    parts = stripped.split(None, 1)
    verb = parts[0].lower().lstrip("/")
    remainder = parts[1] if len(parts) > 1 else ""
    if verb in WRITE_INTENTS:
        kind = IntentKind.WRITE
    elif verb in READ_INTENTS:
        kind = IntentKind.READ
    else:
        kind = IntentKind.UNKNOWN
    return verb, kind, remainder


# ---------------------------------------------------------------------------
# Identity / operator registry
# ---------------------------------------------------------------------------

# Roles allowed to issue WRITE intents. Everything else is read-only.
WRITE_ROLES: frozenset[str] = frozenset({"Owner", "Operator", "Admin"})


@dataclass(frozen=True)
class OperatorIdentity:
    """A resolved, role-scoped identity behind a channel handle."""

    operator_id: str
    role: str = "Public"
    authority_key_id: Optional[str] = None

    @property
    def can_write(self) -> bool:
        return self.role in WRITE_ROLES

    @property
    def is_public(self) -> bool:
        return self.role == "Public"


# Public identity used for anonymous web sessions — read-only, no authority.
PUBLIC_IDENTITY = OperatorIdentity(operator_id="public", role="Public")


class OperatorRegistry:
    """Maps ``(channel, channel_user_id)`` to an :class:`OperatorIdentity`.

    Unbound handles resolve to :data:`PUBLIC_IDENTITY` (read-only) so an
    unknown sender can never acquire write authority by default.
    """

    def __init__(self) -> None:
        self._bindings: dict[tuple[str, str], OperatorIdentity] = {}

    def bind(self, channel: str, channel_user_id: str, identity: OperatorIdentity) -> None:
        self._bindings[(channel, channel_user_id)] = identity

    def unbind(self, channel: str, channel_user_id: str) -> None:
        self._bindings.pop((channel, channel_user_id), None)

    def resolve(self, channel: str, channel_user_id: str) -> OperatorIdentity:
        return self._bindings.get((channel, channel_user_id), PUBLIC_IDENTITY)


# ---------------------------------------------------------------------------
# Messages / responses
# ---------------------------------------------------------------------------

@dataclass
class HermesMessage:
    channel: str
    channel_user_id: str
    raw_text: str
    channel_context: dict[str, Any] = field(default_factory=dict)


@dataclass
class HermesResponse:
    text: str
    success: bool = True
    ui_hints: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Core client interface
# ---------------------------------------------------------------------------

@dataclass
class CoreRequest:
    """A governed request handed to NoblePort Core after gateway checks."""

    operator: OperatorIdentity
    channel: str
    intent: str
    kind: IntentKind
    text: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class CoreResponse:
    text: str
    success: bool = True
    ui_hints: dict[str, Any] = field(default_factory=dict)


class CoreClient(Protocol):
    """Interface the gateway uses to reach NoblePort Core / Stephanie.

    Production wires this to the live NoblePort Core client. The gateway has
    already audited and governance-checked the request before calling
    :meth:`handle`; Core is responsible for the actual business logic.
    """

    async def handle(self, request: CoreRequest) -> CoreResponse: ...


class LocalEchoCore:
    """NON-PRODUCTION placeholder Core client.

    Returns a safe, canned acknowledgement without touching Stephanie or any
    real business system. Present so the drivers and gateway are runnable and
    testable before the live NoblePort Core integration lands. Wire a real
    :class:`CoreClient` in production — see the module docstring / README.
    """

    async def handle(self, request: CoreRequest) -> CoreResponse:
        if request.kind is IntentKind.WRITE:
            # Should never reach here for read-only identities; the gateway
            # rejects those before dispatch. This is a placeholder ack.
            return CoreResponse(
                text=(
                    f"[non-production core] received write intent "
                    f"'{request.intent}' from {request.operator.role}."
                )
            )
        return CoreResponse(
            text=(
                "[non-production core] NoblePort Core is not yet wired in. "
                "This is a placeholder acknowledgement."
            )
        )


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------

AuditHook = Callable[[dict[str, Any]], Awaitable[None]]


def hash_identifier(value: str, salt: str = "") -> str:
    """One-way, truncated hash so audit records never carry raw PII.

    A ``salt`` (from ``HERMES_PHONE_HASH_SALT``) turns this into a keyed HMAC,
    which prevents trivial rainbow-table reversal of a phone number. Without a
    salt it degrades to a plain SHA-256 prefix — acceptable for correlation,
    but set a salt in production.
    """
    if not value:
        return ""
    if salt:
        digest = hmac.new(salt.encode("utf-8"), value.encode("utf-8"), hashlib.sha256)
    else:
        digest = hashlib.sha256(value.encode("utf-8"))
    return digest.hexdigest()[:16]


def make_db_audit(db: Any, *, phone_salt: str = "") -> AuditHook:
    """Build an audit hook backed by the repo's :func:`app.db.audit.insert_audit`.

    The returned coroutine accepts an event dict (the shape drivers already
    emit) and persists it to ``audit_log`` using the existing audit-first
    machinery, so Hermes audit lands in the same immutable log as the
    supervisor's.
    """
    from app.db.audit import insert_audit

    async def _hook(event: dict[str, Any]) -> None:
        payload = dict(event)
        event_type = str(payload.pop("kind", "hermes.event"))
        thread_id = payload.pop("thread_id", None)
        await insert_audit(
            db,
            event_type=event_type,
            payload=payload,
            thread_id=thread_id,
            message=payload.get("message"),
        )

    return _hook


# ---------------------------------------------------------------------------
# Driver base class
# ---------------------------------------------------------------------------

class HermesDriver(abc.ABC):
    """Base class for a channel driver.

    A driver adapts a channel's transport to :class:`HermesMessage` /
    :class:`HermesResponse` and calls ``gateway.receive`` for every inbound
    message. It must never call Core or Stephanie directly.
    """

    name: str = "driver"

    @abc.abstractmethod
    async def start(self, gateway: "HermesGateway") -> None:  # pragma: no cover - interface
        ...

    @abc.abstractmethod
    async def send(self, channel_user_id: str, response: HermesResponse) -> None:  # pragma: no cover
        ...

    async def stop(self) -> None:  # pragma: no cover - default no-op
        return None


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

class GovernanceError(RuntimeError):
    """Raised internally when a request violates a governance boundary."""


# Per-channel default role for a sender that is not explicitly bound in the
# OperatorRegistry. Web is Public (anonymous, read-only). WhatsApp customers
# are read-only "Customer". Slack has no anonymous senders — an unbound Slack
# user is Public (read-only) until an operator binds them.
_CHANNEL_DEFAULT_ROLE = {
    "web": "Public",
    "whatsapp": "Customer",
    "slack": "Public",
}


class HermesGateway:
    """The single governed path from any channel to NoblePort Core."""

    def __init__(
        self,
        *,
        core: CoreClient,
        operators: Optional[OperatorRegistry] = None,
        audit_hook: Optional[AuditHook] = None,
        phone_salt: str = "",
    ) -> None:
        self._core = core
        self._operators = operators or OperatorRegistry()
        self._audit = audit_hook
        self._phone_salt = phone_salt
        self._drivers: dict[str, HermesDriver] = {}

    # -- registry --------------------------------------------------------
    @property
    def operators(self) -> OperatorRegistry:
        return self._operators

    def register_driver(self, driver: HermesDriver) -> None:
        self._drivers[driver.name] = driver

    async def start_all(self) -> None:
        for driver in self._drivers.values():
            await driver.start(self)

    async def stop_all(self) -> None:
        for driver in self._drivers.values():
            try:
                await driver.stop()
            except Exception as e:  # noqa: BLE001 - best-effort shutdown
                log.warning("[gateway] driver %s stop failed: %s", driver.name, e)

    # -- audit -----------------------------------------------------------
    async def _emit_audit(self, event: dict[str, Any]) -> None:
        if not self._audit:
            return
        try:
            await self._audit(event)
        except Exception as e:  # noqa: BLE001 - audit failure must not crash ingress
            log.warning("[gateway] audit emit failed (non-fatal): %s", e)

    def _audit_user_ref(self, msg: HermesMessage) -> dict[str, Any]:
        """Return an audit-safe reference to the sender — never raw phone PII."""
        if msg.channel == "whatsapp":
            ref = msg.channel_context.get("phone_hash") or hash_identifier(
                msg.channel_user_id, self._phone_salt
            )
            return {"user_hash": ref}
        return {"user": msg.channel_user_id}

    # -- ingress ---------------------------------------------------------
    async def receive(self, msg: HermesMessage) -> HermesResponse:
        """Governed ingress. Resolve identity, classify, audit, dispatch."""
        identity = self._resolve_identity(msg)
        intent, kind, _ = parse_intent(msg.raw_text)

        audit_base = {
            "channel": msg.channel,
            "role": identity.role,
            "operator_id": identity.operator_id,
            "intent": intent,
            "intent_kind": kind.value,
            **self._audit_user_ref(msg),
        }

        # Governance: read-only identities may not issue WRITE intents.
        if kind is IntentKind.WRITE and not identity.can_write:
            await self._emit_audit(
                {"kind": "hermes.write_rejected", "reason": "insufficient_authority", **audit_base}
            )
            log.info(
                "[gateway] rejected WRITE intent '%s' from role=%s channel=%s",
                intent, identity.role, msg.channel,
            )
            return HermesResponse(
                text=(
                    "That action would change our records and isn't available "
                    "on this channel. Please contact an operator directly."
                ),
                success=False,
                ui_hints={"rejected": "write_not_permitted"},
            )

        await self._emit_audit({"kind": "hermes.route", **audit_base})

        request = CoreRequest(
            operator=identity,
            channel=msg.channel,
            intent=intent,
            kind=kind,
            text=msg.raw_text,
            context=msg.channel_context,
        )
        try:
            result = await self._core.handle(request)
        except Exception as e:  # noqa: BLE001 - never leak a stack trace to a channel
            log.exception("[gateway] core.handle failed: %s", e)
            await self._emit_audit({"kind": "hermes.core_error", "error": str(e)[:200], **audit_base})
            return HermesResponse(
                text="Something went wrong handling that. Please try again shortly.",
                success=False,
            )
        return HermesResponse(text=result.text, success=result.success, ui_hints=result.ui_hints)

    def _resolve_identity(self, msg: HermesMessage) -> OperatorIdentity:
        identity = self._operators.resolve(msg.channel, msg.channel_user_id)
        if identity is not PUBLIC_IDENTITY:
            return identity
        # Unbound handle: fall back to the channel default role.
        role = _CHANNEL_DEFAULT_ROLE.get(msg.channel, "Public")
        if role == "Public":
            return PUBLIC_IDENTITY
        return OperatorIdentity(operator_id=f"{msg.channel}:anon", role=role)
