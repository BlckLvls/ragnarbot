"""Memory chunking + token-budget invariants, with a deterministic fake tokenizer.

The fake treats whitespace-separated words as tokens (encode/decode round-trips
word count), so the <=MAX_TOKENS guarantee and packing logic are exercised without
the model or network.
"""

import pytest

from ragnarbot.agent.index import MAX_TOKENS, chunking


class _Enc:
    def __init__(self, ids):
        self.ids = ids


class FakeTokenizer:
    def __init__(self):
        self.w2i: dict[str, int] = {}
        self.i2w: dict[int, str] = {}

    def _id(self, w: str) -> int:
        if w not in self.w2i:
            i = len(self.w2i) + 10
            self.w2i[w] = i
            self.i2w[i] = w
        return self.w2i[w]

    def encode(self, text: str, add_special_tokens: bool = False) -> _Enc:
        ids = [self._id(w) for w in text.split()]
        if add_special_tokens:
            ids = [1, *ids, 2]
        return _Enc(ids)

    def decode(self, ids: list[int]) -> str:
        return " ".join(self.i2w.get(i, "") for i in ids if i in self.i2w).strip()


@pytest.fixture(autouse=True)
def _set_tok():
    chunking.set_tokenizer(FakeTokenizer())
    yield
    chunking.set_tokenizer(None)


def _artifact_len(body, breadcrumb=None):
    from ragnarbot.agent.index import DOC_PREFIX
    art = DOC_PREFIX + (breadcrumb + "\n\n" if breadcrumb else "") + body
    return chunking.count_tokens(art, special=True)


def test_small_file_single_chunk():
    text = "# 2026-06-28\n\nBought milk. Discussed the recall feature design."
    rows = chunking.chunk_memory_file(
        text, source_id="d", source_path="/m/2026-06-28.md",
        scope_kind="daily", day="2026-06-28", model_key="mk",
    )
    assert len(rows) == 1
    assert rows[0]["chunk_index"] == 0
    assert rows[0]["day_start"] == rows[0]["day_end"] == "2026-06-28"
    assert rows[0]["corpus"] == "memory"
    assert _artifact_len(rows[0]["body"]) <= MAX_TOKENS


def test_large_file_multichunk_all_within_cap():
    sections = []
    for s in range(4):
        body = " ".join(f"word{s}_{i}" for i in range(400))
        sections.append(f"## Section {s}\n\n{body}")
    text = "# 2026-06-28\n\n" + "\n\n".join(sections)
    rows = chunking.chunk_memory_file(
        text, source_id="d", source_path="/m/x.md",
        scope_kind="daily", day="2026-06-28", model_key="mk",
    )
    assert len(rows) > 1
    for r in rows:
        assert _artifact_len(r["body"], r.get("heading_path")) <= MAX_TOKENS
    # continuation chunks carry a breadcrumb (heading_path) and indices are ordered
    assert [r["chunk_index"] for r in rows] == list(range(len(rows)))


def test_recursive_split_respects_limit():
    text = " ".join(f"w{i}" for i in range(1000))
    parts = chunking.recursive_split(text, limit=100)
    assert len(parts) > 1
    for p in parts:
        assert chunking.count_tokens(p) <= 100


def test_token_windows_never_exceeds_limit():
    text = " ".join(f"w{i}" for i in range(250))
    wins = chunking.token_windows(text, limit=60, overlap=10)
    assert len(wins) > 1
    for w in wins:
        assert chunking.count_tokens(w) <= 60


def test_finalize_reflows_oversized_body():
    big = " ".join(f"w{i}" for i in range(MAX_TOKENS + 300))
    pieces = chunking.finalize(big, breadcrumb=None)
    assert len(pieces) > 1
    for p in pieces:
        assert _artifact_len(p) <= MAX_TOKENS


def test_heading_is_sticky_and_inline():
    text = "## Open Threads\n\nfollow up on the deploy"
    rows = chunking.chunk_memory_file(
        text, source_id="d", source_path="/m/x.md",
        scope_kind="daily", day="2026-06-28", model_key="mk",
    )
    assert "Open Threads" in rows[0]["body"]
    assert rows[0]["heading_path"] == "Open Threads"


def test_long_term_has_empty_day():
    rows = chunking.chunk_memory_file(
        "# Long-term\n\nUser prefers concise answers.",
        source_id="MEMORY.md", source_path="/m/MEMORY.md",
        scope_kind="long_term", day="2026-06-28", model_key="mk",
    )
    assert rows[0]["day_start"] == "" and rows[0]["day_end"] == ""


def test_chunk_hash_depends_on_model_key():
    h1 = chunking.chunk_hash("same body", "mk1")
    h2 = chunking.chunk_hash("same body", "mk2")
    h3 = chunking.chunk_hash("same  body", "mk1")  # whitespace-normalized -> equal to h1
    assert h1 != h2
    assert h1 == h3
