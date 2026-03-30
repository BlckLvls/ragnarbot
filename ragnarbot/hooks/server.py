"""HTTP server for receiving hook triggers."""

import asyncio
import time

from aiohttp import web
from loguru import logger

from ragnarbot.hooks.service import HookService


class HookServer:
    """Lightweight aiohttp server exposing POST /hooks/{hook_id} for triggers."""

    def __init__(
        self,
        service: HookService,
        host: str = "0.0.0.0",
        port: int = 18791,
        max_payload_bytes: int = 65536,
        rate_limit_per_hook: int = 60,
    ):
        self.service = service
        self.host = host
        self.port = port
        self.max_payload_bytes = max_payload_bytes
        self.rate_limit_per_hook = rate_limit_per_hook

        self._rate_windows: dict[str, list[float]] = {}
        self._runner: web.AppRunner | None = None

        self.app = web.Application()
        self.app.router.add_get("/hooks/health", self._handle_health)
        self.app.router.add_post("/hooks/{hook_id}", self._handle_trigger)

    # ========== Lifecycle ==========

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        try:
            await site.start()
        except OSError as e:
            await self._runner.cleanup()
            self._runner = None
            logger.error(
                f"Hook server failed to bind {self.host}:{self.port}: {e}. "
                f"Change hooks.port in config if another profile uses this port."
            )
            return
        logger.info(f"Hook server listening on {self.host}:{self.port}")

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            logger.info("Hook server stopped")

    # ========== Handlers ==========

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _handle_trigger(self, request: web.Request) -> web.Response:
        hook_id = request.match_info["hook_id"]

        # Look up hook by ID (which is also the secret)
        hook = self.service.get_hook(hook_id)
        if hook is None or not hook.enabled:
            return web.json_response(
                {"error": "not found"}, status=404,
            )

        # Rate limiting (sliding window, 1 minute)
        now = time.time()
        window = self._rate_windows.setdefault(hook_id, [])
        cutoff = now - 60.0
        window[:] = [t for t in window if t > cutoff]
        if len(window) >= self.rate_limit_per_hook:
            return web.json_response(
                {"error": "rate limit exceeded"}, status=429,
            )
        window.append(now)

        # Read payload with size limit
        try:
            payload = await request.text()
            if len(payload.encode("utf-8")) > self.max_payload_bytes:
                return web.json_response(
                    {"error": "payload too large"}, status=413,
                )
        except Exception:
            payload = ""

        # Dispatch trigger asynchronously — respond 202 immediately
        self.service.increment_trigger_count(hook_id)
        asyncio.create_task(self._dispatch_trigger(hook, payload))

        return web.json_response(
            {"status": "accepted", "hook": hook.name}, status=202,
        )

    async def _dispatch_trigger(self, hook, payload: str) -> None:
        """Run the trigger callback in the background."""
        try:
            if self.service.on_trigger:
                await self.service.on_trigger(hook, payload)
        except Exception as e:
            logger.error(f"Hook trigger failed for '{hook.name}': {e}")
