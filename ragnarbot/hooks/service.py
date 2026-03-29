"""Hook service — CRUD, storage, and trigger logging."""

import json
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Coroutine

from loguru import logger

from ragnarbot.hooks.types import HookDefinition, HookStore


def _now_ms() -> int:
    return int(time.time() * 1000)


def _generate_hook_id() -> str:
    """Generate a cryptographic hook ID that doubles as the auth secret."""
    return f"hk_{secrets.token_urlsafe(32)}"


class HookService:
    """Manages hook registration, persistence, and trigger logging."""

    def __init__(
        self,
        store_path: Path,
        logs_dir: Path,
        on_trigger: (
            Callable[[HookDefinition, str], Coroutine[Any, Any, str | None]] | None
        ) = None,
    ):
        self.store_path = store_path
        self.logs_dir = logs_dir
        self.on_trigger = on_trigger
        self._store: HookStore | None = None

    # ========== Store I/O ==========

    def _load_store(self) -> HookStore:
        if self._store:
            return self._store

        if self.store_path.exists():
            try:
                data = json.loads(self.store_path.read_text())
                hooks = []
                for h in data.get("hooks", []):
                    hooks.append(HookDefinition(
                        id=h["id"],
                        name=h["name"],
                        instructions=h.get("instructions", ""),
                        mode=h.get("mode", "alert"),
                        enabled=h.get("enabled", True),
                        channel=h.get("channel"),
                        to=h.get("to"),
                        created_at_ms=h.get("createdAtMs", 0),
                        updated_at_ms=h.get("updatedAtMs", 0),
                        trigger_count=h.get("triggerCount", 0),
                    ))
                self._store = HookStore(hooks=hooks)
            except Exception as e:
                logger.warning(f"Failed to load hook store: {e}")
                self._store = HookStore()
        else:
            self._store = HookStore()

        return self._store

    def _save_store(self) -> None:
        if not self._store:
            return

        self.store_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "version": self._store.version,
            "hooks": [
                {
                    "id": h.id,
                    "name": h.name,
                    "instructions": h.instructions,
                    "mode": h.mode,
                    "enabled": h.enabled,
                    "channel": h.channel,
                    "to": h.to,
                    "createdAtMs": h.created_at_ms,
                    "updatedAtMs": h.updated_at_ms,
                    "triggerCount": h.trigger_count,
                }
                for h in self._store.hooks
            ],
        }

        self.store_path.write_text(json.dumps(data, indent=2))

    # ========== CRUD ==========

    def add_hook(
        self,
        name: str,
        instructions: str,
        mode: str = "alert",
        channel: str | None = None,
        to: str | None = None,
    ) -> HookDefinition:
        store = self._load_store()
        now = _now_ms()
        hook = HookDefinition(
            id=_generate_hook_id(),
            name=name,
            instructions=instructions,
            mode=mode if mode in ("alert", "silent") else "alert",
            channel=channel,
            to=to,
            created_at_ms=now,
            updated_at_ms=now,
        )
        store.hooks.append(hook)
        self._save_store()
        logger.info(f"Hook created: '{hook.name}' ({hook.id[:16]}...)")
        return hook

    def get_hook(self, hook_id: str) -> HookDefinition | None:
        store = self._load_store()
        for h in store.hooks:
            if h.id == hook_id:
                return h
        return None

    def list_hooks(self, include_disabled: bool = False) -> list[HookDefinition]:
        store = self._load_store()
        if include_disabled:
            return list(store.hooks)
        return [h for h in store.hooks if h.enabled]

    def update_hook(self, hook_id: str, **changes: Any) -> HookDefinition | None:
        store = self._load_store()
        for h in store.hooks:
            if h.id == hook_id:
                if "name" in changes:
                    h.name = changes["name"]
                if "instructions" in changes:
                    h.instructions = changes["instructions"]
                if "mode" in changes and changes["mode"] in ("alert", "silent"):
                    h.mode = changes["mode"]
                if "enabled" in changes:
                    h.enabled = changes["enabled"]
                h.updated_at_ms = _now_ms()
                self._save_store()
                return h
        return None

    def delete_hook(self, hook_id: str) -> bool:
        store = self._load_store()
        before = len(store.hooks)
        store.hooks = [h for h in store.hooks if h.id != hook_id]
        if len(store.hooks) < before:
            self._save_store()
            return True
        return False

    def increment_trigger_count(self, hook_id: str) -> None:
        store = self._load_store()
        for h in store.hooks:
            if h.id == hook_id:
                h.trigger_count += 1
                self._save_store()
                return

    # ========== Logging ==========

    def log_trigger(
        self,
        hook: HookDefinition,
        payload: str,
        status: str,
        duration_s: float,
        output: str | None = None,
        error: str | None = None,
    ) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = self.logs_dir / f"{hook.id}.jsonl"

        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "hook_id": hook.id[:16],
            "hook_name": hook.name,
            "mode": hook.mode,
            "payload_preview": payload[:200] if payload else "",
            "output": output,
            "status": status,
            "duration_s": round(duration_s, 2),
            "error": error,
        }

        with log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def get_history(self, hook_id: str, limit: int = 10) -> list[dict]:
        log_file = self.logs_dir / f"{hook_id}.jsonl"
        if not log_file.exists():
            return []

        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        entries = []
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
