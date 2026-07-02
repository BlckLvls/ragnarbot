"""HTTP server for the web console: static SPA, WebSocket, and REST API."""

import json
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web
from loguru import logger

from ragnarbot import __version__
from ragnarbot.bus.events import MediaAttachment
from ragnarbot.web.channel import WebChannel

STATIC_DIR = Path(__file__).parent / "static"

WEB_USER_KEY = f"{WebChannel.name}:{WebChannel.DEFAULT_CHAT_ID}"

_TITLE_MAX = 60

_IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


class UploadStore:
    """Disk-backed store for browser uploads, resolvable as message attachments."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save(self, filename: str, data: bytes, mime_type: str) -> dict[str, Any]:
        upload_id = uuid.uuid4().hex[:12]
        safe_name = Path(filename or "file").name or "file"
        path = self.base_dir / f"{upload_id}_{safe_name}"
        path.write_bytes(data)
        kind = "photo" if mime_type in _IMAGE_MIMES else "file"
        return {
            "id": upload_id,
            "filename": safe_name,
            "size": len(data),
            "kind": kind,
            "mime_type": mime_type,
        }

    def _find(self, upload_id: str) -> Path | None:
        if not upload_id.isalnum():
            return None
        matches = list(self.base_dir.glob(f"{upload_id}_*"))
        return matches[0] if matches else None

    def resolve(self, upload_id: str) -> MediaAttachment | None:
        """Resolve an upload id to a MediaAttachment for the inbound pipeline."""
        path = self._find(upload_id)
        if path is None:
            return None
        import mimetypes
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        filename = path.name.split("_", 1)[1] if "_" in path.name else path.name
        if mime in _IMAGE_MIMES:
            return MediaAttachment(
                type="photo", file_id=upload_id, data=path.read_bytes(),
                filename=filename, mime_type=mime,
            )
        return MediaAttachment(
            type="file", file_id=upload_id, filename=filename, mime_type=mime,
        )

    def read(self, upload_id: str) -> tuple[bytes, str] | None:
        """Download-callback contract for MediaManager: (bytes, filename)."""
        path = self._find(upload_id)
        if path is None:
            return None
        filename = path.name.split("_", 1)[1] if "_" in path.name else path.name
        return path.read_bytes(), filename


class WebServer:
    """aiohttp server embedded in the gateway process."""

    def __init__(
        self,
        config: Any,
        channel: WebChannel,
        agent: Any,
        host: str = "127.0.0.1",
        port: int = 18792,
        media_manager: Any = None,
        data_dir: Path | None = None,
        heartbeat: Any = None,
        notifications: Any = None,
    ):
        self.config = config
        self.channel = channel
        self.agent = agent
        self.host = host
        self.port = port
        self.media_manager = media_manager
        self.data_dir = data_dir
        self.heartbeat = heartbeat
        self.notifications = notifications
        self._runner: web.AppRunner | None = None

        self.uploads = UploadStore((data_dir or Path(".")) / "web" / "uploads")
        channel.attachment_resolver = self.uploads.resolve
        if media_manager is not None:
            async def _download(file_id: str):
                result = self.uploads.read(file_id)
                if result is None:
                    raise FileNotFoundError(f"upload {file_id} not found")
                return result
            media_manager.register_download_callback("web", _download)

        self.app = web.Application(client_max_size=64 * 1024 * 1024)
        self._setup_routes()

    # ── lifecycle ────────────────────────────────────────────────

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
                f"Web console failed to bind {self.host}:{self.port}: {e}. "
                f"Change web.port in config if the port is taken."
            )
            return
        logger.info(f"Web console listening on http://{self.host}:{self.port}")

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            logger.info("Web console stopped")

    # ── routes ───────────────────────────────────────────────────

    def _setup_routes(self) -> None:
        from ragnarbot.web.api import ApiRoutes

        r = self.app.router
        r.add_get("/ws", self._handle_ws)
        r.add_get("/api/status", self._handle_status)

        ApiRoutes(self).register(r)

        r.add_post("/api/uploads", self._handle_upload)
        r.add_post("/api/transcribe", self._handle_transcribe)
        r.add_get("/api/media", self._handle_media)

        r.add_get("/api/sessions", self._handle_sessions_list)
        r.add_post("/api/sessions/new", self._handle_session_new)
        r.add_get("/api/sessions/active/messages", self._handle_active_messages)
        r.add_get("/api/sessions/{session_id}/messages", self._handle_session_messages)
        r.add_post("/api/sessions/{session_id}/activate", self._handle_session_activate)
        r.add_patch("/api/sessions/{session_id}", self._handle_session_patch)
        r.add_delete("/api/sessions/{session_id}", self._handle_session_delete)

        if STATIC_DIR.is_dir():
            r.add_get("/", self._handle_index)
            if (STATIC_DIR / "assets").is_dir():
                r.add_static("/assets", STATIC_DIR / "assets")
            r.add_get("/{tail:(?!api/|ws$).*}", self._handle_index)
        else:
            r.add_get("/", self._handle_not_built)

    # ── websocket ────────────────────────────────────────────────

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        return await self.channel.handle_ws(request, state=self._build_state())

    def _build_state(self) -> dict[str, Any]:
        agent = self.agent
        session = agent.sessions.get_or_create(WEB_USER_KEY)
        return {
            "session_id": session.key,
            "session_title": _session_title(session.metadata, session.messages),
            "model": agent.model,
            "reasoning_level": agent.reasoning_level,
            "context_mode": agent.context_mode,
            "lightning": agent.lightning_mode,
            "trace": agent.trace_mode,
            "steering": agent.steering_enabled,
            "version": __version__,
        }

    # ── REST: status ─────────────────────────────────────────────

    async def _handle_status(self, request: web.Request) -> web.Response:
        return web.json_response({
            "version": __version__,
            "model": self.agent.model,
            "web_clients": self.channel.client_count,
        })

    # ── REST: uploads / voice / media ────────────────────────────

    async def _handle_upload(self, request: web.Request) -> web.Response:
        reader = await request.multipart()
        results = []
        async for part in reader:
            if part.name != "file":
                continue
            data = await part.read(decode=False)
            if not data:
                continue
            results.append(self.uploads.save(
                part.filename or "file",
                bytes(data),
                part.headers.get("Content-Type", "application/octet-stream"),
            ))
        if not results:
            raise web.HTTPBadRequest(reason="no files in request")
        return web.json_response(results)

    async def _handle_transcribe(self, request: web.Request) -> web.Response:
        provider_name = self.config.transcription.provider
        if provider_name == "none":
            return web.json_response(
                {"error": "transcription is not configured"}, status=400,
            )

        reader = await request.multipart()
        audio: bytes | None = None
        filename = "voice.webm"
        async for part in reader:
            if part.name in ("file", "audio"):
                audio = bytes(await part.read(decode=False))
                filename = part.filename or filename
                break
        if not audio:
            raise web.HTTPBadRequest(reason="no audio in request")

        import tempfile

        from ragnarbot.auth.credentials import load_credentials
        from ragnarbot.providers.transcription import (
            TranscriptionError,
            create_transcription_provider,
        )

        transcriber = create_transcription_provider(provider_name, load_credentials())
        if transcriber is None:
            return web.json_response(
                {"error": "transcription provider unavailable"}, status=400,
            )

        suffix = Path(filename).suffix or ".webm"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio)
            tmp_path = Path(tmp.name)
        try:
            text = await transcriber.transcribe(str(tmp_path))
            return web.json_response({"text": text})
        except TranscriptionError as e:
            return web.json_response({"error": e.short_message}, status=502)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=502)
        finally:
            tmp_path.unlink(missing_ok=True)

    async def _handle_media(self, request: web.Request) -> web.FileResponse:
        """Serve a media file by absolute path, restricted to bot-owned dirs."""
        raw = request.query.get("path", "")
        if not raw:
            raise web.HTTPBadRequest(reason="path required")
        path = Path(raw).resolve()
        allowed_roots = [p for p in (
            self.data_dir,
            getattr(self.config, "workspace_path", None),
        ) if p]
        if not any(path.is_relative_to(Path(root).resolve()) for root in allowed_roots):
            raise web.HTTPForbidden(reason="path outside allowed roots")
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    # ── REST: sessions ───────────────────────────────────────────

    async def _handle_sessions_list(self, request: web.Request) -> web.Response:
        channel_filter = request.query.get("channel")
        sessions = self.agent.sessions.list_sessions()
        active_id = self.agent.sessions.get_active_id(WEB_USER_KEY)
        result = []
        for info in sessions:
            user_key = info.get("user_key", "")
            ch = user_key.split(":", 1)[0] if ":" in user_key else "unknown"
            if channel_filter and ch != channel_filter:
                continue
            result.append({
                "session_id": info["session_id"],
                "channel": ch,
                "user_key": user_key,
                "created_at": info.get("created_at"),
                "updated_at": info.get("updated_at"),
                "title": _title_from_file(Path(info["path"])),
                "active": info["session_id"] == active_id,
            })
        return web.json_response(result)

    async def _handle_session_new(self, request: web.Request) -> web.Response:
        session = self.agent.sessions.create_new(WEB_USER_KEY)
        await self.channel.broadcast({"type": "session_changed", "session_id": session.key})
        return web.json_response({"session_id": session.key})

    async def _handle_active_messages(self, request: web.Request) -> web.Response:
        session = self.agent.sessions.get_or_create(WEB_USER_KEY)
        return self._messages_response(session, request)

    async def _handle_session_messages(self, request: web.Request) -> web.Response:
        session = self._get_session_or_404(request)
        return self._messages_response(session, request)

    async def _handle_session_activate(self, request: web.Request) -> web.Response:
        session = self._get_session_or_404(request)
        if session.user_key != WEB_USER_KEY:
            raise web.HTTPBadRequest(reason="only web sessions can be activated")
        self.agent.sessions.set_active(WEB_USER_KEY, session.key)
        await self.channel.broadcast({"type": "session_changed", "session_id": session.key})
        return web.json_response({"ok": True})

    async def _handle_session_patch(self, request: web.Request) -> web.Response:
        session = self._get_session_or_404(request)
        body = await request.json()
        title = (body.get("title") or "").strip()
        if not title:
            raise web.HTTPBadRequest(reason="title required")
        session.metadata["title"] = title[:_TITLE_MAX]
        self.agent.sessions.save(session)
        return web.json_response({"ok": True})

    async def _handle_session_delete(self, request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        deleted = self.agent.sessions.delete(session_id)
        if not deleted:
            raise web.HTTPNotFound()
        return web.json_response({"ok": True})

    def _get_session_or_404(self, request: web.Request):
        session = self.agent.sessions.get_by_id(request.match_info["session_id"])
        if session is None:
            raise web.HTTPNotFound()
        return session

    def _messages_response(self, session, request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", "200"))
        before = request.query.get("before")
        messages = [
            m for m in session.messages
            if m.get("role") in ("user", "assistant")
        ]
        end = len(messages)
        if before is not None:
            end = max(0, min(end, int(before)))
        start = max(0, end - limit)
        page = [
            {
                "index": start + i,
                "role": m["role"],
                "content": m.get("content") or "",
                "metadata": m.get("metadata", {}),
                "media_refs": m.get("media_refs", []),
            }
            for i, m in enumerate(messages[start:end])
        ]
        return web.json_response({
            "session_id": session.key,
            "title": _session_title(session.metadata, session.messages),
            "total": len(messages),
            "start": start,
            "messages": page,
        })

    # ── static SPA ───────────────────────────────────────────────

    async def _handle_index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def _handle_not_built(self, request: web.Request) -> web.Response:
        return web.Response(
            text="ragnarbot web console: frontend not built (ragnarbot/web/static missing)",
            content_type="text/plain",
        )


def _session_title(metadata: dict, messages: list[dict]) -> str:
    title = (metadata or {}).get("title")
    if title:
        return title
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), str) and m["content"].strip():
            return m["content"].strip()[:_TITLE_MAX]
    return "New chat"


def _title_from_file(path: Path) -> str:
    """Cheap title read: metadata line + first user message, without loading everything."""
    try:
        with open(path) as f:
            first = f.readline().strip()
            meta = json.loads(first) if first else {}
            title = (meta.get("metadata") or {}).get("title")
            if title:
                return title
            for _ in range(50):
                line = f.readline()
                if not line:
                    break
                try:
                    m = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if m.get("role") == "user" and isinstance(m.get("content"), str) and m["content"].strip():
                    return m["content"].strip()[:_TITLE_MAX]
    except Exception:
        pass
    return "New chat"
