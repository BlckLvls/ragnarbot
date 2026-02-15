"""Gemini Code Assist provider for OAuth-based Gemini access.

Uses the Code Assist API at cloudcode-pa.googleapis.com which wraps
Gemini requests in an envelope format and returns SSE responses.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx

from ragnarbot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

CODE_ASSIST_BASE = "https://cloudcode-pa.googleapis.com"
STREAM_URL = f"{CODE_ASSIST_BASE}/v1internal:streamGenerateContent"

_CLIENT_METADATA = json.dumps({
    "ideType": "IDE_UNSPECIFIED",
    "platform": "PLATFORM_UNSPECIFIED",
    "pluginType": "GEMINI",
})

# Gemini 3 models require thinkingConfig — without it the API returns empty body.
_GEMINI3_THINKING = {
    "pro": {"includeThoughts": True, "thinkingLevel": "LOW"},
    "flash": {"includeThoughts": True, "thinkingLevel": "MEDIUM"},
}


class GeminiCodeAssistProvider(LLMProvider):
    """LLM provider for Gemini via the Code Assist API (OAuth)."""

    def __init__(self, default_model: str = "gemini-2.5-flash"):
        super().__init__()
        self.default_model = default_model

        from ragnarbot.auth.gemini_oauth import get_project_id
        self._project_id = get_project_id()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        from ragnarbot.auth.gemini_oauth import get_access_token

        model = model or self.default_model
        max_tokens = max_tokens if max_tokens is not None else self.default_max_tokens
        temperature = temperature if temperature is not None else self.default_temperature

        # Strip provider prefix (e.g. "gemini/gemini-2.5-flash" → "gemini-2.5-flash")
        if model.startswith("gemini/"):
            model = model[len("gemini/"):]

        access_token = get_access_token()
        if not access_token:
            return LLMResponse(
                content="Error: Gemini OAuth token not available. Run: ragnarbot oauth gemini",
                finish_reason="error",
            )

        # Build request
        system_instruction, contents = self._convert_messages(messages)

        generation_config: dict[str, Any] = {}
        if max_tokens:
            generation_config["maxOutputTokens"] = max_tokens
        if temperature is not None:
            generation_config["temperature"] = temperature

        # Gemini 3 models require thinkingConfig
        thinking = _get_thinking_config(model)
        if thinking:
            generation_config["thinkingConfig"] = thinking

        gemini_request: dict[str, Any] = {"contents": contents}
        if system_instruction:
            gemini_request["systemInstruction"] = {"parts": [{"text": system_instruction}]}
        if generation_config:
            gemini_request["generationConfig"] = generation_config
        if tools:
            gemini_request["tools"] = self._convert_tools(tools)
            gemini_request["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

        # Wrap in Code Assist envelope
        envelope = {
            "project": self._project_id,
            "model": model,
            "requestId": f"ragnarbot-{uuid.uuid4().hex[:12]}",
            "userAgent": "ragnarbot",
            "request": gemini_request,
        }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Client-Metadata": _CLIENT_METADATA,
        }

        try:
            return await self._stream_request(envelope, headers)
        except Exception as e:
            return LLMResponse(
                content=f"Error calling Gemini Code Assist: {e}",
                finish_reason="error",
            )

    async def _stream_request(
        self, envelope: dict, headers: dict
    ) -> LLMResponse:
        """POST to the streaming endpoint and parse SSE events."""
        text_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        finish_reason = "stop"
        usage: dict[str, int] = {}

        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            async with client.stream(
                "POST",
                f"{STREAM_URL}?alt=sse",
                json=envelope,
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

                    # Unwrap Code Assist envelope
                    inner = event.get("response", event)

                    for candidate in inner.get("candidates", []):
                        content = candidate.get("content", {})
                        for part in content.get("parts", []):
                            # Skip thinking parts
                            if part.get("thought"):
                                continue
                            if "text" in part:
                                text_parts.append(part["text"])
                            elif "functionCall" in part:
                                fc = part["functionCall"]
                                call_id = fc.get("id") or (
                                    f"{fc.get('name', 'fn')}_{int(time.time())}_{len(tool_calls)}"
                                )
                                tool_calls.append(ToolCallRequest(
                                    id=call_id,
                                    name=fc.get("name", ""),
                                    arguments=fc.get("args", {}),
                                ))

                        fr = candidate.get("finishReason")
                        if fr == "STOP":
                            finish_reason = "stop"
                        elif fr == "MAX_TOKENS":
                            finish_reason = "length"

                    usage_meta = inner.get("usageMetadata", {})
                    if usage_meta and usage_meta.get("totalTokenCount"):
                        usage = {
                            "prompt_tokens": usage_meta.get("promptTokenCount", 0),
                            "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
                            "total_tokens": usage_meta.get("totalTokenCount", 0),
                        }

        if tool_calls:
            finish_reason = "tool_calls"

        return LLMResponse(
            content="".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )

    @staticmethod
    def _convert_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert OpenAI-format messages to Gemini contents array.

        Returns (system_instruction, contents).
        """
        contents: list[dict[str, Any]] = []
        system_parts: list[str] = []

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")

            if role == "system":
                if isinstance(content, str):
                    system_parts.append(content)
                continue

            if role == "user":
                parts = _content_to_parts(content)
                contents.append({"role": "user", "parts": parts})

            elif role == "assistant":
                parts: list[dict] = []
                if content:
                    parts.append({"text": content})
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, ValueError):
                            args = {"raw": args}
                    fc_item: dict[str, Any] = {
                        "functionCall": {
                            "name": fn.get("name", ""),
                            "args": args,
                        }
                    }
                    # Preserve ID for round-trip
                    tc_id = tc.get("id")
                    if tc_id:
                        fc_item["functionCall"]["id"] = tc_id
                    parts.append(fc_item)
                if parts:
                    contents.append({"role": "model", "parts": parts})

            elif role == "tool":
                tool_name = msg.get("name", "unknown")
                tool_content = content if isinstance(content, str) else str(content)
                fr_item: dict[str, Any] = {
                    "functionResponse": {
                        "name": tool_name,
                        "response": {"output": tool_content},
                    }
                }
                tc_id = msg.get("tool_call_id")
                if tc_id:
                    fr_item["functionResponse"]["id"] = tc_id
                contents.append({
                    "role": "user",
                    "parts": [fr_item],
                })

        # Merge consecutive same-role messages (Gemini requires alternation)
        contents = _merge_consecutive(contents)

        system_instruction = "\n\n".join(system_parts) if system_parts else None
        return system_instruction, contents

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI tool definitions to Gemini function declarations."""
        declarations = []
        for tool in tools:
            fn = tool.get("function", {})
            params = fn.get("parameters", {"type": "object", "properties": {}})
            declarations.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parametersJsonSchema": params,
            })
        return [{"functionDeclarations": declarations}]

    def get_default_model(self) -> str:
        return self.default_model


def _get_thinking_config(model: str) -> dict[str, Any] | None:
    """Return thinkingConfig for Gemini 3 models, None for others."""
    if "gemini-3" not in model:
        return None
    if "pro" in model:
        return _GEMINI3_THINKING["pro"]
    if "flash" in model:
        return _GEMINI3_THINKING["flash"]
    return _GEMINI3_THINKING["flash"]


def _content_to_parts(content: Any) -> list[dict]:
    """Convert message content to Gemini parts."""
    if isinstance(content, str):
        return [{"text": content}]

    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append({"text": block})
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append({"text": block.get("text", "")})
                elif block.get("type") == "image_url":
                    url = block.get("image_url", {}).get("url", "")
                    if url.startswith("data:"):
                        header, data = url.split(",", 1)
                        mime = header.split(":")[1].split(";")[0]
                        parts.append({
                            "inlineData": {
                                "mimeType": mime,
                                "data": data,
                            }
                        })
                    else:
                        parts.append({"text": f"[image: {url}]"})
        return parts or [{"text": ""}]

    return [{"text": str(content)}]


def _merge_consecutive(contents: list[dict]) -> list[dict]:
    """Merge consecutive same-role messages for Gemini's alternation requirement."""
    if not contents:
        return contents

    merged = [contents[0]]
    for entry in contents[1:]:
        if entry.get("role") == merged[-1].get("role"):
            merged[-1]["parts"].extend(entry.get("parts", []))
        else:
            merged.append(entry)

    return merged
