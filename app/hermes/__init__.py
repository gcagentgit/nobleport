"""Hermes channel gateway and drivers for the NoblePort supervisor stack.

Hermes is the ingress layer that fronts NoblePort Core / Stephanie. Every
channel (Slack, WhatsApp, Web Chat) speaks to the outside world but routes
*all* traffic through :class:`HermesGateway`. Drivers never call Core or
Stephanie directly — the gateway is the single governance chokepoint where
operator identity, intent classification, and audit are enforced.

Status: APPROVED ARCHITECTURE BLUEPRINT / TARGET STATE IMPLEMENTATION.
Not VERIFIED until running against a live NoblePort Core and audited.
"""
from __future__ import annotations

from app.hermes.gateway import (
    CoreClient,
    CoreRequest,
    CoreResponse,
    GovernanceError,
    HermesDriver,
    HermesGateway,
    HermesMessage,
    HermesResponse,
    IntentKind,
    LocalEchoCore,
    OperatorIdentity,
    OperatorRegistry,
    hash_identifier,
    make_db_audit,
    parse_intent,
)

__all__ = [
    "CoreClient",
    "CoreRequest",
    "CoreResponse",
    "GovernanceError",
    "HermesDriver",
    "HermesGateway",
    "HermesMessage",
    "HermesResponse",
    "IntentKind",
    "LocalEchoCore",
    "OperatorIdentity",
    "OperatorRegistry",
    "hash_identifier",
    "make_db_audit",
    "parse_intent",
]
