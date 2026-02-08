"""Anthropic SDK provider for OAuth token support."""

import os
from typing import Any

from anthropic import AsyncAnthropic

from ragnarbot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

# Headers required by Anthropic API for OAuth token authentication.
_OAUTH_HEADERS = {
    "anthropic-dangerous-direct-browser-access": "true",
    "anthropic-beta": (
        "claude-code-20250219,oauth-2025-04-20,"
        "fine-grained-tool-streaming-2025-05-14,"
        "interleaved-thinking-2025-05-14"
    ),
    "user-agent": "claude-cli/2.1.2 (external, cli)",
    "x-app": "cli",
}

_CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."


class AnthropicProvider(LLMProvider):
    """LLM provider using the official Anthropic SDK.

    Used when OAuth tokens (sk-ant-oat-*) are configured, since LiteLLM
    doesn't support Bearer auth required by OAuth tokens.
    """

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str = "claude-opus-4-6",
        oauth_token: str | None = None,
    ):
        super().__init__(api_key, oauth_token)
        self.default_model = default_model
        self.client = self._build_client(api_key, oauth_token)

    def _build_client(
        self, api_key: str | None = None, oauth_token: str | None = None,
    ) -> AsyncAnthropic:
        if oauth_token:
            # Remove ANTHROPIC_API_KEY from env — if both headers are sent the API returns 401.
            os.environ.pop("ANTHROPIC_API_KEY", None)
            return AsyncAnthropic(
                api_key=None,
                auth_token=oauth_token,
                default_headers=_OAUTH_HEADERS,
            )

        if api_key:
            return AsyncAnthropic(api_key=api_key)

        return AsyncAnthropic()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        model = model or self.default_model
        # Strip provider prefix — config stores "anthropic/claude-...", SDK expects "claude-..."
        if model.startswith("anthropic/"):
            model = model[len("anthropic/"):]

        # Convert messages from OpenAI format to Anthropic format
        system_prompt, anthropic_messages = self._convert_messages(messages)

        # Cache breakpoint 2: conversation history prefix
        # Mark the second-to-last user message so all previous context is cached
        self._inject_history_cache_control(anthropic_messages)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if system_prompt or self.oauth_token:
            kwargs["system"] = self._build_system(system_prompt)

        if tools:
            kwargs["tools"] = self._convert_tools(tools)

        try:
            response = await self.client.messages.create(**kwargs)
            return self._parse_response(response)
        except Exception as e:
            return LLMResponse(
                content=f"Error calling LLM: {e}",
                finish_reason="error",
            )

    def get_default_model(self) -> str:
        return self.default_model

    def _build_system(self, system_prompt: str | None) -> list[dict[str, Any]]:
        """Build system param as list of text blocks with cache_control.

        When using OAuth, the Claude Code identity block is prepended.
        The last block gets a cache_control breakpoint so the system prompt
        is cached across turns.
        """
        blocks: list[dict[str, Any]] = []
        if self.oauth_token:
            blocks.append({"type": "text", "text": _CLAUDE_CODE_IDENTITY})
        if system_prompt:
            blocks.append({"type": "text", "text": system_prompt})
        if blocks:
            blocks[-1]["cache_control"] = {"type": "ephemeral"}
        return blocks

    @staticmethod
    def _inject_history_cache_control(anthropic_messages: list[dict[str, Any]]) -> None:
        """Add cache_control to the second-to-last user message.

        This caches all history up to (but not including) the current message,
        so within a multi-iteration tool-call turn, only the latest tool
        result is uncached.
        """
        user_count = 0
        for i in range(len(anthropic_messages) - 1, -1, -1):
            if anthropic_messages[i]["role"] == "user":
                user_count += 1
                if user_count == 2:
                    content = anthropic_messages[i]["content"]
                    if isinstance(content, list) and content:
                        content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
                    elif isinstance(content, str):
                        anthropic_messages[i]["content"] = [{
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"},
                        }]
                    break

    @staticmethod
    def _convert_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert OpenAI-format messages to Anthropic format.

        Returns (system_prompt, messages).
        """
        system_parts: list[str] = []
        anthropic_msgs: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")

            if role == "system":
                if isinstance(content, str):
                    system_parts.append(content)
                continue

            if role == "user":
                anthropic_msgs.append({
                    "role": "user",
                    "content": _convert_user_content(content),
                })

            elif role == "assistant":
                blocks: list[dict[str, Any]] = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        import json
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, ValueError):
                            args = {"raw": args}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args,
                    })
                if blocks:
                    anthropic_msgs.append({"role": "assistant", "content": blocks})

            elif role == "tool":
                anthropic_msgs.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": content or "",
                    }],
                })

        # Merge consecutive same-role messages (Anthropic requires alternation)
        anthropic_msgs = _merge_consecutive(anthropic_msgs)

        system = "\n\n".join(system_parts) if system_parts else None
        return system, anthropic_msgs

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI tool definitions to Anthropic format."""
        result = []
        for tool in tools:
            fn = tool.get("function", {})
            result.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return result

    @staticmethod
    def _parse_response(response: Any) -> LLMResponse:
        """Convert Anthropic response to LLMResponse."""
        content_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []

        for block in response.content:
            if block.type == "text":
                content_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCallRequest(
                    id=block.id,
                    name=block.name,
                    arguments=block.input,
                ))

        # Map stop_reason
        stop_reason = response.stop_reason
        if stop_reason == "end_turn":
            finish_reason = "stop"
        elif stop_reason == "tool_use":
            finish_reason = "tool_calls"
        elif stop_reason == "max_tokens":
            finish_reason = "length"
        else:
            finish_reason = stop_reason or "stop"

        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.input_tokens,
                "completion_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
                "cache_creation_input_tokens": (
                    getattr(response.usage, "cache_creation_input_tokens", 0) or 0
                ),
                "cache_read_input_tokens": (
                    getattr(response.usage, "cache_read_input_tokens", 0) or 0
                ),
            }

        return LLMResponse(
            content="\n".join(content_parts) if content_parts else None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )


def _convert_user_content(content: Any) -> Any:
    """Convert user message content (string or multipart) to Anthropic format."""
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        return content

    blocks: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            blocks.append({"type": "text", "text": part})
        elif isinstance(part, dict):
            part_type = part.get("type")
            if part_type == "text":
                blocks.append({"type": "text", "text": part.get("text", "")})
            elif part_type == "image_url":
                url = part.get("image_url", {}).get("url", "")
                if url.startswith("data:"):
                    # data:image/png;base64,AAAA...
                    header, data = url.split(",", 1)
                    media_type = header.split(":")[1].split(";")[0]
                    blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": data,
                        },
                    })
                else:
                    blocks.append({
                        "type": "image",
                        "source": {"type": "url", "url": url},
                    })
            else:
                blocks.append(part)
    return blocks


def _merge_consecutive(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive messages with the same role.

    Anthropic requires strictly alternating user/assistant roles.
    """
    if not messages:
        return messages

    merged: list[dict[str, Any]] = [messages[0]]

    for msg in messages[1:]:
        if msg["role"] == merged[-1]["role"]:
            # Merge content
            prev_content = merged[-1]["content"]
            curr_content = msg["content"]

            # Normalise both to lists
            if isinstance(prev_content, str):
                prev_content = [{"type": "text", "text": prev_content}]
            if isinstance(curr_content, str):
                curr_content = [{"type": "text", "text": curr_content}]

            merged[-1]["content"] = prev_content + curr_content
        else:
            merged.append(msg)

    return merged
