"""Tests for the OpenAI ChatGPT OAuth provider."""

import asyncio
import json
import os
import signal
import sys
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import ragnarbot.providers.openai_chatgpt_provider as openai_chatgpt_provider
from ragnarbot.providers.base import LLMResponse
from ragnarbot.providers.openai_chatgpt_provider import (
    CODEX_APP_SERVER_STREAM_LIMIT,
    CODEX_CLI_MANUAL_INSTALL,
    OpenAIChatGPTProvider,
    _clean_codex_stderr,
    _CodexAppServerProcess,
    _image_url_to_text,
    _JSONRPCError,
    get_codex_cli_install_command,
    install_codex_cli,
)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_for_pid_exit(pid: int, timeout: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while _pid_exists(pid):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.02)
    return True


def _force_kill(pid: int | None) -> None:
    if pid is None or not _pid_exists(pid):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class _FakeSSEStream:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeAsyncClient:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def stream(self, *args, **kwargs):
        return _FakeSSEStream(self._lines)


def _sse_lines(events, *, include_done=False):
    lines = [f"data: {json.dumps(event)}" for event in events]
    if include_done:
        lines.append("data: [DONE]")
    return lines


async def _parse_raw_sse(events, *, include_done=False):
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()
    client = _FakeAsyncClient(_sse_lines(events, include_done=include_done))
    with patch(
        "ragnarbot.providers.openai_chatgpt_provider.httpx.AsyncClient",
        return_value=client,
    ):
        return await provider._stream_request({}, {})


def test_openai_chatgpt_provider_defaults_to_gpt_5_4():
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    assert provider.default_model == "gpt-5.4"


def test_openai_chatgpt_provider_raw_request_skips_service_tier_for_lightning():
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    body = provider._build_request(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model="gpt-5.4",
        reasoning_level="medium",
        lightning_mode=True,
    )

    assert "service_tier" not in body


def test_clean_codex_stderr_filters_project_config_warning_block():
    raw = (
        "2026-03-20T20:28:58.062252Z ERROR codex_app_server: "
        "Project config.toml files are disabled in the following folders. "
        "Settings in those files are ignored, but skills and exec policies still load.\n"
        "    1. /Users/lvls/ragnarbot/.codex\n"
        "       To load config.toml, add /Users/lvls/ragnarbot as a trusted project.\n"
        "\n"
        "2026-03-20T20:29:01.111715Z  WARN codex_core::state_db: "
        "state db backfill not complete at /Users/lvls/.ragnarbot/codex-oauth-fast "
        "(status: pending)\n"
        "2026-03-20T20:29:02.111715Z  WARN codex_core::shell_snapshot: "
        "Failed to delete shell snapshot at \"/tmp/example\": "
        "Os { code: 2, kind: NotFound, message: \"No such file or directory\" }\n"
        "2026-03-20T20:29:44.111715Z ERROR codex_app_server: actual crash\n"
    )

    cleaned = _clean_codex_stderr(raw)

    assert "Project config.toml files are disabled" not in cleaned
    assert "To load config.toml" not in cleaned
    assert "state db backfill not complete" not in cleaned
    assert "Failed to delete shell snapshot" not in cleaned
    assert "actual crash" in cleaned


def test_content_to_text_collapses_data_image_urls():
    marker = _image_url_to_text("data:image/png;base64," + ("A" * 200000))

    assert marker == "[image omitted: image/png]"
    assert len(marker.encode("utf-8")) < 64


def test_content_to_text_truncates_very_long_remote_image_urls():
    url = "https://example.com/" + ("x" * 500)

    marker = _image_url_to_text(url)

    assert marker.startswith("[image: https://example.com/")
    assert marker.endswith("...]")
    assert len(marker) < 140


def test_find_codex_cli_bin_uses_common_install_locations_when_path_is_stale():
    with (
        patch("shutil.which", side_effect=[None, None, None]),
        patch(
            "ragnarbot.providers.openai_chatgpt_provider._is_executable_file",
            side_effect=lambda path: path == "/opt/homebrew/bin/codex",
        ),
    ):
        assert (
            openai_chatgpt_provider.find_codex_cli_bin() == "/opt/homebrew/bin/codex"
        )


def test_find_codex_cli_bin_checks_npm_global_prefix_when_needed():
    def fake_run(command):
        if command[0] == "/usr/bin/npm":
            return "/Users/test/.npm-global"
        return None

    with (
        patch("shutil.which", side_effect=[None, None, "/usr/bin/npm"]),
        patch("ragnarbot.providers.openai_chatgpt_provider._run_probe_command", side_effect=fake_run),
        patch(
            "ragnarbot.providers.openai_chatgpt_provider._is_executable_file",
            side_effect=lambda path: path == "/Users/test/.npm-global/bin/codex",
        ),
    ):
        assert (
            openai_chatgpt_provider.find_codex_cli_bin() == "/Users/test/.npm-global/bin/codex"
        )


@pytest.mark.asyncio
async def test_codex_app_server_start_uses_large_stream_limit(tmp_path):
    class FakeStdin:
        def write(self, _data):
            return None

        async def drain(self):
            return None

    class FakeReader:
        async def readline(self):
            return b""

    class FakeProc:
        stdin = FakeStdin()
        stdout = FakeReader()
        stderr = FakeReader()
        returncode = 0

    proc = _CodexAppServerProcess(codex_home=tmp_path)

    with (
        patch("shutil.which", return_value="/usr/local/bin/codex"),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=FakeProc())) as spawn,
        patch.object(proc, "call", AsyncMock(return_value={"ok": True})),
        patch.object(proc, "notify", AsyncMock()),
    ):
        await proc.start()

    assert spawn.await_args.kwargs["limit"] == CODEX_APP_SERVER_STREAM_LIMIT


@pytest.mark.asyncio
async def test_provider_routes_to_codex_fast_only_for_gpt_5_4_lightning():
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    raw = AsyncMock(return_value=LLMResponse(content="raw"))
    fast = AsyncMock(return_value=LLMResponse(content="fast"))

    with (
        patch("ragnarbot.auth.openai_oauth.get_access_token", return_value="token"),
        patch("ragnarbot.providers.openai_chatgpt_provider.is_codex_cli_available", return_value=True),
        patch.object(provider, "_chat_raw", raw),
        patch.object(provider, "_chat_codex_fast", fast),
    ):
        response = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="openai/gpt-5.4",
            lightning_mode=True,
            session_key="telegram:123",
            tool_runner=AsyncMock(return_value="ok"),
        )

    assert response.content == "fast"
    fast.assert_awaited_once()
    raw.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_keeps_raw_path_when_codex_cli_missing():
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    raw = AsyncMock(return_value=LLMResponse(content="raw"))
    fast = AsyncMock(return_value=LLMResponse(content="fast"))

    with (
        patch("ragnarbot.auth.openai_oauth.get_access_token", return_value="token"),
        patch("ragnarbot.providers.openai_chatgpt_provider.is_codex_cli_available", return_value=False),
        patch.object(provider, "_chat_raw", raw),
        patch.object(provider, "_chat_codex_fast", fast),
    ):
        response = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="openai/gpt-5.4",
            lightning_mode=True,
            session_key="telegram:123",
            tool_runner=AsyncMock(return_value="ok"),
        )

    assert response.content == "raw"
    raw.assert_awaited_once()
    fast.assert_not_awaited()


def test_get_codex_cli_install_command_prefers_brew_on_macos():
    with (
        patch("ragnarbot.providers.openai_chatgpt_provider.sys.platform", "darwin"),
        patch("shutil.which", side_effect=["/opt/homebrew/bin/brew", "/opt/homebrew/bin/npm"]),
    ):
        command = get_codex_cli_install_command()

    assert command == (
        ["/opt/homebrew/bin/brew", "install", "--cask", "codex"],
        "brew install --cask codex",
    )


def test_get_codex_cli_install_command_falls_back_to_npm():
    with (
        patch("ragnarbot.providers.openai_chatgpt_provider.sys.platform", "linux"),
        patch("shutil.which", side_effect=[None, "/usr/bin/npm"]),
    ):
        command = get_codex_cli_install_command()

    assert command == (
        ["/usr/bin/npm", "install", "-g", "@openai/codex"],
        CODEX_CLI_MANUAL_INSTALL,
    )


@pytest.mark.asyncio
async def test_install_codex_cli_reports_manual_step_when_no_installer():
    with (
        patch("ragnarbot.providers.openai_chatgpt_provider.is_codex_cli_available", return_value=False),
        patch("ragnarbot.providers.openai_chatgpt_provider.get_codex_cli_install_command", return_value=None),
    ):
        ok, detail = await install_codex_cli()

    assert ok is False
    assert CODEX_CLI_MANUAL_INSTALL in detail


@pytest.mark.asyncio
async def test_install_codex_cli_runs_detected_installer():
    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"installed", b"")

    with (
        patch("ragnarbot.providers.openai_chatgpt_provider.is_codex_cli_available", side_effect=[False, True]),
        patch(
            "ragnarbot.providers.openai_chatgpt_provider.get_codex_cli_install_command",
            return_value=(["/usr/bin/npm", "install", "-g", "@openai/codex"], CODEX_CLI_MANUAL_INSTALL),
        ),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=FakeProc())) as spawn,
    ):
        ok, detail = await install_codex_cli()

    assert ok is True
    assert detail == CODEX_CLI_MANUAL_INSTALL
    spawn.assert_awaited_once()
    assert spawn.await_args.kwargs["stdin"] == asyncio.subprocess.DEVNULL
    if os.name == "posix":
        assert spawn.await_args.kwargs["start_new_session"] is True


@pytest.mark.asyncio
async def test_install_codex_cli_reports_path_issue_after_successful_install():
    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"installed", b"")

    with (
        patch("ragnarbot.providers.openai_chatgpt_provider.is_codex_cli_available", side_effect=[False, False]),
        patch(
            "ragnarbot.providers.openai_chatgpt_provider.get_codex_cli_install_command",
            return_value=(["/usr/bin/npm", "install", "-g", "@openai/codex"], CODEX_CLI_MANUAL_INSTALL),
        ),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=FakeProc())),
    ):
        ok, detail = await install_codex_cli()

    assert ok is False
    assert "still not on PATH" in detail
    assert CODEX_CLI_MANUAL_INSTALL in detail


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
@pytest.mark.asyncio
async def test_install_codex_cli_timeout_kills_installer_descendants(tmp_path, monkeypatch):
    monkeypatch.setattr(
        openai_chatgpt_provider, "CODEX_CLI_INSTALL_TIMEOUT_SECONDS", 0.5,
    )
    pid_path = tmp_path / "codex-installer-child.pid"
    parent_code = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "time.sleep(30)"
    )
    child_pid = None
    with (
        patch(
            "ragnarbot.providers.openai_chatgpt_provider.is_codex_cli_available",
            return_value=False,
        ),
        patch(
            "ragnarbot.providers.openai_chatgpt_provider.get_codex_cli_install_command",
            return_value=(
                [sys.executable, "-c", parent_code, str(pid_path)],
                CODEX_CLI_MANUAL_INSTALL,
            ),
        ),
    ):
        try:
            ok, detail = await install_codex_cli()

            child_pid = int(pid_path.read_text())
            assert ok is False
            assert "timed out" in detail
            assert await _wait_for_pid_exit(child_pid), "timeout left installer child alive"
        finally:
            _force_kill(child_pid)


@pytest.mark.asyncio
async def test_install_codex_cli_cancellation_cleans_process_tree():
    class FakeProc:
        pid = 1234
        returncode = None

        async def communicate(self):
            raise asyncio.CancelledError

    terminate = AsyncMock()
    proc = FakeProc()
    with (
        patch(
            "ragnarbot.providers.openai_chatgpt_provider.is_codex_cli_available",
            return_value=False,
        ),
        patch(
            "ragnarbot.providers.openai_chatgpt_provider.get_codex_cli_install_command",
            return_value=(["installer"], "manual installer"),
        ),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        patch(
            "ragnarbot.providers.openai_chatgpt_provider.terminate_process_tree", terminate,
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await install_codex_cli()

    terminate.assert_awaited_once_with(proc)


@pytest.mark.asyncio
async def test_install_codex_cli_error_cleans_process_tree_and_reports_manual_step():
    class FakeProc:
        pid = 1234
        returncode = None

        async def communicate(self):
            raise RuntimeError("installer transport failed")

    terminate = AsyncMock()
    proc = FakeProc()
    with (
        patch(
            "ragnarbot.providers.openai_chatgpt_provider.is_codex_cli_available",
            return_value=False,
        ),
        patch(
            "ragnarbot.providers.openai_chatgpt_provider.get_codex_cli_install_command",
            return_value=(["installer"], "manual installer"),
        ),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        patch(
            "ragnarbot.providers.openai_chatgpt_provider.terminate_process_tree", terminate,
        ),
    ):
        ok, detail = await install_codex_cli()

    assert ok is False
    assert "installer transport failed" in detail
    assert "manual installer" in detail
    terminate.assert_awaited_once_with(proc)


@pytest.mark.asyncio
async def test_provider_keeps_raw_path_when_lightning_is_off():
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    raw = AsyncMock(return_value=LLMResponse(content="raw"))
    fast = AsyncMock(return_value=LLMResponse(content="fast"))

    with (
        patch("ragnarbot.auth.openai_oauth.get_access_token", return_value="token"),
        patch.object(provider, "_chat_raw", raw),
        patch.object(provider, "_chat_codex_fast", fast),
    ):
        response = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="openai/gpt-5.4",
            lightning_mode=False,
            session_key="telegram:123",
            tool_runner=AsyncMock(return_value="ok"),
        )

    assert response.content == "raw"
    raw.assert_awaited_once()
    fast.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_keeps_raw_path_for_non_gpt_5_4_models():
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    raw = AsyncMock(return_value=LLMResponse(content="raw"))
    fast = AsyncMock(return_value=LLMResponse(content="fast"))

    with (
        patch("ragnarbot.auth.openai_oauth.get_access_token", return_value="token"),
        patch.object(provider, "_chat_raw", raw),
        patch.object(provider, "_chat_codex_fast", fast),
    ):
        response = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="openai/gpt-5.4-mini",
            lightning_mode=True,
            session_key="telegram:123",
            tool_runner=AsyncMock(return_value="ok"),
        )

    assert response.content == "raw"
    raw.assert_awaited_once()
    fast.assert_not_awaited()


@pytest.mark.asyncio
async def test_raw_transport_retries_once_after_empty_read_error():
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    request = httpx.Request("POST", openai_chatgpt_provider.RESPONSES_URL)
    stream_request = AsyncMock(side_effect=[
        httpx.ReadError("", request=request),
        LLMResponse(content="ok"),
    ])

    with (
        patch("ragnarbot.auth.openai_oauth.get_access_token", return_value="token"),
        patch.object(provider, "_stream_request", stream_request),
        patch("ragnarbot.providers.openai_chatgpt_provider.asyncio.sleep", AsyncMock()) as sleep,
    ):
        response = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="openai/gpt-5.5",
        )

    assert response.content == "ok"
    assert stream_request.await_count == 2
    sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_raw_transport_exhaustion_has_non_empty_exception_type():
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    request = httpx.Request("POST", openai_chatgpt_provider.RESPONSES_URL)
    stream_request = AsyncMock(side_effect=[
        httpx.ReadError("", request=request),
        httpx.ReadError("", request=request),
    ])

    with (
        patch("ragnarbot.auth.openai_oauth.get_access_token", return_value="token"),
        patch.object(provider, "_stream_request", stream_request),
        patch("ragnarbot.providers.openai_chatgpt_provider.asyncio.sleep", AsyncMock()),
    ):
        response = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="openai/gpt-5.5",
        )

    assert response.finish_reason == "error"
    assert response.content == "Error calling OpenAI ChatGPT API: ReadError"
    assert stream_request.await_count == 2


@pytest.mark.asyncio
async def test_raw_transport_logs_never_include_bearer_token_or_traceback():
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    token = "oauth-sentinel.secret-token"
    request = httpx.Request(
        "POST",
        openai_chatgpt_provider.RESPONSES_URL,
        headers={"Authorization": f"Bearer {token}"},
    )
    error_message = f"Authorization: Bearer {token}"
    stream_request = AsyncMock(side_effect=[
        httpx.ReadError(error_message, request=request),
        httpx.ReadError(error_message, request=request),
    ])
    log_records = []
    sink_id = openai_chatgpt_provider.logger.add(
        lambda message: log_records.append(message.record),
    )

    try:
        with (
            patch("ragnarbot.auth.openai_oauth.get_access_token", return_value=token),
            patch.object(provider, "_stream_request", stream_request),
            patch("ragnarbot.providers.openai_chatgpt_provider.asyncio.sleep", AsyncMock()),
        ):
            response = await provider.chat(
                messages=[{"role": "user", "content": "hi"}],
                model="openai/gpt-5.5",
            )
    finally:
        openai_chatgpt_provider.logger.remove(sink_id)

    rendered_logs = "\n".join(record["message"] for record in log_records)
    assert token not in rendered_logs
    assert token not in (response.content or "")
    assert "[REDACTED]" in rendered_logs
    assert all(record["exception"] is None for record in log_records)


@pytest.mark.asyncio
async def test_raw_transport_retries_transient_http_status_only():
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    request = httpx.Request("POST", openai_chatgpt_provider.RESPONSES_URL)
    unavailable = httpx.Response(503, request=request)
    transient = httpx.HTTPStatusError(
        "Server error '503 Service Unavailable'",
        request=request,
        response=unavailable,
    )
    stream_request = AsyncMock(side_effect=[transient, LLMResponse(content="ok")])

    with (
        patch("ragnarbot.auth.openai_oauth.get_access_token", return_value="token"),
        patch.object(provider, "_stream_request", stream_request),
        patch("ragnarbot.providers.openai_chatgpt_provider.asyncio.sleep", AsyncMock()),
    ):
        response = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="openai/gpt-5.5",
        )

    assert response.content == "ok"
    assert stream_request.await_count == 2


@pytest.mark.asyncio
async def test_raw_transport_does_not_retry_auth_status():
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    request = httpx.Request("POST", openai_chatgpt_provider.RESPONSES_URL)
    unauthorized = httpx.Response(401, request=request)
    error = httpx.HTTPStatusError(
        "Client error '401 Unauthorized'",
        request=request,
        response=unauthorized,
    )
    stream_request = AsyncMock(side_effect=error)

    with (
        patch("ragnarbot.auth.openai_oauth.get_access_token", return_value="token"),
        patch.object(provider, "_stream_request", stream_request),
    ):
        response = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="openai/gpt-5.5",
        )

    assert response.finish_reason == "error"
    assert "HTTPStatusError" in (response.content or "")
    assert "401 Unauthorized" in (response.content or "")
    assert stream_request.await_count == 1


@pytest.mark.asyncio
async def test_raw_sse_completed_preserves_text_tools_and_usage_without_done_marker():
    response = await _parse_raw_sse([
        {"type": "response.output_text.delta", "delta": "Hello"},
        {
            "type": "response.output_item.added",
            "item": {"type": "function_call", "id": "item_1", "name": "lookup"},
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item_1",
            "delta": '{"query":"Oslo"}',
        },
        {"type": "response.function_call_arguments.done", "item_id": "item_1"},
        {
            "type": "response.completed",
            "response": {
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            },
        },
    ])

    assert response.content == "Hello"
    assert response.finish_reason == "tool_calls"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id == "item_1"
    assert response.tool_calls[0].name == "lookup"
    assert response.tool_calls[0].arguments == {"query": "Oslo"}
    assert response.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }


@pytest.mark.asyncio
async def test_raw_sse_failed_returns_safe_error_and_discards_partial_output():
    token = "sk-proj-sentinel-secret-token"
    response = await _parse_raw_sse([
        {"type": "response.output_text.delta", "delta": "partial answer"},
        {
            "type": "response.output_item.added",
            "item": {"type": "function_call", "id": "item_1", "name": "dangerous_tool"},
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item_1",
            "delta": '{"partial":true',
        },
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "code": "server_error",
                    "message": f"Authorization: Bearer {token}",
                },
                "usage": {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10},
            },
        },
    ])

    assert response.finish_reason == "error"
    assert response.tool_calls == []
    assert "response.failed" in (response.content or "")
    assert "server_error" in (response.content or "")
    assert "partial answer" not in (response.content or "")
    assert token not in (response.content or "")
    assert "[REDACTED]" in (response.content or "")
    assert response.usage["total_tokens"] == 10


@pytest.mark.asyncio
async def test_raw_sse_max_tokens_preserves_partial_text_but_drops_tools():
    response = await _parse_raw_sse([
        {"type": "response.output_text.delta", "delta": "useful partial answer"},
        {
            "type": "response.output_item.added",
            "item": {"type": "function_call", "id": "item_1", "name": "do_not_run"},
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item_1",
            "delta": "{}",
        },
        {"type": "response.function_call_arguments.done", "item_id": "item_1"},
        {
            "type": "response.incomplete",
            "response": {
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {"input_tokens": 12, "output_tokens": 20, "total_tokens": 32},
            },
        },
    ])

    assert response.finish_reason == "length"
    assert response.content == "useful partial answer"
    assert response.tool_calls == []
    assert response.usage["total_tokens"] == 32


@pytest.mark.asyncio
async def test_raw_sse_max_tokens_without_text_is_error_for_fallback():
    response = await _parse_raw_sse([
        {"type": "response.output_text.delta", "delta": "   "},
        {
            "type": "response.incomplete",
            "response": {
                "incomplete_details": {"reason": "max_output_tokens"},
            },
        },
    ])

    assert response.finish_reason == "error"
    assert response.tool_calls == []
    assert "reason=max_output_tokens" in (response.content or "")


@pytest.mark.asyncio
async def test_raw_sse_other_incomplete_reason_is_error_and_discards_partial_output():
    response = await _parse_raw_sse([
        {"type": "response.output_text.delta", "delta": "filtered partial"},
        {
            "type": "response.incomplete",
            "response": {
                "incomplete_details": {"reason": "content_filter"},
            },
        },
    ])

    assert response.finish_reason == "error"
    assert response.tool_calls == []
    assert "reason=content_filter" in (response.content or "")
    assert "filtered partial" not in (response.content or "")


@pytest.mark.asyncio
async def test_raw_sse_completed_with_pending_tool_retries_then_errors_safely():
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    token = "sk-proj-pending-argument-secret"
    events = [
        {
            "type": "response.output_item.added",
            "item": {"type": "function_call", "id": "item_1", "name": "do_not_run"},
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item_1",
            "delta": f'{{"token":"{token}"',
        },
        {"type": "response.completed", "response": {"usage": {"total_tokens": 4}}},
    ]
    client = _FakeAsyncClient(_sse_lines(events))

    with (
        patch("ragnarbot.auth.openai_oauth.get_access_token", return_value="token"),
        patch(
            "ragnarbot.providers.openai_chatgpt_provider.httpx.AsyncClient",
            return_value=client,
        ) as client_constructor,
        patch("ragnarbot.providers.openai_chatgpt_provider.asyncio.sleep", AsyncMock()),
    ):
        response = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="openai/gpt-5.5",
        )

    assert response.finish_reason == "error"
    assert response.tool_calls == []
    assert "OpenAIIncompleteStreamError" in (response.content or "")
    assert "incomplete function-call arguments" in (response.content or "")
    assert token not in (response.content or "")
    assert client_constructor.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("include_done", "ending"),
    [(False, "EOF"), (True, "[DONE]")],
)
async def test_raw_sse_without_terminal_event_retries_then_errors_without_partial_data(
    include_done,
    ending,
):
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    token = "sk-proj-buffered-sentinel-token"
    events = [
        {"type": "response.output_text.delta", "delta": f"partial {token}"},
        {
            "type": "response.output_item.added",
            "item": {"type": "function_call", "id": "item_1", "name": "do_not_run"},
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item_1",
            "delta": "{}",
        },
        {"type": "response.function_call_arguments.done", "item_id": "item_1"},
    ]
    client = _FakeAsyncClient(_sse_lines(events, include_done=include_done))

    with (
        patch("ragnarbot.auth.openai_oauth.get_access_token", return_value="token"),
        patch(
            "ragnarbot.providers.openai_chatgpt_provider.httpx.AsyncClient",
            return_value=client,
        ) as client_constructor,
        patch("ragnarbot.providers.openai_chatgpt_provider.asyncio.sleep", AsyncMock()),
    ):
        response = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="openai/gpt-5.5",
        )

    assert response.finish_reason == "error"
    assert response.tool_calls == []
    assert "OpenAIIncompleteStreamError" in (response.content or "")
    assert ending in (response.content or "")
    assert token not in (response.content or "")
    assert client_constructor.call_count == 2


@pytest.mark.asyncio
async def test_codex_fast_transport_handles_dynamic_tool_round_trip(tmp_path):
    tool_runner = AsyncMock(return_value="Ticket ABC-123 is open.")
    tool_call_handler = AsyncMock()
    text_delta_handler = AsyncMock()

    class FakeProc:
        instances = []

        def __init__(self, codex_home):
            self.codex_home = codex_home
            self.calls = []
            self.responses = []
            self.sent_results = []
            self.closed = False
            self.stderr_text = ""
            FakeProc.instances.append(self)

        async def start(self):
            return None

        async def close(self):
            self.closed = True

        async def call(self, method, params=None):
            self.calls.append((method, params))
            if method == "account/login/start":
                return {"type": "chatgptAuthTokens"}
            if method == "thread/start":
                return {"thread": {"id": "thr_123"}}
            if method == "turn/start":
                self.responses = [
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thr_123",
                            "turnId": "turn_123",
                            "delta": "Checking ticket.",
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 60,
                        "method": "item/tool/call",
                        "params": {
                            "threadId": "thr_123",
                            "turnId": "turn_123",
                            "callId": "call_123",
                            "tool": "lookup_ticket",
                            "arguments": {"id": "ABC-123"},
                        },
                    },
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thr_123",
                            "turnId": "turn_123",
                            "delta": "Done.",
                        },
                    },
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thr_123",
                            "turn": {
                                "id": "turn_123",
                                "status": "completed",
                                "items": [],
                                "error": None,
                            },
                        },
                    },
                ]
                return {"turn": {"id": "turn_123", "status": "inProgress", "items": []}}
            raise AssertionError(f"Unexpected method: {method}")

        async def next_message(self):
            return self.responses.pop(0)

        async def respond(self, request_id, result):
            self.sent_results.append((request_id, result))

        async def respond_error(self, request_id, *, message, code=-32000, data=None):
            raise AssertionError(f"Unexpected error response: {request_id} {message}")

    with (
        patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"),
        patch("ragnarbot.auth.openai_oauth.get_access_token", return_value="token"),
        patch(
            "ragnarbot.providers.openai_chatgpt_provider._CodexAppServerProcess",
            FakeProc,
        ),
    ):
        provider = OpenAIChatGPTProvider()
        provider._codex_home = tmp_path
        provider._thread_registry_path = tmp_path / "thread_registry.json"
        response = await provider.chat(
            messages=[
                {"role": "system", "content": "Use tools carefully."},
                {"role": "user", "content": "Look up ABC-123 with the tool."},
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "lookup_ticket",
                        "description": "Fetch a ticket by id",
                        "parameters": {
                            "type": "object",
                            "properties": {"id": {"type": "string"}},
                            "required": ["id"],
                        },
                    },
                }
            ],
            model="openai/gpt-5.4",
            reasoning_level="ultra",
            lightning_mode=True,
            session_key="telegram:123",
            tool_runner=tool_runner,
            tool_call_handler=tool_call_handler,
            text_delta_handler=text_delta_handler,
        )

    proc = FakeProc.instances[0]
    assert response.content == "Done."
    assert len(response.executed_tool_calls) == 1
    executed = response.executed_tool_calls[0]
    assert executed.id == "call_123"
    assert executed.name == "lookup_ticket"
    assert executed.arguments == {"id": "ABC-123"}
    assert executed.result == "Ticket ABC-123 is open."
    assert executed.metadata["trace_emitted"] is True
    assert executed.assistant_content == "Checking ticket."

    tool_call = tool_runner.await_args.args[0]
    assert tool_call.name == "lookup_ticket"
    assert tool_call.arguments == {"id": "ABC-123"}
    assert tool_call_handler.await_args.args[0].name == "lookup_ticket"
    assert text_delta_handler.await_count == 1
    assert text_delta_handler.await_args.args[0] == "Checking ticket."

    thread_start = dict(proc.calls)["thread/start"]
    assert thread_start["serviceTier"] == "fast"
    assert thread_start["dynamicTools"][0]["name"] == "lookup_ticket"

    turn_start = dict(proc.calls)["turn/start"]
    assert turn_start["serviceTier"] == "fast"
    assert turn_start["effort"] == "xhigh"
    assert turn_start["input"][0]["type"] == "text"
    assert "Look up ABC-123 with the tool." in turn_start["input"][0]["text"]

    assert proc.sent_results == [
        (
            60,
            {
                "contentItems": [{"type": "inputText", "text": "Ticket ABC-123 is open."}],
                "success": True,
            },
        )
    ]
    assert proc.closed is True


@pytest.mark.asyncio
async def test_codex_fast_first_turn_bootstraps_history_but_keeps_current_image_structured(tmp_path):
    class FakeProc:
        instances = []

        def __init__(self, codex_home):
            self.codex_home = codex_home
            self.calls = []
            self.responses = []
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
                return {"thread": {"id": "thr_bootstrap"}}
            if method == "turn/start":
                self.responses = [
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thr_bootstrap",
                            "turnId": "turn_bootstrap",
                            "delta": "vision ok",
                        },
                    },
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thr_bootstrap",
                            "turn": {"id": "turn_bootstrap", "status": "completed"},
                        },
                    },
                ]
                return {"turn": {"id": "turn_bootstrap", "status": "inProgress", "items": []}}
            raise AssertionError(f"Unexpected method: {method}")

        async def next_message(self):
            return self.responses.pop(0)

        async def respond(self, request_id, result):
            raise AssertionError(f"Unexpected response: {request_id} {result}")

        async def respond_error(self, request_id, *, message, code=-32000, data=None):
            raise AssertionError(f"Unexpected error response: {request_id} {message}")

    with (
        patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"),
        patch("ragnarbot.auth.openai_oauth.get_access_token", return_value="token"),
        patch("ragnarbot.providers.openai_chatgpt_provider._CodexAppServerProcess", FakeProc),
    ):
        provider = OpenAIChatGPTProvider()
        provider._codex_home = tmp_path
        provider._thread_registry_path = tmp_path / "thread_registry.json"
        response = await provider.chat(
            messages=[
                {"role": "system", "content": "Be helpful."},
                {"role": "user", "content": "Previous question."},
                {"role": "assistant", "content": "Previous answer."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What color is this?"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAAA"},
                        },
                    ],
                },
            ],
            tools=[],
            model="openai/gpt-5.4",
            reasoning_level="medium",
            lightning_mode=True,
            session_key="telegram:vision",
            tool_runner=AsyncMock(return_value="ok"),
        )

    proc = FakeProc.instances[0]
    turn_start = dict(proc.calls)["turn/start"]

    assert response.content == "vision ok"
    assert turn_start["input"][0]["type"] == "text"
    assert "Previous question." in turn_start["input"][0]["text"]
    assert turn_start["input"][1] == {
        "type": "text",
        "text": "What color is this?",
        "textElements": [],
    }
    assert turn_start["input"][2] == {
        "type": "image",
        "url": "data:image/png;base64,AAAA",
    }


@pytest.mark.asyncio
async def test_codex_fast_reuses_thread_for_same_session(tmp_path):
    class FakeProc:
        instances = []

        def __init__(self, codex_home):
            self.codex_home = codex_home
            self.calls = []
            self.responses = []
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
                return {"thread": {"id": "thr_reuse"}}
            if method == "thread/resume":
                return {"thread": {"id": "thr_reuse"}}
            if method == "turn/start":
                turn_id = f"turn_{len(self.calls)}"
                self.responses = [
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thr_reuse",
                            "turnId": turn_id,
                            "delta": "ok",
                        },
                    },
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thr_reuse",
                            "turn": {"id": turn_id, "status": "completed"},
                        },
                    },
                ]
                return {"turn": {"id": turn_id, "status": "inProgress", "items": []}}
            raise AssertionError(f"Unexpected method: {method}")

        async def next_message(self):
            return self.responses.pop(0)

        async def respond(self, request_id, result):
            raise AssertionError(f"Unexpected response: {request_id} {result}")

        async def respond_error(self, request_id, *, message, code=-32000, data=None):
            raise AssertionError(f"Unexpected error response: {request_id} {message}")

    with (
        patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"),
        patch("ragnarbot.auth.openai_oauth.get_access_token", return_value="token"),
        patch("ragnarbot.providers.openai_chatgpt_provider._CodexAppServerProcess", FakeProc),
    ):
        provider = OpenAIChatGPTProvider()
        provider._codex_home = tmp_path
        provider._thread_registry_path = tmp_path / "thread_registry.json"

        await provider.chat(
            messages=[{"role": "user", "content": "first"}],
            tools=[],
            model="openai/gpt-5.4",
            lightning_mode=True,
            session_key="telegram:reuse",
            tool_runner=AsyncMock(return_value="ok"),
        )
        await provider.chat(
            messages=[
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "second"},
            ],
            tools=[],
            model="openai/gpt-5.4",
            lightning_mode=True,
            session_key="telegram:reuse",
            tool_runner=AsyncMock(return_value="ok"),
        )

    first_proc, second_proc = FakeProc.instances
    assert "thread/start" in dict(first_proc.calls)
    assert "thread/resume" in dict(second_proc.calls)
    second_turn_start = dict(second_proc.calls)["turn/start"]
    assert second_turn_start["input"] == [{"type": "text", "text": "second", "textElements": []}]


@pytest.mark.asyncio
async def test_codex_fast_closed_message_ignores_project_config_warning():
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    class ClosedProc:
        _stderr_text = (
            "2026-03-20T20:28:58.062252Z ERROR codex_app_server: "
            "Project config.toml files are disabled in the following folders. "
            "Settings in those files are ignored, but skills and exec policies still load.\n"
            "    1. /Users/lvls/ragnarbot/.codex\n"
            "       To load config.toml, add /Users/lvls/ragnarbot as a trusted project.\n"
            "\n"
        )

        @property
        def stderr_text(self):
            return _clean_codex_stderr(self._stderr_text)

        async def next_message(self):
            return {"method": "__closed__"}

    response = await provider._drain_codex_turn(
        proc=ClosedProc(),
        thread_id="thr_123",
        turn_id="turn_123",
        tool_runner=AsyncMock(),
        tool_call_handler=None,
        text_delta_handler=None,
        steering_message_provider=None,
    )

    assert response.finish_reason == "error"
    assert response.content == "Codex Fast transport closed unexpectedly."


@pytest.mark.asyncio
async def test_codex_fast_retries_once_after_transient_closed_response():
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    first = LLMResponse(
        content="Codex Fast transport closed unexpectedly.",
        finish_reason="error",
    )
    second = LLMResponse(content="ok", finish_reason="stop")

    with (
        patch("ragnarbot.auth.openai_oauth.get_access_token", return_value="token"),
        patch.object(
            provider,
            "_chat_codex_fast_once",
            AsyncMock(side_effect=[(first, "exit=1", None), (second, "", None)]),
        ) as fast_once,
    ):
        response = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="openai/gpt-5.4",
            lightning_mode=True,
            session_key="telegram:123",
            tool_runner=AsyncMock(return_value="ok"),
        )

    assert response.content == "ok"
    assert fast_once.await_count == 2


@pytest.mark.asyncio
async def test_codex_fast_retries_once_after_transient_setup_exception():
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    second = LLMResponse(content="ok", finish_reason="stop")

    with (
        patch("ragnarbot.auth.openai_oauth.get_access_token", return_value="token"),
        patch.object(
            provider,
            "_chat_codex_fast_once",
            AsyncMock(
                side_effect=[
                    (None, "last_sent=turn/start", _JSONRPCError("Codex app-server connection closed.")),
                    (second, "", None),
                ]
            ),
        ) as fast_once,
    ):
        response = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="openai/gpt-5.4",
            lightning_mode=True,
            session_key="telegram:123",
            tool_runner=AsyncMock(return_value="ok"),
        )

    assert response.content == "ok"
    assert fast_once.await_count == 2


@pytest.mark.asyncio
async def test_codex_fast_startup_timeout_returns_retryable_error(monkeypatch):
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    class SlowProc:
        async def next_message(self):
            await asyncio.sleep(0.05)
            return {"method": "turn/completed", "params": {"turn": {"status": "completed"}}}

    monkeypatch.setattr(
        "ragnarbot.providers.openai_chatgpt_provider.CODEX_FAST_INITIAL_EVENT_TIMEOUT_SECONDS",
        0.01,
    )

    response = await provider._drain_codex_turn(
        proc=SlowProc(),
        thread_id="thr_123",
        turn_id="turn_123",
        tool_runner=AsyncMock(),
        tool_call_handler=None,
        text_delta_handler=None,
        steering_message_provider=None,
    )

    assert response.finish_reason == "error"
    assert response.content == "Codex Fast transport startup timed out."


@pytest.mark.asyncio
async def test_codex_fast_does_not_timeout_early_between_startup_events(monkeypatch):
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    class SlowStartProc:
        def __init__(self):
            self.step = 0

        async def next_message(self):
            self.step += 1
            if self.step == 1:
                return {
                    "method": "thread/status/changed",
                    "params": {
                        "threadId": "thr_123",
                        "status": "running",
                    },
                }
            if self.step == 2:
                await asyncio.sleep(0.03)
                return {
                    "method": "turn/started",
                    "params": {
                        "threadId": "thr_123",
                        "turn": {"id": "turn_123", "status": "inProgress", "items": []},
                    },
                }
            return {
                "method": "turn/completed",
                "params": {
                    "threadId": "thr_123",
                    "turn": {"id": "turn_123", "status": "completed"},
                },
            }

    monkeypatch.setattr(
        "ragnarbot.providers.openai_chatgpt_provider.CODEX_FAST_EVENT_POLL_INTERVAL_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        "ragnarbot.providers.openai_chatgpt_provider.CODEX_FAST_INITIAL_EVENT_TIMEOUT_SECONDS",
        0.1,
    )

    response = await provider._drain_codex_turn(
        proc=SlowStartProc(),
        thread_id="thr_123",
        turn_id="turn_123",
        tool_runner=AsyncMock(),
        tool_call_handler=None,
        text_delta_handler=None,
        steering_message_provider=None,
    )

    assert response.finish_reason == "stop"
    assert response.content is None


@pytest.mark.asyncio
async def test_codex_fast_turn_sends_live_turn_steer(monkeypatch, tmp_path):
    class FakeProc:
        instances = []

        def __init__(self, codex_home):
            self.codex_home = codex_home
            self.calls = []
            self.responses = []
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
                return {"thread": {"id": "thr_steer"}}
            if method == "turn/start":
                self.responses = [
                    {
                        "method": "turn/started",
                        "params": {
                            "threadId": "thr_steer",
                            "turn": {"id": "turn_steer", "status": "inProgress", "items": []},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 90,
                        "method": "item/tool/call",
                        "params": {
                            "threadId": "thr_steer",
                            "turnId": "turn_steer",
                            "callId": "call_steer",
                            "tool": "ping",
                            "arguments": {"value": "go"},
                        },
                    },
                ]
                return {"turn": {"id": "turn_steer", "status": "inProgress", "items": []}}
            if method == "turn/steer":
                self.responses = [
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thr_steer",
                            "turnId": "turn_steer",
                            "delta": "Steered response.",
                        },
                    },
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thr_steer",
                            "turn": {"id": "turn_steer", "status": "completed"},
                        },
                    },
                ]
                return {"turnId": "turn_steer"}
            raise AssertionError(f"Unexpected method: {method}")

        async def next_message(self):
            if self.responses:
                return self.responses.pop(0)
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

        async def respond(self, request_id, result):
            assert request_id == 90
            assert result["success"] is True

        async def respond_error(self, request_id, *, message, code=-32000, data=None):
            raise AssertionError(f"Unexpected error response: {request_id} {message}")

    monkeypatch.setattr(
        "ragnarbot.providers.openai_chatgpt_provider.CODEX_FAST_EVENT_POLL_INTERVAL_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        "ragnarbot.providers.openai_chatgpt_provider.CODEX_FAST_INITIAL_EVENT_TIMEOUT_SECONDS",
        0.1,
    )

    steering_provider = AsyncMock(
        side_effect=[
            [{"role": "user", "content": "Please be brief."}],
            [],
        ]
    )

    with (
        patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"),
        patch("ragnarbot.auth.openai_oauth.get_access_token", return_value="token"),
        patch("ragnarbot.providers.openai_chatgpt_provider._CodexAppServerProcess", FakeProc),
    ):
        provider = OpenAIChatGPTProvider()
        provider._codex_home = tmp_path
        provider._thread_registry_path = tmp_path / "thread_registry.json"
        response = await provider.chat(
            messages=[{"role": "user", "content": "Tell me something."}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "ping",
                        "description": "Return pong",
                        "parameters": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
                    },
                }
            ],
            model="openai/gpt-5.4",
            reasoning_level="medium",
            lightning_mode=True,
            session_key="telegram:steer",
            tool_runner=AsyncMock(return_value="ok"),
            steering_message_provider=steering_provider,
        )

    proc = FakeProc.instances[0]
    steer_call = dict(proc.calls)["turn/steer"]
    assert response.finish_reason == "stop"
    assert response.content == "Steered response."
    assert len(response.consumed_steering_messages) == 1
    assert response.consumed_steering_messages[0].after_executed_tool_calls == 1
    assert response.consumed_steering_messages[0].user_message["content"] == "Please be brief."
    assert steer_call["threadId"] == "thr_steer"
    assert steer_call["expectedTurnId"] == "turn_steer"
    assert steer_call["input"] == [
        {"type": "text", "text": "Please be brief.", "textElements": []}
    ]


@pytest.mark.asyncio
async def test_codex_fast_does_not_consume_late_steering_after_final_text_starts(monkeypatch):
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider()

    class FakeProc:
        def __init__(self):
            self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            self.calls: list[tuple[str, dict[str, Any] | None]] = []
            self._completed_scheduled = False

        async def call(self, method, params=None):
            self.calls.append((method, params))
            return {"turnId": "turn_123"}

        async def next_message(self):
            return await self.queue.get()

        async def respond(self, request_id, result):
            if not self._completed_scheduled:
                self._completed_scheduled = True

                async def _emit_completion():
                    await asyncio.sleep(0.03)
                    await self.queue.put(
                        {
                            "method": "turn/completed",
                            "params": {
                                "threadId": "thr_123",
                                "turn": {"id": "turn_123", "status": "completed"},
                            },
                        }
                    )

                asyncio.create_task(_emit_completion())

        async def respond_error(self, request_id, *, message, code=-32000, data=None):
            raise AssertionError(f"Unexpected error response: {request_id} {message}")

    proc = FakeProc()
    await proc.queue.put(
        {
            "method": "turn/started",
            "params": {
                "threadId": "thr_123",
                "turn": {"id": "turn_123", "status": "inProgress", "items": []},
            },
        }
    )
    await proc.queue.put(
        {
            "jsonrpc": "2.0",
            "id": 77,
            "method": "item/tool/call",
            "params": {
                "threadId": "thr_123",
                "turnId": "turn_123",
                "callId": "call_123",
                "tool": "lookup_ticket",
                "arguments": {"id": "ABC-123"},
            },
        }
    )
    await proc.queue.put(
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thr_123",
                "turnId": "turn_123",
                "delta": "Already writing final answer.",
            },
        }
    )

    monkeypatch.setattr(
        "ragnarbot.providers.openai_chatgpt_provider.CODEX_FAST_EVENT_POLL_INTERVAL_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        "ragnarbot.providers.openai_chatgpt_provider.CODEX_FAST_INITIAL_EVENT_TIMEOUT_SECONDS",
        0.1,
    )

    steering_provider = AsyncMock(
        return_value=[{"role": "user", "content": "too late"}]
    )
    response = await provider._drain_codex_turn(
        proc=proc,
        thread_id="thr_123",
        turn_id="turn_123",
        tool_runner=AsyncMock(return_value="ok"),
        tool_call_handler=None,
        text_delta_handler=None,
        steering_message_provider=steering_provider,
    )

    assert response.finish_reason == "stop"
    assert response.content == "Already writing final answer."
    assert response.consumed_steering_messages == []
    assert "turn/steer" not in dict(proc.calls)
    steering_provider.assert_not_awaited()
