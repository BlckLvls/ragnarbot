"""Tests for unified reasoning levels and provider request shaping."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ragnarbot.cli.commands import _create_provider
from ragnarbot.config.loader import load_config, save_config
from ragnarbot.config.schema import Config
from ragnarbot.providers.anthropic_provider import AnthropicProvider
from ragnarbot.providers.base import LLMResponse
from ragnarbot.providers.gemini_provider import GeminiCodeAssistProvider
from ragnarbot.providers.litellm_provider import LiteLLMProvider
from ragnarbot.providers.openai_chatgpt_provider import OpenAIChatGPTProvider
from ragnarbot.providers.reasoning import resolve_reasoning


def _mock_litellm_response(content: str = "ok"):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.choices[0].message.tool_calls = None
    response.choices[0].finish_reason = "stop"
    response.usage = MagicMock()
    response.usage.prompt_tokens = 10
    response.usage.completion_tokens = 5
    response.usage.total_tokens = 15
    response.usage.prompt_tokens_details = None
    return response


def test_reasoning_config_roundtrip(tmp_path):
    config = Config()
    config.agents.defaults.reasoning_level = "ultra"

    config_path = tmp_path / "config.json"
    save_config(config, config_path)

    raw = json.loads(config_path.read_text())
    assert raw["agents"]["defaults"]["reasoningLevel"] == "ultra"

    loaded = load_config(config_path)
    assert loaded.agents.defaults.reasoning_level == "ultra"


def test_resolve_reasoning_covers_expected_downgrades():
    openai_flagship = resolve_reasoning("openai/gpt-5.4", "ultra")
    assert openai_flagship.effective_level == "ultra"
    assert openai_flagship.openai_reasoning == {"effort": "xhigh"}

    openai_mini = resolve_reasoning("openai/gpt-5-mini", "off")
    assert openai_mini.effective_level == "medium"
    assert openai_mini.note is not None

    gemini_pro = resolve_reasoning("gemini/gemini-3.1-pro-preview", "medium")
    assert gemini_pro.effective_level == "high"
    assert gemini_pro.note == "This model maps medium to high."

    openrouter = resolve_reasoning("openrouter/openai/gpt-5.4", "ultra")
    assert openrouter.effective_level == "ultra"
    assert openrouter.openrouter_reasoning == {"enabled": True, "effort": "xhigh"}

    openrouter_gemini = resolve_reasoning("openrouter/google/gemini-3.1-pro-preview", "ultra")
    assert openrouter_gemini.effective_level == "high"
    assert openrouter_gemini.note == "OpenRouter maps xhigh to high for Gemini 3 models."
    assert openrouter_gemini.openrouter_reasoning == {"enabled": True, "effort": "xhigh"}

    anthropic = resolve_reasoning("anthropic/claude-opus-4-6", "ultra")
    assert anthropic.effective_level == "ultra"
    assert anthropic.anthropic_thinking == {"type": "adaptive"}
    assert anthropic.anthropic_output_config == {"effort": "max"}

    anthropic_off = resolve_reasoning("anthropic/claude-opus-4-6", "off")
    assert anthropic_off.effective_level == "off"
    assert anthropic_off.anthropic_thinking is None
    assert anthropic_off.anthropic_output_config is None

    anthropic_sonnet = resolve_reasoning("anthropic/claude-sonnet-4-6", "ultra")
    assert anthropic_sonnet.effective_level == "high"
    assert anthropic_sonnet.note == "This model maps ultra to high."
    assert anthropic_sonnet.anthropic_thinking == {"type": "adaptive"}
    assert anthropic_sonnet.anthropic_output_config == {"effort": "high"}


def test_create_provider_uses_native_anthropic_for_api_key():
    creds = MagicMock()
    creds.providers.anthropic.api_key = "sk-ant-test"
    provider = _create_provider("anthropic/claude-opus-4-6", "api_key", creds)
    assert isinstance(provider, AnthropicProvider)


def test_openai_build_request_includes_reasoning():
    with patch("ragnarbot.auth.openai_oauth.get_account_id", return_value="acct_test"):
        provider = OpenAIChatGPTProvider(default_model="openai/gpt-5.4")

    body = provider._build_request(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model="gpt-5.4",
        reasoning_level="ultra",
    )

    assert body["reasoning"] == {"effort": "xhigh"}


@pytest.mark.asyncio
async def test_gemini_chat_includes_thinking_config():
    with patch("ragnarbot.auth.gemini_oauth.get_project_id", return_value="proj"):
        provider = GeminiCodeAssistProvider(default_model="gemini/gemini-3.1-pro-preview")

    stream_request = AsyncMock(return_value=LLMResponse(content="ok"))
    with (
        patch("ragnarbot.auth.gemini_oauth.get_access_token", return_value="token"),
        patch.object(provider, "_stream_request", stream_request),
    ):
        await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            reasoning_level="medium",
        )

    envelope = stream_request.await_args.args[0]
    assert envelope["request"]["generationConfig"]["thinkingConfig"] == {
        "includeThoughts": True,
        "thinkingLevel": "HIGH",
    }


@pytest.mark.asyncio
async def test_anthropic_chat_includes_reasoning_kwargs():
    provider = AnthropicProvider(api_key="sk-test", default_model="anthropic/claude-opus-4-6")

    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "hi"

    mock_response = MagicMock()
    mock_response.content = [text_block]
    mock_response.stop_reason = "end_turn"
    mock_response.usage.input_tokens = 5
    mock_response.usage.output_tokens = 3

    mock_stream = AsyncMock()
    mock_stream.get_final_message = AsyncMock(return_value=mock_response)
    mock_stream.__aenter__ = AsyncMock(return_value=mock_stream)
    mock_stream.__aexit__ = AsyncMock(return_value=False)
    provider.client.messages.stream = MagicMock(return_value=mock_stream)

    await provider.chat(
        [{"role": "user", "content": "hi"}],
        reasoning_level="ultra",
    )

    call_kwargs = provider.client.messages.stream.call_args.kwargs
    assert call_kwargs["thinking"] == {"type": "adaptive"}
    assert call_kwargs["output_config"] == {"effort": "max"}


@pytest.mark.asyncio
async def test_litellm_passes_reasoning_effort():
    provider = LiteLLMProvider(api_key="sk-openai", default_model="openai/gpt-5.2")
    mock_acompletion = AsyncMock(return_value=_mock_litellm_response())

    with (
        patch("ragnarbot.providers.litellm_provider.acompletion", mock_acompletion),
        patch("ragnarbot.config.providers.model_supports_vision", return_value=True),
    ):
        await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            reasoning_level="ultra",
        )

    assert mock_acompletion.await_args.kwargs["reasoning_effort"] == "xhigh"


@pytest.mark.asyncio
async def test_litellm_openrouter_uses_dynamic_reasoning_body():
    provider = LiteLLMProvider(
        api_key="sk-openrouter",
        default_model="openrouter/openai/gpt-5.4",
    )
    mock_acompletion = AsyncMock(return_value=_mock_litellm_response())

    with (
        patch("ragnarbot.providers.litellm_provider.acompletion", mock_acompletion),
        patch("ragnarbot.config.providers.model_supports_vision", return_value=True),
    ):
        await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            reasoning_level="ultra",
        )

    extra_body = mock_acompletion.await_args.kwargs["extra_body"]
    assert extra_body["reasoning"] == {"enabled": True, "effort": "xhigh"}
