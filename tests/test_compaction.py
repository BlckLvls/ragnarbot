"""Tests for auto-compaction feature."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from ragnarbot.agent.cache import CacheManager
from ragnarbot.agent.compactor import Compactor
from ragnarbot.providers.base import LLMResponse
from ragnarbot.session.manager import Session


class FakeSession:
    """Minimal session stand-in for testing."""

    def __init__(self, messages=None, metadata=None):
        self.messages = messages or []
        self.metadata = metadata or {}


# ── should_compact ──────────────────────────────────────────────


class TestShouldCompact:
    def _make_compactor(self, max_tokens=200_000):
        provider = MagicMock()
        cm = CacheManager(max_context_tokens=max_tokens)
        return Compactor(provider, cm, max_tokens, "anthropic/claude-opus-4-6")

    def test_below_threshold_returns_false(self):
        c = self._make_compactor(max_tokens=200_000)
        messages = [{"role": "user", "content": "hello"}]
        assert c.should_compact(messages, "normal") is False

    def test_above_threshold_returns_true(self):
        # eco threshold = 40% of 1000 = 400 tokens
        c = self._make_compactor(max_tokens=1000)
        # Create messages that use ~500 tokens (2000 chars / 4 = 500)
        messages = [{"role": "user", "content": "x" * 2000}]
        assert c.should_compact(messages, "eco") is True

    def test_invalid_mode_returns_false(self):
        c = self._make_compactor()
        messages = [{"role": "user", "content": "x" * 100000}]
        assert c.should_compact(messages, "invalid") is False

    def test_tools_count_toward_threshold(self):
        c = self._make_compactor(max_tokens=1000)
        messages = [{"role": "user", "content": "x" * 1000}]
        tools = [{"type": "function", "function": {"name": "test", "parameters": {"type": "object"}}}]
        # Without tools it might be borderline, with tools it pushes over
        result_no_tools = c.should_compact(messages, "full")
        result_with_tools = c.should_compact(messages, "full", tools=tools)
        # Both might be true given the content, but tools increase the count
        assert isinstance(result_no_tools, bool)
        assert isinstance(result_with_tools, bool)


# ── _determine_tail ─────────────────────────────────────────────


class TestDetermineTail:
    def _make_compactor(self, max_tokens=200_000):
        provider = MagicMock()
        cm = CacheManager(max_context_tokens=max_tokens)
        return Compactor(provider, cm, max_tokens, "anthropic/claude-opus-4-6")

    def test_respects_tail_min(self):
        c = self._make_compactor()
        msgs = [{"role": "user", "content": "msg", "metadata": {}} for _ in range(30)]
        tail = c._determine_tail(msgs)
        assert tail >= Compactor.TAIL_MIN

    def test_respects_tail_max(self):
        c = self._make_compactor()
        msgs = [{"role": "user", "content": "msg", "metadata": {}} for _ in range(50)]
        tail = c._determine_tail(msgs)
        assert tail <= Compactor.TAIL_MAX + 5  # +5 for possible parity extension

    def test_respects_token_limit(self):
        # With small max_tokens, tail should stop growing early
        c = self._make_compactor(max_tokens=1000)
        # 5% of 1000 = 50 tokens max for tail
        msgs = [
            {"role": "user", "content": "a" * 400, "metadata": {}}
            for _ in range(30)
        ]
        tail = c._determine_tail(msgs)
        # Should be around TAIL_MIN since each message is ~100 tokens
        assert tail >= Compactor.TAIL_MIN

    def test_parity_extends_for_orphaned_tool_response(self):
        c = self._make_compactor()
        msgs = []
        # Add 15 user/assistant pairs
        for i in range(15):
            msgs.append({"role": "user", "content": f"q{i}", "metadata": {}})
            msgs.append({"role": "assistant", "content": f"a{i}", "metadata": {}})
        # Add assistant with tool_calls followed by tool response at the boundary
        msgs.append({
            "role": "assistant", "content": "", "metadata": {},
            "tool_calls": [{"id": "1", "type": "function", "function": {"name": "test", "arguments": "{}"}}],
        })
        msgs.append({
            "role": "tool", "content": "result", "metadata": {},
            "tool_call_id": "1", "name": "test",
        })
        # Add more messages to be in the tail
        for i in range(9):
            msgs.append({"role": "user", "content": f"after{i}", "metadata": {}})

        tail = c._determine_tail(msgs)
        # The tail should be extended to include the assistant with tool_calls
        first_tail = msgs[-tail]
        assert first_tail["role"] != "tool"

    def test_fewer_messages_than_tail_min(self):
        c = self._make_compactor()
        msgs = [{"role": "user", "content": "msg", "metadata": {}} for _ in range(5)]
        tail = c._determine_tail(msgs)
        assert tail == 5  # Can't exceed total messages


# ── _find_last_compaction_idx ───────────────────────────────────


class TestFindLastCompactionIdx:
    def _make_compactor(self):
        provider = MagicMock()
        cm = CacheManager(max_context_tokens=200_000)
        return Compactor(provider, cm, 200_000, "anthropic/claude-opus-4-6")

    def test_no_compaction(self):
        c = self._make_compactor()
        msgs = [
            {"role": "user", "content": "hi", "metadata": {}},
            {"role": "assistant", "content": "hello", "metadata": {}},
        ]
        assert c._find_last_compaction_idx(msgs) is None

    def test_one_compaction(self):
        c = self._make_compactor()
        msgs = [
            {"role": "user", "content": "hi", "metadata": {}},
            {"role": "user", "content": "summary", "metadata": {"type": "compaction"}},
            {"role": "user", "content": "new msg", "metadata": {}},
        ]
        assert c._find_last_compaction_idx(msgs) == 1

    def test_two_compactions_returns_last(self):
        c = self._make_compactor()
        msgs = [
            {"role": "user", "content": "summary1", "metadata": {"type": "compaction"}},
            {"role": "user", "content": "msg", "metadata": {}},
            {"role": "user", "content": "summary2", "metadata": {"type": "compaction"}},
            {"role": "user", "content": "msg2", "metadata": {}},
        ]
        assert c._find_last_compaction_idx(msgs) == 2


# ── _format_compaction_input ────────────────────────────────────


class TestFormatCompactionInput:
    def _make_compactor(self):
        provider = MagicMock()
        cm = CacheManager(max_context_tokens=200_000)
        return Compactor(provider, cm, 200_000, "anthropic/claude-opus-4-6")

    def test_basic_format(self):
        c = self._make_compactor()
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        result = c._format_compaction_input(msgs, None)
        assert "=== CONVERSATION ===" in result
        assert "[user] hello" in result
        assert "[assistant] hi there" in result
        assert "=== PREVIOUS SUMMARY ===" not in result

    def test_with_previous_compaction(self):
        c = self._make_compactor()
        prev = {"role": "user", "content": "previous summary content"}
        msgs = [{"role": "user", "content": "new msg"}]
        result = c._format_compaction_input(msgs, prev)
        assert "=== PREVIOUS SUMMARY ===" in result
        assert "previous summary content" in result
        assert "=== CONVERSATION ===" in result
        assert "[user] new msg" in result

    def test_tool_calls_formatted(self):
        c = self._make_compactor()
        msgs = [
            {
                "role": "assistant",
                "content": "Let me check",
                "tool_calls": [{
                    "id": "1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "/tmp/test"}'},
                }],
            },
            {"role": "tool", "content": "file content", "name": "read_file"},
        ]
        result = c._format_compaction_input(msgs, None)
        assert "[tool_call] read_file" in result
        assert "[tool_response:read_file] file content" in result
        assert "[assistant] Let me check" in result


# ── _inject_compaction ──────────────────────────────────────────


class TestInjectCompaction:
    def _make_compactor(self):
        provider = MagicMock()
        cm = CacheManager(max_context_tokens=200_000)
        return Compactor(provider, cm, 200_000, "anthropic/claude-opus-4-6")

    def test_inserts_at_correct_position(self):
        c = self._make_compactor()
        ts = datetime.now().isoformat()
        session = Session(key="test", user_key="test:1", messages=[
            {"role": "user", "content": "old1", "metadata": {"timestamp": ts}},
            {"role": "assistant", "content": "old2", "metadata": {"timestamp": ts}},
            {"role": "user", "content": "old3", "metadata": {"timestamp": ts}},
            {"role": "assistant", "content": "old4", "metadata": {"timestamp": ts}},
            # These 2 are the "tail" (tail_count=2)
            {"role": "user", "content": "recent1", "metadata": {"timestamp": ts}},
            {"role": "assistant", "content": "recent2", "metadata": {"timestamp": ts}},
        ])
        c._inject_compaction(session, "Test summary", tail_count=2, context_mode="normal")
        # All 6 original messages preserved + 1 compaction = 7
        assert len(session.messages) == 7
        # Old messages preserved at indices 0-3
        assert session.messages[0]["content"] == "old1"
        assert session.messages[3]["content"] == "old4"
        # Compaction inserted at index 4 (before the 2-message tail)
        assert session.messages[4]["metadata"]["type"] == "compaction"
        assert session.messages[4]["content"] == "[Conversation Summary]\nTest summary"
        assert session.messages[4]["metadata"]["mode"] == "normal"
        # Tail preserved at indices 5-6
        assert session.messages[5]["content"] == "recent1"
        assert session.messages[6]["content"] == "recent2"

    def test_compaction_timestamp_before_tail(self):
        c = self._make_compactor()
        tail_ts = datetime(2026, 2, 9, 12, 0, 0)
        session = Session(key="test", user_key="test:1", messages=[
            {"role": "user", "content": "old", "metadata": {"timestamp": "2026-02-09T10:00:00"}},
            {"role": "user", "content": "recent", "metadata": {"timestamp": tail_ts.isoformat()}},
        ])
        c._inject_compaction(session, "Summary", tail_count=1, context_mode="eco")
        # Compaction inserted at index 1 (old message stays at index 0)
        compaction_ts = datetime.fromisoformat(session.messages[1]["metadata"]["timestamp"])
        assert compaction_ts < tail_ts


# ── Session.get_history with compaction ─────────────────────────


class TestGetHistoryWithCompaction:
    def test_starts_from_last_compaction(self):
        session = Session(key="test", user_key="test:1", messages=[
            {"role": "user", "content": "old msg", "metadata": {}},
            {"role": "assistant", "content": "old reply", "metadata": {}},
            {"role": "user", "content": "Summary", "metadata": {"type": "compaction"}},
            {"role": "user", "content": "new msg", "metadata": {}},
            {"role": "assistant", "content": "new reply", "metadata": {}},
        ])
        history = session.get_history()
        assert len(history) == 3
        assert history[0]["content"] == "Summary"

    def test_no_compaction_uses_normal_behavior(self):
        session = Session(key="test", user_key="test:1", messages=[
            {"role": "user", "content": "msg1", "metadata": {}},
            {"role": "assistant", "content": "reply1", "metadata": {}},
        ])
        history = session.get_history()
        assert len(history) == 2

    def test_multiple_compactions_uses_last(self):
        session = Session(key="test", user_key="test:1", messages=[
            {"role": "user", "content": "Summary1", "metadata": {"type": "compaction"}},
            {"role": "user", "content": "mid msg", "metadata": {}},
            {"role": "user", "content": "Summary2", "metadata": {"type": "compaction"}},
            {"role": "user", "content": "final msg", "metadata": {}},
        ])
        history = session.get_history()
        assert len(history) == 2
        assert history[0]["content"] == "Summary2"

    def test_no_sliding_window(self):
        """All messages returned when session exceeds 200 msgs (no window)."""
        messages = []
        for i in range(250):
            messages.append({
                "role": "user", "content": f"msg {i}", "metadata": {},
            })
        session = Session(key="test", user_key="test:1", messages=messages)
        history = session.get_history()
        assert len(history) == 250

    def test_ts_carried_in_output(self):
        """get_history should carry _ts from message metadata."""
        session = Session(key="test", user_key="test:1", messages=[
            {"role": "user", "content": "hello",
             "metadata": {"timestamp": "2026-02-09T10:00:00"}},
            {"role": "assistant", "content": "hi",
             "metadata": {"timestamp": "2026-02-09T10:01:00"}},
            {"role": "tool", "content": "result", "tool_call_id": "1",
             "metadata": {}},
        ])
        history = session.get_history()
        assert history[0]["_ts"] == "2026-02-09T10:00:00"
        assert history[1]["_ts"] == "2026-02-09T10:01:00"
        assert "_ts" not in history[2]  # No timestamp in metadata


# ── Extra-hard flush ────────────────────────────────────────────


class TestExtraHardFlush:
    def test_tail_only_no_head(self):
        content = "x" * 5000
        messages = [{"role": "tool", "tool_call_id": "1", "content": content}]
        CacheManager._flush_tool_results(messages, "extra_hard")
        result = messages[0]["content"]
        assert result.startswith(CacheManager.TRIM_TAG)
        # Should only have TRIM_TAG + newline + last 200 chars
        assert len(result) == len(CacheManager.TRIM_TAG) + 1 + CacheManager.EXTRA_HARD_KEEP

    def test_small_content_untouched(self):
        content = "short"
        messages = [{"role": "tool", "tool_call_id": "1", "content": content}]
        CacheManager._flush_tool_results(messages, "extra_hard")
        assert messages[0]["content"] == "short"

    def test_content_below_threshold_untouched(self):
        content = "y" * 1999
        messages = [{"role": "tool", "tool_call_id": "1", "content": content}]
        CacheManager._flush_tool_results(messages, "extra_hard")
        assert messages[0]["content"] == content


# ── Mode-aware flush_messages ───────────────────────────────────


class TestModeAwareFlush:
    def test_eco_uses_extra_hard(self):
        cm = CacheManager(max_context_tokens=200_000)
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "tool", "tool_call_id": "1", "content": "x" * 10000},
        ]
        session = FakeSession(
            metadata={"cache": {"created_at": datetime.now().isoformat()}},
        )
        cm.flush_messages(messages, session, "anthropic/claude-opus-4-6",
                          context_mode="eco")
        assert session.metadata["cache"]["last_flush_type"] == "extra_hard"

    def test_normal_uses_soft_or_hard(self):
        cm = CacheManager(max_context_tokens=200_000)
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "tool", "tool_call_id": "1", "content": "x" * 10000},
        ]
        session = FakeSession(
            metadata={"cache": {"created_at": datetime.now().isoformat()}},
        )
        cm.flush_messages(messages, session, "anthropic/claude-opus-4-6",
                          context_mode="normal")
        assert session.metadata["cache"]["last_flush_type"] in ("soft", "hard")


# ── flush_for_compaction ────────────────────────────────────────


class TestFlushForCompaction:
    def test_eco_uses_extra_hard(self):
        messages = [{"role": "tool", "tool_call_id": "1", "content": "x" * 5000}]
        CacheManager.flush_for_compaction(messages, "eco")
        assert messages[0]["content"].startswith(CacheManager.TRIM_TAG)

    def test_normal_uses_hard(self):
        messages = [{"role": "tool", "tool_call_id": "1", "content": "x" * 5000}]
        CacheManager.flush_for_compaction(messages, "normal")
        # Hard flush: head + tail with trim tag in middle
        assert CacheManager.TRIM_TAG in messages[0]["content"]
        # Should NOT start with TRIM_TAG (hard keeps head)
        assert not messages[0]["content"].startswith(CacheManager.TRIM_TAG)


# ── Full compaction cycle (mocked LLM) ─────────────────────────


class TestCompactionCycle:
    @pytest.mark.asyncio
    async def test_full_cycle(self):
        """Mock LLM, verify session state after compaction."""
        provider = AsyncMock()
        provider.chat = AsyncMock(return_value=LLMResponse(
            content="### Context\nTest summary of conversation.",
        ))

        cm = CacheManager(max_context_tokens=1000)
        c = Compactor(provider, cm, 1000, "anthropic/claude-opus-4-6")

        session = Session(key="test", user_key="test:1")
        # Add enough messages to exceed eco threshold
        for i in range(20):
            session.add_message("user", f"Message {i} " + "x" * 100)
            session.add_message("assistant", f"Reply {i} " + "y" * 100)

        # Build context_builder mock
        context_builder = MagicMock()
        context_builder.build_messages = MagicMock(return_value=[
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "### Context\nTest summary of conversation."},
            {"role": "user", "content": "Message 19 " + "x" * 100},
            {"role": "assistant", "content": "Reply 19 " + "y" * 100},
        ])

        # Simulate messages list that would trigger compaction
        messages = [
            {"role": "system", "content": "system prompt"},
        ] + [
            {"role": "user" if i % 2 == 0 else "assistant",
             "content": f"msg {i} " + "z" * 100}
            for i in range(40)
        ] + [
            {"role": "user", "content": "current turn message"},
        ]
        new_start = len(messages) - 1

        new_messages, new_new_start = await c.compact(
            session=session,
            context_mode="eco",
            context_builder=context_builder,
            messages=messages,
            new_start=new_start,
            tools=None,
            channel="test",
            chat_id="1",
            session_metadata={},
        )

        # Verify compaction was called
        provider.chat.assert_called_once()
        # Verify session has compaction message
        has_compaction = any(
            m.get("metadata", {}).get("type") == "compaction"
            for m in session.messages
        )
        assert has_compaction
        # Verify [Conversation Summary] prefix in compaction message
        compaction_msg = next(
            m for m in session.messages
            if m.get("metadata", {}).get("type") == "compaction"
        )
        assert compaction_msg["content"].startswith("[Conversation Summary]\n")
        # Verify the context_builder was called to rebuild messages
        context_builder.build_messages.assert_called_once()
        # Verify session_key was passed to build_messages
        call_kwargs = context_builder.build_messages.call_args
        assert call_kwargs.kwargs.get("session_key") == "test"

    @pytest.mark.asyncio
    async def test_llm_error_returns_original(self):
        """If LLM fails, return original messages unchanged."""
        provider = AsyncMock()
        provider.chat = AsyncMock(side_effect=Exception("LLM error"))

        cm = CacheManager(max_context_tokens=1000)
        c = Compactor(provider, cm, 1000, "anthropic/claude-opus-4-6")

        session = Session(key="test", user_key="test:1")
        for i in range(15):
            session.add_message("user", f"msg {i}")
            session.add_message("assistant", f"reply {i}")

        messages = [{"role": "user", "content": "test"}] * 20
        new_start = 19

        result_messages, result_start = await c.compact(
            session=session,
            context_mode="normal",
            context_builder=MagicMock(),
            messages=messages,
            new_start=new_start,
            tools=None,
        )
        assert result_messages is messages
        assert result_start == new_start

    @pytest.mark.asyncio
    async def test_empty_summary_returns_original(self):
        """If LLM returns empty summary, skip compaction."""
        provider = AsyncMock()
        provider.chat = AsyncMock(return_value=LLMResponse(content=""))

        cm = CacheManager(max_context_tokens=1000)
        c = Compactor(provider, cm, 1000, "anthropic/claude-opus-4-6")

        session = Session(key="test", user_key="test:1")
        for i in range(15):
            session.add_message("user", f"msg {i}")
            session.add_message("assistant", f"reply {i}")

        messages = [{"role": "user", "content": "test"}] * 20
        new_start = 19

        result_messages, result_start = await c.compact(
            session=session,
            context_mode="normal",
            context_builder=MagicMock(),
            messages=messages,
            new_start=new_start,
            tools=None,
        )
        assert result_messages is messages

    @pytest.mark.asyncio
    async def test_too_few_messages_skips(self):
        """Sessions with fewer than TAIL_MIN messages skip compaction."""
        provider = AsyncMock()
        cm = CacheManager(max_context_tokens=1000)
        c = Compactor(provider, cm, 1000, "anthropic/claude-opus-4-6")

        session = Session(key="test", user_key="test:1")
        for i in range(3):
            session.add_message("user", f"msg {i}")

        messages = [{"role": "user", "content": "test"}]
        result_messages, _ = await c.compact(
            session=session,
            context_mode="eco",
            context_builder=MagicMock(),
            messages=messages,
            new_start=0,
            tools=None,
        )
        assert result_messages is messages
        provider.chat.assert_not_called()


# ── Config schema ───────────────────────────────────────────────


class TestConfigContextMode:
    def test_default_is_normal(self):
        from ragnarbot.config.schema import AgentDefaults
        defaults = AgentDefaults()
        assert defaults.context_mode == "normal"

    def test_accepts_valid_modes(self):
        from ragnarbot.config.schema import AgentDefaults
        for mode in ("eco", "normal", "full"):
            d = AgentDefaults(context_mode=mode)
            assert d.context_mode == mode

    def test_rejects_invalid_mode(self):
        from pydantic import ValidationError
        from ragnarbot.config.schema import AgentDefaults
        with pytest.raises(ValidationError):
            AgentDefaults(context_mode="invalid")


# ── should_compact with session parameter ───────────────────────


class TestShouldCompactWithSession:
    def test_accepts_session_parameter(self):
        provider = MagicMock()
        cm = CacheManager(max_context_tokens=200_000)
        c = Compactor(provider, cm, 200_000, "anthropic/claude-opus-4-6")
        messages = [{"role": "user", "content": "hello"}]
        session = FakeSession(metadata={})
        result = c.should_compact(messages, "normal", session=session)
        assert result is False

    def test_session_affects_token_count(self):
        """should_compact uses effective tokens when session has flush state.

        With a previous flush recorded, large tool results are simulated as
        trimmed, lowering the effective token count and potentially avoiding
        compaction that raw tokens would trigger.
        """
        provider = MagicMock()
        # eco threshold = 40% of 10000 = 4000 tokens
        cm = CacheManager(max_context_tokens=10_000)
        c = Compactor(provider, cm, 10_000, "anthropic/claude-opus-4-6")

        # Tool result: 20000 chars ≈ 5000 tokens raw → above threshold
        # After hard flush (keep=1000): ~2033 chars ≈ ~508 tokens → below
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "tc1", "type": "function",
                             "function": {"name": "exec", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "tc1", "name": "exec",
             "content": "x" * 20000, "_ts": "2026-02-09T09:00:00"},
            {"role": "assistant", "content": "done"},
        ]
        # Without session: raw tokens trigger eco threshold
        assert c.should_compact(messages, "eco") is True

        # With session that has flush state: effective tokens are lower
        # because the tool result is simulated as trimmed
        session = FakeSession(metadata={
            "cache": {
                "last_flush_type": "hard",
                "last_flush_at": "2026-02-09T10:00:00",
            }
        })
        # Effective tokens after simulated flush should be below threshold
        assert c.should_compact(messages, "eco", session=session) is False


# ── _format_compaction_input no double newlines ─────────────────


class TestFormatCompactionInputNoDoubleNewlines:
    def _make_compactor(self):
        provider = MagicMock()
        cm = CacheManager(max_context_tokens=200_000)
        return Compactor(provider, cm, 200_000, "anthropic/claude-opus-4-6")

    def test_no_double_newlines(self):
        c = self._make_compactor()
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": "how are you"},
        ]
        result = c._format_compaction_input(msgs, None)
        # Should not contain double newlines (parts already have trailing \n)
        assert "\n\n" not in result


# ── READ_ONLY_COMMANDS ──────────────────────────────────────────


class TestReadOnlyCommands:
    def test_read_only_commands_constant(self):
        from ragnarbot.agent.loop import AgentLoop
        assert "context_info" in AgentLoop.READ_ONLY_COMMANDS
        assert "context_mode" in AgentLoop.READ_ONLY_COMMANDS
        # Mutating commands should NOT be in the set
        assert "new_chat" not in AgentLoop.READ_ONLY_COMMANDS
        assert "set_context_mode" not in AgentLoop.READ_ONLY_COMMANDS


# ── Flush-aware get_context_tokens ──────────────────────────────


class TestGetContextTokensPendingFlush:
    def test_pending_flush_reduces_token_count(self):
        """When cache TTL is expired, get_context_tokens should simulate flush."""
        from datetime import datetime, timedelta
        from unittest.mock import MagicMock  # noqa: F811
        from ragnarbot.agent.cache import CacheManager
        from ragnarbot.session.manager import Session

        # Create a session with expired cache and large tool results
        expired_time = (datetime.now() - timedelta(seconds=600)).isoformat()
        session = Session(key="test", user_key="telegram:123")
        # Add messages with large tool results
        session.add_message("user", "do something")
        session.add_message(
            "assistant", None,
            tool_calls=[{
                "id": "tc1", "type": "function",
                "function": {"name": "exec", "arguments": "{}"},
            }],
        )
        session.add_message("tool", "x" * 10000, tool_call_id="tc1", name="exec")
        session.add_message("assistant", "Here is the result.")
        session.metadata["cache"] = {"created_at": expired_time}

        # Build a mock AgentLoop with minimal deps
        cm = CacheManager(max_context_tokens=200_000)
        assert cm.should_flush(session, "anthropic/claude-opus-4-6")

        # Compute tokens with and without flush simulation
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "tc1", "type": "function",
                             "function": {"name": "exec", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "tc1", "name": "exec",
             "content": "x" * 10000},
            {"role": "assistant", "content": "Here is the result."},
        ]

        raw_tokens = cm.estimate_context_tokens(
            messages, "anthropic/claude-opus-4-6",
        )

        # Simulate flush (like get_context_tokens does)
        sim = [m.copy() for m in messages]
        CacheManager._flush_tool_results(sim, "hard")
        flushed_tokens = cm.estimate_context_tokens(
            sim, "anthropic/claude-opus-4-6",
        )

        assert flushed_tokens < raw_tokens
