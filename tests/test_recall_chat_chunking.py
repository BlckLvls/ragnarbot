"""Chat normalization + Q->A chunking, with a deterministic fake tokenizer."""

import pytest

from ragnarbot.agent.index import MAX_TOKENS, MAX_TURNS, chunking
from ragnarbot.agent.index import chat_chunking as cc


class _Enc:
    def __init__(self, ids):
        self.ids = ids


class FakeTokenizer:
    """Whitespace-word tokenizer with reversible encode/decode (test-only)."""

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


META = {"source_id": "telegram_42_20260628_abc", "source_path": "/c/x.jsonl",
        "user_key": "telegram:42", "user_name": "Leo"}


@pytest.fixture(autouse=True)
def _tok():
    chunking.set_tokenizer(FakeTokenizer())
    yield
    chunking.set_tokenizer(None)


def _msg(role, content, ts="2026-06-28T10:00:00", **extra):
    m = {"role": role, "content": content, "metadata": {"timestamp": ts}}
    m.update(extra)
    return m


def test_normalization_filters():
    msgs = [
        _msg("user", "real question about vectors"),
        _msg("assistant", "", tool_calls=[{"function": {"name": "grep"}}]),  # tool-only -> drop
        _msg("tool", "tool output", name="grep"),                            # drop
        _msg("assistant", "Found 3 matches in file.py",                       # substantive caption -> keep
             tool_calls=[{"function": {"name": "grep"}}]),
        _msg("assistant", "ok"),                                             # plain reply -> keep
        _msg("user", "[Voice message transcription: привіт як справи]"),      # unwrap
        _msg("user", "[Voice message — transcription failed: net]"),          # drop
        _msg("user", "[System: background_poll tick]"),                       # telemetry -> drop
    ]
    turns = cc.normalize(msgs, 0, len(msgs))
    texts = [(t.role, t.text) for t in turns]
    assert ("user", "real question about vectors") in texts
    assert ("assistant", "Found 3 matches in file.py") in texts  # caption kept (has digit)
    assert ("assistant", "ok") in texts
    assert ("user", "привіт як справи") in texts                 # tag stripped
    assert all("transcription failed" not in t.text for t in turns)
    assert all("background_poll" not in t.text for t in turns)
    # used_tools recorded on the caption turn
    cap = next(t for t in turns if t.text.startswith("Found 3"))
    assert "grep" in cap.used_tools


def test_nonsubstantive_caption_dropped():
    msgs = [_msg("assistant", "ok done", tool_calls=[{"function": {"name": "click"}}])]
    turns = cc.normalize(msgs, 0, 1)
    assert turns == []  # short caption, no signal -> dropped


def test_compaction_summary_becomes_summary_turn():
    msgs = [_msg("user", "[Conversation Summary]\nUser likes vectors.")]
    msgs[0]["metadata"]["type"] = "compaction"
    turns = cc.normalize(msgs, 0, 1)
    assert len(turns) == 1 and turns[0].role == "summary"
    assert "User likes vectors." in turns[0].text


def test_pairing_kinds():
    turns = [
        cc.Turn("user", "q1", "t", 0),
        cc.Turn("assistant", "a1", "t", 1),
        cc.Turn("user", "q2", "t", 2),       # q_only (no following assistant)
        cc.Turn("summary", "s", "t", 3),
    ]
    pairs = cc.pair_turns(turns)
    kinds = [p.kind for p in pairs]
    assert kinds == ["qa", "q_only", "summary"]


def test_chunk_basic_metadata_and_body():
    msgs = [
        _msg("user", "how does hybrid search work", ts="2026-06-28T10:00:00"),
        _msg("assistant", "it fuses BM25 and vectors", ts="2026-06-28T10:01:00"),
    ]
    rows = cc.chunk_chat_segment(msgs, 0, len(msgs), META, "mk")
    assert len(rows) == 1
    r = rows[0]
    assert r["corpus"] == "chat"
    assert r["source_id"] == META["source_id"]
    assert r["dialogue_id"] == META["source_id"]
    assert r["day_start"] == "2026-06-28"
    assert r["ts_start"] == "2026-06-28T10:00:00" and r["ts_end"] == "2026-06-28T10:01:00"
    assert r["msg_start"] == 0 and r["msg_end"] == 1
    assert "Leo:" in r["body"] and "Assistant:" in r["body"] and "[2026-06-28 10:00" in r["body"]


def test_many_turns_respect_turn_and_token_caps():
    msgs = []
    for k in range(20):
        msgs.append(_msg("user", f"question number {k} " + " ".join(f"w{k}_{j}" for j in range(50)),
                         ts=f"2026-06-28T10:{k:02d}:00"))
        msgs.append(_msg("assistant", f"answer number {k} " + " ".join(f"a{k}_{j}" for j in range(50)),
                         ts=f"2026-06-28T10:{k:02d}:30"))
    rows = cc.chunk_chat_segment(msgs, 0, len(msgs), META, "mk")
    assert len(rows) > 1
    from ragnarbot.agent.index import DOC_PREFIX
    for r in rows:
        # token cap on the real embedded artifact
        assert chunking.count_tokens(DOC_PREFIX + r["body"], special=True) <= MAX_TOKENS
        # turn cap: count speaker lines (exclude the header line)
        speaker_lines = [ln for ln in r["body"].split("\n") if ln.startswith(("Leo:", "Assistant:"))]
        assert len(speaker_lines) <= MAX_TURNS


def test_oversized_single_turn_reflows_into_parts():
    huge = " ".join(f"x{i}" for i in range(MAX_TOKENS + 400))
    msgs = [_msg("user", "explain"), _msg("assistant", huge)]
    rows = cc.chunk_chat_segment(msgs, 0, 2, META, "mk")
    assert len(rows) >= 2  # reflowed
    from ragnarbot.agent.index import DOC_PREFIX
    for r in rows:
        assert chunking.count_tokens(DOC_PREFIX + r["body"], special=True) <= MAX_TOKENS


def test_empty_segment_returns_no_rows():
    msgs = [_msg("tool", "x", name="grep"),
            _msg("assistant", "", tool_calls=[{"function": {"name": "grep"}}])]
    assert cc.chunk_chat_segment(msgs, 0, 2, META, "mk") == []
