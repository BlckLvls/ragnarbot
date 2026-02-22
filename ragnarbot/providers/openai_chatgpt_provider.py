"""OpenAI ChatGPT backend provider for OAuth-based access.

Uses the ChatGPT backend API (codex/responses endpoint) with the
OAuth access_token as Bearer authentication. This is the same API
that Codex CLI uses with ChatGPT Plus/Pro subscriptions.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ragnarbot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

API_BASE = "https://chatgpt.com/backend-api/codex"
RESPONSES_URL = f"{API_BASE}/responses"


class OpenAIChatGPTProvider(LLMProvider):
    """LLM provider for OpenAI via ChatGPT backend API (OAuth)."""

    def __init__(
        self,
        default_model: str = "gpt-5.2",
        max_tokens: int = 16_000,
        temperature: float = 0.7,
    ):
        super().__init__(max_tokens=max_tokens, temperature=temperature)
        self.default_model = default_model

        from ragnarbot.auth.openai_oauth import get_account_id
        self._account_id = get_account_id()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        from ragnarbot.auth.openai_oauth import get_access_token

        model = model or self.default_model
        max_tokens = max_tokens if max_tokens is not None else self.default_max_tokens
        temperature = temperature if temperature is not None else self.default_temperature

        # Strip provider prefix (e.g. "openai/gpt-5.2" → "gpt-5.2")
        if model.startswith("openai/"):
            model = model[len("openai/"):]

        access_token = get_access_token()
        if not access_token:
            return LLMResponse(
                content="Error: OpenAI OAuth token not available. Run: ragnarbot oauth openai",
                finish_reason="error",
            )

        # Build the request in OpenAI Responses API format
        request_body = self._build_request(messages, tools, model)

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "chatgpt-account-id": self._account_id or "",
            "OpenAI-Beta": "responses=experimental",
            "originator": "ragnarbot",
            "accept": "text/event-stream",
        }

        try:
            return await self._stream_request(request_body, headers)
        except Exception as e:
            return LLMResponse(
                content=f"Error calling OpenAI ChatGPT API: {e}",
                finish_reason="error",
            )

    async def _stream_request(
        self, request_body: dict, headers: dict
    ) -> LLMResponse:
        """POST to the responses endpoint and parse SSE events."""
        text_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        finish_reason = "stop"
        usage: dict[str, int] = {}

        # Track tool calls being built up across events
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

                    # Text content
                    if event_type == "response.output_text.delta":
                        delta = event.get("delta", "")
                        if delta:
                            text_parts.append(delta)

                    # Function call item added — captures name
                    elif event_type == "response.output_item.added":
                        item = event.get("item", {})
                        if item.get("type") == "function_call":
                            item_id = item.get("id", "")
                            pending_calls[item_id] = {
                                "name": item.get("name", ""),
                                "arguments": "",
                            }

                    # Function call arguments delta
                    elif event_type == "response.function_call_arguments.delta":
                        call_id = event.get("item_id", "")
                        if call_id in pending_calls:
                            pending_calls[call_id]["arguments"] += event.get("delta", "")

                    # Function call done
                    elif event_type == "response.function_call_arguments.done":
                        call_id = event.get("item_id", "")
                        if call_id in pending_calls:
                            call_data = pending_calls.pop(call_id)
                            args = call_data["arguments"]
                            try:
                                args = json.loads(args)
                            except (json.JSONDecodeError, ValueError):
                                args = {"raw": args}
                            tool_calls.append(ToolCallRequest(
                                id=call_id,
                                name=call_data["name"],
                                arguments=args,
                            ))

                    # Response completed
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

    def _build_request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
    ) -> dict[str, Any]:
        """Build the Responses API request body.

        ChatGPT backend does not support max_output_tokens or temperature —
        only model, instructions, input, stream, store, and tools.
        """
        # Extract system instructions from messages
        instructions, input_items = self._convert_messages(messages)

        body: dict[str, Any] = {
            "model": model,
            "instructions": instructions or "You are a helpful assistant.",
            "input": input_items,
            "stream": True,
            "store": False,
        }

        if tools:
            body["tools"] = self._convert_tools(tools)

        return body

    @staticmethod
    def _convert_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert OpenAI chat-format messages to Responses API input items.

        Returns (instructions, input_items). System messages are merged
        into the instructions string; all other messages become input items.
        """
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
                # Text content
                if content:
                    items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    })
                # Tool calls
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

    def get_default_model(self) -> str:
        return self.default_model


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


def _format_tool_output(content: Any) -> Any:
    """Format tool output for Responses API function_call_output.

    The Responses API accepts either a string or an array of
    input_text / input_image objects as function_call_output.output.
    """
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        has_images = any(
            isinstance(b, dict) and b.get("type") == "image_url"
            for b in content
        )
        if not has_images:
            # No images — just join text parts into a string
            texts = []
            for block in content:
                if isinstance(block, str):
                    texts.append(block)
                elif isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
            return "\n".join(texts) if texts else str(content)

        # Has images — return array format
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
