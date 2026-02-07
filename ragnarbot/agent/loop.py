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
        debounce_seconds: float = 0.5,
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
        self.debounce_seconds = debounce_seconds

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

                # Debounce: collect rapid-fire messages into a batch
                batch = await self._debounce(msg)

                # Process the batch
                try:
                    response = await self._process_batch(batch)
                    if response:
                        await self.bus.publish_outbound(response)
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
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

    async def _debounce(self, first: InboundMessage) -> list[InboundMessage]:
        """Collect rapid-fire messages from the same session into a batch.

        Uses a sliding window: after receiving a message, wait up to
        ``debounce_seconds`` for more messages from the same session.
        Each same-session message resets the timer.  Messages from a
        different session are re-published to the bus and stop the
        debounce window.

        Returns:
            A list of one or more messages (all from the same session).
        """
        if self.debounce_seconds <= 0:
            return [first]

        batch = [first]
        session_key = first.session_key

        while True:
            try:
                msg = await asyncio.wait_for(
                    self.bus.consume_inbound(),
                    timeout=self.debounce_seconds,
                )
            except asyncio.TimeoutError:
                break

            if msg.session_key == session_key:
                batch.append(msg)
                logger.debug(
                    f"Debounce: batched message #{len(batch)} for {session_key}"
                )
            else:
                # Different session — put it back and stop debouncing
                await self.bus.publish_inbound(msg)
                break

        if len(batch) > 1:
            logger.info(
                f"Debounced {len(batch)} messages for {session_key}"
            )
        return batch

    async def _process_batch(self, batch: list[InboundMessage]) -> OutboundMessage | None:
        """Process a batch of inbound messages as a single LLM turn.

        The first message determines session, channel, chat_id, and tool
        contexts.  Commands and system messages are handled from the first
        message only.  Each message gets its own timestamp prefix so the
        LLM sees them as distinct inputs but responds once.

        Args:
            batch: One or more inbound messages from the same session.

        Returns:
            The response message, or None if no response needed.
        """
        from datetime import datetime as _dt
        from ragnarbot.session.manager import _build_message_prefix

        msg = batch[0]

        # Handle system messages (subagent announces)
        if msg.channel == "system":
            return await self._process_system_message(msg)

        # Handle commands (e.g. /new) before LLM processing
        command = msg.metadata.get("command")
        if command:
            return self._handle_command(command, msg)

        logger.info(
            f"Processing batch of {len(batch)} message(s) from {msg.channel}:{msg.sender_id}"
        )

        # Signal typing indicator to the channel
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="",
            metadata={"chat_action": "typing"},
        ))

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

        # -- Per-message processing: attachments, prefixes, media_refs --
        batch_data: list[dict] = []  # {prefixed_content, media_refs, media, raw_msg}
        for m in batch:
            # Process attachments
            media_refs: list[dict[str, str]] = []
            if self.media_manager:
                for att in m.attachments:
                    if att.type == "photo" and att.data:
                        ext = _ext_from_mime(att.mime_type)
                        filename = await self.media_manager.save_photo(
                            session.key, att.data, ext
                        )
                        media_refs.append({"type": "photo", "filename": filename})

            # Process reply-to photo — save to disk, add to media_refs
            reply_to = m.metadata.get("reply_to")
            if reply_to and isinstance(reply_to, dict) and self.media_manager:
                photo_data = reply_to.pop("photo_data", None)
                photo_mime = reply_to.pop("photo_mime", None)
                if photo_data:
                    ext = _ext_from_mime(photo_mime)
                    filename = await self.media_manager.save_photo(
                        session.key, photo_data, ext
                    )
                    media_refs.append({"type": "photo", "filename": filename})
                    reply_to["has_photo"] = True

            # Build prefix tags (timestamp only on the first message in the batch)
            is_first = m is batch[0]
            current_meta: dict = {}
            if is_first:
                current_meta["timestamp"] = _dt.now().isoformat()
            for k in ("reply_to", "forwarded_from"):
                if k in m.metadata:
                    current_meta[k] = m.metadata[k]
            prefix = _build_message_prefix(current_meta, include_timestamp=is_first)
            prefixed_content = prefix + m.content if prefix else m.content

            batch_data.append({
                "prefixed_content": prefixed_content,
                "media_refs": media_refs,
                "media": m.media if m.media else None,
                "raw_msg": m,
            })

        # -- Build LLM messages: first item uses build_messages (includes history) --
        first = batch_data[0]
        messages = self.context.build_messages(
            history=session.get_history(),
            current_message=first["prefixed_content"],
            media=first["media"],
            media_refs=first["media_refs"] or None,
            session_key=session.key,
            channel=msg.channel,
            chat_id=msg.chat_id,
            session_metadata=session.metadata,
        )

        # Append additional user messages for the rest of the batch
        for item in batch_data[1:]:
            user_msg = self.context.build_user_message(
                content=item["prefixed_content"],
                media=item["media"],
                media_refs=item["media_refs"] or None,
                session_key=session.key,
            )
            messages.append(user_msg)

        # Track where new messages start (the first user message in this batch)
        new_start = len(messages) - len(batch)

        # Agent loop
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
                if self.stream_steps and response.content:
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
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
            final_content = "I've completed processing but have no response to give."

        messages.append({"role": "assistant", "content": final_content})

        # -- Save new messages to session --
        # User messages come first (one per batch item), then assistant/tool messages.
        for i, m_dict in enumerate(messages[new_start:]):
            extras: dict[str, Any] = {}
            if "tool_calls" in m_dict:
                extras["tool_calls"] = m_dict["tool_calls"]
            if "tool_call_id" in m_dict:
                extras["tool_call_id"] = m_dict["tool_call_id"]
            if "name" in m_dict:
                extras["name"] = m_dict["name"]

            # User messages (first len(batch) items) get per-message metadata
            if i < len(batch):
                raw = batch_data[i]["raw_msg"]
                user_meta = {
                    k: raw.metadata[k]
                    for k in ("message_id", "reply_to", "forwarded_from")
                    if k in raw.metadata
                }
                if batch_data[i]["media_refs"]:
                    extras["media_refs"] = batch_data[i]["media_refs"]
                session.add_message(
                    m_dict["role"], raw.content, msg_metadata=user_meta, **extras
                )
            else:
                session.add_message(
                    m_dict["role"], m_dict.get("content"), **extras
                )
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
        
        # Signal typing indicator to the channel
        await self.bus.publish_outbound(OutboundMessage(
            channel=origin_channel,
            chat_id=origin_chat_id,
            content="",
            metadata={"chat_action": "typing"},
        ))

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
        
        response = await self._process_batch([msg])
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
