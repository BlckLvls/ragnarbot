"""IndexManager hot-path safety + job bookkeeping (no model/network)."""

from types import SimpleNamespace

from ragnarbot.agent.index.manager import IndexManager
from ragnarbot.config.schema import RecallToolConfig


def _mgr(tmp_path, **cfg_over):
    cfg = RecallToolConfig(**cfg_over)
    return IndexManager(sessions=None, config=cfg, save_session_fn=None, workspace=tmp_path)


def _session():
    return SimpleNamespace(key="telegram_1_20260628_aaa", metadata={}, user_key="telegram:1")


def _seg(a, b):
    return SimpleNamespace(start_idx=a, end_idx=b)


def test_enqueue_adds_job_and_dedups(tmp_path):
    mgr = _mgr(tmp_path)
    s = _session()
    mgr.enqueue_chat_segment(s, _seg(0, 4))
    jobs = s.metadata["vector_index"]["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["fingerprint"] == "chat:telegram_1_20260628_aaa:0:4"
    assert jobs[0]["status"] == "pending"
    mgr.enqueue_chat_segment(s, _seg(0, 4))  # same range -> dedup
    assert len(s.metadata["vector_index"]["jobs"]) == 1
    mgr.enqueue_chat_segment(s, _seg(4, 8))  # new range -> added
    assert len(s.metadata["vector_index"]["jobs"]) == 2


def test_enqueue_ignores_empty_range(tmp_path):
    mgr = _mgr(tmp_path)
    s = _session()
    mgr.enqueue_chat_segment(s, _seg(5, 5))
    mgr.enqueue_chat_segment(s, _seg(9, 3))
    assert s.metadata.get("vector_index", {}).get("jobs", []) == []


def test_enqueue_never_raises_on_bad_session(tmp_path):
    mgr = _mgr(tmp_path)
    bad = SimpleNamespace(key="k")  # no metadata attribute
    mgr.enqueue_chat_segment(bad, _seg(0, 2))  # must not raise


def test_enqueue_disabled_is_noop(tmp_path):
    mgr = _mgr(tmp_path, enabled=False)
    s = _session()
    mgr.enqueue_chat_segment(s, _seg(0, 4))
    assert s.metadata == {}


def test_available_and_status_before_ready(tmp_path):
    mgr = _mgr(tmp_path)
    assert mgr.available() is False
    assert "preparing" in mgr.status().lower()
    disabled = _mgr(tmp_path, enabled=False)
    assert "disabled" in disabled.status().lower()


def test_on_memory_written_disabled_is_noop(tmp_path):
    mgr = _mgr(tmp_path, enabled=False)
    # disabled -> returns before scheduling any task (no running loop needed)
    mgr.on_memory_written("daily", "2026-06-28")
    assert mgr._mem_tasks == set()
