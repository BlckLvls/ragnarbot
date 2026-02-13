"""Heartbeat done signal tool."""

from typing import Any

from ragnarbot.agent.tools.base import Tool


class HeartbeatDoneTool(Tool):
    """Signal that the heartbeat check is complete with nothing to report."""

    def __init__(self):
        self._done = False

    @property
    def name(self) -> str:
        return "heartbeat_done"

    @property
    def description(self) -> str:
        return (
            "Signal that the heartbeat check is complete with nothing to report. "
            "Call this when all tasks have been checked and there is nothing to "
            "deliver to the user."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        self._done = True
        return "Heartbeat complete."

    @property
    def done(self) -> bool:
        return self._done
