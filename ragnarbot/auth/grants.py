"""Pending access grant storage for the Telegram access flow."""

import json
import secrets
import string
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GrantInfo:
    """Information about a pending access grant."""
    user_id: str
    chat_id: str


class PendingGrantStore:
    """File-based storage for pending access grant codes.

    Stores codes at ~/.ragnarbot/pending_grants.json.
    Codes are 8-character hex strings generated via secrets.token_hex(4).
    If the same user_id already has a pending grant, the existing code is reused.
    """

    def __init__(self, path: Path | None = None):
        self._path = path or (Path.home() / ".ragnarbot" / "pending_grants.json")

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2))

    def get_or_create(self, user_id: str, chat_id: str) -> str:
        """Return an existing code for user_id, or create a new one."""
        data = self._load()

        # Check if user already has a pending code
        for code, info in data.items():
            if info.get("user_id") == user_id:
                # Update chat_id in case it changed
                info["chat_id"] = chat_id
                self._save(data)
                return code

        # Generate new code (mixed-case alphanumeric)
        alphabet = string.ascii_letters + string.digits
        code = ''.join(secrets.choice(alphabet) for _ in range(8))
        data[code] = {"user_id": user_id, "chat_id": chat_id}
        self._save(data)
        return code

    def validate(self, code: str) -> GrantInfo | None:
        """Validate a code and return grant info, or None if invalid."""
        data = self._load()
        info = data.get(code)
        if info:
            return GrantInfo(user_id=info["user_id"], chat_id=info["chat_id"])
        return None

    def remove(self, code: str) -> None:
        """Remove a grant code after it has been used."""
        data = self._load()
        if code in data:
            del data[code]
            self._save(data)
