"""Tests for steering injection and stop cleanup behavior."""

import asyncio
import contextlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ragnarbot.bus.events import InboundMessage
from ragnarbot.channels.telegram import BOT_COMMANDS
from ragnarbot.providers.base import LLMResponse, ToolCallRequest
from ragnarbot.session.manager import Session


def _make_msg(channel="telegram", chat_id="123", content="hello", **meta):
    return InboundMessage(
        channel=channel,
        sender_id="user1",
        chat_id=chat_id,
        content=content,
        metadata=meta,
    )


def _make_agent(tmp_path: Path, steering_enabled: bool = True):
    """Create a minimal AgentLoop with mocked collaborators."""
    from ragnarbot.agent.loop import AgentLoop

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    with patch("ragnarbot.agent.loop.SubagentManager"):
        agent = AgentLoop(
            bus=MagicMock(),
            provider=provider,
            workspace=tmp_path,
            debounce_seconds=0,
            steering_enabled=steering_enabled,
        )

    agent.bus.publish_outbound = AsyncMock()
    agent.bus.publish_inbound = AsyncMock()
    agent.sessions = MagicMock()
    return agent


@pytest.mark.asyncio
async def test_process_batch_injects_pending_steering(tmp_path):
    """Queued steering is injected before the next LLM call in the same run."""
    agent = _make_agent(tmp_path)
    session = MagicMock(spec=Session)
    session.key = "telegram_123_20260314_abc123"
    session.user_key = "telegram:123"
    session.get_history.return_value = []
    session.metadata = {}
    agent.sessions.get_or_create.return_value = session

    llm_calls: list[list[dict]] = []
    responses = [
        LLMResponse(
            content="working",
            tool_calls=[ToolCallRequest(id="tc1", name="exec", arguments={"command": "echo hi"})],
            finish_reason="tool_calls",
        ),
        LLMResponse(content="done"),
    ]

    async def fake_chat_with_fallback(session_key, **kwargs):
        llm_calls.append(kwargs["messages"])
        return responses[len(llm_calls) - 1], False, None

    async def fake_execute(session_key, tool_name, arguments):
        queued = agent._queue_steering_message(
            _make_msg(content="change direction", message_id="2"),
        )
        assert queued is True
        return "tool ok"

    agent._chat_with_fallback = fake_chat_with_fallback
    agent._execute_tool_with_tracking = fake_execute

    state = agent._start_run_state("telegram:123")
    try:
        response = await agent._process_batch([_make_msg(content="start", message_id="1")])
    finally:
        await agent._finish_run_state("telegram:123")

    assert response is not None
    assert response.content == "done"
    assert len(llm_calls) == 2
    second_call_user_messages = [m for m in llm_calls[1] if m.get("role") == "user"]
    steering_text = second_call_user_messages[-1]["content"]
    assert "[Steering message during active task]" in steering_text
    assert "change direction" in steering_text

    steering_save = next(
        call for call in session.add_message.call_args_list
        if call.args[0] == "user" and call.args[1] == "change direction"
    )
    assert steering_save.kwargs["msg_metadata"]["type"] == "steering"
    assert state.injected_steering


@pytest.mark.asyncio
async def test_queue_steering_message_respects_global_toggle(tmp_path):
    """Disabled steering leaves same-session messages as normal next turns."""
    agent = _make_agent(tmp_path, steering_enabled=False)
    agent._start_run_state("telegram:123")
    try:
        queued = agent._queue_steering_message(_make_msg(content="later"))
        assert queued is False
        assert not agent._run_state.pending_steering
    finally:
        await agent._finish_run_state("telegram:123")


@pytest.mark.asyncio
async def test_request_stop_cancels_active_tool_task(tmp_path):
    """Stop immediately cancels the current foreground tool task."""
    agent = _make_agent(tmp_path)
    session_key = "telegram:123"
    state = agent._start_run_state(session_key)
    agent._processing_session_key = session_key
    agent._processing_task = asyncio.create_task(asyncio.sleep(60))

    state.active_tool_task = asyncio.create_task(asyncio.sleep(60))
    try:
        assert agent._request_stop(session_key) is True
        await asyncio.sleep(0)
        assert state.stop_event.is_set() is True
        assert state.active_tool_task.cancelled() is True
    finally:
        agent._processing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await agent._processing_task
        await agent._finish_run_state(session_key)


@pytest.mark.asyncio
async def test_cleanup_stopped_run_closes_touched_browser_sessions(tmp_path):
    """Stop cleanup closes every browser session touched by the current run."""
    agent = _make_agent(tmp_path)
    session_key = "telegram:123"
    state = agent._start_run_state(session_key)
    state.touched_browser_sessions.update({"abc", "def"})
    agent.browser_manager = MagicMock()
    agent.browser_manager.current_session_ids.return_value = {"abc", "def", "other"}
    agent.browser_manager.close = AsyncMock()

    try:
        await agent._cleanup_stopped_run(session_key)
    finally:
        await agent._finish_run_state(session_key)

    assert agent.browser_manager.close.await_count == 2
    agent.browser_manager.close.assert_any_await("abc")
    agent.browser_manager.close.assert_any_await("def")
    assert not state.touched_browser_sessions


def test_telegram_commands_include_steering():
    """Telegram command list exposes the steering toggle."""
    assert ("steering", "Toggle in-loop steering") in BOT_COMMANDS
