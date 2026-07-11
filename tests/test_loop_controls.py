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
async def test_finish_run_state_requeues_unconsumed_steering(tmp_path):
    """Late steering should go back onto the bus as the next normal turn."""
    agent = _make_agent(tmp_path)
    state = agent._start_run_state("telegram:123")
    steering_msg = _make_msg(content="too late", message_id="2")
    state.pending_steering.append(steering_msg)

    await agent._finish_run_state("telegram:123")

    agent.bus.publish_inbound.assert_awaited_once_with(steering_msg)
    assert agent._run_state is None


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
    assert "doubles usage" in response.content
    assert "Fast/Priority" not in response.content
    assert response.metadata["inline_keyboard"][0][0]["callback_data"] == "lightning_mode:on"
    assert response.metadata["inline_keyboard"][0][0]["text"] == "Enable"


def test_lightning_command_treats_openai_oauth_as_supported(tmp_path):
    """OpenAI OAuth should render Lightning Mode without the no-effect note."""
    agent = _make_agent(tmp_path)
    agent.model = "openai/gpt-5.4"
    agent.auth_method = "oauth"
    agent.lightning_mode = True

    with patch("ragnarbot.providers.openai_chatgpt_provider.is_codex_cli_available", return_value=True):
        response = agent._handle_lightning(_make_msg(content="/lightning", command="lightning"))

    assert "Current: Enabled" in response.content
    assert "Currently has no effect" not in response.content
    assert response.metadata["inline_keyboard"][0][0]["text"] == "Disable"


def test_lightning_command_shows_codex_install_note_when_oauth_cli_missing(tmp_path):
    """OpenAI OAuth Lightning should prompt to install Codex CLI when unavailable."""
    agent = _make_agent(tmp_path)
    agent.model = "openai/gpt-5.4"
    agent.auth_method = "oauth"
    agent.lightning_mode = False

    with patch("ragnarbot.providers.openai_chatgpt_provider.is_codex_cli_available", return_value=False):
        response = agent._handle_lightning(_make_msg(content="/lightning", command="lightning"))

    assert "OpenAI OAuth Lightning requires Codex CLI installed locally." in response.content
    assert response.metadata["inline_keyboard"][0][0]["text"] == "Enable"
    assert response.metadata["inline_keyboard"][1][0]["callback_data"] == "install_codex_cli"
    assert response.metadata["inline_keyboard"][1][0]["text"] == "Install Codex CLI"


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


@pytest.mark.parametrize(
    ("handler_name", "metadata_key", "runtime_attr", "config_attr"),
    [
        ("_handle_set_lightning_mode", "lightning_mode", "lightning_mode", "lightning_mode"),
        ("_handle_set_trace_mode", "trace_mode", "trace_mode", "trace_mode"),
        ("_handle_set_steering_mode", "steering_mode", "steering_enabled", "steering_enabled"),
    ],
)
def test_web_boolean_toggles_persist(
    tmp_path, handler_name, metadata_key, runtime_attr, config_attr,
):
    """Web commands send native booleans rather than Telegram on/off strings."""
    agent = _make_agent(tmp_path)
    agent.model = "openai/gpt-5.4"
    agent.auth_method = "api_key"
    setattr(agent, runtime_attr, True)
    config = MagicMock()

    with (
        patch("ragnarbot.config.loader.load_config", return_value=config),
        patch("ragnarbot.config.loader.save_config") as mock_save,
    ):
        response = getattr(agent, handler_name)(_make_msg(
            channel="web",
            content=f"/{metadata_key}",
            command=handler_name.removeprefix("_handle_"),
            **{metadata_key: False},
        ))

    assert response is not None
    assert getattr(agent, runtime_attr) is False
    assert getattr(config.agents.defaults, config_attr) is False
    mock_save.assert_called_once_with(config)


@pytest.mark.asyncio
async def test_web_media_is_buffered_until_turn_commit(tmp_path):
    agent = _make_agent(tmp_path)
    media_path = tmp_path / "result.png"
    media_path.write_bytes(b"png")
    state = agent._start_run_state("web:main")
    try:
        await agent._publish_media_outbound(OutboundMessage(
            channel="web",
            chat_id="main",
            content="Result caption",
            metadata={"media_type": "photo", "media_path": str(media_path)},
        ))
    finally:
        await agent._finish_run_state("web:main")

    assert state.media_events[0]["content"] == "Result caption"
    assert state.media_events[0]["media_items"][0]["path"] == str(media_path)
    agent.sessions.get_or_create.assert_not_called()


def test_turn_commit_persists_media_after_tools_and_before_final(tmp_path):
    agent = _make_agent(tmp_path)
    session = Session(key="web_main_test", user_key="web:main")
    raw = _make_msg(
        channel="web",
        chat_id="main",
        content="Prompt plus hidden marker",
        display_content="Prompt",
        attachments=[{"type": "file", "filename": "brief.pdf"}],
    )
    messages = [
        {"role": "user", "content": "Prompt plus hidden marker"},
        {
            "role": "assistant",
            "content": "Checking.",
            "tool_calls": [{
                "id": "tc-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
        {"role": "tool", "content": "ok", "tool_call_id": "tc-1", "name": "read_file"},
        {"role": "assistant", "content": "Done."},
    ]
    usage = {
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_read_tokens": 0,
        "model": "test",
        "duration_ms": 100,
    }

    agent._save_batch_messages(
        session,
        messages,
        0,
        [{"raw_msg": raw, "media_refs": []}],
        [],
        media_events=[{
            "content": "Result caption",
            "media_items": [{"path": "/tmp/result.png", "kind": "photo"}],
        }],
        turn_usage=usage,
    )

    assert [message["role"] for message in session.messages] == [
        "user", "assistant", "tool", "assistant", "assistant",
    ]
    assert session.messages[0]["content"] == "Prompt"
    assert session.messages[0]["attachments"] == [{
        "type": "file", "filename": "brief.pdf",
    }]
    assert session.messages[-2]["media_items"][0]["path"] == "/tmp/result.png"
    assert session.messages[-2]["content"] == "Result caption"
    assert session.messages[-1]["content"] == "Done."
    assert session.messages[-1]["usage"] == usage


def test_media_caption_echo_is_removed_from_final_reply():
    from ragnarbot.agent.loop import _strip_media_caption_echo

    media_events = [
        {"content": "First caption", "media_items": []},
        {"content": "MEDIA-CAPTION", "media_items": []},
    ]

    assert _strip_media_caption_echo(
        "MEDIA-CAPTIONFinal answer", media_events,
    ) == "Final answer"
    assert _strip_media_caption_echo(
        "First captionMEDIA-CAPTIONFinal answer", media_events,
    ) == "Final answer"
    assert _strip_media_caption_echo(
        "Unrelated final answer", media_events,
    ) == "Unrelated final answer"
    assert _strip_media_caption_echo(
        "MEDIA-CAPTION", media_events,
    ) is None


def test_set_lightning_mode_requires_codex_cli_for_openai_oauth(tmp_path):
    """Enabling OAuth Lightning without Codex CLI should not persist the toggle."""
    agent = _make_agent(tmp_path)
    agent.model = "openai/gpt-5.4"
    agent.auth_method = "oauth"
    agent.lightning_mode = False
    config = MagicMock()
    config.agents.defaults.lightning_mode = False

    with (
        patch("ragnarbot.providers.openai_chatgpt_provider.is_codex_cli_available", return_value=False),
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
    assert agent.lightning_mode is False
    assert response.metadata["edit_message_id"] == 42
    assert "OpenAI OAuth Lightning requires Codex CLI installed locally." in response.content
    assert response.metadata["inline_keyboard"][1][0]["callback_data"] == "install_codex_cli"
    mock_save.assert_not_called()


@pytest.mark.asyncio
async def test_install_codex_cli_callback_edits_lightning_panel(tmp_path):
    """Install callback should update the same Lightning panel in place."""
    agent = _make_agent(tmp_path)
    agent.model = "openai/gpt-5.4"
    agent.auth_method = "oauth"
    agent.lightning_mode = False

    with (
        patch("ragnarbot.providers.openai_chatgpt_provider.install_codex_cli", AsyncMock(return_value=(True, "brew install --cask codex"))),
        patch("ragnarbot.providers.openai_chatgpt_provider.is_codex_cli_available", return_value=True),
    ):
        response = await agent._handle_install_codex_cli(_make_msg(
            content="/lightning install_codex_cli",
            command="install_codex_cli",
            callback_message_id=42,
        ))

    assert response.metadata["edit_message_id"] == 42
    assert "✅ Codex CLI installed." in response.content
    assert "Current: Disabled" in response.content
    assert "requires Codex CLI" not in response.content
    assert len(response.metadata["inline_keyboard"]) == 1


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
