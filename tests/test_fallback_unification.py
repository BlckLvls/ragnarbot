"""Tests for unified fallback support across all LLM call sites."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ragnarbot.agent.cache import CacheManager
from ragnarbot.agent.compactor import Compactor
from ragnarbot.agent.fallback import FallbackState
from ragnarbot.agent.loop import AgentLoop
from ragnarbot.agent.subagent import SubagentManager
from ragnarbot.bus.events import InboundMessage
from ragnarbot.config.schema import ExecToolConfig, FallbackConfig
from ragnarbot.providers.base import LLMResponse, ToolCallRequest

# ── Compactor chat_fn tests ──────────────────────────────────────


class TestCompactorChatFn:
    """Test that Compactor routes LLM calls through chat_fn."""

    def _make_compactor(self, chat_fn=None):
        provider = AsyncMock()
        cm = CacheManager(max_context_tokens=1000)
        return Compactor(provider, cm, 1000, "test/model", chat_fn=chat_fn)

    @pytest.mark.asyncio
    async def test_default_wrapper_calls_provider(self):
        """Without chat_fn, Compactor uses provider.chat() directly."""
        c = self._make_compactor()
        c.provider.chat = AsyncMock(
            return_value=LLMResponse(content="summary")
        )

        # Call the default wrapper
        response, used_fallback, error = await c._chat_fn(
            None, messages=[{"role": "user", "content": "test"}], tools=None,
        )
        assert response.content == "summary"
        assert used_fallback is False
        assert error is None
        c.provider.chat.assert_called_once()

    @pytest.mark.asyncio
    async def test_injected_chat_fn_is_used(self):
        """When chat_fn is provided, it replaces provider.chat()."""
        custom_fn = AsyncMock(return_value=(
            LLMResponse(content="from custom"), True, "primary failed",
        ))
        c = self._make_compactor(chat_fn=custom_fn)

        response, used_fallback, error = await c._chat_fn(
            None, messages=[], tools=None,
        )
        assert response.content == "from custom"
        assert used_fallback is True
        custom_fn.assert_called_once()
        # provider.chat should NOT be called
        c.provider.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_compact_handles_error_response(self):
        """compact() returns original messages when LLM returns error."""
        error_fn = AsyncMock(return_value=(
            LLMResponse(content="API error", finish_reason="error"),
            True,
            "primary error",
        ))
        c = self._make_compactor(chat_fn=error_fn)

        from ragnarbot.session.manager import Session
        session = Session(key="test", user_key="test:1")
        for i in range(15):
            session.add_message("user", f"msg {i}")
            session.add_message("assistant", f"reply {i}")

        messages = [{"role": "user", "content": "test"}] * 20
        new_start = 19

        result_messages, result_start, memory_segment = await c.compact(
            session=session,
            context_mode="normal",
            context_builder=MagicMock(),
            messages=messages,
            new_start=new_start,
            tools=None,
        )
        assert result_messages is messages
        assert result_start == new_start
        assert memory_segment is None

    @pytest.mark.asyncio
    async def test_compact_handles_none_response(self):
        """compact() returns original messages when response is None."""
        none_fn = AsyncMock(return_value=(None, False, None))
        c = self._make_compactor(chat_fn=none_fn)

        from ragnarbot.session.manager import Session
        session = Session(key="test", user_key="test:1")
        for i in range(15):
            session.add_message("user", f"msg {i}")
            session.add_message("assistant", f"reply {i}")

        messages = [{"role": "user", "content": "test"}] * 20
        new_start = 19

        result_messages, result_start, memory_segment = await c.compact(
            session=session,
            context_mode="normal",
            context_builder=MagicMock(),
            messages=messages,
            new_start=new_start,
            tools=None,
        )
        assert result_messages is messages
        assert result_start == new_start
        assert memory_segment is None


# ── SubagentManager chat_fn tests ────────────────────────────────


class TestSubagentChatFn:
    """Test that SubagentManager routes LLM calls through chat_fn."""

    def _make_manager(self, chat_fn=None, on_fallback_batch=None):
        provider = AsyncMock()
        provider.get_default_model.return_value = "test/model"
        bus = MagicMock()
        bus.publish_inbound = AsyncMock()
        workspace = MagicMock()
        agents_loader = MagicMock()
        agents_loader.load_agent.return_value = None
        return SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            agents_loader=agents_loader,
            model="test/model",
            chat_fn=chat_fn,
            on_fallback_batch=on_fallback_batch,
        )

    @pytest.mark.asyncio
    async def test_default_wrapper_calls_provider(self):
        """Without chat_fn, SubagentManager uses provider.chat() directly."""
        mgr = self._make_manager()
        mgr.provider.chat = AsyncMock(
            return_value=LLMResponse(content="result")
        )

        response, used_fallback, error = await mgr._chat_fn(
            None, messages=[], tools=[], model="test/model",
        )
        assert response.content == "result"
        assert used_fallback is False
        mgr.provider.chat.assert_called_once()

    def _make_agent_task(self, task_id="test-id", task="do something",
                         label="test label", channel="test", chat_id="1"):
        import asyncio

        from ragnarbot.agent.subagent import AgentTask, AgentTaskStatus
        return AgentTask(
            id=task_id,
            label=label,
            agent_name=None,
            task=task,
            status=AgentTaskStatus.running,
            messages=[],
            stop_event=asyncio.Event(),
            origin={"channel": channel, "chat_id": chat_id},
        )

    def _make_tools_and_deliver(self):
        from ragnarbot.agent.tools.deliver_result import DeliverResultTool
        from ragnarbot.agent.tools.registry import ToolRegistry
        reg = ToolRegistry()
        deliver = DeliverResultTool()
        reg.register(deliver)
        return reg, deliver

    @pytest.mark.asyncio
    async def test_injected_chat_fn_is_used_in_subagent(self):
        """Subagent uses injected chat_fn instead of provider.chat()."""
        chat_fn = AsyncMock(return_value=(
            LLMResponse(content="final answer"), False, None,
        ))
        mgr = self._make_manager(chat_fn=chat_fn)
        agent_task = self._make_agent_task()
        tools, deliver = self._make_tools_and_deliver()

        await mgr._run_agent(agent_task, None, "test/model", tools, deliver)

        chat_fn.assert_called()
        mgr.provider.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_batch_called_on_success(self):
        """on_fallback_batch is called when fallback was used and task succeeds."""
        chat_fn = AsyncMock(return_value=(
            LLMResponse(content="done"), True, "primary failed",
        ))
        on_fb = AsyncMock()
        mgr = self._make_manager(chat_fn=chat_fn, on_fallback_batch=on_fb)
        agent_task = self._make_agent_task(channel="telegram", chat_id="42")
        tools, deliver = self._make_tools_and_deliver()

        await mgr._run_agent(agent_task, None, "test/model", tools, deliver)

        on_fb.assert_awaited_once_with(True, "telegram", "42")

    @pytest.mark.asyncio
    async def test_fallback_batch_called_on_error(self):
        """on_fallback_batch is called even when subagent raises RuntimeError."""
        chat_fn = AsyncMock(return_value=(
            LLMResponse(content="both providers failed", finish_reason="error"),
            True,
            "primary error",
        ))
        on_fb = AsyncMock()
        mgr = self._make_manager(chat_fn=chat_fn, on_fallback_batch=on_fb)
        agent_task = self._make_agent_task()
        tools, deliver = self._make_tools_and_deliver()

        await mgr._run_agent(agent_task, None, "test/model", tools, deliver)

        # Should still be called despite the error
        on_fb.assert_awaited_once_with(True, "test", "1")
        # Verify announce was called with error status
        mgr.bus.publish_inbound.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_fallback_batch_when_primary_succeeds(self):
        """on_fallback_batch is NOT called when primary was always used."""
        chat_fn = AsyncMock(return_value=(
            LLMResponse(content="done"), False, None,
        ))
        on_fb = AsyncMock()
        mgr = self._make_manager(chat_fn=chat_fn, on_fallback_batch=on_fb)
        agent_task = self._make_agent_task()
        tools, deliver = self._make_tools_and_deliver()

        await mgr._run_agent(agent_task, None, "test/model", tools, deliver)

        on_fb.assert_not_awaited()


# ── FallbackState + _record_fallback_batch tests ─────────────────


class TestRecordFallbackBatch:
    """Test _record_fallback_batch helper logic (via FallbackState)."""

    def test_record_failure_increments(self):
        state = FallbackState()
        assert state.consecutive_failures == 0
        entered = state.record_primary_failure(3)
        assert not entered
        assert state.consecutive_failures == 1

    def test_record_failure_enters_fallback_at_threshold(self):
        state = FallbackState(consecutive_failures=2)
        entered = state.record_primary_failure(3)
        assert entered
        assert state.fallback_mode is True

    def test_record_failure_already_in_fallback(self):
        state = FallbackState(consecutive_failures=5, fallback_mode=True)
        entered = state.record_primary_failure(3)
        assert not entered  # already in fallback
        assert state.consecutive_failures == 6

    def test_record_success_exits_fallback(self):
        state = FallbackState(consecutive_failures=5, fallback_mode=True)
        was_fb = state.record_primary_success()
        assert was_fb is True
        assert state.fallback_mode is False
        assert state.consecutive_failures == 0

    def test_record_success_no_fallback(self):
        state = FallbackState(consecutive_failures=1)
        was_fb = state.record_primary_success()
        assert was_fb is False
        assert state.consecutive_failures == 0


# ── Foreground/system interaction stickiness ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("system_message", [False, True])
async def test_fallback_stays_sticky_for_entire_interaction(tmp_path, system_message):
    """A fallback tool call must not hand the final answer back to primary."""
    primary_model = "openai/gpt-5.6-sol"
    fallback_model = "anthropic/claude-opus-4-8"

    primary = MagicMock()
    primary.get_default_model.return_value = primary_model
    primary.chat = AsyncMock(side_effect=[
        LLMResponse(content="primary timeout", finish_reason="error"),
        LLMResponse(content="PRIMARY NEXT INTERACTION"),
    ])

    fallback = MagicMock()
    fallback.chat = AsyncMock(side_effect=[
        LLMResponse(
            content="running tool",
            tool_calls=[ToolCallRequest(
                id="tool-1",
                name="exec",
                arguments={"command": "echo ok"},
            )],
            finish_reason="tool_calls",
        ),
        LLMResponse(content="FALLBACK FINAL"),
    ])
    provider_factory = MagicMock(return_value=fallback)

    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_inbound = AsyncMock()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with patch("ragnarbot.agent.loop.SubagentManager"):
        agent = AgentLoop(
            bus=bus,
            provider=primary,
            workspace=workspace,
            model=primary_model,
            exec_config=ExecToolConfig(),
            fallback_model=fallback_model,
            fallback_config=FallbackConfig(model=fallback_model),
            provider_factory=provider_factory,
        )

    agent._fallback_state = FallbackState()
    agent._fallback_state.save = MagicMock()
    agent._execute_tool_with_tracking = AsyncMock(return_value="tool ok")
    agent.index.start_chat_jobs = AsyncMock()

    if system_message:
        message = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id="telegram:123",
            content="system result",
        )
        response = await agent._process_system_message(message)
    else:
        message = InboundMessage(
            channel="telegram",
            sender_id="user-1",
            chat_id="123",
            content="run a tool",
        )
        response = await agent._process_batch([message])

    assert response is not None
    assert response.content == f"FALLBACK FINAL\n\n_⚡ fallback: {fallback_model}_"
    assert primary.chat.await_count == 1
    assert fallback.chat.await_count == 2
    provider_factory.assert_called_once_with(fallback_model, "api_key")
    assert fallback.chat.await_args_list[1].kwargs["model"] == fallback_model
    assert agent._execute_tool_with_tracking.await_count == 1
    assert agent._fallback_state.consecutive_failures == 1
    agent._fallback_state.save.assert_called_once_with()

    # The pin is interaction-local: the next interaction probes primary normally.
    if system_message:
        next_response = await agent._process_system_message(InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id="telegram:123",
            content="next system result",
        ))
    else:
        next_response = await agent._process_batch([InboundMessage(
            channel="telegram",
            sender_id="user-1",
            chat_id="123",
            content="next request",
        )])

    assert next_response is not None
    assert next_response.content == "PRIMARY NEXT INTERACTION"
    assert primary.chat.await_count == 2
    assert fallback.chat.await_count == 2
    assert agent._fallback_state.consecutive_failures == 0
