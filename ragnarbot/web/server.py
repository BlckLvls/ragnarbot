"""HTTP server for the web console: static SPA, WebSocket, and REST API."""

import json
import re
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

# Channels whose sessions are real user conversations (vs cli/system plumbing)
_USER_CHANNELS = {"web", "telegram"}

# Internal bookkeeping injected into sessions for the LLM's benefit — never shown in chat
_MARKER_PREFIXES = (
    "[System:",
    "[Cron result:",
    "[Cron task:",
    "[Heartbeat check",
    "[Heartbeat report]",
    "[Hook triggered:",
)
_TECH_META_KEYS = ("heartbeat_result", "cron_job_id", "system_note")


def _is_technical_message(m: dict) -> bool:
    """True for marker/injection messages that exist for LLM context only."""
    meta = m.get("metadata") or {}
    if any(meta.get(k) for k in _TECH_META_KEYS):
        return True
    content = m.get("content")
    return isinstance(content, str) and content.startswith(_MARKER_PREFIXES)


def _is_real_user_message(m: dict) -> bool:
    return (
        m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and bool(m["content"].strip())
        and not _is_technical_message(m)
    )


def _tool_arguments_preview(arguments: Any, limit: int = 160) -> str:
    """Render stored tool arguments as a compact, stable one-line summary."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            return arguments.replace("\n", " ")[:limit]
    if not isinstance(arguments, dict):
        return str(arguments).replace("\n", " ")[:limit]
    parts = []
    for key, value in arguments.items():
        rendered = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        parts.append(f"{key}={rendered.replace(chr(10), ' ')}")
    return " · ".join(parts)[:limit]


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

    async def start(self) -> bool:
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
            return False
        logger.info(f"Web console listening on http://{self.host}:{self.port}")
        return True

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
        r.add_post("/api/sessions/active/regenerate", self._handle_session_regenerate)
        r.add_post("/api/sessions/active/fork", self._handle_session_fork)
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
        context = self._web_context_state(session)
        channel = getattr(self, "channel", None)
        live_turn = channel.live_turn_snapshot() if channel is not None else None
        return {
            "session_id": session.key,
            "session_title": _session_title(session.metadata, session.messages),
            "processing": live_turn is not None,
            "live_turn": live_turn,
            "model": agent.model,
            "reasoning_level": agent.reasoning_level,
            "context_mode": agent.context_mode,
            "context_used": context["used_tokens"],
            "context_max": context["max_tokens"],
            "context_compactions": context["compactions"],
            "lightning": agent.lightning_mode,
            "trace": agent.trace_mode,
            "steering": agent.steering_enabled,
            "version": __version__,
        }

    def _web_context_state(self, session: Any | None = None) -> dict[str, int]:
        """Return live context usage for the conversation active in Web Chat."""
        try:
            used = self.agent.get_context_tokens(WEB_USER_KEY, "web", "main")
        except Exception:
            logger.exception("Failed to estimate web context usage")
            used = 0
        if session is None:
            session = self.agent.sessions.get_or_create(WEB_USER_KEY)
        compactions = sum(
            1
            for message in session.messages
            if (message.get("metadata") or {}).get("type") == "compaction"
        )
        return {
            "used_tokens": used,
            "max_tokens": self.agent.max_context_tokens,
            "compactions": compactions,
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
        import tempfile

        path = Path(raw).resolve()
        # tempdir included: media tools may send files the agent generated there
        allowed_roots = [p for p in (
            self.data_dir,
            getattr(self.config, "workspace_path", None),
            tempfile.gettempdir(),
        ) if p]
        if not any(path.is_relative_to(Path(root).resolve()) for root in allowed_roots):
            raise web.HTTPForbidden(reason="path outside allowed roots")
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    # ── REST: sessions ───────────────────────────────────────────

    async def _handle_sessions_list(self, request: web.Request) -> web.Response:
        channel_filter = request.query.get("channel")
        # user mode: the unified chat list — real conversations from any user
        # channel, no heartbeat/cli plumbing, no empty sessions
        user_mode = request.query.get("user") in ("1", "true")
        sessions = self.agent.sessions.list_sessions()
        active_id = self.agent.sessions.get_active_id(WEB_USER_KEY)
        result = []
        for info in sessions:
            user_key = info.get("user_key", "")
            ch = user_key.split(":", 1)[0] if ":" in user_key else "unknown"
            if channel_filter and ch != channel_filter:
                continue
            if user_mode and ch not in _USER_CHANNELS:
                continue
            is_active = info["session_id"] == active_id
            title, has_user = _scan_session_file(Path(info["path"]))
            if user_mode and not has_user and not is_active:
                continue
            result.append({
                "session_id": info["session_id"],
                "channel": ch,
                "user_key": user_key,
                "created_at": info.get("created_at"),
                "updated_at": info.get("updated_at"),
                "title": title,
                "active": is_active,
            })
        return web.json_response(result)

    async def _handle_session_new(self, request: web.Request) -> web.Response:
        session = self.agent.sessions.create_new(WEB_USER_KEY)
        await self.channel.broadcast({
            "type": "session_changed",
            "session_id": session.key,
            **self._web_context_state(session),
        })
        return web.json_response({"session_id": session.key})

    async def _handle_active_messages(self, request: web.Request) -> web.Response:
        session = self.agent.sessions.get_or_create(WEB_USER_KEY)
        return self._messages_response(session, request)

    @staticmethod
    def _strip_message_prefix(content: str) -> str:
        """Drop leading [channel]/[timestamp] prefixes from a stored user message."""
        return re.sub(r"^(?:\[[^\]\n]*\]\s*)+", "", content).strip()

    def _attachments_from_refs(self, refs: list[dict]) -> list[MediaAttachment]:
        """Rebuild photo attachments from a stored message's media refs."""
        attachments: list[MediaAttachment] = []
        for i, ref in enumerate(refs):
            if (ref.get("type") or "photo") != "photo":
                continue
            path = ref.get("path")
            if not path or not Path(path).is_file():
                continue
            try:
                data = Path(path).read_bytes()
            except OSError:
                continue
            attachments.append(MediaAttachment(
                type="photo",
                file_id=f"regen-{i}",
                data=data,
                filename=ref.get("filename") or Path(path).name,
                mime_type=ref.get("mime") or ref.get("mime_type") or "image/jpeg",
            ))
        return attachments

    async def _handle_session_regenerate(self, request: web.Request) -> web.Response:
        """Rewind the active chat to before an assistant reply and re-ask.

        The transcript is truncated to just before the user message that
        produced the reply, then that message is re-dispatched through the
        normal channel flow — so the (possibly different) current model
        answers it again.
        """
        body = await request.json()
        raw_index = body.get("raw_index")
        session = self.agent.sessions.get_or_create(WEB_USER_KEY)
        messages = session.messages
        if (
            not isinstance(raw_index, int)
            or not 0 <= raw_index < len(messages)
            or messages[raw_index].get("role") != "assistant"
        ):
            raise web.HTTPBadRequest(reason="raw_index must point at an assistant message")

        user_idx = None
        for i in range(raw_index - 1, -1, -1):
            m = messages[i]
            if (
                m.get("role") == "user"
                and not _is_technical_message(m)
                and (m.get("metadata") or {}).get("type") != "compaction"
            ):
                user_idx = i
                break
        if user_idx is None:
            raise web.HTTPBadRequest(reason="no user message precedes this reply")

        user_msg = messages[user_idx]
        meta = user_msg.get("metadata") or {}
        text = meta.get("display_content") or self._strip_message_prefix(user_msg.get("content") or "")
        if not text:
            raise web.HTTPBadRequest(reason="original user message is empty")
        attachments = self._attachments_from_refs(user_msg.get("media_refs") or [])

        session.messages = messages[:user_idx]
        self.agent.sessions.save(session)

        await self.channel._handle_message(
            sender_id=self.channel.SENDER_ID,
            chat_id=self.channel.DEFAULT_CHAT_ID,
            content=text,
            attachments=attachments,
            metadata={
                "display_content": text,
                "attachments": [{"type": a.type, "filename": a.filename} for a in attachments],
                "regenerated": True,
            },
        )
        return web.json_response({"ok": True, "truncated_to": user_idx})

    async def _handle_session_fork(self, request: web.Request) -> web.Response:
        """Copy the active chat up to (and including) a message into a new chat."""
        import copy as copy_mod

        body = await request.json()
        raw_index = body.get("raw_index")
        source = self.agent.sessions.get_or_create(WEB_USER_KEY)
        if not isinstance(raw_index, int) or not 0 <= raw_index < len(source.messages):
            raise web.HTTPBadRequest(reason="raw_index out of range")

        fork = self.agent.sessions.create_new(WEB_USER_KEY)
        fork.messages = copy_mod.deepcopy(source.messages[: raw_index + 1])
        source_title = _session_title(source.metadata, source.messages)
        fork.metadata["title"] = f"{source_title} (fork)"[:_TITLE_MAX]
        self.agent.sessions.save(fork)

        await self.channel.broadcast({
            "type": "session_changed",
            "session_id": fork.key,
            **self._web_context_state(fork),
        })
        return web.json_response({"session_id": fork.key})

    async def _handle_session_activate(self, request: web.Request) -> web.Response:
        # Any user conversation can become the web's active chat: activation is
        # just repointing web:main's active pointer. The session keeps its own
        # user_key, so e.g. Telegram keeps writing into the same conversation.
        session = self._get_session_or_404(request)
        ch = session.user_key.split(":", 1)[0]
        if ch not in _USER_CHANNELS:
            raise web.HTTPBadRequest(reason="only user conversations can be activated")
        self.agent.sessions.set_active(WEB_USER_KEY, session.key)
        await self.channel.broadcast({
            "type": "session_changed",
            "session_id": session.key,
            **self._web_context_state(session),
        })
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
        # Deleting a chat must also forget it: purge its recall-index chunks
        # so the agent can no longer surface the deleted conversation.
        index = getattr(self.agent, "index", None)
        if index is not None and hasattr(index, "purge_dialogue"):
            await index.purge_dialogue(session_id)
        return web.json_response({"ok": True})

    def _get_session_or_404(self, request: web.Request):
        session = self.agent.sessions.get_by_id(request.match_info["session_id"])
        if session is None:
            raise web.HTTPNotFound()
        return session

    def _messages_response(self, session, request: web.Request) -> web.Response:
        try:
            limit = max(1, min(1000, int(request.query.get("limit", "200"))))
        except ValueError:
            limit = 200
        before = request.query.get("before")
        messages = self._display_messages(session)
        end = len(messages)
        if before is not None:
            try:
                end = max(0, min(end, int(before)))
            except ValueError:
                pass
        start = max(0, end - limit)
        page = [dict(m, index=start + i) for i, m in enumerate(messages[start:end])]
        return web.json_response({
            "session_id": session.key,
            "title": _session_title(session.metadata, session.messages),
            "total": len(messages),
            "start": start,
            "messages": page,
        })

    def _display_message(self, session, message: dict) -> dict[str, Any]:
        """Convert one stored message into the stable web transcript shape."""
        payload: dict[str, Any] = {
            "role": message["role"],
            "content": message.get("content") or "",
            "metadata": dict(message.get("metadata") or {}),
        }
        for key in ("media_items", "media", "usage", "attachments"):
            if message.get(key):
                payload[key] = message[key]

        refs = []
        for ref in message.get("media_refs") or []:
            path = ref.get("path")
            if not path and ref.get("filename") and self.media_manager is not None:
                path = str(self.media_manager.get_photo_path(session.key, ref["filename"]))
            if path:
                refs.append({
                    "path": path,
                    "mime": ref.get("mime") or ref.get("mime_type"),
                    "kind": ref.get("type") or "photo",
                    "filename": ref.get("filename") or Path(path).name,
                })
        if refs:
            payload["media_refs"] = refs
        return payload

    def _display_messages(self, session) -> list[dict[str, Any]]:
        """Rebuild user-visible turns from the canonical LLM transcript.

        Raw sessions preserve assistant tool-call messages and tool results for
        future model context. The web transcript rebuilds them as a chronological
        segment stream so text, tools, and media never move after reload.
        """
        display: list[dict[str, Any]] = []
        intermediate: list[str] = []
        tools: list[dict[str, Any]] = []
        tool_by_id: dict[str, dict[str, Any]] = {}
        media_events: list[dict[str, Any]] = []
        pending_media: list[dict[str, Any]] = []
        segments: list[dict[str, Any]] = []
        turn_open = False

        def reset_turn() -> None:
            nonlocal intermediate, tools, tool_by_id, media_events, segments, turn_open
            intermediate = []
            tools = []
            tool_by_id = {}
            media_events = []
            segments = []
            turn_open = False

        def append_assistant(message: dict, raw_index: int | None = None) -> None:
            payload = self._display_message(session, message)
            metadata = payload["metadata"]
            if raw_index is not None:
                # Anchor back into the canonical transcript for regenerate/fork.
                metadata["raw_index"] = raw_index
            if segments:
                metadata["segments"] = list(segments)
            # Keep the older grouped fields for API compatibility. New clients
            # prefer `segments`; the final content intentionally stays separate
            # so it always renders below the tool activity card.
            if intermediate:
                metadata["intermediate"] = list(intermediate)
            if tools:
                metadata["tools"] = list(tools)
            if media_events:
                metadata["media_events"] = list(media_events)
            display.append(payload)
            reset_turn()

        for raw_index, message in enumerate(session.messages):
            role = message.get("role")
            if role not in ("user", "assistant", "tool") or _is_technical_message(message):
                continue

            metadata = message.get("metadata") or {}
            if role == "user":
                if metadata.get("type") == "compaction":
                    display.append(self._display_message(session, message))
                    continue
                if not turn_open:
                    reset_turn()
                    media_events.extend(pending_media)
                    segments.extend(
                        {"type": "media", **event} for event in pending_media
                    )
                    pending_media = []
                turn_open = True
                payload = self._display_message(session, message)
                payload["metadata"]["raw_index"] = raw_index
                display.append(payload)
                continue

            if role == "assistant" and message.get("media_items"):
                media_event = {
                    "content": message.get("content") or "",
                    "media_items": message.get("media_items") or [],
                }
                if turn_open:
                    media_events.append(media_event)
                    segments.append({"type": "media", **media_event})
                else:
                    pending_media.append(media_event)
                continue

            if role == "assistant" and message.get("tool_calls"):
                content = (message.get("content") or "").strip()
                if content:
                    intermediate.append(content)
                    segments.append({"type": "text", "content": content})
                for tool_call in message.get("tool_calls") or []:
                    fn = tool_call.get("function") or {}
                    step = {
                        "tool_call_id": tool_call.get("id"),
                        "tool": fn.get("name") or "tool",
                        "args_preview": _tool_arguments_preview(fn.get("arguments") or {}),
                        "status": "running",
                        "done": False,
                    }
                    tools.append(step)
                    if step["tool_call_id"]:
                        tool_by_id[step["tool_call_id"]] = step
                continue

            if role == "tool":
                step = tool_by_id.get(message.get("tool_call_id"))
                if step is not None:
                    result = message.get("content") or ""
                    if isinstance(result, list):
                        # Multimodal tool result (e.g. a browser screenshot):
                        # status comes from its text blocks.
                        result = " ".join(
                            block.get("text", "")
                            for block in result
                            if isinstance(block, dict) and block.get("type") == "text"
                        )
                    step["done"] = True
                    step["status"] = "error" if str(result).startswith("Error") else "ok"
                continue

            # A normal assistant message closes the visible turn.
            if pending_media and not turn_open:
                media_events.extend(pending_media)
                segments.extend(
                    {"type": "media", **event} for event in pending_media
                )
                pending_media = []
            append_assistant(message, raw_index)

        # Preserve incomplete/stopped turns and standalone media sends.
        if turn_open and (intermediate or tools or media_events):
            append_assistant({
                "role": "assistant",
                "content": "",
                "metadata": {"stopped": True},
            })
        for event in pending_media:
            display.append({
                "role": "assistant",
                "content": "",
                "metadata": {
                    "media_events": [event],
                    "segments": [{"type": "media", **event}],
                },
            })

        return display

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
        if _is_real_user_message(m):
            return m["content"].strip()[:_TITLE_MAX]
    return "New chat"


def _scan_session_file(path: Path, max_lines: int = 200) -> tuple[str, bool]:
    """Cheap scan: (title, has real user message) without loading the whole session.

    Title = stored metadata.title or the first genuine user message. Sessions
    holding only technical plumbing (heartbeat/cron markers) report False.
    """
    title = None
    has_user = False
    try:
        with open(path) as f:
            first = f.readline().strip()
            meta = json.loads(first) if first else {}
            title = (meta.get("metadata") or {}).get("title")
            for _ in range(max_lines):
                line = f.readline()
                if not line:
                    break
                try:
                    m = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if _is_real_user_message(m):
                    has_user = True
                    if title is None:
                        title = m["content"].strip()[:_TITLE_MAX]
                    break
    except Exception:
        pass
    return title or "New chat", has_user
