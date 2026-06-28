"""SQLite-backed hybrid store: sqlite-vec (dense KNN) + FTS5 (BM25), fused by RRF.

All DB work runs on ONE dedicated thread that owns the single connection (SQLite
allows one writer; sqlite-vec extensions are per-connection). Callers await async
methods that marshal onto that thread, so the event loop never blocks on SQLite.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import sqlite_vec
from loguru import logger

from ragnarbot.agent.index import EMBED_DIM

# Persisted columns of `chunks` (excluding the autoincrement id).
_COLS = [
    "corpus", "source_id", "chunk_hash", "body", "source_path", "scope_kind",
    "dialogue_id", "user_key", "day_start", "day_end", "ts_start", "ts_end",
    "heading_path", "chunk_index", "used_tools", "msg_start", "msg_end",
]

_DDL = f"""
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    corpus TEXT NOT NULL,
    source_id TEXT NOT NULL,
    chunk_hash TEXT NOT NULL,
    body TEXT NOT NULL,
    source_path TEXT,
    scope_kind TEXT,
    dialogue_id TEXT,
    user_key TEXT,
    day_start TEXT,
    day_end TEXT,
    ts_start TEXT,
    ts_end TEXT,
    heading_path TEXT,
    chunk_index INTEGER,
    used_tools TEXT,
    msg_start INTEGER,
    msg_end INTEGER,
    UNIQUE(corpus, source_id, chunk_hash)
);
CREATE INDEX IF NOT EXISTS idx_chunks_src ON chunks(corpus, source_id);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding float[{EMBED_DIM}]);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
    body, content='chunks', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO fts_chunks(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO fts_chunks(fts_chunks, rowid, body) VALUES ('delete', old.id, old.body);
END;

CREATE TABLE IF NOT EXISTS index_state (
    key TEXT PRIMARY KEY,
    content_hash TEXT,
    model_key TEXT,
    indexed_upto INTEGER DEFAULT 0,
    updated_at TEXT
);
"""


def _scope_to_corpora(scope: str) -> set[str]:
    """Map a tool-facing scope to stored corpus values."""
    if scope == "both":
        return {"memory", "chat"}
    if scope == "chats":
        return {"chat"}
    if scope == "memory":
        return {"memory"}
    return {scope}  # tolerate exact corpus names too


def _fts_query(text: str) -> str | None:
    """Build a safe FTS5 MATCH query (OR of quoted unicode word terms)."""
    terms = [t for t in re.findall(r"\w+", text, flags=re.UNICODE) if len(t) > 1][:32]
    if not terms:
        return None
    return " OR ".join('"%s"' % t.replace('"', '""') for t in terms)


class Store:
    """Async facade over a single-writer sqlite-vec + FTS5 database."""

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recall-db")
        self._conn: sqlite3.Connection | None = None

    async def _call(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, fn, *args)

    # ── lifecycle ───────────────────────────────────────────────────
    async def open(self) -> None:
        await self._call(self._open)

    def _open(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_DDL)
        conn.commit()
        self._conn = conn

    async def close(self) -> None:
        await self._call(self._close)
        self._pool.shutdown(wait=True)

    def _close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def self_test(self) -> bool:
        return await self._call(self._self_test)

    def _self_test(self) -> bool:
        try:
            (v,) = self._conn.execute("select vec_version()").fetchone()
            self._conn.execute("select count(*) from chunks").fetchone()
            return bool(v)
        except Exception as exc:  # pragma: no cover
            logger.warning("recall store self-test failed: {}", exc)
            return False

    # ── writes ──────────────────────────────────────────────────────
    async def upsert(self, rows: list[dict[str, Any]]) -> int:
        return await self._call(self._upsert, rows)

    def _upsert(self, rows: list[dict[str, Any]]) -> int:
        conn = self._conn
        placeholders = ", ".join(["?"] * len(_COLS))
        sql = f"INSERT OR IGNORE INTO chunks ({', '.join(_COLS)}) VALUES ({placeholders})"
        inserted = 0
        for row in rows:
            values = [row.get(c) for c in _COLS]
            cur = conn.execute(sql, values)
            if cur.rowcount == 1:  # newly inserted (not a UNIQUE no-op)
                emb = np.asarray(row["embedding"], dtype=np.float32).ravel()
                conn.execute(
                    "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
                    (cur.lastrowid, sqlite_vec.serialize_float32(emb.tolist())),
                )
                inserted += 1
        conn.commit()
        return inserted

    async def delete_source(self, corpus: str, source_id: str) -> int:
        return await self._call(self._delete_source, corpus, source_id)

    def _delete_source(self, corpus: str, source_id: str) -> int:
        conn = self._conn
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM chunks WHERE corpus=? AND source_id=?", (corpus, source_id)
        )]
        if ids:
            conn.executemany("DELETE FROM vec_chunks WHERE rowid=?", [(i,) for i in ids])
            conn.execute(
                "DELETE FROM chunks WHERE corpus=? AND source_id=?", (corpus, source_id)
            )
            conn.commit()
        return len(ids)

    async def replace_source(self, corpus: str, source_id: str, rows: list[dict]) -> int:
        return await self._call(self._replace_source, corpus, source_id, rows)

    def _replace_source(self, corpus: str, source_id: str, rows: list[dict]) -> int:
        self._delete_source(corpus, source_id)
        return self._upsert(rows)

    # ── index_state (manifest / cursor) ─────────────────────────────
    async def get_state(self, key: str) -> dict | None:
        return await self._call(self._get_state, key)

    def _get_state(self, key: str) -> dict | None:
        row = self._conn.execute(
            "SELECT content_hash, model_key, indexed_upto FROM index_state WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return None
        return {"content_hash": row[0], "model_key": row[1], "indexed_upto": row[2]}

    async def set_state(self, key: str, *, content_hash=None, model_key=None, indexed_upto=None):
        return await self._call(self._set_state, key, content_hash, model_key, indexed_upto)

    def _set_state(self, key, content_hash, model_key, indexed_upto):
        self._conn.execute(
            """INSERT INTO index_state(key, content_hash, model_key, indexed_upto, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 content_hash=coalesce(excluded.content_hash, index_state.content_hash),
                 model_key=coalesce(excluded.model_key, index_state.model_key),
                 indexed_upto=coalesce(excluded.indexed_upto, index_state.indexed_upto),
                 updated_at=excluded.updated_at""",
            (key, content_hash, model_key, indexed_upto, datetime.now().isoformat()),
        )
        self._conn.commit()

    # ── hybrid search ───────────────────────────────────────────────
    async def hybrid_search(
        self,
        query_vec: np.ndarray,
        query_text: str,
        *,
        scope: str = "both",
        top_k: int = 8,
        rrf_k: int = 60,
        date_from: str | None = None,
        date_to: str | None = None,
        dialogue_id: str | None = None,
    ) -> list[dict]:
        return await self._call(
            self._hybrid_search, query_vec, query_text, scope, top_k, rrf_k,
            date_from, date_to, dialogue_id,
        )

    def _hybrid_search(self, query_vec, query_text, scope, top_k, rrf_k,
                       date_from, date_to, dialogue_id) -> list[dict]:
        conn = self._conn
        corpora = _scope_to_corpora(scope)
        k = max(top_k * 8, 50)

        qbytes = sqlite_vec.serialize_float32(
            np.asarray(query_vec, dtype=np.float32).ravel().tolist()
        )
        knn = conn.execute(
            "SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? AND k = ? "
            "ORDER BY distance",
            (qbytes, k),
        ).fetchall()
        knn_ids = [r[0] for r in knn]

        bm_ids: list[int] = []
        match = _fts_query(query_text)
        if match:
            bm = conn.execute(
                "SELECT rowid, bm25(fts_chunks) AS s FROM fts_chunks "
                "WHERE fts_chunks MATCH ? ORDER BY s LIMIT ?",
                (match, k),
            ).fetchall()
            bm_ids = [r[0] for r in bm]

        cand = list(dict.fromkeys(knn_ids + bm_ids))
        if not cand:
            return []

        allowed = self._filter_candidates(cand, corpora, date_from, date_to, dialogue_id)
        if not allowed:
            return []

        scores: dict[int, float] = {}
        for ranked in (knn_ids, bm_ids):
            rank = 0
            for cid in ranked:
                if cid not in allowed:
                    continue
                rank += 1
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (rrf_k + rank)

        top = sorted(scores, key=lambda c: scores[c], reverse=True)[:top_k]
        return self._fetch_rows(top, scores)

    def _filter_candidates(self, ids, corpora, date_from, date_to, dialogue_id) -> set[int]:
        qs = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT id, corpus, day_start, day_end, dialogue_id FROM chunks WHERE id IN ({qs})",
            ids,
        ).fetchall()
        allowed: set[int] = set()
        for cid, corpus, day_start, day_end, dlg in rows:
            if corpus not in corpora:
                continue
            if dialogue_id and dlg != dialogue_id:
                continue
            if date_from and (day_end or "9999") < date_from:
                continue
            if date_to and (day_start or "0000") > date_to:
                continue
            allowed.add(cid)
        return allowed

    def _fetch_rows(self, ids: list[int], scores: dict[int, float]) -> list[dict]:
        if not ids:
            return []
        qs = ",".join("?" * len(ids))
        cur = self._conn.execute(
            f"SELECT id, {', '.join(_COLS)} FROM chunks WHERE id IN ({qs})", ids
        )
        names = [d[0] for d in cur.description]
        by_id = {r[0]: dict(zip(names, r)) for r in cur.fetchall()}
        out = []
        for cid in ids:  # preserve RRF order
            row = by_id.get(cid)
            if row:
                row["score"] = round(scores.get(cid, 0.0), 6)
                out.append(row)
        return out
