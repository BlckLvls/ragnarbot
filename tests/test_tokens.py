"""Tests for token estimation module."""

import json

from ragnarbot.agent.tokens import (
    CHARS_PER_TOKEN,
    estimate_image_tokens,
    estimate_messages_tokens,
    estimate_tokens,
    estimate_tools_tokens,
)


class TestEstimateTokens:
    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_basic_text(self):
        text = "Hello, world!"
        expected = len(text) // CHARS_PER_TOKEN
        assert estimate_tokens(text) == expected

    def test_longer_text(self):
        text = "a" * 400
        assert estimate_tokens(text) == 100

    def test_code_text(self):
        code = "def hello():\n    return 'world'\n"
        assert estimate_tokens(code) == len(code) // CHARS_PER_TOKEN


class TestEstimateImageTokens:
    def test_anthropic(self):
        assert estimate_image_tokens("anthropic") == 800

    def test_openai(self):
        assert estimate_image_tokens("openai") == 400

    def test_gemini(self):
        assert estimate_image_tokens("gemini") == 258

    def test_unknown_defaults_to_800(self):
        assert estimate_image_tokens("unknown_provider") == 800


class TestEstimateMessagesTokens:
    def test_empty_messages(self):
        assert estimate_messages_tokens([]) == 0

    def test_single_text_message(self):
        messages = [{"role": "user", "content": "Hello"}]
        result = estimate_messages_tokens(messages)
        # 4 (overhead) + len("Hello") // 4
        assert result == 4 + len("Hello") // CHARS_PER_TOKEN

    def test_multipart_message_with_image(self):
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this image"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
            ],
        }]
        result = estimate_messages_tokens(messages, provider="anthropic")
        expected = 4 + estimate_tokens("Describe this image") + 800
        assert result == expected

    def test_image_type_block(self):
        messages = [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "data": "..."}},
            ],
        }]
        result = estimate_messages_tokens(messages, provider="gemini")
        expected = 4 + 258
        assert result == expected

    def test_tool_calls_in_assistant_message(self):
        messages = [{
            "role": "assistant",
            "content": "Let me search for that.",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "web_search",
                    "arguments": json.dumps({"query": "test"}),
                },
            }],
        }]
        result = estimate_messages_tokens(messages)
        assert result > 4  # overhead + content + tool call tokens

    def test_multiple_messages(self):
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        result = estimate_messages_tokens(messages)
        expected = sum(
            4 + estimate_tokens(m["content"]) for m in messages
        )
        assert result == expected

    def test_provider_affects_image_tokens(self):
        messages = [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
            ],
        }]
        anthropic_result = estimate_messages_tokens(messages, "anthropic")
        openai_result = estimate_messages_tokens(messages, "openai")
        assert anthropic_result > openai_result  # 800 vs 400


class TestEstimateToolsTokens:
    def test_empty_tools(self):
        assert estimate_tools_tokens([]) == estimate_tokens("[]")

    def test_tool_definitions(self):
        tools = [{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file from disk",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }]
        result = estimate_tools_tokens(tools)
        assert result == estimate_tokens(json.dumps(tools))
        assert result > 0
