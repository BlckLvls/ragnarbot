"""Notification pool: append-only feed of background events for the web console."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from ragnarbot.bus.events import OutboundMessage

_MAX_BODY = 4000


class NotificationStore:
    """JSONL-backed notification feed with unread tracking."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._read_ids: set[str] = set()
        self._read_marker = path.with_suffix(".read.json")
        if self._read_marker.exists():
            try:
                self._read_ids = set(json.loads(self._read_marker.read_text()))
            except Exception:
                self._read_ids = set()

    def add(
        self,
        kind: str,
        title: str,
        body: str = "",
        status: str = "ok",
        source_id: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "id": uuid.uuid4().hex[:12],
            "ts": datetime.now().isoformat(),
            "kind": kind,
            "title": title[:200],
            "body": (body or "")[:_MAX_BODY],
            "status": status,
            "source_id": source_id,
        }
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"Failed to persist notification: {e}")
        return record

    async def add_and_publish(self, bus: Any, **kwargs: Any) -> dict[str, Any]:
        """Add a notification and push it to connected web clients."""
        record = self.add(**kwargs)
        try:
            await bus.publish_outbound(OutboundMessage(
                channel="web",
                chat_id="main",
                content="",
                metadata={"event": "notification", "data": record},
            ))
        except Exception as e:
            logger.debug(f"Failed to publish notification event: {e}")
        return record

    def list(
        self, limit: int = 50, before: str | None = None, kind: str | None = None,
    ) -> list[dict[str, Any]]:
        records = []
        if self.path.exists():
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        records.reverse()  # newest first
        if kind:
            records = [r for r in records if r.get("kind") == kind]
        if before:
            ids = [r["id"] for r in records]
            if before in ids:
                records = records[ids.index(before) + 1:]
        records = records[:limit]
        for r in records:
            r["read"] = r["id"] in self._read_ids
        return records

    def unread_count(self) -> int:
        total = 0
        if not self.path.exists():
            return 0
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line)["id"] not in self._read_ids:
                        total += 1
                except Exception:
                    continue
        return total

    def mark_read(self, ids: list[str] | None = None) -> None:
        """Mark specific notifications read, or all when ids is None."""
        if ids is None:
            if self.path.exists():
                with open(self.path) as f:
                    for line in f:
                        try:
                            self._read_ids.add(json.loads(line)["id"])
                        except Exception:
                            continue
        else:
            self._read_ids.update(ids)
        try:
            self._read_marker.write_text(json.dumps(sorted(self._read_ids)))
        except Exception as e:
            logger.warning(f"Failed to persist read markers: {e}")
