"""Approximate token estimation for context management."""

import json

CHARS_PER_TOKEN = 4  # Cross-model estimate (EN text/code/JSON)


def estimate_tokens(text: str) -> int:
    """Estimate token count from character count."""
    return len(text) // CHARS_PER_TOKEN


def estimate_image_tokens(provider: str) -> int:
    """Estimate tokens for a single image by provider.

    Approximations based on typical chat image sizes (~800x600):
    - Anthropic: (w*h)/750 ~ 640, rounded up to 800
    - OpenAI: tile-based high-detail average ~ 400
    - Gemini: fixed 258 tokens per image
    """
    estimates = {"anthropic": 800, "openai": 400, "gemini": 258}
    return estimates.get(provider, 800)


def estimate_messages_tokens(messages: list[dict], provider: str = "anthropic") -> int:
    """Estimate total tokens for a message list."""
    total = 0
    for msg in messages:
        total += 4  # Per-message overhead (role, separators)
        content = msg.get("content", "")

        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        total += estimate_tokens(block.get("text", ""))
                    elif block.get("type") in ("image_url", "image"):
                        total += estimate_image_tokens(provider)

        # Tool calls in assistant messages
        if "tool_calls" in msg:
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                args = fn.get("arguments", "")
                if isinstance(args, dict):
                    args = json.dumps(args)
                total += estimate_tokens(str(fn.get("name", "")))
                total += estimate_tokens(str(args))

    return total


def estimate_tools_tokens(tools: list[dict]) -> int:
    """Estimate tokens for tool definitions."""
    return estimate_tokens(json.dumps(tools))
