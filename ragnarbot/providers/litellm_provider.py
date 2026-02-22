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
        max_tokens: int = 16_000,
        temperature: float = 0.7,
    ):
        super().__init__(api_key, oauth_token, max_tokens=max_tokens, temperature=temperature)
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
            kwargs["extra_body"] = {
                "provider": provider_config,
                "reasoning": {"enabled": True},
            }

        # Strip internal metadata keys (e.g. _image_path) from content blocks
        kwargs["messages"] = self._sanitize_messages(kwargs["messages"])

        # Strip images for models that don't support vision — catches
        # historical images in session that predate a model switch.
        from ragnarbot.config.providers import model_supports_vision
        if not model_supports_vision(model):
            kwargs["messages"] = self._strip_images(kwargs["messages"])

        # OpenRouter: downgrade multimodal tool results to text-only and
        # re-inject stripped images as synthetic user messages so the LLM
        # can still see them.
        if is_openrouter:
            kwargs["messages"] = self._adapt_tool_images(kwargs["messages"])

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
    def _strip_images(messages: list[dict]) -> list[dict]:
        """Remove image_url blocks from all messages for non-vision models.

        Replaces stripped images with a short placeholder so the LLM
        knows content was omitted.
        """
        result = []
        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, list):
                result.append(msg)
                continue

            non_image = [
                b for b in content
                if not (isinstance(b, dict) and b.get("type") == "image_url")
            ]
            n_removed = len(content) - len(non_image)

            if n_removed == 0:
                result.append(msg)
                continue

            label = "image" if n_removed == 1 else f"{n_removed} images"
            non_image.append({
                "type": "text",
                "text": f"[{label} omitted — current model does not support vision]",
            })
            result.append({**msg, "content": non_image})

        return result

    @staticmethod
    def _adapt_tool_images(messages: list[dict]) -> list[dict]:
        """Adapt multimodal tool results for OpenRouter.

        OpenRouter tool messages only accept string content. This method:
        1. Downgrades ALL tool results with list content to text-only
        2. After each consecutive tool block that contained images,
           inserts a synthetic user message carrying those images

        Images remain visible to the LLM until tool flushing converts
        the multimodal content to text (via apply_previous_flush).
        After that, no list content remains → no injection occurs.
        """
        result: list[dict] = []
        i = 0

        while i < len(messages):
            msg = messages[i]

            # Not a tool message — pass through
            if msg.get("role") != "tool":
                result.append(msg)
                i += 1
                continue

            # Collect consecutive tool block, downgrade and extract images
            block_images: list[dict] = []
            while i < len(messages) and messages[i].get("role") == "tool":
                tool_msg = messages[i]
                content = tool_msg.get("content")

                if isinstance(content, list):
                    # Extract images
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "image_url":
                            block_images.append(block)
                    # Downgrade to text
                    text_parts = [
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    tool_msg = {
                        **tool_msg,
                        "content": " ".join(text_parts) if text_parts else "[image]",
                    }

                result.append(tool_msg)
                i += 1

            # Inject synthetic user message with collected images
            if block_images:
                count = len(block_images)
                label = "image" if count == 1 else f"{count} images"
                result.append({
                    "role": "user",
                    "content": block_images + [{
                        "type": "text",
                        "text": f"[{label} from tool result above]",
                    }],
                })

        return result

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

    @staticmethod
    def _recover_truncated_json(raw: str) -> dict:
        """Best-effort recovery of fields from truncated JSON.

        When the LLM output is cut off mid-JSON (e.g. due to max_tokens),
        json.loads fails.  We try to extract top-level string fields so that
        tools still receive usable arguments instead of an opaque 'raw' blob.
        """
        import re

        recovered: dict[str, str] = {}
        # Match top-level "key": "value" pairs (handles escaped quotes)
        for m in re.finditer(r'"(\w+)"\s*:\s*"((?:[^"\\]|\\.)*)"', raw):
            recovered[m.group(1)] = m.group(2).replace('\\"', '"').replace("\\n", "\n")

        if not recovered:
            return {"raw": raw}

        # Try to find if there's a value that was truncated (last key)
        # by checking if the raw string ends without closing the JSON
        last_key_match = list(re.finditer(r'"(\w+)"\s*:\s*"', raw))
        if last_key_match:
            last_key = last_key_match[-1].group(1)
            last_start = last_key_match[-1].end()
            # Extract everything after the last opening quote to end of string
            rest = raw[last_start:]
            # Remove trailing incomplete escapes/quotes
            rest = rest.rstrip('\\')
            if last_key not in recovered:
                recovered[last_key] = rest.replace('\\"', '"').replace("\\n", "\n")

        return recovered

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
                        args = self._recover_truncated_json(args)
                
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
