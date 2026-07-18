"""Hermes channel drivers — Slack, WhatsApp, Web Chat.

Each driver adapts a channel to :class:`~app.hermes.gateway.HermesMessage` /
:class:`~app.hermes.gateway.HermesResponse` and routes **every** inbound
message through :meth:`HermesGateway.receive`. No driver calls NoblePort Core
or Stephanie directly — the gateway is the sole governed path.

Design for testability: all channel-SDK imports (``slack_bolt``, ``twilio``,
``fastapi``) are lazy and live inside ``start`` / ``router`` / sender objects.
The routing, allowlist, dedup, consent, and rate-limit decision logic lives in
plain methods that operate on dicts and can be exercised with a fake gateway
and no third-party packages installed.

Governance posture per channel:

  Slack     Operator-facing. Workspace (team) + channel allowlist. Inbound
            events deduplicated (Slack redelivers on timeout).

  WhatsApp  Customer-facing. Consent-gated: unknown numbers get a consent
            prompt and are NOT routed; STOP/HELP/START handled locally;
            only opted-in numbers reach the gateway; outbound refused unless
            opted-in. Twilio signature verified on every inbound request.

  Web       Public, read-only. WRITE intents stripped at ingress (the gateway
            also rejects them). Session-scoped, bounded rate limiting.

These are implementation safeguards, not legal-compliance guarantees;
consent/keyword handling still requires legal review before customer rollout.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional, Protocol

# FastAPI is a core dependency of this repo (see requirements.txt), so it is
# imported at module scope. This is also required for correct route-parameter
# type resolution: with ``from __future__ import annotations`` in effect,
# FastAPI resolves handler annotations against the module globals, so the
# request/form/response types must live here, not inside ``router()``.
# Only the channel SDKs (slack_bolt, twilio) stay lazily imported.
from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.hermes.gateway import (
    HermesDriver,
    HermesGateway,
    HermesMessage,
    HermesResponse,
    IntentKind,
    hash_identifier,
    parse_intent,
)

log = logging.getLogger("hermes.drivers")


# ============================================================
# 1. Slack driver (operator-facing, Socket Mode)
# ============================================================

class _DedupCache:
    """Bounded TTL set for Slack event de-duplication.

    Slack redelivers an event if the app does not ack within ~3s. Without
    dedup a slow Core call would cause the same operator command to run
    multiple times. Keys expire after ``ttl`` and the set is size-capped.
    """

    def __init__(self, ttl_seconds: int = 300, max_size: int = 10_000) -> None:
        self._ttl = ttl_seconds
        self._max = max_size
        self._seen: dict[str, float] = {}

    def check_and_add(self, key: str) -> bool:
        """Return True if ``key`` is new (and record it); False if duplicate."""
        if not key:
            return True
        now = time.time()
        self._evict(now)
        if key in self._seen and now - self._seen[key] <= self._ttl:
            return False
        self._seen[key] = now
        return True

    def _evict(self, now: float) -> None:
        if len(self._seen) > self._max:
            self._seen = {k: t for k, t in self._seen.items() if now - t <= self._ttl}


class SlackDriver(HermesDriver):
    name = "slack"

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        allowed_channel_ids: Optional[set[str]] = None,
        allowed_team_ids: Optional[set[str]] = None,
        dm_only_if_no_allowlist: bool = True,
        dedup_ttl_seconds: int = 300,
    ) -> None:
        self._bot_token = bot_token
        self._app_token = app_token
        self._allowed = set(allowed_channel_ids or set())
        self._allowed_teams = set(allowed_team_ids or set())
        self._dm_only = dm_only_if_no_allowlist and not self._allowed
        self._dedup = _DedupCache(ttl_seconds=dedup_ttl_seconds)
        self._gateway: Optional[HermesGateway] = None
        self._app: Any = None
        self._handler: Any = None
        self._client: Any = None

    # -- pure, testable ingress logic -----------------------------------
    def _is_allowed(self, event: dict[str, Any]) -> bool:
        team_id = event.get("team") or event.get("team_id") or ""
        if self._allowed_teams and team_id not in self._allowed_teams:
            return False
        channel_type = event.get("channel_type", "")
        channel_id = event.get("channel", "")
        if channel_type == "im":
            return True
        if self._dm_only:
            return False
        if self._allowed and channel_id not in self._allowed:
            return False
        return True

    @staticmethod
    def _dedup_key(event: dict[str, Any]) -> str:
        return (
            event.get("client_msg_id")
            or event.get("event_id")
            or f"{event.get('channel', '')}:{event.get('ts', '')}"
        )

    @staticmethod
    def _is_ignorable(event: dict[str, Any]) -> bool:
        if event.get("subtype") in ("bot_message", "message_changed", "message_deleted"):
            return True
        if event.get("bot_id"):
            return True
        return False

    async def handle_message_event(self, event: dict[str, Any]) -> Optional[HermesResponse]:
        """Decide whether/how to route a Slack message event.

        Returns the gateway response to reply with, or ``None`` when the event
        is ignored (bot echo, disallowed channel, or duplicate).
        """
        if self._is_ignorable(event):
            return None
        if not self._is_allowed(event):
            return None
        if not self._dedup.check_and_add(self._dedup_key(event)):
            log.info("[slack] dropping duplicate event %s", self._dedup_key(event))
            return None
        text = event.get("text", "") or ""
        return await self._route(
            event.get("user", ""),
            text,
            {
                "channel": event.get("channel"),
                "channel_type": event.get("channel_type"),
                "team": event.get("team"),
                "ts": event.get("ts"),
            },
        )

    async def _route(self, slack_user_id: str, text: str, ctx: dict[str, Any]) -> HermesResponse:
        if not self._gateway:
            return HermesResponse(text="Gateway not started.", success=False)
        msg = HermesMessage(
            channel=self.name,
            channel_user_id=slack_user_id,
            raw_text=text,
            channel_context=ctx,
        )
        return await self._gateway.receive(msg)

    # -- lifecycle (lazy SDK import) ------------------------------------
    async def start(self, gateway: HermesGateway) -> None:
        self._gateway = gateway
        try:
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
            from slack_bolt.async_app import AsyncApp
        except ImportError as e:  # pragma: no cover - exercised only with SDK absent
            raise RuntimeError(
                "SlackDriver.start requires slack-bolt: pip install 'slack-bolt>=1.20'"
            ) from e

        self._app = AsyncApp(token=self._bot_token)
        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        self._client = self._app.client
        self._register_handlers()
        log.info(
            "[slack] starting Socket Mode (dm_only=%s, channels=%d, teams=%d)",
            self._dm_only, len(self._allowed), len(self._allowed_teams),
        )
        await self._handler.start_async()

    def _register_handlers(self) -> None:  # pragma: no cover - requires live SDK
        app = self._app

        @app.event("message")
        async def on_message(event, say):
            resp = await self.handle_message_event(event)
            if resp is not None:
                await self._send_via_say(say, resp)

        @app.event("app_mention")
        async def on_mention(event, say):
            text = (event.get("text", "") or "").split(">", 1)[-1].strip()
            evt = dict(event)
            evt["text"] = text
            resp = await self.handle_message_event(evt)
            if resp is not None:
                await self._send_via_say(say, resp)

        @app.action("hermes_action")
        async def on_button(ack, body, respond):
            await ack()
            action = (body.get("actions") or [{}])[0]
            intent, _, req_id = (action.get("value", "") or "").partition("|")
            resp = await self._route(
                body.get("user", {}).get("id", ""),
                f"{intent} {req_id}".strip(),
                {"channel": (body.get("channel") or {}).get("id"), "team": body.get("team", {}).get("id")},
            )
            await respond(resp.text)

    async def _send_via_say(self, say: Any, response: HermesResponse) -> None:  # pragma: no cover
        blocks = slack_blocks(response)
        if blocks:
            await say(text=response.text, blocks=blocks)
        else:
            await say(response.text)

    async def send(self, channel_user_id: str, response: HermesResponse) -> None:
        if not self._client:
            log.warning("[slack] send before start; dropping push to %s", channel_user_id)
            return
        await self._client.chat_postMessage(
            channel=channel_user_id,
            text=response.text,
            blocks=slack_blocks(response) or None,
        )

    async def stop(self) -> None:
        if self._handler is not None:
            try:
                await self._handler.close_async()
            except Exception as e:  # noqa: BLE001 - best-effort shutdown
                log.warning("[slack] stop failed: %s", e)


def _slack_escape(text: str) -> str:
    """Escape Slack control characters so response text can't inject markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_blocks(response: HermesResponse) -> list[dict[str, Any]]:
    """Render :class:`HermesResponse` UI hints into Slack Block Kit."""
    buttons = response.ui_hints.get("action_buttons") or []
    if not buttons:
        return []
    elements = []
    for btn in buttons[:5]:
        elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": str(btn.get("label", ""))[:75]},
                "value": f"{btn.get('intent', '')}|{(btn.get('args') or {}).get('request_id', '')}",
                "action_id": "hermes_action",
                "style": "primary" if btn.get("intent") == "approve" else "danger",
            }
        )
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": _slack_escape(response.text)}},
        {"type": "actions", "elements": elements},
    ]


# ============================================================
# 2. WhatsApp driver (customer-facing, Twilio + FastAPI)
# ============================================================

STOP_KEYWORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
START_KEYWORDS = {"START", "UNSTOP", "YES", "AGREE", "OK", "OKAY", "CONFIRM"}
HELP_KEYWORDS = {"HELP", "INFO"}

CONSENT_PROMPT = (
    "NoblePort Roofing here. This number is used for estimate and job "
    "coordination texts. Reply YES to consent (msg & data rates may apply, "
    "freq varies). STOP to opt out. HELP for info."
)
CONSENT_CONFIRMED = "You're opted in. How can we help? Reply STOP any time to opt out."
OPT_OUT_ACK = (
    "You're opted out. You will not receive further messages from this "
    "number. Reply START to opt back in."
)
WA_HELP_TEXT = (
    "NoblePort Roofing coordination line. Msg & data rates may apply. Msg "
    "frequency varies. Reply STOP to opt out. Support: nobleport.net"
)


class OutboundSender(Protocol):
    async def send(self, *, from_: str, to: str, body: str) -> None: ...


class TwilioSender:
    """Outbound WhatsApp sender wrapping the (blocking) Twilio REST client.

    ``twilio.rest.Client`` is synchronous, so calling it directly from an async
    handler blocks the event loop. Every send is dispatched via
    :func:`asyncio.to_thread` to keep the loop responsive.
    """

    def __init__(self, account_sid: str, auth_token: str) -> None:
        self._sid = account_sid
        self._auth = auth_token
        self._client: Any = None

    def _ensure(self) -> Any:
        if self._client is None:
            from twilio.rest import Client  # lazy import

            self._client = Client(self._sid, self._auth)
        return self._client

    async def send(self, *, from_: str, to: str, body: str) -> None:
        client = self._ensure()
        await asyncio.to_thread(client.messages.create, from_=from_, to=to, body=body)


class WhatsAppDriver(HermesDriver):
    name = "whatsapp"

    MAX_REPLY_CHARS = 1000

    def __init__(
        self,
        twilio_account_sid: str,
        twilio_auth_token: str,
        whatsapp_from: str,  # "whatsapp:+15551234567"
        consent_registry: Any,
        *,
        sender: Optional[OutboundSender] = None,
        route_prefix: str = "/twilio/whatsapp",
        validate_signatures: bool = True,
        public_base_url: str = "",
        trust_forwarded: bool = False,
        phone_salt: str = "",
    ) -> None:
        self._sid = twilio_account_sid
        self._auth = twilio_auth_token
        self._from = whatsapp_from
        self._consent = consent_registry
        self._sender = sender or TwilioSender(twilio_account_sid, twilio_auth_token)
        self._prefix = route_prefix
        self._validate = validate_signatures
        self._public_base_url = public_base_url
        self._trust_forwarded = trust_forwarded
        self._phone_salt = phone_salt
        self._gateway: Optional[HermesGateway] = None

    # -- signature verification -----------------------------------------
    def verify_signature(
        self, observed_url: str, headers: dict[str, str], form: dict[str, str], signature: str
    ) -> bool:
        if not self._validate:
            return True
        from app.hermes.twilio_sig import reconstruct_public_url, validate_signature

        url = reconstruct_public_url(
            observed_url,
            headers,
            public_base_url=self._public_base_url,
            trust_forwarded=self._trust_forwarded,
        )
        return validate_signature(self._auth, url, form, signature)

    # -- consent gate (pure, testable) ----------------------------------
    async def handle_inbound(self, from_addr: str, body: str) -> str:
        """Consent gate. Returns reply text (empty string == silent drop)."""
        phone = from_addr  # keep "whatsapp:+1..." prefix for Twilio addressing
        text = (body or "").strip()
        upper = text.upper()

        # 1. STOP family — always honored, regardless of prior state.
        if upper in STOP_KEYWORDS:
            await self._consent.set(phone, "opted_out", "sms_stop")
            return OPT_OUT_ACK

        # 2. HELP family — informational, never routed.
        if upper in HELP_KEYWORDS:
            return WA_HELP_TEXT

        record = await self._consent.get(phone)

        # 3. Opted-out: silent drop except explicit opt-in restart.
        if record.status == "opted_out":
            if upper in START_KEYWORDS:
                await self._consent.set(phone, "pending", "sms_restart")
                return CONSENT_PROMPT
            return ""

        # 4. Unknown / pending: require explicit YES before any routing.
        if record.status in ("unknown", "pending"):
            if record.status == "unknown":
                await self._consent.set(phone, "pending", "first_contact")
                return CONSENT_PROMPT
            if upper in START_KEYWORDS:
                await self._consent.set(phone, "opted_in", "sms_confirm")
                return CONSENT_CONFIRMED
            return CONSENT_PROMPT  # anything else while pending re-prompts

        # 5. Opted-in — route through the gateway.
        if not self._gateway:
            return "Service warming up, try again in a moment."
        msg = HermesMessage(
            channel=self.name,
            channel_user_id=phone,
            raw_text=text,
            channel_context={"phone_hash": hash_identifier(phone, self._phone_salt)},
        )
        response = await self._gateway.receive(msg)
        return (response.text or "")[: self.MAX_REPLY_CHARS] or "(no reply)"

    # -- HTTP router (lazy fastapi import) ------------------------------
    def router(self) -> Any:
        r = APIRouter()

        @r.post(self._prefix)
        async def inbound(
            request: Request,
            From: str = Form(...),
            Body: str = Form(""),
            MessageSid: str = Form(""),
        ) -> Response:
            form_dict = {k: str(v) for k, v in (await request.form()).items()}
            sig = request.headers.get("X-Twilio-Signature", "")
            if not self.verify_signature(
                str(request.url), dict(request.headers), form_dict, sig
            ):
                log.warning("[whatsapp] rejected inbound: bad signature")
                return Response(status_code=403)

            reply = await self.handle_inbound(From, Body)
            if reply:
                twiml = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    f"<Response><Message>{_xml_escape(reply)}</Message></Response>"
                )
            else:
                twiml = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
            return Response(content=twiml, media_type="application/xml")

        return r

    async def start(self, gateway: HermesGateway) -> None:
        self._gateway = gateway
        log.info("[whatsapp] router ready at %s (sig_validate=%s)", self._prefix, self._validate)

    async def send(self, channel_user_id: str, response: HermesResponse) -> None:
        """Outbound push. Refuses to message a non-opted-in number."""
        rec = await self._consent.get(channel_user_id)
        if rec.status != "opted_in":
            log.warning(
                "[whatsapp] refusing outbound to non-opted-in %s",
                hash_identifier(channel_user_id, self._phone_salt),
            )
            return
        await self._sender.send(
            from_=self._from,
            to=channel_user_id,
            body=(response.text or "")[: self.MAX_REPLY_CHARS],
        )

    async def stop(self) -> None:
        return None


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ============================================================
# 3. Web Chat driver (public, read-only)
# ============================================================

class WebChatDriver(HermesDriver):
    name = "web"

    MAX_TEXT_CHARS = 500

    def __init__(
        self,
        session_registry: Any,
        rate_limiter: Any,
        *,
        route_prefix: str = "/chat",
        secure_cookies: bool = True,
    ) -> None:
        self._sessions = session_registry
        self._rate = rate_limiter
        self._prefix = route_prefix
        self._secure_cookies = secure_cookies
        self._gateway: Optional[HermesGateway] = None

    async def handle_message(self, sid: str, text: str, context: dict[str, Any]) -> dict[str, Any]:
        """Core web-message logic. Returns ``{"reply": ...}`` or raises.

        Read-only enforcement: WRITE intents are refused at ingress here as
        defense-in-depth; the gateway also rejects them for the Public role.
        """
        text = (text or "").strip()
        if not sid or not self._sessions.exists(sid):
            raise _WebError(401, "unknown session")
        if not text:
            raise _WebError(400, "empty text")
        if len(text) > self.MAX_TEXT_CHARS:
            raise _WebError(413, "message too long")
        if not await self._rate.allow(sid):
            raise _WebError(429, "rate limit")

        _, kind, _ = parse_intent(text)
        if kind is IntentKind.WRITE:
            return {
                "reply": (
                    "I can only answer questions here. For anything that would "
                    "change our records, please contact us directly."
                )
            }

        if not self._gateway:
            return {"reply": "Service warming up, try again in a moment."}
        msg = HermesMessage(
            channel=self.name,
            channel_user_id=sid,
            raw_text=text,
            channel_context=context,
        )
        response = await self._gateway.receive(msg)
        return {"reply": response.text}

    def router(self) -> Any:
        r = APIRouter()
        widget_html = _widget_html(self._prefix)

        @r.get(self._prefix)
        async def widget() -> Response:
            return Response(content=widget_html, media_type="text/html")

        @r.post(f"{self._prefix}/session")
        async def open_session() -> JSONResponse:
            sid = self._sessions.create()
            resp = JSONResponse({"session_id": sid})
            resp.set_cookie(
                key="hermes_sid",
                value=sid,
                httponly=True,
                samesite="strict",
                secure=self._secure_cookies,
                max_age=3600,
            )
            return resp

        @r.post(f"{self._prefix}/message")
        async def message(payload: dict, request: Request) -> dict[str, Any]:
            sid = request.cookies.get("hermes_sid") or payload.get("session_id") or ""
            try:
                return await self.handle_message(
                    sid,
                    payload.get("text") or "",
                    {"user_agent": request.headers.get("user-agent", "")[:200]},
                )
            except _WebError as e:
                raise HTTPException(status_code=e.status_code, detail=e.detail)

        return r

    async def start(self, gateway: HermesGateway) -> None:
        self._gateway = gateway
        log.info("[web] router ready at %s", self._prefix)

    async def send(self, channel_user_id: str, response: HermesResponse) -> None:
        return None  # web chat is request/response — no push path

    async def stop(self) -> None:
        return None


class _WebError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _widget_html(prefix: str) -> str:
    return _WIDGET_HTML_TEMPLATE.replace("__PREFIX__", prefix)


_WIDGET_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"/><title>NoblePort Assistant</title>
<style>
  :root { --bg:#0e1220; --fg:#e8e4d6; --brass:#b8944a; --line:#2a3350; }
  html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
    font-family:system-ui,-apple-system,sans-serif}
  #wrap{max-width:640px;margin:0 auto;height:100%;display:flex;flex-direction:column}
  header{padding:16px;border-bottom:1px solid var(--line);font-weight:600;color:var(--brass)}
  #log{flex:1;overflow:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
  .msg{max-width:80%;padding:10px 14px;border-radius:14px;line-height:1.4;white-space:pre-wrap}
  .u{background:#243055;align-self:flex-end}
  .a{background:#1a223c;align-self:flex-start;border:1px solid var(--line)}
  form{display:flex;gap:8px;padding:14px;border-top:1px solid var(--line)}
  input{flex:1;background:#141a2e;color:var(--fg);border:1px solid var(--line);
    border-radius:10px;padding:12px;font-size:15px}
  button{background:var(--brass);color:#111;border:0;border-radius:10px;
    padding:0 18px;font-weight:600;cursor:pointer}
  button:disabled{opacity:0.5;cursor:not-allowed}
</style></head><body>
<div id="wrap">
  <header>NoblePort Assistant · read-only</header>
  <div id="log"></div>
  <form id="f"><input id="i" autocomplete="off" maxlength="500"
    placeholder="Ask about our services…" required/>
    <button id="b">Send</button></form>
</div>
<script>
(async () => {
  const log = document.getElementById('log');
  const form = document.getElementById('f');
  const inp = document.getElementById('i');
  const btn = document.getElementById('b');
  let sid = null;
  const add = (text, cls) => {
    const d = document.createElement('div');
    d.className = 'msg ' + cls; d.textContent = text; log.appendChild(d);
    log.scrollTop = log.scrollHeight;
  };
  try {
    const r = await fetch('__PREFIX__/session', {method:'POST'});
    const j = await r.json(); sid = j.session_id;
    add('Hi — ask about roofing, estimates, or scheduling.', 'a');
  } catch (e) { add('Failed to start session.', 'a'); }
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const text = inp.value.trim(); if (!text || !sid) return;
    add(text, 'u'); inp.value = ''; btn.disabled = true;
    try {
      const r = await fetch('__PREFIX__/message', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({session_id: sid, text})});
      if (r.status === 429) { add('Slow down a moment.', 'a'); return; }
      const j = await r.json();
      add(j.reply || '(no reply)', 'a');
    } catch (e) { add('Connection error.', 'a'); }
    finally { btn.disabled = false; inp.focus(); }
  });
})();
</script></body></html>"""
