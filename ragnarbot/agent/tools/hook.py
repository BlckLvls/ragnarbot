"""Hook tool for creating and managing webhook hooks."""

from typing import Any

from ragnarbot.agent.tools.base import Tool
from ragnarbot.hooks.service import HookService


class HookTool(Tool):
    """Tool to create and manage webhook hooks."""

    def __init__(self, hook_service: HookService):
        self._hooks = hook_service
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current session context for delivery."""
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "hook"

    @property
    def description(self) -> str:
        return "Create and manage webhook hooks. Actions: create, list, update, delete, history."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "update", "delete", "history"],
                    "description": "Action to perform",
                },
                "name": {
                    "type": "string",
                    "description": "Hook name (for create/update)",
                },
                "instructions": {
                    "type": "string",
                    "description": (
                        "Instructions for the isolated handler session. "
                        "Describes what to do with the incoming payload. (for create/update)"
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["alert", "silent"],
                    "description": (
                        "Delivery mode: 'alert' (deliver to chat, default) "
                        "or 'silent' (log only, no delivery unless instructions say so)"
                    ),
                },
                "id": {
                    "type": "string",
                    "description": "Hook ID (for update/delete/history)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of history entries to return (default 10)",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        name: str = "",
        instructions: str = "",
        mode: str = "",
        id: str = "",
        limit: int = 10,
        **kwargs: Any,
    ) -> str:
        if action == "create":
            return self._create(name, instructions, mode)
        elif action == "list":
            return self._list()
        elif action == "update":
            return self._update(id, name, instructions, mode)
        elif action == "delete":
            return self._delete(id)
        elif action == "history":
            return self._history(id, limit)
        return f"Unknown action: {action}"

    def _create(self, name: str, instructions: str, mode: str) -> str:
        if not name:
            return "Error: name is required for create"
        if not instructions:
            return "Error: instructions is required for create"
        if not self._channel or not self._chat_id:
            return "Error: no session context (channel/chat_id)"

        hook_mode = mode if mode in ("alert", "silent") else "alert"
        hook = self._hooks.add_hook(
            name=name,
            instructions=instructions,
            mode=hook_mode,
            channel=self._channel,
            to=self._chat_id,
        )

        return (
            f"Created hook '{hook.name}'\n"
            f"- ID/Secret: {hook.id}\n"
            f"- URL: /hooks/{hook.id}\n"
            f"- Mode: {hook.mode}\n\n"
            f"Trigger with:\n"
            f"  curl -X POST http://HOST:PORT/hooks/{hook.id} "
            f"-H 'Content-Type: application/json' "
            f"-d '{{\"your\": \"payload\"}}'"
        )

    def _list(self) -> str:
        hooks = self._hooks.list_hooks(include_disabled=True)
        if not hooks:
            return "No registered hooks."
        lines = []
        for h in hooks:
            status = "enabled" if h.enabled else "disabled"
            lines.append(
                f"- {h.name} (id: {h.id[:16]}..., mode: {h.mode}, "
                f"triggers: {h.trigger_count}, {status})"
            )
        return "Registered hooks:\n" + "\n".join(lines)

    def _update(self, hook_id: str, name: str, instructions: str, mode: str) -> str:
        if not hook_id:
            return "Error: id is required for update"

        changes: dict[str, Any] = {}
        if name:
            changes["name"] = name
        if instructions:
            changes["instructions"] = instructions
        if mode and mode in ("alert", "silent"):
            changes["mode"] = mode

        if not changes:
            return "Error: nothing to update"

        hook = self._hooks.update_hook(hook_id, **changes)
        if hook:
            return f"Updated hook '{hook.name}' ({hook.id[:16]}...)"
        return "Hook not found"

    def _delete(self, hook_id: str) -> str:
        if not hook_id:
            return "Error: id is required for delete"
        if self._hooks.delete_hook(hook_id):
            return f"Deleted hook {hook_id[:16]}..."
        return "Hook not found"

    def _history(self, hook_id: str, limit: int) -> str:
        if not hook_id:
            return "Error: id is required for history"

        entries = self._hooks.get_history(hook_id, limit=limit)
        if not entries:
            return "No trigger history for this hook."

        lines = []
        for e in entries:
            lines.append(
                f"- [{e.get('timestamp', '?')}] status: {e.get('status', '?')}, "
                f"duration: {e.get('duration_s', '?')}s"
            )
        return f"Last {len(entries)} triggers:\n" + "\n".join(lines)
