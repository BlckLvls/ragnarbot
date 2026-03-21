"""OpenAI ChatGPT backend provider for OAuth-based access."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from ragnarbot.providers.base import (
    ConsumedSteeringMessage,
    ExecutedToolCall,
    LLMProvider,
    LLMResponse,
    SteeringMessageProvider,
    TextDeltaHandler,
    ToolCallHandler,
    ToolCallRequest,
    ToolRunner,
)
from ragnarbot.providers.lightning import resolve_lightning
from ragnarbot.providers.reasoning import resolve_reasoning

API_BASE = "https://chatgpt.com/backend-api/codex"
RESPONSES_URL = f"{API_BASE}/responses"
CODEX_APP_SERVER_STREAM_LIMIT = 4 * 1024 * 1024
CODEX_FAST_MAX_ATTEMPTS = 2
CODEX_FAST_RETRY_DELAY_SECONDS = 0.75
CODEX_FAST_INITIAL_EVENT_TIMEOUT_SECONDS = 30.0
CODEX_FAST_EVENT_POLL_INTERVAL_SECONDS = 0.25
CODEX_FAST_THREAD_REGISTRY = "thread_registry.json"


@dataclass
class _CodexThreadRecord:
    """Persisted Codex thread metadata keyed by Ragnarbot session key."""

    thread_id: str
    tool_signature: str


class _JSONRPCError(RuntimeError):
    """Raised when the Codex app-server returns a JSON-RPC error."""

    def __init__(self, message: str, *, code: int | None = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class _CodexAppServerProcess:
    """Small JSON-RPC client for `codex app-server --listen stdio://`."""

    def __init__(self, codex_home: Path):
        self._codex_home = codex_home
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=60)
        self._last_sent_method: str | None = None
        self._last_received_method: str | None = None
        self._read_error: str | None = None

    @property
    def stderr_text(self) -> str:
        """Return recent stderr output for debugging."""
        return _clean_codex_stderr("".join(self._stderr_tail))

    async def start(self) -> None:
        """Launch the app-server and perform initialize/initialized handshake."""
        codex_bin = shutil.which("codex")
        if not codex_bin:
            raise FileNotFoundError("`codex` CLI is not installed or not on PATH.")

        self._codex_home.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self._codex_home)

        self._proc = await asyncio.create_subprocess_exec(
            codex_bin,
            "app-server",
            "--listen",
            "stdio://",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=CODEX_APP_SERVER_STREAM_LIMIT,
            env=env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        await self.call(
            "initialize",
            {
                "clientInfo": {
                    "name": "ragnarbot",
                    "title": "Ragnarbot",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "experimentalApi": True,
                },
            },
        )
        await self.notify("initialized", {})

    async def close(self) -> None:
        """Terminate the app-server process and settle pending futures."""
        proc = self._proc
        self._proc = None

        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, ProcessLookupError):
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=2)

        for task in (self._reader_task, self._stderr_task):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        while self._pending:
            _, future = self._pending.popitem()
            if not future.done():
                future.set_exception(_JSONRPCError("Codex app-server closed unexpectedly."))

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a JSON-RPC request and wait for the response."""
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        await self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        })
        return await future

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification."""
        await self._send({
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        })

    async def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        """Send a successful JSON-RPC response."""
        await self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        })

    async def respond_error(
        self,
        request_id: Any,
        *,
        message: str,
        code: int = -32000,
        data: Any = None,
    ) -> None:
        """Send an error JSON-RPC response."""
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        await self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": error,
        })

    async def next_message(self) -> dict[str, Any]:
        """Wait for the next server request or notification."""
        return await self._messages.get()

    async def _send(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise _JSONRPCError("Codex app-server is not running.")
        if "method" in payload:
            self._last_sent_method = str(payload["method"])
        elif "result" in payload:
            self._last_sent_method = "jsonrpc_result"
        elif "error" in payload:
            self._last_sent_method = "jsonrpc_error"
        proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await proc.stdin.drain()

    async def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                raw = line.decode("utf-8").strip()
                if not raw:
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "method" in message:
                    self._last_received_method = str(message["method"])

                if "id" in message and "method" not in message:
                    future = self._pending.pop(message["id"], None)
                    if future is None or future.done():
                        continue
                    error = message.get("error")
                    if error is not None:
                        future.set_exception(
                            _JSONRPCError(
                                error.get("message", "JSON-RPC error"),
                                code=error.get("code"),
                                data=error.get("data"),
                            )
                        )
                    else:
                        future.set_result(message.get("result"))
                    continue

                await self._messages.put(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._read_error = f"{type(exc).__name__}: {exc}"
            while self._pending:
                _, future = self._pending.popitem()
                if not future.done():
                    future.set_exception(_JSONRPCError(f"Codex app-server read failed: {exc}"))
        finally:
            while self._pending:
                _, future = self._pending.popitem()
                if not future.done():
                    future.set_exception(_JSONRPCError("Codex app-server connection closed."))
            await self._messages.put({"method": "__closed__"})

    async def close_detail(self) -> str:
        """Summarize recent app-server close context for debug logging."""
        parts: list[str] = []
        proc = self._proc
        if proc is not None:
            if proc.returncode is None:
                with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
                    await asyncio.wait_for(proc.wait(), timeout=0.2)
            if proc.returncode is not None:
                parts.append(f"exit={proc.returncode}")
        if self._last_sent_method:
            parts.append(f"last_sent={self._last_sent_method}")
        if self._last_received_method:
            parts.append(f"last_received={self._last_received_method}")
        if self._read_error:
            parts.append(f"read_error={self._read_error}")
        stderr = self.stderr_text.strip()
        if stderr:
            parts.append(f"stderr={stderr}")
        return "; ".join(parts)

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                self._stderr_tail.append(line.decode("utf-8", errors="replace"))
        except asyncio.CancelledError:
            raise


class OpenAIChatGPTProvider(LLMProvider):
    """LLM provider for OpenAI via ChatGPT backend API (OAuth)."""

    def __init__(
        self,
        default_model: str = "gpt-5.4",
    ):
        super().__init__()
        self.default_model = default_model

        from ragnarbot.auth.openai_oauth import get_account_id
        from ragnarbot.instance import ensure_instance_root

        info = ensure_instance_root()
        self._account_id = get_account_id()
        self._codex_home = info.data_root / "codex-oauth-fast"
        self._thread_registry_path = self._codex_home / CODEX_FAST_THREAD_REGISTRY
        self._thread_registry_lock = asyncio.Lock()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        reasoning_level: str | None = None,
        lightning_mode: bool | None = None,
        session_key: str | None = None,
        tool_runner: ToolRunner | None = None,
        tool_call_handler: ToolCallHandler | None = None,
        text_delta_handler: TextDeltaHandler | None = None,
        steering_message_provider: SteeringMessageProvider | None = None,
    ) -> LLMResponse:
        from ragnarbot.auth.openai_oauth import get_access_token

        _ = max_tokens, temperature
        model = model or self.default_model
        model = _strip_provider_prefix(model)

        access_token = get_access_token()
        if not access_token:
            return LLMResponse(
                content="Error: OpenAI OAuth token not available. Run: ragnarbot oauth openai",
                finish_reason="error",
            )

        account_id = self._account_id or ""
        use_fast_transport = self._should_use_codex_fast_transport(
            model=model,
            lightning_mode=lightning_mode,
            session_key=session_key,
            tool_runner=tool_runner,
        )

        try:
            if use_fast_transport:
                return await self._chat_codex_fast(
                    messages=messages,
                    tools=tools,
                    model=model,
                    reasoning_level=reasoning_level,
                    access_token=access_token,
                    account_id=account_id,
                    session_key=session_key or "",
                    tool_runner=tool_runner,
                    tool_call_handler=tool_call_handler,
                    text_delta_handler=text_delta_handler,
                    steering_message_provider=steering_message_provider,
                )
            return await self._chat_raw(
                messages=messages,
                tools=tools,
                model=model,
                reasoning_level=reasoning_level,
                access_token=access_token,
                account_id=account_id,
            )
        except Exception as e:
            return LLMResponse(
                content=f"Error calling OpenAI ChatGPT API: {e}",
                finish_reason="error",
            )

    async def _chat_raw(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        reasoning_level: str | None,
        access_token: str,
        account_id: str,
    ) -> LLMResponse:
        request_body = self._build_request(
            messages,
            tools,
            model,
            reasoning_level,
            lightning_mode=False,
        )
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "chatgpt-account-id": account_id,
            "OpenAI-Beta": "responses=experimental",
            "originator": "ragnarbot",
            "accept": "text/event-stream",
        }
        return await self._stream_request(request_body, headers)

    async def _chat_codex_fast(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        reasoning_level: str | None,
        access_token: str,
        account_id: str,
        session_key: str,
        tool_runner: ToolRunner | None,
        tool_call_handler: ToolCallHandler | None,
        text_delta_handler: TextDeltaHandler | None,
        steering_message_provider: SteeringMessageProvider | None,
    ) -> LLMResponse:
        if tool_runner is None:
            raise RuntimeError("Codex Fast transport requires a tool runner.")

        last_error_response: LLMResponse | None = None
        last_exception: Exception | None = None
        for attempt in range(1, CODEX_FAST_MAX_ATTEMPTS + 1):
            response, detail, exc = await self._chat_codex_fast_once(
                messages=messages,
                tools=tools,
                model=model,
                reasoning_level=reasoning_level,
                access_token=access_token,
                account_id=account_id,
                session_key=session_key,
                tool_runner=tool_runner,
                tool_call_handler=tool_call_handler,
                text_delta_handler=text_delta_handler,
                steering_message_provider=steering_message_provider,
            )
            if exc is not None:
                last_exception = exc
                if attempt < CODEX_FAST_MAX_ATTEMPTS and _should_retry_codex_fast_exception(exc):
                    logger.warning(
                        "Retrying Codex Fast transport after transient setup failure "
                        f"(attempt {attempt}/{CODEX_FAST_MAX_ATTEMPTS}): {exc} "
                        f"{f'[{detail}]' if detail else ''}"
                    )
                    await asyncio.sleep(CODEX_FAST_RETRY_DELAY_SECONDS * attempt)
                    continue
                raise exc

            assert response is not None
            last_error_response = response
            if (
                attempt < CODEX_FAST_MAX_ATTEMPTS
                and _should_retry_codex_fast_response(response)
            ):
                logger.warning(
                    "Retrying Codex Fast transport after transient turn failure "
                    f"(attempt {attempt}/{CODEX_FAST_MAX_ATTEMPTS}): "
                    f"{response.content or 'unknown error'} "
                    f"{f'[{detail}]' if detail else ''}"
                )
                await asyncio.sleep(CODEX_FAST_RETRY_DELAY_SECONDS * attempt)
                continue
            if response.finish_reason == "error" and detail:
                logger.warning(f"Codex Fast transport failed: {detail}")
            return response

        if last_exception is not None:
            raise last_exception
        assert last_error_response is not None
        return last_error_response

    async def _chat_codex_fast_once(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        reasoning_level: str | None,
        access_token: str,
        account_id: str,
        session_key: str,
        tool_runner: ToolRunner,
        tool_call_handler: ToolCallHandler | None,
        text_delta_handler: TextDeltaHandler | None,
        steering_message_provider: SteeringMessageProvider | None,
    ) -> tuple[LLMResponse | None, str, Exception | None]:
        reasoning = resolve_reasoning(model, reasoning_level)
        instructions = self._build_system_instructions(messages)
        history_messages, current_messages = self._split_codex_messages(messages)
        current_input = self._convert_codex_user_input(current_messages)
        dynamic_tools = self._convert_dynamic_tools(tools)
        tool_signature = _dynamic_tool_signature(dynamic_tools)
        proc = _CodexAppServerProcess(self._codex_home)
        detail = ""

        try:
            await proc.start()
            await proc.call(
                "account/login/start",
                {
                    "type": "chatgptAuthTokens",
                    "accessToken": access_token,
                    "chatgptAccountId": account_id,
                },
            )

            thread_id, bootstrap_text = await self._prepare_codex_thread(
                proc=proc,
                session_key=session_key,
                model=model,
                instructions=instructions,
                dynamic_tools=dynamic_tools,
                tool_signature=tool_signature,
                history_messages=history_messages,
            )

            turn_input = []
            if bootstrap_text:
                turn_input.append({
                    "type": "text",
                    "text": bootstrap_text,
                    "textElements": [],
                })
            turn_input.extend(current_input)
            if not turn_input:
                turn_input.append({
                    "type": "text",
                    "text": "Continue.",
                    "textElements": [],
                })

            turn_params: dict[str, Any] = {
                "threadId": thread_id,
                "input": turn_input,
                "model": model,
                "serviceTier": "fast",
                "personality": "none",
                "approvalPolicy": "untrusted",
                "sandboxPolicy": {"type": "readOnly"},
            }
            if reasoning.reasoning_effort:
                turn_params["effort"] = reasoning.reasoning_effort

            turn_result = await proc.call("turn/start", turn_params)
            turn_id = turn_result["turn"]["id"]
            response = await self._drain_codex_turn(
                proc=proc,
                thread_id=thread_id,
                turn_id=turn_id,
                tool_runner=tool_runner,
                tool_call_handler=tool_call_handler,
                text_delta_handler=text_delta_handler,
                steering_message_provider=steering_message_provider,
            )
            if response.finish_reason == "error":
                detail = await _maybe_close_detail(proc)
            return response, detail, None
        except Exception as exc:
            detail = await _maybe_close_detail(proc)
            return None, detail, exc
        finally:
            await proc.close()

    async def _drain_codex_turn(
        self,
        *,
        proc: _CodexAppServerProcess,
        thread_id: str,
        turn_id: str,
        tool_runner: ToolRunner,
        tool_call_handler: ToolCallHandler | None,
        text_delta_handler: TextDeltaHandler | None,
        steering_message_provider: SteeringMessageProvider | None,
    ) -> LLMResponse:
        assistant_buffer = ""
        executed_tool_calls: list[ExecutedToolCall] = []
        consumed_steering_messages: list[ConsumedSteeringMessage] = []
        turn_active = False
        allow_live_steering = False
        loop = asyncio.get_running_loop()
        initial_deadline = loop.time() + CODEX_FAST_INITIAL_EVENT_TIMEOUT_SECONDS

        while True:
            try:
                timeout = CODEX_FAST_EVENT_POLL_INTERVAL_SECONDS
                if not turn_active:
                    remaining = initial_deadline - loop.time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    timeout = min(timeout, remaining)
                message = await asyncio.wait_for(proc.next_message(), timeout=timeout)
            except asyncio.TimeoutError:
                if not turn_active and loop.time() < initial_deadline:
                    continue
                if steering_message_provider is not None and turn_active and allow_live_steering:
                    steering_messages = await steering_message_provider()
                    if steering_messages:
                        await proc.call(
                            "turn/steer",
                            {
                                "threadId": thread_id,
                                "expectedTurnId": turn_id,
                                "input": self._convert_codex_user_input(steering_messages),
                            },
                        )
                        consumed_steering_messages.extend(
                            ConsumedSteeringMessage(
                                after_executed_tool_calls=len(executed_tool_calls),
                                user_message=user_message,
                            )
                            for user_message in steering_messages
                        )
                        continue
                if turn_active:
                    continue
                return LLMResponse(
                    content="Codex Fast transport startup timed out.",
                    consumed_steering_messages=consumed_steering_messages,
                    finish_reason="error",
                )
            method = message.get("method")
            if method == "__closed__":
                return LLMResponse(
                    content="Codex Fast transport closed unexpectedly.",
                    consumed_steering_messages=consumed_steering_messages,
                    finish_reason="error",
                )

            params = message.get("params", {})
            if params.get("threadId") not in {None, thread_id}:
                continue
            if params.get("turnId") not in {None, turn_id}:
                continue
            if method == "turn/started":
                turn = params.get("turn") or {}
                if turn.get("id") in {None, turn_id}:
                    turn_active = True
            elif params.get("turnId") == turn_id:
                turn_active = True

            if method == "item/agentMessage/delta":
                delta = params.get("delta", "")
                allow_live_steering = False
                assistant_buffer += delta
                continue

            if method == "item/tool/call":
                request_id = message.get("id")
                call_id = params.get("callId", "")
                tool_name = params.get("tool", "")
                arguments = params.get("arguments", {})
                assistant_content = assistant_buffer or None
                assistant_buffer = ""

                tool_call = ToolCallRequest(
                    id=call_id,
                    name=tool_name,
                    arguments=arguments if isinstance(arguments, dict) else {"raw": arguments},
                )
                if assistant_content and text_delta_handler is not None:
                    with contextlib.suppress(Exception):
                        await text_delta_handler(assistant_content)
                trace_emitted = False
                if tool_call_handler is not None:
                    with contextlib.suppress(Exception):
                        await tool_call_handler(tool_call)
                    trace_emitted = True
                try:
                    result = await tool_runner(tool_call)
                    await proc.respond(
                        request_id,
                        {
                            "contentItems": _format_dynamic_tool_output(result),
                            "success": True,
                        },
                    )
                    allow_live_steering = True
                except Exception as exc:
                    result = f"Error: {exc}"
                    await proc.respond(
                        request_id,
                        {
                            "contentItems": [{"type": "inputText", "text": result}],
                            "success": False,
                        },
                    )
                    allow_live_steering = True

                executed_tool_calls.append(
                    ExecutedToolCall(
                        id=tool_call.id,
                        name=tool_call.name,
                        arguments=tool_call.arguments,
                        result=result,
                        metadata={"trace_emitted": trace_emitted},
                        assistant_content=assistant_content,
                    )
                )
                continue

            if method == "account/chatgptAuthTokens/refresh":
                await self._handle_codex_refresh_request(proc, message)
                continue

            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            }:
                await proc.respond(message.get("id"), {"decision": "decline"})
                continue

            if method == "item/permissions/requestApproval":
                await proc.respond(message.get("id"), {"permissions": {}})
                continue

            if method == "turn/completed":
                turn = params.get("turn", {})
                status = turn.get("status")
                if status == "completed":
                    return LLMResponse(
                        content=assistant_buffer or None,
                        executed_tool_calls=executed_tool_calls,
                        consumed_steering_messages=consumed_steering_messages,
                        finish_reason="stop",
                    )
                error = turn.get("error") or {}
                return LLMResponse(
                    content=error.get("message", f"Codex Fast turn failed with status {status}."),
                    executed_tool_calls=executed_tool_calls,
                    consumed_steering_messages=consumed_steering_messages,
                    finish_reason="error" if status == "failed" else "stop",
                )

            if method == "error":
                error = params.get("error") or {}
                return LLMResponse(
                    content=error.get("message", "Codex Fast transport error."),
                    consumed_steering_messages=consumed_steering_messages,
                    finish_reason="error",
                )

    async def _handle_codex_refresh_request(
        self,
        proc: _CodexAppServerProcess,
        message: dict[str, Any],
    ) -> None:
        from ragnarbot.auth.openai_oauth import get_access_token, get_account_id

        access_token = get_access_token()
        account_id = get_account_id()
        if access_token and account_id:
            await proc.respond(
                message.get("id"),
                {
                    "accessToken": access_token,
                    "chatgptAccountId": account_id,
                },
            )
            return
        await proc.respond_error(
            message.get("id"),
            message="OpenAI OAuth refresh failed: token unavailable.",
        )

    def _should_use_codex_fast_transport(
        self,
        *,
        model: str,
        lightning_mode: bool | None,
        session_key: str | None,
        tool_runner: ToolRunner | None,
    ) -> bool:
        resolution = resolve_lightning(model, "oauth", lightning_mode)
        return bool(resolution.applies and session_key and tool_runner)

    async def _prepare_codex_thread(
        self,
        *,
        proc: _CodexAppServerProcess,
        session_key: str,
        model: str,
        instructions: str | None,
        dynamic_tools: list[dict[str, Any]],
        tool_signature: str,
        history_messages: list[dict[str, Any]],
    ) -> tuple[str, str | None]:
        """Resume an existing Codex thread or create a new one for this session."""
        record = await self._get_codex_thread_record(session_key)
        if record is not None and record.tool_signature != tool_signature:
            await self._delete_codex_thread_record(session_key)
            record = None

        if record is not None:
            try:
                result = await proc.call(
                    "thread/resume",
                    {
                        "threadId": record.thread_id,
                        "model": model,
                        "serviceTier": "fast",
                        "approvalPolicy": "untrusted",
                        "sandbox": "read-only",
                        "developerInstructions": instructions or "You are a helpful assistant.",
                        "personality": "none",
                        "persistExtendedHistory": True,
                    },
                )
                return result["thread"]["id"], None
            except _JSONRPCError:
                await self._delete_codex_thread_record(session_key)

        thread_params: dict[str, Any] = {
            "model": model,
            "serviceTier": "fast",
            "personality": "none",
            "approvalPolicy": "untrusted",
            "sandbox": "read-only",
            "developerInstructions": instructions or "You are a helpful assistant.",
            "serviceName": "ragnarbot",
            "persistExtendedHistory": True,
        }
        if dynamic_tools:
            thread_params["dynamicTools"] = dynamic_tools

        thread_result = await proc.call("thread/start", thread_params)
        thread_id = thread_result["thread"]["id"]
        await self._set_codex_thread_record(
            session_key,
            _CodexThreadRecord(thread_id=thread_id, tool_signature=tool_signature),
        )
        bootstrap_text = self._build_codex_bootstrap_text(history_messages)
        return thread_id, bootstrap_text or None

    async def _get_codex_thread_record(self, session_key: str) -> _CodexThreadRecord | None:
        async with self._thread_registry_lock:
            raw = self._read_thread_registry()
            entry = raw.get(session_key)
            if not isinstance(entry, dict):
                return None
            thread_id = entry.get("thread_id")
            tool_signature = entry.get("tool_signature", "")
            if not isinstance(thread_id, str) or not thread_id:
                return None
            if not isinstance(tool_signature, str):
                tool_signature = ""
            return _CodexThreadRecord(thread_id=thread_id, tool_signature=tool_signature)

    async def _set_codex_thread_record(
        self,
        session_key: str,
        record: _CodexThreadRecord,
    ) -> None:
        async with self._thread_registry_lock:
            raw = self._read_thread_registry()
            raw[session_key] = {
                "thread_id": record.thread_id,
                "tool_signature": record.tool_signature,
            }
            self._write_thread_registry(raw)

    async def _delete_codex_thread_record(self, session_key: str) -> None:
        async with self._thread_registry_lock:
            raw = self._read_thread_registry()
            if raw.pop(session_key, None) is not None:
                self._write_thread_registry(raw)

    def _read_thread_registry(self) -> dict[str, Any]:
        if not self._thread_registry_path.is_file():
            return {}
        try:
            data = json.loads(self._thread_registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_thread_registry(self, data: dict[str, Any]) -> None:
        self._thread_registry_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._thread_registry_path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(self._thread_registry_path)

    async def _stream_request(self, request_body: dict, headers: dict) -> LLMResponse:
        """POST to the responses endpoint and parse SSE events."""
        text_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        finish_reason = "stop"
        usage: dict[str, int] = {}

        pending_calls: dict[str, dict] = {}

        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            async with client.stream(
                "POST",
                RESPONSES_URL,
                json=request_body,
                headers=headers,
            ) as resp:
                resp.raise_for_status()

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue

                    raw = line[len("data: "):]
                    if raw.strip() == "[DONE]":
                        break

                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type", "")

                    if event_type == "response.output_text.delta":
                        delta = event.get("delta", "")
                        if delta:
                            text_parts.append(delta)

                    elif event_type == "response.output_item.added":
                        item = event.get("item", {})
                        if item.get("type") == "function_call":
                            item_id = item.get("id", "")
                            pending_calls[item_id] = {
                                "name": item.get("name", ""),
                                "arguments": "",
                            }

                    elif event_type == "response.function_call_arguments.delta":
                        call_id = event.get("item_id", "")
                        if call_id in pending_calls:
                            pending_calls[call_id]["arguments"] += event.get("delta", "")

                    elif event_type == "response.function_call_arguments.done":
                        call_id = event.get("item_id", "")
                        if call_id in pending_calls:
                            call_data = pending_calls.pop(call_id)
                            args = call_data["arguments"]
                            try:
                                args = json.loads(args)
                            except (json.JSONDecodeError, ValueError):
                                args = {"raw": args}
                            tool_calls.append(
                                ToolCallRequest(
                                    id=call_id,
                                    name=call_data["name"],
                                    arguments=args,
                                )
                            )

                    elif event_type == "response.completed":
                        response = event.get("response", {})
                        resp_usage = response.get("usage", {})
                        if resp_usage:
                            usage = {
                                "prompt_tokens": resp_usage.get("input_tokens", 0),
                                "completion_tokens": resp_usage.get("output_tokens", 0),
                                "total_tokens": resp_usage.get("total_tokens", 0),
                            }

        if tool_calls:
            finish_reason = "tool_calls"

        return LLMResponse(
            content="".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )

    def _build_system_instructions(self, messages: list[dict[str, Any]]) -> str | None:
        """Collect system messages into a single developer instruction string."""
        system_parts: list[str] = []
        for msg in messages:
            if msg.get("role") == "system":
                content = msg.get("content")
                system_parts.append(content if isinstance(content, str) else str(content))
        return "\n\n".join(system_parts) if system_parts else None

    def _split_codex_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split full messages into prior context and the pending user turn."""
        non_system = [msg for msg in messages if msg.get("role") != "system"]
        if not non_system:
            return [], []

        pending_start = len(non_system)
        for idx in range(len(non_system) - 1, -1, -1):
            if non_system[idx].get("role") == "user":
                pending_start = idx
                continue
            break

        history_messages = non_system[:pending_start]
        current_messages = non_system[pending_start:]
        if current_messages and all(msg.get("role") == "user" for msg in current_messages):
            return history_messages, current_messages
        return non_system, []

    def _convert_codex_user_input(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Convert pending user messages into Codex app-server UserInput items."""
        items: list[dict[str, Any]] = []
        for message in messages:
            if message.get("role") != "user":
                continue
            items.extend(_format_codex_user_input(message.get("content")))
        return items

    def _build_request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        reasoning_level: str | None,
        lightning_mode: bool | None = None,
    ) -> dict[str, Any]:
        """Build the Responses API request body for the raw OAuth path."""
        _ = lightning_mode
        instructions, input_items = self._convert_messages(messages)
        reasoning = resolve_reasoning(model, reasoning_level)

        body: dict[str, Any] = {
            "model": model,
            "instructions": instructions or "You are a helpful assistant.",
            "input": input_items,
            "stream": True,
            "store": False,
        }

        if tools:
            body["tools"] = self._convert_tools(tools)
        if reasoning.openai_reasoning:
            body["reasoning"] = reasoning.openai_reasoning

        return body

    @staticmethod
    def _convert_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert OpenAI chat-format messages to Responses API input items."""
        system_parts: list[str] = []
        items: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")

            if role == "system":
                system_parts.append(content if isinstance(content, str) else str(content))

            elif role == "user":
                items.append({
                    "role": "user",
                    "content": _format_content(content),
                })

            elif role == "assistant":
                if content:
                    items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    })
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    args = fn.get("arguments", {})
                    if isinstance(args, dict):
                        args = json.dumps(args)
                    call_id = tc.get("id", "")
                    items.append({
                        "type": "function_call",
                        "call_id": call_id,
                        "name": fn.get("name", ""),
                        "arguments": args,
                    })

            elif role == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": _format_tool_output(content),
                })

        instructions = "\n\n".join(system_parts) if system_parts else None
        return instructions, items

    def _build_codex_turn_text(self, messages: list[dict[str, Any]]) -> str:
        """Serialize chat history as a one-time compatibility bootstrap note."""
        transcript: list[str] = [
            "Conversation before Lightning Mode activation:",
            "",
            "Prior transcript:",
        ]

        for message in messages:
            role = message.get("role")
            if role == "system":
                continue
            if role == "user":
                transcript.append(f"User: {_content_to_text(message.get('content'))}")
                continue
            if role == "assistant":
                content = _content_to_text(message.get("content"))
                if content:
                    transcript.append(f"Assistant: {content}")
                for tc in message.get("tool_calls", []):
                    fn = tc.get("function", {})
                    transcript.append(
                        "Assistant tool call: "
                        f"{fn.get('name', '')}({fn.get('arguments', '{}')})"
                    )
                continue
            if role == "tool":
                transcript.append(
                    f"Tool {message.get('name', '')}: {_content_to_text(message.get('content'))}"
                )

        return "\n".join(transcript).strip()

    def _build_codex_bootstrap_text(self, history_messages: list[dict[str, Any]]) -> str:
        """Build a legacy-history bootstrap note for the first fast turn in a session."""
        if not history_messages:
            return ""
        return self._build_codex_turn_text(history_messages)

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI tool definitions to Responses API function tools."""
        result = []
        for tool in tools:
            fn = tool.get("function", {})
            result.append({
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return result

    @staticmethod
    def _convert_dynamic_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """Convert OpenAI function tools to Codex app-server dynamic tools."""
        if not tools:
            return []

        dynamic_tools: list[dict[str, Any]] = []
        for tool in tools:
            fn = tool.get("function", {})
            dynamic_tools.append(
                {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "inputSchema": fn.get(
                        "parameters",
                        {"type": "object", "properties": {}},
                    ),
                }
            )
        return dynamic_tools

    def get_default_model(self) -> str:
        return self.default_model


def _strip_provider_prefix(model: str) -> str:
    if model.startswith("openai/"):
        return model[len("openai/"):]
    return model


def _clean_codex_stderr(text: str) -> str:
    """Remove known benign Codex warnings from stderr tails."""
    lines = text.splitlines(keepends=True)
    cleaned: list[str] = []
    skip_project_config_block = False

    for line in lines:
        if skip_project_config_block:
            if not line.strip():
                skip_project_config_block = False
            continue

        if "Project config.toml files are disabled in the following folders." in line:
            skip_project_config_block = True
            continue
        if "state db backfill not complete" in line:
            continue
        if "Failed to delete shell snapshot" in line:
            continue

        cleaned.append(line)

    return "".join(cleaned)


def _should_retry_codex_fast_response(response: LLMResponse) -> bool:
    """Retry only for early transport flakes before any tool side effects happened."""
    if response.finish_reason != "error" or response.executed_tool_calls:
        return False
    content = (response.content or "").lower()
    return (
        "codex fast transport closed unexpectedly" in content
        or "codex fast transport startup timed out" in content
    )


def _should_retry_codex_fast_exception(exc: Exception) -> bool:
    """Retry transient app-server setup/transport failures."""
    content = str(exc).lower()
    return (
        "connection closed" in content
        or "read failed" in content
        or "not running" in content
    )


async def _maybe_close_detail(proc: Any) -> str:
    """Best-effort close detail extraction for tests and real app-server processes."""
    close_detail = getattr(proc, "close_detail", None)
    if close_detail is None:
        return ""
    return await close_detail()


def _format_content(content: Any) -> Any:
    """Format user message content for Responses API."""
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append({"type": "input_text", "text": block})
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append({"type": "input_text", "text": block.get("text", "")})
                elif block.get("type") == "image_url":
                    url = block.get("image_url", {}).get("url", "")
                    parts.append({"type": "input_image", "image_url": url})
        return parts or content

    return str(content)


def _format_codex_user_input(content: Any) -> list[dict[str, Any]]:
    """Format user content as Codex app-server UserInput items."""
    if isinstance(content, str):
        return [{"type": "text", "text": content, "textElements": []}]

    if isinstance(content, list):
        parts: list[dict[str, Any]] = []
        for block in content:
            if isinstance(block, str):
                parts.append({"type": "text", "text": block, "textElements": []})
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append({
                        "type": "text",
                        "text": block.get("text", ""),
                        "textElements": [],
                    })
                elif block.get("type") == "image_url":
                    url = block.get("image_url", {}).get("url", "")
                    if url:
                        parts.append({"type": "image", "url": url})
        return parts

    return [{"type": "text", "text": str(content), "textElements": []}]


def _dynamic_tool_signature(dynamic_tools: list[dict[str, Any]]) -> str:
    """Build a stable signature for the current dynamic tool surface."""
    return json.dumps(dynamic_tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _format_tool_output(content: Any) -> Any:
    """Format tool output for Responses API function_call_output."""
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        has_images = any(
            isinstance(block, dict) and block.get("type") == "image_url"
            for block in content
        )
        if not has_images:
            texts = []
            for block in content:
                if isinstance(block, str):
                    texts.append(block)
                elif isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
            return "\n".join(texts) if texts else str(content)

        parts: list[dict[str, Any]] = []
        for block in content:
            if isinstance(block, str):
                parts.append({"type": "input_text", "text": block})
            elif isinstance(block, dict):
                if block.get("type") == "image_url":
                    url = block.get("image_url", {}).get("url", "")
                    parts.append({"type": "input_image", "image_url": url})
                elif block.get("type") == "text":
                    parts.append({"type": "input_text", "text": block.get("text", "")})
        return parts or str(content)

    return str(content)


def _format_dynamic_tool_output(content: Any) -> list[dict[str, str]]:
    """Convert a Ragnarbot tool result into Codex dynamic tool content items."""
    if isinstance(content, str):
        return [{"type": "inputText", "text": content}]

    if isinstance(content, list):
        items: list[dict[str, str]] = []
        for block in content:
            if isinstance(block, str):
                items.append({"type": "inputText", "text": block})
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    items.append({"type": "inputText", "text": block.get("text", "")})
                elif block.get("type") == "image_url":
                    url = block.get("image_url", {}).get("url", "")
                    if url:
                        items.append({"type": "inputImage", "imageUrl": url})
        if items:
            return items

    return [{"type": "inputText", "text": json.dumps(content, ensure_ascii=False)}]


def _content_to_text(content: Any) -> str:
    """Render mixed content blocks as plain text for Codex transcript bootstrap."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image_url":
                    url = block.get("image_url", {}).get("url", "")
                    if url:
                        parts.append(_image_url_to_text(url))
        return "\n".join(part for part in parts if part)
    return str(content)


def _image_url_to_text(url: str) -> str:
    """Collapse image URLs to compact text markers for Codex transcript bootstrap."""
    if url.startswith("data:"):
        mime = url[5:].split(";", 1)[0] or "image"
        return f"[image omitted: {mime}]"
    if len(url) > 160:
        return f"[image: {url[:120]}...]"
    return f"[image: {url}]"
