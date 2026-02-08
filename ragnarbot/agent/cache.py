"""Cache manager for prompt caching and tool result flushing."""

from datetime import datetime

from loguru import logger

from ragnarbot.agent.tokens import estimate_messages_tokens, estimate_tools_tokens

# Cache TTL per provider (seconds)
# All providers use prefix-based caching with sliding window behaviour:
# the TTL resets on each cache hit (mark_cache_created updates created_at).
CACHE_TTL = {
    "anthropic": 300,   # 5 min sliding window
    "openai": 600,      # ~5-10 min inactivity; upper bound to avoid premature flush
    "gemini": 300,      # implicit caching — prefix-match like Claude, sliding window
}


class CacheManager:
    """Manages prompt cache lifecycle and tool result flushing.

    When the provider's prompt cache expires (TTL-based), large tool results
    in the LLM message list are trimmed on-the-fly to reduce token costs.
    Session history is never modified — full tool results are always preserved.

    Flush type (soft/hard) is determined by the *effective* context size —
    i.e. what the API actually saw last time (previous flush applied) plus
    new messages at full size.  This prevents overcounting already-flushed
    content when determining aggressiveness.
    """

    # Flush escalation threshold: context ratio above this uses hard flush
    HARD_FLUSH_RATIO = 0.4

    # Soft flush: trim tool results longer than SOFT_THRESHOLD, keeping
    # SOFT_KEEP chars from head and tail.
    SOFT_THRESHOLD = 5000
    SOFT_KEEP = 2500

    # Hard flush: more aggressive thresholds for large contexts
    HARD_THRESHOLD = 2000
    HARD_KEEP = 1000

    TRIM_TAG = "[... trimmed to save tokens ...]"

    def __init__(self, max_context_tokens: int = 200_000):
        if max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive")
        self.max_context_tokens = max_context_tokens

    @staticmethod
    def get_provider_from_model(model: str) -> str:
        """Extract provider name from a model string."""
        lower = model.lower()
        if lower.startswith("anthropic/") or lower.startswith("claude"):
            return "anthropic"
        if lower.startswith("openai/") or lower.startswith("gpt"):
            return "openai"
        if lower.startswith("gemini"):
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

    def _effective_tokens(self, messages: list[dict], model: str,
                          tools: list[dict] | None, session) -> int:
        """Count tokens as the API would see them, respecting previous flush.

        If a previous flush happened, simulates it on a copy to get the
        effective context size.  This avoids overcounting content that was
        already trimmed in the last API call.
        """
        provider = self.get_provider_from_model(model)
        cache = session.metadata.get("cache", {})
        last_flush_type = cache.get("last_flush_type")

        if last_flush_type:
            sim = [m.copy() for m in messages]
            self._flush_tool_results(sim, last_flush_type)
            total = estimate_messages_tokens(sim, provider)
        else:
            total = estimate_messages_tokens(messages, provider)

        if tools:
            total += estimate_tools_tokens(tools)
        return total

    def estimate_context_tokens(
        self, messages: list[dict], model: str,
        tools: list[dict] | None = None,
        session=None,
    ) -> int:
        """Estimate total context tokens for an API call.

        Counts tokens for all messages (system + history + current) and tool
        definitions.  When ``session`` is provided, accounts for previous
        flush state to return the effective token count — i.e. what the
        provider would actually receive.

        Without session or without prior flush history, returns the raw count.

        Args:
            messages: Full LLM message list (from build_messages).
            model: Model string for provider detection.
            tools: Tool definitions (OpenAI format).
            session: Optional session for flush-aware counting.

        Returns:
            Estimated token count.
        """
        if session:
            return self._effective_tokens(messages, model, tools, session)

        provider = self.get_provider_from_model(model)
        total = estimate_messages_tokens(messages, provider)
        if tools:
            total += estimate_tools_tokens(tools)
        return total

    def flush_messages(self, messages: list[dict], session, model: str,
                       tools: list[dict] | None = None):
        """Trim large tool results in-place on the LLM message list.

        Flush type is determined by the effective context size (respecting
        previous flush), then applied to ALL raw messages — a clean slate.

        This modifies the ``messages`` list that will be sent to the API,
        NOT the session history.
        """
        # Effective count (with previous flush simulated) → determines type
        effective_tokens = self._effective_tokens(messages, model, tools, session)
        ratio = effective_tokens / self.max_context_tokens
        flush_type = "soft" if ratio <= self.HARD_FLUSH_RATIO else "hard"

        # Apply flush to raw messages — new flushing cursor
        flushed = self._flush_tool_results(messages, flush_type)

        cache = session.metadata.get("cache", {})
        session.metadata["cache"] = {
            "created_at": cache.get("created_at"),
            "last_flush_at": datetime.now().isoformat(),
            "last_flush_type": flush_type,
        }

        logger.info(
            f"Cache flush ({flush_type}): {effective_tokens} effective tokens, "
            f"{flushed} results trimmed"
        )

    @classmethod
    def _flush_tool_results(cls, messages: list[dict], flush_type: str) -> int:
        """Trim large tool results in-place. Returns count of trimmed results."""
        if flush_type == "soft":
            threshold, keep = cls.SOFT_THRESHOLD, cls.SOFT_KEEP
        else:  # "hard"
            threshold, keep = cls.HARD_THRESHOLD, cls.HARD_KEEP

        min_trimmed = 2 * keep + len(cls.TRIM_TAG) + 2  # +2 for \n wrappers

        count = 0
        for msg in messages:
            if msg.get("role") != "tool":
                continue
            content = msg.get("content", "")
            if not isinstance(content, str) or len(content) <= threshold:
                continue
            # Skip if trimming would produce a result equal or larger than the original
            if len(content) <= min_trimmed:
                continue
            msg["content"] = content[:keep] + f"\n{cls.TRIM_TAG}\n" + content[-keep:]
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
