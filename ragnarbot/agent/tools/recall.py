"""recall: hybrid (vector + BM25) search over the agent's memory files and chats."""

from __future__ import annotations

from typing import Any

from ragnarbot.agent.index.render import render_results
from ragnarbot.agent.tools.base import Tool


class RecallTool(Tool):
    """Search long-term memory and past chats. Backed by an IndexManager-like searcher.

    The searcher must provide: ``available() -> bool``, ``status() -> str`` (shown when
    not yet available), and ``async search(query, *, scope, top_k, date_from, date_to,
    dialogue_id) -> list[dict]``.
    """

    name = "recall"
    description = (
        "Hybrid (semantic + keyword) search over your OWN long-term memory notes and "
        "past chat history — not world knowledge. Reach for it when the user refers to "
        "something discussed earlier, asks what you remember, or you need a "
        "fact/preference/decision from previous days or sessions that isn't in the "
        "current window. For best hits, pair a distinctive literal term (name, number, "
        "rare word) with the concept, in the language it was likely discussed in. "
        "Returns dated, located snippets (which day, which dialogue, which file) you "
        "can file_read to expand."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for (natural language)."},
            "scope": {
                "type": "string",
                "enum": ["memory", "chats", "both"],
                "description": "Where to search. Default: both.",
            },
            "limit": {
                "type": "integer",
                "description": "Max results (default from config).",
                "minimum": 1,
                "maximum": 50,
            },
            "date_from": {"type": "string", "description": "Earliest day, YYYY-MM-DD (optional)."},
            "date_to": {"type": "string", "description": "Latest day, YYYY-MM-DD (optional)."},
            "dialogue_id": {"type": "string", "description": "Restrict to one dialogue/session id (optional)."},
        },
        "required": ["query"],
    }

    def __init__(self, searcher: Any, config: Any):
        self._searcher = searcher
        self._cfg = config

    async def execute(
        self,
        query: str,
        scope: str | None = None,
        limit: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        dialogue_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        if not self._searcher.available():
            return self._searcher.status()
        scope = scope or self._cfg.scope_default
        top_k = limit or self._cfg.top_k
        try:
            rows = await self._searcher.search(
                query,
                scope=scope,
                top_k=top_k,
                date_from=date_from,
                date_to=date_to,
                dialogue_id=dialogue_id,
            )
        except Exception as e:  # never surface a stack trace to the model
            return f"Recall search failed: {e}"
        return render_results(rows, self._cfg.max_output_chars)
