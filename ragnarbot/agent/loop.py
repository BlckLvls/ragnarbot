"""Agent loop: the core processing engine."""

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger

from ragnarbot.bus.events import InboundMessage, OutboundMessage
from ragnarbot.bus.queue import MessageBus
from ragnarbot.providers.base import LLMProvider
from ragnarbot.agent.context import ContextBuilder
from ragnarbot.agent.tools.registry import ToolRegistry
from ragnarbot.agent.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool
from ragnarbot.agent.tools.shell import ExecTool
from ragnarbot.agent.tools.web import WebSearchTool, WebFetchTool
from ragnarbot.agent.tools.media import DownloadFileTool
from ragnarbot.agent.tools.message import MessageTool
from ragnarbot.agent.tools.spawn import SpawnTool
from ragnarbot.agent.tools.cron import CronTool
from ragnarbot.agent.subagent import SubagentManager
from ragnarbot.media.manager import MediaManager
from ragnarbot.session.manager import SessionManager


class AgentLoop:
    """
    The agent loop is the core processing engine.
    
    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """
    
    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 20,
        brave_api_key: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        cron_service: "CronService | None" = None,
        stream_steps: bool = False,
        media_manager: MediaManager | None = None,
<<<<<<< Updated upstream
=======
        debounce_seconds: float = 0.5,
>>>>>>> Stashed changes
    ):
        from ragnarbot.config.schema import ExecToolConfig
        from ragnarbot.cron.service import CronService
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.stream_steps = stream_steps
        self.media_manager = media_manager

        self.context = ContextBuilder(workspace)
        self.sessions = SessionManager(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            brave_api_key=brave_api_key,
            exec_config=self.exec_config,
        )

        self._running = False
        self._register_default_tools()
    
    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        # File tools
        self.tools.register(ReadFileTool())
        self.tools.register(WriteFileTool())
        self.tools.register(EditFileTool())
        self.tools.register(ListDirTool())
        
        # Shell tool
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.exec_config.restrict_to_workspace,
        ))
        
        # Web tools
        self.tools.register(WebSearchTool(api_key=self.brave_api_key))
        self.tools.register(WebFetchTool())
        
        # Message tool
        message_tool = MessageTool(send_callback=self.bus.publish_outbound)
        self.tools.register(message_tool)
        
        # Spawn tool (for subagents)
        spawn_tool = SpawnTool(manager=self.subagents)
        self.tools.register(spawn_tool)
        
        # Cron tool (for scheduling)
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

        # Download file tool (for lazy file downloads)
        if self.media_manager:
            self.tools.register(DownloadFileTool(self.media_manager))
    
    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        logger.info("Agent loop started")
        
        while self._running:
            try:
                # Wait for next message
                msg = await asyncio.wait_for(
                    self.bus.consume_inbound(),
                    timeout=1.0
                )
                
                # Process it
                try:
                    response = await self._process_message(msg)
                    if response:
                        await self.bus.publish_outbound(response)
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    # Send error response
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"Sorry, I encountered an error: {str(e)}"
                    ))
            except asyncio.TimeoutError:
                continue
    
    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")
    
    async def _process_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a single inbound message.
        
        Args:
            msg: The inbound message to process.
        
        Returns:
            The response message, or None if no response needed.
        """
        # Handle system messages (subagent announces)
        # The chat_id contains the original "channel:chat_id" to route back to
        if msg.channel == "system":
            return await self._process_system_message(msg)

        # Handle commands (e.g. /new) before LLM processing
        command = msg.metadata.get("command")
        if command:
            return self._handle_command(command, msg)

        logger.info(f"Processing message from {msg.channel}:{msg.sender_id}")

        # Get or create session
        session = self.sessions.get_or_create(msg.session_key)

        # Store user data in session metadata (telegram only, first time)
        if msg.channel == "telegram" and "user_data" not in session.metadata:
            session.metadata["user_data"] = {
                "user_id": msg.metadata.get("user_id"),
                "username": msg.metadata.get("username"),
                "first_name": msg.metadata.get("first_name"),
                "last_name": msg.metadata.get("last_name"),
            }

        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(msg.channel, msg.chat_id)

        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(msg.channel, msg.chat_id)

        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(msg.channel, msg.chat_id)

        download_tool = self.tools.get("download_file")
        if isinstance(download_tool, DownloadFileTool):
            download_tool.set_context(msg.channel, session.key)

        # Process attachments — save photos to disk, build media_refs
        media_refs: list[dict[str, str]] = []
        if self.media_manager:
            for att in msg.attachments:
                if att.type == "photo" and att.data:
                    ext = _ext_from_mime(att.mime_type)
                    filename = await self.media_manager.save_photo(session.key, att.data, ext)
                    media_refs.append({"type": "photo", "filename": filename})

        # Build prefix tags for the current message (timestamp + reply/forward context)
        from datetime import datetime as _dt
        from ragnarbot.session.manager import _build_message_prefix

        current_meta = {"timestamp": _dt.now().isoformat()}
        for k in ("message_id", "reply_to", "forwarded_from"):
            if k in msg.metadata:
                current_meta[k] = msg.metadata[k]
        prefix = _build_message_prefix(current_meta)
        prefixed_content = prefix + msg.content if prefix else msg.content

        # Build initial messages (use get_history for LLM-formatted messages)
        messages = self.context.build_messages(
            history=session.get_history(),
            current_message=prefixed_content,
            media=msg.media if msg.media else None,
            media_refs=media_refs or None,
            session_key=session.key,
            channel=msg.channel,
            chat_id=msg.chat_id,
            session_metadata=session.metadata,
        )

        # Track where new messages start (the current user message)
        new_start = len(messages) - 1

        # Agent loop
        iteration = 0
        final_content = None

        while iteration < self.max_iterations:
            iteration += 1

            # Call LLM
            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model
            )

            # Handle tool calls
            if response.has_tool_calls:
                # Stream intermediate content to user if enabled
                if self.stream_steps and response.content:
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=response.content,
                        metadata={"intermediate": True},
                    ))

                # Add assistant message with tool calls
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments)  # Must be JSON string
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts
                )

                # Execute tools
                for tool_call in response.tool_calls:
                    args_str = json.dumps(tool_call.arguments)
                    logger.debug(f"Executing tool: {tool_call.name} with arguments: {args_str}")
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                # No tool calls, we're done
                final_content = response.content
                break

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        # Add final assistant message to the messages list
        messages.append({"role": "assistant", "content": final_content})

        # Save ALL new messages to session (user, intermediate assistant+tool_calls, tool results, final)
        for i, m in enumerate(messages[new_start:]):
            extras = {}
            if "tool_calls" in m:
                extras["tool_calls"] = m["tool_calls"]
            if "tool_call_id" in m:
                extras["tool_call_id"] = m["tool_call_id"]
            if "name" in m:
                extras["name"] = m["name"]
            # First message (user) gets enriched metadata from the inbound message.
            # Save original content (not prefixed) so get_history() doesn't double-prefix.
            # Attach media_refs so history can re-encode photos later.
            user_meta = None
            if i == 0:
                user_meta = {
                    k: msg.metadata[k]
                    for k in ("message_id", "reply_to", "forwarded_from")
                    if k in msg.metadata
                }
                if media_refs:
                    extras["media_refs"] = media_refs
            content = msg.content if i == 0 else m.get("content")
            session.add_message(m["role"], content, msg_metadata=user_meta, **extras)
        self.sessions.save(session)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content
        )
    
    def _handle_command(self, command: str, msg: InboundMessage) -> OutboundMessage | None:
        """Dispatch a channel command without calling the LLM."""
        if command == "new_chat":
            return self._handle_new_chat(msg)
        logger.warning(f"Unknown command: {command}")
        return None

    def _handle_new_chat(self, msg: InboundMessage) -> OutboundMessage:
        """Create a new chat session and return a confirmation message."""
        session = self.sessions.create_new(msg.session_key)

        if msg.channel == "telegram":
            session.metadata["user_data"] = {
                "user_id": msg.metadata.get("user_id"),
                "username": msg.metadata.get("username"),
                "first_name": msg.metadata.get("first_name"),
                "last_name": msg.metadata.get("last_name"),
            }
            self.sessions.save(session)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=f"✨ <b>New chat started</b>\n\n🤖 Model: <code>{self.model}</code>",
            metadata={"raw_html": True},
        )

    async def _process_system_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a system message (e.g., subagent announce).
        
        The chat_id field contains "original_channel:original_chat_id" to route
        the response back to the correct destination.
        """
        logger.info(f"Processing system message from {msg.sender_id}")
        
        # Parse origin from chat_id (format: "channel:chat_id")
        if ":" in msg.chat_id:
            parts = msg.chat_id.split(":", 1)
            origin_channel = parts[0]
            origin_chat_id = parts[1]
        else:
            # Fallback
            origin_channel = "cli"
            origin_chat_id = msg.chat_id
        
        # Use the origin session for context
        session_key = f"{origin_channel}:{origin_chat_id}"
        session = self.sessions.get_or_create(session_key)
        
        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(origin_channel, origin_chat_id)
        
        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(origin_channel, origin_chat_id)
        
        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(origin_channel, origin_chat_id)
        
        # Build messages with the announce content
        messages = self.context.build_messages(
            history=session.get_history(),
            current_message=msg.content,
            channel=origin_channel,
            chat_id=origin_chat_id,
            session_metadata=session.metadata,
        )

        # Track where new messages start
        new_start = len(messages) - 1

        # Agent loop (limited for announce handling)
        iteration = 0
        final_content = None

        while iteration < self.max_iterations:
            iteration += 1

            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model
            )

            if response.has_tool_calls:
                # Stream intermediate content to user if enabled
                if self.stream_steps and response.content:
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=origin_channel,
                        chat_id=origin_chat_id,
                        content=response.content,
                        metadata={"intermediate": True},
                    ))

                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments)
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts
                )

                for tool_call in response.tool_calls:
                    args_str = json.dumps(tool_call.arguments)
                    logger.debug(f"Executing tool: {tool_call.name} with arguments: {args_str}")
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                final_content = response.content
                break

        if final_content is None:
            final_content = "Background task completed."

        # Add final assistant message to the messages list
        messages.append({"role": "assistant", "content": final_content})

        # Override the user message content to mark it as system
        messages[new_start]["content"] = f"[System: {msg.sender_id}] {msg.content}"

        # Save ALL new messages to session
        for i, m in enumerate(messages[new_start:]):
            extras = {}
            if "tool_calls" in m:
                extras["tool_calls"] = m["tool_calls"]
            if "tool_call_id" in m:
                extras["tool_call_id"] = m["tool_call_id"]
            if "name" in m:
                extras["name"] = m["name"]
            user_meta = None
            if i == 0:
                user_meta = {
                    k: msg.metadata[k]
                    for k in ("message_id", "reply_to", "forwarded_from")
                    if k in msg.metadata
                }
            session.add_message(m["role"], m.get("content"), msg_metadata=user_meta, **extras)
        self.sessions.save(session)

        return OutboundMessage(
            channel=origin_channel,
            chat_id=origin_chat_id,
            content=final_content
        )
    
    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
    ) -> str:
        """
        Process a message directly (for CLI or cron usage).
        
        Args:
            content: The message content.
            session_key: Session identifier.
            channel: Source channel (for context).
            chat_id: Source chat ID (for context).
        
        Returns:
            The agent's response.
        """
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content
        )
        
        response = await self._process_message(msg)
        return response.content if response else ""


def _ext_from_mime(mime_type: str) -> str:
    """Extract a short extension from a MIME type."""
    mapping = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
    }
    return mapping.get(mime_type, "jpg")
