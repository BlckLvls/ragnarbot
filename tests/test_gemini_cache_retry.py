"""Tests for Gemini free tier cache retry logic."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import litellm

from ragnarbot.providers.litellm_provider import LiteLLMProvider
from ragnarbot.providers.base import LLMResponse


class TestStripCacheControl:
    def test_removes_from_content_blocks(self):
        messages = [
            {"role": "system", "content": [
                {"type": "text", "text": "You are helpful.", "cache_control": {"type": "ephemeral"}},
            ]},
            {"role": "user", "content": "Hi"},
        ]
        result = LiteLLMProvider._strip_cache_control(messages)
        assert "cache_control" not in result[0]["content"][0]

    def test_removes_from_message_level(self):
        messages = [
            {"role": "tool", "tool_call_id": "1", "content": "result",
             "cache_control": {"type": "ephemeral"}},
        ]
        result = LiteLLMProvider._strip_cache_control(messages)
        assert "cache_control" not in result[0]

    def test_preserves_other_fields(self):
        messages = [
            {"role": "system", "content": [
                {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}},
            ]},
            {"role": "tool", "tool_call_id": "42", "content": "data",
             "cache_control": {"type": "ephemeral"}},
        ]
        result = LiteLLMProvider._strip_cache_control(messages)
        assert result[0]["role"] == "system"
        assert result[0]["content"][0]["type"] == "text"
        assert result[0]["content"][0]["text"] == "hello"
        assert result[1]["role"] == "tool"
        assert result[1]["tool_call_id"] == "42"
        assert result[1]["content"] == "data"

    def test_does_not_mutate_originals(self):
        block = {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
        msg = {"role": "system", "content": [block]}
        tool_msg = {"role": "tool", "content": "r", "cache_control": {"type": "ephemeral"}}
        messages = [msg, tool_msg]

        LiteLLMProvider._strip_cache_control(messages)

        assert "cache_control" in block
        assert "cache_control" in tool_msg


class TestGeminiFreeTrierCacheRetry:
    @pytest.mark.asyncio
    async def test_retries_without_cache_on_free_tier_error(self):
        provider = LiteLLMProvider.__new__(LiteLLMProvider)
        provider.default_model = "gemini/gemini-2.0-flash"

        free_tier_error = litellm.RateLimitError(
            message="TotalCachedContentStorageTokensPerModelFreeTier limit exceeded",
            model="gemini/gemini-2.0-flash",
            llm_provider="gemini",
        )

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Hello!"
        mock_response.choices[0].message.tool_calls = None
        mock_response.choices[0].finish_reason = "stop"
        mock_response.usage = MagicMock()
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5
        mock_response.usage.total_tokens = 15
        mock_response.usage.prompt_tokens_details = None

        mock_acompletion = AsyncMock(side_effect=[free_tier_error, mock_response])

        with patch("ragnarbot.providers.litellm_provider.acompletion", mock_acompletion), \
             patch("ragnarbot.config.providers.model_supports_vision", return_value=True):
            result = await provider.chat(
                messages=[
                    {"role": "system", "content": "You are helpful."},
                    {"role": "user", "content": "Hi"},
                ],
                model="gemini/gemini-2.0-flash",
            )

        assert result.finish_reason == "stop"
        assert result.content == "Hello!"
        assert mock_acompletion.call_count == 2

        # Second call should have no cache_control in messages
        retry_messages = mock_acompletion.call_args_list[1].kwargs["messages"]
        for msg in retry_messages:
            assert "cache_control" not in msg
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        assert "cache_control" not in block

    @pytest.mark.asyncio
    async def test_no_retry_on_unrelated_rate_limit(self):
        provider = LiteLLMProvider.__new__(LiteLLMProvider)
        provider.default_model = "gemini/gemini-2.0-flash"

        rate_limit_error = litellm.RateLimitError(
            message="Rate limit exceeded for model gemini-2.0-flash",
            model="gemini/gemini-2.0-flash",
            llm_provider="gemini",
        )

        mock_acompletion = AsyncMock(side_effect=rate_limit_error)

        with patch("ragnarbot.providers.litellm_provider.acompletion", mock_acompletion), \
             patch("ragnarbot.config.providers.model_supports_vision", return_value=True):
            result = await provider.chat(
                messages=[
                    {"role": "system", "content": "You are helpful."},
                    {"role": "user", "content": "Hi"},
                ],
                model="gemini/gemini-2.0-flash",
            )

        assert result.finish_reason == "error"
        assert "Rate limit exceeded" in result.content
        assert mock_acompletion.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_failure_returns_error(self):
        provider = LiteLLMProvider.__new__(LiteLLMProvider)
        provider.default_model = "gemini/gemini-2.0-flash"

        free_tier_error = litellm.RateLimitError(
            message="TotalCachedContentStorageTokensPerModelFreeTier limit exceeded",
            model="gemini/gemini-2.0-flash",
            llm_provider="gemini",
        )
        second_error = Exception("Connection failed")

        mock_acompletion = AsyncMock(side_effect=[free_tier_error, second_error])

        with patch("ragnarbot.providers.litellm_provider.acompletion", mock_acompletion), \
             patch("ragnarbot.config.providers.model_supports_vision", return_value=True):
            result = await provider.chat(
                messages=[
                    {"role": "system", "content": "You are helpful."},
                    {"role": "user", "content": "Hi"},
                ],
                model="gemini/gemini-2.0-flash",
            )

        assert result.finish_reason == "error"
        assert "Connection failed" in result.content
        assert mock_acompletion.call_count == 2
