"""HTTP host that mounts every HTTP-based Hermes driver on one FastAPI app.

WhatsApp and Web Chat expose FastAPI routers; Slack does not (it uses Socket
Mode). This host mounts the HTTP drivers, wires a lifespan that starts each
driver against the gateway on boot and stops them on shutdown (graceful
lifecycle), and can serve via uvicorn.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from app.hermes.gateway import HermesDriver, HermesGateway

log = logging.getLogger("hermes.http_host")


class HermesHTTPHost:
    """Mounts HTTP driver routers and manages their lifecycle."""

    def __init__(self, gateway: HermesGateway) -> None:
        try:
            from fastapi import FastAPI
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "HermesHTTPHost requires fastapi: pip install 'fastapi>=0.110'"
            ) from e

        self._gateway = gateway
        self._drivers: list[HermesDriver] = []

        @asynccontextmanager
        async def lifespan(app: "FastAPI"):
            for driver in self._drivers:
                await driver.start(self._gateway)
            log.info("[http_host] started %d HTTP driver(s)", len(self._drivers))
            try:
                yield
            finally:
                for driver in self._drivers:
                    try:
                        await driver.stop()
                    except Exception as e:  # noqa: BLE001 - best-effort shutdown
                        log.warning("[http_host] stop %s failed: %s", driver.name, e)

        self.app = FastAPI(title="Hermes HTTP Host", lifespan=lifespan)

    def mount(self, driver: HermesDriver) -> None:
        router = getattr(driver, "router", None)
        if not callable(router):
            raise TypeError(f"{driver.name} is not an HTTP driver (no router())")
        self.app.include_router(router())
        self._drivers.append(driver)
        # Ensure the gateway also knows about the driver for push (send) paths.
        self._gateway.register_driver(driver)

    async def serve(self, host: str = "0.0.0.0", port: int = 8080) -> None:  # pragma: no cover
        import uvicorn

        config = uvicorn.Config(self.app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        await server.serve()
