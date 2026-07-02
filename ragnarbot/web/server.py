"""HTTP server for the web console: static SPA, WebSocket, and REST API."""

import json
from pathlib import Path
from typing import Any

from aiohttp import web
from loguru import logger

from ragnarbot import __version__
from ragnarbot.web.channel import WebChannel

STATIC_DIR = Path(__file__).parent / "static"

WEB_USER_KEY = f"{WebChannel.name}:{WebChannel.DEFAULT_CHAT_ID}"

_TITLE_MAX = 60


class WebServer:
    """aiohttp server embedded in the gateway process."""

    def __init__(
        self,
        config: Any,
        channel: WebChannel,
        agent: Any,
        host: str = "127.0.0.1",
        port: int = 18792,
    ):
        self.config = config
        self.channel = channel
        self.agent = agent
        self.host = host
        self.port = port
        self._runner: web.AppRunner | None = None

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
        r = self.app.router
        r.add_get("/ws", self._handle_ws)
        r.add_get("/api/status", self._handle_status)

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
