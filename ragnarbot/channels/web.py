"""Web channel — browser-based chat via WebSocket."""

import base64
import ipaddress
import json
import mimetypes
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.web import WebSocketResponse
from loguru import logger

from ragnarbot.bus.events import MediaAttachment, OutboundMessage
from ragnarbot.bus.queue import MessageBus
from ragnarbot.channels.base import BaseChannel
from ragnarbot.config.schema import WebConfig

STATIC_DIR = Path(__file__).parent / "web_static"

# Upload dir — persistent, not temp
UPLOAD_DIR = Path.home() / ".ragnarbot" / "uploads"

# Max upload: 500 MB (files stay on disk, not sent to LLM)
MAX_UPLOAD_SIZE = 500 * 1024 * 1024


class WebChannel(BaseChannel):
    """Chat channel served over HTTP + WebSocket."""

    name = "web"

    def __init__(self, config: WebConfig, bus: MessageBus):
        super().__init__(config, bus)
        self._connections: dict[str, WebSocketResponse] = {}  # device_id → ws
        self._device_session: dict[str, str] = {}  # device_id → session_id
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._uploads: dict[str, dict] = {}  # file_id → {path, filename, mime_type, size}
        self._msg_counter: int = 0
        self._allowed_nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
            ipaddress.ip_network(n, strict=False) for n in config.allowed_networks
        ] if config.allowed_networks else []

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def is_allowed(self, sender_id: str) -> bool:
        # Localhost — no auth required.
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _make_ip_filter(self):
        nets = self._allowed_nets

        @web.middleware
        async def ip_filter(request: web.Request, handler):
            peer = request.remote
            try:
                addr = ipaddress.ip_address(peer)
            except ValueError:
                raise web.HTTPForbidden(text="forbidden")
            if not any(addr in net for net in nets):
                logger.warning("Blocked request from {} (not in allowed_networks)", peer)
                raise web.HTTPForbidden(text="forbidden")
            return await handler(request)

        return ip_filter

    async def start(self) -> None:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

        middlewares = [self._make_ip_filter()] if self._allowed_nets else []
        self._app = web.Application(client_max_size=MAX_UPLOAD_SIZE, middlewares=middlewares)
        self._app.router.add_get("/", self._handle_index)
        self._app.router.add_get("/ws", self._handle_websocket)
        self._app.router.add_post("/upload", self._handle_upload)
        self._app.router.add_static("/static", STATIC_DIR, show_index=False)

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
        if not self._connections:
            return

        device_id = msg.chat_id  # chat_id = device_id in web channel
        ws = self._connections.get(device_id)

        # Update device→session mapping from agent metadata (fallback for new sessions)
        session_id = msg.metadata.get("_session_id")
        if session_id and device_id in self._connections:
            old_session = self._device_session.get(device_id)
            self._device_session[device_id] = session_id
            # Notify client when server assigns/changes session_id
            if old_session != session_id and ws and not ws.closed:
                await self._ws_send(ws, {"type": "session_update", "session_id": session_id})

        # ── Global broadcast (all devices) ───────────────────────

        # Stop typing (e.g. after processing cancelled)
        if msg.metadata.get("chat_action") == "typing_stop":
            await self._broadcast({"type": "typing", "active": False})
            return

        # Processing stopped
        if msg.metadata.get("stopped"):
            await self._broadcast({"type": "stopped"})
            await self._broadcast({"type": "typing", "active": False})
            return

        # ── Session-aware broadcast (devices on same session) ────

        # Typing indicator
        if msg.metadata.get("chat_action") == "typing":
            await self._send_to_session_peers(device_id, {"type": "typing", "active": True})
            return

        # ── Per-client responses (only to requesting device) ─────

        # Session list
        session_list = msg.metadata.get("session_list")
        if session_list is not None:
            if ws and not ws.closed:
                await self._ws_send(ws, {"type": "session_list", "sessions": session_list})
            return

        # Session resumed
        session_resumed = msg.metadata.get("session_resumed")
        if session_resumed:
            if ws and not ws.closed:
                await self._ws_send(ws, {"type": "session_resumed", **session_resumed})
            return

        # Session deleted
        session_deleted = msg.metadata.get("session_deleted")
        if session_deleted:
            if ws and not ws.closed:
                await self._ws_send(ws, {"type": "session_deleted", **session_deleted})
            return

        # Context data
        context_data = msg.metadata.get("context_data")
        if context_data:
            if ws and not ws.closed:
                await self._ws_send(ws, {"type": "context_info", **context_data})
            if msg.metadata.get("raw_html"):
                return

        # raw_html without context_data (e.g. "New chat started") → per-client
        if msg.metadata.get("raw_html"):
            if ws and not ws.closed:
                await self._ws_send(ws, {"type": "message", "content": msg.content})
                await self._ws_send(ws, {"type": "typing", "active": False})
            return

        # ── Session-aware content (devices on same session) ──────

        is_intermediate = msg.metadata.get("intermediate", False)

        # Reaction
        reaction = msg.metadata.get("reaction")
        if reaction:
            await self._send_to_session_peers(device_id, {
                "type": "reaction",
                "emoji": reaction,
                "msg_id": msg.metadata.get("target_message_id"),
            })
            return

        # Media
        media_type = msg.metadata.get("media_type")
        media_path = msg.metadata.get("media_path")
        if media_type and media_path:
            p = Path(media_path)
            if p.is_file():
                mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
                if media_type == "photo" or (mime and mime.startswith("image/")):
                    b64 = base64.b64encode(p.read_bytes()).decode()
                    data_url = f"data:{mime};base64,{b64}"
                    payload: dict[str, Any] = {
                        "type": "message",
                        "content": msg.content or "",
                        "media": {"type": "image", "url": data_url, "filename": p.name},
                    }
                else:
                    payload = {
                        "type": "message",
                        "content": (msg.content or "") + f"\n\n[file saved: {p}]",
                    }
                await self._send_to_session_peers(device_id, payload)
                if not msg.metadata.get("keep_typing"):
                    await self._send_to_session_peers(device_id, {"type": "typing", "active": False})
            return

        # Regular message
        payload: dict[str, Any] = {
            "type": "message",
            "content": msg.content,
        }
        if is_intermediate:
            payload["intermediate"] = True

        await self._send_to_session_peers(device_id, payload)

        if not is_intermediate and not msg.metadata.get("keep_typing"):
            await self._send_to_session_peers(device_id, {"type": "typing", "active": False})

    # ------------------------------------------------------------------
    # HTTP handlers
    # ------------------------------------------------------------------

    async def _handle_index(self, _request: web.Request) -> web.Response:
        html = (STATIC_DIR / "index.html").read_text()
        html = html.replace("{{title}}", self.config.title)
        return web.Response(
            text=html,
            content_type="text/html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    async def _handle_upload(self, request: web.Request) -> web.Response:
        """Accept file upload via multipart/form-data, return file_id."""
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "file":
            return web.json_response({"error": "no file field"}, status=400)

        filename = field.filename or "upload"
        mime = (
            field.headers.get("Content-Type", "")
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream"
        )

        # Save to persistent upload dir (not temp)
        file_id = str(uuid.uuid4())
        safe_name = f"{file_id[:8]}_{filename}"
        dest = UPLOAD_DIR / safe_name

        size = 0
        with open(dest, "wb") as f:
            while True:
                chunk = await field.read_chunk(65536)
                if not chunk:
                    break
                size += len(chunk)
                f.write(chunk)

        self._uploads[file_id] = {
            "path": dest,
            "filename": filename,
            "mime_type": mime,
            "size": size,
        }

        logger.debug("Upload saved: {} ({}, {} bytes) -> {}", filename, file_id[:8], size, dest)

        return web.json_response({
            "file_id": file_id,
            "filename": filename,
            "mime_type": mime,
            "size": size,
        })

    async def _handle_websocket(self, request: web.Request) -> WebSocketResponse:
        ws = WebSocketResponse()
        await ws.prepare(request)

        device_id: str | None = None

        async for raw in ws:
            if raw.type.name == "TEXT":
                try:
                    data = json.loads(raw.data)
                except json.JSONDecodeError:
                    await self._ws_send(ws, {"type": "error", "content": "Invalid JSON"})
                    continue

                msg_type = data.get("type")

                if msg_type == "hello":
                    device_id = data.get("device_id", "")
                    session_id = data.get("session_id")  # None on first visit
                    if device_id:
                        self._connections[device_id] = ws
                        if session_id:
                            self._device_session[device_id] = session_id
                        await self._ws_send(ws, {"type": "hello", "device_id": device_id})
                        logger.debug(
                            "Web client connected: dev={} sess={}",
                            device_id[:8],
                            session_id[:8] if session_id else "none",
                        )

                elif msg_type == "message":
                    content = data.get("content", "").strip()
                    if not device_id:
                        continue

                    # Assign message ID for reaction targeting
                    self._msg_counter += 1
                    msg_id = self._msg_counter

                    cur_session = self._device_session.get(device_id)
                    metadata: dict[str, Any] = {"message_id": msg_id}
                    if cur_session:
                        metadata["explicit_session_id"] = cur_session

                    # Build attachments from uploaded files
                    attachments: list[MediaAttachment] = []
                    content_parts: list[str] = []
                    if content:
                        content_parts.append(content)

                    for att in data.get("attachments", []):
                        file_id = att.get("file_id", "")
                        info = self._uploads.pop(file_id, None)
                        if not info:
                            continue

                        path: Path = info["path"]
                        if not path.exists():
                            continue

                        att_type = att.get("type", "file")
                        mime = info["mime_type"]
                        fname = info["filename"]

                        if att_type == "voice":
                            attachments.append(MediaAttachment(
                                type="voice",
                                file_id=file_id,
                                data=path.read_bytes(),
                                filename=fname,
                                mime_type=mime or "audio/webm",
                            ))
                            content_parts.append(f"[voice: {fname}]")
                        elif mime.startswith("image/"):
                            attachments.append(MediaAttachment(
                                type="photo",
                                file_id=file_id,
                                data=path.read_bytes(),
                                filename=fname,
                                mime_type=mime,
                            ))
                            content_parts.append(f"[image: {fname}]")
                        else:
                            attachments.append(MediaAttachment(
                                type="file",
                                file_id=str(path),
                                data=None,
                                filename=fname,
                                mime_type=mime,
                            ))
                            size_mb = info["size"] / (1024 * 1024)
                            content_parts.append(
                                f"[file: {fname}, {size_mb:.1f} MB, saved to {path}]"
                            )

                    final_content = "\n".join(content_parts) if content_parts else "[attachment]"

                    await self._handle_message(
                        sender_id=device_id,
                        chat_id=device_id,
                        content=final_content,
                        attachments=attachments or None,
                        metadata=metadata,
                    )

                    # Send msg_id back to client for reaction targeting
                    await self._ws_send(ws, {"type": "msg_id", "msg_id": msg_id})

                    # Echo user message to OTHER devices on the SAME session
                    if cur_session:
                        echo = {"type": "user_echo", "content": content or "[attachment]"}
                        for other_dev, other_ws in list(self._connections.items()):
                            if other_dev != device_id and not other_ws.closed:
                                if self._device_session.get(other_dev) == cur_session:
                                    try:
                                        await other_ws.send_json(echo)
                                    except Exception:
                                        pass

                elif msg_type == "command":
                    command = data.get("command")
                    if not device_id:
                        continue

                    cur_session = self._device_session.get(device_id)

                    if command == "new_chat":
                        # Session will be assigned by server → session_update sent back
                        self._device_session.pop(device_id, None)
                        await self._handle_message(
                            sender_id=device_id,
                            chat_id=device_id,
                            content="",
                            metadata={"command": "new_chat"},
                        )

                    elif command == "context_info":
                        await self._handle_message(
                            sender_id=device_id,
                            chat_id=device_id,
                            content="",
                            metadata={"command": "context_info"},
                        )

                    elif command == "set_context_mode":
                        mode = data.get("mode", "normal")
                        await self._handle_message(
                            sender_id=device_id,
                            chat_id=device_id,
                            content="",
                            metadata={"command": "set_context_mode", "context_mode": mode},
                        )

                    elif command == "list_sessions":
                        await self._handle_message(
                            sender_id=device_id,
                            chat_id=device_id,
                            content="",
                            metadata={
                                "command": "list_sessions",
                                "current_session_id": cur_session,
                            },
                        )

                    elif command == "resume_session":
                        new_sid = data.get("session_id", "")
                        if new_sid:
                            self._device_session[device_id] = new_sid
                        await self._handle_message(
                            sender_id=device_id,
                            chat_id=device_id,
                            content="",
                            metadata={"command": "resume_session", "session_id": new_sid},
                        )

                    elif command == "delete_session":
                        del_sid = data.get("session_id", "")
                        await self._handle_message(
                            sender_id=device_id,
                            chat_id=device_id,
                            content="",
                            metadata={"command": "delete_session", "session_id": del_sid},
                        )

                    elif command == "stop":
                        await self._handle_message(
                            sender_id=device_id,
                            chat_id=device_id,
                            content="",
                            metadata={"command": "stop"},
                        )

            elif raw.type.name == "ERROR":
                logger.warning("WS error: {}", ws.exception())
                break

        # Cleanup on disconnect.
        if device_id:
            self._connections.pop(device_id, None)
            self._device_session.pop(device_id, None)
            logger.debug("Web client disconnected: {}", device_id[:8])

        return ws

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _ws_send(ws: WebSocketResponse, data: dict) -> None:
        if not ws.closed:
            await ws.send_json(data)

    async def _broadcast(self, data: dict) -> None:
        """Send to all connected web clients."""
        for ws in list(self._connections.values()):
            if not ws.closed:
                try:
                    await ws.send_json(data)
                except Exception:
                    pass

    async def _send_to_session_peers(self, device_id: str, data: dict) -> None:
        """Send to all devices viewing the same session as device_id."""
        my_session = self._device_session.get(device_id)
        if not my_session:
            # Fallback: send only to the requesting device
            ws = self._connections.get(device_id)
            if ws and not ws.closed:
                try:
                    await ws.send_json(data)
                except Exception:
                    pass
            return
        for dev, ws in list(self._connections.items()):
            if self._device_session.get(dev) == my_session and not ws.closed:
                try:
                    await ws.send_json(data)
                except Exception:
                    pass
