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

    # Extra-hard flush (eco mode): tail-only, no head preservation
    EXTRA_HARD_THRESHOLD = 2000
    EXTRA_HARD_KEEP = 200

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
        effective context size.  Only messages that existed at the time of the
        flush are trimmed — newer messages are counted at full size.
        """
        provider = self.get_provider_from_model(model)
        cache = session.metadata.get("cache", {})
        last_flush_type = cache.get("last_flush_type")
        last_flush_at = cache.get("last_flush_at")

        if last_flush_type and last_flush_at:
            sim = [m.copy() for m in messages]
            self._flush_tool_results(sim, last_flush_type, before_ts=last_flush_at)
            total = estimate_messages_tokens(sim, provider)
        elif last_flush_type:
            # Fallback: no timestamp, simulate all (backward compat)
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
                       tools: list[dict] | None = None,
                       context_mode: str = "normal"):
        """Trim large tool results in-place on the LLM message list.

        Flush type is determined by the effective context size (respecting
        previous flush), then applied to ALL raw messages — a clean slate.
        In eco mode, always uses extra_hard flush for maximum savings.

        This modifies the ``messages`` list that will be sent to the API,
        NOT the session history.
        """
        # Eco mode always uses the most aggressive flush
        effective_tokens = self._effective_tokens(messages, model, tools, session)
        if context_mode == "eco":
            flush_type = "extra_hard"
        else:
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
    def _flush_tool_results(cls, messages: list[dict], flush_type: str,
                            before_ts: str | None = None) -> int:
        """Trim large tool results in-place. Returns count of trimmed results.

        Args:
            messages: Message list to modify in-place.
            flush_type: One of "soft", "hard", "extra_hard".
            before_ts: Optional ISO timestamp cutoff. Messages with ``_ts``
                after this value are skipped (they were added after the flush).
        """
        is_extra_hard = flush_type == "extra_hard"

        if flush_type == "soft":
            threshold, keep = cls.SOFT_THRESHOLD, cls.SOFT_KEEP
        elif is_extra_hard:
            threshold, keep = cls.EXTRA_HARD_THRESHOLD, cls.EXTRA_HARD_KEEP
        else:  # "hard"
            threshold, keep = cls.HARD_THRESHOLD, cls.HARD_KEEP

        count = 0
        for msg in messages:
            if msg.get("role") != "tool":
                continue
            # Skip messages added after the flush cutoff
            if before_ts:
                msg_ts = msg.get("_ts") or ""
                if msg_ts > before_ts:
                    continue
            content = msg.get("content", "")
            if not isinstance(content, str) or len(content) <= threshold:
                continue
            if is_extra_hard:
                # Tail only — no head preservation
                msg["content"] = cls.TRIM_TAG + "\n" + content[-keep:]
            else:
                min_trimmed = 2 * keep + len(cls.TRIM_TAG) + 2
                if len(content) <= min_trimmed:
                    continue
                msg["content"] = content[:keep] + f"\n{cls.TRIM_TAG}\n" + content[-keep:]
            count += 1
        return count

    @classmethod
    def flush_for_compaction(cls, messages: list[dict], context_mode: str) -> int:
        """Flush tool results before feeding messages to the compaction LLM.

        Uses hard flush normally, extra_hard for eco mode.
        """
        flush_type = "extra_hard" if context_mode == "eco" else "hard"
        return cls._flush_tool_results(messages, flush_type)

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
