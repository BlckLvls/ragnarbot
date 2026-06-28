"""Chat-session normalization + Q->A chunking for the recall index.

Chats are structured messages, not plain text. We strip tool noise, unwrap voice
transcriptions, keep genuine user/assistant utterances (and substantive captions),
index compaction summaries standalone, then pack consecutive Q->A turns into
<=6-turn / <=512-token chunks. ``finalize()`` (chunking.py) is the hard cap backstop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ragnarbot.agent.index import (
    DOC_PREFIX,
    MAX_TOKENS,
    MAX_TURNS,
    OVERLAP_TOKENS,
    SPECIAL_HEADROOM,
    chunking,
)

_VOICE_OK = re.compile(r"^\[Voice message transcription:\s*(.*)\]$", re.S)
_VOICE_BAD = re.compile(r"^\[(?:Voice message|voice:|audio:)", re.I)
_OPS_ASSISTANT = re.compile(r"^\[(?:Cron result|Hook triggered|Heartbeat)", re.I)
_MEDIA_MARKER = re.compile(r"^\[(?:photo|file|image|empty)\b", re.I)
_SYS_DROP = ("background", "gateway", "update", "poll", "heartbeat")
_SUMMARY_PREFIX = "[Conversation Summary]"


@dataclass
class Turn:
    role: str          # user | assistant | summary | system
    text: str
    ts: str | None
    idx: int
    used_tools: tuple[str, ...] = ()


@dataclass
class Pair:
    turns: list[Turn]
    kind: str          # qa | q_only | a_only | summary | system
    scope_kind: str = field(init=False)

    def __post_init__(self):
        self.scope_kind = self.kind if self.kind in ("summary", "system") else "qa"


# ── normalization (§8.1) ────────────────────────────────────────────

def _flatten(content) -> str:
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return " ".join(p for p in parts if p)
    return content or ""


def _strip_media_markers(text: str) -> str:
    keep = [ln for ln in text.split("\n") if not _MEDIA_MARKER.match(ln.strip())]
    return "\n".join(keep).strip()


def _is_substantive_caption(text: str) -> bool:
    """Keep a caption (assistant text alongside a tool_call) only if it carries signal."""
    return bool(re.search(r"\d|https?://", text)) or len(text.split()) >= 12


def normalize(messages: list[dict], start: int, end: int) -> list[Turn]:
    turns: list[Turn] = []
    for i in range(start, min(end, len(messages))):
        m = messages[i]
        meta = m.get("metadata", {}) or {}
        typ = meta.get("type")
        ts = meta.get("timestamp")
        content = _flatten(m.get("content")).strip()
        role = m.get("role")

        if typ == "compaction":
            body = content
            if body.startswith(_SUMMARY_PREFIX):
                body = body[len(_SUMMARY_PREFIX):].lstrip("\n").strip()
            if body:
                turns.append(Turn("summary", body, ts, i))
            continue

        if role == "tool":
            continue

        if role == "user":
            mv = _VOICE_OK.match(content)
            if mv:
                content = mv.group(1).strip()
            elif _VOICE_BAD.match(content):
                continue  # failed/empty voice
            if content.startswith("[System:"):
                low = content.lower()
                if any(k in low for k in _SYS_DROP):
                    continue
                turns.append(Turn("system", content, ts, i))
                continue
            content = _strip_media_markers(content)
            if content:
                turns.append(Turn("user", content, ts, i))
            continue

        if role == "assistant":
            if _OPS_ASSISTANT.match(content):
                continue
            tools = tuple(
                tc.get("function", {}).get("name", "")
                for tc in (m.get("tool_calls") or [])
            )
            if tools:
                if content and _is_substantive_caption(content):
                    turns.append(Turn("assistant", content, ts, i, used_tools=tools))
                continue  # tool-only cycle (or non-substantive caption) -> drop text
            if content:
                turns.append(Turn("assistant", content, ts, i))
            continue
    return turns


# ── pairing (§8.3) ──────────────────────────────────────────────────

def pair_turns(turns: list[Turn]) -> list[Pair]:
    pairs: list[Pair] = []
    i, n = 0, len(turns)
    while i < n:
        t = turns[i]
        if t.role in ("summary", "system"):
            pairs.append(Pair([t], t.role))
            i += 1
            continue
        burst: list[Turn] = []
        n_user = 0
        while i < n and turns[i].role == "user":
            burst.append(turns[i])
            n_user += 1
            i += 1
        n_asst = 0
        while i < n and turns[i].role == "assistant":
            burst.append(turns[i])
            n_asst += 1
            i += 1
        if not burst:
            i += 1
            continue
        if n_user and n_asst:
            kind = "qa"
        elif n_user:
            kind = "q_only"
        else:
            kind = "a_only"
        pairs.append(Pair(burst, kind))
    return pairs


# ── packing + rendering (§8.3 / §8.4) ───────────────────────────────

def _hard() -> int:
    overhead = chunking.count_tokens(DOC_PREFIX, special=True) + SPECIAL_HEADROOM
    return MAX_TOKENS - overhead - 8  # small margin for the time header line


def _label(role: str, user_name: str) -> str:
    return {"user": user_name, "assistant": "Assistant", "summary": "Summary",
            "system": "System"}.get(role, role.title())


def _time_header(turns: list[Turn]) -> str:
    ts = [t.ts for t in turns if t.ts]
    if not ts:
        return ""
    lo, hi = min(ts), max(ts)
    a, b = lo[:16].replace("T", " "), hi[:16].replace("T", " ")
    return f"[{a}]" if a == b else f"[{a} … {b}]"


def _render(turns: list[Turn], user_name: str) -> str:
    lines = []
    header = _time_header(turns)
    if header:
        lines.append(header)
    for t in turns:
        lines.append(f"{_label(t.role, user_name)}: {t.text}")
    return "\n".join(lines)


def _carry(turns: list[Turn]) -> list[Turn]:
    """Trailing turns to repeat as overlap into the next chunk (<= OVERLAP_TOKENS)."""
    if not turns:
        return []
    last = turns[-1]
    return [last] if chunking.count_tokens(last.text) <= OVERLAP_TOKENS else []


def chunk_chat_segment(
    messages: list[dict],
    start: int,
    end: int,
    meta: dict,
    model_key: str,
) -> list[dict]:
    """Normalize + chunk a session range [start, end) into store rows (no embeddings)."""
    turns = normalize(messages, start, end)
    if not turns:
        return []
    pairs = pair_turns(turns)
    hard = _hard()
    user_name = meta.get("user_name") or "User"

    groups: list[tuple[list[Turn], str]] = []
    buf: list[Turn] = []
    for pair in pairs:
        if pair.scope_kind in ("summary", "system"):
            if buf:
                groups.append((buf, "qa"))
                buf = []
            groups.append((pair.turns, pair.scope_kind))
            continue
        if buf and (
            len(buf) + len(pair.turns) > MAX_TURNS
            or chunking.count_tokens(_render(buf + pair.turns, user_name)) > hard
        ):
            groups.append((buf, "qa"))
            buf = _carry(buf)
        buf = buf + pair.turns
    if buf:
        groups.append((buf, "qa"))

    rows: list[dict] = []
    for turns_g, scope_kind in groups:
        for sub in _split_turns(turns_g):           # enforce <= MAX_TURNS per chunk
            body = _render(sub, user_name)
            for piece in chunking.finalize(body):   # hard <=512 guarantee (reflow if needed)
                rows.append(_row(sub, piece, scope_kind, len(rows), meta, model_key))
    return rows


def _split_turns(turns: list[Turn]) -> list[list[Turn]]:
    if len(turns) <= MAX_TURNS:
        return [turns]
    return [turns[i:i + MAX_TURNS] for i in range(0, len(turns), MAX_TURNS)]


def _row(turns: list[Turn], body: str, scope_kind: str, chunk_index: int,
         meta: dict, model_key: str) -> dict:
    ts = [t.ts for t in turns if t.ts]
    ts_start, ts_end = (min(ts), max(ts)) if ts else (None, None)
    idxs = [t.idx for t in turns]
    tools = sorted({name for t in turns for name in t.used_tools if name})
    return {
        "corpus": "chat",
        "source_id": meta["source_id"],
        "chunk_hash": chunking.chunk_hash(body, model_key),
        "body": body,
        "source_path": meta.get("source_path"),
        "scope_kind": scope_kind,
        "dialogue_id": meta.get("dialogue_id") or meta["source_id"],
        "user_key": meta.get("user_key"),
        "day_start": ts_start[:10] if ts_start else None,
        "day_end": ts_end[:10] if ts_end else None,
        "ts_start": ts_start,
        "ts_end": ts_end,
        "chunk_index": chunk_index,
        "used_tools": ", ".join(tools) if tools else None,
        "msg_start": min(idxs) if idxs else None,
        "msg_end": max(idxs) if idxs else None,
    }
