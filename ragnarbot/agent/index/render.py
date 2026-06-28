"""Render recall hits into a compact, located, dated payload for the LLM."""

from __future__ import annotations


def _when(ts: str | None) -> str:
    return ts[:16].replace("T", " ") if ts else ""


def _meta_line(r: dict) -> str:
    if r.get("corpus") == "chat":
        span = _when(r.get("ts_start"))
        end = _when(r.get("ts_end"))
        when = span if (not end or end == span) else f"{span} … {end}"
        dlg = r.get("dialogue_id") or r.get("source_id") or "?"
        return f"chat · {dlg} · {when}"
    scope = r.get("scope_kind") or "memory"
    day = r.get("day_start") or "long-term"
    hp = r.get("heading_path")
    tail = f" · {hp}" if hp else ""
    return f"memory · {scope} · {day}{tail}"


def render_results(rows: list[dict], max_chars: int = 20000) -> str:
    """Numbered, metadata-headed result blocks; truncated to a char budget."""
    if not rows:
        return "No matches."
    blocks = []
    for i, r in enumerate(rows, 1):
        path = r.get("source_path") or ""
        loc = f"  ↳ {path}" if path else ""
        blocks.append(f"[{i}] {_meta_line(r)}{loc}\n{(r.get('body') or '').strip()}")
    text = "\n\n".join(blocks)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n… (results truncated)"
    return text
