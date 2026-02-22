"""Subagent manager for background task execution."""

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from loguru import logger

from ragnarbot.agent.agents_loader import AgentDefinition, AgentsLoader
from ragnarbot.agent.tools.deliver_result import DeliverResultTool
from ragnarbot.agent.tools.registry import ToolRegistry
from ragnarbot.bus.events import InboundMessage
from ragnarbot.bus.queue import MessageBus
from ragnarbot.config.schema import ExecToolConfig
from ragnarbot.providers.base import LLMProvider

BUILTIN_DIR = Path(__file__).parent.parent / "builtin"

# Tools that are safe for sub-agents
SAFE_TOOL_NAMES = {
    "file_read", "file_write", "file_edit", "list_dir",
    "exec", "web_search", "web_fetch", "browser",
    "exec_bg", "poll", "output", "kill", "dismiss",
}


class AgentTaskStatus(str, Enum):
    running = "running"
    completed = "completed"
    stopped = "stopped"
    error = "error"


@dataclass
class AgentTask:
    id: str
    label: str
    agent_name: str | None  # None = general-purpose
    task: str
    status: AgentTaskStatus
    messages: list[dict[str, Any]]  # Full LLM conversation for progress tracking
    stop_event: asyncio.Event
    definition: "AgentDefinition | None" = None  # Stored for resume
    resolved_model: str = ""  # Stored for resume
    result: str | None = None
    error: str | None = None
    created_at: str = ""
    completed_at: str | None = None
    origin: dict[str, str] = field(default_factory=dict)  # channel, chat_id


class SubagentManager:
    """
    Manages background sub-agent execution.

    Sub-agents are agent instances that run in the background to handle
    specific tasks. They can be general-purpose or use a named agent
    definition with specialized instructions and tool access.
    """

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        agents_loader: AgentsLoader,
        model: str | None = None,
        brave_api_key: str | None = None,
        search_engine: str = "brave",
        exec_config: ExecToolConfig | None = None,
        chat_fn=None,
        on_fallback_batch=None,
        browser_manager=None,
        context_builder=None,
    ):
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.agents_loader = agents_loader
        self.model = model or provider.get_default_model()
        self.brave_api_key = brave_api_key
        self.search_engine = search_engine
        self.exec_config = exec_config or ExecToolConfig()
        self.browser_manager = browser_manager
        self.context_builder = context_builder
        self._tasks: dict[str, AgentTask] = {}
        self._async_tasks: dict[str, asyncio.Task[None]] = {}

        if chat_fn is not None:
            self._chat_fn = chat_fn
        else:
            async def _default(session_key=None, **kwargs):
                return await self.provider.chat(**kwargs), False, None
            self._chat_fn = _default
        self._on_fallback_batch = on_fallback_batch

    async def spawn(
        self,
        task: str,
        agent_name: str | None = None,
        model: str | None = None,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
    ) -> str:
        """
        Spawn a sub-agent to execute a task in the background.

        Args:
            task: The task description.
            agent_name: Optional agent type name. None = general-purpose.
            model: Optional model override.
            label: Optional human-readable label.
            origin_channel: The channel to announce results to.
            origin_chat_id: The chat ID to announce results to.

        Returns:
            Status message indicating the sub-agent was started.
        """
        # Load agent definition if specified
        definition: AgentDefinition | None = None
        if agent_name:
            definition = self.agents_loader.load_agent(agent_name)
            if not definition:
                return f"Error: Agent '{agent_name}' not found."

        # Validate allowed tools
        if definition and isinstance(definition.allowed_tools, list):
            unknown = set(definition.allowed_tools) - SAFE_TOOL_NAMES
            if unknown:
                return (
                    f"Error: Agent '{agent_name}' references unknown tools: "
                    f"{', '.join(sorted(unknown))}. "
                    f"Allowed: {', '.join(sorted(SAFE_TOOL_NAMES))}."
                )

        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:40] + ("..." if len(task) > 40 else "")

        # Resolve model: explicit > AGENT.md > self.model
        resolved_model = self.model
        if model:
            resolved_model = model
        elif definition and definition.model != "default":
            resolved_model = definition.model

        origin = {"channel": origin_channel, "chat_id": origin_chat_id}

        agent_task = AgentTask(
            id=task_id,
            label=display_label,
            agent_name=agent_name,
            task=task,
            status=AgentTaskStatus.running,
            messages=[],
            stop_event=asyncio.Event(),
            definition=definition,
            resolved_model=resolved_model,
            created_at=self._timestamp(),
            origin=origin,
        )
        self._tasks[task_id] = agent_task

        # Build tool registry
        tools, deliver_tool = self._build_agent_tool_registry(
            definition=definition,
            channel=origin_channel,
            chat_id=origin_chat_id,
        )

        # Launch background task
        bg_task = asyncio.create_task(
            self._run_agent(agent_task, definition, resolved_model, tools, deliver_tool)
        )
        self._async_tasks[task_id] = bg_task
        bg_task.add_done_callback(lambda _: self._async_tasks.pop(task_id, None))

        agent_label = f"[{agent_name}]" if agent_name else "[general]"
        logger.info(f"Spawned agent {agent_label} [{task_id}]: {display_label}")

        return (
            f"Agent task started (id: {task_id}, "
            f"agent: {agent_name or 'general-purpose'}). "
            f"Use agent_progress to check status."
        )

    async def _run_agent(
        self,
        task: AgentTask,
        definition: AgentDefinition | None,
        model: str,
        tools: ToolRegistry,
        deliver_tool: DeliverResultTool,
        resume: bool = False,
    ) -> None:
        """Main agent execution loop."""
        logger.info(f"Agent [{task.id}] {'resuming' if resume else 'starting'}: {task.label}")
        batch_used_fallback = False

        try:
            if resume:
                # Continue from existing task.messages
                messages: list[dict[str, Any]] = list(task.messages)
            else:
                # Build system prompt
                system_prompt = self._build_system_prompt(task, definition)
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": task.task},
                ]
                task.messages = list(messages)

            while True:
                # Check stop event
                if task.stop_event.is_set():
                    task.status = AgentTaskStatus.stopped
                    task.result = "Task was stopped by user."
                    logger.info(f"Agent [{task.id}] stopped by user")
                    await self._announce_result(task, "stopped")
                    return

                # LLM call
                response, used_fallback, _ = await self._chat_fn(
                    None,
                    messages=messages,
                    tools=tools.get_definitions(),
                    model=model,
                )
                if used_fallback:
                    batch_used_fallback = True

                if response is None or response.finish_reason == "error":
                    raise RuntimeError(
                        response.content if response else "LLM call failed"
                    )

                if response.has_tool_calls:
                    # Add assistant message with tool calls
                    tool_call_dicts = []
                    for tc in response.tool_calls:
                        _tc = {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        if tc.metadata:
                            _tc["metadata"] = tc.metadata
                        tool_call_dicts.append(_tc)

                    assistant_msg = {
                        "role": "assistant",
                        "content": response.content or "",
                        "tool_calls": tool_call_dicts,
                    }
                    messages.append(assistant_msg)
                    task.messages.append(assistant_msg)

                    # Truncated response — tool call arguments are
                    # likely incomplete, return error for each call.
                    if response.finish_reason == "length":
                        logger.warning(
                            f"Agent [{task.id}] response truncated "
                            f"(finish_reason=length), rejecting tool calls"
                        )
                        for tc in response.tool_calls:
                            err = (
                                f"Error: response was cut off (max_tokens limit "
                                f"reached) and this tool call was incomplete. "
                                f"Your output must fit within the token limit. "
                                f"Split large content into smaller calls."
                            )
                            tool_msg = {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "name": tc.name,
                                "content": err,
                            }
                            messages.append(tool_msg)
                            task.messages.append(tool_msg)
                        continue

                    # Execute tools
                    for tc in response.tool_calls:
                        logger.debug(
                            f"Agent [{task.id}] executing: {tc.name}"
                        )
                        result = await tools.execute(tc.name, tc.arguments)
                        tool_msg = {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tc.name,
                            "content": result,
                        }
                        messages.append(tool_msg)
                        task.messages.append(tool_msg)

                    # Check if deliver_result was called
                    if deliver_tool.result is not None:
                        task.status = AgentTaskStatus.completed
                        task.result = deliver_tool.result
                        logger.info(f"Agent [{task.id}] delivered result")
                        await self._announce_result(task, "ok")
                        break
                else:
                    # No tool calls — treat text response as implicit result
                    task.status = AgentTaskStatus.completed
                    task.result = response.content or "Task completed (no output)."
                    logger.info(f"Agent [{task.id}] completed with text response")
                    await self._announce_result(task, "ok")
                    break

        except Exception as e:
            task.status = AgentTaskStatus.error
            task.error = str(e)
            logger.error(f"Agent [{task.id}] failed: {e}")
            await self._announce_result(task, "error")

        # Record fallback accounting
        if self._on_fallback_batch and batch_used_fallback:
            try:
                await self._on_fallback_batch(
                    True, task.origin["channel"], task.origin["chat_id"],
                )
            except Exception:
                logger.warning(f"Agent [{task.id}] failed to record fallback batch")

    def get_progress(self, task_id: str) -> dict[str, Any]:
        """Return task status, tool usage stats, and optional conversation detail."""
        from collections import Counter
        from datetime import datetime

        task = self._tasks.get(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found."}

        # Elapsed time (use completed_at for finished tasks, now for running)
        elapsed_str = ""
        if task.created_at:
            try:
                started = datetime.fromisoformat(task.created_at)
                if task.completed_at:
                    end = datetime.fromisoformat(task.completed_at)
                else:
                    end = datetime.now()
                elapsed = (end - started).total_seconds()
                mins, secs = divmod(int(elapsed), 60)
                elapsed_str = f"{mins}m {secs}s"
            except ValueError:
                elapsed_str = "unknown"

        # Tool usage stats
        tool_counts: Counter[str] = Counter()
        for msg in task.messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls", []):
                    name = tc.get("function", {}).get("name", "?")
                    tool_counts[name] += 1

        return {
            "task_id": task.id,
            "label": task.label,
            "agent": task.agent_name or "general-purpose",
            "status": task.status.value,
            "result": task.result,
            "error": task.error,
            "message_count": len(task.messages),
            "elapsed": elapsed_str,
            "tool_counts": dict(tool_counts),
            "messages": task.messages,
        }

    async def send_message(self, task_id: str, content: str) -> str:
        """Send a follow-up message to a completed agent, resuming it."""
        task = self._tasks.get(task_id)
        if not task:
            return f"Error: Task '{task_id}' not found."
        if task.status == AgentTaskStatus.running:
            return f"Error: Task '{task_id}' is still running. Wait for it to finish first."

        # Resume: append user message, reset state, relaunch
        task.messages.append({"role": "user", "content": content})
        task.status = AgentTaskStatus.running
        task.result = None
        task.error = None
        task.stop_event = asyncio.Event()

        tools, deliver_tool = self._build_agent_tool_registry(
            definition=task.definition,
            channel=task.origin["channel"],
            chat_id=task.origin["chat_id"],
        )

        bg_task = asyncio.create_task(
            self._run_agent(
                task, task.definition, task.resolved_model,
                tools, deliver_tool, resume=True,
            )
        )
        self._async_tasks[task_id] = bg_task
        bg_task.add_done_callback(lambda _: self._async_tasks.pop(task_id, None))

        logger.info(f"Agent [{task_id}] resumed with follow-up message")
        return f"Follow-up message sent. Agent task {task_id} resumed."

    async def stop_task(self, task_id: str) -> str:
        """Signal a running agent to stop."""
        task = self._tasks.get(task_id)
        if not task:
            return f"Error: Task '{task_id}' not found."
        if task.status != AgentTaskStatus.running:
            return f"Task '{task_id}' is already {task.status.value}."

        task.stop_event.set()
        return f"Stop signal sent to agent task {task_id}. It will finish its current step."

    def list_tasks(self) -> list[dict[str, Any]]:
        """List all tracked tasks with summary info."""
        result = []
        for task in self._tasks.values():
            result.append({
                "id": task.id,
                "label": task.label,
                "agent": task.agent_name or "general-purpose",
                "status": task.status.value,
                "message_count": len(task.messages),
                "created_at": task.created_at,
            })
        return result

    def dismiss_task(self, task_id: str) -> str:
        """Remove a completed/stopped/error task from tracking."""
        task = self._tasks.get(task_id)
        if not task:
            return f"Error: Task '{task_id}' not found."
        if task.status == AgentTaskStatus.running:
            return "Error: Cannot dismiss running task. Stop it first."
        self._tasks.pop(task_id, None)
        return f"Task {task_id} dismissed."

    def get_running_count(self) -> int:
        """Return the number of currently running sub-agents."""
        return sum(1 for t in self._tasks.values() if t.status == AgentTaskStatus.running)

    def _build_system_prompt(
        self, task: AgentTask, definition: AgentDefinition | None,
    ) -> str:
        """Build the system prompt for a sub-agent."""
        # Load SUBAGENT.md preamble
        preamble = ""
        preamble_path = BUILTIN_DIR / "SUBAGENT.md"
        if preamble_path.exists():
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).astimezone()
            tz_name = now.strftime("%Z")
            utc_offset = now.strftime("%z")  # e.g. +0200
            offset_fmt = f"UTC{utc_offset[:3]}:{utc_offset[3:]}"  # UTC+02:00
            started_at = (
                f"{now.strftime('%A, %d %B %Y, %H:%M')} ({tz_name}, {offset_fmt})"
            )
            preamble = preamble_path.read_text(encoding="utf-8").format(
                task_id=task.id,
                workspace=str(self.workspace),
                started_at=started_at,
            )

        if definition:
            # Named agent: preamble + AGENT.md body + optional skills
            parts = [preamble, "---", definition.body]

            if definition.allowed_skills != "none" and self.context_builder:
                only = (
                    None if definition.allowed_skills == "all"
                    else list(definition.allowed_skills)
                )
                summary = self.context_builder.skills.build_skills_summary(only=only)
                if summary:
                    parts.append(
                        "---\n\n## Available Skills\n\n"
                        "The following skills are available. To load a skill's full "
                        "instructions, use `file_read` on its `<location>` path.\n\n"
                        + summary
                    )

            return "\n\n".join(parts)
        else:
            # General-purpose: full main agent profile + preamble
            base_prompt = ""
            if self.context_builder:
                base_prompt = self.context_builder.build_system_prompt()
            if base_prompt and preamble:
                return f"{base_prompt}\n\n---\n\n{preamble}"
            return preamble or "You are a helpful assistant completing a background task."

    def _build_agent_tool_registry(
        self,
        definition: AgentDefinition | None,
        channel: str,
        chat_id: str,
    ) -> tuple[ToolRegistry, DeliverResultTool]:
        """Build a tool registry for a sub-agent.

        Named agents get only their allowed tools.
        General-purpose agents get all safe tools.
        """
        from ragnarbot.agent.background import BackgroundProcessManager
        from ragnarbot.agent.tools.background import (
            DismissTool,
            ExecBgTool,
            KillTool,
            OutputTool,
            PollTool,
        )
        from ragnarbot.agent.tools.filesystem import (
            EditFileTool,
            ListDirTool,
            ReadFileTool,
            WriteFileTool,
        )
        from ragnarbot.agent.tools.shell import ExecTool
        from ragnarbot.agent.tools.web import WebFetchTool, WebSearchTool

        reg = ToolRegistry()

        # Determine which tools to include
        if definition and definition.allowed_tools != "all":
            allowed = set(definition.allowed_tools) if isinstance(
                definition.allowed_tools, list
            ) else {definition.allowed_tools}
        else:
            allowed = SAFE_TOOL_NAMES

        # Auto-add file_read when skills are allowed (needed to load SKILL.md)
        if definition and definition.allowed_skills != "none":
            allowed = set(allowed)  # ensure mutable copy
            allowed.add("file_read")

        # File tools
        if "file_read" in allowed:
            reg.register(ReadFileTool(model=self.model))
        if "file_write" in allowed:
            reg.register(WriteFileTool())
        if "file_edit" in allowed:
            reg.register(EditFileTool())
        if "list_dir" in allowed:
            reg.register(ListDirTool())

        # Shell
        if "exec" in allowed:
            reg.register(ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.exec_config.restrict_to_workspace,
                safety_guard=self.exec_config.safety_guard,
            ))

        # Web
        if "web_search" in allowed:
            reg.register(WebSearchTool(
                engine=self.search_engine, api_key=self.brave_api_key,
            ))
        if "web_fetch" in allowed:
            reg.register(WebFetchTool())

        # Browser
        if "browser" in allowed and self.browser_manager:
            from ragnarbot.agent.tools.browser import BrowserTool
            reg.register(BrowserTool(manager=self.browser_manager))

        # Background execution
        if any(t in allowed for t in ("exec_bg", "poll", "output", "kill", "dismiss")):
            bg = BackgroundProcessManager(
                bus=self.bus, workspace=self.workspace, exec_config=self.exec_config,
            )
            if "exec_bg" in allowed:
                exec_bg = ExecBgTool(manager=bg)
                exec_bg.set_context(channel, chat_id)
                reg.register(exec_bg)
            if "poll" in allowed:
                poll = PollTool(manager=bg)
                poll.set_context(channel, chat_id)
                reg.register(poll)
            if "output" in allowed:
                reg.register(OutputTool(manager=bg))
            if "kill" in allowed:
                reg.register(KillTool(manager=bg))
            if "dismiss" in allowed:
                reg.register(DismissTool(manager=bg))

        # Always inject deliver_result
        deliver_tool = DeliverResultTool()
        reg.register(deliver_tool)

        return reg, deliver_tool

    async def _announce_result(
        self,
        task: AgentTask,
        status: str,
    ) -> None:
        """Announce the sub-agent result to the main agent via the message bus."""
        from datetime import datetime

        # Record completion time
        task.completed_at = self._timestamp()

        # Calculate elapsed time
        elapsed_str = ""
        if task.created_at:
            try:
                started = datetime.fromisoformat(task.created_at)
                elapsed = (datetime.now() - started).total_seconds()
                mins, secs = divmod(int(elapsed), 60)
                elapsed_str = f"{mins}m {secs}s"
            except ValueError:
                pass

        status_text = {
            "ok": "completed successfully",
            "error": "failed",
            "stopped": "was stopped",
        }.get(status, status)

        result_content = task.result or task.error or "No output."

        time_note = f" in {elapsed_str}" if elapsed_str else ""
        announce_content = (
            f"[Agent task '{task.label}' {status_text}{time_note}]\n\n"
            f"Task: {task.task}\n\n"
            f"Result:\n{result_content}"
        )

        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{task.origin['channel']}:{task.origin['chat_id']}",
            content=announce_content,
        )

        await self.bus.publish_inbound(msg)
        logger.debug(
            f"Agent [{task.id}] announced result to "
            f"{task.origin['channel']}:{task.origin['chat_id']}"
        )

    @staticmethod
    def _timestamp() -> str:
        from datetime import datetime
        return datetime.now().isoformat()
