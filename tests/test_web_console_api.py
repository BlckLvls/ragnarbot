"""Focused coverage for the simplified web-console surface."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from ragnarbot.agent.agents_loader import AgentsLoader
from ragnarbot.agent.loop import _system_notification_status
from ragnarbot.bus.events import MediaAttachment
from ragnarbot.bus.queue import MessageBus
from ragnarbot.web.api import ApiRoutes
from ragnarbot.web.channel import WebChannel
from ragnarbot.web.server import WEB_USER_KEY, WebServer, _is_technical_message


class JsonRequest(SimpleNamespace):
    async def json(self):
        return self.body


def make_routes(workspace: Path, *, skills=None, agents=None, cron=None) -> ApiRoutes:
    skills = skills or SimpleNamespace(list_skills=lambda **_: [], builtin_skills=None)
    agents = agents or SimpleNamespace(list_agents=lambda: [], load_agent=lambda _: None)
    agent = SimpleNamespace(
        workspace=workspace,
        context=SimpleNamespace(skills=skills, agents=agents),
        cron_service=cron,
    )
    server = SimpleNamespace(
        agent=agent,
        config=SimpleNamespace(),
        notifications=None,
        heartbeat=None,
    )
    return ApiRoutes(server)


@pytest.mark.parametrize(
    "content",
    [
        "[System: background] command output",
        "[System: background_poll] status",
        "[Cron result: digest | status: ok]",
        "[Heartbeat check | silent]",
        "[Hook triggered: deploy]",
    ],
)
def test_technical_messages_are_hidden_from_chat(content):
    assert _is_technical_message({"role": "assistant", "content": content})


def test_workspace_api_rejects_agent_definitions(tmp_path):
    routes = make_routes(tmp_path)
    with pytest.raises(web.HTTPForbidden, match="agent definitions"):
        routes._workspace_path("agents/researcher/AGENT.md")


def test_workspace_api_rejects_parent_traversal(tmp_path):
    routes = make_routes(tmp_path)
    with pytest.raises(web.HTTPForbidden, match="outside workspace"):
        routes._workspace_path("../secrets.json")


@pytest.mark.asyncio
async def test_workspace_tree_omits_agent_directory(tmp_path):
    (tmp_path / "agents" / "researcher").mkdir(parents=True)
    (tmp_path / "agents" / "researcher" / "AGENT.md").write_text("hidden")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "MEMORY.md").write_text("visible")
    routes = make_routes(tmp_path)

    response = await routes.workspace_tree(SimpleNamespace())
    paths = {entry["path"] for entry in json.loads(response.text)}

    assert "memory/MEMORY.md" in paths
    assert not any(path.startswith("agents") for path in paths)


@pytest.mark.asyncio
async def test_workspace_tree_exposes_safe_text_and_image_files(tmp_path):
    (tmp_path / "skills" / "image").mkdir(parents=True)
    (tmp_path / "skills" / "image" / "SKILL.md").write_text("visible")
    (tmp_path / "skills" / "image" / "helper.py").write_text("print('visible')")
    (tmp_path / "skills" / "image" / "auth.json").write_text("hidden")
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "photo.png").write_bytes(b"binary")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "helper.py").write_text("hidden")
    routes = make_routes(tmp_path)

    response = await routes.workspace_tree(SimpleNamespace())
    entries = json.loads(response.text)
    paths = {entry["path"] for entry in entries}
    kinds = {entry["path"]: entry["kind"] for entry in entries}

    assert "skills/image/SKILL.md" in paths
    assert "skills/image/helper.py" in paths
    assert kinds["skills/image/SKILL.md"] == "text"
    assert kinds["skills/image/helper.py"] == "text"
    assert "skills/image/auth.json" not in paths
    assert kinds["media"] == "directory"
    assert kinds["media/photo.png"] == "image"
    assert not any("__pycache__" in path for path in paths)


@pytest.mark.asyncio
async def test_workspace_tree_preserves_deep_folder_hierarchies(tmp_path):
    deep_file = tmp_path / "one" / "two" / "three" / "four" / "five" / "six" / "seven" / "note.md"
    deep_file.parent.mkdir(parents=True)
    deep_file.write_text("deep workspace")
    routes = make_routes(tmp_path)

    response = await routes.workspace_tree(SimpleNamespace())
    entries = json.loads(response.text)
    paths = {entry["path"] for entry in entries}

    assert "one/two/three/four/five/six/seven" in paths
    assert "one/two/three/four/five/six/seven/note.md" in paths


@pytest.mark.parametrize("path", ["skills/image/auth.json", "archive.zip", ".env"])
def test_workspace_file_api_rejects_unexposed_files(tmp_path, path):
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hidden")
    routes = make_routes(tmp_path)

    with pytest.raises(web.HTTPForbidden, match="not exposed"):
        routes._workspace_path(path, exposed_file=True)


def test_workspace_file_api_does_not_edit_images(tmp_path):
    target = tmp_path / "media" / "photo.png"
    target.parent.mkdir()
    target.write_bytes(b"image")
    routes = make_routes(tmp_path)

    with pytest.raises(web.HTTPUnsupportedMediaType, match="not editable text"):
        routes._workspace_path("media/photo.png", exposed_file=True, editable_file=True)


@pytest.mark.asyncio
async def test_workspace_image_preview_is_inline_and_not_cached(tmp_path):
    target = tmp_path / "media" / "photo.png"
    target.parent.mkdir()
    target.write_bytes(b"image")
    routes = make_routes(tmp_path)

    response = await routes.workspace_file_preview(
        SimpleNamespace(query={"path": "media/photo.png"}),
    )

    assert isinstance(response, web.FileResponse)
    assert response.headers["Content-Disposition"] == 'inline; filename="photo.png"'
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.asyncio
async def test_workspace_image_preview_rejects_text_files(tmp_path):
    (tmp_path / "note.md").write_text("hello")
    routes = make_routes(tmp_path)

    with pytest.raises(web.HTTPUnsupportedMediaType, match="only available for media files"):
        await routes.workspace_file_preview(SimpleNamespace(query={"path": "note.md"}))


@pytest.mark.asyncio
async def test_workspace_tree_and_preview_support_video_files(tmp_path):
    target = tmp_path / "media" / "clip.mp4"
    target.parent.mkdir()
    target.write_bytes(b"video")
    routes = make_routes(tmp_path)

    tree_response = await routes.workspace_tree(SimpleNamespace())
    entries = {entry["path"]: entry for entry in json.loads(tree_response.text)}
    preview_response = await routes.workspace_file_preview(
        SimpleNamespace(query={"path": "media/clip.mp4"}),
    )

    assert entries["media/clip.mp4"]["kind"] == "video"
    assert entries["media/clip.mp4"]["previewable"] is True
    assert preview_response.headers["Content-Disposition"] == 'inline; filename="clip.mp4"'


@pytest.mark.asyncio
async def test_large_media_stays_visible_with_download_fallback(tmp_path):
    target = tmp_path / "media" / "large.mp4"
    target.parent.mkdir()
    with target.open("wb") as handle:
        handle.truncate(101 * 1024 * 1024)
    routes = make_routes(tmp_path)

    tree_response = await routes.workspace_tree(SimpleNamespace())
    entries = {entry["path"]: entry for entry in json.loads(tree_response.text)}
    preview_response = await routes.workspace_file_preview(
        SimpleNamespace(query={"path": "media/large.mp4"}),
    )

    assert entries["media/large.mp4"]["kind"] == "video"
    assert entries["media/large.mp4"]["previewable"] is False
    assert preview_response.status == 413
    assert "100 MB preview limit" in json.loads(preview_response.text)["error"]


@pytest.mark.asyncio
async def test_workspace_download_sets_attachment_header(tmp_path):
    (tmp_path / "note.md").write_text("hello")
    routes = make_routes(tmp_path)

    response = await routes.workspace_file_download(
        SimpleNamespace(query={"path": "note.md"}),
    )

    assert response.headers["Content-Disposition"] == 'attachment; filename="note.md"'


@pytest.mark.asyncio
async def test_skills_endpoint_lists_workspace_only(tmp_path):
    skills = SimpleNamespace(
        list_skills=lambda **_: [
            {"name": "local", "source": "workspace"},
            {"name": "weather", "source": "builtin"},
        ],
        builtin_skills=None,
    )
    routes = make_routes(tmp_path, skills=skills)

    response = await routes.skills_list(SimpleNamespace())

    assert json.loads(response.text) == [{"name": "local", "source": "workspace"}]


@pytest.mark.asyncio
async def test_builtin_skill_cannot_be_overridden_from_web(tmp_path):
    builtin_root = tmp_path / "builtin"
    (builtin_root / "weather").mkdir(parents=True)
    (builtin_root / "weather" / "SKILL.md").write_text("builtin")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skills = SimpleNamespace(list_skills=lambda **_: [], builtin_skills=builtin_root)
    routes = make_routes(workspace, skills=skills)
    request = JsonRequest(
        match_info={"name": "weather"},
        body={"content": "workspace override"},
    )

    response = await routes.skills_put(request)

    assert response.status == 403
    assert not (workspace / "skills" / "weather" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_cron_web_api_only_accepts_enabled_toggle(tmp_path):
    cron = SimpleNamespace(enable_job=lambda *_: None)
    routes = make_routes(tmp_path, cron=cron)
    request = JsonRequest(match_info={"job_id": "job-1"}, body={"name": "edited"})

    response = await routes.cron_update(request)

    assert response.status == 400
    assert "only supports enabling" in json.loads(response.text)["error"]


def test_removed_admin_routes_are_not_registered(tmp_path):
    routes = make_routes(tmp_path)
    app = web.Application()
    routes.register(app.router)
    paths = {resource.canonical for resource in app.router.resources()}

    assert "/api/agents/definitions" in paths
    assert "/api/agents/defs" not in paths
    assert "/api/secrets/reveal" not in paths
    assert "/api/recall/search" not in paths
    assert "/api/heartbeat/trigger" not in paths
    assert "/api/usage" not in paths


@pytest.mark.asyncio
async def test_agent_definitions_are_exposed_read_only_and_parsed(tmp_path):
    workspace = tmp_path / "workspace"
    agent_dir = workspace / "agents" / "reviewer"
    agent_dir.mkdir(parents=True)
    (agent_dir / "AGENT.md").write_text(
        """---
name: reviewer
description: Reviews implementation quality.
model: default
reasoningLevel: high
allowedTools: [file_read, grep]
allowedSkills: none
---

You are a careful reviewer.
""",
        encoding="utf-8",
    )
    builtin = tmp_path / "builtin-agents"
    builtin_agent = builtin / "researcher"
    builtin_agent.mkdir(parents=True)
    (builtin_agent / "AGENT.md").write_text(
        """---
name: researcher
description: Built-in research agent.
---

Research carefully.
""",
        encoding="utf-8",
    )
    loader = AgentsLoader(workspace, builtin_agents_dir=builtin)
    routes = make_routes(workspace, agents=loader)

    response = await routes.agents_definitions(SimpleNamespace())

    assert json.loads(response.text) == [
        {
            "name": "reviewer",
            "description": "Reviews implementation quality.",
            "source": "workspace",
            "path": str(agent_dir / "AGENT.md"),
            "config": {
                "model": "default",
                "reasoning_level": "high",
                "allowed_tools": ["file_read", "grep"],
                "allowed_skills": "none",
            },
            "instructions": "You are a careful reviewer.",
        },
        {
            "name": "researcher",
            "description": "Built-in research agent.",
            "source": "builtin",
            "path": str(builtin_agent / "AGENT.md"),
            "config": {
                "model": "default",
                "reasoning_level": "inherit",
                "allowed_tools": "all",
                "allowed_skills": "none",
            },
            "instructions": "Research carefully.",
        },
    ]


@pytest.mark.asyncio
async def test_update_check_returns_structured_result(tmp_path):
    class UpdateTool:
        async def execute(self, **kwargs):
            assert kwargs == {"action": "check"}
            return json.dumps({
                "current_version": "0.11.2",
                "latest_version": "0.12.0",
                "update_available": True,
                "pending_update": None,
            })

    routes = make_routes(tmp_path)
    routes.agent.tools = {"update": UpdateTool()}

    response = await routes.update_check(SimpleNamespace())

    assert json.loads(response.text) == {
        "current_version": "0.11.2",
        "latest_version": "0.12.0",
        "update_available": True,
        "pending_update": None,
    }


@pytest.mark.asyncio
async def test_web_config_rejects_hidden_settings(tmp_path):
    routes = make_routes(tmp_path)
    request = JsonRequest(
        body={"path": "agents.defaults.model", "value": "openai/gpt-5.5"},
    )

    response = await routes.config_set(request)

    assert response.status == 403
    assert "managed through the agent" in json.loads(response.text)["error"]


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("[Background job finished | exit code: 0]", "ok"),
        ("[Background job finished | exit code: 2]", "error"),
        ("[Sub-agent status: failed]", "error"),
        ("[Gateway ready]", "ok"),
    ],
)
def test_system_notification_status(content, expected):
    assert _system_notification_status(content) == expected


def test_websocket_state_includes_live_context_usage():
    session = SimpleNamespace(
        key="web_main_test",
        metadata={},
        messages=[
            {"role": "user", "content": "summary", "metadata": {"type": "compaction"}},
            {"role": "user", "content": "hello", "metadata": {}},
        ],
    )
    agent = SimpleNamespace(
        sessions=SimpleNamespace(get_or_create=lambda key: session),
        get_context_tokens=lambda key, channel, chat_id: 25_000,
        max_context_tokens=200_000,
        model="anthropic/claude-opus-4-8",
        reasoning_level="high",
        context_mode="normal",
        lightning_mode=False,
        trace_mode=False,
        steering_enabled=True,
    )
    server = object.__new__(WebServer)
    server.agent = agent

    state = server._build_state()

    assert WEB_USER_KEY == "web:main"
    assert state["context_used"] == 25_000
    assert state["context_max"] == 200_000
    assert state["context_compactions"] == 1


def test_web_transcript_rebuilds_tool_turn_without_merging_intermediate_messages(tmp_path):
    media_path = tmp_path / "report.pdf"
    media_path.write_text("report")
    session = SimpleNamespace(
        key="web_main_test",
        metadata={},
        messages=[
            {
                "role": "assistant",
                "content": "Generated report",
                "metadata": {},
                "media_items": [{
                    "path": str(media_path),
                    "kind": "file",
                    "filename": "report.pdf",
                    "size": 6,
                    "mime": "application/pdf",
                }],
            },
            {"role": "user", "content": "Build it", "metadata": {}},
            {
                "role": "assistant",
                "content": "I will inspect the source first.",
                "metadata": {},
                "tool_calls": [{
                    "id": "tc-1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "source.md"}),
                    },
                }],
            },
            {
                "role": "tool",
                "content": "source contents",
                "metadata": {},
                "tool_call_id": "tc-1",
                "name": "read_file",
            },
            {
                "role": "assistant",
                "content": "Now I will write the report.",
                "metadata": {},
                "tool_calls": [{
                    "id": "tc-2",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "report.pdf"}),
                    },
                }],
            },
            {
                "role": "tool",
                "content": "ok",
                "metadata": {},
                "tool_call_id": "tc-2",
                "name": "write_file",
            },
            {
                "role": "assistant",
                "content": "Done.",
                "metadata": {},
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_tokens": 0,
                    "model": "test",
                    "duration_ms": 250,
                },
            },
        ],
    )
    server = object.__new__(WebServer)
    server.media_manager = None

    response = server._messages_response(session, SimpleNamespace(query={"limit": "200"}))
    payload = json.loads(response.text)

    assert payload["total"] == 2
    assert [message["role"] for message in payload["messages"]] == ["user", "assistant"]
    assistant = payload["messages"][1]
    assert assistant["content"] == "Done."
    assert assistant["metadata"]["intermediate"] == [
        "I will inspect the source first.",
        "Now I will write the report.",
    ]
    assert [tool["tool"] for tool in assistant["metadata"]["tools"]] == [
        "read_file",
        "write_file",
    ]
    assert all(tool["done"] for tool in assistant["metadata"]["tools"])
    assert assistant["metadata"]["tools"][0]["args_preview"] == "path=source.md"
    assert assistant["metadata"]["media_events"] == [{
        "content": "Generated report",
        "media_items": [{
            "path": str(media_path),
            "kind": "file",
            "filename": "report.pdf",
            "size": 6,
            "mime": "application/pdf",
        }],
    }]
    assert assistant["usage"]["duration_ms"] == 250


def test_web_transcript_resolves_session_photo_references(tmp_path):
    photo_path = tmp_path / "web_main_test" / "photos" / "photo.png"
    photo_path.parent.mkdir(parents=True)
    photo_path.write_bytes(b"png")
    session = SimpleNamespace(
        key="web_main_test",
        metadata={},
        messages=[{
            "role": "user",
            "content": "look",
            "metadata": {},
            "media_refs": [{"type": "photo", "filename": "photo.png"}],
        }],
    )
    server = object.__new__(WebServer)
    server.media_manager = SimpleNamespace(
        get_photo_path=lambda session_key, filename: tmp_path / session_key / "photos" / filename,
    )

    messages = server._display_messages(session)

    assert messages[0]["media_refs"] == [{
        "path": str(photo_path),
        "mime": None,
        "kind": "photo",
        "filename": "photo.png",
    }]


def test_web_transcript_keeps_content_above_tools_and_final_below(tmp_path):
    media_path = tmp_path / "result.png"
    media_path.write_bytes(b"png")
    session = SimpleNamespace(
        key="web_main_test",
        metadata={},
        messages=[
            {"role": "user", "content": "Run it", "metadata": {}},
            {
                "role": "assistant",
                "content": "I will check first.",
                "metadata": {},
                "tool_calls": [{
                    "id": "tc-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            },
            {
                "role": "tool",
                "content": "ok",
                "metadata": {},
                "tool_call_id": "tc-1",
                "name": "read_file",
            },
            {
                "role": "assistant",
                "content": "Preview caption",
                "metadata": {},
                "media_items": [{
                    "path": str(media_path),
                    "kind": "photo",
                    "filename": "result.png",
                    "size": 3,
                    "mime": "image/png",
                }],
            },
            {"role": "assistant", "content": "Final answer.", "metadata": {}},
        ],
    )
    server = object.__new__(WebServer)
    server.media_manager = None

    messages = server._display_messages(session)
    assistant = messages[-1]

    assert assistant["metadata"]["segments"] == [
        {"type": "text", "content": "I will check first."},
        {
            "type": "media",
            "content": "Preview caption",
            "media_items": [{
                "path": str(media_path),
                "kind": "photo",
                "filename": "result.png",
                "size": 3,
                "mime": "image/png",
            }],
        },
    ]
    assert assistant["metadata"]["tools"][0]["tool"] == "read_file"
    assert assistant["content"] == "Final answer."


@pytest.mark.asyncio
async def test_web_file_upload_keeps_display_text_and_adds_agent_download_marker():
    bus = MessageBus()
    channel = WebChannel(SimpleNamespace(), bus)
    channel.attachment_resolver = lambda upload_id: MediaAttachment(
        type="file",
        file_id=upload_id,
        filename="brief.pdf",
        mime_type="application/pdf",
    )

    await channel._handle_client_message({
        "type": "send",
        "text": "Review this",
        "attachment_ids": ["upload-1"],
    }, SimpleNamespace())

    message = await bus.consume_inbound()
    assert message.content == (
        "Review this\n[file available: brief.pdf (file_id: upload-1)]"
    )
    assert message.metadata["display_content"] == "Review this"
    assert message.metadata["attachments"] == [{
        "type": "file",
        "filename": "brief.pdf",
    }]


def test_web_transcript_handles_multimodal_tool_results():
    """Tool results with list content (e.g. screenshots) must not crash the transcript."""
    session = SimpleNamespace(
        key="web_main_test",
        metadata={},
        messages=[
            {"role": "user", "content": "screenshot something", "metadata": {}},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "t1", "function": {"name": "browser", "arguments": {}}}],
                "metadata": {},
            },
            {
                "role": "tool",
                "tool_call_id": "t1",
                "content": [
                    {"type": "text", "text": "Screenshot captured"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
                ],
                "metadata": {},
            },
            {"role": "assistant", "content": "Вот скриншот.", "metadata": {}},
        ],
    )
    server = object.__new__(WebServer)
    server.media_manager = None

    messages = server._display_messages(session)

    final = messages[-1]
    assert final["content"] == "Вот скриншот."
    tools = final["metadata"]["tools"]
    assert tools[0]["done"] is True and tools[0]["status"] == "ok"


def test_live_turn_snapshot_replays_in_flight_progress():
    """Clients that reconnect mid-turn get the accumulated tool timeline back."""
    channel = WebChannel(SimpleNamespace(), MessageBus())
    track = channel._track_live_turn
    track({"type": "turn_started", "turn_id": "t1"})
    track({"type": "delta", "text": "думаю... "})
    track({"type": "tool_start", "tool": "web_search", "args_preview": "q=news"})
    track({"type": "tool_end", "tool": "web_search", "status": "ok", "duration_ms": 1200})
    track({"type": "delta", "text": "нашла: "})

    snap = channel.live_turn_snapshot()
    assert snap["turn_id"] == "t1"
    assert snap["tools"] == [{
        "turn_id": None, "tool": "web_search", "args_preview": "q=news",
        "done": True, "status": "ok", "duration_ms": 1200,
    }]
    assert snap["segments"] == [{"type": "text", "content": "думаю... "}]
    assert snap["current_text"] == "нашла: "

    track({"type": "final"})
    assert channel.live_turn_snapshot() is None


def test_live_turn_snapshot_includes_pending_user_messages():
    """User messages persist server-side only at end of turn — the snapshot carries them."""
    channel = WebChannel(SimpleNamespace(), MessageBus())
    track = channel._track_live_turn
    track({"type": "user_message", "message": {"role": "user", "content": "вопрос"}})
    snap = channel.live_turn_snapshot()
    assert snap["user_messages"] == [{"role": "user", "content": "вопрос"}]
    assert snap["turn_id"] is None  # debounce window: sent but not started

    track({"type": "turn_started", "turn_id": "t1"})
    snap = channel.live_turn_snapshot()
    assert snap["turn_id"] == "t1"
    assert snap["user_messages"] == [{"role": "user", "content": "вопрос"}]

    track({"type": "turn_ended"})
    assert channel.live_turn_snapshot() is None
