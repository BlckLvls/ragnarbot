"""Web channel — browser-based chat via WebSocket."""

import json
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.web import WebSocketResponse
from loguru import logger

from ragnarbot.bus.events import OutboundMessage
from ragnarbot.bus.queue import MessageBus
from ragnarbot.channels.base import BaseChannel
from ragnarbot.config.schema import WebConfig

STATIC_DIR = Path(__file__).parent / "web_static"


class WebChannel(BaseChannel):
    """Chat channel served over HTTP + WebSocket."""

    name = "web"

    def __init__(self, config: WebConfig, bus: MessageBus):
        super().__init__(config, bus)
        self._connections: dict[str, WebSocketResponse] = {}
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def is_allowed(self, sender_id: str) -> bool:
        # Localhost — no auth required.
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._app = web.Application()
        self._app.router.add_get("/", self._handle_index)
        self._app.router.add_get("/ws", self._handle_websocket)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        site = web.TCPSite(
            self._runner,
            self.config.host,
            self.config.port,
        )
        await site.start()
        self._running = True
        logger.info(
            "Web channel listening on http://{}:{}",
            self.config.host,
            self.config.port,
        )

        # Block forever so ChannelManager.start_all keeps this task alive.
        import asyncio
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        self._running = False

        # Close every open WS connection.
        for ws in list(self._connections.values()):
            await ws.close()
        self._connections.clear()

        if self._runner:
            await self._runner.cleanup()
            self._runner = None

        logger.info("Web channel stopped")

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send(self, msg: OutboundMessage) -> None:
        ws = self._connections.get(msg.chat_id)
        if not ws or ws.closed:
            return

        # Typing indicator
        if msg.metadata.get("chat_action") == "typing":
            await self._ws_send(ws, {"type": "typing", "active": True})
            return

        is_intermediate = msg.metadata.get("intermediate", False)

        # Skip web-irrelevant metadata silently.
        if msg.metadata.get("reaction"):
            return
        if msg.metadata.get("media_type"):
            return

        # Send the message.
        payload: dict[str, Any] = {
            "type": "message",
            "content": msg.content,
        }
        if is_intermediate:
            payload["intermediate"] = True

        await self._ws_send(ws, payload)

        # Final message → stop typing.
        if not is_intermediate and not msg.metadata.get("keep_typing"):
            await self._ws_send(ws, {"type": "typing", "active": False})

    # ------------------------------------------------------------------
    # HTTP handlers
    # ------------------------------------------------------------------

    async def _handle_index(self, _request: web.Request) -> web.Response:
        html = (STATIC_DIR / "index.html").read_text()
        html = html.replace("{{title}}", self.config.title)
        return web.Response(text=html, content_type="text/html")

    async def _handle_websocket(self, request: web.Request) -> WebSocketResponse:
        ws = WebSocketResponse()
        await ws.prepare(request)

        chat_id: str | None = None

        async for raw in ws:
            if raw.type.name == "TEXT":
                try:
                    data = json.loads(raw.data)
                except json.JSONDecodeError:
                    await self._ws_send(ws, {"type": "error", "content": "Invalid JSON"})
                    continue

                msg_type = data.get("type")

                if msg_type == "hello":
                    chat_id = data.get("chat_id", "")
                    if chat_id:
                        self._connections[chat_id] = ws
                        await self._ws_send(ws, {"type": "hello", "chat_id": chat_id})
                        logger.debug("Web client connected: {}", chat_id[:8])

                elif msg_type == "message":
                    content = data.get("content", "").strip()
                    if chat_id and content:
                        await self._handle_message(
                            sender_id=chat_id,
                            chat_id=chat_id,
                            content=content,
                        )

                elif msg_type == "command":
                    command = data.get("command")
                    if chat_id and command == "new_chat":
                        # Remove old mapping; client will re-hello with a new chat_id.
                        self._connections.pop(chat_id, None)
                        chat_id = None

            elif raw.type.name == "ERROR":
                logger.warning("WS error: {}", ws.exception())
                break

        # Cleanup on disconnect.
        if chat_id:
            self._connections.pop(chat_id, None)
            logger.debug("Web client disconnected: {}", chat_id[:8])

        return ws

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _ws_send(ws: WebSocketResponse, data: dict) -> None:
        if not ws.closed:
            await ws.send_json(data)
