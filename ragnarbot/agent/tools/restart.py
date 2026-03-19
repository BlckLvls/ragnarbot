"""Restart tool for scheduling graceful gateway restarts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ragnarbot.agent.tools.base import Tool
from ragnarbot.instance import ensure_instance_root

if TYPE_CHECKING:
    from ragnarbot.agent.loop import AgentLoop


def get_restart_marker_path():
    return ensure_instance_root().restart_marker_path


class RestartTool(Tool):
    """Tool to schedule a graceful gateway restart."""

    name = "restart"
    description = (
        "Schedule a graceful gateway restart. "
        "The restart happens after the current response is fully sent. "
        "Use after changing 'warm' config values that require a restart to apply."
    )

    parameters = {
        "type": "object",
        "properties": {},
    }

    def __init__(self, agent: AgentLoop):
        self._agent = agent
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current session context so we know where to send the post-restart message."""
        self._channel = channel
        self._chat_id = chat_id

    @staticmethod
    def _validate_config() -> str | None:
        """Run the same auth validation the gateway uses at startup."""
        from ragnarbot.auth.credentials import load_credentials
        from ragnarbot.cli.commands import _validate_auth
        from ragnarbot.config.loader import load_config

        config = load_config()
        creds = load_credentials()
        return _validate_auth(config, creds)

    async def execute(self, **kwargs: Any) -> str:
        # Pre-flight: validate config before scheduling restart
        error = self._validate_config()
        if error:
            return json.dumps({
                "status": "blocked",
                "error": error,
                "note": "Fix the config issue before restarting.",
            })

        # Write marker so the new process knows where to report back
        if self._channel and self._chat_id:
            marker_path = get_restart_marker_path()
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text(json.dumps({
                "channel": self._channel,
                "chat_id": self._chat_id,
            }))

        self._agent.request_restart()
        return json.dumps({
            "status": "restart_scheduled",
            "note": "Gateway will restart after this response completes.",
        })
