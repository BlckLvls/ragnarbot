"""Integration coverage for provider-managed OpenAI OAuth fast tool turns."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ragnarbot.agent.loop import AgentLoop
from ragnarbot.bus.events import InboundMessage
from ragnarbot.config.schema import ExecToolConfig
from ragnarbot.providers.base import ExecutedToolCall, LLMResponse, ToolCallRequest
from ragnarbot.providers.openai_chatgpt_provider import OpenAIChatGPTProvider


class _ProviderWithExecutedTools:
    def __init__(self):
        self.calls = []

    def get_default_model(self) -> str:
        return "openai/gpt-5.4"

    async def chat(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        tool_runner = kwargs["tool_runner"]
        tool_call = ToolCallRequest(
            id="call_123",
            name="lookup_ticket",
            arguments={"id": "ABC-123"},
        )
        result = await tool_runner(tool_call)
        return LLMResponse(
            content="Ticket handled.",
            executed_tool_calls=[
                ExecutedToolCall(
                    id="call_123",
                    name="lookup_ticket",
                    arguments={"id": "ABC-123"},
                    result=result,
                    assistant_content="Checking the ticket first.",
                )
            ],
            finish_reason="stop",
        )

    async def aclose(self) -> None:
        return None


class _ProviderWithLiveCallbacks:
    async def chat(self, **kwargs) -> LLMResponse:
        text_delta_handler = kwargs["text_delta_handler"]
        tool_call_handler = kwargs["tool_call_handler"]
        tool_runner = kwargs["tool_runner"]

        await text_delta_handler("Checking live callback.")

        tool_call = ToolCallRequest(
            id="call_live",
            name="lookup_ticket",
            arguments={"id": "ABC-999"},
        )
        await tool_call_handler(tool_call)
        result = await tool_runner(tool_call)

        return LLMResponse(
            content="Done.",
            executed_tool_calls=[
                ExecutedToolCall(
                    id="call_live",
                    name="lookup_ticket",
                    arguments={"id": "ABC-999"},
                    result=result,
                    metadata={"trace_emitted": True},
                    assistant_content="Checking live callback.",
                )
            ],
            finish_reason="stop",
        )

    def get_default_model(self) -> str:
        return "openai/gpt-5.4"

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_agent_loop_replays_provider_executed_tools_without_double_execution(tmp_path):
    provider = _ProviderWithExecutedTools()

    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_inbound = AsyncMock()

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        model="openai/gpt-5.4",
        auth_method="oauth",
        lightning_mode=True,
        exec_config=ExecToolConfig(),
    )
    loop._fallback_state.fallback_mode = False
    loop.tools.execute = AsyncMock(return_value="Ticket ABC-123 is open.")

    result = await loop.process_direct(
        "Please look up ABC-123.",
        session_key="cli:direct",
        channel="cli",
        chat_id="direct",
    )

    assert result == "Ticket handled."
    loop.tools.execute.assert_awaited_once_with("lookup_ticket", {"id": "ABC-123"})

    call_kwargs = provider.calls[0]
    assert call_kwargs["session_key"] == "cli:direct"
    assert call_kwargs["lightning_mode"] is True
    assert call_kwargs["tool_runner"] is not None

    session = loop.sessions.get_or_create("cli:direct")
    history = session.get_history()

    assert [message["role"] for message in history[-4:]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert history[-3]["tool_calls"][0]["function"]["name"] == "lookup_ticket"
    assert history[-2]["name"] == "lookup_ticket"
    assert history[-2]["content"] == "Ticket ABC-123 is open."
    assert history[-1]["content"] == "Ticket handled."


@pytest.mark.asyncio
async def test_agent_loop_traces_provider_managed_tools_live_without_duplicate_replay(tmp_path):
    provider = _ProviderWithLiveCallbacks()

    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_inbound = AsyncMock()

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        model="openai/gpt-5.4",
        auth_method="oauth",
        lightning_mode=True,
        stream_steps=True,
        trace_mode=True,
        exec_config=ExecToolConfig(),
    )
    loop._fallback_state.fallback_mode = False
    loop.tools.execute = AsyncMock(return_value="Ticket ABC-999 is open.")

    result = await loop.process_direct(
        "Please look up ABC-999.",
        session_key="cli:trace",
        channel="telegram",
        chat_id="trace",
    )

    assert result == "Done."
    outbound_messages = [call.args[0] for call in bus.publish_outbound.await_args_list]
    text_messages = [
        message
        for message in outbound_messages
        if message.metadata.get("intermediate") and not message.metadata.get("raw_html")
    ]
    trace_messages = [
        call.args[0]
        for call in bus.publish_outbound.await_args_list
        if call.args[0].metadata.get("raw_html")
    ]
    assert len(text_messages) == 1
    assert text_messages[0].content == "Checking live callback."
    assert len(trace_messages) == 1
    assert "lookup_ticket" in trace_messages[0].content

    session = loop.sessions.get_or_create("telegram:trace")
    history = session.get_history()
    assert [message["role"] for message in history[-4:]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert history[-3]["tool_calls"][0]["function"]["name"] == "lookup_ticket"


@pytest.mark.asyncio
async def test_agent_loop_fast_provider_applies_live_steering_and_persists_it(tmp_path, monkeypatch):
    class FakeProc:
        instances = []

        def __init__(self, codex_home):
            self.codex_home = codex_home
            self.calls = []
            self.responses = []
            self.sent_results = []
            self.closed = False
            FakeProc.instances.append(self)

        async def start(self):
            return None

        async def close(self):
            self.closed = True

        async def call(self, method, params=None):
            self.calls.append((method, params))
            if method == "account/login/start":
                return {"ok": True}
            if method == "thread/start":
                return {"thread": {"id": "thr_live"}}
            if method == "turn/start":
                self.responses = [
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thr_live",
                            "turnId": "turn_live",
                            "delta": "Checking first.",
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 70,
                        "method": "item/tool/call",
                        "params": {
                            "threadId": "thr_live",
                            "turnId": "turn_live",
                            "callId": "call_live",
                            "tool": "lookup_ticket",
                            "arguments": {"id": "ABC-321"},
                        },
                    },
                ]
                return {"turn": {"id": "turn_live", "status": "inProgress", "items": []}}
            if method == "turn/steer":
                self.responses = [
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thr_live",
                            "turnId": "turn_live",
                            "delta": "Adjusted answer.",
                        },
                    },
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thr_live",
                            "turn": {"id": "turn_live", "status": "completed"},
                        },
                    },
                ]
                return {"turnId": "turn_live"}
            raise AssertionError(f"Unexpected method: {method}")

        async def next_message(self):
            if self.responses:
                return self.responses.pop(0)
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

        async def respond(self, request_id, result):
            self.sent_results.append((request_id, result))

        async def respond_error(self, request_id, *, message, code=-32000, data=None):
            raise AssertionError(f"Unexpected error response: {request_id} {message}")

    monkeypatch.setattr(
        "ragnarbot.providers.openai_chatgpt_provider.CODEX_FAST_EVENT_POLL_INTERVAL_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        "ragnarbot.providers.openai_chatgpt_provider.CODEX_FAST_INITIAL_EVENT_TIMEOUT_SECONDS",
        0.2,
    )

    with (
        patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"),
        patch("ragnarbot.auth.openai_oauth.get_access_token", return_value="token"),
        patch("ragnarbot.providers.openai_chatgpt_provider._CodexAppServerProcess", FakeProc),
    ):
        provider = OpenAIChatGPTProvider()
        provider._codex_home = tmp_path / "codex-home"
        provider._thread_registry_path = provider._codex_home / "thread_registry.json"

        bus = MagicMock()
        bus.publish_outbound = AsyncMock()
        bus.publish_inbound = AsyncMock()

        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)

        loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=workspace,
            model="openai/gpt-5.4",
            auth_method="oauth",
            lightning_mode=True,
            exec_config=ExecToolConfig(),
        )
        loop._fallback_state.fallback_mode = False

        async def _execute(tool_name, arguments):
            queued = loop._queue_steering_message(
                InboundMessage(
                    channel="telegram",
                    sender_id="user1",
                    chat_id="steer",
                    content="Actually keep it short.",
                    metadata={"message_id": "2"},
                )
            )
            assert queued is True
            return "Ticket ABC-321 is open."

        loop.tools.execute = AsyncMock(side_effect=_execute)
        session_key = "telegram:steer"
        msg = InboundMessage(
            channel="telegram",
            sender_id="user1",
            chat_id="steer",
            content="Look up ABC-321 and then continue.",
            metadata={"message_id": "1"},
        )
        loop._start_run_state(session_key)
        loop._processing_session_key = session_key
        try:
            response = await loop._process_batch([msg])
            result = response.content if response else None
        finally:
            loop._processing_session_key = None
            await loop._finish_run_state(session_key)

    proc = FakeProc.instances[0]
    assert result == "Adjusted answer."
    assert dict(proc.calls)["turn/steer"]["expectedTurnId"] == "turn_live"
    steer_input = dict(proc.calls)["turn/steer"]["input"]
    assert len(steer_input) == 1
    assert steer_input[0]["type"] == "text"
    assert "[Steering message during active task]" in steer_input[0]["text"]
    assert "Actually keep it short." in steer_input[0]["text"]
    assert "2026-" in steer_input[0]["text"]

    session = loop.sessions.get_or_create("telegram:steer")
    history = session.messages
    assert [message["role"] for message in history[-5:]] == [
        "user",
        "assistant",
        "tool",
        "user",
        "assistant",
    ]
    assert history[-2]["content"] == "Actually keep it short."
    assert history[-2]["metadata"]["type"] == "steering"
    assert history[-1]["content"] == "Adjusted answer."


@pytest.mark.asyncio
async def test_agent_loop_stop_cancels_fast_provider_turn_and_closes_transport(tmp_path, monkeypatch):
    class SlowProc:
        instances = []

        def __init__(self, codex_home):
            self.codex_home = codex_home
            self.calls = []
            self.closed = False
            SlowProc.instances.append(self)

        async def start(self):
            return None

        async def close(self):
            self.closed = True

        async def call(self, method, params=None):
            self.calls.append((method, params))
            if method == "account/login/start":
                return {"ok": True}
            if method == "thread/start":
                return {"thread": {"id": "thr_stop"}}
            if method == "turn/start":
                return {"turn": {"id": "turn_stop", "status": "inProgress", "items": []}}
            raise AssertionError(f"Unexpected method: {method}")

        async def next_message(self):
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

        async def respond(self, request_id, result):
            raise AssertionError(f"Unexpected response: {request_id} {result}")

        async def respond_error(self, request_id, *, message, code=-32000, data=None):
            raise AssertionError(f"Unexpected error response: {request_id} {message}")

    monkeypatch.setattr(
        "ragnarbot.providers.openai_chatgpt_provider.CODEX_FAST_EVENT_POLL_INTERVAL_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        "ragnarbot.providers.openai_chatgpt_provider.CODEX_FAST_INITIAL_EVENT_TIMEOUT_SECONDS",
        30.0,
    )

    with (
        patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"),
        patch("ragnarbot.auth.openai_oauth.get_access_token", return_value="token"),
        patch("ragnarbot.providers.openai_chatgpt_provider._CodexAppServerProcess", SlowProc),
    ):
        provider = OpenAIChatGPTProvider()
        provider._codex_home = tmp_path / "codex-home"
        provider._thread_registry_path = provider._codex_home / "thread_registry.json"

        bus = MagicMock()
        bus.publish_outbound = AsyncMock()
        bus.publish_inbound = AsyncMock()

        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)

        loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=workspace,
            model="openai/gpt-5.4",
            auth_method="oauth",
            lightning_mode=True,
            exec_config=ExecToolConfig(),
        )
        loop._fallback_state.fallback_mode = False

        session_key = "telegram:stop"
        msg = InboundMessage(
            channel="telegram",
            sender_id="user1",
            chat_id="stop",
            content="Do a long thing.",
        )
        loop._start_run_state(session_key)
        task = asyncio.create_task(loop._process_batch([msg]))
        loop._processing_session_key = session_key
        loop._processing_task = task
        try:
            await asyncio.sleep(0.05)
            assert loop._request_stop(session_key) is True
            response = await task
            assert response is None
        finally:
            loop._processing_task = None
            loop._processing_session_key = None
            await loop._finish_run_state(session_key)

    assert SlowProc.instances[0].closed is True
