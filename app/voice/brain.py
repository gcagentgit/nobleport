"""Stephanie.ai conversation brain (controlled-prompt, no rambling).

This module enforces the launch-rule scoping for what Stephanie may answer
without human review. It is intentionally a deterministic classifier +
templated responder rather than a free-form LLM, so launch behaviour is
auditable and machine-checkable.

Domains:
  - greeting: open the conversation; never refuses.
  - construction: permit/jobsite/scheduling answers, scoped templates.
  - permit: permit-status / filing answers, scoped templates.
  - defi_nbpt: any NoblePort token / DeFi topic. Always returns a response
    paired with the regulatory disclaimer, never investment advice.
  - investor_compliance: investor relations, securities, legal compliance.
    Always requires human approval (`requires_human_approval=True`) and
    Stephanie returns a holding response.
  - unknown: scoped fallback rather than a long generated answer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class BrainResponse:
    domain: str
    reply: str
    next_action: str
    requires_human_approval: bool
    disclaimers: tuple[str, ...]


_GREETING_PATTERNS = (
    r"\bhi\b",
    r"\bhello\b",
    r"\bhey\b",
    r"\bgood (morning|afternoon|evening)\b",
    r"\bstephanie\b",
)

_CONSTRUCTION_PATTERNS = (
    r"\bjobsite\b",
    r"\bconstruction\b",
    r"\bcontractor\b",
    r"\bschedule (a )?(walk|inspection)\b",
    r"\bsite walk\b",
)

_PERMIT_PATTERNS = (
    r"\bpermit\b",
    r"\bzoning\b",
    r"\bcertificate of occupancy\b",
    r"\bbuilding department\b",
)

_DEFI_PATTERNS = (
    r"\bdefi\b",
    r"\bnbpt\b",
    r"\btoken\b",
    r"\byield\b",
    r"\bstak(e|ing)\b",
    r"\bapy\b",
)

_INVESTOR_PATTERNS = (
    r"\baccredited\b",
    r"\binvestor\b",
    r"\bsubscription agreement\b",
    r"\bsecurities\b",
    r"\bsec\b",
    r"\b506\(c\)\b",
    r"\bcompliance\b",
    r"\binvest\b",
)


REG_DISCLAIMER = (
    "This is general information, not investment, tax, or legal advice. "
    "NoblePort tokens (NBPT) are offered only to accredited investors under "
    "applicable securities exemptions; suitability is verified by a human."
)
NO_AUTOMATED_INVESTMENT_ADVICE = (
    "Stephanie does not provide automated investment recommendations."
)


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def classify(text: str) -> str:
    """Classify by precedence: investor > defi > permit > construction >
    greeting > unknown. Investor wins because it's the highest-risk domain
    and must never be answered without human approval.
    """
    if _matches(text, _INVESTOR_PATTERNS):
        return "investor_compliance"
    if _matches(text, _DEFI_PATTERNS):
        return "defi_nbpt"
    if _matches(text, _PERMIT_PATTERNS):
        return "permit"
    if _matches(text, _CONSTRUCTION_PATTERNS):
        return "construction"
    if _matches(text, _GREETING_PATTERNS):
        return "greeting"
    return "unknown"


_GREETING_REPLY = (
    "Hi, this is Stephanie at NoblePort. I can help with construction "
    "schedules, permit lookups, or general NoblePort questions. Anything "
    "investment-related I'll route to a human. What can I help with?"
)
_CONSTRUCTION_REPLY = (
    "I can pull up jobsite schedule, contractor contact, and recent site "
    "activity. I will not approve change orders or commit funds — those go "
    "to a human."
)
_PERMIT_REPLY = (
    "I can look up permit status, filing dates, and inspection windows for "
    "Essex County properties. For appeals or zoning interpretations I will "
    "loop in a human reviewer."
)
_DEFI_REPLY = (
    "I can describe published NoblePort token mechanics at a high level. I "
    "can't quote yield, sign you up, or give investment advice."
)
_INVESTOR_HOLDING_REPLY = (
    "Investor and compliance questions require a human reviewer. I'm "
    "logging your request and a NoblePort representative will follow up."
)
_UNKNOWN_REPLY = (
    "I don't have a verified answer for that yet. Let me get a human to "
    "follow up rather than guess."
)


def respond(text: str) -> BrainResponse:
    """Generate a launch-safe response. Caller is responsible for routing
    `requires_human_approval=True` responses through escalation BEFORE any
    follow-up action is taken.
    """
    domain = classify(text)
    if domain == "greeting":
        return BrainResponse(
            domain=domain,
            reply=_GREETING_REPLY,
            next_action="await_user",
            requires_human_approval=False,
            disclaimers=(),
        )
    if domain == "construction":
        return BrainResponse(
            domain=domain,
            reply=_CONSTRUCTION_REPLY,
            next_action="lookup_jobsite",
            requires_human_approval=False,
            disclaimers=(),
        )
    if domain == "permit":
        return BrainResponse(
            domain=domain,
            reply=_PERMIT_REPLY,
            next_action="lookup_permit",
            requires_human_approval=False,
            disclaimers=(),
        )
    if domain == "defi_nbpt":
        return BrainResponse(
            domain=domain,
            reply=_DEFI_REPLY,
            next_action="provide_published_overview",
            requires_human_approval=False,
            disclaimers=(REG_DISCLAIMER, NO_AUTOMATED_INVESTMENT_ADVICE),
        )
    if domain == "investor_compliance":
        return BrainResponse(
            domain=domain,
            reply=_INVESTOR_HOLDING_REPLY,
            next_action="escalate_to_human",
            requires_human_approval=True,
            disclaimers=(REG_DISCLAIMER,),
        )
    return BrainResponse(
        domain="unknown",
        reply=_UNKNOWN_REPLY,
        next_action="escalate_to_human",
        requires_human_approval=True,
        disclaimers=(),
    )
