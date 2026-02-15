"""LiteLLM provider implementation for multi-provider support."""

import os
from typing import Any

import litellm
from litellm import acompletion

from ragnarbot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.

    Supports Anthropic, OpenAI, and Gemini through a unified interface.
    """

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str = "anthropic/claude-opus-4-5",
        oauth_token: str | None = None,
    ):
        super().__init__(api_key, oauth_token)
        self.default_model = default_model

        # Configure LiteLLM env vars based on provider
        # Check openrouter first — model strings like openrouter/anthropic/...
        # contain provider substrings that would match the wrong branch.
        if api_key:
            if "openrouter" in default_model:
                os.environ.setdefault("OPENROUTER_API_KEY", api_key)
            elif "anthropic" in default_model:
                os.environ.setdefault("ANTHROPIC_API_KEY", api_key)
            elif "openai" in default_model or "gpt" in default_model:
                os.environ.setdefault("OPENAI_API_KEY", api_key)
            elif "gemini" in default_model.lower():
                os.environ.setdefault("GEMINI_API_KEY", api_key)

        # Disable LiteLLM logging noise
        litellm.suppress_debug_info = True
    
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """
        Send a chat completion request via LiteLLM.
        
        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions in OpenAI format.
            model: Model identifier (e.g., 'anthropic/claude-sonnet-4-5').
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
        
        Returns:
            LLMResponse with content and/or tool calls.
        """
        model = model or self.default_model
        max_tokens = max_tokens if max_tokens is not None else self.default_max_tokens
        temperature = temperature if temperature is not None else self.default_temperature

        is_openrouter = model.startswith("openrouter/")

        # For Gemini, ensure gemini/ prefix if not already present (skip for OpenRouter)
        if not is_openrouter and "gemini" in model.lower() and not model.startswith("gemini/"):
            model = f"gemini/{model}"

        # Inject cache_control for Anthropic and Gemini models (skip for OpenRouter)
        if not is_openrouter and ("anthropic" in model or "gemini" in model.lower()):
            messages = self._inject_cache_control(messages)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        # OpenRouter provider routing
        if is_openrouter:
            from ragnarbot.config.providers import get_model_info
            provider_config: dict = {"sort": "throughput", "allow_fallbacks": True}
            model_info = get_model_info(model)
            if model_info and model_info.get("providers"):
                provider_config["only"] = model_info["providers"]
            kwargs["extra_body"] = {"provider": provider_config}

        # Strip internal metadata keys (e.g. _image_path) from content blocks
        kwargs["messages"] = self._sanitize_messages(kwargs["messages"])

        # OpenRouter: downgrade multimodal tool results to text-only.
        # Many sub-providers support images in user messages but not in
        # tool results, causing silent failures or empty responses.
        if is_openrouter:
            kwargs["messages"] = self._downgrade_tool_images(kwargs["messages"])

        try:
            response = await acompletion(**kwargs)
            return self._parse_response(response)
        except Exception as e:
            # Return error as content for graceful handling
            return LLMResponse(
                content=f"Error calling LLM: {str(e)}",
                finish_reason="error",
            )
    
    @staticmethod
    def _sanitize_messages(messages: list[dict]) -> list[dict]:
        """Strip internal underscore-prefixed keys from content block dicts.

        Keys like ``_image_path`` and ``_mime_type`` are used internally
        for session persistence but must not reach the LLM API.
        """
        cleaned = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                new_content = []
                for block in content:
                    if isinstance(block, dict) and any(k.startswith("_") for k in block):
                        block = {k: v for k, v in block.items() if not k.startswith("_")}
                    new_content.append(block)
                msg = {**msg, "content": new_content}
            cleaned.append(msg)
        return cleaned

    @staticmethod
    def _downgrade_tool_images(messages: list[dict]) -> list[dict]:
        """Convert multimodal tool results to text-only.

        Some providers support images in user messages but not in tool
        results. This extracts text blocks and discards image blocks.
        """
        cleaned = []
        for msg in messages:
            if msg.get("role") == "tool" and isinstance(msg.get("content"), list):
                text_parts = [
                    b.get("text", "") for b in msg["content"]
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                msg = {**msg, "content": " ".join(text_parts) if text_parts else "[image]"}
            cleaned.append(msg)
        return cleaned

    @staticmethod
    def _inject_cache_control(messages: list[dict]) -> list[dict]:
        """Add cache_control breakpoints to messages for Anthropic/Gemini via LiteLLM."""
        messages = [m.copy() for m in messages]

        # Breakpoint 1: System prompt
        for msg in messages:
            if msg["role"] == "system":
                if isinstance(msg["content"], str):
                    msg["content"] = [{
                        "type": "text",
                        "text": msg["content"],
                        "cache_control": {"type": "ephemeral"},
                    }]
                elif isinstance(msg["content"], list):
                    msg["content"] = [b.copy() for b in msg["content"]]
                    if msg["content"]:
                        msg["content"][-1] = {
                            **msg["content"][-1],
                            "cache_control": {"type": "ephemeral"},
                        }
                break

        # Breakpoint 2: Sliding — last tool result message so accumulated
        # tool results are cached across agent-loop iterations.
        # Fallback: 2nd-to-last user message (first call, no tool results yet).
        bp2_set = False
        for i in range(len(messages) - 1, -1, -1):
            if messages[i]["role"] == "tool":
                messages[i]["cache_control"] = {"type": "ephemeral"}
                bp2_set = True
                break

        if not bp2_set:
            user_count = 0
            for i in range(len(messages) - 1, -1, -1):
                if messages[i]["role"] == "user":
                    user_count += 1
                    if user_count == 2:
                        content = messages[i]["content"]
                        if isinstance(content, str):
                            messages[i]["content"] = [{
                                "type": "text",
                                "text": content,
                                "cache_control": {"type": "ephemeral"},
                            }]
                        elif isinstance(content, list):
                            messages[i]["content"] = [b.copy() for b in content]
                            if messages[i]["content"]:
                                messages[i]["content"][-1] = {
                                    **messages[i]["content"][-1],
                                    "cache_control": {"type": "ephemeral"},
                                }
                        break

        return messages

    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse LiteLLM response into our standard format."""
        choice = response.choices[0]
        message = choice.message
        
        tool_calls = []
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                # Parse arguments from JSON string if needed
                args = tc.function.arguments
                if isinstance(args, str):
                    import json
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}
                
                tool_calls.append(ToolCallRequest(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))
        
        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
            # Extract cache usage from prompt_tokens_details (LiteLLM unified format)
            details = getattr(response.usage, "prompt_tokens_details", None)
            if details:
                if isinstance(details, dict):
                    usage["cache_creation_input_tokens"] = (
                        details.get("cache_creation_input_tokens", 0) or 0
                    )
                    usage["cache_read_input_tokens"] = (
                        details.get("cache_read_input_tokens", 0) or 0
                    )
                    usage["cached_tokens"] = details.get("cached_tokens", 0) or 0
        
        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )
    
    def get_default_model(self) -> str:
        """Get the default model."""
        return self.default_model
