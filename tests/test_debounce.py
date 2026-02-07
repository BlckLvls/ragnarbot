"""Tests for the message debounce mechanism."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ragnarbot.bus.events import InboundMessage
from ragnarbot.bus.queue import MessageBus


def _make_msg(channel="telegram", chat_id="123", content="hello", **meta):
    return InboundMessage(
        channel=channel,
        sender_id="user1",
        chat_id=chat_id,
        content=content,
        metadata=meta,
    )


def _make_agent(bus, debounce_seconds=0.3, tmp_path=None):
    """Create a minimal AgentLoop with mocked provider."""
    from ragnarbot.agent.loop import AgentLoop

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    workspace = tmp_path if tmp_path else Path("/tmp/ragnarbot_test_ws")
    if not tmp_path:
        workspace.mkdir(parents=True, exist_ok=True)

    with patch("ragnarbot.agent.loop.SessionManager"), \
         patch("ragnarbot.agent.loop.SubagentManager"):
        agent = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=workspace,
            debounce_seconds=debounce_seconds,
        )
    return agent


class TestDebounce:
    """Tests for AgentLoop._debounce()."""

    @pytest.mark.asyncio
    async def test_single_message_passes_through(self):
        """A single message with no follow-up returns a batch of one."""
        bus = MessageBus()
        agent = _make_agent(bus, debounce_seconds=0.05)

        msg = _make_msg(content="only one")
        batch = await agent._debounce(msg)

        assert len(batch) == 1
        assert batch[0].content == "only one"

    @pytest.mark.asyncio
    async def test_same_session_messages_batched(self):
        """Rapid messages from the same session are collected into one batch."""
        bus = MessageBus()
        agent = _make_agent(bus, debounce_seconds=0.1)

        first = _make_msg(content="msg1")
        # Pre-load the bus with two more messages from the same session
        await bus.publish_inbound(_make_msg(content="msg2"))
        await bus.publish_inbound(_make_msg(content="msg3"))

        batch = await agent._debounce(first)

        assert len(batch) == 3
        assert [m.content for m in batch] == ["msg1", "msg2", "msg3"]

    @pytest.mark.asyncio
    async def test_different_session_breaks_debounce(self):
        """A message from a different session stops debouncing and is re-published."""
        bus = MessageBus()
        agent = _make_agent(bus, debounce_seconds=0.1)

        first = _make_msg(content="msg1", chat_id="123")
        # Second message from same session
        await bus.publish_inbound(_make_msg(content="msg2", chat_id="123"))
        # Third message from DIFFERENT session
        await bus.publish_inbound(_make_msg(content="other", chat_id="999"))

        batch = await agent._debounce(first)

        # Batch should only contain same-session messages
        assert len(batch) == 2
        assert [m.content for m in batch] == ["msg1", "msg2"]

        # The different-session message should be back on the bus
        assert bus.inbound_size == 1
        remaining = await bus.consume_inbound()
        assert remaining.chat_id == "999"
        assert remaining.content == "other"

    @pytest.mark.asyncio
    async def test_debounce_disabled_when_zero(self):
        """debounce_seconds=0 disables debouncing — returns single-item batch."""
        bus = MessageBus()
        agent = _make_agent(bus, debounce_seconds=0)

        # Pre-load extra messages
        await bus.publish_inbound(_make_msg(content="msg2"))

        first = _make_msg(content="msg1")
        batch = await agent._debounce(first)

        assert len(batch) == 1
        assert batch[0].content == "msg1"

        # Extra message remains on the bus
        assert bus.inbound_size == 1

    @pytest.mark.asyncio
    async def test_debounce_disabled_when_negative(self):
        """Negative debounce_seconds also disables debouncing."""
        bus = MessageBus()
        agent = _make_agent(bus, debounce_seconds=-1)

        first = _make_msg(content="only")
        batch = await agent._debounce(first)

        assert len(batch) == 1


class TestProcessBatchContext:
    """Tests that _process_batch builds correct LLM context for batches."""

    @pytest.mark.asyncio
    async def test_batch_produces_multiple_user_messages(self, tmp_path):
        """A batch of N messages produces N user messages in the LLM context."""
        bus = MessageBus()
        agent = _make_agent(bus, debounce_seconds=0, tmp_path=tmp_path)

        # Mock session
        mock_session = MagicMock()
        mock_session.key = "telegram_123_20260207_abc123"
        mock_session.user_key = "telegram:123"
        mock_session.get_history.return_value = []
        mock_session.metadata = {}
        agent.sessions.get_or_create.return_value = mock_session

        # Track messages sent to LLM
        captured_messages = []

        async def mock_chat(messages, tools, model):
            captured_messages.extend(messages)
            resp = MagicMock()
            resp.has_tool_calls = False
            resp.content = "batch response"
            resp.tool_calls = []
            return resp

        agent.provider.chat = mock_chat

        batch = [
            _make_msg(content="first message"),
            _make_msg(content="second message"),
            _make_msg(content="third message"),
        ]

        response = await agent._process_batch(batch)

        assert response is not None
        assert response.content == "batch response"

        # Count user messages in the LLM context
        user_msgs = [m for m in captured_messages if m.get("role") == "user"]
        assert len(user_msgs) == 3

    @pytest.mark.asyncio
    async def test_only_first_message_has_timestamp_prefix(self, tmp_path):
        """Only the first user message in a batch gets a timestamp prefix."""
        bus = MessageBus()
        agent = _make_agent(bus, debounce_seconds=0, tmp_path=tmp_path)

        mock_session = MagicMock()
        mock_session.key = "telegram_123_20260207_abc123"
        mock_session.user_key = "telegram:123"
        mock_session.get_history.return_value = []
        mock_session.metadata = {}
        agent.sessions.get_or_create.return_value = mock_session

        captured_messages = []

        async def mock_chat(messages, tools, model):
            captured_messages.extend(messages)
            resp = MagicMock()
            resp.has_tool_calls = False
            resp.content = "ok"
            resp.tool_calls = []
            return resp

        agent.provider.chat = mock_chat

        batch = [
            _make_msg(content="msg A", message_id="100"),
            _make_msg(content="msg B", message_id="101"),
        ]

        await agent._process_batch(batch)

        user_msgs = [m for m in captured_messages if m.get("role") == "user"]
        assert len(user_msgs) == 2

        content_a = user_msgs[0]["content"]
        content_b = user_msgs[1]["content"]

        # Content is either a string or a list (multimodal). Extract text.
        text_a = content_a if isinstance(content_a, str) else next(
            p["text"] for p in content_a if isinstance(p, dict) and p.get("type") == "text"
        )
        text_b = content_b if isinstance(content_b, str) else next(
            p["text"] for p in content_b if isinstance(p, dict) and p.get("type") == "text"
        )

        # First message gets timestamp prefix (no msgID)
        assert "[" in text_a  # has a tag bracket
        assert "msg A" in text_a
        assert "msgID:" not in text_a
        # Second message has no timestamp prefix
        assert "msg B" in text_b
        assert "[" not in text_b.split("msg B")[0]  # no tag before content

    @pytest.mark.asyncio
    async def test_single_message_batch_saves_session(self, tmp_path):
        """A single-message batch correctly saves to session."""
        bus = MessageBus()
        agent = _make_agent(bus, debounce_seconds=0, tmp_path=tmp_path)

        mock_session = MagicMock()
        mock_session.key = "telegram_123_20260207_abc123"
        mock_session.user_key = "telegram:123"
        mock_session.get_history.return_value = []
        mock_session.metadata = {}
        agent.sessions.get_or_create.return_value = mock_session

        async def mock_chat(messages, tools, model):
            resp = MagicMock()
            resp.has_tool_calls = False
            resp.content = "reply"
            resp.tool_calls = []
            return resp

        agent.provider.chat = mock_chat

        batch = [_make_msg(content="hello")]
        await agent._process_batch(batch)

        # Session should have add_message called: 1 user + 1 assistant = 2
        assert mock_session.add_message.call_count == 2
        agent.sessions.save.assert_called_once_with(mock_session)

    @pytest.mark.asyncio
    async def test_multi_message_batch_saves_all_user_messages(self, tmp_path):
        """A multi-message batch saves each user message to session."""
        bus = MessageBus()
        agent = _make_agent(bus, debounce_seconds=0, tmp_path=tmp_path)

        mock_session = MagicMock()
        mock_session.key = "telegram_123_20260207_abc123"
        mock_session.user_key = "telegram:123"
        mock_session.get_history.return_value = []
        mock_session.metadata = {}
        agent.sessions.get_or_create.return_value = mock_session

        async def mock_chat(messages, tools, model):
            resp = MagicMock()
            resp.has_tool_calls = False
            resp.content = "reply"
            resp.tool_calls = []
            return resp

        agent.provider.chat = mock_chat

        batch = [
            _make_msg(content="msg1"),
            _make_msg(content="msg2"),
            _make_msg(content="msg3"),
        ]
        await agent._process_batch(batch)

        # 3 user messages + 1 assistant = 4 calls
        assert mock_session.add_message.call_count == 4

        # First 3 calls should be user messages with original content
        user_calls = mock_session.add_message.call_args_list[:3]
        for i, call in enumerate(user_calls):
            assert call[0][0] == "user"  # role
            assert call[0][1] == f"msg{i + 1}"  # original content

        # Last call should be assistant
        assistant_call = mock_session.add_message.call_args_list[3]
        assert assistant_call[0][0] == "assistant"
