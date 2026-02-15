"""Agent loop: the core processing engine."""

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger

from ragnarbot.agent.background import BackgroundProcessManager
from ragnarbot.agent.cache import CacheManager
from ragnarbot.agent.compactor import Compactor
from ragnarbot.agent.context import ContextBuilder
from ragnarbot.agent.subagent import SubagentManager
from ragnarbot.agent.tools.background import (
    DismissTool,
    ExecBgTool,
    KillTool,
    OutputTool,
    PollTool,
)
from ragnarbot.agent.tools.config_tool import ConfigTool
from ragnarbot.agent.tools.cron import CronTool
from ragnarbot.agent.tools.deliver_result import DeliverResultTool
from ragnarbot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from ragnarbot.agent.tools.heartbeat import HeartbeatTool, parse_blocks
from ragnarbot.agent.tools.heartbeat_done import HeartbeatDoneTool
from ragnarbot.agent.tools.media import DownloadFileTool
from ragnarbot.agent.tools.registry import ToolRegistry
from ragnarbot.agent.tools.restart import RestartTool
from ragnarbot.agent.tools.shell import ExecTool
from ragnarbot.agent.tools.spawn import SpawnTool
from ragnarbot.agent.tools.telegram import (
    SendFileTool,
    SendPhotoTool,
    SendVideoTool,
    SetReactionTool,
)
from ragnarbot.agent.tools.update import UpdateTool
from ragnarbot.agent.tools.web import WebFetchTool, WebSearchTool
from ragnarbot.bus.events import InboundMessage, OutboundMessage
from ragnarbot.bus.queue import MessageBus
from ragnarbot.media.manager import MediaManager
from ragnarbot.providers.base import LLMProvider
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

    COMPACT_MIN_MESSAGES = 60
    READ_ONLY_COMMANDS = frozenset({"context_info", "context_mode", "stop"})

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        brave_api_key: str | None = None,
        search_engine: str = "brave",
        exec_config: "ExecToolConfig | None" = None,
        cron_service: "CronService | None" = None,
        stream_steps: bool = False,
        media_manager: MediaManager | None = None,
        debounce_seconds: float = 0.5,
        max_context_tokens: int = 200_000,
        context_mode: str = "normal",
        heartbeat_interval_m: int = 30,
    ):
        from ragnarbot.config.schema import ExecToolConfig
        from ragnarbot.cron.service import CronService
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.brave_api_key = brave_api_key
        self.search_engine = search_engine
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.stream_steps = stream_steps
        self.media_manager = media_manager
        self.debounce_seconds = debounce_seconds
        self.max_context_tokens = max_context_tokens
        self.context_mode = context_mode
        self.cache_manager = CacheManager(max_context_tokens=max_context_tokens)
        self.compactor = Compactor(
            provider=provider,
            cache_manager=self.cache_manager,
            max_context_tokens=max_context_tokens,
            model=self.model,
        )

        self.context = ContextBuilder(workspace, heartbeat_interval_m=heartbeat_interval_m)
        self.context.model = self.model
        self.sessions = SessionManager(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            brave_api_key=brave_api_key,
            search_engine=search_engine,
            exec_config=self.exec_config,
        )

        self.bg_processes = BackgroundProcessManager(
            bus=bus, workspace=workspace, exec_config=self.exec_config,
        )

        self._running = False
        self._restart_requested = False
        self._stop_events: dict[str, asyncio.Event] = {}
        self._processing_session_key: str | None = None
        self.last_active_chat: tuple[str, str] | None = None
        self._register_default_tools()
    
    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        # File tools
        self.tools.register(ReadFileTool(model=self.model))
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
        self.tools.register(WebSearchTool(engine=self.search_engine, api_key=self.brave_api_key))
        self.tools.register(WebFetchTool())
        
        # Telegram media & reaction tools
        send_cb = self.bus.publish_outbound
        self.tools.register(SendPhotoTool(send_callback=send_cb))
        self.tools.register(SendVideoTool(send_callback=send_cb))
        self.tools.register(SendFileTool(send_callback=send_cb))
        self.tools.register(SetReactionTool(send_callback=send_cb))

        # Spawn tool (for subagents)
        spawn_tool = SpawnTool(manager=self.subagents)
        self.tools.register(spawn_tool)
        
        # Cron tool (for scheduling)
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

        # Heartbeat tool (for managing periodic tasks)
        self.tools.register(HeartbeatTool(workspace=self.workspace))

        # Background execution tools
        self.tools.register(ExecBgTool(manager=self.bg_processes))
        self.tools.register(PollTool(manager=self.bg_processes))
        self.tools.register(OutputTool(manager=self.bg_processes))
        self.tools.register(KillTool(manager=self.bg_processes))
        self.tools.register(DismissTool(manager=self.bg_processes))

        # Download file tool (for lazy file downloads)
        if self.media_manager:
            self.tools.register(DownloadFileTool(self.media_manager))

        # Config and restart tools
        self.tools.register(ConfigTool(agent=self))
        self.tools.register(RestartTool(agent=self))
        self.tools.register(UpdateTool(agent=self))

    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        self._processing_task: asyncio.Task | None = None
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(
                    self.bus.consume_inbound(), timeout=1.0,
                )
            except asyncio.TimeoutError:
                self._reap_processing_task()
                continue

            command = msg.metadata.get("command")

            # Read-only commands: respond immediately, even during processing
            if command in self.READ_ONLY_COMMANDS:
                response = self._handle_command(command, msg)
                if response:
                    if (command != "stop"
                            and self._processing_task
                            and not self._processing_task.done()):
                        response.metadata["keep_typing"] = True
                    await self.bus.publish_outbound(response)
                continue

            # Everything else: wait for active processing first
            await self._await_processing_task()

            # System messages → background task
            if msg.channel == "system":
                self._processing_task = asyncio.create_task(
                    self._process_and_send(msg, system=True),
                )
                continue

            # Mutating commands (new_chat, set_context_mode, compact)
            if command:
                if command == "compact":
                    self._processing_task = asyncio.create_task(
                        self._handle_compact_async(msg),
                    )
                else:
                    response = self._handle_command(command, msg)
                    if response:
                        await self.bus.publish_outbound(response)
                continue

            # Regular messages: debounce, then process in background
            batch = await self._debounce(msg)
            self._processing_task = asyncio.create_task(
                self._process_and_send(batch),
            )
    
    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    @property
    def restart_requested(self) -> bool:
        """Whether a restart has been requested."""
        return self._restart_requested

    def request_restart(self) -> None:
        """Schedule a graceful restart after the current response completes."""
        self._restart_requested = True
        logger.info("Restart requested — will restart after current processing completes")

    def _is_stopped(self, session_key: str) -> bool:
        event = self._stop_events.get(session_key)
        return event is not None and event.is_set()

    def _request_stop(self, session_key: str) -> bool:
        """Returns True if there was something to stop."""
        if (self._processing_task and not self._processing_task.done()
                and self._processing_session_key == session_key):
            event = self._stop_events.get(session_key)
            if event:
                event.set()
                return True
        return False

    def _clear_stop(self, session_key: str):
        """Reset stop state with a fresh (unset) event for this session."""
        self._stop_events[session_key] = asyncio.Event()

    async def _chat_or_stop(self, session_key: str, **chat_kwargs):
        """Race provider.chat() against the stop event.

        Returns the LLM response, or None if stopped mid-call.
        """
        event = self._stop_events.get(session_key)
        chat_task = asyncio.create_task(self.provider.chat(**chat_kwargs))

        if not event:
            return await chat_task

        stop_task = asyncio.create_task(event.wait())
        done, pending = await asyncio.wait(
            [chat_task, stop_task], return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        if stop_task in done:
            # Consume result to avoid unhandled-exception warnings
            if chat_task in done:
                try:
                    chat_task.result()
                except Exception:
                    pass
            return None
        return chat_task.result()

    def _reap_processing_task(self):
        """Check for completed/failed background task."""
        if self._processing_task and self._processing_task.done():
            try:
                self._processing_task.result()
            except Exception as e:
                logger.error(f"Processing task error: {e}")
            self._processing_task = None
            if self._restart_requested:
                self._running = False

    async def _await_processing_task(self):
        """Wait for any active processing to complete."""
        if self._processing_task and not self._processing_task.done():
            try:
                await self._processing_task
            except Exception as e:
                logger.error(f"Processing task error: {e}")
            self._processing_task = None
        self._reap_processing_task()

    async def _process_and_send(self, batch_or_msg, system=False):
        """Run processing and publish response (background task wrapper)."""
        if system:
            msg = batch_or_msg
            parts = msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
            session_key = f"{parts[0]}:{parts[1]}"
        else:
            batch = batch_or_msg
            msg = batch[0]
            session_key = f"{msg.channel}:{msg.chat_id}"

        self._processing_session_key = session_key
        self._clear_stop(session_key)

        try:
            if system:
                try:
                    response = await self._process_system_message(msg)
                    if response:
                        await self.bus.publish_outbound(response)
                    else:
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=parts[0],
                            chat_id=parts[1],
                            content="",
                            metadata={"stop_typing": True},
                        ))
                except Exception as e:
                    logger.error(f"Error processing system message: {e}")
            else:
                try:
                    response = await self._process_batch(batch)
                    if response:
                        await self.bus.publish_outbound(response)
                    else:
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="",
                            metadata={"stop_typing": True},
                        ))
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"Sorry, I encountered an error: {str(e)}"
                    ))
        finally:
            self._processing_session_key = None
            self._stop_events.pop(session_key, None)

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

            # Read-only commands: respond immediately, keep debouncing
            command = msg.metadata.get("command")
            if command in self.READ_ONLY_COMMANDS:
                response = self._handle_command(command, msg)
                if response:
                    await self.bus.publish_outbound(response)
                continue

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
        contexts.  Each message gets its own timestamp prefix so the
        LLM sees them as distinct inputs but responds once.

        System messages and commands are dispatched by ``run()`` before
        reaching this method.

        Args:
            batch: One or more inbound messages from the same session.

        Returns:
            The response message, or None if no response needed.
        """
        from datetime import datetime as _dt
        from ragnarbot.session.manager import _build_message_prefix

        msg = batch[0]

        if msg.channel != "cli":
            self.last_active_chat = (msg.channel, msg.chat_id)

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
        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(msg.channel, msg.chat_id)

        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(msg.channel, msg.chat_id)

        exec_bg_tool = self.tools.get("exec_bg")
        if isinstance(exec_bg_tool, ExecBgTool):
            exec_bg_tool.set_context(msg.channel, msg.chat_id)

        poll_tool = self.tools.get("poll")
        if isinstance(poll_tool, PollTool):
            poll_tool.set_context(msg.channel, msg.chat_id)

        restart_tool = self.tools.get("restart")
        if isinstance(restart_tool, RestartTool):
            restart_tool.set_context(msg.channel, msg.chat_id)

        update_tool = self.tools.get("update")
        if isinstance(update_tool, UpdateTool):
            update_tool.set_context(msg.channel, msg.chat_id)

        download_tool = self.tools.get("download_file")
        if isinstance(download_tool, DownloadFileTool):
            download_tool.set_context(msg.channel, session.key)

        # Telegram media tools
        last_message_id = batch[-1].metadata.get("message_id")
        for tool_name in ("send_photo", "send_video", "send_file"):
            tool = self.tools.get(tool_name)
            if tool and hasattr(tool, "set_context"):
                tool.set_context(msg.channel, msg.chat_id)
        reaction_tool = self.tools.get("set_reaction")
        if isinstance(reaction_tool, SetReactionTool):
            reaction_tool.set_context(msg.channel, msg.chat_id, last_message_id)

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

            # Append ephemeral system note (visible to LLM only, not saved to session)
            system_note = m.metadata.get("system_note")
            if system_note:
                prefixed_content += f"\n\n{system_note}"

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
        session_key = f"{msg.channel}:{msg.chat_id}"
        final_content = None
        compacted_this_turn = False
        stopped = False

        try:
            while True:

                # CHECKPOINT 1 — before LLM call
                if self._is_stopped(session_key):
                    logger.info(f"Stop requested before LLM call for {session_key}")
                    stopped = True
                    break

                # Cache flush escalation (if TTL expired)
                flushed = False
                if self.cache_manager.should_flush(session, self.model):
                    self.cache_manager.flush_messages(
                        messages, session, model=self.model,
                        tools=self.tools.get_definitions(),
                        context_mode=self.context_mode,
                    )
                    flushed = True

                # Auto-compaction check (max once per turn)
                if not compacted_this_turn and self.compactor.should_compact(
                    messages, self.context_mode,
                    tools=self.tools.get_definitions(),
                    session=session,
                ):
                    messages, new_start = await self.compactor.compact(
                        session=session,
                        context_mode=self.context_mode,
                        context_builder=self.context,
                        messages=messages,
                        new_start=new_start,
                        tools=self.tools.get_definitions(),
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        session_metadata=session.metadata,
                    )
                    compacted_this_turn = True

                # Re-apply previous flush to history messages so the API
                # sees the same effective size that was estimated.
                # (skip if flush_messages just ran — it already trimmed everything)
                if not flushed:
                    self.cache_manager.apply_previous_flush(messages, session)

                # Safety: force flush if context still exceeds API limit
                # (e.g. new tool results grew within this turn)
                tools_defs = self.tools.get_definitions()
                actual_tokens = self.cache_manager.estimate_context_tokens(
                    messages, self.model, tools=tools_defs,
                )
                if actual_tokens > self.max_context_tokens:
                    flush_type = "extra_hard" if self.context_mode == "eco" else "hard"
                    logger.warning(
                        f"Safety flush ({flush_type}): {actual_tokens} tokens "
                        f"exceed {self.max_context_tokens} limit"
                    )
                    CacheManager._flush_tool_results(messages, flush_type)

                # Strip internal _ts metadata before sending to API
                api_messages = [
                    {k: v for k, v in m.items() if k != "_ts"} for m in messages
                ]
                response = await self._chat_or_stop(
                    session_key,
                    messages=api_messages,
                    tools=tools_defs,
                    model=self.model,
                )

                # LLM call cancelled by stop
                if response is None:
                    logger.info(f"LLM call cancelled by stop for {session_key}")
                    stopped = True
                    break

                # Track cache creation/read for flush scheduling
                self.cache_manager.mark_cache_created(session, response.usage)

                # Stop check after LLM returns (covers final text response)
                if self._is_stopped(session_key):
                    logger.info(f"Stop requested after LLM call for {session_key}")
                    stopped = True
                    break

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

                    for idx, tool_call in enumerate(response.tool_calls):
                        # Stop check before each individual tool execution
                        if self._is_stopped(session_key):
                            logger.info(
                                f"Stop requested during tool execution for {session_key}"
                            )
                            stopped = True
                            for remaining in response.tool_calls[idx:]:
                                messages = self.context.add_tool_result(
                                    messages, remaining.id, remaining.name,
                                    "[Stopped by user]"
                                )
                            break

                        args_str = json.dumps(tool_call.arguments)
                        logger.debug(f"Executing tool: {tool_call.name} with arguments: {args_str}")
                        result = await self.tools.execute(tool_call.name, tool_call.arguments)
                        messages = self.context.add_tool_result(
                            messages, tool_call.id, tool_call.name, result
                        )

                    if stopped:
                        break
                else:
                    final_content = response.content
                    break
        finally:
            # Persist cache metadata even if tool execution throws, so
            # should_flush() sees the correct created_at on the next turn.
            self.sessions.save(session)

        if not stopped:
            messages.append({"role": "assistant", "content": final_content or ""})
        else:
            messages.append({"role": "user", "content": "[Stopped by user]"})

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

        if stopped or not final_content:
            return None

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content
        )
    
    def _handle_command(self, command: str, msg: InboundMessage) -> OutboundMessage | None:
        """Dispatch a channel command without calling the LLM."""
        if command == "new_chat":
            return self._handle_new_chat(msg)
        if command == "stop":
            return self._handle_stop(msg)
        if command == "context_mode":
            return self._handle_context_mode(msg)
        if command == "set_context_mode":
            return self._handle_set_context_mode(msg)
        if command == "context_info":
            return self._handle_context_info(msg)
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

    def _handle_stop(self, msg: InboundMessage) -> OutboundMessage:
        """Stop the currently running agent response for this session."""
        session_key = f"{msg.channel}:{msg.chat_id}"
        if self._request_stop(session_key):
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="✋ Agent response stopped",
            )
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="Nothing to stop.",
        )

    def _handle_context_mode(self, msg: InboundMessage) -> OutboundMessage:
        """Show current mode with inline keyboard buttons."""
        mode_labels = {
            "eco": "🌿 eco (40%)",
            "normal": "⚖️ normal (60%)",
            "full": "🔥 full (85%)",
        }
        current = self.context_mode
        text = f"⚙️ <b>Context Mode</b>\n\nCurrent: {mode_labels[current]}"
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=text,
            metadata={
                "raw_html": True,
                "inline_keyboard": [[
                    {"text": "🌿 eco", "callback_data": "ctx_mode:eco"},
                    {"text": "⚖️ normal", "callback_data": "ctx_mode:normal"},
                    {"text": "🔥 full", "callback_data": "ctx_mode:full"},
                ]],
            },
        )

    def _handle_set_context_mode(self, msg: InboundMessage) -> OutboundMessage | None:
        """Update context mode (from callback query)."""
        mode = msg.metadata.get("context_mode")
        if mode not in ("eco", "normal", "full"):
            return None

        self.context_mode = mode
        from ragnarbot.config.loader import load_config, save_config
        config = load_config()
        config.agents.defaults.context_mode = mode
        save_config(config)

        mode_labels = {
            "eco": "🌿 eco (40%)",
            "normal": "⚖️ normal (60%)",
            "full": "🔥 full (85%)",
        }
        text = f"✅ Context mode set to: {mode_labels[mode]}"
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=text,
            metadata={
                "raw_html": True,
                "edit_message_id": msg.metadata.get("callback_message_id"),
            },
        )

    def _handle_context_info(self, msg: InboundMessage) -> OutboundMessage:
        """Show context usage info."""
        tokens = self.get_context_tokens(
            f"{msg.channel}:{msg.chat_id}", msg.channel, msg.chat_id
        )
        threshold = Compactor.THRESHOLDS.get(self.context_mode, 0.60)
        effective_max = int(self.max_context_tokens * threshold)
        pct = min(int(tokens / effective_max * 100), 100) if effective_max > 0 else 0
        tokens_k = f"{tokens // 1000}k"
        max_k = f"{effective_max // 1000}k"

        session = self.sessions.get_or_create(f"{msg.channel}:{msg.chat_id}")
        compactions = sum(
            1 for m in session.messages
            if m.get("metadata", {}).get("type") == "compaction"
        )

        mode_labels = {"eco": "🌿 eco", "normal": "⚖️ normal", "full": "🔥 full"}
        text = (
            f"📊 <b>Context</b>\n\n"
            f"🤖 <code>{self.model}</code>\n"
            f"📦 {mode_labels[self.context_mode]} ({int(threshold * 100)}%)\n\n"
            f"📈 Usage: <b>{pct}%</b>  ({tokens_k} / {max_k})\n"
            f"💾 Compactions: {compactions}"
        )
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=text,
            metadata={"raw_html": True},
        )

    async def _handle_compact_async(self, msg: InboundMessage) -> None:
        """Handle /compact command — forced compaction as a blocking task."""
        session_key = f"{msg.channel}:{msg.chat_id}"
        session = self.sessions.get_or_create(session_key)

        # Check minimum threshold — count only messages after last compaction
        last_idx = self.compactor._find_last_compaction_idx(session.messages)
        after_compaction = len(session.messages) - (last_idx + 1 if last_idx is not None else 0)

        if after_compaction < self.COMPACT_MIN_MESSAGES:
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=(
                    f"📦 Not enough new messages to compact "
                    f"({after_compaction}/{self.COMPACT_MIN_MESSAGES})"
                ),
            ))
            return

        # Check compactable range (messages between last compaction and tail)
        compact_start = last_idx if last_idx is not None else 0
        tail_count = self.compactor._determine_tail(session.messages)
        compact_end = len(session.messages) - tail_count
        compactable = compact_end - compact_start

        if compactable <= 0:
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="📦 Nothing to compact — all messages are in the tail.",
            ))
            return

        # Send "compacting" status message
        logger.info(
            f"Manual compaction started for {session_key}: "
            f"{compactable} messages to compact, {tail_count} in tail"
        )
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=f"📦 Compacting {compactable} messages...",
            metadata={"keep_typing": True},
        ))

        # Send typing indicator
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="",
            metadata={"chat_action": "typing"},
        ))

        # Build messages (system + history, no current message)
        messages = self.context.build_messages(
            history=session.get_history(),
            channel=msg.channel,
            chat_id=msg.chat_id,
            session_key=session.key,
            session_metadata=session.metadata,
        )

        compactions_before = sum(
            1 for m in session.messages
            if m.get("metadata", {}).get("type") == "compaction"
        )

        # Run compaction
        try:
            messages, _ = await self.compactor.compact(
                session=session,
                context_mode=self.context_mode,
                context_builder=self.context,
                messages=messages,
                new_start=len(messages),
                tools=self.tools.get_definitions(),
                channel=msg.channel,
                chat_id=msg.chat_id,
                session_metadata=session.metadata,
            )
        except Exception as e:
            logger.error(f"Manual compaction failed for {session_key}: {e}")
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=f"❌ Compaction failed: {e}",
            ))
            return

        compactions_after = sum(
            1 for m in session.messages
            if m.get("metadata", {}).get("type") == "compaction"
        )

        self.sessions.save(session)

        if compactions_after > compactions_before:
            logger.info(f"Manual compaction completed for {session_key}")
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="✅ Conversation compacted",
            ))
        else:
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="📦 Compaction skipped — nothing to compact.",
            ))

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
        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(origin_channel, origin_chat_id)

        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(origin_channel, origin_chat_id)

        exec_bg_tool = self.tools.get("exec_bg")
        if isinstance(exec_bg_tool, ExecBgTool):
            exec_bg_tool.set_context(origin_channel, origin_chat_id)

        poll_tool = self.tools.get("poll")
        if isinstance(poll_tool, PollTool):
            poll_tool.set_context(origin_channel, origin_chat_id)

        restart_tool = self.tools.get("restart")
        if isinstance(restart_tool, RestartTool):
            restart_tool.set_context(origin_channel, origin_chat_id)

        update_tool = self.tools.get("update")
        if isinstance(update_tool, UpdateTool):
            update_tool.set_context(origin_channel, origin_chat_id)

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

        # Agent loop
        final_content = None
        compacted_this_turn = False
        stopped = False

        try:
            while True:

                # CHECKPOINT 1 — before LLM call
                if self._is_stopped(session_key):
                    logger.info(f"Stop requested before LLM call for {session_key}")
                    stopped = True
                    break

                # Cache flush escalation (if TTL expired)
                flushed = False
                if self.cache_manager.should_flush(session, self.model):
                    self.cache_manager.flush_messages(
                        messages, session, model=self.model,
                        tools=self.tools.get_definitions(),
                        context_mode=self.context_mode,
                    )
                    flushed = True

                # Auto-compaction check (max once per turn)
                if not compacted_this_turn and self.compactor.should_compact(
                    messages, self.context_mode,
                    tools=self.tools.get_definitions(),
                    session=session,
                ):
                    messages, new_start = await self.compactor.compact(
                        session=session,
                        context_mode=self.context_mode,
                        context_builder=self.context,
                        messages=messages,
                        new_start=new_start,
                        tools=self.tools.get_definitions(),
                        channel=origin_channel,
                        chat_id=origin_chat_id,
                        session_metadata=session.metadata,
                    )
                    compacted_this_turn = True

                # Re-apply previous flush to history messages
                if not flushed:
                    self.cache_manager.apply_previous_flush(messages, session)

                # Safety: force flush if context still exceeds API limit
                tools_defs = self.tools.get_definitions()
                actual_tokens = self.cache_manager.estimate_context_tokens(
                    messages, self.model, tools=tools_defs,
                )
                if actual_tokens > self.max_context_tokens:
                    flush_type = "extra_hard" if self.context_mode == "eco" else "hard"
                    logger.warning(
                        f"Safety flush ({flush_type}): {actual_tokens} tokens "
                        f"exceed {self.max_context_tokens} limit"
                    )
                    CacheManager._flush_tool_results(messages, flush_type)

                # Strip internal _ts metadata before sending to API
                api_messages = [
                    {k: v for k, v in m.items() if k != "_ts"} for m in messages
                ]
                response = await self._chat_or_stop(
                    session_key,
                    messages=api_messages,
                    tools=tools_defs,
                    model=self.model,
                )

                # LLM call cancelled by stop
                if response is None:
                    logger.info(f"LLM call cancelled by stop for {session_key}")
                    stopped = True
                    break

                # Track cache creation/read for flush scheduling
                self.cache_manager.mark_cache_created(session, response.usage)

                # Stop check after LLM returns (covers final text response)
                if self._is_stopped(session_key):
                    logger.info(f"Stop requested after LLM call for {session_key}")
                    stopped = True
                    break

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

                    for idx, tool_call in enumerate(response.tool_calls):
                        # Stop check before each individual tool execution
                        if self._is_stopped(session_key):
                            logger.info(
                                f"Stop requested during tool execution for {session_key}"
                            )
                            stopped = True
                            for remaining in response.tool_calls[idx:]:
                                messages = self.context.add_tool_result(
                                    messages, remaining.id, remaining.name,
                                    "[Stopped by user]"
                                )
                            break

                        args_str = json.dumps(tool_call.arguments)
                        logger.debug(f"Executing tool: {tool_call.name} with arguments: {args_str}")
                        result = await self.tools.execute(tool_call.name, tool_call.arguments)
                        messages = self.context.add_tool_result(
                            messages, tool_call.id, tool_call.name, result
                        )

                    if stopped:
                        break
                else:
                    final_content = response.content
                    break
        finally:
            self.sessions.save(session)

        if not stopped:
            # Add final assistant message to the messages list
            messages.append({"role": "assistant", "content": final_content or ""})

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

        if stopped or not final_content:
            return None

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

    def _build_isolated_tool_registry(
        self, channel: str, chat_id: str,
    ) -> tuple[ToolRegistry, DeliverResultTool]:
        """Build a fresh tool registry for an isolated cron job.

        Each invocation creates new tool instances so concurrent isolated jobs
        share no mutable state.  The ``spawn`` tool is excluded
        (it doesn't make sense in non-interactive mode).
        """
        reg = ToolRegistry()

        # File tools
        reg.register(ReadFileTool(model=self.model))
        reg.register(WriteFileTool())
        reg.register(EditFileTool())
        reg.register(ListDirTool())

        # Shell
        reg.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.exec_config.restrict_to_workspace,
        ))

        # Web
        reg.register(WebSearchTool(engine=self.search_engine, api_key=self.brave_api_key))
        reg.register(WebFetchTool())

        # Telegram media & reaction
        send_cb = self.bus.publish_outbound
        photo_tool = SendPhotoTool(send_callback=send_cb)
        photo_tool.set_context(channel, chat_id)
        reg.register(photo_tool)

        video_tool = SendVideoTool(send_callback=send_cb)
        video_tool.set_context(channel, chat_id)
        reg.register(video_tool)

        file_tool = SendFileTool(send_callback=send_cb)
        file_tool.set_context(channel, chat_id)
        reg.register(file_tool)

        reaction_tool = SetReactionTool(send_callback=send_cb)
        reaction_tool.set_context(channel, chat_id)
        reg.register(reaction_tool)

        # Cron
        if self.cron_service:
            cron_tool = CronTool(self.cron_service)
            cron_tool.set_context(channel, chat_id)
            reg.register(cron_tool)

        # Background execution
        bg = BackgroundProcessManager(
            bus=self.bus, workspace=self.workspace, exec_config=self.exec_config,
        )
        exec_bg = ExecBgTool(manager=bg)
        exec_bg.set_context(channel, chat_id)
        reg.register(exec_bg)

        poll = PollTool(manager=bg)
        poll.set_context(channel, chat_id)
        reg.register(poll)
        reg.register(OutputTool(manager=bg))
        reg.register(KillTool(manager=bg))
        reg.register(DismissTool(manager=bg))

        # Config / restart / update
        config_t = ConfigTool(agent=self)
        reg.register(config_t)
        restart_t = RestartTool(agent=self)
        restart_t.set_context(channel, chat_id)
        reg.register(restart_t)
        update_t = UpdateTool(agent=self)
        update_t.set_context(channel, chat_id)
        reg.register(update_t)

        # Download file
        if self.media_manager:
            dl = DownloadFileTool(self.media_manager)
            dl.set_context(channel, f"{channel}:{chat_id}")
            reg.register(dl)

        # deliver_result — isolated-only
        deliver_tool = DeliverResultTool()
        reg.register(deliver_tool)

        return reg, deliver_tool

    async def process_cron_isolated(
        self,
        job_name: str,
        message: str,
        schedule_desc: str,
        channel: str,
        chat_id: str,
    ) -> str | None:
        """Run an isolated cron job — fresh context, no session history.

        Returns the result string (from deliver_result or final LLM text),
        or None if the agent produced no output.
        """
        tools, deliver_tool = self._build_isolated_tool_registry(channel, chat_id)

        session_metadata = {
            "cron_isolated": {
                "job_name": job_name,
                "schedule_desc": schedule_desc,
                "task_message": message,
            },
        }

        messages = self.context.build_messages(
            history=[],
            current_message=message,
            channel=channel,
            chat_id=chat_id,
            session_metadata=session_metadata,
        )

        max_iterations = 20
        for _ in range(max_iterations):
            tools_defs = tools.get_definitions()
            api_messages = [
                {k: v for k, v in m.items() if k != "_ts"} for m in messages
            ]
            response = await self.provider.chat(
                messages=api_messages, tools=tools_defs, model=self.model,
            )

            if response.has_tool_calls:
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                )

                for tc in response.tool_calls:
                    result = await tools.execute(tc.name, tc.arguments)
                    messages = self.context.add_tool_result(
                        messages, tc.id, tc.name, result,
                    )

                # If deliver_result was called, return immediately
                if deliver_tool.result is not None:
                    return deliver_tool.result
            else:
                # Agent finished with text — use as fallback result
                return response.content or None

        # Exhausted iterations — return whatever deliver_tool captured
        return deliver_tool.result

    def _build_heartbeat_tool_registry(
        self,
        channel: str,
        chat_id: str,
    ) -> tuple[ToolRegistry, DeliverResultTool, HeartbeatDoneTool]:
        """Build a tool registry for heartbeat execution.

        Extends the isolated registry with HeartbeatTool and HeartbeatDoneTool.
        """
        reg, deliver_tool = self._build_isolated_tool_registry(channel, chat_id)

        done_tool = HeartbeatDoneTool()
        reg.register(done_tool)

        heartbeat_tool = HeartbeatTool(workspace=self.workspace)
        reg.register(heartbeat_tool)

        return reg, deliver_tool, done_tool

    async def process_heartbeat(self) -> tuple[str | None, str | None, str | None]:
        """Run a heartbeat check — isolated context with rolling session.

        Returns (result, channel, chat_id):
            - result: content from deliver_result, or None if heartbeat_done
            - channel, chat_id: last active chat for delivery (None if no active chat)
        """
        channel, chat_id = self.last_active_chat or (None, None)
        tools, deliver_tool, done_tool = self._build_heartbeat_tool_registry(
            channel or "cli", chat_id or "direct",
        )

        # Load rolling session
        session = self.sessions.get_or_create("heartbeat:isolated")

        # Build tasks summary from HEARTBEAT.md
        hb_path = self.workspace / "HEARTBEAT.md"
        tasks_summary = "No tasks."
        if hb_path.exists():
            content = hb_path.read_text(encoding="utf-8")
            blocks = parse_blocks(content)
            if blocks:
                lines = [f"- [{b['id']}] {b['message'][:50]}" for b in blocks]
                tasks_summary = "\n".join(lines)

        session_metadata = {
            "heartbeat_isolated": {
                "tasks_summary": tasks_summary,
            },
        }

        messages = self.context.build_messages(
            history=session.get_history(),
            current_message="Execute heartbeat check.",
            channel=channel,
            chat_id=chat_id,
            session_metadata=session_metadata,
        )

        # Track where new messages start (for session persistence)
        new_start = len(messages) - 1  # the user message we just added

        max_iterations = 20
        result = None
        for _ in range(max_iterations):
            tools_defs = tools.get_definitions()

            # Safety flush: only if context exceeds 80% of max (unlikely
            # in a single run, but guards against extreme cases). During
            # normal execution, full tool results are preserved — the real
            # trimming happens after the run in _trim_heartbeat_session.
            safety_limit = int(self.max_context_tokens * 0.8)
            actual_tokens = self.cache_manager.estimate_context_tokens(
                messages, self.model, tools=tools_defs,
            )
            if actual_tokens > safety_limit:
                logger.warning(
                    f"Heartbeat safety flush: {actual_tokens} tokens "
                    f"exceed {safety_limit} safety limit"
                )
                CacheManager._flush_tool_results(messages, "hard")

            api_messages = [
                {k: v for k, v in m.items() if k != "_ts"} for m in messages
            ]
            response = await self.provider.chat(
                messages=api_messages, tools=tools_defs, model=self.model,
            )

            if response.has_tool_calls:
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                )

                for tc in response.tool_calls:
                    tc_result = await tools.execute(tc.name, tc.arguments)
                    messages = self.context.add_tool_result(
                        messages, tc.id, tc.name, tc_result,
                    )

                if deliver_tool.result is not None:
                    result = deliver_tool.result
                    break
                if done_tool.done:
                    result = None
                    break
            else:
                # Text response — treat as done with no result
                messages.append({"role": "assistant", "content": response.content or ""})
                result = None
                break

        # Save new messages to rolling session
        for m in messages[new_start:]:
            extras: dict[str, Any] = {}
            if "tool_calls" in m:
                extras["tool_calls"] = m["tool_calls"]
            if "tool_call_id" in m:
                extras["tool_call_id"] = m["tool_call_id"]
            if "name" in m:
                extras["name"] = m["name"]
            session.add_message(m["role"], m.get("content"), **extras)
        self.sessions.save(session)

        # Trim rolling session to stay within budget
        self._trim_heartbeat_session(session)

        return result, channel, chat_id

    def _trim_heartbeat_session(self, session, max_tokens: int = 20_000) -> None:
        """Trim heartbeat session to stay under max_tokens.

        Flushes tool results first so the token count reflects what will
        actually be sent to the API on the next heartbeat run.
        """
        from ragnarbot.agent.tokens import estimate_messages_tokens

        provider = self.cache_manager.get_provider_from_model(self.model)
        history = session.get_history()

        # Flush tool results before counting — these will be flushed by
        # the safety check on the next run anyway, so counting them at
        # full size would cause us to discard useful messages prematurely.
        CacheManager._flush_tool_results(history, "hard")

        total = estimate_messages_tokens(history, provider)
        if total <= max_tokens:
            # Still save — flush may have shrunk tool results
            self._rebuild_session(session, history)
            return

        # Remove oldest messages, keeping tool-call groups intact
        while total > max_tokens and history:
            msg = history[0]
            # If this is a tool result, also remove the preceding assistant
            # message with matching tool_calls (already removed or not present
            # at index 0). Just remove the oldest message.
            history.pop(0)

            # If we removed an assistant message with tool_calls, also remove
            # all its subsequent tool results
            if msg.get("tool_calls"):
                tool_call_ids = {
                    tc.get("id") for tc in msg.get("tool_calls", [])
                }
                while history and history[0].get("tool_call_id") in tool_call_ids:
                    history.pop(0)

            total = estimate_messages_tokens(history, provider)

        self._rebuild_session(session, history)

    def _rebuild_session(self, session, history: list[dict]) -> None:
        """Rebuild and save session messages from a processed history list."""
        session.messages = []
        for m in history:
            extras: dict[str, Any] = {}
            if "tool_calls" in m:
                extras["tool_calls"] = m["tool_calls"]
            if "tool_call_id" in m:
                extras["tool_call_id"] = m["tool_call_id"]
            if "name" in m:
                extras["name"] = m["name"]
            session.add_message(m["role"], m.get("content"), **extras)
        self.sessions.save(session)

    def get_context_tokens(
        self,
        session_key: str,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> int:
        """Estimate current context token usage for a session.

        Builds system prompt + history (without a current message) and
        returns the effective token count, accounting for any previous
        flush state stored in the session.

        Read-only: does not create a session if one doesn't exist.

        Args:
            session_key: User routing key (e.g. "telegram:12345").
            channel: Channel name (for system prompt context).
            chat_id: Chat ID (for system prompt context).

        Returns:
            Estimated token count.
        """
        if channel is None and ":" in session_key:
            channel, chat_id = session_key.split(":", 1)

        active_id = self.sessions.get_active_id(session_key)
        if not active_id:
            messages = self.context.build_messages(
                history=[], channel=channel, chat_id=chat_id,
            )
            return self.cache_manager.estimate_context_tokens(
                messages, self.model, tools=self.tools.get_definitions(),
            )

        session = self.sessions._load(active_id, session_key)
        if not session:
            # Stale pointer — treat as empty session
            messages = self.context.build_messages(
                history=[], channel=channel, chat_id=chat_id,
            )
            return self.cache_manager.estimate_context_tokens(
                messages, self.model, tools=self.tools.get_definitions(),
            )

        history = session.get_history()

        # Count image refs before build_messages resolves them
        image_count = sum(
            len(m.get("media_refs", []))
            for m in history if m.get("role") == "user"
        )

        messages = self.context.build_messages(
            history=history,
            channel=channel,
            chat_id=chat_id,
            session_metadata=session.metadata,
        )
        tools = self.tools.get_definitions()
        tokens = self.cache_manager.estimate_context_tokens(
            messages, self.model,
            tools=tools,
            session=session,
        )

        # If a flush is pending, simulate it for accurate estimation
        if self.cache_manager.should_flush(session, self.model):
            ratio = tokens / self.max_context_tokens
            if self.context_mode == "eco":
                flush_type = "extra_hard"
            else:
                flush_type = "soft" if ratio <= CacheManager.HARD_FLUSH_RATIO else "hard"
            sim_messages = [m.copy() for m in messages]
            CacheManager._flush_tool_results(sim_messages, flush_type)
            tokens = self.cache_manager.estimate_context_tokens(
                sim_messages, self.model, tools=tools,
            )

        # Add image tokens without disk I/O (no base64 resolution needed)
        if image_count:
            from ragnarbot.agent.tokens import estimate_image_tokens
            provider = self.cache_manager.get_provider_from_model(self.model)
            tokens += image_count * estimate_image_tokens(provider)

        return tokens


def _ext_from_mime(mime_type: str) -> str:
    """Extract a short extension from a MIME type."""
    mapping = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
    }
    return mapping.get(mime_type, "jpg")
