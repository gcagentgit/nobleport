"""Request ID + structured-logging middleware.

Guarantees every request gets an X-Request-ID header (echoed if the client
sent one, generated otherwise). Emits a single JSON line per request to
stdout with method, path, status, latency_ms, and the request id, so log
aggregators can index without parsing.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


_LOGGER = logging.getLogger("nobleport.access")
if not _LOGGER.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _LOGGER.addHandler(handler)
    _LOGGER.setLevel(logging.INFO)
    _LOGGER.propagate = False


_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_\-:.]{1,128}$")


class RequestIdLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        incoming = request.headers.get("X-Request-ID", "").strip()
        request_id = incoming if _SAFE_REQUEST_ID.match(incoming) else str(uuid.uuid4())
        request.state.request_id = request_id

        started = time.perf_counter()
        response: Response | None = None
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            entry = {
                "ts": time.time(),
                "level": "info",
                "msg": "http_request",
                "method": request.method,
                "path": request.url.path,
                "status": status,
                "latency_ms": round(elapsed_ms, 3),
                "request_id": request_id,
            }
            try:
                _LOGGER.info(json.dumps(entry, separators=(",", ":")))
            except Exception:  # noqa: BLE001 - logging must never raise
                pass
            if response is not None:
                response.headers["X-Request-ID"] = request_id
