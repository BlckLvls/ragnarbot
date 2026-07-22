"""Coverage for the chat reply-action endpoints: regenerate and fork."""

import json
from types import SimpleNamespace

import pytest
from aiohttp import web

from ragnarbot.session.manager import Session
from ragnarbot.web.server import WebServer


class JsonRequest(SimpleNamespace):
    async def json(self):
        return self.body


class FakeChannel:
    """Records the dispatch/broadcast side effects the endpoints trigger."""

    SENDER_ID = "web"
    DEFAULT_CHAT_ID = "main"

    def __init__(self):
        self.dispatched = []
        self.broadcasts = []

    async def _handle_message(self, *, sender_id, chat_id, content, attachments, metadata):
        self.dispatched.append({
            "sender_id": sender_id,
            "chat_id": chat_id,
            "content": content,
            "attachments": attachments,
            "metadata": metadata,
        })

    async def broadcast(self, event):
        self.broadcasts.append(event)


class FakeSessions:
    """Minimal SessionManager stand-in over a single active session."""

    def __init__(self, active):
        self.active = active
        self.saved = []
        self.created = []
        self._counter = 0

    def get_or_create(self, user_key):
        return self.active

    def create_new(self, user_key):
        self._counter += 1
        session = Session(key=f"web:fork-{self._counter}", user_key=user_key)
        self.created.append(session)
        return session

    def save(self, session):
        self.saved.append(session)


def make_server(messages, metadata=None):
    """Build a WebServer with fake channel/agent wired to `messages`."""
    session = Session(
        key="web:main",
        user_key="web:main",
        messages=messages,
        metadata=metadata or {},
    )
    sessions = FakeSessions(session)
    agent = SimpleNamespace(
        sessions=sessions,
        get_context_tokens=lambda user_key, channel, chat_id: 1234,
        max_context_tokens=200_000,
    )
    channel = FakeChannel()
    server = object.__new__(WebServer)
    server.agent = agent
    server.channel = channel
    server.media_manager = None
    return server, session, sessions, channel


# ── regenerate ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_regenerate_truncates_to_previous_user_and_redispatches():
    """Regenerate rewinds before the user turn and re-asks it through the channel."""
    messages = [
        {"role": "user", "content": "first question", "metadata": {}},
        {"role": "assistant", "content": "first answer", "metadata": {}},
        {"role": "user", "content": "second question", "metadata": {}},
        {"role": "assistant", "content": "second answer", "metadata": {}},
    ]
    server, session, sessions, channel = make_server(messages)

    response = await server._handle_session_regenerate(
        JsonRequest(body={"raw_index": 3}),
    )

    assert json.loads(response.text) == {"ok": True, "truncated_to": 2}
    # Transcript truncated to just before the second user message.
    assert [m["content"] for m in session.messages] == ["first question", "first answer"]
    assert session in sessions.saved
    # The original user message is re-dispatched for a fresh reply.
    assert len(channel.dispatched) == 1
    dispatched = channel.dispatched[0]
    assert dispatched["content"] == "second question"
    assert dispatched["sender_id"] == "web"
    assert dispatched["chat_id"] == "main"
    assert dispatched["metadata"]["regenerated"] is True
    assert dispatched["metadata"]["display_content"] == "second question"


@pytest.mark.asyncio
async def test_regenerate_uses_display_content_when_present():
    """Regenerate prefers metadata.display_content over the stored raw content."""
    messages = [
        {
            "role": "user",
            "content": "[web] hello",
            "metadata": {"display_content": "hello there"},
        },
        {"role": "assistant", "content": "hi", "metadata": {}},
    ]
    server, session, sessions, channel = make_server(messages)

    await server._handle_session_regenerate(JsonRequest(body={"raw_index": 1}))

    assert channel.dispatched[0]["content"] == "hello there"


@pytest.mark.asyncio
async def test_regenerate_rejects_non_assistant_index():
    """A raw_index pointing at a user message is a 400."""
    messages = [
        {"role": "user", "content": "hi", "metadata": {}},
        {"role": "assistant", "content": "yo", "metadata": {}},
    ]
    server, *_ = make_server(messages)

    with pytest.raises(web.HTTPBadRequest, match="assistant message"):
        await server._handle_session_regenerate(JsonRequest(body={"raw_index": 0}))


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_index", [99, -1])
async def test_regenerate_rejects_out_of_range_index(raw_index):
    """A raw_index outside the transcript bounds is a 400."""
    messages = [{"role": "assistant", "content": "yo", "metadata": {}}]
    server, *_ = make_server(messages)

    with pytest.raises(web.HTTPBadRequest):
        await server._handle_session_regenerate(JsonRequest(body={"raw_index": raw_index}))


@pytest.mark.asyncio
async def test_regenerate_rejects_when_no_preceding_user_message():
    """Only technical/marker messages precede the reply, so there is nothing to re-ask."""
    messages = [
        {"role": "user", "content": "[System: boot]", "metadata": {}},
        {"role": "assistant", "content": "ready", "metadata": {}},
    ]
    server, *_ = make_server(messages)

    with pytest.raises(web.HTTPBadRequest, match="no user message"):
        await server._handle_session_regenerate(JsonRequest(body={"raw_index": 1}))


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"raw_index": "1"}, {}])
async def test_regenerate_rejects_invalid_index_type(body):
    """A non-int (string) or missing raw_index is a 400."""
    messages = [
        {"role": "user", "content": "hi", "metadata": {}},
        {"role": "assistant", "content": "yo", "metadata": {}},
    ]
    server, *_ = make_server(messages)

    with pytest.raises(web.HTTPBadRequest):
        await server._handle_session_regenerate(JsonRequest(body=body))


@pytest.mark.asyncio
async def test_regenerate_does_not_dispatch_on_bad_request():
    """A rejected regenerate leaves the transcript and channel untouched."""
    messages = [
        {"role": "user", "content": "keep me", "metadata": {}},
        {"role": "assistant", "content": "kept", "metadata": {}},
    ]
    server, session, sessions, channel = make_server(messages)

    with pytest.raises(web.HTTPBadRequest):
        await server._handle_session_regenerate(JsonRequest(body={"raw_index": 0}))

    assert len(session.messages) == 2
    assert channel.dispatched == []
    assert sessions.saved == []


# ── fork ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fork_copies_slice_into_new_session():
    """Fork deep-copies messages[:raw_index+1] into a new titled session, leaving the source intact."""
    messages = [
        {"role": "user", "content": "q1", "metadata": {}},
        {"role": "assistant", "content": "a1", "metadata": {}},
        {"role": "user", "content": "q2", "metadata": {}},
        {"role": "assistant", "content": "a2", "metadata": {}},
    ]
    server, source, sessions, channel = make_server(messages)

    response = await server._handle_session_fork(JsonRequest(body={"raw_index": 2}))

    fork = sessions.created[0]
    payload = json.loads(response.text)
    assert payload == {"session_id": fork.key}
    # Slice is messages[:raw_index+1] — the first three turns.
    assert [m["content"] for m in fork.messages] == ["q1", "a1", "q2"]
    assert fork.metadata["title"] == "q1 (fork)"
    assert fork in sessions.saved
    # The copy is deep: mutating the fork must not touch the source.
    assert fork.messages[0] is not source.messages[0]
    fork.messages[0]["content"] = "mutated"
    assert source.messages[0]["content"] == "q1"
    assert len(source.messages) == 4
    # A session_changed broadcast points clients at the new fork.
    assert len(channel.broadcasts) == 1
    assert channel.broadcasts[0]["type"] == "session_changed"
    assert channel.broadcasts[0]["session_id"] == fork.key


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_index", [99, -1, "1", None])
async def test_fork_rejects_invalid_index(raw_index):
    """An out-of-range, negative, or non-int raw_index is a 400 and creates nothing."""
    messages = [
        {"role": "user", "content": "q1", "metadata": {}},
        {"role": "assistant", "content": "a1", "metadata": {}},
    ]
    server, source, sessions, channel = make_server(messages)

    with pytest.raises(web.HTTPBadRequest, match="out of range"):
        await server._handle_session_fork(JsonRequest(body={"raw_index": raw_index}))

    assert sessions.created == []
    assert sessions.saved == []
    assert channel.broadcasts == []
    assert len(source.messages) == 2
