"""Anthropic SDK provider for OAuth token support."""

import asyncio
import logging
import os
from typing import Any

import httpx
from anthropic import APIStatusError, AsyncAnthropic

from ragnarbot.providers.base import (
    MAX_OUTPUT_TOKENS,
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
    format_provider_exception,
)
from ragnarbot.providers.reasoning import resolve_reasoning

# Anthropic streams every request (see chat()), so it is not bound by the SDK's
# non-streaming max_tokens ValueError. Default to the full 128k sync-API output
# ceiling (Claude 4.x) so extended thinking at "max"/"xhigh" effort (Opus 4.7/4.8)
# is never truncated early. (300k exists but is Batches-API-only.)
ANTHROPIC_DEFAULT_MAX_TOKENS = MAX_OUTPUT_TOKENS

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

# Application-level retry delays (seconds) for 529 Overloaded errors.
# Each retry is a fresh SDK-level attempt set (the SDK itself retries twice internally).
_OVERLOADED_RETRY_DELAYS = [0, 1, 2]
_MAX_APP_ATTEMPTS = 1 + len(_OVERLOADED_RETRY_DELAYS)
_OPEN_STREAM_RETRY_DELAY_SECONDS = 0.75

logger = logging.getLogger(__name__)


class AnthropicProvider(LLMProvider):
    """LLM provider using the official Anthropic SDK.

    Used when OAuth tokens (sk-ant-oat-*) are configured, since LiteLLM
    doesn't support Bearer auth required by OAuth tokens.
    """

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str = "claude-opus-4-8",
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
        max_tokens: int | None = None,
        temperature: float | None = None,
        reasoning_level: str | None = None,
        lightning_mode: bool | None = None,
        session_key: str | None = None,
        tool_runner=None,
        tool_call_handler=None,
        text_delta_handler=None,
        steering_message_provider=None,
    ) -> LLMResponse:
        _ = (
            session_key,
            tool_runner,
            tool_call_handler,
            steering_message_provider,
        )
        _ = lightning_mode
        model = model or self.default_model
        reasoning = resolve_reasoning(model, reasoning_level)
        max_tokens = max_tokens if max_tokens is not None else ANTHROPIC_DEFAULT_MAX_TOKENS
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
        }
        if reasoning.anthropic_thinking is not None:
            # Anthropic requires temperature=1 when thinking is enabled;
            # omit it entirely so the API uses its default (1.0)
            pass
        elif temperature is not None:
            kwargs["temperature"] = temperature

        if system_prompt or self.oauth_token:
            kwargs["system"] = self._build_system(system_prompt)

        if tools:
            kwargs["tools"] = self._convert_tools(tools)
        if reasoning.anthropic_thinking is not None:
            kwargs["thinking"] = reasoning.anthropic_thinking
        if reasoning.anthropic_output_config is not None:
            kwargs["output_config"] = reasoning.anthropic_output_config

        overloaded_retry_index = 0
        open_stream_retry_used = False
        for attempt in range(1, _MAX_APP_ATTEMPTS + 1):
            stream_opened = False
            final_message_received = False
            try:
                # Use streaming to avoid Anthropic SDK ValueError for max_tokens > ~21k
                async with self.client.messages.stream(**kwargs) as stream:
                    stream_opened = True
                    # Only token-level handlers get raw deltas (see LiteLLMProvider._complete)
                    if getattr(text_delta_handler, "token_level", False):
                        async for event in stream:
                            if (
                                event.type == "content_block_delta"
                                and getattr(event.delta, "type", "") == "text_delta"
                                and event.delta.text
                            ):
                                await text_delta_handler(event.delta.text)
                    response = await stream.get_final_message()
                    final_message_received = True
                return self._parse_response(response)
            except APIStatusError as e:
                if (
                    e.status_code == 529
                    and attempt < _MAX_APP_ATTEMPTS
                    and overloaded_retry_index < len(_OVERLOADED_RETRY_DELAYS)
                ):
                    delay = _OVERLOADED_RETRY_DELAYS[overloaded_retry_index]
                    overloaded_retry_index += 1
                    if delay > 0:
                        await asyncio.sleep(delay)
                    logger.info(
                        "Retrying after 529 Overloaded (attempt %d/%d)",
                        attempt + 1,
                        _MAX_APP_ATTEMPTS,
                    )
                    continue
                return self._error_response(e)
            except httpx.TransportError as e:
                # The Anthropic SDK already retries connection failures while
                # opening the request. It cannot retry a body-read failure once
                # the streaming response is open, so allow exactly one such
                # provider-level retry before any final message was received.
                if (
                    stream_opened
                    and not final_message_received
                    and not open_stream_retry_used
                    and attempt < _MAX_APP_ATTEMPTS
                ):
                    open_stream_retry_used = True
                    logger.warning(
                        "Retrying Anthropic stream after transient read failure "
                        "(attempt %d/%d): %s",
                        attempt + 1,
                        _MAX_APP_ATTEMPTS,
                        format_provider_exception(e),
                    )
                    await asyncio.sleep(_OPEN_STREAM_RETRY_DELAY_SECONDS)
                    continue
                return self._error_response(e)
            except Exception as e:
                return self._error_response(e)

        raise AssertionError("Anthropic retry loop exhausted unexpectedly")

    @staticmethod
    def _error_response(exc: Exception) -> LLMResponse:
        detail = format_provider_exception(exc)
        logger.error("Anthropic API call failed: %s", detail)
        return LLMResponse(
            content=f"Error calling LLM: {detail}",
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
        """Add sliding cache_control breakpoint to conversation history.

        Targets the last user message containing tool_result blocks, so
        accumulated tool results are cached across agent-loop iterations.
        Falls back to the second-to-last user message when no tool results
        exist (first call in a turn).
        """
        # Sliding: last user message with tool_result content
        for i in range(len(anthropic_messages) - 1, -1, -1):
            msg = anthropic_messages[i]
            if msg["role"] != "user":
                continue
            content = msg["content"]
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            ):
                content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
                return

        # Fallback: 2nd-to-last user message
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
                tool_content = content or ""
                if isinstance(content, list):
                    tool_content = _convert_user_content(content)
                anthropic_msgs.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": tool_content,
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
