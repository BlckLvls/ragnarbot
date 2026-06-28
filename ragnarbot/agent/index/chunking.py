"""Tokenizer-accurate text chunking for the recall index.

The tokenizer is injected once (the embedder's real ``tokenizers.Tokenizer``) so
budgeting uses the *same* counts the model sees — never ``len // 4``, which
under-counts Cyrillic 2-3x and would silently blow the 512 cap. The 512 ceiling is
enforced on the actual embedded artifact (``finalize``), not on a sum of fragment
counts (SentencePiece is non-additive).
"""

from __future__ import annotations

import hashlib
import re

from ragnarbot.agent.index import (
    DOC_PREFIX,
    MAX_TOKENS,
    MIN_CHUNK_TOKENS,
    OVERLAP_TOKENS,
    SPECIAL_HEADROOM,
    TARGET_TOKENS,
)

_SEPS = ["\n\n", "\n", ". ", " "]
_TOK = None  # injected tokenizers.Tokenizer


def set_tokenizer(tok) -> None:
    """Inject the embedder's tokenizer (must expose encode(...).ids and decode())."""
    global _TOK
    _TOK = tok


def _require_tok():
    if _TOK is None:
        raise RuntimeError("chunking tokenizer not set; call set_tokenizer() first")
    return _TOK


def count_tokens(text: str, *, special: bool = False) -> int:
    return len(_require_tok().encode(text, add_special_tokens=special).ids)


def token_windows(text: str, limit: int, overlap: int = 0) -> list[str]:
    """Last-resort splitter: fixed windows over token ids (never per-character)."""
    tok = _require_tok()
    ids = tok.encode(text, add_special_tokens=False).ids
    if len(ids) <= limit:
        return [text]
    step = max(1, limit - overlap)
    out = []
    for i in range(0, len(ids), step):
        piece = tok.decode(ids[i:i + limit]).strip()
        if piece:
            out.append(piece)
        if i + limit >= len(ids):
            break
    return out or [text]


def recursive_split(text: str, limit: int, seps: list[str] | None = None) -> list[str]:
    """Split text so every part is <= limit tokens, preferring natural boundaries."""
    if count_tokens(text) <= limit:
        return [text]
    seps = _SEPS if seps is None else seps
    if not seps:
        return token_windows(text, limit, overlap=0)

    sep, rest = seps[0], seps[1:]
    parts = [p for p in text.split(sep) if p != ""]
    if len(parts) <= 1:
        return recursive_split(text, limit, rest)

    out: list[str] = []
    for p in parts:
        if count_tokens(p) <= limit:
            out.append(p)
        else:
            out.extend(recursive_split(p, limit, rest))
    return out


def _overhead_tokens(breadcrumb: str | None) -> int:
    pre = DOC_PREFIX + (breadcrumb + "\n\n" if breadcrumb else "")
    return count_tokens(pre, special=True) + SPECIAL_HEADROOM


def finalize(body: str, breadcrumb: str | None = None) -> list[str]:
    """Re-measure the real embedded artifact; reflow into <=MAX_TOKENS bodies."""
    artifact = DOC_PREFIX + (breadcrumb + "\n\n" if breadcrumb else "") + body
    if count_tokens(artifact, special=True) <= MAX_TOKENS:
        return [body]
    available = max(MIN_CHUNK_TOKENS, MAX_TOKENS - _overhead_tokens(breadcrumb))
    return token_windows(body, available, overlap=min(OVERLAP_TOKENS, available // 4))


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def chunk_hash(body: str, model_key: str) -> str:
    h = hashlib.sha256((normalize_ws(body) + "\x00" + model_key).encode("utf-8"))
    return h.hexdigest()[:16]


# ── memory-dump (Markdown) chunking ─────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def _parse_blocks(text: str) -> list[dict]:
    """Ordered content blocks, each carrying its live heading_path.

    Headings are prepended to the following content block (sticky — never stranded
    as a chunk tail) and kept inline so FTS sees section names.
    """
    blocks: list[dict] = []
    stack: dict[int, str] = {}
    pending: list[str] = []
    buf: list[str] = []

    def heading_path() -> str:
        return " > ".join(stack[lvl] for lvl in sorted(stack))

    def flush_buf():
        if not buf:
            return
        body = "".join(buf).strip()
        buf.clear()
        if not body:
            return
        text_out = ("\n".join(pending) + "\n" + body) if pending else body
        blocks.append({"text": text_out, "heading_path": heading_path()})
        pending.clear()

    for line in text.splitlines():
        m = _HEADING_RE.match(line.strip())
        if m:
            flush_buf()
            level, title = len(m.group(1)), m.group(2).strip()
            for lvl in [x for x in stack if x >= level]:
                del stack[lvl]
            stack[level] = title
            pending.append(line.strip())
        elif line.strip() == "":
            flush_buf()
        else:
            buf.append(line)
    flush_buf()
    # trailing heading(s) with no content
    if pending:
        blocks.append({"text": "\n".join(pending), "heading_path": heading_path()})
    return blocks


def chunk_memory_file(
    text: str,
    *,
    source_id: str,
    source_path: str,
    scope_kind: str,
    day: str,
    model_key: str,
) -> list[dict]:
    """Chunk a memory Markdown file into <=512-token store rows (no embeddings yet)."""
    blocks = _parse_blocks(text)
    # split oversized blocks up front
    segments: list[dict] = []
    for b in blocks:
        if count_tokens(b["text"]) <= MAX_TOKENS:
            segments.append(b)
        else:
            for piece in recursive_split(b["text"], TARGET_TOKENS):
                segments.append({"text": piece, "heading_path": b["heading_path"]})

    chunks_raw: list[dict] = []
    cur: list[dict] = []
    cur_tok = 0
    started_with_overlap = False

    def emit(group: list[dict], overlap_started: bool):
        if not group:
            return
        body = "\n\n".join(s["text"] for s in group)
        hpath = group[0]["heading_path"]
        is_continuation = bool(chunks_raw) and (overlap_started or not body.lstrip().startswith("#"))
        breadcrumb = hpath if (is_continuation and hpath) else None
        for piece in finalize(body, breadcrumb):
            chunks_raw.append({"body": piece, "heading_path": hpath, "breadcrumb": breadcrumb})

    for seg in segments:
        st = count_tokens(seg["text"])
        if cur and cur_tok + st > MAX_TOKENS:
            emit(cur, started_with_overlap)
            carry = cur[-1:] if count_tokens(cur[-1]["text"]) <= OVERLAP_TOKENS else []
            cur = list(carry)
            cur_tok = sum(count_tokens(s["text"]) for s in cur)
            started_with_overlap = bool(carry)
        cur.append(seg)
        cur_tok += st
        if cur_tok >= TARGET_TOKENS:
            emit(cur, started_with_overlap)
            carry = cur[-1:] if count_tokens(cur[-1]["text"]) <= OVERLAP_TOKENS else []
            cur = list(carry)
            cur_tok = sum(count_tokens(s["text"]) for s in cur)
            started_with_overlap = bool(carry)
    emit(cur, started_with_overlap)

    day_val = "" if scope_kind == "long_term" else day
    rows: list[dict] = []
    for idx, c in enumerate(chunks_raw):
        rows.append({
            "corpus": "memory",
            "source_id": source_id,
            "chunk_hash": chunk_hash(c["body"], model_key),
            "body": c["body"],
            "source_path": source_path,
            "scope_kind": scope_kind,
            "day_start": day_val,
            "day_end": day_val,
            "heading_path": c["heading_path"] or None,
            "chunk_index": idx,
        })
    return rows
