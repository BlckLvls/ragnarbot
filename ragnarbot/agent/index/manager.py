"""IndexManager — background indexing + search, a sibling of MemoryFlushManager.

Chat jobs are durable (session.metadata["vector_index"]["jobs"], idempotent by
fingerprint) and reuse the memory-flush trigger points. Memory re-index runs as
transient tasks (files persist on disk; the startup scan is the safety net). All
heavy work (embed/chunk/DB) happens in the background — the hot path only does a
metadata append + a non-blocking task spawn, and every public hot-path method is
defensively never-raise so a recall bug can never crash a turn.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path
from typing import Callable

from loguru import logger

from ragnarbot.agent.index import chat_chunking, chunking, model_key
from ragnarbot.agent.index.embedder import Embedder
from ragnarbot.agent.index.provision import ensure_embedding_model, sqlite_vec_supported
from ragnarbot.agent.index.store import Store
from ragnarbot.agent.memory import MemoryStore
from ragnarbot.utils.helpers import get_index_dir, get_memory_path, get_models_dir

_META_KEY = "vector_index"
_JOBS_KEY = "jobs"
_MAX_RETRIES = 3
_RETRY_DELAY = 5
_EMBED_BATCH = 64


class IndexManager:
    def __init__(self, sessions, config, save_session_fn: Callable, workspace: Path):
        self.sessions = sessions
        self._cfg = config
        self._save_session_fn = save_session_fn
        self.workspace = workspace
        self._memory = MemoryStore(workspace)

        self._store: Store | None = None
        self._embedder: Embedder | None = None
        self._fail_reason: str | None = None
        self._init_lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task] = {}
        self._mem_tasks: set[asyncio.Task] = set()

    # ── readiness / provisioning ────────────────────────────────────
    def available(self) -> bool:
        return self._store is not None and self._embedder is not None

    def status(self) -> str:
        if self._fail_reason:
            return f"Recall is unavailable: {self._fail_reason}"
        if not getattr(self._cfg, "enabled", True):
            return "Recall is disabled in config."
        return "The recall index is still preparing (downloading the embedding model). Try again shortly."

    async def _ensure_ready(self) -> bool:
        if self.available():
            return True
        if self._fail_reason is not None:
            return False
        async with self._init_lock:
            if self.available():
                return True
            if self._fail_reason is not None:
                return False
            try:
                if not getattr(self._cfg, "enabled", True):
                    self._fail_reason = "disabled in config"
                    return False
                if not sqlite_vec_supported():
                    self._fail_reason = (
                        "this Python build cannot load SQLite extensions "
                        "(use a Homebrew/uv CPython 3.11+)"
                    )
                    logger.warning("recall disabled: {}", self._fail_reason)
                    return False
                model_dir = await ensure_embedding_model(
                    get_models_dir(),
                    quant=getattr(self._cfg, "quant", "q4"),
                    rev=getattr(self._cfg, "embed_rev", None) or _default_rev(),
                    allow_download=getattr(self._cfg, "auto_install", True),
                )
                if model_dir is None:
                    self._fail_reason = "embedding model could not be provisioned"
                    return False
                embedder = Embedder(model_dir, getattr(self._cfg, "quant", "q4"))
                chunking.set_tokenizer(embedder.tokenizer)
                store = Store(get_index_dir() / "recall.db")
                await store.open()
                if not await store.self_test():
                    self._fail_reason = "index self-test failed"
                    return False
                self._embedder, self._store = embedder, store
                logger.info("recall index ready")
                return True
            except Exception as exc:
                self._fail_reason = str(exc)
                logger.warning("recall provisioning failed: {}", exc)
                return False

    # ── search ──────────────────────────────────────────────────────
    async def search(self, query: str, *, scope="both", top_k=8,
                     date_from=None, date_to=None, dialogue_id=None) -> list[dict]:
        if not await self._ensure_ready():
            raise RuntimeError(self._fail_reason or "index not ready")
        vec = await self._embedder.aembed_query(query)
        return await self._store.hybrid_search(
            vec, query, scope=scope, top_k=top_k,
            rrf_k=getattr(self._cfg, "rrf_k", 60),
            date_from=date_from, date_to=date_to, dialogue_id=dialogue_id,
        )

    # ── chat indexing (hot path: never-raise) ───────────────────────
    def enqueue_chat_segment(self, session, segment) -> None:
        try:
            if not getattr(self._cfg, "enabled", True):
                return
            self._enqueue(session, segment.start_idx, segment.end_idx)
        except Exception as exc:
            logger.warning("recall enqueue_chat_segment failed: {}", exc)

    def _enqueue(self, session, start_idx: int, end_idx: int) -> bool:
        if end_idx <= start_idx:
            return False
        state = session.metadata.setdefault(_META_KEY, {})
        jobs = state.setdefault(_JOBS_KEY, [])
        fp = f"chat:{session.key}:{start_idx}:{end_idx}"
        if any(j.get("fingerprint") == fp for j in jobs):
            return False
        jobs.append({
            "id": uuid.uuid4().hex[:12],
            "fingerprint": fp,
            "start_idx": start_idx,
            "end_idx": end_idx,
            "status": "pending",
            "attempts": 0,
        })
        return True

    async def start_chat_jobs(self, session) -> None:
        try:
            jobs = session.metadata.get(_META_KEY, {}).get(_JOBS_KEY, [])
            for job in jobs:
                if job.get("status") not in ("pending", "error"):
                    continue
                jid = job["id"]
                if jid in self._tasks:
                    continue
                task = asyncio.create_task(self._run_chat_job(session.key, jid))
                self._tasks[jid] = task
                task.add_done_callback(lambda _, j=jid: self._tasks.pop(j, None))
        except Exception as exc:
            logger.warning("recall start_chat_jobs failed: {}", exc)

    async def purge_dialogue(self, session_key: str) -> int:
        """Remove a deleted chat's chunks and index cursor from the recall store.

        Best-effort: returns the number of chunks removed, 0 when the index
        is unavailable.
        """
        try:
            if not await self._ensure_ready() or self._store is None:
                return 0
            removed = await self._store.delete_source("chat", session_key)
            await self._store.delete_state(f"chat:{session_key}")
            if removed:
                logger.info("recall: purged {} chunk(s) of deleted chat {}", removed, session_key)
            return removed
        except Exception as exc:
            logger.warning("recall purge_dialogue failed for {}: {}", session_key, exc)
            return 0

    async def _run_chat_job(self, session_key: str, job_id: str) -> None:
        if not await self._ensure_ready():
            return  # stays pending; retried by next trigger/backfill
        session = self.sessions.get_by_id(session_key)
        if session is None:
            return
        job = _find_job(session, job_id)
        if job is None:
            return
        try:
            job["status"] = "running"
            meta = {
                "source_id": session.key,
                "dialogue_id": session.key,
                "source_path": str((self.sessions.chats_dir / f"{session.key}.jsonl")),
                "user_key": session.user_key,
                "user_name": session.metadata.get("user_name") or "User",
            }
            rows = chat_chunking.chunk_chat_segment(
                session.messages, job["start_idx"], job["end_idx"], meta, model_key(self._quant()),
            )
            await self._embed_and_upsert(rows)
            job["status"] = "done"
            await self._store.set_state(
                f"chat:{session.key}", model_key=model_key(self._quant()),
                indexed_upto=max(job["end_idx"], _state_cursor(session)),
            )
            session.metadata[_META_KEY]["cursor"] = max(job["end_idx"], _state_cursor(session))
            await self._save_session_fn(session)
        except Exception as exc:
            logger.warning("recall chat job {} failed: {}", job_id, exc)
            await self._mark_error_and_retry(session_key, job_id)

    async def _mark_error_and_retry(self, session_key: str, job_id: str) -> None:
        session = self.sessions.get_by_id(session_key)
        if session is None:
            return
        job = _find_job(session, job_id)
        if job is None:
            return
        job["attempts"] = int(job.get("attempts", 0)) + 1
        job["status"] = "error"
        await self._save_session_fn(session)
        if job["attempts"] < _MAX_RETRIES:
            asyncio.create_task(self._retry_later(session_key, job_id, job["attempts"]))

    async def _retry_later(self, session_key: str, job_id: str, attempts: int) -> None:
        await asyncio.sleep(_RETRY_DELAY * max(1, attempts))
        session = self.sessions.get_by_id(session_key)
        if session is not None:
            await self.start_chat_jobs(session)

    # ── memory indexing (transient tasks) ───────────────────────────
    def on_memory_written(self, scope: str, date: str | None) -> None:
        try:
            if not getattr(self._cfg, "enabled", True):
                return
            path = self._memory.memory_file if scope == "long_term" else self._memory.get_day_file(date)
            task = asyncio.create_task(self._reindex_memory(Path(path), scope, date))
            self._mem_tasks.add(task)
            task.add_done_callback(self._mem_tasks.discard)
        except Exception as exc:
            logger.warning("recall on_memory_written failed: {}", exc)

    async def _reindex_memory(self, path: Path, scope: str, date: str | None) -> None:
        if not await self._ensure_ready():
            return
        try:
            key = f"memory:{path}"
            if not path.exists():
                await self._store.delete_source("memory", str(path))
                return
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            mk = model_key(self._quant())
            state = await self._store.get_state(key)
            if state and state["content_hash"] == digest and state["model_key"] == mk:
                return  # GATE 1: unchanged
            rows = chunking.chunk_memory_file(
                data.decode("utf-8", "ignore"),
                source_id=str(path), source_path=str(path),
                scope_kind=scope, day=date or "", model_key=mk,
            )
            await self._embed_rows(rows)
            await self._store.replace_source("memory", str(path), rows)
            await self._store.set_state(key, content_hash=digest, model_key=mk)
        except Exception as exc:
            logger.warning("recall memory reindex of {} failed: {}", path, exc)

    async def _scan_memory(self) -> None:
        mem_dir = get_memory_path(self.workspace)
        for f in sorted(mem_dir.glob("????-??-??.md")):
            await self._reindex_memory(f, "daily", f.stem)
        mfile = self._memory.memory_file
        if mfile.exists():
            await self._reindex_memory(mfile, "long_term", None)

    # ── startup resume + backfill ───────────────────────────────────
    async def resume_and_backfill(self) -> None:
        try:
            if not getattr(self._cfg, "enabled", True):
                return
            if not await self._ensure_ready():
                return
            for info in self.sessions.list_sessions():
                sid = info.get("session_id")
                if not sid:
                    continue
                session = self.sessions.get_by_id(sid)
                if session is None:
                    continue
                cursor = await self._chat_cursor(session.key)
                n = len(session.messages)
                changed = self._enqueue(session, cursor, n) if n > cursor else False
                # also re-queue any leftover pending/error jobs
                if changed:
                    await self._save_session_fn(session)
                await self.start_chat_jobs(session)
            await self._scan_memory()
        except Exception as exc:
            logger.warning("recall resume_and_backfill failed: {}", exc)

    async def _chat_cursor(self, session_key: str) -> int:
        state = await self._store.get_state(f"chat:{session_key}")
        return int(state["indexed_upto"]) if state and state.get("indexed_upto") else 0

    # ── helpers ─────────────────────────────────────────────────────
    def _quant(self) -> str:
        return getattr(self._cfg, "quant", "q4")

    async def _embed_and_upsert(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        await self._embed_rows(rows)
        return await self._store.upsert(rows)

    async def _embed_rows(self, rows: list[dict]) -> None:
        for i in range(0, len(rows), _EMBED_BATCH):
            batch = rows[i:i + _EMBED_BATCH]
            vecs = await self._embedder.aembed_documents([r["body"] for r in batch])
            for r, v in zip(batch, vecs):
                r["embedding"] = v

    async def close(self) -> None:
        if self._store is not None:
            await self._store.close()
        if self._embedder is not None:
            self._embedder.close()


def _find_job(session, job_id: str) -> dict | None:
    for job in session.metadata.get(_META_KEY, {}).get(_JOBS_KEY, []):
        if job.get("id") == job_id:
            return job
    return None


def _state_cursor(session) -> int:
    return int(session.metadata.get(_META_KEY, {}).get("cursor", 0))


def _default_rev() -> str:
    from ragnarbot.agent.index.provision import DEFAULT_REV
    return DEFAULT_REV
