"""Tests for steering injection and stop cleanup behavior."""

import asyncio
import contextlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ragnarbot.bus.events import InboundMessage, OutboundMessage
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


def test_telegram_commands_include_reasoning():
    """Telegram command list exposes the reasoning picker."""
    assert ("reasoning", "Change reasoning level") in BOT_COMMANDS


def test_telegram_commands_include_lightning():
    """Telegram command list exposes Lightning Mode."""
    assert ("lightning", "Toggle Lightning Mode") in BOT_COMMANDS


def test_reasoning_command_shows_picker(tmp_path):
    """Reasoning command renders the inline button picker."""
    agent = _make_agent(tmp_path)
    response = agent._handle_reasoning(_make_msg(content="/reasoning", command="reasoning"))

    assert "Reasoning" in response.content
    assert "Selected: <b>Medium</b>" in response.content
    assert response.metadata["inline_keyboard"][0][0]["callback_data"] == "reasoning_level:off"
    assert response.metadata["inline_keyboard"][2][0]["text"] == "✓ Medium"
    assert response.metadata["inline_keyboard"][4][0]["callback_data"] == "reasoning_level:ultra"
    assert all(len(row) == 1 for row in response.metadata["inline_keyboard"])


def test_set_reasoning_level_persists_and_edits_message(tmp_path):
    """Callback updates the stored reasoning level and edits the source panel."""
    agent = _make_agent(tmp_path)
    config = MagicMock()
    config.agents.defaults.reasoning_level = "medium"

    with (
        patch("ragnarbot.config.loader.load_config", return_value=config),
        patch("ragnarbot.config.loader.save_config") as mock_save,
    ):
        response = agent._handle_set_reasoning_level(_make_msg(
            content="/reasoning high",
            command="set_reasoning_level",
            reasoning_level="high",
            callback_message_id=42,
        ))

    assert response is not None
    assert agent.reasoning_level == "high"
    assert response.metadata["edit_message_id"] == 42
    assert "Selected: <b>High</b>" in response.content
    assert response.metadata["inline_keyboard"][3][0]["text"] == "✓ High"
    mock_save.assert_called_once_with(config)


def test_lightning_command_shows_toggle(tmp_path):
    """Lightning command renders the enable/disable panel."""
    agent = _make_agent(tmp_path)
    agent.model = "openai/gpt-5.4"
    agent.auth_method = "api_key"
    agent.lightning_mode = False

    response = agent._handle_lightning(_make_msg(content="/lightning", command="lightning"))

    assert "Lightning Mode" in response.content
    assert "Current: Disabled" in response.content
    assert "Priority processing" in response.content
    assert response.metadata["inline_keyboard"][0][0]["callback_data"] == "lightning_mode:on"
    assert response.metadata["inline_keyboard"][0][0]["text"] == "Enable"


def test_lightning_command_treats_openai_oauth_as_supported(tmp_path):
    """OpenAI OAuth should render Lightning Mode without the no-effect note."""
    agent = _make_agent(tmp_path)
    agent.model = "openai/gpt-5.4"
    agent.auth_method = "oauth"
    agent.lightning_mode = True

    response = agent._handle_lightning(_make_msg(content="/lightning", command="lightning"))

    assert "Current: Enabled" in response.content
    assert "Currently has no effect" not in response.content
    assert response.metadata["inline_keyboard"][0][0]["text"] == "Disable"


def test_lightning_command_shows_no_effect_note_when_unsupported(tmp_path):
    """Unsupported setups still show the panel with a no-effect note."""
    agent = _make_agent(tmp_path)
    agent.model = "anthropic/claude-opus-4-6"
    agent.auth_method = "api_key"
    agent.lightning_mode = True

    response = agent._handle_lightning(_make_msg(content="/lightning", command="lightning"))

    assert "Current: Enabled" in response.content
    assert "Currently has no effect" in response.content
    assert response.metadata["inline_keyboard"][0][0]["text"] == "Disable"


def test_set_lightning_mode_persists_and_edits_message(tmp_path):
    """Callback updates Lightning Mode and edits the source panel."""
    agent = _make_agent(tmp_path)
    agent.model = "openai/gpt-5.4"
    agent.auth_method = "api_key"
    config = MagicMock()
    config.agents.defaults.lightning_mode = False

    with (
        patch("ragnarbot.config.loader.load_config", return_value=config),
        patch("ragnarbot.config.loader.save_config") as mock_save,
    ):
        response = agent._handle_set_lightning_mode(_make_msg(
            content="/lightning on",
            command="set_lightning_mode",
            lightning_mode="on",
            callback_message_id=42,
        ))

    assert response is not None
    assert agent.lightning_mode is True
    assert response.metadata["edit_message_id"] == 42
    assert "Current: Enabled" in response.content
    assert response.metadata["inline_keyboard"][0][0]["text"] == "Disable"
    mock_save.assert_called_once_with(config)


@pytest.mark.asyncio
async def test_deliver_pending_update_notice_skips_wrong_chat(tmp_path):
    """Stored pending-update targets must not leak into another chat."""
    agent = _make_agent(tmp_path)
    agent._process_system_message = AsyncMock(return_value=OutboundMessage(
        channel="telegram", chat_id="123", content="notice",
    ))

    with patch("ragnarbot.agent.loop.load_pending_update", return_value={
        "requires_restart": True,
        "target_channel": "telegram",
        "target_chat_id": "123",
    }):
        delivered = await agent.deliver_pending_update_notice("telegram", "999")

    assert delivered is False
    agent._process_system_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_deliver_pending_update_notice_binds_first_chat_and_clears_info_notice(tmp_path):
    """Info-only update notices bind to the first eligible chat and clear after delivery."""
    agent = _make_agent(tmp_path)
    agent._process_system_message = AsyncMock(return_value=OutboundMessage(
        channel="telegram", chat_id="123", content="notice",
    ))

    payload = {
        "requires_restart": False,
        "old_version": "0.3.0",
        "new_version": "0.4.0",
    }
    bound_payload = {
        **payload,
        "target_channel": "telegram",
        "target_chat_id": "123",
    }

    with (
        patch("ragnarbot.agent.loop.load_pending_update", return_value=payload),
        patch("ragnarbot.agent.loop.bind_pending_update_target", return_value=bound_payload) as mock_bind,
        patch("ragnarbot.agent.loop.clear_pending_update") as mock_clear,
    ):
        delivered = await agent.deliver_pending_update_notice("telegram", "123")

    assert delivered is True
    mock_bind.assert_called_once_with("telegram", "123")
    mock_clear.assert_called_once()
    agent.bus.publish_outbound.assert_awaited_once()


@pytest.mark.asyncio
async def test_queue_pending_update_notice_uses_persisted_target(tmp_path):
    """Signal-driven notices should use the stored target chat."""
    agent = _make_agent(tmp_path)

    with patch("ragnarbot.agent.loop.load_pending_update", return_value={
        "requires_restart": True,
        "target_channel": "telegram",
        "target_chat_id": "123",
    }):
        queued = await agent.queue_pending_update_notice()

    assert queued is True
    agent.bus.publish_inbound.assert_awaited_once()
    queued_msg = agent.bus.publish_inbound.await_args.args[0]
    assert queued_msg.chat_id == "telegram:123"
