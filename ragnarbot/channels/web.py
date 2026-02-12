"""Web channel — browser-based chat via WebSocket."""

import base64
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
        self._connections: dict[str, WebSocketResponse] = {}
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._uploads: dict[str, dict] = {}  # file_id → {path, filename, mime_type, size}
        self._msg_counter: int = 0

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
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

        self._app = web.Application(client_max_size=MAX_UPLOAD_SIZE)
        self._app.router.add_get("/", self._handle_index)
        self._app.router.add_get("/ws", self._handle_websocket)
        self._app.router.add_post("/upload", self._handle_upload)

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

        # Context data → send as context_info WS message
        context_data = msg.metadata.get("context_data")
        if context_data:
            await self._ws_send(ws, {"type": "context_info", **context_data})
            # If message is just a context command response, don't send the HTML text
            if msg.metadata.get("raw_html"):
                return

        is_intermediate = msg.metadata.get("intermediate", False)

        # Reaction → send as reaction WS message
        reaction = msg.metadata.get("reaction")
        if reaction:
            await self._ws_send(ws, {
                "type": "reaction",
                "emoji": reaction,
                "msg_id": msg.metadata.get("target_message_id"),
            })
            return

        # Media (photo/video/document from bot) → inline in chat
        media_type = msg.metadata.get("media_type")
        media_path = msg.metadata.get("media_path")
        if media_type and media_path:
            p = Path(media_path)
            if p.is_file():
                mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
                if media_type == "photo" or (mime and mime.startswith("image/")):
                    # Inline image as data URL (snapshots are typically small)
                    b64 = base64.b64encode(p.read_bytes()).decode()
                    data_url = f"data:{mime};base64,{b64}"
                    payload: dict[str, Any] = {
                        "type": "message",
                        "content": msg.content or "",
                        "media": {"type": "image", "url": data_url, "filename": p.name},
                    }
                else:
                    # Non-image file — tell the user where it is
                    payload = {
                        "type": "message",
                        "content": (msg.content or "") + f"\n\n[file saved: {p}]",
                    }
                await self._ws_send(ws, payload)
                if not msg.metadata.get("keep_typing"):
                    await self._ws_send(ws, {"type": "typing", "active": False})
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
                    if not chat_id:
                        continue

                    # Assign message ID for reaction targeting
                    self._msg_counter += 1
                    msg_id = self._msg_counter
                    metadata: dict[str, Any] = {"message_id": msg_id}

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
                            # Voice: load bytes for transcription pipeline
                            attachments.append(MediaAttachment(
                                type="voice",
                                file_id=file_id,
                                data=path.read_bytes(),
                                filename=fname,
                                mime_type=mime or "audio/webm",
                            ))
                            content_parts.append(f"[voice: {fname}]")
                        elif mime.startswith("image/"):
                            # Images: load bytes (small enough for vision)
                            attachments.append(MediaAttachment(
                                type="photo",
                                file_id=file_id,
                                data=path.read_bytes(),
                                filename=fname,
                                mime_type=mime,
                            ))
                            content_parts.append(f"[image: {fname}]")
                        else:
                            # Files: don't load into memory — pass path
                            # Agent loop will save to session dir
                            attachments.append(MediaAttachment(
                                type="file",
                                file_id=str(path),  # abuse file_id to pass path
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
                        sender_id=chat_id,
                        chat_id=chat_id,
                        content=final_content,
                        attachments=attachments or None,
                        metadata=metadata,
                    )

                    # Send msg_id back to client for reaction targeting
                    await self._ws_send(ws, {"type": "msg_id", "msg_id": msg_id})

                elif msg_type == "command":
                    command = data.get("command")
                    if not chat_id:
                        continue

                    if command == "new_chat":
                        # Remove old mapping; client will re-hello with a new chat_id.
                        self._connections.pop(chat_id, None)
                        chat_id = None

                    elif command == "context_info":
                        await self._handle_message(
                            sender_id=chat_id,
                            chat_id=chat_id,
                            content="",
                            metadata={"command": "context_info"},
                        )

                    elif command == "set_context_mode":
                        mode = data.get("mode", "normal")
                        await self._handle_message(
                            sender_id=chat_id,
                            chat_id=chat_id,
                            content="",
                            metadata={
                                "command": "set_context_mode",
                                "context_mode": mode,
                            },
                        )

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
