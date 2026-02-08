"""Tests for cache manager module."""

from datetime import datetime, timedelta

from ragnarbot.agent.cache import CACHE_TTL, CacheManager


class FakeSession:
    """Minimal session stand-in for testing."""

    def __init__(self, messages=None, metadata=None):
        self.messages = messages or []
        self.metadata = metadata or {}


class TestGetProviderFromModel:
    def test_anthropic_prefix(self):
        assert CacheManager.get_provider_from_model("anthropic/claude-opus-4-6") == "anthropic"

    def test_claude_without_prefix(self):
        assert CacheManager.get_provider_from_model("claude-sonnet-4-5") == "anthropic"

    def test_openai_prefix(self):
        assert CacheManager.get_provider_from_model("openai/gpt-4o") == "openai"

    def test_gpt_without_prefix(self):
        assert CacheManager.get_provider_from_model("gpt-4o") == "openai"

    def test_gemini_prefix(self):
        assert CacheManager.get_provider_from_model("gemini/gemini-2.0-flash") == "gemini"

    def test_unknown_defaults_to_anthropic(self):
        assert CacheManager.get_provider_from_model("some-random-model") == "anthropic"


class TestShouldFlush:
    def test_no_cache_metadata(self):
        cm = CacheManager()
        session = FakeSession()
        assert cm.should_flush(session, "anthropic/claude-opus-4-6") is False

    def test_cache_not_expired(self):
        cm = CacheManager()
        session = FakeSession(metadata={
            "cache": {"created_at": datetime.now().isoformat()}
        })
        assert cm.should_flush(session, "anthropic/claude-opus-4-6") is False

    def test_cache_expired(self):
        cm = CacheManager()
        expired_time = datetime.now() - timedelta(seconds=CACHE_TTL["anthropic"] + 10)
        session = FakeSession(metadata={
            "cache": {"created_at": expired_time.isoformat()}
        })
        assert cm.should_flush(session, "anthropic/claude-opus-4-6") is True

    def test_invalid_created_at(self):
        cm = CacheManager()
        session = FakeSession(metadata={
            "cache": {"created_at": "not-a-date"}
        })
        assert cm.should_flush(session, "anthropic/claude-opus-4-6") is False


class TestFlushToolResults:
    def test_soft_flush_trims_large_results(self):
        large_content = "x" * 10000
        messages = [
            {"role": "user", "content": "Do something"},
            {"role": "tool", "tool_call_id": "1", "content": large_content},
            {"role": "tool", "tool_call_id": "2", "content": "short"},
        ]
        count = CacheManager._flush_tool_results(messages, "soft")
        assert count == 1
        assert len(messages[1]["content"]) < len(large_content)
        assert "[... trimmed to save tokens ...]" in messages[1]["content"]
        assert messages[2]["content"] == "short"  # Unchanged

    def test_hard_flush_lower_threshold(self):
        content_3k = "y" * 3000
        messages = [
            {"role": "tool", "tool_call_id": "1", "content": content_3k},
        ]
        # Soft flush won't touch 3k (threshold is 5000)
        count_soft = CacheManager._flush_tool_results(messages, "soft")
        assert count_soft == 0

        # Hard flush will trim 3k (threshold is 2000)
        count_hard = CacheManager._flush_tool_results(messages, "hard")
        assert count_hard == 1
        assert "[... trimmed to save tokens ...]" in messages[0]["content"]

    def test_non_tool_messages_untouched(self):
        large_content = "z" * 10000
        messages = [
            {"role": "assistant", "content": large_content},
            {"role": "user", "content": large_content},
        ]
        count = CacheManager._flush_tool_results(messages, "hard")
        assert count == 0
        assert messages[0]["content"] == large_content
        assert messages[1]["content"] == large_content

    def test_escalation_soft_then_hard(self):
        """Soft-flushed results should get further trimmed on hard flush."""
        large_content = "a" * 10000
        messages = [
            {"role": "tool", "tool_call_id": "1", "content": large_content},
        ]
        # First: soft flush
        CacheManager._flush_tool_results(messages, "soft")
        soft_result = messages[0]["content"]
        # Should be ~5000+tag chars
        assert len(soft_result) < 10000
        assert "[... trimmed to save tokens ...]" in soft_result

        # Second: hard flush (soft result is ~5050 chars, above hard threshold 2000)
        CacheManager._flush_tool_results(messages, "hard")
        hard_result = messages[0]["content"]
        assert len(hard_result) < len(soft_result)


class TestPerformFlush:
    def test_soft_flush_under_40_percent(self):
        cm = CacheManager(max_context_tokens=200_000)
        # Create messages totaling well under 40% of 200k (80k chars ~ 20k tokens)
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "tool", "tool_call_id": "1", "content": "x" * 10000},
        ]
        session = FakeSession(
            messages=messages,
            metadata={"cache": {"created_at": datetime.now().isoformat()}},
        )
        cm.perform_flush(session, "anthropic/claude-opus-4-6")
        assert session.metadata["cache"]["last_flush_type"] == "soft"

    def test_hard_flush_over_40_percent(self):
        cm = CacheManager(max_context_tokens=10_000)
        # Create messages large enough to exceed 40% of 10k tokens
        messages = [
            {"role": "user", "content": "a" * 8000},
            {"role": "tool", "tool_call_id": "1", "content": "b" * 20000},
        ]
        session = FakeSession(
            messages=messages,
            metadata={"cache": {"created_at": datetime.now().isoformat()}},
        )
        cm.perform_flush(session, "anthropic/claude-opus-4-6")
        assert session.metadata["cache"]["last_flush_type"] == "hard"

    def test_flush_updates_metadata(self):
        cm = CacheManager()
        messages = [
            {"role": "user", "content": "test"},
            {"role": "tool", "tool_call_id": "1", "content": "x" * 6000},
        ]
        created_at = datetime.now().isoformat()
        session = FakeSession(
            messages=messages,
            metadata={"cache": {"created_at": created_at}},
        )
        cm.perform_flush(session, "anthropic/claude-opus-4-6")
        cache = session.metadata["cache"]
        assert cache["created_at"] == created_at  # Preserved
        assert "last_flush_at" in cache
        assert "last_flush_type" in cache


class TestMarkCacheCreated:
    def test_sets_created_at_on_cache_creation(self):
        session = FakeSession()
        CacheManager.mark_cache_created(session, {
            "cache_creation_input_tokens": 5000,
            "cache_read_input_tokens": 0,
        })
        assert "created_at" in session.metadata["cache"]

    def test_sets_created_at_on_cache_read(self):
        session = FakeSession()
        CacheManager.mark_cache_created(session, {
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 3000,
        })
        assert "created_at" in session.metadata["cache"]

    def test_no_update_without_cache_activity(self):
        session = FakeSession()
        CacheManager.mark_cache_created(session, {
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        })
        cache = session.metadata.get("cache", {})
        assert "created_at" not in cache

    def test_no_update_with_empty_usage(self):
        session = FakeSession()
        CacheManager.mark_cache_created(session, {})
        cache = session.metadata.get("cache", {})
        assert "created_at" not in cache
