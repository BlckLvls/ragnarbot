"""LiteLLM provider implementation for multi-provider support."""

import os
from typing import Any

import litellm
from litellm import acompletion

from ragnarbot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

# Workaround for LiteLLM bug #19618: get_anthropic_headers() always emits
# x-api-key, but Anthropic OAuth tokens only work via Authorization: Bearer.
# When both headers are present, x-api-key takes precedence and fails (401).
# This patch removes x-api-key when the api_key is an OAuth token (sk-ant-oat*),
# letting the Authorization: Bearer header handle auth instead.
_OAUTH_PREFIX = "sk-ant-oat"
_oauth_patch_applied = False


def _apply_oauth_header_patch():
    global _oauth_patch_applied
    if _oauth_patch_applied:
        return
    _oauth_patch_applied = True

    from litellm.llms.anthropic.common_utils import AnthropicModelInfo

    _original = AnthropicModelInfo.get_anthropic_headers

    def _patched(self, api_key, **kwargs):
        headers = _original(self, api_key=api_key, **kwargs)
        if isinstance(api_key, str) and api_key.startswith(_OAUTH_PREFIX):
            headers.pop("x-api-key", None)
        return headers

    AnthropicModelInfo.get_anthropic_headers = _patched


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.

    Supports Anthropic, OpenAI, and Gemini through a unified interface.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "anthropic/claude-opus-4-5",
        oauth_token: str | None = None,
    ):
        super().__init__(api_key, api_base, oauth_token)
        self.default_model = default_model

        # Configure LiteLLM env vars (not needed for OAuth — handled per-request)
        if api_key and not oauth_token:
            if "anthropic" in default_model:
                os.environ.setdefault("ANTHROPIC_API_KEY", api_key)
            elif "openai" in default_model or "gpt" in default_model:
                os.environ.setdefault("OPENAI_API_KEY", api_key)
            elif "gemini" in default_model.lower():
                os.environ.setdefault("GEMINI_API_KEY", api_key)

        if oauth_token:
            _apply_oauth_header_patch()

        if api_base:
            litellm.api_base = api_base

        # Disable LiteLLM logging noise
        litellm.suppress_debug_info = True
    
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
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

        # For Gemini, ensure gemini/ prefix if not already present
        if "gemini" in model.lower() and not model.startswith("gemini/"):
            model = f"gemini/{model}"
        
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        
        # Pass api_base directly for custom endpoints (vLLM, etc.)
        if self.api_base:
            kwargs["api_base"] = self.api_base
        
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        # OAuth for Anthropic: Authorization: Bearer + beta header.
        # api_key kwarg passes LiteLLM's early validation.
        # Our _apply_oauth_header_patch removes the conflicting x-api-key
        # that LiteLLM always adds (bug #19618).
        if self.oauth_token and "anthropic" in model:
            await self._maybe_refresh_token()
            kwargs["api_key"] = self.oauth_token
            kwargs["extra_headers"] = {
                "Authorization": f"Bearer {self.oauth_token}",
                "anthropic-beta": "oauth-2025-04-20",
            }

        try:
            response = await acompletion(**kwargs)
            return self._parse_response(response)
        except Exception as e:
            # Return error as content for graceful handling
            return LLMResponse(
                content=f"Error calling LLM: {str(e)}",
                finish_reason="error",
            )
    
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
        
        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )
    
    async def _maybe_refresh_token(self) -> None:
        """Refresh OAuth token if expired."""
        if not self.oauth_token:
            return
        try:
            from ragnarbot.auth.credentials import load_credentials
            from ragnarbot.auth.oauth import ensure_valid_token
            creds = load_credentials()
            new_token = await ensure_valid_token(creds)
            if new_token:
                self.oauth_token = new_token
        except Exception:
            pass  # Keep existing token on failure

    def get_default_model(self) -> str:
        """Get the default model."""
        return self.default_model
