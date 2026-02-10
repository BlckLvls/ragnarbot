"""Tests for cache manager module."""

import pytest
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

    def test_before_ts_skips_newer_messages(self):
        """Messages with _ts after the cutoff should NOT be trimmed."""
        messages = [
            {"role": "tool", "tool_call_id": "1", "content": "x" * 10000,
             "_ts": "2026-02-09T10:00:00"},
            {"role": "tool", "tool_call_id": "2", "content": "y" * 10000,
             "_ts": "2026-02-09T12:00:00"},
        ]
        count = CacheManager._flush_tool_results(
            messages, "soft", before_ts="2026-02-09T11:00:00"
        )
        # Only the first message (before cutoff) should be trimmed
        assert count == 1
        assert "[... trimmed to save tokens ...]" in messages[0]["content"]
        assert messages[1]["content"] == "y" * 10000  # Untouched

    def test_before_ts_none_trims_all(self):
        """Without before_ts, all eligible messages are trimmed."""
        messages = [
            {"role": "tool", "tool_call_id": "1", "content": "x" * 10000,
             "_ts": "2026-02-09T10:00:00"},
            {"role": "tool", "tool_call_id": "2", "content": "y" * 10000,
             "_ts": "2026-02-09T12:00:00"},
        ]
        count = CacheManager._flush_tool_results(messages, "soft")
        assert count == 2

    def test_before_ts_with_missing_ts_skips(self):
        """Messages without _ts are treated as new (current turn) — not trimmed."""
        messages = [
            {"role": "tool", "tool_call_id": "1", "content": "x" * 10000},
        ]
        count = CacheManager._flush_tool_results(
            messages, "soft", before_ts="2026-02-09T11:00:00"
        )
        assert count == 0
        assert messages[0]["content"] == "x" * 10000


class TestFlushMessages:
    def test_soft_flush_under_40_percent(self):
        cm = CacheManager(max_context_tokens=200_000)
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "tool", "tool_call_id": "1", "content": "x" * 10000},
        ]
        session = FakeSession(
            metadata={"cache": {"created_at": datetime.now().isoformat()}},
        )
        cm.flush_messages(messages, session, "anthropic/claude-opus-4-6")
        assert session.metadata["cache"]["last_flush_type"] == "soft"

    def test_hard_flush_over_40_percent(self):
        cm = CacheManager(max_context_tokens=10_000)
        messages = [
            {"role": "user", "content": "a" * 8000},
            {"role": "tool", "tool_call_id": "1", "content": "b" * 20000},
        ]
        session = FakeSession(
            metadata={"cache": {"created_at": datetime.now().isoformat()}},
        )
        cm.flush_messages(messages, session, "anthropic/claude-opus-4-6")
        assert session.metadata["cache"]["last_flush_type"] == "hard"

    def test_flush_updates_metadata(self):
        cm = CacheManager()
        messages = [
            {"role": "user", "content": "test"},
            {"role": "tool", "tool_call_id": "1", "content": "x" * 6000},
        ]
        created_at = datetime.now().isoformat()
        session = FakeSession(
            metadata={"cache": {"created_at": created_at}},
        )
        cm.flush_messages(messages, session, "anthropic/claude-opus-4-6")
        cache = session.metadata["cache"]
        assert cache["created_at"] == created_at  # Preserved
        assert "last_flush_at" in cache
        assert "last_flush_type" in cache

    def test_session_messages_untouched(self):
        """Flushing LLM messages must NOT modify session history."""
        cm = CacheManager()
        original_content = "x" * 10000
        session = FakeSession(
            messages=[
                {"role": "tool", "tool_call_id": "1", "content": original_content},
            ],
            metadata={"cache": {"created_at": datetime.now().isoformat()}},
        )
        # LLM messages are a separate list
        llm_messages = [
            {"role": "tool", "tool_call_id": "1", "content": original_content},
        ]
        cm.flush_messages(llm_messages, session, "anthropic/claude-opus-4-6")
        # LLM messages trimmed
        assert "[... trimmed to save tokens ...]" in llm_messages[0]["content"]
        # Session messages untouched
        assert session.messages[0]["content"] == original_content


class TestEstimateContextTokens:
    def test_without_session_counts_raw(self):
        cm = CacheManager()
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ]
        result = cm.estimate_context_tokens(messages, "anthropic/claude-opus-4-6")
        assert result > 0

    def test_without_session_includes_tools(self):
        cm = CacheManager()
        messages = [{"role": "user", "content": "Hi"}]
        tools = [{"type": "function", "function": {"name": "test", "parameters": {}}}]
        without_tools = cm.estimate_context_tokens(messages, "anthropic/claude-opus-4-6")
        with_tools = cm.estimate_context_tokens(
            messages, "anthropic/claude-opus-4-6", tools=tools
        )
        assert with_tools > without_tools

    def test_with_session_no_prior_flush(self):
        """Session without last_flush_type returns raw count."""
        cm = CacheManager()
        messages = [
            {"role": "user", "content": "Hi"},
            {"role": "tool", "tool_call_id": "1", "content": "x" * 10000},
        ]
        session = FakeSession(metadata={
            "cache": {"created_at": datetime.now().isoformat()}
        })
        result = cm.estimate_context_tokens(
            messages, "anthropic/claude-opus-4-6", session=session
        )
        raw = cm.estimate_context_tokens(messages, "anthropic/claude-opus-4-6")
        assert result == raw

    def test_with_prior_flush_simulates_effective_size(self):
        """With last_flush_type and last_flush_at, returns effective count."""
        cm = CacheManager()
        flush_at = "2026-02-09T10:00:00"
        messages = [
            {"role": "user", "content": "Hi"},
            {"role": "tool", "tool_call_id": "1", "content": "x" * 10000,
             "_ts": "2026-02-09T09:00:00"},
        ]
        session = FakeSession(metadata={
            "cache": {
                "created_at": datetime.now().isoformat(),
                "last_flush_type": "soft",
                "last_flush_at": flush_at,
            }
        })
        effective = cm.estimate_context_tokens(
            messages, "anthropic/claude-opus-4-6", session=session
        )
        raw = cm.estimate_context_tokens(messages, "anthropic/claude-opus-4-6")
        assert effective < raw

    def test_new_messages_after_flush_counted_raw(self):
        """Tool results added after last flush should be counted at full size."""
        cm = CacheManager()
        flush_at = "2026-02-09T10:00:00"
        messages = [
            {"role": "user", "content": "Hi"},
            {"role": "tool", "tool_call_id": "1", "content": "x" * 10000,
             "_ts": "2026-02-09T09:00:00"},  # Before flush — will be trimmed
            {"role": "tool", "tool_call_id": "2", "content": "y" * 10000,
             "_ts": "2026-02-09T11:00:00"},  # After flush — raw
        ]
        session = FakeSession(metadata={
            "cache": {
                "created_at": datetime.now().isoformat(),
                "last_flush_type": "soft",
                "last_flush_at": flush_at,
            }
        })
        effective = cm.estimate_context_tokens(
            messages, "anthropic/claude-opus-4-6", session=session
        )
        # Should be less than raw (first tool result trimmed) but more than
        # fully-flushed (second tool result NOT trimmed)
        raw = cm.estimate_context_tokens(messages, "anthropic/claude-opus-4-6")
        assert effective < raw
        # Now test with no timestamp awareness (backward compat)
        session_no_ts = FakeSession(metadata={
            "cache": {
                "created_at": datetime.now().isoformat(),
                "last_flush_type": "soft",
            }
        })
        effective_no_ts = cm.estimate_context_tokens(
            messages, "anthropic/claude-opus-4-6", session=session_no_ts
        )
        # Without timestamp, both are trimmed — should be less than ts-aware
        assert effective_no_ts < effective

    def test_effective_count_prevents_overcounting(self):
        """Flush type should be based on effective size, not raw.

        Scenario: first flush brought context to 30%. New messages add 8%.
        Raw would be 53% (hard), but effective is 38% (soft).
        """
        cm = CacheManager(max_context_tokens=10_000)
        flush_at = "2026-02-09T10:00:00"
        # Old tool result: raw ~2500 tokens, after soft flush ~1275 tokens
        # New user message: ~500 tokens
        messages = [
            {"role": "tool", "tool_call_id": "1", "content": "a" * 10000,
             "_ts": "2026-02-09T09:00:00"},
            {"role": "user", "content": "b" * 2000,
             "_ts": "2026-02-09T11:00:00"},
        ]
        session = FakeSession(metadata={
            "cache": {
                "created_at": (datetime.now() - timedelta(seconds=600)).isoformat(),
                "last_flush_type": "soft",
                "last_flush_at": flush_at,
            }
        })
        # Flush with effective counting — should see ~38% → soft
        cm.flush_messages(messages, session, "anthropic/claude-opus-4-6")
        assert session.metadata["cache"]["last_flush_type"] == "soft"

    def test_simulation_does_not_modify_original(self):
        """Flush simulation must not touch the original messages."""
        cm = CacheManager()
        original_content = "y" * 10000
        messages = [
            {"role": "tool", "tool_call_id": "1", "content": original_content},
        ]
        session = FakeSession(metadata={
            "cache": {
                "created_at": datetime.now().isoformat(),
                "last_flush_type": "soft",
            }
        })
        cm.estimate_context_tokens(
            messages, "anthropic/claude-opus-4-6", session=session
        )
        assert messages[0]["content"] == original_content


class TestApplyPreviousFlush:
    def test_trims_pre_flush_messages(self):
        """Re-applies previous flush to history messages."""
        cm = CacheManager()
        messages = [
            {"role": "tool", "tool_call_id": "1", "content": "x" * 10000,
             "_ts": "2026-02-09T09:00:00"},
            {"role": "tool", "tool_call_id": "2", "content": "y" * 10000,
             "_ts": "2026-02-09T11:00:00"},
        ]
        session = FakeSession(metadata={
            "cache": {
                "last_flush_type": "soft",
                "last_flush_at": "2026-02-09T10:00:00",
            }
        })
        count = cm.apply_previous_flush(messages, session)
        assert count == 1
        assert "[... trimmed to save tokens ...]" in messages[0]["content"]
        assert messages[1]["content"] == "y" * 10000  # After flush — untouched

    def test_skips_current_turn_messages(self):
        """Messages without _ts (from current turn) are not trimmed."""
        cm = CacheManager()
        messages = [
            {"role": "tool", "tool_call_id": "1", "content": "x" * 10000,
             "_ts": "2026-02-09T09:00:00"},
            {"role": "tool", "tool_call_id": "2", "content": "z" * 10000},
        ]
        session = FakeSession(metadata={
            "cache": {
                "last_flush_type": "soft",
                "last_flush_at": "2026-02-09T10:00:00",
            }
        })
        count = cm.apply_previous_flush(messages, session)
        assert count == 1
        assert messages[1]["content"] == "z" * 10000  # No _ts — untouched

    def test_no_flush_state_returns_zero(self):
        cm = CacheManager()
        messages = [
            {"role": "tool", "tool_call_id": "1", "content": "x" * 10000},
        ]
        session = FakeSession(metadata={})
        count = cm.apply_previous_flush(messages, session)
        assert count == 0

    def test_backward_compat_no_timestamp(self):
        """Without last_flush_at, trims all eligible messages."""
        cm = CacheManager()
        messages = [
            {"role": "tool", "tool_call_id": "1", "content": "x" * 10000,
             "_ts": "2026-02-09T09:00:00"},
        ]
        session = FakeSession(metadata={
            "cache": {"last_flush_type": "soft"}
        })
        count = cm.apply_previous_flush(messages, session)
        assert count == 1


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


class TestOverlappingTrimEdgeCase:
    """Content between threshold and 2*keep should not grow after trimming."""

    def test_soft_flush_content_just_above_threshold(self):
        """Content of 5001 chars should not become larger after soft flush."""
        content = "x" * 5001
        messages = [{"role": "tool", "tool_call_id": "1", "content": content}]
        CacheManager._flush_tool_results(messages, "soft")
        assert len(messages[0]["content"]) <= len(content)

    def test_hard_flush_content_just_above_threshold(self):
        """Content of 2001 chars should not become larger after hard flush."""
        content = "y" * 2001
        messages = [{"role": "tool", "tool_call_id": "1", "content": content}]
        CacheManager._flush_tool_results(messages, "hard")
        assert len(messages[0]["content"]) <= len(content)

    def test_soft_flush_boundary_content_skipped(self):
        """Content at exactly 2*keep + trim_tag size is left untouched."""
        trim_tag = CacheManager.TRIM_TAG
        boundary = 2 * CacheManager.SOFT_KEEP + len(trim_tag) + 2
        content = "a" * boundary
        messages = [{"role": "tool", "tool_call_id": "1", "content": content}]
        CacheManager._flush_tool_results(messages, "soft")
        # Should be left untouched since trimming wouldn't reduce size
        assert messages[0]["content"] == content

    def test_hard_flush_boundary_content_skipped(self):
        """Content at exactly 2*keep + trim_tag size is left untouched."""
        trim_tag = CacheManager.TRIM_TAG
        boundary = 2 * CacheManager.HARD_KEEP + len(trim_tag) + 2
        content = "b" * boundary
        messages = [{"role": "tool", "tool_call_id": "1", "content": content}]
        CacheManager._flush_tool_results(messages, "hard")
        assert messages[0]["content"] == content

    def test_content_well_above_boundary_is_trimmed(self):
        """Content well above the boundary still gets trimmed normally."""
        content = "c" * 10000
        messages = [{"role": "tool", "tool_call_id": "1", "content": content}]
        CacheManager._flush_tool_results(messages, "soft")
        assert len(messages[0]["content"]) < len(content)
        assert CacheManager.TRIM_TAG in messages[0]["content"]


class TestProviderDetection:
    """Provider detection should use prefix matching, not substring."""

    def test_claude_prefix_match(self):
        assert CacheManager.get_provider_from_model("claude-3-opus") == "anthropic"

    def test_claude_substring_not_detected_as_prefix(self):
        """Model with 'claude' in middle should fall to default, not match claude prefix."""
        # "my-claude-killer" doesn't startswith("claude"), so it falls to default.
        # Before the fix, `"claude" in lower` would have matched it as anthropic
        # regardless of the default — the distinction matters for non-anthropic defaults.
        result = CacheManager.get_provider_from_model("my-claude-killer")
        # Falls through to default (anthropic), NOT matched by the claude rule
        assert result == "anthropic"

    def test_gemini_prefix_match(self):
        assert CacheManager.get_provider_from_model("gemini-2.0-flash") == "gemini"

    def test_gemini_substring_not_detected(self):
        """Model with 'gemini' in middle should not match gemini prefix."""
        # "my-gemini-clone" doesn't startswith("gemini"), falls to default
        result = CacheManager.get_provider_from_model("my-gemini-clone")
        assert result == "anthropic"  # default, not "gemini"


class TestMaxContextTokensValidation:
    def test_zero_raises(self):
        with pytest.raises(ValueError, match="positive"):
            CacheManager(max_context_tokens=0)

    def test_negative_raises(self):
        with pytest.raises(ValueError, match="positive"):
            CacheManager(max_context_tokens=-1)

    def test_positive_works(self):
        cm = CacheManager(max_context_tokens=1)
        assert cm.max_context_tokens == 1


class TestInjectCacheControlLiteLLM:
    """Tests for LiteLLM provider cache breakpoint injection."""

    def test_system_prompt_gets_cache_control(self):
        from ragnarbot.providers.litellm_provider import LiteLLMProvider

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ]
        result = LiteLLMProvider._inject_cache_control(messages)
        # System message content should be converted to list with cache_control
        sys_msg = result[0]
        assert isinstance(sys_msg["content"], list)
        assert sys_msg["content"][0]["cache_control"] == {"type": "ephemeral"}

    def test_original_messages_not_modified(self):
        from ragnarbot.providers.litellm_provider import LiteLLMProvider

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ]
        original_content = messages[0]["content"]
        LiteLLMProvider._inject_cache_control(messages)
        # Original should be untouched
        assert messages[0]["content"] == original_content

    def test_last_tool_message_gets_cache_control(self):
        from ragnarbot.providers.litellm_provider import LiteLLMProvider

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Using tool"},
            {"role": "tool", "tool_call_id": "1", "content": "result 1"},
            {"role": "tool", "tool_call_id": "2", "content": "result 2"},
            {"role": "user", "content": "And then?"},
        ]
        result = LiteLLMProvider._inject_cache_control(messages)
        # Last tool message (index 4) should have cache_control
        assert result[4].get("cache_control") == {"type": "ephemeral"}
        # Earlier tool message should not
        assert "cache_control" not in result[3]

    def test_fallback_to_second_user_message(self):
        from ragnarbot.providers.litellm_provider import LiteLLMProvider

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Second question"},
        ]
        result = LiteLLMProvider._inject_cache_control(messages)
        # No tool messages → fallback: 2nd-to-last user (index 1) gets breakpoint
        assert isinstance(result[1]["content"], list)
        assert result[1]["content"][0].get("cache_control") == {"type": "ephemeral"}

    def test_single_user_message_no_breakpoint_2(self):
        from ragnarbot.providers.litellm_provider import LiteLLMProvider

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Only question"},
        ]
        result = LiteLLMProvider._inject_cache_control(messages)
        # System gets breakpoint, but user message should not (no 2nd user message)
        assert isinstance(result[0]["content"], list)  # system has it
        user_content = result[1]["content"]
        if isinstance(user_content, str):
            pass  # no cache_control — correct
        else:
            # If somehow converted, check no cache_control
            assert not any(
                b.get("cache_control") for b in user_content
                if isinstance(b, dict)
            )


class TestInjectCacheControlAnthropic:
    """Tests for Anthropic provider cache breakpoint injection."""

    def test_tool_result_user_message_gets_cache_control(self):
        from ragnarbot.providers.anthropic_provider import AnthropicProvider

        messages = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Let me check"},
                {"type": "tool_use", "id": "1", "name": "test", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "1", "content": "result"},
            ]},
            {"role": "assistant", "content": [{"type": "text", "text": "Done"}]},
            {"role": "user", "content": "Thanks"},
        ]
        AnthropicProvider._inject_history_cache_control(messages)
        # Last user message with tool_result is index 2
        last_block = messages[2]["content"][-1]
        assert last_block.get("cache_control") == {"type": "ephemeral"}

    def test_fallback_to_second_user_message(self):
        from ragnarbot.providers.anthropic_provider import AnthropicProvider

        messages = [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
            {"role": "user", "content": "Second question"},
        ]
        AnthropicProvider._inject_history_cache_control(messages)
        # No tool_result → fallback: 2nd-to-last user (index 0) gets breakpoint
        content = messages[0]["content"]
        assert isinstance(content, list)
        assert content[-1].get("cache_control") == {"type": "ephemeral"}

    def test_no_mutation_of_original_content_blocks(self):
        from ragnarbot.providers.anthropic_provider import AnthropicProvider

        original_block = {"type": "tool_result", "tool_use_id": "1", "content": "result"}
        messages = [
            {"role": "user", "content": [original_block]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            {"role": "user", "content": "Next"},
        ]
        AnthropicProvider._inject_history_cache_control(messages)
        # The injection uses {**block, "cache_control": ...} — original should be untouched
        assert "cache_control" not in original_block
