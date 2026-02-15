"""Restart tool for scheduling graceful gateway restarts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ragnarbot.agent.tools.base import Tool

if TYPE_CHECKING:
    from ragnarbot.agent.loop import AgentLoop

RESTART_MARKER = Path.home() / ".ragnarbot" / ".restart_marker"


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
            RESTART_MARKER.parent.mkdir(parents=True, exist_ok=True)
            RESTART_MARKER.write_text(json.dumps({
                "channel": self._channel,
                "chat_id": self._chat_id,
            }))

        self._agent.request_restart()
        return json.dumps({
            "status": "restart_scheduled",
            "note": "Gateway will restart after this response completes.",
        })
