"""Background memory flushing for session history."""

from __future__ import annotations

import asyncio
import json
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from ragnarbot.agent.cache import CacheManager
from ragnarbot.agent.memory import MemoryStore
from ragnarbot.prompts.memory_flush import (
    DAILY_MEMORY_SYSTEM_PROMPT,
    LONG_TERM_MEMORY_SYSTEM_PROMPT,
)
from ragnarbot.providers.base import LLMResponse
from ragnarbot.session.manager import Session, SessionManager

MemoryChatFn = Callable[..., Any]


@dataclass(slots=True, frozen=True)
class MemorySegment:
    """A compact description of a session segment to flush into memory."""

    start_idx: int
    end_idx: int
    trigger: str
    flush_type: str


class MemoryFlushManager:
    """Schedules and executes background writes to daily and long-term memory."""

    META_KEY = "memory_flush"
    JOBS_KEY = "jobs"
    LONG_TERM_NOOP_SENTINEL = "__NO_LONG_TERM_MEMORY_UPDATE__"
    STARTABLE_STATUSES = {"pending", "running"}
    RESUMABLE_STATUSES = {"pending", "error", "running"}
    MAX_RETRIES = 3
    RETRY_DELAY_SECONDS = 5

    def __init__(
        self,
        workspace: Path,
        sessions: SessionManager,
        chat_fn: MemoryChatFn,
        save_session_fn: Callable[[Session], Any],
    ):
        self.workspace = workspace
        self.sessions = sessions
        self.memory = MemoryStore(workspace)
        self._chat_fn = chat_fn
        self._save_session_fn = save_session_fn
        self._tasks: dict[str, asyncio.Task] = {}
        self._file_locks: dict[str, asyncio.Lock] = {}

    async def save_session(self, session: Session) -> None:
        """Persist a session using the shared session-save path."""
        await self._save_session_fn(session)

    def enqueue_segment(
        self,
        session: Session,
        segment: MemorySegment,
    ) -> list[dict[str, Any]]:
        """Create durable memory flush jobs for a session segment."""
        if segment.end_idx <= segment.start_idx:
            return []

        if not self._has_meaningful_messages(session, segment.start_idx, segment.end_idx):
            return []

        created: list[dict[str, Any]] = []
        state = session.metadata.setdefault(self.META_KEY, {})
        jobs = state.setdefault(self.JOBS_KEY, [])

        long_term_job = self._make_job(
            session=session,
            scope="long_term",
            trigger=segment.trigger,
            flush_type=segment.flush_type,
            start_idx=segment.start_idx,
            end_idx=segment.end_idx,
            date_str=None,
        )
        existing = self._find_job(jobs, long_term_job["fingerprint"])
        if existing is None:
            jobs.append(long_term_job)
            created.append(long_term_job)

        for date_str, start_idx, end_idx in self._split_segment_by_date(
            session, segment.start_idx, segment.end_idx,
        ):
            daily_job = self._make_job(
                session=session,
                scope="daily",
                trigger=segment.trigger,
                flush_type=segment.flush_type,
                start_idx=start_idx,
                end_idx=end_idx,
                date_str=date_str,
            )
            existing = self._find_job(jobs, daily_job["fingerprint"])
            if existing is None:
                jobs.append(daily_job)
                created.append(daily_job)

        if created:
            logger.info(
                f"Enqueued {len(created)} memory flush job(s) for session {session.key}"
            )
        return created

    async def start_session_jobs(
        self,
        session_id: str,
        *,
        include_errors: bool = False,
    ) -> None:
        """Start pending jobs for a session without blocking the caller."""
        session = self.sessions.get_by_id(session_id)
        if session is None:
            return

        jobs = session.metadata.get(self.META_KEY, {}).get(self.JOBS_KEY, [])
        allowed = self.RESUMABLE_STATUSES if include_errors else self.STARTABLE_STATUSES

        for job in jobs:
            job_id = job.get("id")
            if not job_id or job_id in self._tasks:
                continue
            if job.get("status") not in allowed:
                continue

            task = asyncio.create_task(self._run_job(session_id, job_id))
            self._tasks[job_id] = task
            task.add_done_callback(lambda _, jid=job_id: self._tasks.pop(jid, None))

    async def resume_pending_jobs(self) -> None:
        """Resume any persisted jobs after process startup."""
        for info in self.sessions.list_sessions():
            session_id = info.get("session_id")
            if session_id:
                await self.start_session_jobs(session_id, include_errors=True)

    async def wait_for_all(self) -> None:
        """Wait for all active background memory jobs."""
        active = list(self._tasks.values())
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    def _make_job(
        self,
        *,
        session: Session,
        scope: str,
        trigger: str,
        flush_type: str,
        start_idx: int,
        end_idx: int,
        date_str: str | None,
    ) -> dict[str, Any]:
        fingerprint = ":".join([
            scope,
            date_str or "-",
            str(start_idx),
            str(end_idx),
        ])
        now = datetime.now().isoformat()
        return {
            "id": uuid.uuid4().hex[:12],
            "session_id": session.key,
            "scope": scope,
            "date": date_str,
            "trigger": trigger,
            "flush_type": flush_type,
            "start_idx": start_idx,
            "end_idx": end_idx,
            "fingerprint": fingerprint,
            "status": "pending",
            "result": None,
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
            "last_error": None,
        }

    def _find_job(
        self,
        jobs: list[dict[str, Any]],
        fingerprint: str,
    ) -> dict[str, Any] | None:
        for job in jobs:
            if job.get("fingerprint") == fingerprint:
                return job
        return None

    def _split_segment_by_date(
        self,
        session: Session,
        start_idx: int,
        end_idx: int,
    ) -> list[tuple[str, int, int]]:
        """Split a session segment into contiguous daily buckets."""
        ranges: list[tuple[str, int, int]] = []
        current_date: str | None = None
        bucket_start: int | None = None
        fallback_date = session.updated_at.strftime("%Y-%m-%d")

        for idx in range(start_idx, end_idx):
            msg = session.messages[idx]
            if msg.get("metadata", {}).get("type") == "compaction":
                continue

            msg_date = self._message_date(msg) or fallback_date
            if current_date is None:
                current_date = msg_date
                bucket_start = idx
                continue

            if msg_date != current_date:
                if bucket_start is not None:
                    ranges.append((current_date, bucket_start, idx))
                current_date = msg_date
                bucket_start = idx

        if current_date is not None and bucket_start is not None:
            ranges.append((current_date, bucket_start, end_idx))

        return ranges

    def _message_date(self, message: dict[str, Any]) -> str | None:
        ts_raw = message.get("metadata", {}).get("timestamp")
        if not ts_raw:
            return None
        try:
            return datetime.fromisoformat(ts_raw).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            return None

    def _has_meaningful_messages(
        self,
        session: Session,
        start_idx: int,
        end_idx: int,
    ) -> bool:
        for idx in range(start_idx, end_idx):
            msg = session.messages[idx]
            if msg.get("metadata", {}).get("type") == "compaction":
                continue
            return True
        return False

    async def _run_job(self, session_id: str, job_id: str) -> None:
        target_path: Path | None = None
        try:
            session, job = await self._mark_job_running(session_id, job_id)
            if session is None or job is None:
                return

            target_path = self._target_path(job)
            lock = self._file_locks.setdefault(str(target_path), asyncio.Lock())
            async with lock:
                existing = self._read_existing(job)
                conversation = self._build_conversation_text(session, job)
                if not conversation.strip():
                    await self._mark_job_done(session_id, job_id, result="noop")
                    return

                messages = self._build_prompt_messages(job, existing, conversation)
                response, _, _ = await self._chat_fn(
                    None,
                    messages=messages,
                    tools=None,
                    temperature=0.2,
                )
                content = self._extract_content(job, response)
                if content is None:
                    await self._mark_job_done(session_id, job_id, result="noop")
                    logger.info(f"Memory flush job {job_id} skipped long-term update")
                    return
                self._write_target(job, content)

            await self._mark_job_done(session_id, job_id, result="updated")
            logger.info(
                f"Memory flush job {job_id} completed for {target_path or 'unknown target'}"
            )
        except Exception as exc:
            logger.warning(f"Memory flush job {job_id} failed: {exc}")
            attempts = await self._mark_job_error(session_id, job_id, str(exc))
            if attempts is not None and attempts < self.MAX_RETRIES:
                asyncio.create_task(self._retry_later(session_id, job_id, attempts))

    def _extract_content(
        self,
        job: dict[str, Any],
        response: LLMResponse | None,
    ) -> str | None:
        if response is None:
            raise RuntimeError("memory flush cancelled")
        if response.finish_reason == "error":
            raise RuntimeError(response.content or "LLM error during memory flush")
        content = (response.content or "").strip()
        if not content:
            raise RuntimeError("memory flush returned empty content")
        if (
            job["scope"] == "long_term"
            and content == self.LONG_TERM_NOOP_SENTINEL
        ):
            return None
        return content

    def _build_prompt_messages(
        self,
        job: dict[str, Any],
        existing: str,
        conversation: str,
    ) -> list[dict[str, str]]:
        if job["scope"] == "daily":
            date_str = job["date"]
            user_content = (
                f"TARGET DATE: {date_str}\n\n"
                f"=== EXISTING DAILY MEMORY ===\n{existing or '(empty)'}\n\n"
                f"=== CONVERSATION SLICE ===\n{conversation}"
            )
            system_prompt = DAILY_MEMORY_SYSTEM_PROMPT
        else:
            user_content = (
                f"=== EXISTING LONG-TERM MEMORY ===\n{existing or '(empty)'}\n\n"
                f"=== CONVERSATION SLICE ===\n{conversation}"
            )
            system_prompt = LONG_TERM_MEMORY_SYSTEM_PROMPT

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

    def _build_conversation_text(
        self,
        session: Session,
        job: dict[str, Any],
    ) -> str:
        messages = deepcopy(session.messages[job["start_idx"]:job["end_idx"]])
        llm_messages = self._to_llm_messages(messages)
        CacheManager._flush_tool_results(llm_messages, job["flush_type"])
        return self._format_conversation(llm_messages)

    def _to_llm_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        llm_messages: list[dict[str, Any]] = []
        for message in messages:
            if message.get("metadata", {}).get("type") == "compaction":
                continue

            llm_msg: dict[str, Any] = {
                "role": message["role"],
                "content": message.get("content", "") or "",
            }
            if "tool_calls" in message:
                llm_msg["tool_calls"] = message["tool_calls"]
            if "tool_call_id" in message:
                llm_msg["tool_call_id"] = message["tool_call_id"]
            if "name" in message:
                llm_msg["name"] = message["name"]
            llm_messages.append(llm_msg)
        return llm_messages

    def _format_conversation(self, messages: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "") or ""

            if role == "user":
                parts.append(f"[user] {content}\n")
            elif role == "assistant":
                if msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        fn = tc.get("function", {})
                        name = fn.get("name", "")
                        args = fn.get("arguments", "")
                        if isinstance(args, dict):
                            args = json.dumps(args, ensure_ascii=False)
                        parts.append(f"[tool_call] {name}({args})\n")
                if content:
                    parts.append(f"[assistant] {content}\n")
            elif role == "tool":
                parts.append(
                    f"[tool_response:{msg.get('name', '')}] {content}\n"
                )
        return "".join(parts)

    def _target_path(self, job: dict[str, Any]) -> Path:
        if job["scope"] == "daily":
            return self.memory.get_day_file(job["date"])
        return self.memory.memory_file

    def _read_existing(self, job: dict[str, Any]) -> str:
        if job["scope"] == "daily":
            return self.memory.read_day(job["date"])
        return self.memory.read_long_term()

    def _write_target(self, job: dict[str, Any], content: str) -> None:
        if job["scope"] == "daily":
            self.memory.write_day(job["date"], content)
            return
        self.memory.write_long_term(content)

    async def _mark_job_running(
        self,
        session_id: str,
        job_id: str,
    ) -> tuple[Session | None, dict[str, Any] | None]:
        session = self.sessions.get_by_id(session_id)
        if session is None:
            return None, None

        jobs = session.metadata.get(self.META_KEY, {}).get(self.JOBS_KEY, [])
        for job in jobs:
            if job.get("id") != job_id:
                continue
            job["status"] = "running"
            job["attempts"] = int(job.get("attempts", 0)) + 1
            job["updated_at"] = datetime.now().isoformat()
            job["last_error"] = None
            await self.save_session(session)
            return session, deepcopy(job)
        return None, None

    async def _mark_job_done(
        self,
        session_id: str,
        job_id: str,
        *,
        result: str,
    ) -> None:
        session = self.sessions.get_by_id(session_id)
        if session is None:
            return

        jobs = session.metadata.get(self.META_KEY, {}).get(self.JOBS_KEY, [])
        for job in jobs:
            if job.get("id") == job_id:
                job["status"] = "done"
                job["result"] = result
                job["updated_at"] = datetime.now().isoformat()
                job["last_error"] = None
                await self.save_session(session)
                return

    async def _mark_job_error(
        self,
        session_id: str,
        job_id: str,
        error: str,
    ) -> int | None:
        session = self.sessions.get_by_id(session_id)
        if session is None:
            return None

        jobs = session.metadata.get(self.META_KEY, {}).get(self.JOBS_KEY, [])
        for job in jobs:
            if job.get("id") == job_id:
                job["status"] = "error"
                job["updated_at"] = datetime.now().isoformat()
                job["last_error"] = error
                await self.save_session(session)
                return int(job.get("attempts", 0))
        return None

    async def _retry_later(self, session_id: str, job_id: str, attempts: int) -> None:
        """Retry an errored job after a short backoff."""
        await asyncio.sleep(self.RETRY_DELAY_SECONDS * max(1, attempts))

        session = self.sessions.get_by_id(session_id)
        if session is None:
            return

        jobs = session.metadata.get(self.META_KEY, {}).get(self.JOBS_KEY, [])
        for job in jobs:
            if job.get("id") != job_id:
                continue
            if job.get("status") != "error":
                return
            break
        else:
            return

        await self.start_session_jobs(session_id, include_errors=True)
