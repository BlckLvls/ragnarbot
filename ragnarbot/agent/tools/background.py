"""Background execution tools."""

from typing import TYPE_CHECKING, Any

from ragnarbot.agent.tools.base import Tool

if TYPE_CHECKING:
    from ragnarbot.agent.background import BackgroundProcessManager


class ExecBgTool(Tool):
    """Tool to execute a shell command in the background."""

    def __init__(self, manager: "BackgroundProcessManager"):
        self._manager = manager
        self._origin_channel = "cli"
        self._origin_chat_id = "direct"

    def set_context(self, channel: str, chat_id: str) -> None:
        self._origin_channel = channel
        self._origin_chat_id = chat_id

    @property
    def name(self) -> str:
        return "exec_bg"

    @property
    def description(self) -> str:
        return (
            "Execute a shell command in the background. The command runs asynchronously "
            "and you'll be notified when it completes. Use for long-running tasks "
            "(builds, image generation, data processing). Use 'output' to check progress, "
            "'kill' to stop, 'poll' to schedule a status check."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute in the background",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command",
                },
                "label": {
                    "type": "string",
                    "description": "Optional short label for this job (for display)",
                },
            },
            "required": ["command"],
        }

    async def execute(
        self, command: str, working_dir: str | None = None, label: str | None = None, **kwargs: Any
    ) -> str:
        origin = {"channel": self._origin_channel, "chat_id": self._origin_chat_id}
        return await self._manager.spawn(
            command=command,
            working_dir=working_dir,
            label=label,
            origin=origin,
        )


class PollTool(Tool):
    """Tool to schedule a background jobs status poll."""

    def __init__(self, manager: "BackgroundProcessManager"):
        self._manager = manager
        self._origin_channel = "cli"
        self._origin_chat_id = "direct"

    def set_context(self, channel: str, chat_id: str) -> None:
        self._origin_channel = channel
        self._origin_chat_id = chat_id

    @property
    def name(self) -> str:
        return "poll"

    @property
    def description(self) -> str:
        return (
            "Schedule a deferred status poll for background jobs. This is fire-and-forget: "
            "after the specified number of seconds, a status summary will be delivered to you "
            "automatically as a system message. Do NOT call output or poll while waiting — "
            "just continue the conversation normally until the poll result arrives."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "after": {
                    "type": "integer",
                    "description": "Seconds to wait before delivering the status poll",
                },
            },
            "required": ["after"],
        }

    async def execute(self, after: int, **kwargs: Any) -> str:
        origin = {"channel": self._origin_channel, "chat_id": self._origin_chat_id}
        return await self._manager.schedule_poll(after=after, origin=origin)


class OutputTool(Tool):
    """Tool to read output from a background job."""

    def __init__(self, manager: "BackgroundProcessManager"):
        self._manager = manager

    @property
    def name(self) -> str:
        return "output"

    @property
    def description(self) -> str:
        return (
            "Read the current output of a background job. Returns the last N lines "
            "of stdout plus status information."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The background job ID",
                },
                "lines": {
                    "type": "integer",
                    "description": "Number of output lines to return (default 20)",
                },
            },
            "required": ["job_id"],
        }

    async def execute(self, job_id: str, lines: int = 20, **kwargs: Any) -> str:
        return self._manager.get_output(job_id=job_id, lines=lines)


class KillTool(Tool):
    """Tool to kill a running background job."""

    def __init__(self, manager: "BackgroundProcessManager"):
        self._manager = manager

    @property
    def name(self) -> str:
        return "kill"

    @property
    def description(self) -> str:
        return "Kill a running background job or cancel a scheduled poll."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The background job ID to kill",
                },
            },
            "required": ["job_id"],
        }

    async def execute(self, job_id: str, **kwargs: Any) -> str:
        return await self._manager.kill(job_id=job_id)


class DismissTool(Tool):
    """Tool to dismiss a completed background job from the status list."""

    def __init__(self, manager: "BackgroundProcessManager"):
        self._manager = manager

    @property
    def name(self) -> str:
        return "dismiss"

    @property
    def description(self) -> str:
        return (
            "Dismiss a completed, errored, or killed background job. "
            "Removes it from the status summary. Cannot dismiss running jobs."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The background job ID to dismiss",
                },
            },
            "required": ["job_id"],
        }

    async def execute(self, job_id: str, **kwargs: Any) -> str:
        return self._manager.dismiss(job_id=job_id)
