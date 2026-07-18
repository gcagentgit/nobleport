"""Twilio request-signature validation (stdlib implementation).

Twilio signs each webhook with ``X-Twilio-Signature``: HMAC-SHA1, keyed by the
account auth token, over ``full_url`` followed by each POST parameter's key and
value concatenated in lexicographic key order, then base64-encoded. Validating
it proves the request genuinely came from Twilio.

Implementing the check with the standard library (rather than importing
``twilio``) keeps the inbound path dependency-light and unit-testable without
the SDK installed.

Proxy note: behind a load balancer / TLS-terminating proxy the URL the app
observes (``http://internal:8080/...``) differs from the public URL Twilio
signed (``https://public.example.com/...``). :func:`reconstruct_public_url`
rebuilds the signed URL from a configured public base or trusted
``X-Forwarded-*`` headers. If the reconstructed URL is wrong, validation fails
closed (rejects), which is the safe default.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit


def compute_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    """Compute the expected base64 ``X-Twilio-Signature`` for a request."""
    data = url
    for key in sorted(params.keys()):
        data += key + (params[key] if params[key] is not None else "")
    mac = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode("utf-8")


def validate_signature(
    auth_token: str, url: str, params: Mapping[str, str], signature: str
) -> bool:
    """Constant-time comparison of the provided signature against the expected."""
    if not signature:
        return False
    expected = compute_signature(auth_token, url, params)
    return hmac.compare_digest(expected, signature)


def reconstruct_public_url(
    observed_url: str,
    headers: Mapping[str, str],
    *,
    public_base_url: str = "",
    trust_forwarded: bool = False,
) -> str:
    """Rebuild the public URL Twilio signed.

    Precedence:
      1. ``public_base_url`` (explicit config) — most reliable; its scheme and
         host replace the observed ones, path/query preserved.
      2. ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` — only when
         ``trust_forwarded`` is set (i.e. the deployment sits behind a proxy
         you control). Untrusted forwarded headers are ignored to prevent
         signature-bypass via header spoofing.
      3. The observed URL unchanged (direct, non-proxied deployment).
    """
    parts = urlsplit(observed_url)
    scheme, netloc = parts.scheme, parts.netloc

    if public_base_url:
        base = urlsplit(public_base_url)
        if base.scheme:
            scheme = base.scheme
        if base.netloc:
            netloc = base.netloc
    elif trust_forwarded:
        proto = headers.get("x-forwarded-proto") or headers.get("X-Forwarded-Proto")
        host = headers.get("x-forwarded-host") or headers.get("X-Forwarded-Host")
        if proto:
            scheme = proto.split(",")[0].strip()
        if host:
            netloc = host.split(",")[0].strip()

    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))
