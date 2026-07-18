"""Hermes channel configuration loaded from environment variables.

Follows the same dataclass-from-env pattern as :mod:`app.config`. No secrets
are hard-coded; everything comes from the environment. Absent tokens simply
mean the corresponding driver is not wired up at boot.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _csv_set(raw: str) -> set[str]:
    return {item.strip() for item in raw.split(",") if item.strip()}


@dataclass(frozen=True)
class HermesSettings:
    # Slack
    slack_bot_token: str
    slack_app_token: str
    slack_allowed_channel_ids: frozenset[str]
    slack_allowed_team_ids: frozenset[str]
    slack_dm_only_if_no_allowlist: bool

    # WhatsApp / Twilio
    twilio_account_sid: str
    twilio_auth_token: str
    whatsapp_from: str
    twilio_validate_signatures: bool
    twilio_public_base_url: str
    twilio_trust_forwarded: bool

    # Web
    web_rate_per_min: int
    web_rate_per_hour: int
    web_session_idle_seconds: int
    web_secure_cookies: bool

    # Shared
    phone_hash_salt: str
    redis_url: str

    @classmethod
    def from_env(cls) -> "HermesSettings":
        return cls(
            slack_bot_token=os.environ.get("SLACK_BOT_TOKEN", ""),
            slack_app_token=os.environ.get("SLACK_APP_TOKEN", ""),
            slack_allowed_channel_ids=frozenset(_csv_set(os.environ.get("SLACK_ALLOWED_CHANNELS", ""))),
            slack_allowed_team_ids=frozenset(_csv_set(os.environ.get("SLACK_ALLOWED_TEAM_IDS", ""))),
            slack_dm_only_if_no_allowlist=_flag("SLACK_DM_ONLY_IF_NO_ALLOWLIST", True),
            twilio_account_sid=os.environ.get("TWILIO_ACCOUNT_SID", ""),
            twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN", ""),
            whatsapp_from=os.environ.get("WHATSAPP_FROM", ""),
            twilio_validate_signatures=_flag("TWILIO_VALIDATE_SIGNATURES", True),
            twilio_public_base_url=os.environ.get("TWILIO_PUBLIC_BASE_URL", ""),
            twilio_trust_forwarded=_flag("TWILIO_TRUST_FORWARDED", False),
            web_rate_per_min=int(os.environ.get("WEB_RATE_PER_MIN", "5")),
            web_rate_per_hour=int(os.environ.get("WEB_RATE_PER_HOUR", "30")),
            web_session_idle_seconds=int(os.environ.get("WEB_SESSION_IDLE_SECONDS", "3600")),
            web_secure_cookies=_flag("WEB_SECURE_COOKIES", True),
            phone_hash_salt=os.environ.get("HERMES_PHONE_HASH_SALT", ""),
            redis_url=os.environ.get("REDIS_URL", ""),
        )


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
