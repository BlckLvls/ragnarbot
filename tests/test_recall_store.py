"""Store mechanics (sqlite-vec KNN + FTS5 BM25 + RRF) with synthetic vectors.

No model/network: vectors are constructed by hand so ranking is deterministic.
"""

import asyncio

import numpy as np
import pytest

from ragnarbot.agent.index import EMBED_DIM
from ragnarbot.agent.index.store import Store


def _unit(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(EMBED_DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def _row(corpus, source_id, body, vec, **extra):
    base = dict(
        corpus=corpus, source_id=source_id, chunk_hash=f"{corpus}:{source_id}:{body}",
        body=body, embedding=vec, day_start="2026-06-28", day_end="2026-06-28",
    )
    base.update(extra)
    return base


async def _fresh_store(tmp_path) -> Store:
    st = Store(tmp_path / "recall.db")
    await st.open()
    return st


def test_open_self_test(tmp_path):
    async def go():
        st = await _fresh_store(tmp_path)
        assert await st.self_test() is True
        await st.close()
    asyncio.run(go())


def test_upsert_dedup_and_delete(tmp_path):
    async def go():
        st = await _fresh_store(tmp_path)
        rows = [_row("chat", "s1", "hello world", _unit(1))]
        assert await st.upsert(rows) == 1
        assert await st.upsert(rows) == 0  # UNIQUE(corpus,source_id,chunk_hash) -> no-op
        assert await st.delete_source("chat", "s1") == 1
        assert await st.upsert(rows) == 1  # reinsert after delete
        await st.close()
    asyncio.run(go())


def test_knn_ranks_nearest_vector_first(tmp_path):
    async def go():
        st = await _fresh_store(tmp_path)
        target = _unit(7)
        near = (target + 0.01 * _unit(99)).astype(np.float32)
        near /= np.linalg.norm(near)
        await st.upsert([
            _row("memory", "m", "alpha", target),
            _row("memory", "m", "beta", _unit(2)),
            _row("memory", "m", "gamma", _unit(3)),
        ])
        # query with a text that matches nothing (force pure vector signal)
        res = await st.hybrid_search(near, "zzzznomatch", scope="memory", top_k=3)
        assert res and res[0]["body"] == "alpha"
        await st.close()
    asyncio.run(go())


def test_bm25_contributes_via_text(tmp_path):
    async def go():
        st = await _fresh_store(tmp_path)
        # all vectors random/unrelated to the query vector; only the lexical term differs
        await st.upsert([
            _row("chat", "s", "the quarterly revenue grew sharply", _unit(11)),
            _row("chat", "s", "buy milk and bread", _unit(12)),
        ])
        res = await st.hybrid_search(_unit(500), "revenue", scope="chats", top_k=2)
        assert res and "revenue" in res[0]["body"]
        await st.close()
    asyncio.run(go())


def test_scope_and_date_filters(tmp_path):
    async def go():
        st = await _fresh_store(tmp_path)
        v = _unit(21)
        await st.upsert([
            _row("memory", "m", "vector search notes", v, day_start="2026-01-01", day_end="2026-01-01"),
            _row("chat", "c", "vector search chat", _unit(22), day_start="2026-06-28", day_end="2026-06-28"),
        ])
        only_mem = await st.hybrid_search(v, "vector search", scope="memory", top_k=5)
        assert {r["corpus"] for r in only_mem} == {"memory"}

        both_recent = await st.hybrid_search(
            v, "vector search", scope="both", top_k=5, date_from="2026-06-01", date_to="2026-06-30"
        )
        assert {r["corpus"] for r in both_recent} == {"chat"}  # memory row is out of date range
        await st.close()
    asyncio.run(go())


def test_index_state_roundtrip(tmp_path):
    async def go():
        st = await _fresh_store(tmp_path)
        assert await st.get_state("k") is None
        await st.set_state("k", content_hash="h1", model_key="mk", indexed_upto=5)
        s = await st.get_state("k")
        assert s["content_hash"] == "h1" and s["indexed_upto"] == 5
        await st.set_state("k", indexed_upto=9)  # partial update keeps content_hash
        s = await st.get_state("k")
        assert s["content_hash"] == "h1" and s["indexed_upto"] == 9
        await st.close()
    asyncio.run(go())


@pytest.mark.parametrize("scope", ["memory", "chats", "both"])
def test_empty_db_returns_empty(tmp_path, scope):
    async def go():
        st = await _fresh_store(tmp_path)
        assert await st.hybrid_search(_unit(1), "anything", scope=scope, top_k=5) == []
        await st.close()
    asyncio.run(go())


def test_delete_state_removes_cursor(tmp_path):
    async def go():
        st = await _fresh_store(tmp_path)
        await st.set_state("chat:dead_session", indexed_upto=42)
        assert (await st.get_state("chat:dead_session"))["indexed_upto"] == 42
        await st.delete_state("chat:dead_session")
        assert await st.get_state("chat:dead_session") is None
        await st.delete_state("chat:never_existed")  # idempotent
        await st.close()
    asyncio.run(go())
