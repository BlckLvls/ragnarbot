"""RecallTool behavior + result rendering, with a fake searcher (no model/db)."""

import asyncio
from types import SimpleNamespace

from ragnarbot.agent.index.render import render_results
from ragnarbot.agent.tools.recall import RecallTool


class FakeSearcher:
    def __init__(self, rows, ok=True, status="preparing"):
        self.rows = rows
        self._ok = ok
        self._status = status
        self.calls = []

    def available(self):
        return self._ok

    def status(self):
        return self._status

    async def search(self, query, *, scope, top_k, date_from, date_to, dialogue_id):
        self.calls.append(dict(query=query, scope=scope, top_k=top_k,
                               date_from=date_from, date_to=date_to, dialogue_id=dialogue_id))
        return self.rows


def _cfg(scope_default="both", top_k=8, max_output_chars=20000):
    return SimpleNamespace(scope_default=scope_default, top_k=top_k,
                           max_output_chars=max_output_chars)


CHAT_ROW = {
    "corpus": "chat", "dialogue_id": "telegram_42_20260628_abc",
    "ts_start": "2026-06-28T10:00:00", "ts_end": "2026-06-28T10:05:00",
    "source_path": "/c/x.jsonl", "body": "Leo: hi\nAssistant: hello",
}
MEM_ROW = {
    "corpus": "memory", "scope_kind": "daily", "day_start": "2026-06-27",
    "heading_path": "Open Threads", "source_path": "/m/2026-06-27.md",
    "body": "follow up on deploy",
}


def test_render_includes_metadata_and_locator():
    out = render_results([CHAT_ROW, MEM_ROW])
    assert "[1] chat · telegram_42_20260628_abc · 2026-06-28 10:00 … 2026-06-28 10:05" in out
    assert "/c/x.jsonl" in out
    assert "[2] memory · daily · 2026-06-27 · Open Threads" in out
    assert "follow up on deploy" in out


def test_render_empty():
    assert render_results([]) == "No matches."


def test_render_truncates_to_budget():
    big = [{"corpus": "memory", "scope_kind": "daily", "day_start": "2026-06-27",
            "source_path": "/m/x.md", "body": "x" * 5000} for _ in range(10)]
    out = render_results(big, max_chars=1000)
    assert len(out) <= 1100 and "truncated" in out


def test_tool_uses_defaults_and_passes_params():
    searcher = FakeSearcher([CHAT_ROW])
    tool = RecallTool(searcher, _cfg(scope_default="both", top_k=8))
    out = asyncio.run(tool.execute(query="vectors"))
    assert "chat ·" in out
    call = searcher.calls[0]
    assert call["scope"] == "both" and call["top_k"] == 8

    asyncio.run(tool.execute(query="x", scope="memory", limit=3, date_from="2026-06-01"))
    call = searcher.calls[1]
    assert call["scope"] == "memory" and call["top_k"] == 3 and call["date_from"] == "2026-06-01"


def test_tool_reports_unavailable():
    tool = RecallTool(FakeSearcher([], ok=False, status="still preparing the search index"), _cfg())
    out = asyncio.run(tool.execute(query="anything"))
    assert out == "still preparing the search index"


def test_tool_param_validation():
    tool = RecallTool(FakeSearcher([]), _cfg())
    assert tool.validate_params({"query": "x"}) == []
    assert tool.validate_params({}) != []  # query required
    assert tool.validate_params({"query": "x", "scope": "bogus"}) != []  # enum
    assert tool.validate_params({"query": "x", "limit": 0}) != []  # minimum


def test_tool_search_error_is_caught():
    class Boom(FakeSearcher):
        async def search(self, *a, **k):
            raise RuntimeError("db gone")
    tool = RecallTool(Boom([]), _cfg())
    out = asyncio.run(tool.execute(query="x"))
    assert "Recall search failed" in out
