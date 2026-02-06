"""Tests for AnthropicProvider message/tool conversion and response parsing."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ragnarbot.providers.anthropic_provider import AnthropicProvider, _convert_user_content, _merge_consecutive


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------


class TestConvertMessages:
    def test_system_extracted(self):
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ]
        system, msgs = AnthropicProvider._convert_messages(messages)
        assert system == "You are helpful."
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_multiple_system_joined(self):
        messages = [
            {"role": "system", "content": "Part 1"},
            {"role": "system", "content": "Part 2"},
            {"role": "user", "content": "Hi"},
        ]
        system, msgs = AnthropicProvider._convert_messages(messages)
        assert system == "Part 1\n\nPart 2"

    def test_no_system(self):
        messages = [{"role": "user", "content": "Hi"}]
        system, msgs = AnthropicProvider._convert_messages(messages)
        assert system is None
        assert len(msgs) == 1

    def test_user_string_content(self):
        messages = [{"role": "user", "content": "Hello"}]
        _, msgs = AnthropicProvider._convert_messages(messages)
        assert msgs[0]["content"] == "Hello"

    def test_assistant_with_tool_calls(self):
        messages = [
            {"role": "user", "content": "Search for X"},
            {
                "role": "assistant",
                "content": "I'll search.",
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": {"query": "X"},
                        },
                    }
                ],
            },
        ]
        _, msgs = AnthropicProvider._convert_messages(messages)
        assistant_msg = msgs[1]
        assert assistant_msg["role"] == "assistant"
        assert len(assistant_msg["content"]) == 2
        assert assistant_msg["content"][0] == {"type": "text", "text": "I'll search."}
        assert assistant_msg["content"][1]["type"] == "tool_use"
        assert assistant_msg["content"][1]["id"] == "tc_1"
        assert assistant_msg["content"][1]["name"] == "web_search"
        assert assistant_msg["content"][1]["input"] == {"query": "X"}

    def test_assistant_tool_calls_string_arguments(self):
        messages = [
            {"role": "user", "content": "Do it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc_2",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "/tmp/x"}',
                        },
                    }
                ],
            },
        ]
        _, msgs = AnthropicProvider._convert_messages(messages)
        tool_block = msgs[1]["content"][0]  # no text block since content=""
        assert tool_block["input"] == {"path": "/tmp/x"}

    def test_tool_result_conversion(self):
        messages = [
            {"role": "user", "content": "Go"},
            {
                "role": "assistant",
                "content": "Using tool",
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "function": {"name": "read_file", "arguments": {}},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc_1", "content": "file contents"},
        ]
        _, msgs = AnthropicProvider._convert_messages(messages)
        # tool result becomes a user message
        tool_msg = msgs[2]
        assert tool_msg["role"] == "user"
        assert tool_msg["content"][0]["type"] == "tool_result"
        assert tool_msg["content"][0]["tool_use_id"] == "tc_1"
        assert tool_msg["content"][0]["content"] == "file contents"


# ---------------------------------------------------------------------------
# Image conversion
# ---------------------------------------------------------------------------


class TestConvertUserContent:
    def test_string_passthrough(self):
        assert _convert_user_content("hello") == "hello"

    def test_base64_image(self):
        content = [
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
            }
        ]
        result = _convert_user_content(content)
        assert result[0]["type"] == "image"
        assert result[0]["source"]["type"] == "base64"
        assert result[0]["source"]["media_type"] == "image/png"
        assert result[0]["source"]["data"] == "iVBORw0KGgo="

    def test_text_part(self):
        content = [{"type": "text", "text": "describe this"}]
        result = _convert_user_content(content)
        assert result[0] == {"type": "text", "text": "describe this"}


# ---------------------------------------------------------------------------
# Tool definition conversion
# ---------------------------------------------------------------------------


class TestConvertTools:
    def test_openai_to_anthropic(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the web",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            }
        ]
        result = AnthropicProvider._convert_tools(tools)
        assert len(result) == 1
        assert result[0]["name"] == "web_search"
        assert result[0]["description"] == "Search the web"
        assert result[0]["input_schema"]["type"] == "object"
        assert "query" in result[0]["input_schema"]["properties"]


# ---------------------------------------------------------------------------
# Merge consecutive
# ---------------------------------------------------------------------------


class TestMergeConsecutive:
    def test_no_merge_needed(self):
        msgs = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
        ]
        assert _merge_consecutive(msgs) == msgs

    def test_merge_two_user_strings(self):
        msgs = [
            {"role": "user", "content": "A"},
            {"role": "user", "content": "B"},
        ]
        merged = _merge_consecutive(msgs)
        assert len(merged) == 1
        assert merged[0]["role"] == "user"
        # Both converted to text blocks
        assert len(merged[0]["content"]) == 2

    def test_merge_tool_results(self):
        msgs = [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "a"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "2", "content": "b"}]},
        ]
        merged = _merge_consecutive(msgs)
        assert len(merged) == 1
        assert len(merged[0]["content"]) == 2

    def test_empty_list(self):
        assert _merge_consecutive([]) == []


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


class TestParseResponse:
    def _make_response(self, content_blocks, stop_reason="end_turn", input_tokens=10, output_tokens=5):
        response = MagicMock()
        response.content = content_blocks
        response.stop_reason = stop_reason
        response.usage.input_tokens = input_tokens
        response.usage.output_tokens = output_tokens
        return response

    def test_text_response(self):
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Hello!"

        response = self._make_response([text_block])
        result = AnthropicProvider._parse_response(response)

        assert result.content == "Hello!"
        assert result.tool_calls == []
        assert result.finish_reason == "stop"
        assert result.usage["prompt_tokens"] == 10
        assert result.usage["completion_tokens"] == 5
        assert result.usage["total_tokens"] == 15

    def test_tool_use_response(self):
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.id = "tu_123"
        tool_block.name = "web_search"
        tool_block.input = {"query": "test"}

        response = self._make_response([tool_block], stop_reason="tool_use")
        result = AnthropicProvider._parse_response(response)

        assert result.content is None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].id == "tu_123"
        assert result.tool_calls[0].name == "web_search"
        assert result.tool_calls[0].arguments == {"query": "test"}
        assert result.finish_reason == "tool_calls"

    def test_mixed_content(self):
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Let me search."

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.id = "tu_456"
        tool_block.name = "read_file"
        tool_block.input = {"path": "/tmp/x"}

        response = self._make_response([text_block, tool_block], stop_reason="tool_use")
        result = AnthropicProvider._parse_response(response)

        assert result.content == "Let me search."
        assert len(result.tool_calls) == 1
        assert result.finish_reason == "tool_calls"

    def test_max_tokens_finish_reason(self):
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Truncated..."

        response = self._make_response([text_block], stop_reason="max_tokens")
        result = AnthropicProvider._parse_response(response)
        assert result.finish_reason == "length"


# ---------------------------------------------------------------------------
# Provider init and model stripping
# ---------------------------------------------------------------------------


class TestProviderInit:
    @pytest.mark.asyncio
    async def test_model_prefix_stripped_in_chat(self):
        """Verify anthropic/ prefix is stripped before calling the SDK."""
        provider = AnthropicProvider(api_key="sk-test", default_model="anthropic/claude-opus-4-6")

        # Mock the client
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "hi"

        mock_response = MagicMock()
        mock_response.content = [text_block]
        mock_response.stop_reason = "end_turn"
        mock_response.usage.input_tokens = 5
        mock_response.usage.output_tokens = 3

        provider.client.messages.create = AsyncMock(return_value=mock_response)

        result = await provider.chat([{"role": "user", "content": "hi"}])

        call_kwargs = provider.client.messages.create.call_args
        assert call_kwargs.kwargs["model"] == "claude-opus-4-6"
        assert result.content == "hi"
