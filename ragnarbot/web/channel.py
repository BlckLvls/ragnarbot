"""Web console channel: bridges browser WebSocket clients to the message bus."""

import asyncio
import json
from typing import Any

from aiohttp import WSMsgType, web
from loguru import logger

from ragnarbot.bus.events import InboundMessage, MediaAttachment, OutboundMessage
from ragnarbot.bus.queue import MessageBus
from ragnarbot.channels.base import BaseChannel

# Events the agent loop emits for streaming-aware channels via metadata["event"].
_PASSTHROUGH_EVENTS = {
    "turn_started",
    "turn_ended",
    "delta",
    "tool_start",
    "tool_end",
    "context_info",
    "session_changed",
    "notification",
    "jobs_update",
}


class WebChannel(BaseChannel):
    """Single-user web channel. All browser tabs share one chat identity."""

    name = "web"

    SENDER_ID = "web"
    DEFAULT_CHAT_ID = "main"

    def __init__(self, config: Any, bus: MessageBus):
        super().__init__(config, bus)
        self._clients: set[web.WebSocketResponse] = set()
        self._stopped: asyncio.Event = asyncio.Event()

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._stopped.clear()
        await self._stopped.wait()

    async def stop(self) -> None:
        self._running = False
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()
        self._stopped.set()

    def is_allowed(self, sender_id: str) -> bool:
        # The web console is bound to localhost/private network by design;
        # anyone who can reach it owns the bot.
        return True

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # ── outbound: bus → browser ──────────────────────────────────

    async def send(self, msg: OutboundMessage) -> None:
        event = self._outbound_to_event(msg)
        if event:
            await self.broadcast(event)

    async def broadcast(self, event: dict[str, Any]) -> None:
        if not self._clients:
            return
        payload = json.dumps(event, ensure_ascii=False)
        for ws in list(self._clients):
            try:
                await ws.send_str(payload)
            except Exception:
                self._clients.discard(ws)

    def _outbound_to_event(self, msg: OutboundMessage) -> dict[str, Any] | None:
        md = msg.metadata or {}

        event_type = md.get("event")
        if event_type in _PASSTHROUGH_EVENTS:
            return {"type": event_type, **md.get("data", {}), "text": msg.content or ""}

        if md.get("chat_action"):
            return {"type": "processing", "value": True}

        # Telegram-formatted panels/confirmations (HTML + inline keyboards) —
        # the web client has native controls for these, drop them.
        if md.get("raw_html"):
            return None

        if not msg.content and not msg.media:
            if md.get("stop_typing"):
                return {"type": "processing", "value": False}
            return None

        message: dict[str, Any] = {
            "role": "assistant",
            "content": msg.content or "",
            "media": msg.media or [],
        }
        if md.get("usage"):
            message["usage"] = md["usage"]
        if md.get("intermediate"):
            return {"type": "intermediate", "message": message, "turn_id": md.get("turn_id")}
        return {"type": "final", "message": message, "turn_id": md.get("turn_id")}

    # ── inbound: browser → bus ───────────────────────────────────

    async def handle_ws(self, request: web.Request, state: dict[str, Any]) -> web.WebSocketResponse:
        """WebSocket endpoint handler. `state` is the snapshot sent on connect."""
        origin = request.headers.get("Origin")
        if origin and request.host and request.host not in origin:
            raise web.HTTPForbidden(reason="cross-origin websocket rejected")

        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._clients.add(ws)
        logger.info(f"Web client connected ({len(self._clients)} online)")

        try:
            await ws.send_str(json.dumps({"type": "state", **state}, ensure_ascii=False))
            async for raw in ws:
                if raw.type == WSMsgType.TEXT:
                    try:
                        await self._handle_client_message(json.loads(raw.data), ws)
                    except Exception as e:
                        logger.warning(f"Web client message failed: {e}")
                        await ws.send_str(json.dumps({"type": "error", "text": str(e)}))
                elif raw.type == WSMsgType.ERROR:
                    break
        finally:
            self._clients.discard(ws)
            logger.info(f"Web client disconnected ({len(self._clients)} online)")
        return ws

    async def _handle_client_message(self, data: dict[str, Any], ws: web.WebSocketResponse) -> None:
        msg_type = data.get("type")

        if msg_type == "ping":
            await ws.send_str(json.dumps({"type": "pong"}))
            return

        if msg_type == "send":
            text = (data.get("text") or "").strip()
            attachments = self._resolve_attachments(data.get("attachment_ids") or [])
            if not text and not attachments:
                return
            metadata: dict[str, Any] = {}
            if data.get("reply_to"):
                metadata["reply_to"] = data["reply_to"]
            await self._handle_message(
                sender_id=self.SENDER_ID,
                chat_id=self.DEFAULT_CHAT_ID,
                content=text,
                attachments=attachments,
                metadata=metadata,
            )
            # Echo to all tabs so every client renders the user message
            await self.broadcast({
                "type": "user_message",
                "message": {
                    "role": "user",
                    "content": text,
                    "attachments": [
                        {"type": a.type, "filename": a.filename} for a in attachments
                    ],
                },
            })
            return

        if msg_type == "stop":
            await self._publish_command("stop")
            return

        if msg_type == "command":
            name = data.get("name", "")
            if not name:
                return
            await self._publish_command(name, data.get("args") or {})
            return

        logger.warning(f"Unknown web client message type: {msg_type}")

    async def _publish_command(self, name: str, args: dict[str, Any] | None = None) -> None:
        await self.bus.publish_inbound(InboundMessage(
            channel=self.name,
            sender_id=self.SENDER_ID,
            chat_id=self.DEFAULT_CHAT_ID,
            content=f"/{name}",
            metadata={"command": name, **(args or {})},
        ))

    def _resolve_attachments(self, attachment_ids: list[str]) -> list[MediaAttachment]:
        """Resolve uploaded attachment IDs to MediaAttachment objects.

        Upload support arrives with the uploads API; until then unknown IDs are dropped.
        """
        resolver = getattr(self, "attachment_resolver", None)
        if not resolver:
            return []
        attachments = []
        for aid in attachment_ids:
            att = resolver(aid)
            if att:
                attachments.append(att)
        return attachments
