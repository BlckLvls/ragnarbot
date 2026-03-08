"""Tests for background memory flushing."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from ragnarbot.agent.memory_flush import MemoryFlushManager, MemorySegment
from ragnarbot.bus.queue import MessageBus
from ragnarbot.providers.base import LLMResponse
from ragnarbot.session.manager import Session


class FakeSessions:
    """Minimal session manager surface for memory flush tests."""

    def __init__(self, sessions):
        self._sessions = {session.key: session for session in sessions}

    def get_by_id(self, session_id):
        return self._sessions.get(session_id)

    def list_sessions(self):
        return [{"session_id": session_id} for session_id in self._sessions]


def _message(role: str, content: str, ts: str, **kwargs):
    return {
        "role": role,
        "content": content,
        "metadata": {"timestamp": ts},
        **kwargs,
    }


class TestMemoryFlushManager:
    def _make_manager(self, tmp_path, sessions, chat_fn):
        fake_sessions = FakeSessions(sessions)

        async def save_session(session):
            fake_sessions._sessions[session.key] = session

        return MemoryFlushManager(
            workspace=tmp_path / "workspace",
            sessions=fake_sessions,
            chat_fn=chat_fn,
            save_session_fn=save_session,
        )

    def test_enqueue_segment_splits_dates_and_dedupes(self, tmp_path):
        day1 = datetime(2026, 3, 7, 23, 55)
        day2 = day1 + timedelta(minutes=10)
        session = Session(
            key="test",
            user_key="telegram:1",
            messages=[
                _message("user", "late work", day1.isoformat()),
                _message("assistant", "done", day1.isoformat()),
                _message("user", "next day", day2.isoformat()),
                _message("assistant", "continue", day2.isoformat()),
            ],
            updated_at=day2,
        )

        async def fake_chat(*args, **kwargs):
            return LLMResponse(content="unused"), False, None

        manager = self._make_manager(tmp_path, [session], fake_chat)
        created = manager.enqueue_segment(
            session,
            MemorySegment(0, len(session.messages), "compaction", "extra_hard"),
        )

        assert len(created) == 3
        assert {job["scope"] for job in created} == {"daily", "long_term"}
        assert sorted(job["date"] for job in created if job["scope"] == "daily") == [
            "2026-03-07",
            "2026-03-08",
        ]

        duplicate = manager.enqueue_segment(
            session,
            MemorySegment(0, len(session.messages), "compaction", "extra_hard"),
        )
        assert duplicate == []

    @pytest.mark.asyncio
    async def test_jobs_write_daily_and_long_term_memory(self, tmp_path):
        ts = datetime(2026, 3, 8, 10, 0, 0).isoformat()
        session = Session(
            key="test",
            user_key="telegram:1",
            messages=[
                _message("user", "Investigated bug in /tmp/app.py", ts),
                _message("assistant", "Found root cause", ts),
            ],
            updated_at=datetime(2026, 3, 8, 10, 5, 0),
        )

        async def fake_chat(*args, **kwargs):
            messages = kwargs["messages"]
            user_content = messages[1]["content"]
            if "TARGET DATE:" in user_content:
                date_str = user_content.split("TARGET DATE: ", 1)[1].splitlines()[0]
                return LLMResponse(
                    content=f"# {date_str}\n\n## Progress\n- flushed daily memory"
                ), False, None
            return LLMResponse(
                content="# Long-term Memory\n\n- durable project fact"
            ), False, None

        manager = self._make_manager(tmp_path, [session], fake_chat)
        created = manager.enqueue_segment(
            session,
            MemorySegment(0, len(session.messages), "new_chat", "extra_hard"),
        )

        assert len(created) == 2

        await manager.start_session_jobs(session.key)
        await manager.wait_for_all()

        daily_file = tmp_path / "workspace" / "memory" / "2026-03-08.md"
        long_term_file = tmp_path / "workspace" / "memory" / "MEMORY.md"

        assert daily_file.read_text(encoding="utf-8") == (
            "# 2026-03-08\n\n## Progress\n- flushed daily memory"
        )
        assert long_term_file.read_text(encoding="utf-8") == (
            "# Long-term Memory\n\n- durable project fact"
        )

        jobs = session.metadata["memory_flush"]["jobs"]
        assert all(job["status"] == "done" for job in jobs)
        assert {job["result"] for job in jobs} == {"updated"}

    @pytest.mark.asyncio
    async def test_long_term_can_explicitly_noop(self, tmp_path):
        ts = datetime(2026, 3, 8, 11, 0, 0).isoformat()
        session = Session(
            key="test",
            user_key="telegram:1",
            messages=[
                _message("user", "Did a small one-off task", ts),
                _message("assistant", "Handled it", ts),
            ],
            updated_at=datetime(2026, 3, 8, 11, 5, 0),
        )

        async def fake_chat(*args, **kwargs):
            messages = kwargs["messages"]
            user_content = messages[1]["content"]
            if "TARGET DATE:" in user_content:
                date_str = user_content.split("TARGET DATE: ", 1)[1].splitlines()[0]
                return LLMResponse(
                    content=f"# {date_str}\n\n## Progress\n- one-off work was done"
                ), False, None
            return LLMResponse(
                content=MemoryFlushManager.LONG_TERM_NOOP_SENTINEL
            ), False, None

        manager = self._make_manager(tmp_path, [session], fake_chat)
        manager.memory.write_long_term("# Long-term Memory\n\n- existing durable fact")
        manager.enqueue_segment(
            session,
            MemorySegment(0, len(session.messages), "new_chat", "extra_hard"),
        )

        await manager.start_session_jobs(session.key)
        await manager.wait_for_all()

        long_term_file = tmp_path / "workspace" / "memory" / "MEMORY.md"
        daily_file = tmp_path / "workspace" / "memory" / "2026-03-08.md"

        assert long_term_file.read_text(encoding="utf-8") == (
            "# Long-term Memory\n\n- existing durable fact"
        )
        assert daily_file.read_text(encoding="utf-8") == (
            "# 2026-03-08\n\n## Progress\n- one-off work was done"
        )

        results = {
            job["scope"]: job["result"]
            for job in session.metadata["memory_flush"]["jobs"]
        }
        assert results["daily"] == "updated"
        assert results["long_term"] == "noop"

    def test_new_chat_segment_uses_tail_after_last_compaction(self, tmp_path):
        from ragnarbot.agent.loop import AgentLoop

        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"

        with patch("ragnarbot.agent.loop.SessionManager"), \
             patch("ragnarbot.agent.loop.SubagentManager"):
            loop = AgentLoop(
                bus=MessageBus(),
                provider=provider,
                workspace=tmp_path / "workspace",
            )

        session = Session(
            key="test",
            user_key="telegram:1",
            messages=[
                _message("user", "old", "2026-03-08T09:00:00"),
                _message(
                    "user",
                    "[Conversation Summary]\nsummary",
                    "2026-03-08T09:30:00",
                    metadata={"type": "compaction", "timestamp": "2026-03-08T09:30:00"},
                ),
                _message("user", "tail-1", "2026-03-08T10:00:00"),
                _message("assistant", "tail-2", "2026-03-08T10:01:00"),
            ],
        )

        segment = loop._build_new_chat_memory_segment(session)

        assert segment is not None
        assert segment.start_idx == 2
        assert segment.end_idx == 4
        assert segment.flush_type == "extra_hard"
