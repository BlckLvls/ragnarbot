"""Session management for conversation history."""

import json
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from ragnarbot.utils.helpers import (
    get_active_sessions_path,
    get_chats_path,
    get_sessions_path,
    safe_filename,
)


@dataclass
class Session:
    """
    A conversation session.

    Stores messages in JSONL format for easy reading and persistence.
    """

    key: str  # unique session ID, e.g. telegram_934517574_20260207_a1b2c3
    user_key: str  # routing key, e.g. telegram:934517574
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_message(self, role: str, content: str | None, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content or "",
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def get_history(self, max_messages: int = 200) -> list[dict[str, Any]]:
        """
        Get message history for LLM context.

        Returns full LLM-compatible messages (with tool_calls, tool_call_id, name)
        and ensures truncation happens at a user-message boundary to avoid
        orphaned tool_calls without results.

        Args:
            max_messages: Maximum messages to return.

        Returns:
            List of messages in LLM format.
        """
        recent = self.messages[-max_messages:] if len(self.messages) > max_messages else self.messages

        # Find safe start — must begin at a "user" message
        start = 0
        for i, m in enumerate(recent):
            if m["role"] == "user":
                start = i
                break

        result = []
        for m in recent[start:]:
            msg: dict[str, Any] = {"role": m["role"], "content": m.get("content") or ""}
            if "tool_calls" in m:
                msg["tool_calls"] = m["tool_calls"]
            if "tool_call_id" in m:
                msg["tool_call_id"] = m["tool_call_id"]
            if "name" in m:
                msg["name"] = m["name"]
            result.append(msg)
        return result

    def clear(self) -> None:
        """Clear all messages in the session."""
        self.messages = []
        self.updated_at = datetime.now()


class SessionManager:
    """
    Manages conversation sessions with multi-session support.

    Directory layout:
        ~/.ragnarbot/sessions/
        ├── active/          # Per-user active session pointers
        │   └── {ch}_{chatid}.json
        └── chats/           # Chat JSONL files
            └── {ch}_{chatid}_{date}_{hex}.jsonl
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.chats_dir = get_chats_path()
        self.active_dir = get_active_sessions_path()
        self._cache: dict[str, Session] = {}
        self._migrate_legacy()

    # ── public API ──────────────────────────────────────────────

    def get_or_create(self, user_key: str) -> Session:
        """
        Get the active session for a user, or create a new one.

        Args:
            user_key: Routing key (channel:chat_id).

        Returns:
            The active session.
        """
        active_id = self.get_active_id(user_key)

        if active_id:
            # Check cache
            if active_id in self._cache:
                return self._cache[active_id]

            # Load from disk
            session = self._load(active_id, user_key)
            if session:
                self._cache[active_id] = session
                return session

        # No active or file missing — create new
        return self.create_new(user_key)

    def create_new(self, user_key: str) -> Session:
        """
        Create a brand-new session and set it as active.

        Args:
            user_key: Routing key (channel:chat_id).

        Returns:
            The newly created session.
        """
        session_id = self._generate_session_id(user_key)
        session = Session(key=session_id, user_key=user_key)
        self.set_active(user_key, session_id)
        self.save(session)
        self._cache[session_id] = session
        logger.info(f"Created new session {session_id} for {user_key}")
        return session

    def get_active_id(self, user_key: str) -> str | None:
        """Read the active session ID for a user."""
        path = self._get_active_path(user_key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return data.get("session_id")
        except Exception:
            return None

    def set_active(self, user_key: str, session_id: str) -> None:
        """Write the active session pointer for a user."""
        path = self._get_active_path(user_key)
        path.write_text(json.dumps({
            "session_id": session_id,
            "updated_at": datetime.now().isoformat(),
        }))

    def save(self, session: Session) -> None:
        """Save a session to disk."""
        path = self._get_session_path(session.key)

        with open(path, "w") as f:
            metadata_line = {
                "_type": "metadata",
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "user_key": session.user_key,
                "metadata": session.metadata,
            }
            f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")

            for msg in session.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        self._cache[session.key] = session

    def delete(self, session_id: str) -> bool:
        """
        Delete a session.

        Args:
            session_id: The session ID.

        Returns:
            True if deleted, False if not found.
        """
        self._cache.pop(session_id, None)

        path = self._get_session_path(session_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def list_sessions(self, user_key: str | None = None) -> list[dict[str, Any]]:
        """
        List sessions, optionally filtered by user_key.

        Returns:
            List of session info dicts sorted by updated_at descending.
        """
        sessions = []

        for path in self.chats_dir.glob("*.jsonl"):
            try:
                with open(path) as f:
                    first_line = f.readline().strip()
                    if not first_line:
                        continue
                    data = json.loads(first_line)
                    if data.get("_type") != "metadata":
                        continue

                    stored_user_key = data.get("user_key", "")
                    if user_key and stored_user_key != user_key:
                        continue

                    sessions.append({
                        "session_id": path.stem,
                        "user_key": stored_user_key,
                        "created_at": data.get("created_at"),
                        "updated_at": data.get("updated_at"),
                        "path": str(path),
                    })
            except Exception:
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)

    # ── internal helpers ────────────────────────────────────────

    def _generate_session_id(self, user_key: str) -> str:
        """Generate a unique session ID from a user key."""
        safe_key = safe_filename(user_key.replace(":", "_"))
        date_str = datetime.now().strftime("%Y%m%d")
        hex_suffix = uuid.uuid4().hex[:6]
        return f"{safe_key}_{date_str}_{hex_suffix}"

    def _get_active_path(self, user_key: str) -> Path:
        """Get the active-pointer file path for a user."""
        safe_key = safe_filename(user_key.replace(":", "_"))
        return self.active_dir / f"{safe_key}.json"

    def _get_session_path(self, session_id: str) -> Path:
        """Get the JSONL file path for a session."""
        return self.chats_dir / f"{session_id}.jsonl"

    def _load(self, session_id: str, user_key: str) -> Session | None:
        """Load a session from disk."""
        path = self._get_session_path(session_id)

        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            stored_user_key = user_key

            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = (
                            datetime.fromisoformat(data["created_at"])
                            if data.get("created_at")
                            else None
                        )
                        stored_user_key = data.get("user_key", user_key)
                    else:
                        messages.append(data)

            return Session(
                key=session_id,
                user_key=stored_user_key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata,
            )
        except Exception as e:
            logger.warning(f"Failed to load session {session_id}: {e}")
            return None

    # ── legacy migration ────────────────────────────────────────

    def _migrate_legacy(self) -> None:
        """Move old flat sessions into the new chats/ structure."""
        sessions_root = get_sessions_path()

        legacy_files = [
            p for p in sessions_root.glob("*.jsonl")
            if p.is_file()
        ]
        if not legacy_files:
            return

        logger.info(f"Migrating {len(legacy_files)} legacy session(s)...")

        for legacy_path in legacy_files:
            try:
                stem = legacy_path.stem  # e.g. telegram_934517574

                # Reconstruct user_key from filename
                # Old format: {channel}_{chat_id} → channel:chat_id
                parts = stem.split("_", 1)
                if len(parts) == 2:
                    user_key = f"{parts[0]}:{parts[1]}"
                else:
                    user_key = f"unknown:{stem}"

                # Build new session ID using file modification date
                mtime = datetime.fromtimestamp(legacy_path.stat().st_mtime)
                date_str = mtime.strftime("%Y%m%d")
                new_id = f"{stem}_{date_str}_000000"

                new_path = self.chats_dir / f"{new_id}.jsonl"

                # Move file
                shutil.move(str(legacy_path), str(new_path))

                # Patch metadata line to include user_key
                self._patch_legacy_metadata(new_path, user_key)

                # Set as active
                self.set_active(user_key, new_id)

                logger.info(f"Migrated {legacy_path.name} → {new_path.name}")
            except Exception as e:
                logger.warning(f"Failed to migrate {legacy_path.name}: {e}")

    def _patch_legacy_metadata(self, path: Path, user_key: str) -> None:
        """Add user_key to a legacy session's metadata line."""
        try:
            lines = path.read_text().splitlines(keepends=True)
            if not lines:
                return

            first = json.loads(lines[0].strip())
            if first.get("_type") == "metadata" and "user_key" not in first:
                first["user_key"] = user_key
                lines[0] = json.dumps(first) + "\n"
                path.write_text("".join(lines))
        except Exception as e:
            logger.warning(f"Failed to patch metadata for {path.name}: {e}")
