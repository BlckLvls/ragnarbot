"""Cache manager for prompt caching and tool result flushing."""

from datetime import datetime

from loguru import logger

from ragnarbot.agent.tokens import estimate_messages_tokens, estimate_tools_tokens

# Cache TTL per provider (seconds)
CACHE_TTL = {
    "anthropic": 300,  # 5 minutes
    "openai": 300,     # ~5 minutes (approximate)
    "gemini": 300,     # 5 minutes
}


class CacheManager:
    """Manages prompt cache lifecycle and tool result flushing.

    When the provider's prompt cache expires (TTL-based), large tool results
    in session history are trimmed to reduce token costs on the next cache
    creation.
    """

    def __init__(self, max_context_tokens: int = 200_000):
        self.max_context_tokens = max_context_tokens

    @staticmethod
    def get_provider_from_model(model: str) -> str:
        """Extract provider name from a model string."""
        lower = model.lower()
        if lower.startswith("anthropic/") or "claude" in lower:
            return "anthropic"
        if lower.startswith("openai/") or lower.startswith("gpt"):
            return "openai"
        if lower.startswith("gemini/") or "gemini" in lower:
            return "gemini"
        return "anthropic"

    def get_cache_ttl(self, model: str) -> int:
        """Get cache TTL in seconds for a model."""
        provider = self.get_provider_from_model(model)
        return CACHE_TTL.get(provider, 300)

    def should_flush(self, session, model: str) -> bool:
        """Check if cache has expired and flushing is needed."""
        cache = session.metadata.get("cache", {})
        created_at = cache.get("created_at")
        if not created_at:
            return False  # No cache ever created — nothing to flush

        try:
            created_dt = datetime.fromisoformat(created_at)
        except (ValueError, TypeError):
            return False

        elapsed = (datetime.now() - created_dt).total_seconds()
        return elapsed >= self.get_cache_ttl(model)

    def perform_flush(self, session, model: str, tools: list[dict] | None = None):
        """Count tokens, determine flush level, flush tool results in-place."""
        provider = self.get_provider_from_model(model)
        cache = session.metadata.get("cache", {})

        total_tokens = estimate_messages_tokens(session.messages, provider)
        total_tokens += 5000  # System prompt buffer
        if tools:
            total_tokens += estimate_tools_tokens(tools)

        ratio = total_tokens / self.max_context_tokens
        flush_type = "soft" if ratio <= 0.4 else "hard"

        flushed = self._flush_tool_results(session.messages, flush_type)

        session.metadata["cache"] = {
            "created_at": cache.get("created_at"),
            "last_flush_at": datetime.now().isoformat(),
            "last_flush_type": flush_type,
        }

        logger.info(
            f"Cache flush ({flush_type}): {total_tokens} est. tokens, "
            f"{flushed} results trimmed"
        )

    @staticmethod
    def _flush_tool_results(messages: list[dict], flush_type: str) -> int:
        """Trim large tool results in-place. Returns count of trimmed results."""
        trim_tag = "[... trimmed to save tokens ...]"

        if flush_type == "soft":
            threshold, keep = 5000, 2500
        else:  # "hard"
            threshold, keep = 2000, 1000

        count = 0
        for msg in messages:
            if msg.get("role") != "tool":
                continue
            content = msg.get("content", "")
            if not isinstance(content, str) or len(content) <= threshold:
                continue
            msg["content"] = content[:keep] + f"\n{trim_tag}\n" + content[-keep:]
            count += 1
        return count

    @staticmethod
    def mark_cache_created(session, usage: dict):
        """Update cache metadata after LLM call if caching occurred."""
        if not isinstance(usage, dict):
            return
        cache = session.metadata.setdefault("cache", {})
        cache_created = usage.get("cache_creation_input_tokens", 0) or 0
        cache_read = usage.get("cache_read_input_tokens", 0) or 0
        if cache_created > 0 or cache_read > 0:
            cache["created_at"] = datetime.now().isoformat()
