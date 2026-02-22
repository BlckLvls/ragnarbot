"""Unified agent management tool for spawning and controlling sub-agents."""

from typing import TYPE_CHECKING, Any

from ragnarbot.agent.tools.base import Tool

if TYPE_CHECKING:
    from ragnarbot.agent.subagent import SubagentManager

ACTIONS = ["spawn", "progress", "list", "message", "stop", "dismiss"]


class AgentTool(Tool):
    """Manage background sub-agents via a single tool with action dispatch."""

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._origin_channel = "cli"
        self._origin_chat_id = "direct"

    def set_context(self, channel: str, chat_id: str) -> None:
        self._origin_channel = channel
        self._origin_chat_id = chat_id

    @property
    def name(self) -> str:
        return "agent"

    @property
    def description(self) -> str:
        return (
            "Manage background sub-agents. Actions: "
            "spawn (start a new agent task), "
            "progress (check task status and tool usage stats), "
            "list (show all tasks), "
            "message (send follow-up to completed task), "
            "stop (cancel running task), "
            "dismiss (remove finished task). "
            "Sub-agents announce their own results when done — "
            "don't poll progress unless the user asks. "
            "The full=true flag on progress is for debugging only, "
            "use it only when the user explicitly wants to see agent internals."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ACTIONS,
                    "description": "The agent action to perform.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID (required for progress/message/stop/dismiss).",
                },
                "task": {
                    "type": "string",
                    "description": "The task for the agent to complete (spawn).",
                },
                "agent_name": {
                    "type": "string",
                    "description": "Agent type name from available agents (spawn; omit for general-purpose).",
                },
                "model": {
                    "type": "string",
                    "description": "Model override (spawn; only if user explicitly requests a specific model).",
                },
                "label": {
                    "type": "string",
                    "description": "Short display label for the task (spawn).",
                },
                "content": {
                    "type": "string",
                    "description": "Message to send to the agent (message).",
                },
                "full": {
                    "type": "boolean",
                    "description": (
                        "Show full conversation log (progress). "
                        "Only use when user explicitly asks to debug agent internals."
                    ),
                },
            },
            "required": ["action"],
        }

    async def execute(self, action: str, **kwargs: Any) -> str:
        dispatch = {
            "spawn": self._action_spawn,
            "progress": self._action_progress,
            "list": self._action_list,
            "message": self._action_message,
            "stop": self._action_stop,
            "dismiss": self._action_dismiss,
        }

        handler = dispatch.get(action)
        if not handler:
            return f"Error: Unknown agent action '{action}'."

        return await handler(**kwargs)

    async def _action_spawn(self, **kwargs: Any) -> str:
        task = kwargs.get("task")
        if not task:
            return "Error: 'task' is required for spawn."
        return await self._manager.spawn(
            task=task,
            agent_name=kwargs.get("agent_name"),
            model=kwargs.get("model"),
            label=kwargs.get("label"),
            origin_channel=self._origin_channel,
            origin_chat_id=self._origin_chat_id,
        )

    async def _action_progress(self, **kwargs: Any) -> str:
        task_id = kwargs.get("task_id")
        if not task_id:
            return "Error: 'task_id' is required for progress."
        progress = self._manager.get_progress(task_id)
        if "task_id" not in progress:
            return progress.get("error", "Task not found.")

        full = kwargs.get("full", False)

        lines = [
            f"Task: {progress['task_id']} ({progress['label']})",
            f"Agent: {progress['agent']}",
            f"Status: {progress['status']}",
            f"Elapsed: {progress['elapsed']}",
            f"Messages: {progress['message_count']}",
        ]

        # Tool usage summary
        tool_counts = progress.get("tool_counts", {})
        if tool_counts:
            lines.append("")
            lines.append("Tool usage:")
            for name, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
                lines.append(f"  {name}: {count}")

        if progress.get("result"):
            lines.append(f"\nResult: {progress['result'][:500]}")
        if progress.get("error"):
            lines.append(f"\nError: {progress['error']}")

        # Full conversation log (debug only)
        if full:
            messages = progress.get("messages", [])
            if messages:
                lines.append("")
                lines.append("--- Conversation log ---")
                for msg in messages:
                    role = msg.get("role", "")
                    if role == "system":
                        continue
                    elif role == "user":
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            lines.append(f"[User] {content[:100]}")
                    elif role == "assistant":
                        content = msg.get("content", "")
                        if content:
                            lines.append(f"[Assistant] {content[:100]}")
                        for tc in msg.get("tool_calls", []):
                            fn = tc.get("function", {})
                            args = fn.get("arguments", "")
                            lines.append(f"  -> {fn.get('name', '?')}({args[:100]})")
                    elif role == "tool":
                        name = msg.get("name", "?")
                        content = msg.get("content", "")
                        preview = content[:100] if isinstance(content, str) else str(content)[:100]
                        lines.append(f"  <- {name}: {preview}")

        return "\n".join(lines)

    async def _action_list(self, **kwargs: Any) -> str:
        tasks = self._manager.list_tasks()
        if not tasks:
            return "No agent tasks."

        lines = ["ID       | Agent            | Status    | Label"]
        lines.append("-" * 60)
        for t in tasks:
            lines.append(
                f"{t['id']:<8} | {t['agent']:<16} | {t['status']:<9} | {t['label']}"
            )
        return "\n".join(lines)

    async def _action_message(self, **kwargs: Any) -> str:
        task_id = kwargs.get("task_id")
        content = kwargs.get("content")
        if not task_id or not content:
            return "Error: 'task_id' and 'content' are required for message."
        return await self._manager.send_message(task_id, content)

    async def _action_stop(self, **kwargs: Any) -> str:
        task_id = kwargs.get("task_id")
        if not task_id:
            return "Error: 'task_id' is required for stop."
        return await self._manager.stop_task(task_id)

    async def _action_dismiss(self, **kwargs: Any) -> str:
        task_id = kwargs.get("task_id")
        if not task_id:
            return "Error: 'task_id' is required for dismiss."
        return self._manager.dismiss_task(task_id)
