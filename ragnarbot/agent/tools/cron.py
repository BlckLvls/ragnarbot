"""Cron tool for scheduling reminders and tasks."""

from typing import Any

from ragnarbot.agent.tools.base import Tool
from ragnarbot.cron.service import CronService, _detect_timezone
from ragnarbot.cron.types import CronSchedule


class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""

    def __init__(self, cron_service: CronService):
        self._cron = cron_service
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current session context for delivery."""
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return "Schedule and manage recurring tasks. Actions: add, list, update, remove."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "update", "remove"],
                    "description": "Action to perform",
                },
                "message": {
                    "type": "string",
                    "description": "Task message (for add/update)",
                },
                "name": {
                    "type": "string",
                    "description": "Job name (for add/update)",
                },
                "mode": {
                    "type": "string",
                    "enum": ["isolated", "session"],
                    "description": (
                        "Execution mode: 'isolated' (fresh context, deliver_result) "
                        "or 'session' (injected into user's active chat)"
                    ),
                },
                "every_seconds": {
                    "type": "integer",
                    "description": "Interval in seconds (for recurring tasks)",
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression like '0 9 * * *' (for scheduled tasks)",
                },
                "job_id": {
                    "type": "string",
                    "description": "Job ID (for update/remove)",
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Enable or disable a job (for update)",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        message: str = "",
        name: str = "",
        mode: str = "",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        job_id: str | None = None,
        enabled: bool | None = None,
        **kwargs: Any,
    ) -> str:
        if action == "add":
            return self._add_job(message, name, mode, every_seconds, cron_expr)
        elif action == "list":
            return self._list_jobs()
        elif action == "update":
            return self._update_job(job_id, message, name, mode, every_seconds, cron_expr, enabled)
        elif action == "remove":
            return self._remove_job(job_id)
        return f"Unknown action: {action}"

    def _add_job(
        self,
        message: str,
        name: str,
        mode: str,
        every_seconds: int | None,
        cron_expr: str | None,
    ) -> str:
        if not message:
            return "Error: message is required for add"
        if not self._channel or not self._chat_id:
            return "Error: no session context (channel/chat_id)"

        # Build schedule
        if every_seconds:
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            tz = _detect_timezone()
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
        else:
            return "Error: either every_seconds or cron_expr is required"

        job_name = name or message[:30]
        job_mode = mode if mode in ("isolated", "session") else "isolated"

        job = self._cron.add_job(
            name=job_name,
            schedule=schedule,
            message=message,
            mode=job_mode,
            deliver=True,
            channel=self._channel,
            to=self._chat_id,
        )
        return f"Created job '{job.name}' (id: {job.id}, mode: {job.payload.mode})"

    def _list_jobs(self) -> str:
        jobs = self._cron.list_jobs(include_disabled=True)
        if not jobs:
            return "No scheduled jobs."
        lines = []
        for j in jobs:
            status = "enabled" if j.enabled else "disabled"
            lines.append(
                f"- {j.name} (id: {j.id}, {j.schedule.kind}, "
                f"mode: {j.payload.mode}, {status})"
            )
        return "Scheduled jobs:\n" + "\n".join(lines)

    def _update_job(
        self,
        job_id: str | None,
        message: str,
        name: str,
        mode: str,
        every_seconds: int | None,
        cron_expr: str | None,
        enabled: bool | None,
    ) -> str:
        if not job_id:
            return "Error: job_id is required for update"

        updates: dict[str, Any] = {}
        if name:
            updates["name"] = name
        if message:
            updates["message"] = message
        if mode and mode in ("isolated", "session"):
            updates["mode"] = mode
        if enabled is not None:
            updates["enabled"] = enabled
        if every_seconds:
            updates["schedule"] = {"kind": "every", "every_seconds": every_seconds}
        elif cron_expr:
            updates["schedule"] = {"kind": "cron", "cron_expr": cron_expr}

        if not updates:
            return "Error: nothing to update"

        job = self._cron.update_job(job_id, **updates)
        if job:
            return f"Updated job '{job.name}' ({job.id})"
        return f"Job {job_id} not found"

    def _remove_job(self, job_id: str | None) -> str:
        if not job_id:
            return "Error: job_id is required for remove"
        if self._cron.remove_job(job_id):
            return f"Removed job {job_id}"
        return f"Job {job_id} not found"
